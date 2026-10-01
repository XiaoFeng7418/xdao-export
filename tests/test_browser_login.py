"""浏览器登录库的测试（全部离线）。

帧层用 ``socket.socketpair()`` 造一对假连接：一端由测试手工拼字节，另一端交给实现解析；
反方向也做一遍（实现发出去的帧由测试手工拆开核对）。握手同样用假连接跑：
假服务端回一个 101，就能把「余包不能丢」这条规则也钉住。

不启动真浏览器、不联网。只有末尾那条集成用例例外：本机确实装了浏览器
**并且**显式设了 ``XDAO_BROWSER_TEST=1`` 才跑，默认 skip。

产物目录用 ``artifacts_dir``（工作区内已提交的 ``.test-artifacts``），
和仓库其它测试保持一致。
"""

from __future__ import annotations

import base64
import contextlib
import errno
import hashlib
import inspect
import json
import os
import queue
import shutil
import socket
import struct
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable, Iterator

import pytest

from xdao import browser_login as bl
from xdao import cdp as cdp

# 测试自己写一遍握手魔术串，不引用实现里的常量：两边一起写错就测不出来了。
_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


# ---------------------------------------------------------------- 工具


def _touch(root: Path, relative: str) -> Path:
    """在假目录里造一个「浏览器可执行文件」（内容无所谓，只看它在不在）。"""
    path = root / Path(relative)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"MZ")
    return path


def _no_browsers_env(root: Path) -> dict[str, str]:
    """一个「这台机器什么都没装」的环境：显式给空串，测试就不受开发机影响。"""
    return {
        "PROGRAMFILES": str(root / "nope"),
        "PROGRAMFILES(X86)": "",
        "LOCALAPPDATA": "",
    }


@contextlib.contextmanager
def _socket_pair() -> Iterator[tuple[socket.socket, socket.socket]]:
    left, right = socket.socketpair()
    try:
        yield left, right
    finally:
        left.close()
        right.close()


def _exact_reader(sock: socket.socket) -> Callable[[int], bytes]:
    """把 socket 包成「读满 n 个字节」的函数，喂给实现里的帧解析。"""

    def read_exact(count: int) -> bytes:
        data = b""
        while len(data) < count:
            chunk = sock.recv(count - len(data))
            if not chunk:
                raise AssertionError("对端提前关闭了连接")
            data += chunk
        return data

    return read_exact


def _manual_frame(
    payload: bytes,
    mask: bytes = b"\x11\x22\x33\x44",
    opcode: int = 0x1,
    fin: bool = True,
) -> bytes:
    """测试自己拼一个帧（服务端方向 mask 传空串）。"""
    header = bytearray([(0x80 if fin else 0x00) | opcode])
    length = len(payload)
    if length < 126:
        header.append((0x80 if mask else 0x00) | length)
    elif length < 1 << 16:
        header.append((0x80 if mask else 0x00) | 126)
        header += struct.pack(">H", length)
    else:
        header.append((0x80 if mask else 0x00) | 127)
        header += struct.pack(">Q", length)
    if mask:
        header += mask
        return bytes(header) + bytes(
            byte ^ mask[index % 4] for index, byte in enumerate(payload)
        )
    return bytes(header) + payload


def _manual_split(raw: bytes) -> tuple[bool, int, bytes, bytes]:
    """测试自己拆一个帧，返回 (fin, opcode, mask, 明文载荷)。"""
    fin = bool(raw[0] & 0x80)
    opcode = raw[0] & 0x0F
    masked = bool(raw[1] & 0x80)
    length = raw[1] & 0x7F
    position = 2
    if length == 126:
        (length,) = struct.unpack(">H", raw[position : position + 2])
        position += 2
    elif length == 127:
        (length,) = struct.unpack(">Q", raw[position : position + 8])
        position += 8
    mask = raw[position : position + 4] if masked else b""
    position += 4 if masked else 0
    body = raw[position : position + length]
    if masked:
        body = bytes(byte ^ mask[index % 4] for index, byte in enumerate(body))
    return fin, opcode, mask, body


def _accept_key(key: str) -> str:
    digest = hashlib.sha1((key + _WS_GUID).encode("ascii")).digest()
    return base64.b64encode(digest).decode("ascii")


def _switching_protocols(request: str, extra: bytes = b"", accept: str | None = None) -> bytes:
    """按收到的请求头算一个 101 应答；``extra`` 追加在应答之后一起发出去。"""
    key = _header_value(request, "sec-websocket-key")
    value = _accept_key(key) if accept is None else accept
    return (
        b"HTTP/1.1 101 Switching Protocols\r\n"
        b"Upgrade: websocket\r\n"
        b"Connection: Upgrade\r\n"
        b"Sec-WebSocket-Accept: " + value.encode("ascii") + b"\r\n\r\n" + extra
    )


def _serve_handshake(
    server: socket.socket, respond: Callable[[str], bytes]
) -> list[str]:
    """假服务端：读一个握手请求，把 ``respond(请求头)`` 的结果回过去。

    返回收到的请求头列表（线程回填，调用方在读完之后再看）。
    """

    def run() -> None:
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = server.recv(4096)
            if not chunk:
                return
            data += chunk
        head, _, _ = data.partition(b"\r\n\r\n")
        request = head.decode("latin-1")
        requests.append(request)
        server.sendall(respond(request))

    requests: list[str] = []
    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return requests


def _header_value(head: str, name: str) -> str:
    for line in head.split("\r\n"):
        key, _, value = line.partition(":")
        if key.strip().lower() == name.lower():
            return value.strip()
    return ""


# ---------------------------------------------------------------- 找浏览器


def test_known_paths_lists_edge_then_chrome_then_chromium_then_brave(
    artifacts_dir: Path,
) -> None:
    x86 = artifacts_dir / "x86"
    pf = artifacts_dir / "pf"
    edge = _touch(x86, r"Microsoft\Edge\Application\msedge.exe")
    chrome = _touch(pf, r"Google\Chrome\Application\chrome.exe")
    chromium = _touch(pf, r"Chromium\Application\chrome.exe")
    brave = _touch(pf, r"BraveSoftware\Brave-Browser\Application\brave.exe")
    env = {
        "PROGRAMFILES(X86)": str(x86),
        "PROGRAMFILES": str(pf),
        "LOCALAPPDATA": str(artifacts_dir / "local"),
    }
    found = bl.known_paths(env)
    assert [info.name for info in found] == ["Edge", "Chrome", "Chromium", "Brave"]
    assert [info.path for info in found] == [
        str(edge),
        str(chrome),
        str(chromium),
        str(brave),
    ]


def test_known_paths_skips_paths_that_are_not_files(artifacts_dir: Path) -> None:
    empty = artifacts_dir / "empty"
    (empty / r"Microsoft\Edge\Application").mkdir(parents=True, exist_ok=True)
    env = _no_browsers_env(artifacts_dir)
    env["PROGRAMFILES(X86)"] = str(empty)
    assert bl.known_paths(env) == []


def test_known_paths_treats_empty_env_value_as_absent(artifacts_dir: Path) -> None:
    """显式空串表示「没有这个变量」，不许回落到真实环境。"""
    env = {
        "PROGRAMFILES": "",
        "PROGRAMFILES(X86)": "",
        "LOCALAPPDATA": "",
    }
    assert bl.known_paths(env) == []


def test_known_paths_falls_back_to_the_real_environment(
    artifacts_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pf = artifacts_dir / "pf"
    edge = _touch(pf, r"Microsoft\Edge\Application\msedge.exe")
    monkeypatch.setenv("PROGRAMFILES", str(pf))
    monkeypatch.setenv("PROGRAMFILES(X86)", "")
    monkeypatch.setenv("LOCALAPPDATA", "")
    assert [info.path for info in bl.known_paths()] == [str(edge)]


@pytest.mark.parametrize(
    "relative,expected",
    [
        (r"Custom\msedge.exe", "Edge"),
        (r"Custom\chrome.exe", "Chrome"),
        (r"Custom\brave.exe", "Brave"),
        (r"Chromium\Application\chrome.exe", "Chromium"),
        (r"Custom\mybrowser.exe", "mybrowser"),
    ],
)
def test_find_browser_uses_the_explicit_path(
    artifacts_dir: Path, relative: str, expected: str
) -> None:
    exe = _touch(artifacts_dir, relative)
    info = bl.find_browser(explicit=str(exe), env=_no_browsers_env(artifacts_dir))
    assert info is not None
    assert (info.name, info.path) == (expected, str(exe))


def test_find_browser_falls_back_when_the_explicit_path_is_missing(
    artifacts_dir: Path,
) -> None:
    """用户填错路径不该让整条路走不下去，退回自动找。"""
    pf = artifacts_dir / "pf"
    edge = _touch(pf, r"Microsoft\Edge\Application\msedge.exe")
    env = _no_browsers_env(artifacts_dir)
    env["PROGRAMFILES"] = str(pf)
    info = bl.find_browser(explicit=str(artifacts_dir / "没有这个.exe"), env=env)
    assert info is not None
    assert info.path == str(edge)


@pytest.mark.parametrize(
    "path,expected",
    [
        # 反斜杠在 Linux 上不是分隔符：只取「文件名」不能依赖宿主平台，
        # 否则界面上会给用户显示一整段路径当浏览器名（Linux CI 上真红过一次）。
        (r"C:\Program Files\Microsoft\Edge\Application\msedge.exe", "Edge"),
        (r"C:\Users\me\AppData\Local\Google\Chrome\Application\chrome.exe", "Chrome"),
        (r"C:\x\Chromium\Application\chrome.exe", "Chromium"),
        (r"C:\x\brave.exe", "Brave"),
        (r"C:\x\Custom\mybrowser.exe", "mybrowser"),
        ("/usr/bin/chromium-browser", "Chromium"),
        ("/usr/bin/my-browser", "my-browser"),
        ("", "Chromium"),
    ],
)
def test_guess_name_ignores_the_host_separator(path: str, expected: str) -> None:
    assert bl._guess_name(Path(path)) == expected


def test_find_browser_returns_none_when_nothing_is_installed(
    artifacts_dir: Path,
) -> None:
    assert bl.find_browser(env=_no_browsers_env(artifacts_dir)) is None


# ------------------------------------------------- 默认浏览器优先（真实期望差）


class _FakeRegistry:
    """够用的假 ``winreg``：只要 ``OpenKey`` / ``QueryValueEx`` 与两个 hive 常量。

    为什么要假的：Windows 上不能为了测试去改用户的默认浏览器；Linux 上压根没有
    ``winreg``，那段逻辑就会在 CI 上没人看着。两个地方都用它，测的是同一件事。
    """

    HKEY_CURRENT_USER = "HKCU"
    HKEY_LOCAL_MACHINE = "HKLM"

    def __init__(self, keys: dict[tuple[str, str], dict[str, object]]) -> None:
        self.keys = keys

    def OpenKey(self, hive: str, path: str):  # noqa: N802 —— 照抄 winreg 的名字
        if (hive, path) not in self.keys:
            raise FileNotFoundError(2, "系统找不到指定的文件。", path)
        return _FakeKey(self, hive, path)

    def QueryValueEx(self, key, name: str) -> tuple[object, int]:  # noqa: N802
        values = self.keys[(key.hive, key.path)]
        if name not in values:
            raise FileNotFoundError(2, "系统找不到指定的文件。", name)
        return values[name], 1


class _FakeKey:
    def __init__(self, registry: _FakeRegistry, hive: str, path: str) -> None:
        self.registry = registry
        self.hive = hive
        self.path = path

    def __enter__(self) -> "_FakeKey":
        return self

    def __exit__(self, *exc_info: object) -> bool:
        return False


def _fake_registry(monkeypatch: pytest.MonkeyPatch, keys: dict) -> None:
    fake = _FakeRegistry(keys)
    monkeypatch.setattr(bl, "_REGISTRY_OPENER", lambda: fake)


@pytest.mark.parametrize(
    "command,expected",
    [
        (
            '"C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe" --single-argument %1',
            "Edge",
        ),
        # 真机（本机）上 Chrome 的 ProgId 命令行就长这样
        (
            '"C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe" --single-argument %1',
            "Chrome",
        ),
        (r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe", "Brave"),
        # 认不出就别硬认：宁可回到固定顺序，也不要挑一个别的浏览器
        ('"C:\\Program Files\\Mozilla Firefox\\firefox.exe" -osint -url "%1"', ""),
        ("", ""),
    ],
)
def test_name_of_executable_only_recognises_chromium_family(
    command: str, expected: str
) -> None:
    assert bl._name_of_executable(command) == expected


def test_windows_default_exe_reads_the_user_choice_and_the_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_registry(
        monkeypatch,
        {
            (_FakeRegistry.HKEY_CURRENT_USER, bl._DEFAULT_BROWSER_KEY): {
                "ProgId": "ChromeHTML"
            },
            (
                _FakeRegistry.HKEY_LOCAL_MACHINE,
                r"SOFTWARE\Classes\ChromeHTML\shell\open\command",
            ): {"": '"C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe" --single-argument %1'},
        },
    )
    assert bl._windows_default_exe() == r"C:\Program Files\Google\Chrome\Application\chrome.exe"
    assert bl._default_browser_label() == "Chrome"


def test_windows_default_exe_ignores_a_broken_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """注册表里没有这个键（或读不动）时，当没有默认浏览器处理，不许抛出来。"""
    _fake_registry(monkeypatch, {})
    assert bl._windows_default_exe() == ""
    assert bl._default_browser_label() == ""


def test_windows_default_exe_gives_up_when_the_prog_id_is_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_registry(
        monkeypatch,
        {
            (_FakeRegistry.HKEY_CURRENT_USER, bl._DEFAULT_BROWSER_KEY): {
                "ProgId": "FirefoxURL-308046B0AF4A39CB"
            }
        },
    )
    assert bl._default_browser_label() == ""


def test_find_browser_prefers_the_windows_default_browser(
    artifacts_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """用户把默认浏览器改成 Chrome，就不该先拿 Edge 去试（真机上量到的期望差）。"""
    pf = artifacts_dir / "pf"
    edge = _touch(pf, r"Microsoft\Edge\Application\msedge.exe")
    chrome = _touch(pf, r"Google\Chrome\Application\chrome.exe")
    env = _no_browsers_env(artifacts_dir)
    env["PROGRAMFILES"] = str(pf)
    _fake_registry(
        monkeypatch,
        {
            (_FakeRegistry.HKEY_CURRENT_USER, bl._DEFAULT_BROWSER_KEY): {
                "ProgId": "ChromeHTML"
            },
            (
                _FakeRegistry.HKEY_CURRENT_USER,
                r"SOFTWARE\Classes\ChromeHTML\shell\open\command",
            ): {"": f'"{chrome}" --single-argument %1'},
        },
    )
    assert [info.name for info in bl.ordered_browsers(env)] == ["Chrome", "Edge"]
    chosen = bl.find_browser(env=env)
    assert chosen is not None
    assert (chosen.name, chosen.path) == ("Chrome", str(chrome))
    # 认不出默认浏览器时，原来的固定顺序（Edge 先）要原样保留
    _fake_registry(monkeypatch, {})
    assert [info.name for info in bl.ordered_browsers(env)] == ["Edge", "Chrome"]
    assert bl.find_browser(env=env).path == str(edge)  # type: ignore[union-attr]


def test_browser_candidates_puts_the_current_one_first_and_dedupes(
    artifacts_dir: Path,
) -> None:
    """重试时要先试「刚才那个」，再换别的；重复的不许试两遍。"""
    pf = artifacts_dir / "pf"
    edge = _touch(pf, r"Microsoft\Edge\Application\msedge.exe")
    chrome = _touch(pf, r"Google\Chrome\Application\chrome.exe")
    env = _no_browsers_env(artifacts_dir)
    env["PROGRAMFILES"] = str(pf)
    current = bl.BrowserInfo(name="Chrome", path=str(chrome))
    got = bl.browser_candidates(current, env=env)
    assert [info.path for info in got] == [str(chrome), str(edge)]
    # 传一个不在候选表里的（用户手动指定的路径）也要排最前
    custom = _touch(artifacts_dir, r"Custom\msedge.exe")
    got = bl.browser_candidates(bl.BrowserInfo(name="Edge", path=str(custom)), env=env)
    assert [info.path for info in got] == [str(custom), str(edge), str(chrome)]
    assert bl.browser_candidates(None, env=env)[0].path == str(edge)


# ---------------------------------------------------------------- 启动参数


def test_user_data_dir_lives_under_the_config_dir() -> None:
    assert bl.user_data_dir(Path("cfg")) == Path("cfg") / bl.USER_DATA_DIR_NAME
    assert bl.USER_DATA_DIR_NAME == "browser-profile"


def test_build_args_has_the_required_flags() -> None:
    info = bl.BrowserInfo(name="Edge", path=r"C:\somewhere\msedge.exe")
    args = bl.build_args(info, Path("profile"))
    assert args[0] == info.path
    assert "--remote-debugging-port=0" in args
    assert f"--user-data-dir={Path('profile')}" in args
    assert "--no-first-run" in args
    assert "--no-default-browser-check" in args
    assert "--disable-features=Translate" in args
    assert args[-1] == bl.LOGIN_URL
    # 不留登录态的几项一个都不能出现。
    for forbidden in ("headless", "guest", "incognito"):
        assert not any(forbidden in arg for arg in args), forbidden


def test_build_args_adds_proxy_only_when_asked() -> None:
    info = bl.BrowserInfo(name="Chrome", path=r"C:\somewhere\chrome.exe")
    plain = bl.build_args(info, Path("p"))
    assert not any(arg.startswith("--proxy-server") for arg in plain)
    proxied = bl.build_args(info, Path("p"), "http://127.0.0.1:7890")
    assert "--proxy-server=http://127.0.0.1:7890" in proxied
    assert proxied[-1] == bl.LOGIN_URL


def test_build_args_carries_the_frozen_launch_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    """打包版必须补 ``--no-sandbox``：不补的话浏览器在冻结进程里根本起不来。

    这条守的是「接线」—— ``build_args`` 得真的去问 ``browser_flags``，
    开关本身的值由 ``tests/test_browser_flags.py`` 守。
    """
    info = bl.BrowserInfo(name="Edge", path=r"C:\somewhere\msedge.exe")
    monkeypatch.setattr(bl, "launch_flags", lambda: ["--no-sandbox"])
    args = bl.build_args(info, Path("p"))
    assert "--no-sandbox" in args
    # 开关必须落在 URL 之前，否则会被浏览器当成网址而不是参数。
    assert args.index("--no-sandbox") < args.index(bl.LOGIN_URL)
    assert args[-1] == bl.LOGIN_URL


def test_build_args_stays_clean_when_flags_are_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """源码运行（``python main.py``）不该多出任何开关，也不该多出空串。"""
    info = bl.BrowserInfo(name="Edge", path=r"C:\somewhere\msedge.exe")
    monkeypatch.setattr(bl, "launch_flags", lambda: [])
    args = bl.build_args(info, Path("p"))
    assert "--no-sandbox" not in args
    assert args[-1] == bl.LOGIN_URL
    assert args[-2] == "--disable-features=Translate"


# ---------------------------------------------------------------- 端口文件


@pytest.mark.parametrize(
    "text",
    [
        "9222\n/devtools/browser/6f2a-1c\n",
        "  9222  \n/devtools/browser/x\n",
        "9222",
        "9222\r\n/devtools/browser/x\r\n",
    ],
)
def test_parse_devtools_port_accepts_the_first_line(text: str) -> None:
    assert bl.parse_devtools_port(text) == 9222


@pytest.mark.parametrize(
    "text",
    ["", "\n \n", "oops\n/devtools/browser/x\n", "0\n", "-1\n", "70000\n", "9222.5\n"],
)
def test_parse_devtools_port_rejects_junk(text: str) -> None:
    with pytest.raises(bl.BrowserLoginError):
        bl.parse_devtools_port(text)


# ---------------------------------------------------------------- 帧层


def test_read_frame_parses_a_masked_text_frame() -> None:
    payload = "你好，X 岛".encode("utf-8")
    with _socket_pair() as (left, right):
        right.sendall(_manual_frame(payload, mask=b"\x01\x02\x03\x04"))
        frame = bl.read_frame(_exact_reader(left))
    assert frame.fin is True
    assert frame.opcode == bl.OPCODE_TEXT
    assert frame.payload == payload


def test_read_frame_parses_an_unmasked_server_frame() -> None:
    with _socket_pair() as (left, right):
        right.sendall(_manual_frame(b"pong", mask=b"", opcode=bl.OPCODE_PONG))
        frame = bl.read_frame(_exact_reader(left))
    assert (frame.opcode, frame.payload) == (bl.OPCODE_PONG, b"pong")


def test_read_frame_reassembles_a_frame_split_across_reads() -> None:
    """真网络上一帧可能被拆成好几段到，读满再解析才不会错位。"""
    payload = b"split-me"
    raw = _manual_frame(payload)
    source = iter(raw)

    def one_byte_at_a_time(count: int) -> bytes:
        return bytes(next(source) for _ in range(count))

    frame = bl.read_frame(one_byte_at_a_time)
    assert (frame.opcode, frame.payload) == (bl.OPCODE_TEXT, payload)


@pytest.mark.parametrize("size", [0, 1, 125, 126, 65535, 65536, 70000])
def test_read_frame_handles_every_length_form(size: int) -> None:
    payload = bytes(index % 251 for index in range(size))
    with _socket_pair() as (left, right):
        right.sendall(_manual_frame(payload))
        frame = bl.read_frame(_exact_reader(left))
    assert frame.payload == payload


@pytest.mark.parametrize("size", [0, 1, 125, 126, 65535, 65536, 70000])
def test_build_frame_round_trips_through_a_hand_written_parser(size: int) -> None:
    """反方向：实现发出去的帧，由测试自己拆。"""
    payload = bytes(index % 251 for index in range(size))
    fin, opcode, mask, body = _manual_split(bl.build_frame(payload, mask=b"\xaa\xbb\xcc\xdd"))
    assert fin is True
    assert opcode == bl.OPCODE_TEXT
    assert mask == b"\xaa\xbb\xcc\xdd"
    assert body == payload


def test_build_frame_masks_the_payload_by_default() -> None:
    raw = bl.build_frame(b"hello")
    fin, opcode, mask, body = _manual_split(raw)
    assert (fin, opcode) == (True, bl.OPCODE_TEXT)
    assert len(mask) == 4
    assert body == b"hello"


def test_build_frame_rejects_a_bad_mask() -> None:
    with pytest.raises(bl.BrowserLoginError):
        bl.build_frame(b"hello", mask=b"\x00\x00\x00")


def test_build_frame_can_mark_a_non_final_frame() -> None:
    fin, opcode, _, _ = _manual_split(bl.build_frame(b"part", fin=False))
    assert fin is False
    assert opcode == bl.OPCODE_TEXT


# ---------------------------------------------------------------- 握手


def test_accept_key_matches_the_rfc6455_example() -> None:
    assert bl._accept_key("dGhlIHNhbXBsZSBub25jZQ==") == "s3pPLMBiTxaQ9kYGzzhZRbK+xOo="


def test_ws_handshake_sends_a_valid_request_and_keeps_leftover() -> None:
    with _socket_pair() as (client, server):
        # 假服务端故意把第一帧和 101 应答拼在一起发：余包丢了这个测试就会卡住。
        # 应答里的校验值必须按「收到的 key」算，因为客户端的 key 每次都是随机的。
        requests = _serve_handshake(
            server,
            lambda request: _switching_protocols(
                request, extra=_manual_frame(b"hi", mask=b"", opcode=1)
            ),
        )
        leftover = bl._ws_handshake(client, "ws://127.0.0.1:9222/devtools/page/ABC", 5.0)
        frame = bl._FrameReader(client, leftover).read_frame()
    assert requests, "假服务端没收到握手请求"
    request_head = requests[0]
    assert request_head.startswith("GET /devtools/page/ABC HTTP/1.1")
    assert "Host: 127.0.0.1:9222" in request_head
    assert "Upgrade: websocket" in request_head
    assert "Sec-WebSocket-Version: 13" in request_head
    assert _header_value(request_head, "sec-websocket-key")
    assert (frame.opcode, frame.payload) == (bl.OPCODE_TEXT, b"hi")


def test_ws_handshake_rejects_a_non_switching_response() -> None:
    with _socket_pair() as (client, server):
        _serve_handshake(
            server, lambda request: b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n"
        )
        with pytest.raises(bl.BrowserLoginError):
            bl._ws_handshake(client, "ws://127.0.0.1:9222/devtools/page/ABC", 5.0)


def test_ws_handshake_rejects_a_wrong_accept_key() -> None:
    """应答里的校验值不对，说明对面不是 CDP，别继续说话。"""
    with _socket_pair() as (client, server):
        _serve_handshake(
            server,
            lambda request: _switching_protocols(request, accept="bm90LXRoZS1yaWdodC1rZXk="),
        )
        with pytest.raises(bl.BrowserLoginError):
            bl._ws_handshake(client, "ws://127.0.0.1:9222/devtools/page/ABC", 5.0)


def test_split_ws_url_rejects_a_non_websocket_address() -> None:
    with pytest.raises(bl.BrowserLoginError):
        bl._split_ws_url("http://127.0.0.1:9222/json/list")


@pytest.mark.parametrize(
    "given,expected_list_url",
    [
        ("http://127.0.0.1:9222/json/list", "http://127.0.0.1:9222/json/list"),
        ("http://127.0.0.1:9222/", "http://127.0.0.1:9222/json/list"),
        ("http://127.0.0.1:9222", "http://127.0.0.1:9222/json/list"),
        ("ws://127.0.0.1:9222/devtools/browser/abc", "http://127.0.0.1:9222/json/list"),
        ("ws://127.0.0.1:9222/devtools/page/ABC", "http://127.0.0.1:9222/json/list"),
    ],
)
def test_cdp_session_accepts_both_ws_and_http_addresses(
    monkeypatch: pytest.MonkeyPatch, given: str, expected_list_url: str
) -> None:
    """界面层自己启动浏览器时会拼 ``http://127.0.0.1:<端口>/json/list``，这条路也要认。"""
    seen: list[str] = []

    def fake_list(url: str, timeout: float = 5.0) -> object:
        seen.append(url)
        return [
            {"type": "browser", "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/browser/x"},
            {"type": "page", "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/XYZ"},
        ]

    monkeypatch.setattr(bl, "_http_json", fake_list)
    session = bl._new_session(given)
    assert session.page_ws_url() == "ws://127.0.0.1:9222/devtools/page/XYZ"
    assert seen == [expected_list_url]


def test_cdp_session_reports_a_browser_without_page_targets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bl, "_http_json", lambda url, timeout=5.0: [{"type": "browser"}])
    with pytest.raises(bl.BrowserLoginError):
        bl._new_session("http://127.0.0.1:9222/json/list").page_ws_url()


def test_page_ws_url_prefers_our_site_over_browser_dialogs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """真机上量到过：Edge 自带的 ``edge://sync-confirmation-dialog/`` 排在登录页前面。

    连到那种页面上，读饼干脚本里的 fetch 属于别的源，会一律读空。
    """
    monkeypatch.setattr(
        bl,
        "_http_json",
        lambda url, timeout=5.0: [
            {
                "type": "page",
                "url": "edge://sync-confirmation-dialog/",
                "webSocketDebuggerUrl": "ws://127.0.0.1:1/devtools/page/DIALOG",
            },
            {
                "type": "page",
                "url": "about:blank",
                "webSocketDebuggerUrl": "ws://127.0.0.1:1/devtools/page/BLANK",
            },
            {
                "type": "page",
                "url": bl.LOGIN_URL,
                "webSocketDebuggerUrl": "ws://127.0.0.1:1/devtools/page/SITE",
            },
            {"type": "browser", "webSocketDebuggerUrl": "ws://127.0.0.1:1/devtools/browser/x"},
        ],
    )
    session = bl._new_session("http://127.0.0.1:9222/json/list")
    assert session.page_ws_url().endswith("/devtools/page/SITE")


def test_page_ws_url_prefers_a_real_web_page_over_a_blank_tab(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        bl,
        "_http_json",
        lambda url, timeout=5.0: [
            {
                "type": "page",
                "url": "about:blank",
                "webSocketDebuggerUrl": "ws://127.0.0.1:1/devtools/page/BLANK",
            },
            {
                "type": "page",
                "url": "https://example.com/",
                "webSocketDebuggerUrl": "ws://127.0.0.1:1/devtools/page/OTHER",
            },
        ],
    )
    session = bl._new_session("http://127.0.0.1:9222/json/list")
    assert session.page_ws_url().endswith("/devtools/page/OTHER")


def test_page_ws_url_falls_back_to_a_blank_tab(monkeypatch: pytest.MonkeyPatch) -> None:
    """只剩空标签时也要交出地址：导航那一步由 ensure_login_page 补上。"""
    monkeypatch.setattr(
        bl,
        "_http_json",
        lambda url, timeout=5.0: [
            {
                "type": "page",
                "url": "about:blank",
                "webSocketDebuggerUrl": "ws://127.0.0.1:1/devtools/page/BLANK",
            }
        ],
    )
    session = bl._new_session("http://127.0.0.1:9222/json/list")
    assert session.page_ws_url().endswith("/devtools/page/BLANK")


def test_resolve_page_url_waits_for_the_site_page(monkeypatch: pytest.MonkeyPatch) -> None:
    """端口刚写出来就连时，页面列表里可能还没有登录页（真机上见过 sync 对话框）。

    挂到别的页面上，读饼干脚本里的 ``fetch`` 属于别的源，会一律读空 ——
    所以 connect 要等站点页面开出来，而不是「第一个就算」。
    """
    dialog = {
        "type": "page",
        "url": "edge://sync-confirmation-dialog/",
        "webSocketDebuggerUrl": "ws://127.0.0.1:1/devtools/page/DIALOG",
    }
    site = {
        "type": "page",
        "url": bl.LOGIN_URL,
        "webSocketDebuggerUrl": "ws://127.0.0.1:1/devtools/page/SITE",
    }
    rounds = [[dialog], [dialog, site]]
    reads: list[int] = []

    def fake_list(url: str, timeout: float = 5.0) -> object:
        reads.append(1)
        return rounds[min(len(reads) - 1, 1)]

    monkeypatch.setattr(bl, "_http_json", fake_list)
    session = bl._new_session("http://127.0.0.1:9222/json/list", timeout=5.0)
    assert session._resolve_page_url().endswith("/devtools/page/SITE")
    assert len(reads) == 2


def test_resolve_page_url_gives_up_and_uses_what_is_there(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """等到预算用完也要交出一个能用的连接：界面层靠它去读，读不到自然是空。"""
    monkeypatch.setattr(
        bl,
        "_http_json",
        lambda url, timeout=5.0: [
            {
                "type": "page",
                "url": "about:blank",
                "webSocketDebuggerUrl": "ws://127.0.0.1:1/devtools/page/BLANK",
            }
        ],
    )
    session = bl._new_session("http://127.0.0.1:9222/json/list", timeout=0.3)
    assert session._resolve_page_url().endswith("/devtools/page/BLANK")


class _FakePageSession:
    """只实现 current_url/call 的假会话：导航决策不必真连浏览器。"""

    def __init__(self, url: str) -> None:
        self.url = url
        self.calls: list[tuple[str, dict]] = []

    def current_url(self) -> str:
        return self.url

    def call(self, method: str, params: dict | None = None, timeout: float = 15.0) -> dict:
        self.calls.append((method, params or {}))
        return {}


def test_ensure_login_page_navigates_only_when_needed() -> None:
    blank = _FakePageSession("about:blank")
    assert bl.ensure_login_page(blank) is True  # type: ignore[arg-type]
    assert blank.calls == [("Page.navigate", {"url": bl.LOGIN_URL})]

    dialog = _FakePageSession("edge://sync-confirmation-dialog/")
    assert bl.ensure_login_page(dialog) is True  # type: ignore[arg-type]
    assert dialog.calls == [("Page.navigate", {"url": bl.LOGIN_URL})]

    on_site = _FakePageSession(bl.LOGIN_URL)
    assert bl.ensure_login_page(on_site) is False  # type: ignore[arg-type]
    assert on_site.calls == []


def test_ensure_login_page_passes_a_real_failure_on() -> None:
    """会话坏了就如实报错：调用方（界面层）要拿它换一句人话。"""

    class Broken:
        def current_url(self) -> str:
            raise bl.BrowserLoginError("连接断了")

        def call(self, method: str, params: dict | None = None, timeout: float = 15.0) -> dict:
            raise bl.BrowserLoginError("连接断了")

    with pytest.raises(bl.BrowserLoginError):
        bl.ensure_login_page(Broken())  # type: ignore[arg-type]


def test_cdp_session_rejects_an_address_that_is_neither_ws_nor_http() -> None:
    with pytest.raises(bl.BrowserLoginError):
        bl._new_session("ftp://127.0.0.1:9222/x").page_ws_url()


def test_cdp_session_connect_resolves_an_http_address_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """给 HTTP 地址时，connect 要先去问 /json/list，而不是拿它当 ws 地址去连。"""
    calls: list[str] = []

    def fake_list(url: str, timeout: float = 5.0) -> object:
        calls.append(url)
        return [
            {
                "type": "page",
                "url": bl.LOGIN_URL,
                "webSocketDebuggerUrl": "ws://127.0.0.1:1/devtools/page/DEAD",
            }
        ]

    monkeypatch.setattr(bl, "_http_json", fake_list)
    session = bl._new_session("http://127.0.0.1:9222/json/list", timeout=2.0)
    with pytest.raises(bl.BrowserLoginError):
        session.connect()
    assert calls == ["http://127.0.0.1:9222/json/list"]


# ------------------------------------------------- 调试接口：端口在、口还不通

#: 真机上那条报错：端口文件已经出现，调试服务却还没开始收连接。
#:
#: 两个平台的真实形状不一样，这里按平台各造一份，让用例在哪儿跑都测同一件事：
#: * Windows：``OSError(10061, …)`` 自己就是 ``WinError``，``winerror=10061``；
#: * POSIX：``OSError(10061, …)`` 只是个 ``errno=10061`` 的怪码（``errno`` 里
#:   没有 10061），真机上这份错是 ``winerror=10061`` / ``errno=ECONNREFUSED``。
#: 之前只有前一种写法，于是 CI 的两条 ubuntu 矩阵把「该重试的失败」判成
#: 「再等也没用」（2026-10-01 的 run 36822858995：3 failed）。
if os.name == "nt":  # pragma: no cover - 平台分支，两边各在一边机器上跑
    _REFUSED: OSError = OSError(10061, "由于目标计算机积极拒绝，无法连接。")
else:  # pragma: no cover - 同上
    # POSIX 上真机抛的是 ConnectionRefusedError：别让用例测一个真机上见不到的类型
    _REFUSED = ConnectionRefusedError(
        errno.ECONNREFUSED, "由于目标计算机积极拒绝，无法连接。"
    )
    _REFUSED.winerror = 10061  # type: ignore[attr-defined]


def _refuse_then_succeed(monkeypatch: pytest.MonkeyPatch, failures: int) -> list[float]:
    """让 ``_http_json_once`` 先连不上 ``failures`` 次，之后返回一份页面列表。

    抛的是 ``_REFUSED`` 那个**真实的连接被拒**异常，外面按真代码的写法包成
    ``CdpError``（``_http_json_once`` 就是这么抛的），让用例测到的错误链跟真机一致。

    返回调用时刻清单，供用例核对「确实重试了、而且很快就回来了」。
    """
    moments: list[float] = []

    def fake_once(url: str, timeout: float = 5.0) -> object:
        moments.append(time.monotonic())
        if len(moments) <= failures:
            raise bl.CdpError(f"读取浏览器的调试接口失败：{_REFUSED}") from _REFUSED
        return [{"type": "page", "webSocketDebuggerUrl": "ws://127.0.0.1:1/devtools/page/x"}]

    # ``bl._http_json`` 是 ``cdp._http_json`` 的同一个函数对象，它调的是本模块
    # 名字表里的 ``_http_json_once``，所以要替换的是 ``cdp`` 这一边。
    monkeypatch.setattr(cdp, "_http_json_once", fake_once)
    return moments


def test_http_json_retries_the_window_between_the_port_file_and_a_live_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """端口文件刚出现的那几百毫秒里读不通，要自己等过去，而不是当场报失败。"""
    moments = _refuse_then_succeed(monkeypatch, failures=3)
    started = time.monotonic()
    targets = bl._http_json("http://127.0.0.1:9222/json/list", budget=3.0)
    elapsed = time.monotonic() - started
    assert isinstance(targets, list) and targets
    assert len(moments) == 4, "没有重试"
    assert elapsed < 1.0, f"该在毫秒级等到，实际花了 {elapsed:.2f}s"


def test_http_json_gives_up_when_the_budget_runs_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """一直连不上就别无限等：预算用完就把原始错误抛上去。"""
    moments = _refuse_then_succeed(monkeypatch, failures=10_000)
    started = time.monotonic()
    with pytest.raises(bl.CdpError):
        bl._http_json("http://127.0.0.1:9222/json/list", budget=0.4)
    elapsed = time.monotonic() - started
    # 预算是 0.4s：循环在「下一次等待会超预算」时就收手，所以实际用时略短于预算，
    # 只钉住「确实等过、也没等到预算之外」。
    assert 0.1 <= elapsed < 1.0, f"预算没起作用：{elapsed:.2f}s"
    assert len(moments) >= 2


def test_http_json_stops_early_when_the_hint_has_something_to_say(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """浏览器当场死了就别耗满预算：hint 一有话说就按它报错。"""
    _refuse_then_succeed(monkeypatch, failures=10_000)
    started = time.monotonic()
    with pytest.raises(bl.CdpError) as caught:
        bl._http_json(
            "http://127.0.0.1:9222/json/list",
            budget=5.0,
            hint=lambda: "浏览器刚起来就退出了",
        )
    assert "刚起来就退出了" in str(caught.value)
    assert time.monotonic() - started < 1.0


def test_http_json_does_not_retry_a_failure_that_waiting_cannot_fix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """路径写错之类的错重试也没用：一次就抛，别白等几秒。"""
    calls: list[str] = []

    def fake_once(url: str, timeout: float = 5.0) -> object:
        calls.append(url)
        raise bl.CdpError("读取浏览器的调试接口失败：404 Not Found")

    monkeypatch.setattr(cdp, "_http_json_once", fake_once)
    started = time.monotonic()
    with pytest.raises(bl.CdpError):
        bl._http_json("http://127.0.0.1:9222/json/list", budget=5.0)
    assert calls == ["http://127.0.0.1:9222/json/list"]
    assert time.monotonic() - started < 0.5


def test_retryable_read_failure_looks_at_both_the_windows_code_and_errno(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同一个「连不上」在 Windows 上是 winerror、在 POSIX 上是 errno，两边都得认。

    这条钉的是 CI：``OSError(10061, …)`` 在 Linux 上只是个 errno=10061 的怪码
    （``errno`` 里没有它），在 Windows 上才是 ``winerror=10061``。少认一边，
    就会在某条矩阵上把「该重试的失败」判成「再等也没用」（2026-10-01 的
    run 36822858995：ubuntu 两条矩阵 3 failed，就是栽在这里）。

    下面先把 errno 表清空，好把「只靠 winerror 也认得出」这件事单独钉住 ——
    否则在 Windows 上跑时它总是先被 ``winerror`` 命中，这行断言就形同虚设。
    """
    refused = _win_error(10061, "由于目标计算机积极拒绝，无法连接。")
    assert cdp._retryable_read_failure(refused)
    assert cdp._retryable_read_failure(ConnectionRefusedError(errno.ECONNREFUSED, "连接被拒"))
    assert not cdp._retryable_read_failure(_win_error(2, "找不到文件"))

    monkeypatch.setattr(cdp, "_RETRY_ERRNOS", frozenset())
    assert cdp._retryable_read_failure(refused), "只认 errno 的话，Linux 上这条会漏"


def test_session_passes_the_hint_through_to_the_reader(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """界面层给的那句「浏览器还在不在」要一路交到读接口的实现里。

    ``_new_session`` 在**调用时**从本模块名字表里取 ``_http_json``，所以替换
    这个名字就能换掉会话的读法（界面层建会话走的就是这条路）。
    """
    seen: list[object] = []

    def reader(url: str, timeout: float = 5.0, hint=None) -> object:
        seen.append(hint)
        return [{"type": "page", "webSocketDebuggerUrl": "ws://127.0.0.1:1/devtools/page/x"}]

    monkeypatch.setattr(bl, "_http_json", reader)
    session = bl._new_session(
        "http://127.0.0.1:9222/json/list", failure_hint=lambda: "dead"
    )
    assert session.page_ws_url() == "ws://127.0.0.1:1/devtools/page/x"
    assert len(seen) == 1 and callable(seen[0]), "hint 没传到读接口"
    assert seen[0]() == "dead"


def test_page_targets_still_works_with_a_reader_that_does_not_take_a_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """只吃 (url, timeout) 的替身/旧调用方照旧能用（不认识 hint 就退回老读法）。"""
    calls: list[tuple[str, float]] = []

    def two_arg_reader(url: str, timeout: float) -> object:
        calls.append((url, timeout))
        return [{"type": "page", "webSocketDebuggerUrl": "ws://127.0.0.1:1/devtools/page/x"}]

    monkeypatch.setattr(bl, "_http_json", two_arg_reader)
    session = bl._new_session(
        "http://127.0.0.1:9222/json/list", timeout=2.0, failure_hint=lambda: "x"
    )
    assert session.page_ws_url() == "ws://127.0.0.1:1/devtools/page/x"
    assert calls == [("http://127.0.0.1:9222/json/list", 2.0)]


# ---------------------------------------------------------------- 会话收帧


def test_frame_reader_keeps_the_leftover_bytes() -> None:
    with _socket_pair() as (left, right):
        right.sendall(_manual_frame(b"one") + _manual_frame(b"two"))
        reader = bl._FrameReader(left, _manual_frame(b"zero"))
        assert reader.read_frame().payload == b"zero"
        assert reader.read_frame().payload == b"one"
        assert reader.read_frame().payload == b"two"


def test_session_demuxes_replies_by_id() -> None:
    """按报文 id 分发是收帧线程的核心规则，这里绕开 socket 直接验它。"""
    session = bl._new_session("ws://127.0.0.1:9222/devtools/browser/x")
    waiter: queue.Queue[dict | None] = queue.Queue(maxsize=1)
    session._waiters[7] = waiter  # 直接放一个假等待者：这条规则不依赖网络
    session._dispatch(json.dumps({"id": 7, "result": {"ok": 1}}).encode("utf-8"))
    assert waiter.get_nowait() == {"id": 7, "result": {"ok": 1}}
    assert session._waiters == {}, "应答取走后不该留下残渣"
    # 事件通知没有 id：本模块不订阅，直接忽略，不许崩也不许发给谁。
    session._dispatch(b'{"method": "Network.loadingFinished", "params": {}}')
    session._dispatch(b"not json at all")
    session._dispatch(b'["not", "an", "object"]')
    assert session._waiters == {}


def test_session_close_is_safe_before_connect() -> None:
    session = bl._new_session("ws://127.0.0.1:9222/devtools/browser/x")
    session.close()
    session.close()
    with pytest.raises(bl.BrowserLoginError):
        session.evaluate("location.href")


def test_login_browser_devtools_http_returns_a_full_url() -> None:
    """契约写的是「完整 http URL」，界面层会直接拿它去读 /json/list。"""
    browser = bl.LoginBrowser(bl.BrowserInfo("Edge", "msedge.exe"), Path("profile"))
    browser.port = 9222
    assert browser.devtools_http("/json/list") == "http://127.0.0.1:9222/json/list"
    assert browser.devtools_http("json/version") == "http://127.0.0.1:9222/json/version"


def test_login_browser_tolerates_being_stopped_before_start() -> None:
    browser = bl.LoginBrowser(bl.BrowserInfo("Edge", "msedge.exe"), Path("profile"))
    browser.stop()
    browser.stop()
    with pytest.raises(bl.BrowserLoginError):
        browser.devtools_http("/json/list")


def test_start_reports_a_browser_that_cannot_run(artifacts_dir: Path) -> None:
    """路径不存在时给一句人话，不是把 traceback 丢到界面上。"""
    info = bl.BrowserInfo("Edge", str(artifacts_dir / "nope" / "msedge.exe"))
    browser = bl.LoginBrowser(info, artifacts_dir / "profile", timeout=5.0)
    with pytest.raises(bl.BrowserLoginError):
        browser.start()


def _win_error(
    winerror: int, message: str = "拒绝访问", path: str | None = None
) -> OSError:
    """造一个「带 Windows 错误码」的异常，在哪个平台跑都一样。

    ``OSError(13, msg, path, 5)`` 这个第四位参数只在 Windows 上会被塞进
    ``winerror``；Linux 上它被忽略，``winerror`` 是 ``None``，用例就会在 CI 上
    红（2026-10-01 的 run 36812719489 就是这么红的）。所以这里显式赋值。

    ``errno`` 跟着 ``winerror`` 走（2=ENOENT、5=EACCES、32=EBUSY），免得造出
    「Windows 码说找不到文件、errno 却说自己没权限」这种自相矛盾的异常。
    """
    errno_for_win = {2: errno.ENOENT, 5: errno.EACCES, 32: errno.EBUSY}
    exc = OSError(errno_for_win.get(winerror, errno.EACCES), message, path or "")
    exc.winerror = winerror  # type: ignore[attr-defined]
    return exc


class _PortWritingPopen:
    """假 Popen：像真浏览器那样立刻写出 DevToolsActivePort。"""

    def __init__(self, args: list[str], **kwargs: object) -> None:
        self.args = list(args)
        self.kwargs = dict(kwargs)
        self.pid = 4321
        self.returncode: int | None = None
        profile = Path(next(a.split("=", 1)[1] for a in self.args if a.startswith("--user-data-dir=")))
        (profile / "DevToolsActivePort").write_text(
            "9333\n/devtools/browser/abc\n", encoding="utf-8"
        )

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.returncode = 0

    def wait(self, timeout: float | None = None) -> int:
        return self.returncode or 0

    def kill(self) -> None:
        self.returncode = -1


def test_start_reads_the_port_file_and_hides_the_console_window(
    artifacts_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GUI 里启动浏览器不能弹黑框，端口要等文件真写出来才认。"""
    created: list[_PortWritingPopen] = []

    def fake_popen(args: list[str], **kwargs: object) -> _PortWritingPopen:
        process = _PortWritingPopen(args, **kwargs)
        created.append(process)
        return process

    monkeypatch.setattr(bl.subprocess, "Popen", fake_popen)
    info = bl.BrowserInfo("Edge", "msedge.exe")
    with bl.LoginBrowser(info, artifacts_dir / "profile", timeout=5.0) as browser:
        assert browser.port == 9333
        assert browser.browser_ws_url == "ws://127.0.0.1:9333/devtools/browser/abc"
        assert created[0].kwargs["creationflags"] == getattr(subprocess, "CREATE_NO_WINDOW", 0)
        assert created[0].kwargs["stdin"] == subprocess.DEVNULL
        assert created[0].kwargs["stdout"] == subprocess.DEVNULL
        assert browser.profile_note == "", "第一次就起来了，不该说换了目录"


def test_fallback_profile_dirs_are_fresh_and_not_the_original(artifacts_dir: Path) -> None:
    """备用目录不能就是原来那个，也不能互相重复。"""
    profile = artifacts_dir / "browser-profile"
    options = bl.fallback_profile_dirs(profile)

    assert options, "至少要给一个备用目录"
    assert profile not in options
    assert len(set(options)) == len(options)
    assert all(Path(option) != profile for option in options)


@pytest.mark.parametrize("winerror", [5, 32])
def test_profile_failure_recognises_permission_and_lock(winerror: int) -> None:
    """只有「拒绝访问 / 被占用」才值得换目录重试。"""
    assert bl._profile_failure(_win_error(winerror))


def test_profile_failure_reads_posix_errno_too() -> None:
    """同一个毛病在 Linux / macOS 上只有 errno：EACCES / EBUSY 也认。"""
    assert bl._profile_failure(OSError(errno.EACCES, "拒绝访问"))
    assert bl._profile_failure(OSError(errno.EBUSY, "设备或资源忙"))


def test_profile_failure_sees_through_the_wrapped_error() -> None:
    """启动失败是包成 CdpError 抛的，藏在里面那个错也得认出来。"""
    cause = _win_error(5, "拒绝访问。", "profile")
    try:
        try:
            raise cause
        except OSError as exc:
            raise bl.BrowserLoginError(f"启动 Edge 失败：{exc}") from exc
    except bl.BrowserLoginError as wrapped:
        assert bl._profile_failure(wrapped), "外面这层是壳，里面才是真原因"


def test_profile_failure_ignores_other_errors() -> None:
    assert not bl._profile_failure(_win_error(2, "找不到文件"))


def test_devtools_read_failure_recognises_a_refused_local_port() -> None:
    """用户截图那条：端口文件在、调试口却拒绝连接，换目录重试常能好。"""
    assert bl._devtools_read_failure(_win_error(10061, "由于目标计算机积极拒绝，无法连接。"))
    assert bl._devtools_read_failure(OSError(errno.ECONNREFUSED, "连接被拒"))
    assert bl._devtools_read_failure(OSError(errno.ECONNRESET, "连接被重置"))


def test_devtools_read_failure_sees_through_the_wrapped_error() -> None:
    """包成 CdpError 抛的也得认（界面层收到的就是这一层）。"""
    try:
        try:
            raise _win_error(10061, "由于目标计算机积极拒绝，无法连接。")
        except OSError as exc:
            raise bl.CdpError(f"读取浏览器的调试接口失败：{exc}") from exc
    except bl.CdpError as wrapped:
        assert bl._devtools_read_failure(wrapped)


def test_devtools_read_failure_ignores_errors_that_waiting_cannot_fix() -> None:
    """路径写错、端口文件是坏的重试也没用，别白白多起一次浏览器。"""
    assert not bl._devtools_read_failure(_win_error(2, "找不到文件"))
    assert not bl._devtools_read_failure(bl.CdpError("浏览器里没有可用的页面标签"))


class _FakeProcess:
    """最小的假进程：只管回答 poll()，够 ``devtools_failure_hint`` 用。"""

    def __init__(self, exit_code: int | None = None) -> None:
        self.pid = 4321
        self.exit_code = exit_code

    def poll(self) -> int | None:
        return self.exit_code


def test_devtools_failure_hint_stays_quiet_while_the_browser_is_alive() -> None:
    """浏览器还活着就没什么可说：让读接口把预算等满（也许只是慢了半拍）。"""
    browser = bl.LoginBrowser(
        bl.BrowserInfo(name="Fake", path="fake-browser.exe"), Path("profile")
    )
    assert browser.devtools_failure_hint() == ""
    browser.process = _FakeProcess()
    assert browser.devtools_failure_hint() == ""


def test_devtools_failure_hint_reports_a_browser_that_already_died() -> None:
    """进程已经退出就直接报死因，别让用户对着「正在打开浏览器」空等几秒。"""
    browser = bl.LoginBrowser(
        bl.BrowserInfo(name="Fake", path="fake-browser.exe"), Path("profile")
    )
    browser.process = _FakeProcess(3)
    hint = browser.devtools_failure_hint()
    assert "Fake" in hint and "3" in hint
    assert "直接粘贴饼干登录" in hint, "要顺手指一条走得通的路"
    assert not bl._profile_failure(OSError(errno.ENOENT, "找不到文件"))
    assert not bl._profile_failure(RuntimeError("跟目录无关"))


def test_start_switches_profile_when_the_config_one_is_not_writable(
    artifacts_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """真机上的样子：配置目录里的 profile 一碰就是 WinError 5。

    这时必须换一个目录再试，而不是让用户对着「打开浏览器失败」反复点。
    """
    profile = artifacts_dir / "browser-profile"
    refused = profile / "DevToolsActivePort"
    created: list[Path] = []

    def fake_popen(args: list[str], **kwargs: object) -> _PortWritingPopen:
        chosen = Path(
            next(a.split("=", 1)[1] for a in args if a.startswith("--user-data-dir="))
        )
        created.append(chosen)
        if chosen == profile:
            raise _win_error(5, "拒绝访问。", str(refused))
        return _PortWritingPopen(args, **kwargs)

    monkeypatch.setattr(bl.subprocess, "Popen", fake_popen)
    info = bl.BrowserInfo("Edge", "msedge.exe")
    with bl.LoginBrowser(info, profile, timeout=5.0) as browser:
        assert browser.port == 9333
        assert created[0] == profile, "该先试配置目录里那个"
        assert created[1] != profile, "被拒之后要换一个目录"
        assert browser.profile_note, "换了目录得留下话，好让界面说清楚"
        assert str(created[1]) in browser.profile_note


def test_start_still_reports_a_real_failure(
    artifacts_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """浏览器本身起不来（不是目录问题）时，别重试，直接把错报上去。"""
    profile = artifacts_dir / "browser-profile"
    attempts: list[Path] = []

    def fake_popen(args: list[str], **kwargs: object) -> _PortWritingPopen:
        chosen = Path(
            next(a.split("=", 1)[1] for a in args if a.startswith("--user-data-dir="))
        )
        attempts.append(chosen)
        raise _win_error(2, "系统找不到指定的文件。", args[0])

    monkeypatch.setattr(bl.subprocess, "Popen", fake_popen)
    info = bl.BrowserInfo("Edge", "msedge.exe")
    browser = bl.LoginBrowser(info, profile, timeout=5.0)
    with pytest.raises(bl.BrowserLoginError):
        browser.start()
    assert attempts == [profile], "跟目录无关的错不该换目录重试"


class _DeadBrowserPopen:
    """一起来就退出的假浏览器：端口文件永远等不到。"""

    def __init__(self, args: list[str], exit_code: int = 21, **kwargs: object) -> None:
        self.args = list(args)
        self.pid = 4321
        self.returncode = exit_code

    def poll(self) -> int:
        return self.returncode

    def terminate(self) -> None:
        pass

    def wait(self, timeout: float | None = None) -> int:
        return self.returncode


def _fake_browser_info(artifacts_dir: Path, name: str, exe: str) -> bl.BrowserInfo:
    path = artifacts_dir / exe
    path.write_bytes(b"")
    return bl.BrowserInfo(name=name, path=str(path))


def test_dead_on_startup_sees_through_the_wrapped_error() -> None:
    """这条错在真机上会经过好几层包装，认的是文案，不是类型。"""
    dead = bl.BrowserLoginError("Edge 刚起来就退出了（退出码 21）")
    assert bl._dead_on_startup(dead)
    assert bl._dead_on_startup(bl.BrowserLoginError("换目录也白搭", dead))
    # 别的错不能混进来：那会让程序白白多起几次浏览器
    assert not bl._dead_on_startup(_win_error(2, "系统找不到指定的文件。"))
    assert not bl._dead_on_startup(
        bl.BrowserLoginError("跟启动无关的错", _win_error(2, "系统找不到指定的文件。"))
    )


def test_start_switches_to_the_next_browser_when_the_first_one_dies_at_once(
    artifacts_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """真机上的样子：Edge 起来就退（退出码 21）。

    用户装了安全软件、或者 Edge 那个资料目录的旧状态坏了时会这样。程序该做的
    是换一个干净目录、再换一个浏览器试，而不是立刻把错摊给用户看。
    """
    edge = _fake_browser_info(artifacts_dir, "Edge", "msedge.exe")
    chrome = _fake_browser_info(artifacts_dir, "Chrome", "chrome.exe")
    profile = artifacts_dir / "browser-profile"
    launched: list[str] = []

    def fake_popen(args: list[str], **kwargs: object):
        launched.append(args[0])
        if args[0] == edge.path:
            return _DeadBrowserPopen(args)
        return _PortWritingPopen(args, **kwargs)

    monkeypatch.setattr(bl.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(bl, "browser_candidates", lambda info=None, env=None: [edge, chrome])
    with bl.LoginBrowser(edge, profile, timeout=5.0) as browser:
        assert browser.port == 9333
        assert browser.info.path == chrome.path, "第一个起不来就该换下一个"
        assert "Chrome" in browser.browser_note
        assert browser.process is not None
    assert launched.count(edge.path) == 4, "Edge 该把每个候选目录都试一遍"
    assert launched.count(chrome.path) == 1


def test_start_reports_clearly_when_every_browser_dies_at_once(
    artifacts_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """全都起不来时才报错，而且要说清「还能怎么登录」。"""
    edge = _fake_browser_info(artifacts_dir, "Edge", "msedge.exe")
    chrome = _fake_browser_info(artifacts_dir, "Chrome", "chrome.exe")

    def fake_popen(args: list[str], **kwargs: object) -> _DeadBrowserPopen:
        return _DeadBrowserPopen(args)

    monkeypatch.setattr(bl.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(bl, "browser_candidates", lambda info=None, env=None: [edge, chrome])
    browser = bl.LoginBrowser(edge, artifacts_dir / "browser-profile", timeout=5.0)
    with pytest.raises(bl.BrowserLoginError) as caught:
        browser.start()
    message = str(caught.value)
    assert bl._DEAD_ON_STARTUP_MARKER in message
    assert "直接粘贴饼干登录" in message
    assert browser.process is None, "试完要收干净，别留一个已经死掉的进程"


class _StubbornProcess:
    """赖着不走的假进程：terminate 当耳旁风，wait 一直超时。"""

    pid = 4321

    def __init__(self) -> None:
        self.killed = False

    def poll(self) -> int | None:
        return None

    def terminate(self) -> None:
        pass

    def wait(self, timeout: float | None = None) -> int:
        raise subprocess.TimeoutExpired(cmd="msedge", timeout=timeout or 0)

    def kill(self) -> None:
        self.killed = True


def test_stop_escalates_to_a_whole_tree_kill(
    artifacts_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """超时不肯退就收整棵进程树：渲染、GPU 子进程同样占着用户目录里的文件，
    只杀父进程会让界面层删不掉临时目录、在系统临时目录里留下垃圾。"""
    commands: list[list[str]] = []

    def fake_run(command: list[str], **kwargs: object) -> None:
        commands.append(list(command))

    monkeypatch.setattr(bl, "_IS_WINDOWS", True)
    monkeypatch.setattr(bl.subprocess, "run", fake_run)
    browser = bl.LoginBrowser(bl.BrowserInfo("Edge", "msedge.exe"), artifacts_dir / "profile")
    browser.process = _StubbornProcess()  # type: ignore[assignment]
    browser.stop()
    assert browser.process is None
    assert commands, "超时后没有去收进程树"
    assert commands[0][:2] == ["taskkill", "/PID"]
    assert "/T" in commands[0] and "/F" in commands[0]


def test_stop_swallows_a_failing_tree_kill(
    artifacts_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """收尾阶段的报错不许冒出来盖住真正的问题。"""

    def boom(command: list[str], **kwargs: object) -> None:
        raise OSError("taskkill 不可用")

    monkeypatch.setattr(bl, "_IS_WINDOWS", True)
    monkeypatch.setattr(bl.subprocess, "run", boom)
    browser = bl.LoginBrowser(bl.BrowserInfo("Edge", "msedge.exe"), artifacts_dir / "profile")
    browser.process = _StubbornProcess()  # type: ignore[assignment]
    browser.stop()
    assert browser.process is None


def test_kill_process_tree_uses_kill_off_windows(
    artifacts_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bl, "_IS_WINDOWS", False)
    process = _StubbornProcess()
    bl._kill_process_tree(process)  # type: ignore[arg-type]
    assert process.killed


def test_cdp_session_enter_connects() -> None:
    """``with`` 进来就连上：连不上要当场报错，不能装作没事。"""
    with pytest.raises(bl.BrowserLoginError):
        with bl._new_session("ws://127.0.0.1:1/devtools/page/x", timeout=1.0):
            pass


# ---------------------------------------------------------------- 粘贴解析


@pytest.mark.parametrize(
    "text,expected",
    [
        ("a=1; userhash=ABC12345; b=2", "ABC12345"),
        ("userhash=ABC12345", "ABC12345"),
        ("userhash=ABC12345\n", "ABC12345"),
        ("Set-Cookie: userhash=ABC12345; path=/", "ABC12345"),
        ('userhash="ABC12345"', "ABC12345"),
        ('{"cookie": "ABC12345"}', "ABC12345"),
        ('{"userhash":"ABC12345"}', "ABC12345"),
        ('  "ABC12345"  ', "ABC12345"),
        ("ABC12345", "ABC12345"),
        ("userhash\nABC12345", "ABC12345"),
        ("cookie\tABC12345", "ABC12345"),
    ],
)
def test_parse_userhash_input_accepts_every_paste_shape(
    text: str, expected: str
) -> None:
    assert bl.parse_userhash_input(text) == expected


@pytest.mark.parametrize(
    "text",
    ["", "   ", "hello world", "a=1; b=2", "userhash=", "{ not json", "{}"],
)
def test_parse_userhash_input_returns_none_for_junk(text: str) -> None:
    """粘错了要给「没认出来」，不能把一段 HTML 当 userhash 存下去。"""
    assert bl.parse_userhash_input(text) is None


def test_parse_userhash_input_is_not_fooled_by_markup() -> None:
    assert bl.parse_userhash_input('{"userhash":"ABC12345"}') == "ABC12345"
    assert bl.parse_userhash_input("<div>userhash</div>") is None


@pytest.mark.parametrize(
    "value",
    ["ABC12345", "abcdefgh", "Ab3_-9", "123456"],
)
def test_looks_like_userhash_accepts_opaque_tokens(value: str) -> None:
    assert bl.looks_like_userhash(value)


@pytest.mark.parametrize(
    "value",
    ["", "abc", "ABC 12345", "ABC;12345", "ABC12345\n", "饼干饼干饼干", "ABC\t12345"],
)
def test_looks_like_userhash_rejects_everything_else(value: str) -> None:
    assert not bl.looks_like_userhash(value)


# ---------------------------------------------------------------- 应用饼干


def test_build_apply_cookie_script_has_the_needed_pieces() -> None:
    script = bl.build_apply_cookie_script()
    assert script.strip()
    for piece in (
        "switchTo",
        "export",
        "userhash",
        bl.COOKIE_SITE,
        "document.body.innerHTML",
        "await fetch",
        "credentials: 'include'",
    ):
        assert piece in script, piece
    # id 的来源：实测有效的链接正则（列表行的 switchTo/export 链接最可靠）。
    assert r"Cookie\/(?:switchTo|export)\/id\/" in script
    # 链接地址里带 .html：捕获到的 id 得先摘掉这个后缀，否则拼出来是 xxx.html.html。
    assert r"replace(/\.html$/i, '')" in script
    assert "'switchTo/id/' + encodeURIComponent(id) + '.html'" in script
    assert "'export/id/' + encodeURIComponent(id) + '.html'" in script
    # 最新申请的饼干排在列表最后（去重后），要取它而不是第一块。
    assert "ids[ids.length - 1]" in script
    # 取值要覆盖导出接口的三种返回形态。
    assert 'data.cookie' in script
    assert r'/"cookie"\s*:\s*"([^"]+)"/' in script


def test_build_apply_cookie_script_points_at_the_cookie_interfaces() -> None:
    script = bl.build_apply_cookie_script()
    assert '"https://www.nmbxd1.com/Member/User/Cookie/"' in script


class _FakeSession:
    """只实现 evaluate 的假会话：apply_leaf_cookie 的取舍不必真连浏览器。"""

    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.calls: list[tuple[str, bool]] = []

    def evaluate(self, expression: str, await_promise: bool = False) -> str:
        self.calls.append((expression, await_promise))
        return self.reply


def test_apply_leaf_cookie_awaits_the_script_in_the_page() -> None:
    session = _FakeSession("ABC12345")
    assert bl.apply_leaf_cookie(session) == "ABC12345"  # type: ignore[arg-type]
    assert session.calls == [(bl.build_apply_cookie_script(), True)]


def test_apply_leaf_cookie_rejects_anything_that_is_not_a_userhash() -> None:
    assert bl.apply_leaf_cookie(_FakeSession("")) is None  # type: ignore[arg-type]
    for junk in ("<html>跳转提示</html>", "   ", "请先登录"):
        assert bl.apply_leaf_cookie(_FakeSession(junk)) is None  # type: ignore[arg-type]


def test_apply_leaf_cookie_returns_none_when_the_session_is_broken() -> None:
    class Broken:
        def evaluate(self, expression: str, await_promise: bool = False) -> str:
            raise bl.BrowserLoginError("连接断了")

    assert bl.apply_leaf_cookie(Broken()) is None  # type: ignore[arg-type]


# ---------------------------------------------------------------- 契约守卫


def test_public_api_surface_matches_the_ui_contract() -> None:
    """界面层按这些名字并行开发，签名或常量变了要有人立刻发现。"""
    assert bl.USER_DATA_DIR_NAME == "browser-profile"
    assert bl.LOGIN_URL.endswith("/Member/User/Index/login.html")
    assert bl.COOKIE_LIST_PATH == "/Member/User/Cookie/index.html"
    assert bl.COOKIE_SITE == "https://www.nmbxd1.com"
    for name in (
        "known_paths",
        "find_browser",
        "user_data_dir",
        "build_args",
        "parse_devtools_port",
        "browser_open",
        "looks_like_userhash",
        "parse_userhash_input",
        "build_apply_cookie_script",
        "apply_leaf_cookie",
        "ensure_login_page",
        "LoginBrowser",
        "CDPSession",
        "BrowserInfo",
        "BrowserLoginError",
    ):
        assert hasattr(bl, name), name
    assert set(inspect.signature(bl.user_data_dir).parameters) == {"config_dir"}
    assert set(inspect.signature(bl.build_args).parameters) == {
        "info",
        "profile",
        "proxy",
        "start_url",
    }
    assert inspect.signature(bl.build_args).parameters["proxy"].default == ""
    assert inspect.signature(bl.build_args).parameters["start_url"].default == bl.LOGIN_URL
    # 按名字建会话（``_new_session``）会把本站点前缀带进去，界面层走的就是这条路；
    # ``failure_hint`` 是「端口在、口还不通」那几秒里问「浏览器还在不在」的钩子。
    assert set(inspect.signature(bl._new_session).parameters) == {
        "ws_url",
        "timeout",
        "failure_hint",
    }
    assert inspect.signature(bl._new_session).parameters["failure_hint"].default is None
    assert "failure_hint" in inspect.signature(bl.CDPSession.__init__).parameters
    assert inspect.signature(bl.CDPSession.__init__).parameters["timeout"].default == 15.0
    assert inspect.signature(bl.CDPSession.call).parameters["timeout"].default == 15.0
    assert inspect.signature(bl.CDPSession.evaluate).parameters["await_promise"].default is False


def test_library_has_no_gui_dependency() -> None:
    """库里不许有界面依赖：它会牵出一整套 Tcl/Tk，命令行场景根本用不到。"""
    source = Path(bl.__file__).read_text(encoding="utf-8")
    assert "import tkinter" not in source
    assert "from tkinter" not in source


def test_library_only_imports_the_standard_library() -> None:
    """协议层只用标准库：多一个第三方依赖，打包和「装不上」的风险都要跟着涨。"""
    source = Path(bl.__file__).read_text(encoding="utf-8")
    imported = {
        line.split()[1].split(".")[0]
        for line in source.splitlines()
        if line.startswith("import ")
    }
    assert "xdao" not in imported
    assert imported <= {
        "base64",
        "errno",
        "hashlib",
        "itertools",
        "json",
        "os",
        "queue",
        "re",
        "secrets",
        "socket",
        "struct",
        "subprocess",
        "tempfile",
        "threading",
        "time",
        "urllib",
    }


# ---------------------------------------------------------------- 集成（默认跳过）

_REAL_BROWSER = bl.find_browser()
_INTEGRATION_ENABLED = os.environ.get("XDAO_BROWSER_TEST") == "1"


def _remove_tree(path: Path, budget: float = 20.0) -> None:
    """反复尝试删临时用户目录，直到删掉或超出预算。

    Windows 上浏览器进程退出后还会有一小会儿占着目录里的文件，删一次就可能失败。
    界面层在会话结束时做的是同一件事，所以这里也照它做一遍。
    """
    deadline = time.monotonic() + budget
    while True:
        shutil.rmtree(path, ignore_errors=True)
        if not path.exists() or time.monotonic() >= deadline:
            return
        time.sleep(0.5)


@pytest.mark.skipif(
    not (_REAL_BROWSER and _INTEGRATION_ENABLED),
    reason="需要本机装了浏览器，并且显式设 XDAO_BROWSER_TEST=1 才跑（默认跳过）",
)
def test_real_browser_reports_a_devtools_port() -> None:
    """真开一次浏览器，确认整条启动路径在这台机器上走得通。

    用户目录用系统临时目录、跑完删掉：不留长期痕迹，也不会跟用户自己开着的
    浏览器抢 profile 锁。界面层用的是同一套办法。
    连的是 ``/json/list`` 的 HTTP 地址（界面层就是这么给的），顺带钉住两件事：
    没登录时 ``apply_leaf_cookie`` 要优雅地返回 None，临时目录要能被删掉。
    """
    assert _REAL_BROWSER is not None
    profile = Path(tempfile.mkdtemp(prefix="xdao-browser-login-"))
    try:
        with bl.LoginBrowser(_REAL_BROWSER, profile, timeout=45.0) as browser:
            assert 0 < browser.port < 65536
            assert browser.browser_ws_url.startswith(f"ws://127.0.0.1:{browser.port}")
            list_url = browser.devtools_http("/json/list")
            targets = bl._http_json(list_url, 10.0)
            site = bl._pick_site_page(
                [
                    target
                    for target in targets
                    if isinstance(target, dict) and target.get("type") == "page"
                ]
            )
            # 起始地址是 build_args 给的登录页，所以这个标签必须已经在列表里。
            assert site is not None, targets
            with bl._new_session(list_url, timeout=20.0) as session:
                # 必须挂在站内那个标签上：挂到浏览器自己的页面（空标签、自带的对话框页）上时，
                # 脚本里的 fetch 是跨源请求，饼干一律读不到 —— 真机上就是这么发现
                # edge://sync-confirmation-dialog/ 排在登录页前面的。
                assert session._ws_url == site["webSocketDebuggerUrl"]
                # 文档加载到哪一步取决于这台机器连不连得出去，不拿它当断言：
                # 目标列表里报的是「待加载的地址」，连不上时文档会一直停在 about:blank。
                # 但挂错页面（自己浏览器里的 edge:// 页面）必须被这条挡住。
                href = session.current_url()
                assert href.startswith(bl.COOKIE_SITE) or href == "about:blank", href
                assert isinstance(session.read_cookies(), list)
                assert bl.apply_leaf_cookie(session) is None
    finally:
        _remove_tree(profile)
    assert not profile.exists(), "停止浏览器后临时用户目录应当能删掉"
