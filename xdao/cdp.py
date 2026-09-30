"""CDP（Chrome DevTools Protocol）通道：纯标准库的 WebSocket 客户端 + 一条会话。

这个模块只关心「怎么和调试端口说话」，**不关心连的是哪个站、要办什么事**：
浏览器登录（``xdao.browser_login``）与 PDF 渲染（``xdao.exporters.pdf``）
共用它，因此这里没有任何登录相关的常量、也不会 import 业务模块。

分层（自上而下）：

1. 帧层：:func:`build_frame` / :func:`read_frame` / :class:`_FrameReader`
   —— RFC 6455 的收发，脱离 socket 也能测（传一个「读满 n 字节」的函数即可）；
2. 握手层：:func:`_ws_handshake` / :func:`_split_ws_url` / :func:`_accept_key`
   —— 手写握手是为了不引入任何第三方依赖；
3. 会话层：:class:`CDPSession` —— 一个专职读线程按报文 id 把应答投给等待者，
   外部线程只碰队列、不碰 socket；``call()`` 是唯一的出口。

为什么要自己写：项目一直保持纯标准库（打包体积、离线可用、不受依赖升级影响）。
"""

from __future__ import annotations

import base64
import hashlib
import itertools
import json
import queue
import secrets
import socket
import struct
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Callable

# 页面等待：浏览器先把调试端口写进 DevToolsActivePort、再加载命令行给的地址，
# 端口文件一出现就连，页面列表里可能只有一个空标签，所以给页面几秒开出来。
_SITE_WAIT = 8.0
_POLL_INTERVAL = 0.2

_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

OPCODE_CONTINUATION = 0x0
OPCODE_TEXT = 0x1
OPCODE_BINARY = 0x2
OPCODE_CLOSE = 0x8
OPCODE_PING = 0x9
OPCODE_PONG = 0xA


class CdpError(Exception):
    """和调试端口打交道时出的错。"""


# 老名字：这个模块从 ``browser_login`` 里搬出来之前就叫这个，
# 保留别名是为了既有调用方与测试不用改。
BrowserLoginError = CdpError


@dataclass(frozen=True)
class Frame:
    """一个 WebSocket 帧。"""

    fin: bool
    opcode: int
    payload: bytes


# ---------------------------------------------------------------- 帧

def _accept_key(key: str) -> str:
    """算出握手应答里的 Sec-WebSocket-Accept（RFC 6455）。"""
    digest = hashlib.sha1((key + _WS_GUID).encode("ascii")).digest()
    return base64.b64encode(digest).decode("ascii")


def _split_ws_url(ws_url: str) -> tuple[str, int, str]:
    """把 ``ws://主机:端口/路径`` 拆成三段。"""
    parts = urllib.parse.urlsplit(ws_url)
    if parts.scheme != "ws" or not parts.hostname:
        raise CdpError(f"调试地址不是合法的 WebSocket 地址：{ws_url}")
    path = parts.path or "/"
    if parts.query:
        path = f"{path}?{parts.query}"
    return parts.hostname, parts.port or 80, path


def build_frame(
    payload: bytes,
    opcode: int = OPCODE_TEXT,
    mask: bytes | None = None,
    fin: bool = True,
) -> bytes:
    """拼一个客户端帧。

    RFC 要求客户端发出的每一帧都要掩码；``mask`` 只为测试能固定字节而留。
    """
    if mask is None:
        mask = secrets.token_bytes(4)
    if len(mask) != 4:
        raise CdpError("WebSocket 掩码必须是 4 字节。")
    header = bytearray([(0x80 if fin else 0x00) | opcode])
    length = len(payload)
    if length < 126:
        header.append(0x80 | length)
    elif length < 1 << 16:
        header.append(0x80 | 126)
        header += struct.pack(">H", length)
    else:
        header.append(0x80 | 127)
        header += struct.pack(">Q", length)
    header += mask
    return bytes(header) + bytes(
        byte ^ mask[index % 4] for index, byte in enumerate(payload)
    )


def read_frame(read_exact: Callable[[int], bytes]) -> Frame:
    """读一帧。

    参数是「读满 n 个字节」的函数，这样帧层能脱离 socket 单独测
    （用 socketpair 造一对假连接即可）。服务端的帧本不带掩码，
    真带了也照着解，兼容性白捡。
    """
    first, second = read_exact(2)
    fin = bool(first & 0x80)
    opcode = first & 0x0F
    masked = bool(second & 0x80)
    length = second & 0x7F
    if length == 126:
        (length,) = struct.unpack(">H", read_exact(2))
    elif length == 127:
        (length,) = struct.unpack(">Q", read_exact(8))
    mask = read_exact(4) if masked else b""
    payload = read_exact(length) if length else b""
    if masked:
        payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
    return Frame(fin=fin, opcode=opcode, payload=payload)


class _FrameReader:
    """把 socket 上的连续字节切成帧。

    握手时多读出来的余包要先塞回缓冲区，否则第一帧当场就丢了。
    """

    def __init__(self, sock: socket.socket, initial: bytes = b"") -> None:
        self._sock = sock
        self._buffer = bytearray(initial)

    def read_exact(self, count: int) -> bytes:
        while len(self._buffer) < count:
            chunk = self._sock.recv(65536)
            if not chunk:
                raise CdpError("调试连接被浏览器关闭了。")
            self._buffer += chunk
        data = bytes(self._buffer[:count])
        del self._buffer[:count]
        return data

    def read_frame(self) -> Frame:
        return read_frame(self.read_exact)


# ---------------------------------------------------------------- HTTP 与握手

def _http_json(url: str, timeout: float = 5.0) -> object:
    """读一个本地 HTTP 接口（CDP 的 ``/json/list``）。

    显式关掉代理：调试端口在 127.0.0.1 上，而用户可能开着系统代理，
    走代理会连不上自己机器上的端口。

    实现在这里、``http_json`` 只是它的别名：会话内部调用走的是本模块这个名字，
    这样调用方（或测试）替换 ``_http_json`` 就能换掉「怎么读那个接口」。
    """
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(url, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8", "replace"))
    except (OSError, ValueError) as exc:
        raise CdpError(f"读取浏览器的调试接口失败：{exc}") from exc


# 公开名（``_http_json`` 是历史写法，两者是同一个函数对象）。
http_json = _http_json


def _ws_handshake(sock: socket.socket, ws_url: str, timeout: float = 10.0) -> bytes:
    """完成 WebSocket 握手，返回「握手响应之后可能已经读到的余包」。

    为什么要把余包交回去：服务端常把 101 响应和自己的第一帧写在同一个 TCP 段里，
    直接丢掉会把第一帧吃掉（表现成「发出去的命令永远等不到应答」）。
    """
    host, port, path = _split_ws_url(ws_url)
    key = base64.b64encode(secrets.token_bytes(16)).decode("ascii")
    request = (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: {host}:{port}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        "Sec-WebSocket-Version: 13\r\n\r\n"
    )
    sock.settimeout(timeout)
    sock.sendall(request.encode("ascii"))

    buffer = b""
    while b"\r\n\r\n" not in buffer:
        chunk = sock.recv(4096)
        if not chunk:
            raise CdpError("和浏览器建立调试连接时被对方关闭了。")
        buffer += chunk
    head, _, leftover = buffer.partition(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    if "101" not in lines[0]:
        raise CdpError(f"调试地址拒绝了 WebSocket 升级：{lines[0].strip()}")
    fields: dict[str, str] = {}
    for line in lines[1:]:
        name, _, value = line.partition(":")
        fields[name.strip().lower()] = value.strip()
    if fields.get("sec-websocket-accept") != _accept_key(key):
        raise CdpError("调试地址的 WebSocket 应答校验失败，可能不是 CDP 端口。")
    return leftover


# ---------------------------------------------------------------- 选页面标签

def pick_page(pages: list[dict]) -> dict:
    """从不带站点信息的页面列表里挑一个（一般是第一个）。"""
    for page in pages:
        if not page.get("url", "").startswith(
            ("edge://", "chrome://", "devtools://", "about:blank")
        ):
            return page
    if pages:
        return pages[0]
    raise CdpError("浏览器里没有可用的页面标签。")


def pick_site_page(pages: list[dict], url_prefixes: list[str] | None = None) -> dict | None:
    """挑出属于目标站点的那个标签；没有就返回 None。

    为什么值得挑：真机上见过 Edge 自带的 ``edge://sync-confirmation-dialog/``
    排在目标页前面，挂到它上面去读 cookie 会一直读空。
    """
    prefixes = [prefix for prefix in (url_prefixes or []) if prefix]
    for page in pages:
        url = str(page.get("url", ""))
        if any(url.startswith(prefix) for prefix in prefixes):
            return page
    for page in pages:
        url = str(page.get("url", ""))
        if url.startswith(("edge://", "chrome://", "devtools://")):
            continue
        if url in ("", "about:blank"):
            continue
        return page
    return None


def _describe_exception(details: object) -> str:
    """从 Runtime.evaluate 的 exceptionDetails 里挖出一句能看的原因。"""
    if not isinstance(details, dict):
        return str(details)
    exception = details.get("exception")
    if isinstance(exception, dict):
        for key in ("description", "value"):
            text = exception.get(key)
            if text:
                return str(text)
    return str(details.get("text") or details)


class CDPSession:
    """一条 CDP 连接。

    为什么单开一个读线程：WebSocket 是全双工通道，应答可能和事件通知混在一起、
    也可能乱序到达，而调用方只想要「发一条命令、等这条命令的结果」。
    所以收帧交给一个专职线程，按报文 id 把结果投进各自的队列，
    外部线程只碰队列，不碰 socket。
    """

    def __init__(
        self,
        ws_url: str,
        timeout: float = 15.0,
        site_urls: list[str] | None = None,
        http_json: Callable[[str, float], object] | None = None,
    ) -> None:
        self._ws_url = ws_url
        self._timeout = timeout
        # 站点地址前缀：``connect()`` 靠它挑对页面标签。不传就退化成「随便挑一个
        # 不是 edge:// 的页面」，对 PDF 渲染（本地 file:// 页面）正合适。
        self._site_urls = [url for url in (site_urls or []) if url]
        # 「怎么读 /json/list」可以由调用方换掉（登录流程就传自己模块里那个名字，
        # 这样替换它就能换掉会话的行为）；不传就用本模块的实现。
        self._http_json = http_json or _http_json
        self._sock: socket.socket | None = None
        self._frames: _FrameReader | None = None
        self._reader: threading.Thread | None = None
        self._waiters: dict[int, queue.Queue[dict | None]] = {}
        self._ids = itertools.count(1)
        self._state_lock = threading.Lock()
        self._send_lock = threading.Lock()
        self._failure = ""

    # ---------- 连接管理 ----------

    def page_ws_url(self) -> str:
        """从 ``/json/list`` 里挑一个页面标签的调试地址（读一次就定，不等）。

        浏览器端点（``/devtools/browser/…``）只能做 Target 层面的操作，
        ``Network.getCookies`` / ``Runtime.evaluate`` 必须挂在页面标签上。

        构造参数既可以是 ``ws://`` 地址，也可以是 CDP 的 HTTP 地址
        （``http://127.0.0.1:<端口>/json/list``）—— 界面层自己启动浏览器时
        就是这么拼地址的，两条路都认，省得调用方先想清楚该给哪一种。

        要连的时候别用这个：``connect()`` 会等站点页面开出来再挂上去
        （刚启动那会儿可能只有空标签），这里只回答「现在有哪个页面」。
        """
        pages = self._page_targets()
        if not pages:
            raise CdpError("浏览器里没有可用的页面标签，读不到登录状态。")
        return str(self._pick(pages)["webSocketDebuggerUrl"])

    def _pick(self, pages: list[dict]) -> dict:
        site = pick_site_page(pages, self._site_urls)
        return site if site is not None else pick_page(pages)

    def _page_targets(self) -> list[dict]:
        """读一次 ``/json/list``，只留能挂上去的页面标签。"""
        targets = self._http_json(self._list_url(), self._timeout)
        return [
            target
            for target in (targets if isinstance(targets, list) else [])
            if isinstance(target, dict)
            and target.get("type") == "page"
            and target.get("webSocketDebuggerUrl")
        ]

    def _resolve_page_url(self) -> str:
        """等目标页面出现，再把它交给 connect()。

        为什么不一次定生死：浏览器是先把调试端口写进 ``DevToolsActivePort``、
        再加载命令行给的地址的 —— 端口文件一出现就连，页面列表里可能只有一个空标签，
        真机上还见过 Edge 自带的 ``edge://sync-confirmation-dialog/``。
        挂到那种页面上，页面里的 ``fetch`` 属于别的源，读饼干会一直读空，
        用户明明登录了程序却说没登录。所以这里给它几秒把目标页开出来。
        一直没等到（比如网断了）就退回「第一个页面标签」，让调用方拿到一个能用的
        连接，读不到东西自然会返回空。
        """
        deadline = time.monotonic() + min(_SITE_WAIT, self._timeout)
        fallback: str | None = None
        while True:
            pages = self._page_targets()
            if pages:
                site = pick_site_page(pages, self._site_urls)
                if site is not None:
                    return str(site["webSocketDebuggerUrl"])
                if fallback is None:
                    fallback = str(pick_page(pages)["webSocketDebuggerUrl"])
            if time.monotonic() >= deadline:
                if fallback is not None:
                    return fallback
                raise CdpError("浏览器里没有可用的页面标签，读不到登录状态。")
            time.sleep(_POLL_INTERVAL)

    def _list_url(self) -> str:
        """把构造参数归一成 ``/json/list`` 的 HTTP 地址。"""
        parts = urllib.parse.urlsplit(self._ws_url)
        if parts.scheme in ("http", "https"):
            if not parts.hostname:
                raise CdpError(f"调试接口地址不合法：{self._ws_url}")
            path = parts.path if parts.path not in ("", "/") else "/json/list"
            return urllib.parse.urlunsplit((parts.scheme, parts.netloc, path, "", ""))
        host, port, _ = _split_ws_url(self._ws_url)
        return f"http://{host}:{port}/json/list"

    def connect(self) -> None:
        """建立连接并开始收帧。

        给的是浏览器端点、或者干脆是 ``/json/list`` 那种 HTTP 地址时，
        会自动换成页面标签 —— 调用方不必先想清楚该连哪个。
        换的时候会等目标页面开出来（见 ``_resolve_page_url``）。
        """
        if self._sock is not None:
            return
        url = self._ws_url
        if urllib.parse.urlsplit(url).scheme != "ws":
            url = self._resolve_page_url()
        elif _split_ws_url(url)[2].startswith("/devtools/browser"):
            url = self._resolve_page_url()
        host, port, _ = _split_ws_url(url)
        try:
            sock = socket.create_connection((host, port), timeout=self._timeout)
        except OSError as exc:
            raise CdpError(f"连不上浏览器的调试端口（{host}:{port}）：{exc}") from exc
        try:
            leftover = _ws_handshake(sock, url, self._timeout)
        except OSError as exc:
            sock.close()
            raise CdpError(f"和浏览器的调试连接握手失败：{exc}") from exc
        except CdpError:
            sock.close()
            raise
        # 之后靠 close() 打断阻塞读：用户可能盯着登录页发呆很久，空闲不算超时。
        sock.settimeout(None)
        self._sock = sock
        self._ws_url = url
        self._frames = _FrameReader(sock, leftover)
        self._reader = threading.Thread(target=self._read_loop, name="cdp-reader", daemon=True)
        self._reader.start()

    def close(self) -> None:
        """关掉连接，不抛异常。"""
        self._fail("调试连接已关闭。")
        sock, self._sock = self._sock, None
        if sock is not None:
            try:
                with self._send_lock:
                    sock.sendall(build_frame(b"", OPCODE_CLOSE))
            except (OSError, CdpError):
                pass
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass
        reader, self._reader = self._reader, None
        if reader is not None and reader.is_alive():
            reader.join(timeout=2.0)

    def __enter__(self) -> "CDPSession":
        """``with`` 进来就连上 —— 界面层不必记得先调 connect()。"""
        self.connect()
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> bool:
        self.close()
        return False

    # ---------- 收帧 ----------

    def _fail(self, reason: str) -> None:
        """记下断线原因，并把所有等待者叫醒（拿 None 表示「不用等了」）。"""
        with self._state_lock:
            self._failure = reason
            waiters = list(self._waiters.values())
            self._waiters.clear()
        for waiter in waiters:
            waiter.put(None)

    def _read_loop(self) -> None:
        message: bytearray | None = None
        try:
            while True:
                frame = self._must_read_frame()
                if frame.opcode == OPCODE_CLOSE:
                    raise CdpError("浏览器关闭了调试连接。")
                if frame.opcode == OPCODE_PING:
                    self._send(build_frame(frame.payload, OPCODE_PONG))
                    continue
                if frame.opcode == OPCODE_PONG:
                    continue
                if frame.opcode in (OPCODE_TEXT, OPCODE_BINARY):
                    message = bytearray(frame.payload)
                elif frame.opcode == OPCODE_CONTINUATION:
                    if message is None:
                        continue  # 没收到过起始帧，这半截只能丢
                    message += frame.payload
                else:
                    continue
                if frame.fin and message is not None:
                    self._dispatch(bytes(message))
                    message = None
        except (OSError, CdpError) as exc:
            self._fail(str(exc) or "调试连接中断了。")
        except Exception as exc:  # noqa: BLE001 - 后台线程里漏出去的异常没人接得住
            self._fail(f"读取调试连接时出错：{exc}")

    def _must_read_frame(self) -> Frame:
        frames = self._frames
        if frames is None:
            raise CdpError("调试连接还没建立。")
        return frames.read_frame()

    def _dispatch(self, data: bytes) -> None:
        """按报文 id 把结果投给等待者；没有 id 的是事件通知，本模块不需要。"""
        try:
            message = json.loads(data.decode("utf-8", "replace"))
        except ValueError:
            return
        if not isinstance(message, dict):
            return
        message_id = message.get("id")
        if not isinstance(message_id, int):
            return
        with self._state_lock:
            waiter = self._waiters.pop(message_id, None)
        if waiter is not None:
            waiter.put(message)

    def _send(self, raw: bytes) -> None:
        sock = self._sock
        if sock is None:
            raise CdpError("调试连接还没建立，发不出命令。")
        with self._send_lock:
            try:
                sock.sendall(raw)
            except OSError as exc:
                raise CdpError(f"向浏览器发送命令失败：{exc}") from exc

    # ---------- 命令 ----------

    def call(
        self, method: str, params: dict | None = None, timeout: float = 15.0
    ) -> dict:
        """发一条 CDP 命令并等它的应答，返回 ``result`` 字典。

        超时按单次调用算：浏览器可能长时间没有动作（用户在慢慢登录），
        所以不做全局超时，只保证一次调用不会把界面卡住。
        """
        self.connect()
        if self._failure:
            raise CdpError(self._failure)
        waiter: queue.Queue[dict | None] = queue.Queue(maxsize=1)
        with self._state_lock:
            message_id = next(self._ids)
            self._waiters[message_id] = waiter
        payload: dict = {"id": message_id, "method": method}
        if params:
            payload["params"] = params
        try:
            self._send(build_frame(json.dumps(payload).encode("utf-8")))
        except BaseException:
            with self._state_lock:
                self._waiters.pop(message_id, None)
            raise
        try:
            response = waiter.get(timeout=timeout)
        except queue.Empty:
            with self._state_lock:
                self._waiters.pop(message_id, None)
            raise CdpError(f"等浏览器返回 {method} 超时（{timeout:g} 秒）。") from None
        if response is None:
            raise CdpError(self._failure or "调试连接已断开。")
        error = response.get("error")
        if error:
            detail = error.get("message") if isinstance(error, dict) else error
            raise CdpError(f"浏览器拒绝了 {method}：{detail}")
        result = response.get("result")
        return result if isinstance(result, dict) else {}

    def read_cookies(self, urls: list[str] | None = None) -> list[dict]:
        """读浏览器里的 cookie。

        这条路是「不打扰用户」的关键：``Network.getCookies`` 直接读浏览器自己的
        cookie 存储，不需要页面脚本参与，HttpOnly 的 userhash 一样拿得到。

        ``urls`` 省略时读默认站点（本模块不知道业务站点，取 ``site_urls`` 的第一项）。
        """
        targets = urls if urls else (self._site_urls[:1] or ["about:blank"])
        result = self.call("Network.getCookies", {"urls": targets})
        cookies = result.get("cookies")
        if not isinstance(cookies, list):
            return []
        return [cookie for cookie in cookies if isinstance(cookie, dict)]

    def evaluate(self, expression: str, await_promise: bool = False) -> str:
        """在页面里跑一段脚本，把结果当字符串拿回来。

        ``await_promise=True`` 时页面里的 fetch 才会跑完再返回，
        「应用一块饼干」那一步全靠它。
        """
        result = self.call(
            "Runtime.evaluate",
            {
                "expression": expression,
                "awaitPromise": await_promise,
                "returnByValue": True,
            },
        )
        if result.get("exceptionDetails"):
            raise CdpError(
                f"页面脚本执行出错：{_describe_exception(result['exceptionDetails'])}"
            )
        remote = result.get("result")
        value = remote.get("value") if isinstance(remote, dict) else None
        if value is None:
            return ""
        if isinstance(value, str):
            return value
        return json.dumps(value, ensure_ascii=False)

    def current_url(self) -> str:
        """当前页面地址，界面层用它判断用户是不是还停在登录页。"""
        return self.evaluate("location.href")
