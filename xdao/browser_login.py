"""在自己熟悉的浏览器里登录，再由程序把 userhash 取回来。

为什么要有这个模块：程序需要 userhash 才能取串，而它只存在浏览器的 cookie 里。
原本只能让用户按 F12、翻开发者工具、复制 cookie —— 对普通用户来说这一步最容易卡死。
这里换个路子：程序用系统自带的 Edge/Chrome 打开一个**独立用户目录**的窗口，
用户在窗口里正常登录，程序通过 Chrome DevTools Protocol（CDP）把 cookie 读走，
顺便自动「应用一块饼干」把 userhash 落到这个会话里。

Chrome 与 Edge 都是 Chromium，CDP 协议完全一致，所以同一套实现同时支持两者。

协议层刻意只用标准库：CDP 需要的只是一次 HTTP 升级握手加几条文本帧，
socket + base64 + hashlib + struct 就够；多一个第三方依赖，打包体积和
「装不上」的风险都要跟着涨。

本模块只做逻辑，不含任何界面代码（界面层见 gui.py）。
"""

from __future__ import annotations

import base64
import hashlib
import itertools
import json
import os
import queue
import re
import secrets
import socket
import struct
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping

USER_DATA_DIR_NAME = "browser-profile"
LOGIN_URL = "https://www.nmbxd1.com/Member/User/Index/login.html"
# 收进程树时走哪条路（taskkill / kill）。写成模块级常量、测试改它，而不是去改
# ``os.name``：``os`` 是全局共享的模块对象，改 ``os.name`` 会顺带改掉 ``pathlib``
# 的行为（它按 ``os.name`` 在模块级决定 ``Path`` 用哪个具体类）；Linux 上一边把
# ``os.name`` 改成 "nt" 一边有小用例失败，pytest 格式化失败信息时构造 ``WindowsPath``
# 就会直接 `NotImplementedError`，把真正的失败盖成 INTERNALERROR。
_IS_WINDOWS = os.name == "nt"
COOKIE_LIST_PATH = "/Member/User/Cookie/index.html"
COOKIE_SITE = "https://www.nmbxd1.com"

# RFC 6455 规定的握手魔术串：Sec-WebSocket-Accept 由它和客户端随机 key 哈希得来。
_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

OPCODE_CONTINUATION = 0x0
OPCODE_TEXT = 0x1
OPCODE_BINARY = 0x2
OPCODE_CLOSE = 0x8
OPCODE_PING = 0x9
OPCODE_PONG = 0xA

# 浏览器的调试端口文件是先创建、后写入的，读到半截要接着等，所以轮询间隔取小一点。
_POLL_INTERVAL = 0.2
# 关浏览器时先 terminate 给足它写盘的时间，超时才 kill。
_STOP_GRACE = 8.0
# 连调试端口时，等站点页面出现的最长时间。
# 浏览器是先写端口文件、再加载命令行给的地址的：端口一出来就连，可能只看到一个空标签
# （真机上还量到过 Edge 自带的 edge://sync-confirmation-dialog/）。连到那种页面上，
# 页面里的 fetch 属于别的源，读饼干会一律读空，所以这里给它几秒把登录页开出来。
_SITE_WAIT = 8.0
# 判断「裸粘贴」的字符集：整段的粘贴不会只由这些字符组成，因此能挡掉 HTML/JSON 残渣。
_BARE_VALUE_RE = re.compile(r"^[A-Za-z0-9._~+/=-]+$")
# 站点根地址：用来认出「哪个页面标签才是我们自己的页面」。
_SITE_ROOT = COOKIE_SITE.rstrip("/")
# 浏览器自己的内部页面：它们没有我们要的页面内容，页面里的 fetch 也不是站内请求。
_INTERNAL_URL_PREFIXES = (
    "about:",
    "edge:",
    "chrome:",
    "devtools:",
    "chrome-extension:",
    "edge-extension:",
)


class BrowserLoginError(Exception):
    """浏览器登录流程里可以直接展示给用户看的错误。"""


@dataclass(frozen=True)
class BrowserInfo:
    """一个候选浏览器：名字只用于界面提示，路径才是真正要启动的东西。"""

    name: str
    path: str


@dataclass(frozen=True)
class Frame:
    """一个 WebSocket 帧（只留下本模块需要的三个字段）。"""

    fin: bool
    opcode: int
    payload: bytes


# 候选浏览器。顺序就是优先级：先 Edge（Windows 必装），再 Chrome、Chromium、Brave。
# 每一项是（界面名, 环境变量, 相对路径）。
_CANDIDATES: tuple[tuple[str, str, str], ...] = (
    ("Edge", "PROGRAMFILES(X86)", r"Microsoft\Edge\Application\msedge.exe"),
    ("Edge", "PROGRAMFILES", r"Microsoft\Edge\Application\msedge.exe"),
    ("Chrome", "PROGRAMFILES", r"Google\Chrome\Application\chrome.exe"),
    ("Chrome", "PROGRAMFILES(X86)", r"Google\Chrome\Application\chrome.exe"),
    ("Chrome", "LOCALAPPDATA", r"Google\Chrome\Application\chrome.exe"),
    ("Chromium", "PROGRAMFILES", r"Chromium\Application\chrome.exe"),
    ("Chromium", "PROGRAMFILES(X86)", r"Chromium\Application\chrome.exe"),
    ("Chromium", "LOCALAPPDATA", r"Chromium\Application\chrome.exe"),
    ("Brave", "PROGRAMFILES", r"BraveSoftware\Brave-Browser\Application\brave.exe"),
    ("Brave", "PROGRAMFILES(X86)", r"BraveSoftware\Brave-Browser\Application\brave.exe"),
    ("Brave", "LOCALAPPDATA", r"BraveSoftware\Brave-Browser\Application\brave.exe"),
)


def _env_lookup(env: Mapping[str, str] | None, key: str) -> str:
    """取一个环境变量。

    传进来的映射里**显式给了空串**就表示「这台机器没有它」，不再去看真实环境 ——
    测试要能用空串把候选全关掉，否则结果会随开发机装了什么都变。
    """
    if env is not None:
        if key in env:
            return env[key]
        for name, value in env.items():
            if name.upper() == key:
                return value
    return os.environ.get(key, "")


def known_paths(env: Mapping[str, str] | None = None) -> list[BrowserInfo]:
    """按固定顺序列出这台机器上真正存在的浏览器（Edge → Chrome → Chromium → Brave）。"""
    found: list[BrowserInfo] = []
    seen: set[str] = set()
    for name, variable, relative in _CANDIDATES:
        base = _env_lookup(env, variable)
        if not base:
            continue
        path = Path(base) / Path(relative)
        marker = os.path.normcase(str(path))
        if marker in seen:
            continue
        seen.add(marker)
        if path.is_file():
            found.append(BrowserInfo(name=name, path=str(path)))
    return found


def _guess_name(path: Path) -> str:
    """按可执行文件名猜浏览器名，只用于界面文案。"""
    stem = path.stem.lower()
    if "msedge" in stem or stem == "edge":
        return "Edge"
    if "brave" in stem:
        return "Brave"
    if "chromium" in str(path).lower():
        return "Chromium"
    if "chrome" in stem:
        return "Chrome"
    return path.stem or "Chromium"


def find_browser(
    explicit: str | None = None, env: Mapping[str, str] | None = None
) -> BrowserInfo | None:
    """挑一个浏览器。

    用户手动指定的路径优先（他可能装在非默认位置）；指定的路径不存在时
    不报错，而是退回去自动找 —— 用户随手填个错路径不该让整条路走不下去。
    """
    if explicit:
        candidate = Path(explicit)
        if candidate.is_file():
            return BrowserInfo(name=_guess_name(candidate), path=str(candidate))
    found = known_paths(env)
    return found[0] if found else None


def user_data_dir(config_dir: Path) -> Path:
    """浏览器用户目录在配置目录下的位置。

    为什么不复用用户自己的浏览器 profile：一是他可能正开着浏览器，
    同一个 profile 会被锁住（新进程只会把标签页丢给已开的窗口，不监听调试端口）；
    二是调试端口和临时登录态都不该落进他的日常浏览记录里。
    """
    return Path(config_dir) / USER_DATA_DIR_NAME


def build_args(info: BrowserInfo, profile: Path, proxy: str = "") -> list[str]:
    """拼启动参数。

    刻意不加的几项都有原因：``--headless`` 用户看不见窗口就没法登录；
    ``--guest`` / ``--incognito`` 用完即弃，留不住登录态，下次还得重来。
    """
    args = [
        info.path,
        "--remote-debugging-port=0",
        f"--user-data-dir={profile}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-features=Translate",
    ]
    if proxy:
        args.append(f"--proxy-server={proxy}")
    args.append(LOGIN_URL)
    return args


def _pick_page(pages: list[dict]) -> dict:
    """从页面标签里挑一个「真是我们站点」的。

    为什么不直接拿第一个（这条是真机上量出来的）：浏览器会自己多开页面标签 ——
    装好的 Edge 上来就有一个 ``edge://sync-confirmation-dialog/``，它会排在登录页前面。
    连到那种页面上，页面里的 ``fetch`` 属于别的源，读饼干的脚本一律失败；
    而且 ``about:blank`` 这种空标签也没有我们要的页面内容。
    所以顺序是：站点页面 → 普通网页 → 退而求其次拿第一个。
    """
    site = _pick_site_page(pages)
    if site is not None:
        return site
    for page in pages:
        url = str(page.get("url", ""))
        if url and not url.startswith(_INTERNAL_URL_PREFIXES):
            return page
    return pages[0]


def _pick_site_page(pages: list[dict]) -> dict | None:
    """只认站点自己的页面标签，没有就返回 None。"""
    for page in pages:
        if str(page.get("url", "")).startswith(_SITE_ROOT):
            return page
    return None


def _site_host() -> str:
    """站点的域名，用来判断「当前页面是不是已经在站内」。"""
    return urllib.parse.urlsplit(COOKIE_SITE).hostname or ""


def ensure_login_page(session: "CDPSession") -> bool:
    """确保这个会话停在登录页，返回「是否真的导航过」。

    只在页面确实不在站内时才导航：用户可能已经登录、正在自己的页面上点，
    没必要每隔几秒就把标签页拽回登录页。
    需要它的场景是：我们连上的那个标签还停在 ``about:blank``（浏览器刚起来、
    起始地址还没落地），这时候不导航的话，页面里的 ``fetch`` 根本读不到饼干。
    """
    try:
        current = session.current_url()
    except BrowserLoginError:
        current = ""
    if urllib.parse.urlsplit(current).hostname == _site_host():
        return False
    session.call("Page.navigate", {"url": LOGIN_URL})
    return True


def parse_devtools_port(text: str) -> int:
    """从 DevToolsActivePort 文件内容里取端口（第一行）。

    文件里第二行是浏览器级 WebSocket 路径，端口取不到时整个流程都无从谈起，
    所以这里直接抛错，让界面层给一句人话。
    """
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        raise BrowserLoginError("浏览器的调试端口文件是空的，启动可能没成功。")
    try:
        port = int(lines[0])
    except ValueError as exc:
        raise BrowserLoginError(f"调试端口文件的内容不对：{lines[0][:40]}") from exc
    if not 0 < port < 65536:
        raise BrowserLoginError(f"调试端口超出范围：{port}")
    return port


def _parse_devtools_file(text: str) -> tuple[int, str]:
    """取（端口, 浏览器级 WebSocket 路径）。"""
    port = parse_devtools_port(text)
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return port, lines[1] if len(lines) > 1 else ""


def _kill_process_tree(process: subprocess.Popen[bytes]) -> None:
    """强杀，并且尽量连子进程一起收掉。

    浏览器把渲染、GPU、崩溃上报拆成一堆子进程，它们同样占着用户目录里的文件：
    只 terminate 父进程的话，界面层随后删临时目录会失败，在 %TEMP% 里留下删不掉的垃圾。
    ``taskkill /T`` 收的是我们自己启动的这一棵进程树，不会碰到用户自己开着的浏览器。
    任何一步失败都不抛异常 —— 收尾阶段的报错不该盖住真正的问题。
    """
    try:
        if _IS_WINDOWS:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                check=False,
            )
        else:
            process.kill()
    except OSError:
        pass


class LoginBrowser:
    """一个等着用户去登录的浏览器窗口。

    用 ``with`` 用：进入时启动并等端口出现，退出时关掉。
    调试端口固定写 ``--remote-debugging-port=0``，由浏览器自己挑空闲端口，
    避免和我们自己或其他程序的端口撞车。
    """

    def __init__(
        self,
        info: BrowserInfo,
        profile: Path,
        proxy: str = "",
        timeout: float = 20.0,
    ) -> None:
        self.info = info
        self.profile = Path(profile)
        self.proxy = proxy
        self.timeout = timeout
        self.process: subprocess.Popen[bytes] | None = None
        self.port = 0
        self.ws_path = ""
        self.browser_ws_url = ""

    def start(self) -> "LoginBrowser":
        """启动浏览器并等它把调试端口写出来。"""
        if self.process is not None:
            return self
        self.profile.mkdir(parents=True, exist_ok=True)
        port_file = self.profile / "DevToolsActivePort"
        # 上次崩溃可能留下过期端口：留着它会让等待立刻「成功」，然后连到一个死端口。
        try:
            port_file.unlink()
        except OSError:
            pass
        try:
            self.process = subprocess.Popen(
                build_args(self.info, self.profile, self.proxy),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                # GUI 程序里启动浏览器时别弹一个黑框（这个常量只在 Windows 上有）。
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError as exc:
            raise BrowserLoginError(f"启动 {self.info.name} 失败：{exc}") from exc

        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                code = self.process.returncode
                self.stop()
                raise BrowserLoginError(
                    f"{self.info.name} 启动后立刻退出了（退出码 {code}）。"
                    "如果是手动指定的路径，请确认它真的是浏览器；"
                    "也可以换一个浏览器再试。"
                )
            if port_file.exists():
                try:
                    text = port_file.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    text = ""
                try:
                    # 文件刚建好时可能只写了一半，读到半截就当还没好，继续等。
                    self.port, self.ws_path = _parse_devtools_file(text)
                except BrowserLoginError:
                    self.port, self.ws_path = 0, ""
                if self.port:
                    self.browser_ws_url = f"ws://127.0.0.1:{self.port}{self.ws_path}"
                    return self
            time.sleep(_POLL_INTERVAL)

        self.stop()
        raise BrowserLoginError(
            f"等了 {self.timeout:g} 秒还没等到 {self.info.name} 的调试端口。"
            "请确认浏览器能正常打开；装了安全软件时也可能拦下调试端口。"
        )

    def devtools_http(self, path: str) -> str:
        """拼出 CDP 的 HTTP 地址（``/json/list``、``/json/version`` 都在它下面）。"""
        if not self.port:
            raise BrowserLoginError("浏览器还没启动，调试端口未知。")
        return f"http://127.0.0.1:{self.port}/{path.lstrip('/')}"

    def stop(self) -> None:
        """关掉浏览器。已经关掉了、或者它不肯走，都不抛异常。"""
        process, self.process = self.process, None
        if process is None:
            return
        if process.poll() is None:
            try:
                process.terminate()
            except OSError:
                pass
            try:
                process.wait(timeout=_STOP_GRACE)
            except subprocess.TimeoutExpired:
                _kill_process_tree(process)
                try:
                    process.wait(timeout=5.0)
                except (subprocess.TimeoutExpired, OSError):
                    pass

    def __enter__(self) -> "LoginBrowser":
        return self.start()

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> bool:
        self.stop()
        return False


def browser_open(
    info: BrowserInfo,
    profile: Path,
    proxy: str = "",
    timeout: float = 20.0,
) -> LoginBrowser:
    """启动浏览器并等它就绪，返回一个可以用 ``with`` 收尾的句柄。"""
    return LoginBrowser(info, profile, proxy, timeout).start()


# ---------------------------------------------------------------- 协议层


def _accept_key(key: str) -> str:
    """算出握手应答里的 Sec-WebSocket-Accept（RFC 6455）。"""
    digest = hashlib.sha1((key + _WS_GUID).encode("ascii")).digest()
    return base64.b64encode(digest).decode("ascii")


def _split_ws_url(ws_url: str) -> tuple[str, int, str]:
    """把 ``ws://主机:端口/路径`` 拆成三段。"""
    parts = urllib.parse.urlsplit(ws_url)
    if parts.scheme != "ws" or not parts.hostname:
        raise BrowserLoginError(f"调试地址不是合法的 WebSocket 地址：{ws_url}")
    path = parts.path or "/"
    if parts.query:
        path = f"{path}?{parts.query}"
    return parts.hostname, parts.port or 80, path


def _http_json(url: str, timeout: float = 5.0) -> object:
    """读一个本地 HTTP 接口（CDP 的 ``/json/list``）。

    显式关掉代理：调试端口在 127.0.0.1 上，而用户可能开着系统代理，
    走代理会连不上自己机器上的端口。
    """
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(url, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8", "replace"))
    except (OSError, ValueError) as exc:
        raise BrowserLoginError(f"读取浏览器的调试接口失败：{exc}") from exc


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
            raise BrowserLoginError("和浏览器建立调试连接时被对方关闭了。")
        buffer += chunk
    head, _, leftover = buffer.partition(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    if "101" not in lines[0]:
        raise BrowserLoginError(f"调试地址拒绝了 WebSocket 升级：{lines[0].strip()}")
    fields: dict[str, str] = {}
    for line in lines[1:]:
        name, _, value = line.partition(":")
        fields[name.strip().lower()] = value.strip()
    if fields.get("sec-websocket-accept") != _accept_key(key):
        raise BrowserLoginError("调试地址的 WebSocket 应答校验失败，可能不是 CDP 端口。")
    return leftover


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
        raise BrowserLoginError("WebSocket 掩码必须是 4 字节。")
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
                raise BrowserLoginError("调试连接被浏览器关闭了。")
            self._buffer += chunk
        data = bytes(self._buffer[:count])
        del self._buffer[:count]
        return data

    def read_frame(self) -> Frame:
        return read_frame(self.read_exact)


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

    def __init__(self, ws_url: str, timeout: float = 15.0) -> None:
        self._ws_url = ws_url
        self._timeout = timeout
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
        （刚启动那会儿只有空标签），这里只回答「现在有哪个页面」。
        """
        pages = self._page_targets()
        if not pages:
            raise BrowserLoginError("浏览器里没有可用的页面标签，读不到登录状态。")
        return str(_pick_page(pages)["webSocketDebuggerUrl"])

    def _page_targets(self) -> list[dict]:
        """读一次 ``/json/list``，只留能挂上去的页面标签。"""
        targets = _http_json(self._list_url(), self._timeout)
        return [
            target
            for target in (targets if isinstance(targets, list) else [])
            if isinstance(target, dict)
            and target.get("type") == "page"
            and target.get("webSocketDebuggerUrl")
        ]

    def _resolve_page_url(self) -> str:
        """等站点页面出现，再把它交给 connect()。

        为什么不一次定生死：浏览器是先把调试端口写进 ``DevToolsActivePort``、
        再加载命令行给的地址的 —— 端口文件一出现就连，页面列表里可能只有一个空标签，
        真机上还见过 Edge 自带的 ``edge://sync-confirmation-dialog/``。
        挂到那种页面上，页面里的 ``fetch`` 属于别的源，读饼干会一直读空，
        用户明明登录了程序却说没登录。所以这里给它几秒把登录页开出来。
        一直没等到（比如网断了）就退回 ``_pick_page()``，让调用方拿到一个能用的连接，
        读不到东西自然会返回空。
        """
        deadline = time.monotonic() + min(_SITE_WAIT, self._timeout)
        fallback: str | None = None
        while True:
            pages = self._page_targets()
            if pages:
                site = _pick_site_page(pages)
                if site is not None:
                    return str(site["webSocketDebuggerUrl"])
                if fallback is None:
                    fallback = str(_pick_page(pages)["webSocketDebuggerUrl"])
            if time.monotonic() >= deadline:
                if fallback is not None:
                    return fallback
                raise BrowserLoginError("浏览器里没有可用的页面标签，读不到登录状态。")
            time.sleep(_POLL_INTERVAL)

    def _list_url(self) -> str:
        """把构造参数归一成 ``/json/list`` 的 HTTP 地址。"""
        parts = urllib.parse.urlsplit(self._ws_url)
        if parts.scheme in ("http", "https"):
            if not parts.hostname:
                raise BrowserLoginError(f"调试接口地址不合法：{self._ws_url}")
            path = parts.path if parts.path not in ("", "/") else "/json/list"
            return urllib.parse.urlunsplit((parts.scheme, parts.netloc, path, "", ""))
        host, port, _ = _split_ws_url(self._ws_url)
        return f"http://{host}:{port}/json/list"

    def connect(self) -> None:
        """建立连接并开始收帧。

        给的是浏览器端点、或者干脆是 ``/json/list`` 那种 HTTP 地址时，
        会自动换成页面标签 —— 调用方不必先想清楚该连哪个。
        换的时候会等站点页面开出来（见 ``_resolve_page_url``）。
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
            raise BrowserLoginError(
                f"连不上浏览器的调试端口（{host}:{port}）：{exc}"
            ) from exc
        try:
            leftover = _ws_handshake(sock, url, self._timeout)
        except OSError as exc:
            sock.close()
            raise BrowserLoginError(f"和浏览器的调试连接握手失败：{exc}") from exc
        except BrowserLoginError:
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
            except (OSError, BrowserLoginError):
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
                    raise BrowserLoginError("浏览器关闭了调试连接。")
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
        except (OSError, BrowserLoginError) as exc:
            self._fail(str(exc) or "调试连接中断了。")
        except Exception as exc:  # noqa: BLE001 - 后台线程里漏出去的异常没人接得住
            self._fail(f"读取调试连接时出错：{exc}")

    def _must_read_frame(self) -> Frame:
        frames = self._frames
        if frames is None:
            raise BrowserLoginError("调试连接还没建立。")
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
            raise BrowserLoginError("调试连接还没建立，发不出命令。")
        with self._send_lock:
            try:
                sock.sendall(raw)
            except OSError as exc:
                raise BrowserLoginError(f"向浏览器发送命令失败：{exc}") from exc

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
            raise BrowserLoginError(self._failure)
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
            raise BrowserLoginError(
                f"等浏览器返回 {method} 超时（{timeout:g} 秒）。"
            ) from None
        if response is None:
            raise BrowserLoginError(self._failure or "调试连接已断开。")
        error = response.get("error")
        if error:
            detail = error.get("message") if isinstance(error, dict) else error
            raise BrowserLoginError(f"浏览器拒绝了 {method}：{detail}")
        result = response.get("result")
        return result if isinstance(result, dict) else {}

    def read_cookies(self, urls: list[str] | None = None) -> list[dict]:
        """读浏览器里的 cookie。

        这条路是「不打扰用户」的关键：``Network.getCookies`` 直接读浏览器自己的
        cookie 存储，不需要页面脚本参与，HttpOnly 的 userhash 一样拿得到。
        """
        params = {"urls": urls if urls else [COOKIE_SITE + "/"]}
        result = self.call("Network.getCookies", params)
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
            raise BrowserLoginError(
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


# ---------------------------------------------------------------- 饼干


def looks_like_userhash(value: str) -> bool:
    """像不像一个 userhash：非空、无空白、无 ``;``、至少 6 位可打印 ASCII。

    X 岛的 userhash 是 8 位左右的不透明串。这里是给「用户粘贴了一堆东西」
    兜底用的粗筛，不要求它懂 X 岛的内部规则。
    """
    if not value or len(value) < 6:
        return False
    if ";" in value:
        return False
    # 空白要显式挡：``str.isprintable()`` 认为空格是可打印的，只靠它会把
    # 「ABC 12345」这种两句拼在一起的东西当成一个值。
    if any(character.isspace() for character in value):
        return False
    return all(character.isprintable() and ord(character) < 128 for character in value)


def _bare_candidate(token: str) -> str | None:
    """把一小段文本当成「只是一个值」去认：去掉成对引号后再粗筛。

    带 ``=`` 的片段一律不认：那说明这是 ``名字=值`` 的残留（比如用户只复制了
    ``userhash=`` 这半行），而值本身就带等号的情况由上面那条 ``userhash=``
    正则负责——那条路径才是「用户明确贴了前缀」的证据。
    """
    candidate = token.strip().strip("\"'").strip()
    if "=" in candidate:
        return None
    if not _BARE_VALUE_RE.match(candidate):
        return None
    return candidate if looks_like_userhash(candidate) else None


def parse_userhash_input(text: str) -> str | None:
    """从用户粘贴的内容里抠出 userhash，抠不到返回 None。

    用户会贴进来的东西五花八门：整段 Cookie 串、``userhash=…`` 一行、
    导出接口返回的 JSON、以及只把值本身复制过来（有时带引号或换行）。
    按「越像整段粘贴的越先试」的顺序依次尝试。
    """
    if not text or not text.strip():
        return None
    # 值两侧可能带引号（"userhash=\"…\""），引号本身不能算进值里。
    match = re.search(r"userhash\s*=\s*[\"']?([^;\"'\s]+)", text, re.IGNORECASE)
    if match:
        return match.group(1)
    try:
        data = json.loads(text.strip())
    except ValueError:
        data = None
    if isinstance(data, dict):
        for key in ("cookie", "userhash", "userHash"):
            value = data.get(key)
            if isinstance(value, str) and value:
                return value
    match = re.search(r'"cookie"\s*:\s*"([^"]+)"', text)
    if match:
        return match.group(1)
    bare = _bare_candidate(text)
    if bare:
        return bare
    # 从开发者工具的 cookie 表格里复制时会带上 "userhash" 这一行标题：
    # 这时整段里只有唯一一个像值的片段。
    values = [
        token
        for piece in re.split(r"[\s,;]+", text.strip())
        if (token := _bare_candidate(piece)) is not None
        and piece.strip().strip("\"'").lower() not in {"userhash", "cookie"}
    ]
    if len(values) == 1:
        return values[0]
    return None


# 「应用一块饼干」的页面脚本。
#
# 依据的是仓库里已经跑通的客户端实现（xdao/client.py 的 apply_cookie 与
# _extract_userhash_from_export）与 tests/test_client.py 里那份真实的列表页片段：
#   1. 列表行的 ``id`` 从 ``Cookie/(switchTo|export)/id/<id>`` 链接里取最可靠，
#      取不到时退回「``<tr>`` 行第二个单元格」；链接里带 ``.html``，
#      捕获到的 id 要把这个后缀摘掉，否则拼出来的地址会变成 ``xxx.html.html``；
#   2. 同一个 id 会因为 switchTo 与 export 两个链接在正则结果里各出现一次，
#      先去重；列表里最新申请的那块排在后面，取最后一个才是权限最全的叶子饼干；
#   3. 先 ``switchTo`` 让这个浏览器会话正式用上它，再 ``export`` 取值。
# 取值的三种返回形态（JSON、userhash= 文本、"cookie":"…" 片段）照 client.py 的
# _extract_userhash_from_export 写，少一种都会漏。
_APPLY_COOKIE_JS = r"""
(async () => {
  const BASE = __COOKIE_BASE__;
  const html = (document.body && document.body.innerHTML) || '';
  const ids = [];
  const linkRe = /Cookie\/(?:switchTo|export)\/id\/([^\/\s"'<]+)/g;
  let hit;
  while ((hit = linkRe.exec(html)) !== null) {
    const id = hit[1].replace(/\.html$/i, '');
    if (id && ids.indexOf(id) < 0) {
      ids.push(id);
    }
  }
  if (ids.length === 0) {
    for (const row of document.querySelectorAll('tr')) {
      const cells = row.querySelectorAll('td');
      if (cells.length < 2) {
        continue;
      }
      const candidate = (cells[1].textContent || '').trim();
      if (candidate && ids.indexOf(candidate) < 0) {
        ids.push(candidate);
      }
    }
  }
  if (ids.length === 0) {
    return '';
  }
  const id = ids[ids.length - 1];
  await fetch(BASE + 'switchTo/id/' + encodeURIComponent(id) + '.html', {credentials: 'include'});
  const text = await fetch(BASE + 'export/id/' + encodeURIComponent(id) + '.html', {credentials: 'include'}).then((r) => r.text());
  let value = '';
  try {
    const data = JSON.parse(text);
    if (data && data.cookie) {
      value = String(data.cookie);
    }
  } catch (err) {
    value = '';
  }
  if (!value) {
    const byText = text.match(/userhash=([^;"'\s]+)/);
    if (byText) {
      value = byText[1];
    }
  }
  if (!value) {
    const byJson = text.match(/"cookie"\s*:\s*"([^"]+)"/);
    if (byJson) {
      value = byJson[1];
    }
  }
  return value.trim();
})()
"""


def _cookie_action_base() -> str:
    """``switchTo`` / ``export`` 两个接口所在的目录，从常量推出来免得写两遍。"""
    prefix = COOKIE_LIST_PATH.rsplit("/", 1)[0]
    return f"{COOKIE_SITE}{prefix}/"


def build_apply_cookie_script() -> str:
    """返回在 Cookies 页里「应用最新一块饼干并取值」的 JS。

    在已登录的页面里执行它（``await_promise=True``），返回 userhash 字符串；
    页面里取不到时返回空串，由调用方决定怎么提示。
    """
    return _APPLY_COOKIE_JS.replace("__COOKIE_BASE__", json.dumps(_cookie_action_base()))


def apply_leaf_cookie(session: CDPSession) -> str | None:
    """在已登录的浏览器会话里应用一块饼干，返回 userhash 或 None。

    失败一律返回 None 而不是抛异常：拿不到就走 cookie 兜底这条路，
    界面层只需要判断「有没有」。拿到的值还要再粗筛一遍 ——
    页面万一返回了别的东西（跳转页 HTML 之类），别把它当 userhash 存下去。
    """
    try:
        value = session.evaluate(build_apply_cookie_script(), await_promise=True)
    except BrowserLoginError:
        return None
    value = (value or "").strip()
    return value if looks_like_userhash(value) else None
