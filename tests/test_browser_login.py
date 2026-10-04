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
import dataclasses
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

# 抓一份**还没被下面 autouse fixture 换掉的**原实现，给「专测探测本身」的用例还原用。
_REAL_LIVE_BROWSER_DIRS = bl.live_browser_dirs

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


# ---------- retarget()：每轮重挑标签（v0.13.31，真机 m34935） ----------

_STALE_TAB = {
    "type": "page",
    "url": "about:blank",
    "webSocketDebuggerUrl": "ws://127.0.0.1:1/devtools/page/STALE",
}
_SITE_TAB = {
    "type": "page",
    "url": bl.LOGIN_URL,
    "webSocketDebuggerUrl": "ws://127.0.0.1:1/devtools/page/SITE",
}
# v0.13.36：第二条站点标签 —— 用户登进去之后停着的「饼干列表」页。
_COOKIE_TAB = {
    "type": "page",
    "url": bl.COOKIE_SITE + "/Member/User/Cookie/index.html",
    "webSocketDebuggerUrl": "ws://127.0.0.1:1/devtools/page/COOKIE",
}


def _retarget_session(
    monkeypatch: pytest.MonkeyPatch,
    pages: list[dict],
    *,
    attached_ws: str,
    alive: bool = True,
) -> tuple[object, list[str]]:
    """造一条「已经挂着某个标签」的会话；返回（会话, 后来挂上去的地址表）。"""
    monkeypatch.setattr(bl, "_http_json", lambda url, timeout=5.0: pages)
    session = bl._new_session("http://127.0.0.1:9222/json/list")
    session._ws_url = attached_ws  # type: ignore[attr-defined]
    session._sock = object() if alive else None  # type: ignore[attr-defined]
    if not alive:
        session._failure = "调试连接已关闭。"  # type: ignore[attr-defined]
    attached: list[str] = []
    monkeypatch.setattr(session, "_attach", attached.append)
    monkeypatch.setattr(session, "close", lambda: None)
    return session, attached


def test_retarget_moves_onto_the_site_tab(monkeypatch: pytest.MonkeyPatch) -> None:
    """用户把挂着的那个标签关掉另开一个去登录：下一轮就得换过去。

    m34935 的冤案正是这里 —— connect 那一刻挂的标签此后永不更换，程序一路盯着
    旧标签报「页面停在登录页」，用户在新的饼干页里登录成功它也不看。
    """
    session, attached = _retarget_session(
        monkeypatch, [_STALE_TAB, _SITE_TAB], attached_ws=_STALE_TAB["webSocketDebuggerUrl"]
    )
    assert session.retarget() is True
    assert attached == [_SITE_TAB["webSocketDebuggerUrl"]]


def test_retarget_leaves_a_correct_attachment_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """已经挂在站点页上：什么都不做，别把好好的连接拆了重连。"""
    session, attached = _retarget_session(
        monkeypatch, [_STALE_TAB, _SITE_TAB], attached_ws=_SITE_TAB["webSocketDebuggerUrl"]
    )
    assert session.retarget() is False
    assert attached == []


def test_retarget_does_not_chase_the_user_off_their_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """浏览器里还没有本站点的页面、手头的连接又活着：按兵不动。

    用户可能正停在别处看（甚至刚把登录页关了在想要不要登），连接没断就不该
    把他的页面换来换去。
    """
    other = {
        "type": "page",
        "url": "https://example.com/",
        "webSocketDebuggerUrl": "ws://127.0.0.1:1/devtools/page/OTHER",
    }
    session, attached = _retarget_session(
        monkeypatch, [other], attached_ws=_STALE_TAB["webSocketDebuggerUrl"]
    )
    assert session.retarget() is False
    assert attached == []


def test_retarget_revives_a_dead_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    """连接已经断了（标签被关、进程重启）：哪怕没有站点页也先重挂回第一个页面。

    这是「读挂一下不算挂」能缓过来的底气：下一轮 retarget 自己把连接救回来。
    """
    session, attached = _retarget_session(
        monkeypatch,
        [_STALE_TAB],
        attached_ws=_STALE_TAB["webSocketDebuggerUrl"],
        alive=False,
    )
    assert session.retarget() is True
    assert attached == [_STALE_TAB["webSocketDebuggerUrl"]]


def test_retarget_reports_no_tabs_at_all(monkeypatch: pytest.MonkeyPatch) -> None:
    """一个页面标签都不剩：照抛，让界面层数着次数决定收场。"""
    monkeypatch.setattr(bl, "_http_json", lambda url, timeout=5.0: [])
    session = bl._new_session("http://127.0.0.1:9222/json/list")
    with pytest.raises(cdp.CdpError, match="没有可用的页面标签"):
        session.retarget()


def test_retarget_settles_on_a_live_non_login_tab(monkeypatch: pytest.MonkeyPatch) -> None:
    """两页站点标签（没关掉的旧登录页 + 现在的饼干页）：已经挂在饼干页上就稳住。

    v0.13.36（真机 m37565）：以前「挑列表里第一个站点页」，而 ``/json/list`` 的顺序
    没有稳定定义 —— 两页会被轮着挑中，``挂=`` 在 login.html 和饼干页之间来回跳，
    横幅跟着换、读数跟着换上下文，一屏日志自相矛盾，永远定不了案。
    """
    session, attached = _retarget_session(
        monkeypatch,
        [_SITE_TAB, _COOKIE_TAB],
        attached_ws=_COOKIE_TAB["webSocketDebuggerUrl"],
    )
    assert session.retarget() is False
    assert attached == [], "挂着的就是活着的非登录站点页：一克也不动"


def test_retarget_steps_off_a_zombie_login_page_onto_the_live_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """当前挂着的是登录页、浏览器里另有一页非登录站点页：挪到那一页去。

    m34935 的教训不能丢：用户早就登进去停在饼干页，程序死守僵尸登录页就会一路
    报「页面停在登录页」。只挪这一次 —— 下一轮当前页已是非登录页，由上一条用例稳住。
    """
    session, attached = _retarget_session(
        monkeypatch,
        [_SITE_TAB, _COOKIE_TAB],
        attached_ws=_SITE_TAB["webSocketDebuggerUrl"],
    )
    assert session.retarget() is True
    assert attached == [_COOKIE_TAB["webSocketDebuggerUrl"]]


def test_list_page_targets_only_keeps_attachable_pages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """「读罐对账」的 页= 段用的公开读法：只留 type=page 且带调试地址的标签。"""
    pages = [
        _STALE_TAB,
        {
            "type": "extension-host",
            "url": "chrome-extension://abc/",
            "webSocketDebuggerUrl": "ws://127.0.0.1:1/devtools/page/EH",
        },
        {
            "type": "page",
            "url": "https://example.com/x",
            "webSocketDebuggerUrl": "ws://127.0.0.1:1/devtools/page/PLAIN",
        },
        {"type": "page", "url": "https://example.com/y"},  # 没地址挂不上，不算
    ]
    monkeypatch.setattr(bl, "_http_json", lambda url, timeout=5.0: pages)
    session = bl._new_session("http://127.0.0.1:9222/json/list")
    got = session.list_page_targets()
    assert [str(p.get("url")) for p in got] == ["about:blank", "https://example.com/x"]


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


def test_start_reuses_a_window_the_program_left_open(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """正是要修的那个现场（v0.13.33）：上次程序开的窗口还开着 —— 直接接上它。

    真机 m35800：「每次我自己都去手动点了应用，但是仍然抓不到饼干」。要是每回
    启动都开一扇新窗、一个新罐，用户操作着的永远是登录过的那扇旧窗，程序盯着的
    永远是本轮的空罐，两边永远对不上。复用之后同一时刻只有一个罐。
    接上的窗不是我们生的：``process`` 为 None，收尾不碰它（见下一条）。
    """
    live = tmp_path / "xdao-export-browser-profile-4321-1727000000123-0"
    live.mkdir()
    (live / "DevToolsActivePort").write_text("9333\n/devtools/browser/guid-old\n", encoding="utf-8")
    monkeypatch.setattr(bl, "live_browser_dirs", lambda profile: [live])
    popped: list = []
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: popped.append(args))
    browser = bl.LoginBrowser(bl.BrowserInfo("Edge", "msedge.exe"), tmp_path / "profile", timeout=5.0)
    result = browser.start()
    assert popped == [], "还开着的窗口，绝不能再开一扇"
    assert result.process is None
    assert result.port == 9333
    assert result.browser_ws_url == "ws://127.0.0.1:9333/devtools/browser/guid-old"
    assert "接到之前开着的程序窗口" in result.profile_note
    # v0.13.35：接了谁也要写进普查句，并让「真正在用的目录」可查（对账行的罐名用它）。
    assert f"接手了「{live.name}」这一扇" in result.census_note
    assert "探到 1 扇" in result.census_note
    assert result.used_profile == live


def test_census_note_says_a_plain_new_window_was_all_there_was(tmp_path: Path) -> None:
    """没探到任何活窗（autouse 把普查钉成空）：普查句要**明说**没探到（v0.13.35）。

    这句和「探到了但没接上」必须分得开 —— 真机 m36897 若日志写着「探到 2 扇」，
    用户登录的窗多半就是另一扇；写着「没探到」才轮到别的原因。
    """
    browser = bl.LoginBrowser(bl.BrowserInfo("Edge", "msedge.exe"), tmp_path / "profile")
    assert browser._try_attach_live() is None
    assert browser.census_note == "开窗前普查：没探到活着的程序窗口，这次是全新开的一扇。"
    assert browser.used_profile == tmp_path / "profile"


def test_census_note_records_windows_it_could_not_attach(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """探到了、挨个问却没接上：普查句要报数、也要认账「照常新开」（v0.13.35）。"""
    dead = tmp_path / "xdao-export-browser-profile-9-9-0"
    dead.mkdir()  # 没有 DevToolsActivePort —— 读文件就 OSError，逐个跳过
    monkeypatch.setattr(bl, "live_browser_dirs", lambda profile: [dead])
    browser = bl.LoginBrowser(bl.BrowserInfo("Edge", "msedge.exe"), tmp_path / "profile")
    assert browser._try_attach_live() is None
    assert "探到 1 扇" in browser.census_note
    assert "都没接上" in browser.census_note and "照常新开" in browser.census_note


def test_stop_leaves_a_reused_window_alone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """接上的窗不归我杀、目录也不归我删：stop() 得原样留下它。"""
    live = tmp_path / "xdao-export-browser-profile-1-2-0"
    live.mkdir()
    (live / "DevToolsActivePort").write_text("9444\n/devtools/browser/guid-b\n", encoding="utf-8")
    monkeypatch.setattr(bl, "live_browser_dirs", lambda profile: [live])
    browser = bl.LoginBrowser(bl.BrowserInfo("Edge", "msedge.exe"), tmp_path / "profile", timeout=5.0)
    browser.start()
    browser.stop()
    assert live.exists()
    assert (live / "DevToolsActivePort").exists()


def _live_dir(root: Path, name: str, port: int) -> Path:
    """造一扇「还开着的窗」：目录里有端口文件（内容真假不重要，探测那一层会被替身拦下）。"""
    live = root / name
    live.mkdir()
    (live / "DevToolsActivePort").write_text(
        f"{port}\n/devtools/browser/{name}\n", encoding="utf-8"
    )
    return live


def test_reuse_prefers_the_window_whose_jar_has_a_userhash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """v0.13.38（真机 m38110）：两扇窗时接**罐里有饼干**的那扇，不再按谁先开的挑。

    现场：用户在 A 窗里登录着（罐里有 userhash），程序却接上了 B 窗（空罐），
    于是「F12 里明明有饼干、程序说没有」，怎么点「应用」都对不上。谁最后开的窗
    跟「谁登录过」毫无关系，能回答这个问题的只有罐本身。
    """
    first = _live_dir(tmp_path, "xdao-export-browser-profile-1-1-0", 9333)
    second = _live_dir(tmp_path, "xdao-export-browser-profile-2-2-0", 9444)
    monkeypatch.setattr(bl, "live_browser_dirs", lambda profile: [first, second])
    asked: list[str] = []

    def fake_probe(ws_url: str, timeout: float = 0.0) -> str:
        asked.append(ws_url)
        return "f0e1d2c3b4a5" if ws_url.endswith(f"/devtools/browser/{second.name}") else ""

    monkeypatch.setattr(bl, "window_userhash", fake_probe)
    browser = bl.LoginBrowser(
        bl.BrowserInfo("Edge", "msedge.exe"), tmp_path / "profile", timeout=5.0
    )
    result = browser._try_attach_live()
    assert result is not None
    assert asked == [
        f"ws://127.0.0.1:9333/devtools/browser/{first.name}",
        f"ws://127.0.0.1:9444/devtools/browser/{second.name}",
    ], "两扇都得问一遍才知道谁的罐里有东西"
    assert result.used_profile == second, "罐里有 userhash 的那扇优先"
    assert result.port == 9444
    assert "其中 1 扇的罐里留着登录痕迹" in result.census_note
    assert "探到 2 扇" in result.census_note
    assert f"接手了「{second.name}」这一扇" in result.census_note


def test_reuse_keeps_the_candidate_order_when_no_jar_has_a_userhash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """都没有登录痕迹就**不重排**：没有判据时别自作主张，免得每次接上另一扇、横幅乱跳。"""
    first = _live_dir(tmp_path, "xdao-export-browser-profile-3-3-0", 9555)
    second = _live_dir(tmp_path, "xdao-export-browser-profile-4-4-0", 9666)
    monkeypatch.setattr(bl, "live_browser_dirs", lambda profile: [first, second])
    monkeypatch.setattr(bl, "window_userhash", lambda ws_url, timeout=0.0: "")
    browser = bl.LoginBrowser(
        bl.BrowserInfo("Edge", "msedge.exe"), tmp_path / "profile", timeout=5.0
    )
    result = browser._try_attach_live()
    assert result is not None and result.used_profile == first
    assert "登录痕迹" not in result.census_note, "都没登录过就没什么可点名的"
    assert f"接手了「{first.name}」这一扇" in result.census_note


def test_reuse_does_not_ask_about_the_jar_when_only_one_window_is_alive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """只有一扇窗时不问罐：那一问要连一次调试端口，最多的情形没必要为它加开销。"""
    only = _live_dir(tmp_path, "xdao-export-browser-profile-5-5-0", 9777)
    monkeypatch.setattr(bl, "live_browser_dirs", lambda profile: [only])

    def boom(ws_url: str, timeout: float = 0.0) -> str:
        raise AssertionError("只有一扇窗就别去问罐了")

    monkeypatch.setattr(bl, "window_userhash", boom)
    browser = bl.LoginBrowser(
        bl.BrowserInfo("Edge", "msedge.exe"), tmp_path / "profile", timeout=5.0
    )
    result = browser._try_attach_live()
    assert result is not None and result.used_profile == only
    assert result.port == 9777


def test_window_userhash_returns_empty_when_the_window_cannot_be_reached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """问不到就回空串（调用方按「这扇没有登录痕迹」处理），绝不往外抛。"""

    class _AliveButMute:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def connect(self) -> None:
            raise bl.CdpError("连不上")

        def close(self) -> None:  # pragma: no cover —— 没连上就不该有这一句
            raise AssertionError("没连上就不该去关它")

    monkeypatch.setattr(bl, "CDPSession", _AliveButMute)
    assert bl.window_userhash("ws://127.0.0.1:1/devtools/browser/x") == ""


class _JarWindow:
    """假窗口会话（v0.13.45）：按 WebSocket 地址回一份罐，够演 ``window_jar`` 那一族。"""

    jars: dict[str, list[dict]] = {}
    mute: set[str] = set()

    def __init__(self, ws_url: str, **kwargs: object) -> None:
        self.ws_url = ws_url
        self.closed = False

    def connect(self) -> None:
        if self.ws_url in type(self).mute:
            raise bl.CdpError("连不上")

    def read_all_cookies(self) -> list[dict]:
        return [dict(item) for item in type(self).jars.get(self.ws_url, [])]

    def close(self) -> None:
        self.closed = True


def _live_window(
    tmp_path: Path, name: str, port: int, ws_path: str = "/devtools/page/AB"
) -> Path:
    """在 tmp_path 下摆一份「像是还活着」的窗口目录（端口文件就是唯一的证据）。"""
    window = tmp_path / name
    window.mkdir(parents=True, exist_ok=True)
    (window / "DevToolsActivePort").write_text(f"{port}\n{ws_path}\n", encoding="utf-8")
    return window


def _site_cookies(*names: str) -> list[dict]:
    """一份「域沾 nmbxd1」的假罐：名字给全，值无所谓。"""
    return [
        {"name": name, "value": f"v-{name}", "domain": ".nmbxd1.com", "path": "/"}
        for name in names
    ]


def test_window_jar_keeps_only_the_site_cookies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """别家的饼干一律不进程序的手（v0.13.45）：逐窗读也守着这条老规矩。"""
    ws = "ws://127.0.0.1:7001/devtools/page/AB"
    _JarWindow.mute = set()
    _JarWindow.jars = {
        ws: _site_cookies("PHPSESSID")
        + [{"name": "cookie", "value": "x", "domain": ".example.com", "path": "/"}]
    }
    monkeypatch.setattr(bl, "CDPSession", _JarWindow)
    assert [item["name"] for item in bl.window_jar(ws)] == ["PHPSESSID"]


def test_window_jar_returns_empty_when_the_window_cannot_be_reached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """连不上就回空表（调用方按「这一扇里没有」处理），绝不往外抛。"""
    ws = "ws://127.0.0.1:7002/devtools/page/AB"
    _JarWindow.mute = {ws}
    _JarWindow.jars = {ws: _site_cookies("PHPSESSID")}
    monkeypatch.setattr(bl, "CDPSession", _JarWindow)
    assert bl.window_jar(ws) == []


def test_live_window_jars_skips_the_window_we_are_already_reading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """已经把着的那一扇不再问第二遍（v0.13.45）：逐窗是补刀，不是重读自己。"""
    profile = tmp_path / "browser-profile"
    first = _live_window(tmp_path, "browser-profile", 7001)
    second = _live_window(tmp_path, "browser-profile-8-9", 7002)
    _JarWindow.mute = set()
    _JarWindow.jars = {
        "ws://127.0.0.1:7001/devtools/page/AB": _site_cookies("PHPSESSID"),
        "ws://127.0.0.1:7002/devtools/page/AB": _site_cookies("PHPSESSID", "userhash"),
    }
    monkeypatch.setattr(bl, "live_browser_dirs", lambda wanted: [first, second])
    monkeypatch.setattr(bl, "CDPSession", _JarWindow)
    found = bl.live_window_jars(profile, skip_port=7001)
    assert [(name, port) for name, port, _jar in found] == [("browser-profile-8-9", 7002)]


def test_userhash_across_windows_finds_the_cookie_in_another_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """真机 m40502 的形状（v0.13.45）：这一罐只有匿名会话号，userhash 在隔壁那扇窗的罐里。

    这就是「同一扇挂着程序横幅的窗、F12 里有三条饼干，程序却一路只报 PHPSESSID」的
    唯一剩余解释，逐窗读是冲它去的：谁的罐里有就用谁的，并点名是哪一扇。
    """
    profile = tmp_path / "browser-profile"
    other = _live_window(tmp_path, "browser-profile-8-9", 7002)
    _JarWindow.mute = set()
    _JarWindow.jars = {
        "ws://127.0.0.1:7002/devtools/page/AB": _site_cookies("PHPSESSID")
        + [
            {
                "name": "userhash",
                "value": "D-9691%04%02abc",
                "domain": ".nmbxd1.com",
                "path": "/",
            }
        ]
    }
    monkeypatch.setattr(bl, "live_browser_dirs", lambda wanted: [other])
    monkeypatch.setattr(bl, "CDPSession", _JarWindow)
    value, notes = bl.userhash_across_windows(profile, skip_port=7001)
    assert value == "D-9691%04%02abc"
    assert notes == [
        "端口7002 browser-profile-8-9：PHPSESSID、userhash（userhash 在这一扇）"
    ]


def test_userhash_across_windows_keeps_going_when_one_window_is_mute(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """问不动的那一扇不影响别的扇，名单里也老实写「空」（v0.13.45）。"""
    profile = tmp_path / "browser-profile"
    mute = _live_window(tmp_path, "browser-profile-8-9", 7002)
    good = _live_window(tmp_path, "browser-profile-8-10", 7003)
    _JarWindow.mute = {"ws://127.0.0.1:7002/devtools/page/AB"}
    _JarWindow.jars = {
        "ws://127.0.0.1:7003/devtools/page/AB": [
            {
                "name": "userhash",
                "value": "D-9691%04%02abc",
                "domain": ".nmbxd1.com",
                "path": "/",
            }
        ]
    }
    monkeypatch.setattr(bl, "live_browser_dirs", lambda wanted: [mute, good])
    monkeypatch.setattr(bl, "CDPSession", _JarWindow)
    value, notes = bl.userhash_across_windows(profile)
    assert value == "D-9691%04%02abc"
    assert notes == [
        "端口7002 browser-profile-8-9：空",
        "端口7003 browser-profile-8-10：userhash（userhash 在这一扇）",
    ]


def test_userhash_across_windows_stays_quiet_when_the_census_blows_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """普查自己出问题也只是回「没探到」，绝不把异常甩给界面层（v0.13.45）。"""

    def boom(_profile: Path) -> list[Path]:
        raise bl.CdpError("普查炸了")

    monkeypatch.setattr(bl, "live_browser_dirs", boom)
    assert bl.userhash_across_windows(tmp_path / "browser-profile") == ("", [])



def test_program_profile_lines_only_name_our_own_dirs(tmp_path: Path) -> None:
    """进程侧普查的认门规矩（v0.13.37）：自家两族全认，别人家的一个不沾。

    铁律写在 :func:`_parse_program_profile_lines` 的 docstring 里——用户自己浏览器的
    ``User Data`` 目录永远不进名单。这一条用例就是给那条铁律上的锁。
    """
    profile = tmp_path / "browser-profile"
    own = str(profile)
    alt = str(tmp_path / "browser-profile-4321-1727000000")
    new = str(profile / "_new")
    temp = str(Path(tempfile.gettempdir()) / "xdao-export-browser-profile-9-9-0")
    stranger = "C:\\Users\\me\\AppData\\Local\\Google\\Chrome\\User Data"
    other_parent = str(tmp_path.parent / "browser-profile")
    text = "\n".join(
        [
            f"111\tmsedge.exe --user-data-dir={own}",
            f'222\tchrome.exe --user-data-dir="{alt}"',
            f"333\tmsedge.exe --user-data-dir={new} --x=1",
            f"444\tmsedge.exe --user-data-dir={temp}",
            f"555\tmsedge.exe --user-data-dir={stranger}",
            f"666\tapp.exe --user-data-dir={other_parent}",
            f"777\tmsedge.exe --user-data-dir={own}",
            "888\tmsedge.exe --no-profile-flag-at-all",
        ]
    )
    found = bl._parse_program_profile_lines(text, profile)
    # 去重保序：777 的 own 与 111 重复，只剩一份；用户浏览器、别人家的目录都不在。
    assert [str(p) for p in found] == [own, alt, new, temp], found


def test_process_side_census_adopts_a_live_window_the_port_census_missed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """端口文件被删但进程还活着且端口答话：进程侧把它补回接手名单（v0.13.37）。

    备用目录的名字带着当时进程的 PID 和时间戳，新一轮进程猜不到——
    ``live_browser_dirs`` 因此瞎掉（真机 m38110 的一半）。补回来的窗照常接手，
    绝不许再开第二扇。
    """
    live = tmp_path / "browser-profile-4321-1727000000"
    live.mkdir()
    (live / "DevToolsActivePort").write_text("9666\n/devtools/browser/guid-p\n", encoding="utf-8")
    monkeypatch.setattr(bl, "program_profile_dirs", lambda profile: [live])
    monkeypatch.setattr(bl, "_profile_in_use", lambda path: True)
    killed: list = []
    monkeypatch.setattr(bl, "_kill_processes_using_profile", lambda path: killed.append(path))
    browser = bl.LoginBrowser(
        bl.BrowserInfo("Edge", "msedge.exe"), tmp_path / "profile", timeout=5.0
    )
    result = browser._try_attach_live()
    assert result is not None and result.used_profile == live
    assert result.port == 9666
    assert killed == [], "端口还在答话的窗不是僵尸，不许收"
    assert f"接手了「{live.name}」这一扇" in result.census_note
    assert "探到 1 扇" in result.census_note


def test_process_side_census_reaps_zombie_windows_before_opening_a_new_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """端口失联的僵尸窗：先收掉、写进普查句，再照常新开（v0.13.37）。

    真机 m38110 的另一半：饼干窗硬刷新都不弹回登录页（进程罐里有效 userhash），
    程序对账却只见登录页与 PHPSESSID —— 两个进程各拿一个罐。收掉端口失联的旧进程，
    世界上重新只有一扇程序窗口、一个罐；普查句把收掉的目录名字点名写出来。
    """
    zombie = tmp_path / "browser-profile-9-9"
    zombie.mkdir()  # 没有 DevToolsActivePort：真 _profile_in_use 读到文件缺失即 False
    monkeypatch.setattr(bl, "program_profile_dirs", lambda profile: [zombie])
    killed: list = []
    monkeypatch.setattr(bl, "_kill_processes_using_profile", lambda path: killed.append(path))
    browser = bl.LoginBrowser(bl.BrowserInfo("Edge", "msedge.exe"), tmp_path / "profile")
    assert browser._try_attach_live() is None
    assert killed == [zombie], "端口都失联了还不收，留着它继续串门？"
    assert "进程侧探到 1 扇" in browser.census_note
    assert zombie.name in browser.census_note and "已先收掉" in browser.census_note
    assert "没探到活着的程序窗口" in browser.census_note


def test_live_browser_dirs_lists_only_live_windows_newest_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """从 %TEMP% 与已知目录里列出活的：端口不答话的不算，端口文件写得最晚的排前面。"""
    root = tmp_path / "temp"
    root.mkdir()
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(root))
    dirs = {}
    for name, age in (("old", 100.0), ("new", 10.0), ("dead", 5.0)):
        d = root / f"xdao-export-browser-profile-{name}"
        d.mkdir()
        port_file = d / "DevToolsActivePort"
        port_file.write_text("9000\n/devtools/browser/x\n", encoding="utf-8")
        stamp = time.time() - age
        os.utime(port_file, (stamp, stamp))
        dirs[name] = d
    unrelated = root / "unrelated-dir"
    unrelated.mkdir()
    monkeypatch.setattr(bl, "_profile_in_use", lambda p: p != dirs["dead"])
    monkeypatch.setattr(bl, "live_browser_dirs", _REAL_LIVE_BROWSER_DIRS)
    got = bl.live_browser_dirs(tmp_path / "profile")
    assert got == [dirs["new"], dirs["old"]]
    assert unrelated not in got


def test_stale_window_dirs_names_the_mute_ones_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """哑窗那一段（v0.13.46）：有端口文件、端口却不答话的自家目录名，按端口文件时间倒序。

    真机 m40810 的僵局里「逐窗=一句都没有」看不出到底有没有另一扇窗：调试端口已经死了的
    那扇窗，端口普查看不见它、进程还在 —— 名字出现在这里就是那张图。只报**目录名**
    （这行会被用户截图贴到公开版面，盘上路径不写）。答话的、没端口文件的、别人家的目录
    都不许混进来。
    """
    root = tmp_path / "temp"
    root.mkdir()
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(root))
    dirs = {}
    for name, age in (("mute-old", 100.0), ("mute-new", 10.0), ("alive", 5.0), ("noport", 1.0)):
        d = root / f"xdao-export-browser-profile-{name}"
        d.mkdir()
        if name != "noport":
            port_file = d / "DevToolsActivePort"
            port_file.write_text("9000\n/devtools/browser/x\n", encoding="utf-8")
            stamp = time.time() - age
            os.utime(port_file, (stamp, stamp))
        dirs[name] = d
    unrelated = root / "unrelated-dir"
    unrelated.mkdir()
    (unrelated / "DevToolsActivePort").write_text("9000\n", encoding="utf-8")
    monkeypatch.setattr(bl, "_profile_in_use", lambda p: p == dirs["alive"])
    monkeypatch.setattr(bl, "live_browser_dirs", _REAL_LIVE_BROWSER_DIRS)
    got = bl.stale_window_dirs(tmp_path / "profile")
    assert got == [dirs["mute-new"].name, dirs["mute-old"].name]
    assert dirs["alive"].name not in got and dirs["noport"].name not in got
    assert all(str(root) not in name for name in got), "只报目录名，不报盘上路径"


def test_profile_process_pids_counts_holders_of_the_jar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """同罐进程那一段（v0.13.46）：命令行里正拿着这只罐的浏览器进程有几个（只读）。

    真机 m40810：「同一只罐一条也读不到」只有两种形状 —— 还有第二个实例在共用这只罐
    （pid 多于一个），或者用户在自己平时的浏览器里登录（pid 只有一个）。这一手只查、
    不杀；普查被用例关掉（``_PROCESS_CENSUS``）时连 powershell 都不许起。
    """
    profile = tmp_path / "browser-profile"
    profile.mkdir()
    lines = "\n".join(
        [
            f"1111\t\"msedge.exe\" --user-data-dir=\"{profile}\" --remote-debugging-port=0",
            f"2222\t\"msedge.exe\" --user-data-dir=\"{profile}\" --remote-debugging-port=0",
            # 别人家浏览器（占位符写法，公开材料里不许出现本机用户名）。
            "3333\t\"chrome.exe\" --user-data-dir=\"C:\\Users\\你\\AppData\\Local\\Google\\Chrome\\User Data\"",
            f"4444\t\"msedge.exe\" --user-data-dir=\"{profile}\\browser-profile-8-9\"",
            "",
        ]
    )
    assert bl._parse_profile_process_pids(lines, profile) == [1111, 2222]
    assert bl._parse_profile_process_pids("表头\t没有命令行\n", profile) == []
    spawned: list = []
    monkeypatch.setattr(bl, "_PROCESS_CENSUS", False)
    monkeypatch.setattr(bl.subprocess, "run", lambda *a, **k: spawned.append(a))
    assert bl.profile_process_pids(profile) == []
    assert spawned == [], "普查关掉时不许真起一个 powershell"

    class _Done:
        stdout = lines

    monkeypatch.setattr(bl, "_PROCESS_CENSUS", True)
    monkeypatch.setattr(bl.subprocess, "run", lambda *a, **k: _Done())
    assert bl.profile_process_pids(profile) == [1111, 2222]


def test_watch_banner_script_carries_the_identity_line() -> None:
    """横幅脚本要点齐三件事：id 幂等、只在站内出现、说清「自动带上/换一块才点应用」。

    v0.13.34（m36307）：口径改成「登录成功站点就自动带上当前饼干」，『应用』只留给
    换饼干 —— 但这两个词仍要在句里，让人知道去哪换。
    v0.13.35（m36897）：旧窗口上冻着的横幅会装死，所以文案补「十几秒没拿到就点
    『应用』」这句保底，且**已存在的横幅也要刷新文案**（脚本里得有赋值那一手）。
    v0.13.46（m40810）：横条从页面**顶上**挪到**窗底**（顶上的那条把「我的饼干」
    列表的复选框与『应用』按钮压住了一半），并自带一个「×」收得起。
    """
    script = bl.build_watch_banner_script()
    assert bl.WATCH_BANNER_ID in script
    assert "程序正在看这个窗口" in script and "『应用』" in script
    assert "自己带上" in script, "别再吓人说光登录不算完（m36307 证伪）"
    assert "光登录不算完" not in script and "才算数" not in script
    assert "十几秒还没拿到" in script, "点应用这条保底路要留在句里"
    assert "窗口号" not in script, "没传号不许凭空造一个"
    assert "nmbxd1" in script, "站外的页面不该出现横幅"
    assert "getElementById" in script, "同一轮里反复注入要能认出『已经有了』"
    assert "label.textContent = text" in script, "旧横幅不刷新文案就是假信号（m36897）"
    # v0.13.46：横条在窗底（bottom:0，不再 top:0），可收起，且只有那个「×」收得到点击
    assert "bottom:0" in script and "top:0;" not in script, "别再把页面顶上的操作区压住"
    assert "close" in script and "bar.remove()" in script, "横条要能自己收起（m40810）"
    assert "pointer-events:auto" in script, "横条本体不吃点击，只有那个「×」吃"


def test_watch_banner_script_carries_the_window_stamp() -> None:
    """横幅末尾的【窗口号】（v0.13.35）：界面话术报同一个号，肉眼一比识破冻横幅。

    v0.13.46 起传了端口就一起报「窗口号 · 端口」：日志里的「接=端口…」与眼前的窗
    从此对得上号（真机 m40810 的两只罐现场就是靠这个分出来的）。
    """
    script = bl.build_watch_banner_script("3F7A")
    assert "【窗口号 3F7A】" in script
    assert "【窗口号 】" not in script
    assert "端口" not in script, "没传端口就不许写端口"
    with_port = bl.build_watch_banner_script("3F7A", 53051)
    assert "【窗口号 3F7A · 端口53051】" in with_port


def test_remove_watch_banner_script_takes_the_bar_off() -> None:
    """摘横幅的脚本（v0.13.46）：认得出、删掉、没有也不报错，全程吞异常。"""
    script = bl.build_remove_watch_banner_script()
    assert bl.WATCH_BANNER_ID in script
    assert "remove()" in script
    assert "'absent'" in script and "'removed'" in script

    class _Session:
        def evaluate(self, expression: str, await_promise: bool = False) -> str:
            assert bl.WATCH_BANNER_ID in expression
            return "removed"

    assert bl.remove_watch_banner(_Session()) == "removed"  # type: ignore[arg-type]

    class _Broken:
        def evaluate(self, expression: str, await_promise: bool = False) -> str:
            raise bl.CdpError("标签没了")

    assert bl.remove_watch_banner(_Broken()) == ""  # type: ignore[arg-type]


def test_ensure_watch_banner_passes_the_stamp_into_the_page() -> None:
    """注入这手要把号与端口原样带进 evaluate 的脚本里；吞异常的规矩不变。"""
    seen: list[str] = []

    class _Session:
        def evaluate(self, expression: str, await_promise: bool = False) -> str:
            seen.append(expression)
            return "added"

    assert bl.ensure_watch_banner(_Session(), "AB12", 53051) == "added"  # type: ignore[arg-type]
    assert "AB12" in seen[0]
    assert "端口53051" in seen[0]


def test_ensure_watch_banner_swallows_every_failure() -> None:
    """横幅是辅助说明：注入失败不许把登录带崩，也不许不试就跳过。"""
    calls: list[str] = []

    class _Session:
        def evaluate(self, expression: str, await_promise: bool = False) -> str:
            calls.append(expression)
            raise bl.CdpError("目标没了")

    assert bl.ensure_watch_banner(_Session()) == ""  # type: ignore[arg-type]
    assert calls, "至少真试过一次，坏在注入里面也要咽下"


def test_ensure_watch_banner_reports_what_the_page_said() -> None:
    class _Session:
        def evaluate(self, expression: str, await_promise: bool = False) -> str:
            return "present"

    assert bl.ensure_watch_banner(_Session()) == "present"  # type: ignore[arg-type]


@pytest.fixture(autouse=True)
def _no_reuse_of_live_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    """默认让「接上之前开着的程序窗口」这一探（v0.13.33）永远扑空。

    真去探测会读开发机真实的 %TEMP%：恰好留着一扇没关的程序登录窗时，
    下面所有「启动」用例都会变成「接上它」，结果不再确定。
    v0.13.37 起进程侧普查（问操作系统谁在用自家目录）也要钉住——它在
    Windows 开发机上默认是真起 powershell 的，不钉就等于每个用例跑一遍 CIM。
    专测复用路的用例在自己的函数体里重新 monkeypatch —— fixture 先跑、
    函数体后跑，后写的赢。
    """
    monkeypatch.setattr(bl, "live_browser_dirs", lambda profile: [])
    monkeypatch.setattr(bl, "program_profile_dirs", lambda profile: [])


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


class _SilentPopen:
    """假 Popen：进程活着，但永远不写 DevToolsActivePort。

    真机上「目录里那份端口文件是上一扇窗留下的」就是这个样子：新进程起得来、
    可它（或者安全软件）没写出自己的端口，于是文件只剩旧的那一份。
    """

    def __init__(self, args: list[str], **kwargs: object) -> None:
        self.args = list(args)
        self.kwargs = dict(kwargs)
        self.pid = 4321
        self.returncode: int | None = None

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.returncode = 0

    def wait(self, timeout: float | None = None) -> int:
        return self.returncode or 0

    def kill(self) -> None:
        self.returncode = -1


def test_port_file_fresh_recognises_only_a_new_file() -> None:
    """v0.13.38：认不认这份端口文件，只看它有没有在这次启动之后被动过。"""
    assert bl._port_file_fresh("1\n/x\n", 10.0, None, None), "启动前没这个文件：写出来就是新的"
    assert not bl._port_file_fresh(
        "60105\n/old\n", 10.0, "60105\n/old\n", 10.0
    ), "内容与时间戳都没变：这是上次留下的一份"
    assert bl._port_file_fresh(
        "60222\n/new\n", 10.0, "60105\n/old\n", 10.0
    ), "内容变了：浏览器新写的一份"
    assert bl._port_file_fresh(
        "60105\n/old\n", 11.0, "60105\n/old\n", 10.0
    ), "时间戳变了：浏览器又写了一遍"


def test_launch_leaves_a_foreign_port_file_alone_and_never_trusts_it(
    artifacts_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """v0.13.38（真机 m38110 的直接病因）：**不删**别人那份端口文件，也不认它的端口。

    以前的写法是启动前先 unlink 掉 ``DevToolsActivePort``。当候选落到配置目录那份
    持久资料目录上、而那扇窗**还开着**时，这一删就删在了它头上：它从此读不到自己
    的端口，端口普查永远探不到它，程序又开一扇新窗 ⇒ 同一时刻世上两个罐。
    """
    profile = artifacts_dir / "profile"
    profile.mkdir(parents=True, exist_ok=True)
    stale = profile / "DevToolsActivePort"
    stale_text = "60105\n/devtools/browser/old-window\n"
    stale.write_text(stale_text, encoding="utf-8")

    monkeypatch.setattr(bl.subprocess, "Popen", _SilentPopen)
    monkeypatch.setattr(bl, "_KILL_PROFILE_PROCESSES", False)
    browser = bl.LoginBrowser(bl.BrowserInfo("Edge", "msedge.exe"), profile, timeout=0.3)
    with pytest.raises(bl.BrowserLoginError) as raised:
        browser._launch(profile)
    assert "还没等到" in str(raised.value), "旧端口不算数：要一直等到超时"
    assert browser.port == 0
    assert stale.read_text(encoding="utf-8") == stale_text, "别人那扇窗的文件一个字节都不许动"


class _TakeoverPopen:
    """假 Popen：第二个实例把参数交给已经在跑的那一扇，自己干净退出（退出码 0）。

    2026-10-04 真机量到（_scratch/probe_v01338_persistent_profile.py）：在一个
    **还开着**的持久资料目录上再走一次 ``_launch``，Edge 就是这么反应的 ——
    退出码 0，不是 21，也不是崩溃。
    """

    def __init__(self, args: list[str], **kwargs: object) -> None:
        self.args = list(args)
        self.kwargs = dict(kwargs)
        self.pid = 4321
        self.returncode: int | None = 0

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.returncode = 0

    def wait(self, timeout: float | None = None) -> int:
        return self.returncode or 0

    def kill(self) -> None:
        self.returncode = -1


def test_launch_explains_a_takeover_by_an_already_open_window(
    artifacts_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """启动前就有一扇窗开着（端口文件是它留的）、第二个实例干净退出：要说清是被接管了。

    这句提示不点破，用户读到「刚起来就退出了」只会往「资料目录坏了」猜，
    而这里的原因恰好相反 —— 这一份**正被用着**。
    """
    profile = artifacts_dir / "profile"
    profile.mkdir(parents=True, exist_ok=True)
    stale_text = "49714\n/devtools/browser/live-window\n"
    (profile / "DevToolsActivePort").write_text(stale_text, encoding="utf-8")

    monkeypatch.setattr(bl.subprocess, "Popen", _TakeoverPopen)
    monkeypatch.setattr(bl, "_KILL_PROFILE_PROCESSES", False)
    browser = bl.LoginBrowser(bl.BrowserInfo("Edge", "msedge.exe"), profile, timeout=1.0)
    with pytest.raises(bl.BrowserLoginError) as raised:
        browser._launch(profile)
    message = str(raised.value)
    assert "退出码 0" in message
    assert "就有一扇窗开着" in message, "退出码 0 + 启动前有端口文件：是那一扇接管了，不是目录坏了"
    assert (profile / "DevToolsActivePort").read_text(encoding="utf-8") == stale_text


def test_launch_accepts_the_port_file_this_launch_wrote_over_the_old_one(
    artifacts_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """旧端口文件还在盘上、浏览器新写了一份：认新那份（内容或时间戳变了就算新）。"""
    profile = artifacts_dir / "profile"
    profile.mkdir(parents=True, exist_ok=True)
    (profile / "DevToolsActivePort").write_text(
        "60105\n/devtools/browser/old-window\n", encoding="utf-8"
    )

    monkeypatch.setattr(bl.subprocess, "Popen", _PortWritingPopen)
    monkeypatch.setattr(bl, "_KILL_PROFILE_PROCESSES", False)
    browser = bl.LoginBrowser(bl.BrowserInfo("Edge", "msedge.exe"), profile, timeout=5.0)
    browser._launch(profile)
    assert browser.port == 9333
    assert browser.browser_ws_url == "ws://127.0.0.1:9333/devtools/browser/abc"


class _LoudDeadPopen:
    """假 Popen：起来就退（退出码 21），退之前往 stderr 上留下自己的原话。

    v0.13.40 之前 ``_launch`` 把浏览器 stderr 一律丢进 DEVNULL，「刚起来就退出了」
    只剩程序自己猜的那几句；真机上真正的原因（目录被锁、被安全软件拦下、参数被拒绝）
    就在这几行里。
    """

    def __init__(self, args: list[str], **kwargs: object) -> None:
        self.args = list(args)
        self.kwargs = dict(kwargs)
        self.pid = 4321
        self.returncode: int | None = 21
        stream = kwargs.get("stderr")
        if stream is not None and hasattr(stream, "write"):
            stream.write(b"ERROR: could not create the profile directory\n")  # type: ignore[union-attr]
            stream.write(b"ERROR: profile is locked by another process\n")  # type: ignore[union-attr]
            stream.flush()  # type: ignore[union-attr]

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.returncode = 0

    def wait(self, timeout: float | None = None) -> int:
        return self.returncode or 0

    def kill(self) -> None:
        self.returncode = -1


def test_read_stderr_tail_keeps_only_the_last_few_lines(artifacts_dir: Path) -> None:
    """只留最后几行、连成一行、按字符数封顶；文件不在就回空串。"""
    log = artifacts_dir / "noisy.log"
    log.write_text("\n".join(f"line {i}" for i in range(1, 10)) + "\n", encoding="utf-8")
    assert bl.STDERR_TAIL_LINES == 5
    assert bl._read_stderr_tail(log) == "line 5 / line 6 / line 7 / line 8 / line 9"
    assert bl._read_stderr_tail(log, lines=2) == "line 8 / line 9"
    assert bl._read_stderr_tail(log, limit=7) == "line 5 "
    assert bl._read_stderr_tail(artifacts_dir / "missing.log") == ""
    empty = artifacts_dir / "empty.log"
    empty.write_text("\n\n", encoding="utf-8")
    assert bl._read_stderr_tail(empty) == ""


def test_launch_reports_the_browsers_own_last_words(
    artifacts_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """v0.13.40：浏览器「起来就退」时，把它自己写在 stderr 上的那几行带进错误里。"""
    profile = artifacts_dir / "profile"
    profile.mkdir(parents=True, exist_ok=True)
    log = artifacts_dir / "browser-stderr.log"
    monkeypatch.setattr(bl, "_stderr_log_path", lambda: log)
    monkeypatch.setattr(bl.subprocess, "Popen", _LoudDeadPopen)
    monkeypatch.setattr(bl, "_KILL_PROFILE_PROCESSES", False)
    browser = bl.LoginBrowser(bl.BrowserInfo("Edge", "msedge.exe"), profile, timeout=3.0)
    with pytest.raises(bl.BrowserLoginError) as raised:
        browser._launch(profile)
    message = str(raised.value)
    assert "退出码 21" in message
    assert "浏览器自己最后几行话" in message
    assert "profile is locked by another process" in message, "原话要带出来，不能只留程序猜的"
    assert not log.exists(), "读完了就把日志删掉，别给它攒垃圾"


def test_launch_deletes_the_stderr_log_after_a_good_start(
    artifacts_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """起好了，那份 stderr 日志就不该留在 %TEMP% 里。"""
    profile = artifacts_dir / "profile"
    profile.mkdir(parents=True, exist_ok=True)
    log = artifacts_dir / "browser-stderr.log"
    monkeypatch.setattr(bl, "_stderr_log_path", lambda: log)
    monkeypatch.setattr(bl.subprocess, "Popen", _PortWritingPopen)
    monkeypatch.setattr(bl, "_KILL_PROFILE_PROCESSES", False)
    browser = bl.LoginBrowser(bl.BrowserInfo("Edge", "msedge.exe"), profile, timeout=5.0)
    browser._launch(profile)
    assert browser.port == 9333
    assert not log.exists()


def test_launch_still_starts_when_the_stderr_log_cannot_be_opened(
    artifacts_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """临时目录写不进去（安全软件拦下）时照旧起浏览器：少几行话不是起不来的理由。"""
    profile = artifacts_dir / "profile"
    profile.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(bl, "_stderr_log_path", lambda: artifacts_dir / "no-such-dir" / "x.log")
    monkeypatch.setattr(bl.subprocess, "Popen", _PortWritingPopen)
    monkeypatch.setattr(bl, "_KILL_PROFILE_PROCESSES", False)
    browser = bl.LoginBrowser(bl.BrowserInfo("Edge", "msedge.exe"), profile, timeout=5.0)
    browser._launch(profile)
    assert browser.port == 9333
    assert browser._stderr_log is None


def test_launch_cleans_up_the_stderr_log_when_the_browser_cannot_even_start(
    artifacts_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Popen 自己就抛（可执行文件不在 / 被拦）时，那份 stderr 日志也不许留在盘上。"""
    profile = artifacts_dir / "profile"
    profile.mkdir(parents=True, exist_ok=True)
    log = artifacts_dir / "browser-stderr.log"

    def boom(args: list[str], **kwargs: object) -> object:
        raise _win_error(2, "系统找不到指定的文件。")

    monkeypatch.setattr(bl, "_stderr_log_path", lambda: log)
    monkeypatch.setattr(bl.subprocess, "Popen", boom)
    monkeypatch.setattr(bl, "_KILL_PROFILE_PROCESSES", False)
    browser = bl.LoginBrowser(bl.BrowserInfo("Edge", "msedge.exe"), profile, timeout=3.0)
    with pytest.raises(bl.BrowserLoginError) as raised:
        browser._launch(profile)
    assert "启动 Edge 失败" in str(raised.value)
    assert browser._stderr_log is None
    assert not log.exists(), "连浏览器都没起来，日志不该留着"


def test_start_reads_the_port_file_and_hides_the_console_window(
    artifacts_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GUI 里启动浏览器不能弹黑框，端口要等文件真写出来才认。

    v0.13.9 起第一个候选是**新建的临时资料目录**（真机上「自检里浏览器起得来、
    登录却一起来就退」的差别就在资料目录），所以这里也顺带钉住这件事：
    先试临时目录、界面上要说清「这次用的是临时目录」、关窗后目录要清掉。
    """
    created: list[_PortWritingPopen] = []

    def fake_popen(args: list[str], **kwargs: object) -> _PortWritingPopen:
        process = _PortWritingPopen(args, **kwargs)
        created.append(process)
        return process

    monkeypatch.setattr(bl.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(bl, "_KILL_PROFILE_PROCESSES", False)
    info = bl.BrowserInfo("Edge", "msedge.exe")
    profile = artifacts_dir / "profile"
    with bl.LoginBrowser(info, profile, timeout=5.0) as browser:
        assert browser.port == 9333
        assert browser.browser_ws_url == "ws://127.0.0.1:9333/devtools/browser/abc"
        assert created[0].kwargs["creationflags"] == getattr(subprocess, "CREATE_NO_WINDOW", 0)
        assert created[0].kwargs["stdin"] == subprocess.DEVNULL
        assert created[0].kwargs["stdout"] == subprocess.DEVNULL
        chosen = Path(
            next(
                a.split("=", 1)[1]
                for a in created[0].args
                if a.startswith("--user-data-dir=")
            )
        )
        assert chosen != profile, "第一个候选该是新建的临时目录，不是配置目录里那个"
        assert browser.temp_profile == chosen
        assert "临时资料目录" in browser.profile_note, "得跟用户说清登录态没落在老地方"
        assert str(chosen) in browser.profile_note
    assert not chosen.exists(), "关掉登录窗口后临时目录该清掉，别在系统临时目录里攒垃圾"


def test_fresh_profile_dir_is_under_the_system_temp_dir() -> None:
    """临时资料目录要落在系统临时目录下，而且每次都不一样。"""
    first = bl.fresh_profile_dir()
    second = bl.fresh_profile_dir()

    assert first.parent == Path(tempfile.gettempdir())
    assert bl.USER_DATA_DIR_NAME in first.name
    assert first != second, "同一个名字会撞上前一次留下的目录"


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
    """配置目录里那个 profile 起不来（真机上是 WinError 5）时要接着往下试。

    v0.13.9 起第一个候选是新临时目录，配置目录那个排第二：所以这里让**两个都**
    起不来，看它会不会换到第三个候选、并且把「这次用的是哪个目录」写在界面上。
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
        if len(created) == 1:
            # 临时目录那一次：真机上「一起来就退」正是这个样子。
            return _DeadBrowserPopen(args)
        return _PortWritingPopen(args, **kwargs)

    monkeypatch.setattr(bl.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(bl, "_KILL_PROFILE_PROCESSES", False)
    info = bl.BrowserInfo("Edge", "msedge.exe")
    with bl.LoginBrowser(info, profile, timeout=5.0) as browser:
        assert browser.port == 9333
        assert created[0] != profile, "第一个该是新临时目录"
        assert created[1] == profile, "临时目录不行就该试配置目录那个"
        assert created[2] not in (profile, created[0]), "被拒之后要换一个目录"
        assert browser.profile_note, "换了目录得留下话，好让界面说清楚"
        assert str(created[2]) in browser.profile_note


def test_start_tells_the_user_when_it_lands_on_the_persistent_profile(
    artifacts_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """v0.13.37：退到配置目录那份**持久**资料时不能再一声不吭。

    真机（2026-10-03 深夜）：这台机器上新临时目录一起来就退，每次都悄悄落到
    ``browser-profile``；Edge 用同一份目录会把上次登录窗口那一页原样恢复，用户
    看到「已经登录的饼干列表」却读不到饼干，又因为文档说「每次都是全新临时目录」
    而认定窗里有「自己平时保存的饼干」。这句提示要把「窗里的旧页面从哪来、跟你
    自己的浏览器无关、怎么恢复成全新」一次说清。
    """
    profile = artifacts_dir / "browser-profile"

    def fake_popen(args: list[str], **kwargs: object) -> _PortWritingPopen:
        chosen = Path(
            next(a.split("=", 1)[1] for a in args if a.startswith("--user-data-dir="))
        )
        if chosen != profile:
            # 临时目录那一次：真机上「一起来就退」正是这个样子。
            return _DeadBrowserPopen(args)
        return _PortWritingPopen(args, **kwargs)

    monkeypatch.setattr(bl.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(bl, "_KILL_PROFILE_PROCESSES", False)
    info = bl.BrowserInfo("Edge", "msedge.exe")
    with bl.LoginBrowser(info, profile, timeout=5.0) as browser:
        note = browser.profile_note
        assert note, "落到持久目录不能一声不吭"
        assert str(profile) in note, "得把用的是哪份目录点名"
        assert "程序自己存的" in note, "先卸下「碰了我自己浏览器」的担心"
        assert "饼干列表" in note, "窗里旧画面的来历要说破"
        assert "删掉这个文件夹" in note, "得给一条回到全新状态的路"


def test_a_launch_error_that_never_retries_still_deletes_the_temp_profile(
    artifacts_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """启动本身就抛错（连进程都没起来）时也没人删临时目录。

    这种错不走 start() 的「换目录重试」分支：换目录救不了它，于是临时目录
    一直留在 %TEMP% 里。真机上量到过一百多个这样的空壳，一半是这么来的。
    """
    temp_profile = artifacts_dir / "temp-profile"
    monkeypatch.setattr(bl, "_KILL_PROFILE_PROCESSES", False)
    monkeypatch.setattr(bl, "fresh_profile_dir", lambda: temp_profile)

    def boom(args: list[str], **kwargs: object) -> None:
        raise _win_error(2, "系统找不到指定的文件。", args[0])

    monkeypatch.setattr(bl.subprocess, "Popen", boom)
    browser = bl.LoginBrowser(bl.BrowserInfo("假的浏览器", "没有这个.exe"), artifacts_dir / "p")
    with pytest.raises(bl.CdpError):
        browser.start()

    assert not temp_profile.exists(), "起都起不来，临时目录不该留着"
    assert browser.temp_profile is None


def test_failed_attempts_also_delete_the_temp_profile(
    artifacts_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """起不来的那次也要把临时目录删掉：失败尝试同样会在目录里落文件。

    真机上量到过：%TEMP% 里积了一百多个 ``xdao-export-browser-profile-*``，
    其中一批是**空壳** —— 浏览器一起来就退，只在「成功」那一步才记 ``temp_profile``
    的话，这些目录永远没人删；一连失败几次就攒一堆。
    """
    temp_profile = artifacts_dir / "temp-profile"
    other = artifacts_dir / "temp-profile-2"

    def fake_fresh() -> Path:
        # 真机上 ``fresh_profile_dir`` 只给名字、目录由 ``_launch`` 里的 mkdir 建出来；
        # 这里干脆先建出来 —— 不然「目录不该还在」就成了一句空话（没建过的路径当然不在）。
        temp_profile.mkdir(parents=True, exist_ok=True)
        (temp_profile / "Cookies").write_bytes(b"x" * 16)
        return temp_profile

    monkeypatch.setattr(bl, "_KILL_PROFILE_PROCESSES", False)
    monkeypatch.setattr(bl, "fresh_profile_dir", fake_fresh)
    monkeypatch.setattr(bl, "fallback_profile_dirs", lambda profile: [other])
    monkeypatch.setattr(
        bl.subprocess,
        "Popen",
        lambda args, **kwargs: _DeadBrowserPopen(args),
    )
    browser = bl.LoginBrowser(bl.BrowserInfo("Edge", "msedge.exe"), artifacts_dir / "profile")
    monkeypatch.setattr(bl, "browser_candidates", lambda info=None, env=None: [browser.info])
    with pytest.raises(bl.BrowserLoginError):
        browser.start()

    assert not temp_profile.exists(), "失败的那次也要把临时目录清掉"
    assert browser.temp_profile is None, "清过一次就不再记着它"
    assert other.exists(), "备用目录该照常被试到（只是不归我们删）"


def test_each_browser_gets_a_fresh_temp_profile_and_none_is_left_behind(
    artifacts_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """换浏览器时要换一个**新的**临时目录，别把刚删掉的那个又建出来。

    真机上就是这么在 %TEMP% 里留下空壳的：第一个浏览器把临时目录试败、目录当场删掉，
    换下一个浏览器时第一个候选还是那个路径，``_launch`` 的 mkdir 又把它建出来 ——
    可这时 ``temp_profile`` 已经清空了，那一份就永远没人删（量到的空目录就是这么来的）。
    """
    edge = _fake_browser_info(artifacts_dir, "Edge", "msedge.exe")
    chrome = _fake_browser_info(artifacts_dir, "Chrome", "chrome.exe")
    handed_out: list[Path] = []

    def fake_fresh() -> Path:
        fresh = artifacts_dir / f"temp-profile-{len(handed_out) + 1}"
        fresh.mkdir(parents=True, exist_ok=True)
        (fresh / "Cookies").write_bytes(b"x" * 16)
        handed_out.append(fresh)
        return fresh

    monkeypatch.setattr(bl, "_KILL_PROFILE_PROCESSES", False)
    monkeypatch.setattr(bl, "fresh_profile_dir", fake_fresh)
    monkeypatch.setattr(bl, "fallback_profile_dirs", lambda profile: [])
    monkeypatch.setattr(bl, "browser_candidates", lambda info=None, env=None: [edge, chrome])
    monkeypatch.setattr(
        bl.subprocess, "Popen", lambda args, **kwargs: _DeadBrowserPopen(args)
    )
    browser = bl.LoginBrowser(edge, artifacts_dir / "browser-profile", timeout=5.0)
    with pytest.raises(bl.BrowserLoginError):
        browser.start()

    assert len(handed_out) == 2, "两个浏览器各拿一个干净目录，不共用"
    assert not any(path.exists() for path in handed_out), "试完一个都不许留在 %TEMP% 里"
    assert browser.temp_profile is None


def test_a_fallback_profile_under_the_temp_dir_is_cleaned_up_too(
    artifacts_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """备用候选里有一个也建在系统临时目录下（见 ``fallback_profile_dirs``）。

    那也是我们建出来的：「起来就退」的每次尝试都会把那个候选新建一遍，失败之后
    就在 %TEMP% 里留一个空壳（真机上量到过一批短名字的 ``…-<pid>`` 就是这么来的）。
    配置目录下的备用目录不归我们删 —— 那是用户的地盘，留着下次还能用。
    """
    fake_temp = artifacts_dir / "temp-root"
    fake_temp.mkdir(parents=True, exist_ok=True)
    under_temp = fake_temp / f"xdao-export-{bl.USER_DATA_DIR_NAME}-{os.getpid()}"
    fresh = artifacts_dir / "temp-profile"
    other = artifacts_dir / "browser-profile-1"

    def fake_fresh() -> Path:
        fresh.mkdir(parents=True, exist_ok=True)
        return fresh

    monkeypatch.setattr(bl.tempfile, "gettempdir", lambda: str(fake_temp))
    monkeypatch.setattr(bl, "fresh_profile_dir", fake_fresh)
    monkeypatch.setattr(bl, "fallback_profile_dirs", lambda profile: [other, under_temp])
    monkeypatch.setattr(bl, "_KILL_PROFILE_PROCESSES", False)
    monkeypatch.setattr(
        bl, "browser_candidates", lambda info=None, env=None: [bl.BrowserInfo("Edge", "msedge.exe")]
    )
    monkeypatch.setattr(
        bl.subprocess, "Popen", lambda args, **kwargs: _DeadBrowserPopen(args)
    )
    browser = bl.LoginBrowser(
        bl.BrowserInfo("Edge", "msedge.exe"), artifacts_dir / "browser-profile", timeout=5.0
    )
    with pytest.raises(bl.BrowserLoginError):
        browser.start()

    assert not fresh.exists(), "头一个候选（临时目录）要收掉"
    assert not under_temp.exists(), "建在系统临时目录下的备用目录也要收掉"
    assert other.exists(), "配置目录下的备用目录是用户的地盘，别动它"


def _backdate(path: Path, seconds: float) -> None:
    """把路径的修改时间往前拨，让它算「旧的」。"""
    stamp = time.time() - seconds
    os.utime(path, (stamp, stamp))


def _temp_profile(root: Path, name: str, *, age: float | None = None) -> Path:
    """在假的临时根下造一个「我们建的」目录；给了 ``age`` 就把它拨旧。"""
    path = root / name
    path.mkdir(parents=True, exist_ok=True)
    (path / "Cookies").write_text("x", encoding="utf-8")
    if age is not None:
        _backdate(path, age)
    return path


def test_sweep_removes_old_temp_profiles_and_leaves_the_rest(
    artifacts_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """启动时清扫：旧的删掉，别的（刚建的、不是我们的、不是目录的）一概不碰。

    「程序被强杀」这条路没人收尾 —— v0.13.10 只盖住了程序自己收尾的那几种失败。
    """
    root = artifacts_dir / "temp-root"
    root.mkdir(parents=True, exist_ok=True)
    old_login = _temp_profile(
        root, f"xdao-export-{bl.USER_DATA_DIR_NAME}-4242-1000-1", age=2 * bl.SWEEP_MIN_AGE
    )
    old_check = _temp_profile(root, "xdao-browser-check-abc123", age=2 * bl.SWEEP_MIN_AGE)
    new_login = _temp_profile(root, f"xdao-export-{bl.USER_DATA_DIR_NAME}-4242-2000-1")
    new_check = _temp_profile(root, "xdao-browser-check-def456")
    stranger = _temp_profile(root, "some-other-tool", age=2 * bl.SWEEP_MIN_AGE)
    plain_file = root / f"xdao-export-{bl.USER_DATA_DIR_NAME}-4242-3000-1"
    plain_file.write_text("名字像但不是目录", encoding="utf-8")
    _backdate(plain_file, 2 * bl.SWEEP_MIN_AGE)

    monkeypatch.setattr(bl.tempfile, "gettempdir", lambda: str(root))
    monkeypatch.setattr(bl, "_profile_in_use", lambda profile: False)

    assert bl.sweep_stale_temp_profiles() == 2
    assert not old_login.exists(), "登录窗口那份旧的该删"
    assert not old_check.exists(), "自检那份旧的也该删 —— 它同样是我们建的"
    assert new_login.exists(), "刚建出来的多半是别的实例正在用，别碰"
    assert new_check.exists(), "自检那份同上"
    assert stranger.exists(), "不是我们建的目录，一个都不许动"
    assert plain_file.exists(), "名字像但我们没把它当目录，别动"


def test_sweep_skips_a_profile_a_browser_is_still_using(
    artifacts_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """浏览器还开着那份资料目录时（程序被强杀就会这样）不能删。"""
    root = artifacts_dir / "temp-root"
    root.mkdir(parents=True, exist_ok=True)
    busy = _temp_profile(
        root, f"xdao-export-{bl.USER_DATA_DIR_NAME}-4242-1000-1", age=2 * bl.SWEEP_MIN_AGE
    )
    idle = _temp_profile(
        root, f"xdao-export-{bl.USER_DATA_DIR_NAME}-4242-1000-2", age=2 * bl.SWEEP_MIN_AGE
    )

    monkeypatch.setattr(bl.tempfile, "gettempdir", lambda: str(root))
    monkeypatch.setattr(bl, "_profile_in_use", lambda profile: profile == busy)

    assert bl.sweep_stale_temp_profiles() == 1
    assert busy.exists(), "还有浏览器在用，删了等于把它脚下的目录抽走"
    assert not idle.exists(), "没人用的那份照删"


def test_sweep_keeps_the_paths_it_was_told_to_keep(
    artifacts_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``keep`` 里的路径一律不碰（这次会话自己建的那几份）。"""
    root = artifacts_dir / "temp-root"
    root.mkdir(parents=True, exist_ok=True)
    mine = _temp_profile(
        root, f"xdao-export-{bl.USER_DATA_DIR_NAME}-4242-1000-1", age=2 * bl.SWEEP_MIN_AGE
    )
    other = _temp_profile(
        root, f"xdao-export-{bl.USER_DATA_DIR_NAME}-4242-1000-2", age=2 * bl.SWEEP_MIN_AGE
    )

    monkeypatch.setattr(bl.tempfile, "gettempdir", lambda: str(root))
    monkeypatch.setattr(bl, "_profile_in_use", lambda profile: False)

    assert bl.sweep_stale_temp_profiles(keep=[mine]) == 1
    assert mine.exists(), "这次会话正在用的那份不能被自己扫掉"
    assert not other.exists()


def test_sweep_returns_zero_when_it_cannot_even_look(
    artifacts_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """拿不到、列不出临时目录时安静地返回 0（不该在启动线程里炸）。"""

    def no_temp_dir() -> str:
        raise OSError("没有临时目录")

    monkeypatch.setattr(bl.tempfile, "gettempdir", no_temp_dir)
    assert bl.sweep_stale_temp_profiles() == 0

    monkeypatch.setattr(bl.tempfile, "gettempdir", lambda: str(artifacts_dir / "没有这个目录"))
    assert bl.sweep_stale_temp_profiles() == 0


def test_sweep_does_not_count_a_directory_it_could_not_delete(
    artifacts_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """删不掉（被占用、被安全软件拦下）就留着，下一版再说 —— 但不能报成删掉了。"""
    root = artifacts_dir / "temp-root"
    root.mkdir(parents=True, exist_ok=True)
    stubborn = _temp_profile(
        root, f"xdao-export-{bl.USER_DATA_DIR_NAME}-4242-1000-1", age=2 * bl.SWEEP_MIN_AGE
    )

    monkeypatch.setattr(bl.tempfile, "gettempdir", lambda: str(root))
    monkeypatch.setattr(bl, "_profile_in_use", lambda profile: False)
    monkeypatch.setattr(bl.shutil, "rmtree", lambda path, **kwargs: None)

    assert bl.sweep_stale_temp_profiles() == 0
    assert stubborn.exists()


def test_sweep_leaves_a_symlink_with_our_name_alone(
    artifacts_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """名字像但其实是符号链接的，不跟着删 —— 指向哪儿不归我们管。"""
    root = artifacts_dir / "temp-root"
    root.mkdir(parents=True, exist_ok=True)
    target = artifacts_dir / "somebody-elses-dir"
    target.mkdir(parents=True, exist_ok=True)
    link = root / f"xdao-export-{bl.USER_DATA_DIR_NAME}-4242-1000-1"
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("这台机器不给普通用户建符号链接")

    monkeypatch.setattr(bl.tempfile, "gettempdir", lambda: str(root))
    monkeypatch.setattr(bl, "_profile_in_use", lambda profile: False)

    assert bl.sweep_stale_temp_profiles() == 0
    assert link.exists(), "符号链接本身不该被删"
    assert target.exists(), "它指向的目录更不该被删"


def test_profile_in_use_asks_the_devtools_port(
    artifacts_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """判据就是这个：调试端口答话＝还有浏览器在用；文件不在、坏了、端口是死的都算没人用。"""
    profile = artifacts_dir / "profile-in-use"
    profile.mkdir(parents=True, exist_ok=True)
    (profile / "DevToolsActivePort").write_text("5555\n/devtools/browser/abc\n", encoding="utf-8")
    asked: list[str] = []

    def fake_once(url: str, timeout: float = 5.0) -> object:
        asked.append(url)
        return {"Browser": "Edg/1"}

    monkeypatch.setattr(cdp, "_http_json_once", fake_once)
    assert bl._profile_in_use(profile) is True
    assert asked == ["http://127.0.0.1:5555/json/version"], "要按端口文件里那个端口去问"

    def refused(url: str, timeout: float = 5.0) -> object:
        raise bl.CdpError(f"读取浏览器的调试接口失败：{_REFUSED}")

    monkeypatch.setattr(cdp, "_http_json_once", refused)
    assert bl._profile_in_use(profile) is False, "端口是死的，说明浏览器早没了"

    monkeypatch.setattr(cdp, "_http_json_once", fake_once)
    assert bl._profile_in_use(artifacts_dir / "没有这个目录") is False, "连端口文件都没有"
    (profile / "DevToolsActivePort").write_text("这不是数字\n", encoding="utf-8")
    assert bl._profile_in_use(profile) is False, "端口文件是坏的"


def test_is_temp_profile_dir_recognises_both_kinds_and_nothing_else(
    artifacts_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """认位置 + 名字前缀：登录那份、自检那份都算我们的；别人的、配置目录里的都不算。"""
    root = artifacts_dir / "temp-root"
    root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(bl.tempfile, "gettempdir", lambda: str(root))

    assert bl._is_temp_profile_dir(root / f"xdao-export-{bl.USER_DATA_DIR_NAME}-1-2-3")
    assert bl._is_temp_profile_dir(root / "xdao-browser-check-abc123")
    assert not bl._is_temp_profile_dir(root / "some-other-tool")
    assert not bl._is_temp_profile_dir(artifacts_dir / "browser-profile")
    assert not bl._is_temp_profile_dir(
        root / f"xdao-export-{bl.USER_DATA_DIR_NAME}-1-2-3" / "Default"
    ), "系统临时目录**里面**的子目录不算 —— 收尾收的是那一份资料目录本身"


def test_is_fallback_profile_dir_only_accepts_the_strict_name(artifacts_dir: Path) -> None:
    """配置目录下那族备用目录：名字认到「两段都是纯数字」为止（v0.13.39）。

    认松一点就会误收用户的持久目录、或者他自己起的名字 —— 那不是我们的地盘。
    """
    config_dir = artifacts_dir / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    profile = config_dir / bl.USER_DATA_DIR_NAME
    spare = config_dir / f"{bl.USER_DATA_DIR_NAME}-12268-1790850954"

    assert bl._is_fallback_profile_dir(spare, profile)
    assert not bl._is_fallback_profile_dir(profile, profile), "持久目录本体不是备用目录"
    assert not bl._is_fallback_profile_dir(profile / "_new", profile), "_new 在 profile 里面"
    assert not bl._is_fallback_profile_dir(config_dir / f"{bl.USER_DATA_DIR_NAME}-backup", profile)
    assert not bl._is_fallback_profile_dir(
        config_dir / f"{bl.USER_DATA_DIR_NAME}-12268-1790850954-1", profile
    ), "多一段就不像我们建的名字了"
    assert not bl._is_fallback_profile_dir(config_dir / f"{bl.USER_DATA_DIR_NAME}-abc-def", profile)
    assert not bl._is_fallback_profile_dir(config_dir / "some-other-tool-1-2", profile)
    assert not bl._is_fallback_profile_dir(
        artifacts_dir / f"{bl.USER_DATA_DIR_NAME}-12268-1790850954", profile
    ), "不在同一个父目录里，长得再像也不算"


def test_is_our_profile_dir_keeps_the_persistent_one_out(
    artifacts_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """哪些目录算「我们建的、该由我们收尾删掉」：临时族、配置目录备用族、_new。"""
    root = artifacts_dir / "temp-root"
    root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(bl.tempfile, "gettempdir", lambda: str(root))
    config_dir = artifacts_dir / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    profile = config_dir / bl.USER_DATA_DIR_NAME

    assert bl._is_our_profile_dir(config_dir / f"{bl.USER_DATA_DIR_NAME}-7-1000", profile)
    assert bl._is_our_profile_dir(profile / "_new", profile)
    assert bl._is_our_profile_dir(root / f"xdao-export-{bl.USER_DATA_DIR_NAME}-7-1000-1", profile)
    assert not bl._is_our_profile_dir(profile, profile), "用户那份持久的永远不记、也不删"
    assert not bl._is_our_profile_dir(config_dir / "some-other-tool", profile)


def test_sweep_removes_old_fallback_profiles_but_never_the_persistent_one(
    artifacts_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """配置目录里遗留的备用资料目录要收掉；用户那份持久的 ``browser-profile`` 绝不碰。

    真机 2026-10-01 量到过两个没人收的空壳：
    ``browser-profile-12268-1790850954``（96 个文件 / 5.6MB）与
    ``browser-profile-12268-1790850956``（66 个文件 / 6.3MB）—— 名字带着当时进程的
    PID，下一个进程猜不出来，v0.13.39 之前也没人收。
    """
    config_dir = artifacts_dir / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    profile = config_dir / bl.USER_DATA_DIR_NAME
    _temp_profile(config_dir, bl.USER_DATA_DIR_NAME)
    _backdate(profile, 2 * bl.SWEEP_MIN_AGE)
    old_one = _temp_profile(
        config_dir, f"{bl.USER_DATA_DIR_NAME}-12268-1790850954", age=2 * bl.SWEEP_MIN_AGE
    )
    old_two = _temp_profile(
        config_dir, f"{bl.USER_DATA_DIR_NAME}-12268-1790850956", age=2 * bl.SWEEP_MIN_AGE
    )
    fresh = _temp_profile(config_dir, f"{bl.USER_DATA_DIR_NAME}-999-1790850960")
    stranger = _temp_profile(config_dir, "some-other-tool", age=2 * bl.SWEEP_MIN_AGE)
    inner = _temp_profile(profile, "_new", age=2 * bl.SWEEP_MIN_AGE)

    monkeypatch.setattr(bl, "_profile_in_use", lambda path: False)

    assert bl.sweep_stale_fallback_profiles(profile) == 3
    assert not old_one.exists()
    assert not old_two.exists()
    assert not inner.exists(), "profile/_new 是同一族的，也要收"
    assert profile.exists(), "用户那份持久的资料目录永远不许删"
    assert (profile / "Cookies").exists(), "里面的内容更不许动"
    assert fresh.exists(), "刚建出来的多半是别的实例正在用，别碰"
    assert stranger.exists(), "名字不是那副严格模样，一个都不许动"


def test_sweep_fallback_skips_a_dir_still_in_use_and_the_ones_kept(
    artifacts_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """端口还答话的（有浏览器在用）、以及点名要留的，都不许碰。"""
    config_dir = artifacts_dir / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    profile = config_dir / bl.USER_DATA_DIR_NAME
    busy = _temp_profile(config_dir, f"{bl.USER_DATA_DIR_NAME}-7-1000", age=2 * bl.SWEEP_MIN_AGE)
    mine = _temp_profile(config_dir, f"{bl.USER_DATA_DIR_NAME}-7-2000", age=2 * bl.SWEEP_MIN_AGE)
    idle = _temp_profile(config_dir, f"{bl.USER_DATA_DIR_NAME}-7-3000", age=2 * bl.SWEEP_MIN_AGE)

    monkeypatch.setattr(bl, "_profile_in_use", lambda path: path == busy)

    assert bl.sweep_stale_fallback_profiles(profile, keep=[mine]) == 1
    assert busy.exists(), "还有浏览器在用，删了等于把它脚下的目录抽走"
    assert mine.exists(), "这次会话点名要留的不能被自己扫掉"
    assert not idle.exists(), "没人用的那份照删"


def test_sweep_fallback_returns_zero_when_it_cannot_even_look(artifacts_dir: Path) -> None:
    """配置目录列不出来时安静地返回 0（它在启动线程里跑，不许炸）。"""
    profile = artifacts_dir / "没有这个目录" / bl.USER_DATA_DIR_NAME
    assert bl.sweep_stale_fallback_profiles(profile) == 0


def test_a_config_dir_spare_profile_is_cleaned_up_and_the_persistent_one_is_not(
    artifacts_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``_launch`` 也要给配置目录那族备用目录记账 —— 失败时当场收掉，别留空壳。

    v0.13.38 之前只记 ``%TEMP%`` 那一族（``_is_temp_profile_dir``），配置目录里
    ``browser-profile-<PID>-<时间戳>`` 建了没人收，真机上就攒下了那两个。
    """
    edge = _fake_browser_info(artifacts_dir, "Edge", "msedge.exe")
    config_dir = artifacts_dir / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    profile = config_dir / bl.USER_DATA_DIR_NAME
    profile.mkdir(parents=True, exist_ok=True)
    (profile / "Cookies").write_text("用户的登录状态", encoding="utf-8")
    spare = config_dir / f"{bl.USER_DATA_DIR_NAME}-4242-1700000000"

    def fake_popen(args: list[str], **kwargs: object):
        raise _win_error(2, "系统找不到指定的文件。", args[0])

    monkeypatch.setattr(bl.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(bl, "_KILL_PROFILE_PROCESSES", False)
    browser = bl.LoginBrowser(edge, profile, timeout=0.5)

    with pytest.raises(bl.BrowserLoginError):
        browser._launch(spare)
    assert not spare.exists(), "配置目录下的备用目录失败时要当场收掉，不再留空壳"

    with pytest.raises(bl.BrowserLoginError):
        browser._launch(profile)
    assert profile.exists(), "用户那份持久资料目录不许记进清单、更不许删"
    assert (profile / "Cookies").read_text(encoding="utf-8") == "用户的登录状态"


def test_a_self_check_profile_is_cleaned_up_by_stop(
    artifacts_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--check-browser`` 自建的那份资料目录（``xdao-browser-check-…``）也得由我们收。

    以前只认登录那份名字，自检这份不在清单里，收尾只剩 ``browser_check`` 自己那一次
    ``rmtree(ignore_errors=True)`` —— 浏览器还在咽气时它会静默失败，真机 %TEMP% 里
    攒了 5 个。
    """
    fake_temp = artifacts_dir / "temp-root"
    fake_temp.mkdir(parents=True, exist_ok=True)
    check_dir = fake_temp / "xdao-browser-check-abc123"
    check_dir.mkdir()

    monkeypatch.setattr(bl.tempfile, "gettempdir", lambda: str(fake_temp))
    monkeypatch.setattr(bl, "_KILL_PROFILE_PROCESSES", False)
    monkeypatch.setattr(
        bl.subprocess, "Popen", lambda args, **kwargs: _PortWritingPopen(args, **kwargs)
    )
    browser = bl.LoginBrowser(bl.BrowserInfo("Edge", "msedge.exe"), check_dir, timeout=5.0)
    browser.fallback_profiles = False  # 自检那条路就是这样：只用给它的那份目录
    browser.start()

    assert browser.temp_profile is None, "自检不用临时资料目录，用的是它自己建的那份"
    assert check_dir in browser._temp_dirs, "自检那份也要记进清单，收尾才有人删"
    browser.stop()
    assert not check_dir.exists(), "stop() 得把它收掉（自带重试，不等 browser_check 那一次）"


def test_start_still_reports_a_real_failure(
    artifacts_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """浏览器本身起不来（跟目录无关）时，别重试，直接把错报上去。

    只在头一个候选上就抛这种错 —— 换目录 / 换浏览器都救不了它。
    """
    profile = artifacts_dir / "browser-profile"
    attempts: list[Path] = []

    def fake_popen(args: list[str], **kwargs: object) -> _PortWritingPopen:
        chosen = Path(
            next(a.split("=", 1)[1] for a in args if a.startswith("--user-data-dir="))
        )
        attempts.append(chosen)
        raise _win_error(2, "系统找不到指定的文件。", args[0])

    monkeypatch.setattr(bl.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(bl, "_KILL_PROFILE_PROCESSES", False)
    info = bl.BrowserInfo("Edge", "msedge.exe")
    browser = bl.LoginBrowser(info, profile, timeout=5.0)
    with pytest.raises(bl.BrowserLoginError):
        browser.start()
    assert len(attempts) == 1, "跟目录无关的错不该换目录重试"
    assert attempts[0] != profile, "第一个候选是新临时目录"


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
    monkeypatch.setattr(bl, "_KILL_PROFILE_PROCESSES", False)
    monkeypatch.setattr(bl, "browser_candidates", lambda info=None, env=None: [edge, chrome])
    with bl.LoginBrowser(edge, profile, timeout=5.0) as browser:
        assert browser.port == 9333
        assert browser.info.path == chrome.path, "第一个起不来就该换下一个"
        assert "Chrome" in browser.browser_note
        assert browser.process is not None
    # Edge 试的目录数 = 候选数（新临时目录 + 配置目录 + 备用目录），一个不少。
    expected_dirs = 1 + 1 + len(bl.fallback_profile_dirs(profile))
    assert launched.count(edge.path) == expected_dirs, "Edge 该把每个候选目录都试一遍"
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
    monkeypatch.setattr(bl, "_KILL_PROFILE_PROCESSES", False)
    monkeypatch.setattr(bl, "browser_candidates", lambda info=None, env=None: [edge, chrome])
    browser = bl.LoginBrowser(edge, artifacts_dir / "browser-profile", timeout=5.0)
    with pytest.raises(bl.BrowserLoginError) as caught:
        browser.start()
    message = str(caught.value)
    assert bl._DEAD_ON_STARTUP_MARKER in message
    assert "直接粘贴饼干登录" in message
    assert "资料目录" in message, "要说清是目录的事，别让用户以为是浏览器坏了"
    assert browser.process is None, "试完要收干净，别留一个已经死掉的进程"
    assert browser.temp_profile is None or not browser.temp_profile.exists(), (
        "试完的临时目录也要清掉"
    )


def test_dead_persistent_profile_is_quarantined_after_two_strikes(
    artifacts_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """持久资料目录连着两次「刚起来就退出」就改名让位（v0.13.41）。

    真机上这一份目录的旧状态坏了就是**每次**同一个下场：绕过去用临时目录这一轮
    能登录，用户下次点还是读同一句错。所以第二次要动它 —— 但只改名、一个字不删，
    里面存的是他自己的登录状态。
    """
    edge = _fake_browser_info(artifacts_dir, "Edge", "msedge.exe")
    profile = artifacts_dir / "browser-profile"
    profile.mkdir()
    (profile / "Cookies").write_text("用户的登录状态", encoding="utf-8")
    monkeypatch.setattr(bl.subprocess, "Popen", lambda args, **kwargs: _DeadBrowserPopen(args))
    monkeypatch.setattr(bl, "_KILL_PROFILE_PROCESSES", False)
    monkeypatch.setattr(bl, "browser_candidates", lambda info=None, env=None: [edge])
    monkeypatch.setattr(bl, "fresh_profile_dir", lambda: artifacts_dir / "fresh-profile")

    first = bl.LoginBrowser(edge, profile, timeout=0.5)
    with pytest.raises(bl.BrowserLoginError) as caught:
        first.start()
    assert bl._DEAD_ON_STARTUP_MARKER in str(caught.value)
    assert profile.exists(), "第一次只是记一笔，不许动用户的目录"
    assert "改名留在原地" not in str(caught.value)
    assert bl._dead_profile_strikes(profile) == 1

    second = bl.LoginBrowser(edge, profile, timeout=0.5)
    with pytest.raises(bl.BrowserLoginError) as caught:
        second.start()
    message = str(caught.value)
    assert f"连着 {bl.DEAD_PROFILE_STRIKES} 次没起来" in message
    assert "改名留在原地" in message
    assert not profile.exists(), "第二次该让位了"
    backups = list(artifacts_dir.glob(f"{bl.USER_DATA_DIR_NAME}.damaged-*"))
    assert len(backups) == 1, backups
    assert backups[0].name in message, "要让用户知道东西被搬到哪个名字下面了"
    assert (backups[0] / "Cookies").read_text(encoding="utf-8") == "用户的登录状态"


def test_dead_persistent_profile_is_left_alone_while_a_window_still_uses_it(
    artifacts_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """端口还答话 = 有一扇窗正开着它：那是「地上两个罐」的现场，不是死目录。"""
    edge = _fake_browser_info(artifacts_dir, "Edge", "msedge.exe")
    profile = artifacts_dir / "browser-profile"
    profile.mkdir()
    monkeypatch.setattr(bl, "_profile_in_use", lambda path: True)
    browser = bl.LoginBrowser(edge, profile)
    assert browser._quarantine_dead_persistent(True) is None
    assert profile.exists()
    assert not list(artifacts_dir.glob(f"{bl.USER_DATA_DIR_NAME}.damaged-*"))
    assert not (profile / bl.DEAD_PROFILE_MARKER).exists(), "这份账也不许记"


def test_our_own_disposable_profile_is_never_quarantined(tmp_path: Path) -> None:
    """``--check-browser`` 自建在 %TEMP% 的那份归收尾删，改名反而没人收。"""
    fresh = Path(tempfile.mkdtemp(prefix="xdao-browser-check-"))
    try:
        browser = bl.LoginBrowser(bl.BrowserInfo("Edge", "msedge.exe"), fresh)
        assert browser._quarantine_dead_persistent(True) is None
        assert fresh.exists()
    finally:
        shutil.rmtree(fresh, ignore_errors=True)


def test_a_good_start_clears_the_dead_profile_account(
    artifacts_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """这份目录又起来了，账就清了 —— 老账不能留给下一次。"""
    edge = _fake_browser_info(artifacts_dir, "Edge", "msedge.exe")
    profile = artifacts_dir / "browser-profile"
    profile.mkdir()
    assert bl._note_dead_profile_strike(profile, "上一轮刚起来就退出") == 1
    monkeypatch.setattr(bl, "_KILL_PROFILE_PROCESSES", False)
    monkeypatch.setattr(
        bl.subprocess, "Popen", lambda args, **kwargs: _PortWritingPopen(args, **kwargs)
    )
    browser = bl.LoginBrowser(edge, profile, timeout=5.0)
    assert browser._launch(profile).port == 9333
    assert bl._dead_profile_strikes(profile) == 0
    assert not (profile / bl.DEAD_PROFILE_MARKER).exists()


def test_dead_profile_account_reads_writes_and_clears(tmp_path: Path) -> None:
    """这份账本身：读不懂就当 0 次，记一次加一，清掉之后再清一次不许抛。"""
    profile = tmp_path / "browser-profile"
    profile.mkdir()
    assert bl._dead_profile_strikes(profile) == 0, "没有账就是 0 次"
    (profile / bl.DEAD_PROFILE_MARKER).write_text("读不懂的账\n", encoding="utf-8")
    assert bl._dead_profile_strikes(profile) == 0
    assert bl._note_dead_profile_strike(profile, "刚起来就退出") == 1
    assert bl._dead_profile_strikes(profile) == 1
    assert bl._note_dead_profile_strike(profile) == 2
    bl._clear_dead_profile_strikes(profile)
    assert bl._dead_profile_strikes(profile) == 0
    bl._clear_dead_profile_strikes(profile)


def test_quarantine_only_renames_and_never_overwrites_a_backup(tmp_path: Path) -> None:
    """改名让位：名字撞上已存在的备份就换后缀，绝不许覆盖用户以前的目录。"""
    profile = tmp_path / "browser-profile"
    profile.mkdir()
    (profile / "Cookies").write_text("新的", encoding="utf-8")
    taken = tmp_path / "browser-profile.damaged-20261004-010101"
    taken.mkdir()
    (taken / "Cookies").write_text("旧的", encoding="utf-8")

    moved = bl._quarantine_profile_dir(profile, stamp="20261004-010101")

    assert moved == tmp_path / "browser-profile.damaged-20261004-010101-2"
    assert (moved / "Cookies").read_text(encoding="utf-8") == "新的"
    assert (taken / "Cookies").read_text(encoding="utf-8") == "旧的"
    assert not profile.exists()
    # 让位后的名字既不进备用目录那一族、也不是临时目录：两个清扫函数都不碰它。
    assert not bl._is_fallback_profile_dir(moved, profile)
    assert not bl._is_temp_profile_dir(moved)
    assert not bl._is_our_profile_dir(moved, profile)


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


def test_stop_removes_the_temp_profile_it_used(
    artifacts_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """用到临时资料目录就要负责删掉：一次登录几十上百 MB，不能关一个留一个。"""
    temp_profile = artifacts_dir / "temp-profile"
    temp_profile.mkdir(parents=True, exist_ok=True)
    (temp_profile / "Cookies").write_bytes(b"x" * 32)
    monkeypatch.setattr(bl, "_KILL_PROFILE_PROCESSES", False)
    monkeypatch.setattr(bl, "fresh_profile_dir", lambda: temp_profile)

    browser = bl.LoginBrowser(bl.BrowserInfo("Edge", "msedge.exe"), artifacts_dir / "profile")
    monkeypatch.setattr(
        bl.subprocess,
        "Popen",
        lambda args, **kwargs: _PortWritingPopen(args, **kwargs),
    )
    browser.start()
    assert temp_profile.exists(), "刚起来时目录还在（浏览器正开着它）"

    browser.stop()

    assert not temp_profile.exists(), "关掉浏览器后临时目录要删掉"
    assert browser.temp_profile is None
    assert browser.cleanup_temp_profile() is False, "删过一次就不再是「用到了临时目录」"


def test_cleanup_leaves_a_directory_whose_window_is_still_open(
    artifacts_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """端口还答话 = 里面那扇窗还活着：这种目录一个字节都不许动（m38110 那一类事故）。

    真机上量到过：在一份**已经开着窗**的临时资料目录上再启动一次，第二次启动会被
    「已有实例接管」当场退出（退出码 0），随后收尾把那份目录连同还开着的那扇窗一起
    带走。收尾前先问一次端口，就能把这一手挡住。
    """
    busy = artifacts_dir / "temp-profile"
    busy.mkdir(parents=True, exist_ok=True)
    (busy / "Cookies").write_bytes("用户的登录状态".encode("utf-8"))
    (busy / "DevToolsActivePort").write_text("60105\n/devtools/browser/live\n", encoding="utf-8")

    def _explode(profile: Path) -> None:
        raise AssertionError(f"还开着的窗不许动：{profile}")

    monkeypatch.setattr(bl, "_profile_in_use", lambda profile: Path(profile) == busy)
    monkeypatch.setattr(bl, "_kill_processes_using_profile", _explode)

    browser = bl.LoginBrowser(bl.BrowserInfo("Edge", "msedge.exe"), artifacts_dir / "profile")
    browser.temp_profile = busy

    assert browser.cleanup_temp_profile() is False, "有窗开着就别删"
    assert busy.exists(), "目录要留在原地"
    assert (busy / "Cookies").read_bytes() == "用户的登录状态".encode("utf-8"), "里面的登录状态不许丢"
    assert busy in browser._temp_dirs, "留在清单里，下次收尾再问一次端口"


def test_cleanup_temp_profile_gives_up_quietly_when_it_cannot_delete(
    artifacts_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """删不掉也不许抛异常：收尾阶段的错不该盖住真正的问题。"""
    temp_profile = artifacts_dir / "temp-profile"
    temp_profile.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(bl, "_KILL_PROFILE_PROCESSES", False)
    monkeypatch.setattr(bl, "TEMP_PROFILE_WAIT", 0.0)
    monkeypatch.setattr(bl.shutil, "rmtree", lambda path, **kwargs: None)

    browser = bl.LoginBrowser(bl.BrowserInfo("Edge", "msedge.exe"), artifacts_dir / "profile")
    browser.temp_profile = temp_profile
    assert browser.cleanup_temp_profile() is False
    assert browser.temp_profile is None


def test_cleanup_temp_profile_skips_a_directory_that_is_gone(
    artifacts_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """目录本来就不在（没用上临时目录 / 已经删过）就别去惊动进程表。"""
    called: list[Path] = []
    monkeypatch.setattr(
        bl, "_kill_processes_using_profile", lambda profile: called.append(profile)
    )

    browser = bl.LoginBrowser(bl.BrowserInfo("Edge", "msedge.exe"), artifacts_dir / "profile")
    assert browser.cleanup_temp_profile() is False, "没用到临时目录时什么都不做"

    browser.temp_profile = artifacts_dir / "nope"
    assert browser.cleanup_temp_profile() is False
    assert called == [], "目录不在就不该去关什么进程"


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


@pytest.mark.parametrize(
    "value",
    [
        # 百分号转义：站点正文里最常见的一种写法。
        "D-%91%04%02%12%F8%B8E9",
        # 二进制字节被解码出来的样子（替换字符、高位字节、控制字符混在一起）：
        # 旧规则要求「全是可打印 ASCII」，真值就这么被静默丢掉的（m31725）。
        "D-\x91\x04\x02\x12\xf8\xb8E9",
        "\u00d2\u00ea\x04\x02\x12\xf8",
        "\ufffd\ufffdD-%91%04",
    ],
)
def test_looks_like_userhash_keeps_values_that_are_not_plain_ascii(value: str) -> None:
    """v0.13.25 放宽：不再要求「全是可打印 ASCII」，真假交给服务端去判。"""
    assert bl.looks_like_userhash(value)


@pytest.mark.parametrize(
    "value",
    [
        "并没有权限访问这块饼干",
        "<div>userhash</div>",
        "userhash：这块饼干已经过期了",
        "饼干 列表 请 重新登录",
    ],
)
def test_looks_like_userhash_still_rejects_page_text(value: str) -> None:
    """放宽的是「高位字节」，不是「人话」：站点把整句话塞进正文时还得挡住。"""
    assert not bl.looks_like_userhash(value)


# ---------------------------------------------------------------- 应用饼干


COOKIE_LIST_URL = bl.COOKIE_SITE + bl.COOKIE_LIST_PATH


def test_build_find_apply_script_reads_the_page_without_requesting_anything() -> None:
    """「看看当前页有什么」这一步**一个请求都不许发**。

    这是 v0.13.17 的核心教训：v0.13.15 在页面里用 fetch 复刻站点自己的
    ``switchTo`` / ``export`` 两个接口，真机上拿回来的只是站点那张
    「跳转提示」页 —— 那一跳不是 HTTP 重定向，页面里的 fetch 不会去执行它，
    userhash 于是永远种不进浏览器，界面一路等到超时。现在改成「读页面 + 导航」，
    读的这一半必须干干净净只读 DOM。
    """
    script = bl.build_find_apply_script()
    assert script.strip()
    for piece in (
        "location.href",
        "document.querySelectorAll('a[href]')",
        "getElementById('href')",
        "meta[http-equiv=\"refresh\" i]",
        "switchTo",
        "export",
        "new URL(raw, location.href).href",
    ):
        assert piece in script, piece
    # 一个请求都不许发。
    assert "fetch(" not in script
    assert "XMLHttpRequest" not in script
    assert "DOMParser" not in script
    # id 的来源：实测有效的链接正则；后缀 .html 要先摘掉，否则拼出来是 xxx.html.html。
    assert r"Cookie\/(?:switchTo|export)\/id\/" in script
    assert r"replace(/\.html$/i, '')" in script
    # 最新申请的饼干排在列表最后（去重后），要取它而不是第一块。
    assert "state.ids[state.ids.length - 1]" in script
    # 兼容只有表格、没有链接的写法：退回每行第二个单元格。
    assert "querySelectorAll('tr')" in script
    assert "cells[1].textContent" in script


def test_build_find_apply_script_points_at_the_cookie_interfaces() -> None:
    script = bl.build_find_apply_script()
    assert '"https://www.nmbxd1.com/Member/User/Cookie/"' in script
    assert "switchTo/id/" in script


def test_build_find_apply_script_reports_a_countdown_page() -> None:
    """站点自己的倒计时页要能从脚本里认出来（v0.13.21）。

    认不出来就等于「没什么可等的」：调用方读一眼就判定这一页是终点，然后去读饼干罐、
    再去导出页，把还在倒数的标签页拽走 —— 站点自己那一跳再也不会发生（用户 m29500 的
    截图里，标签页正停在「饼干切换成功!／页面自动跳转 等待时间：1」上）。
    """
    script = bl.build_find_apply_script()
    assert "countdown" in script
    assert "等待时间" in script
    # 光有这两个词不够（注释里也写着它们）：要钉住真做判断的那两行。
    assert "state.countdown = /等待时间|自动" in script
    assert "state.jump || state.countdown" in script


class _ScriptedSession:
    """按「地址 → 页面状态」脚本化的假会话：只管导航与饼干罐。

    导航在真机上就是换一份文档，所以这里换的只是「当前地址」，随后 ``evaluate``
    读到的就是那一页登记的状态；饼干罐由用例自己往里放 —— 「应用」成没成，在真机上
    正体现在这里（主站 Set-Cookie 落到罐里）。
    """

    def __init__(
        self,
        pages: dict | None = None,
        cookies: list[dict] | None = None,
        *,
        export_text: str = "",
        url: str = "",
    ) -> None:
        # 两个位置参数都是「容器」，传错了 dict()/list() 会默默吃掉（踩过一次：
        # 把饼干表当成 pages 传进来，dict() 把 {"name": …, "value": …} 变成了
        # {"name": "value"}，用例照样跑、只是永远读不到饼干）。
        assert pages is None or isinstance(pages, dict), "pages 要传「地址 → 页面状态」的字典"
        assert cookies is None or isinstance(cookies, list), "cookies 要传饼干表（列表）"
        self.pages: dict[str, dict] = dict(pages or {})
        self.cookies: list[dict] = list(cookies or [])
        self.export_text = export_text
        self.url = url
        self.navigations: list[str] = []
        self.cookie_reads: list[list[str] | None] = []
        self.evaluations: list[str] = []
        # v0.13.29：整罐读（不问地址那一读）的替身答案，默认空罐 —— 老用例里
        # 「按地址读」就是全部答案，行为不变。
        self.all_cookies: list[dict] = []
        self.read_all_error: Exception | None = None
        self.all_cookie_reads: int = 0
        # v0.13.42：``Storage.getCookies``（F12 面板那一族读法）的替身答案。
        # 真会话里 ``read_all_cookies`` 已经是它和 ``Network.getAllCookies`` 的并集，
        # 替身也照办 —— 分区饼干（CHIPS）只在 ``Storage`` 那一读里出现，替身要是
        # 不并，新用例就演不出真机的形状。
        self.storage_cookies: list[dict] = []
        self.read_storage_error: Exception | None = None
        self.storage_cookie_reads: int = 0
        # v0.13.30：页面内 fetch 与浏览器自报 UA 的替身答案。
        # ``fetch_pages`` 是「地址 → fetch 响应」；没登记的地址 raise —— 真机上对应
        # 「这一页 fetch 根本没送出去」（CSP、换文档），让 _fetch_in_page 吞成 None。
        self.fetch_pages: dict[str, dict] = {}
        self.user_agent = ""
        # v0.13.34：页面自己的 document.cookie 那一路（第四读）的替身答案。
        self.document_cookie = ""
        self.document_cookie_error: Exception | None = None
        # v0.13.36：「读罐对账」里 页= 那一段的替身答案。默认 None 表示
        # 「就当前这一页」；用例想演出僵尸标签就自己填一张 target 表。
        self.page_targets: list[dict] | None = None

    # ---- CDPSession 的那几面 ----
    def call(self, method: str, params: dict | None = None, timeout: float = 15.0) -> dict:
        assert method == "Page.navigate", method
        self.url = str((params or {}).get("url") or "")
        self.navigations.append(self.url)
        return {}

    def current_url(self) -> str:
        return self.url

    def read_cookies(self, urls: list[str] | None = None) -> list[dict]:
        self.cookie_reads.append(list(urls) if urls else None)
        return list(self.cookies)

    def read_all_cookies(self) -> list[dict]:
        self.all_cookie_reads += 1
        if self.read_all_error is not None:
            raise self.read_all_error
        # v0.13.42：真会话这里是 Network 与 Storage 两条路的并集（分区键进去重键），
        # 替身照办 —— 只想演「两条路都瞎」的用例就把 read_all_error 挂上。
        merged = list(self.all_cookies)
        seen = {
            (
                str(item.get("name")),
                str(item.get("domain")),
                str(item.get("path")),
                json.dumps(item.get("partitionKey") or "", sort_keys=True),
            )
            for item in merged
        }
        for item in self.storage_cookies:
            key = (
                str(item.get("name")),
                str(item.get("domain")),
                str(item.get("path")),
                json.dumps(item.get("partitionKey") or "", sort_keys=True),
            )
            if key in seen:
                continue
            seen.add(key)
            merged.append(item)
        return merged

    def read_storage_cookies(self) -> list[dict]:
        self.storage_cookie_reads += 1
        if self.read_storage_error is not None:
            raise self.read_storage_error
        return list(self.storage_cookies)

    def list_page_targets(self) -> list[dict]:
        # v0.13.36：默认「就现在这一页」；想看多标签的用例自己填 page_targets。
        if self.page_targets is not None:
            return [dict(item) for item in self.page_targets]
        return [{"url": self.url, "type": "page"}]

    def evaluate(self, expression: str, await_promise: bool = False) -> str:
        self.evaluations.append(expression)
        if expression == bl.build_find_apply_script():
            return json.dumps(self.pages.get(self.url, {"url": self.url, "kind": "other"}))
        if expression == "document.readyState":
            return "complete"
        if expression == "navigator.userAgent":
            return self.user_agent
        if expression.startswith("(async"):
            # v0.13.30 页面内 fetch：按脚本里那个地址回登记的响应。没登记过 = 这一趟
            # 根本没送出去（CSP 拦脚本、页面正在换文档），raise 给 _fetch_in_page 吞。
            _, sep, rest = expression.partition('fetch("')
            asked = rest.split('"', 1)[0] if sep else ""
            if asked not in self.fetch_pages:
                raise AssertionError(f"未登记的页面内 fetch：{asked}")
            return json.dumps(self.fetch_pages[asked])
        if expression == "location.href":
            return self.url
        if expression.startswith("document.body"):
            return self.export_text
        if expression == "document.cookie":
            # v0.13.34 第四读：只在被盯页确实站在站内时才会问到这 script。
            if self.document_cookie_error is not None:
                raise self.document_cookie_error
            return self.document_cookie
        raise AssertionError(f"意料之外的脚本：{expression[:60]}")


def _login_page_state() -> dict:
    return {"url": bl.LOGIN_URL, "login": True, "jump": "", "kind": "login", "ids": []}


def _list_page_state(*ids: str) -> dict:
    newest = ids[-1]
    return {
        "url": COOKIE_LIST_URL,
        "login": False,
        "jump": "",
        "kind": "list",
        "ids": list(ids),
        "href": f"{bl.COOKIE_SITE}/Member/User/Cookie/switchTo/id/{newest}.html",
    }


def test_fetch_leaf_cookie_never_navigates_a_page_that_is_still_a_login_form() -> None:
    """用户还在输验证码时**绝不能**把他从表单上拽走。

    这是新流程的底线：这一步每几秒就要跑一次，要是它也带导航，用户正打字就会被弹走。
    """
    session = _ScriptedSession({bl.LOGIN_URL: _login_page_state()}, url=bl.LOGIN_URL)
    result = bl.fetch_leaf_cookie(session)  # type: ignore[arg-type]
    assert result.value is None
    assert result.navigated is False
    assert session.navigations == []
    assert "登录" in result.detail


def test_fetch_leaf_cookie_applies_the_newest_cookie_and_reads_it_back() -> None:
    """登录好了：导航到「应用」地址 → 饼干罐里出现 userhash。

    站点自己的「应用」是跳转式的，只能让浏览器自己走；走完主站才会 Set-Cookie。
    这里钉住两件事：走的是**最新那块**饼干的 switchTo 地址，值是从**饼干罐**里
    读回来的（不是从页面正文里抠的）。
    """
    applied = f"{bl.COOKIE_SITE}/Member/User/Cookie/switchTo/id/bbb.html"
    session = _ScriptedSession(
        {bl.LOGIN_URL: _login_page_state(), COOKIE_LIST_URL: _list_page_state("aaa", "bbb")},
        [{"name": "userhash", "value": "ABC12345", "domain": ".nmbxd1.com"}],
        url=COOKIE_LIST_URL,
    )
    result = bl.fetch_leaf_cookie(session)  # type: ignore[arg-type]
    assert result.value == "ABC12345"
    assert result.navigated is True
    assert applied in session.navigations
    assert result.detail == ""
    # 值是从罐里读的：读饼干时问了好几个地址（只问一个会漏）。
    assert session.cookie_reads and len(session.cookie_reads[-1] or []) >= 3


def test_fetch_leaf_cookie_goes_to_the_cookie_list_when_the_user_is_elsewhere() -> None:
    """用户登录完停在论坛/用户首页：那些页面的 DOM 里没有列表，得自己去「饼干」页。

    v0.13.15 的教训：这一步以前靠页面里的 fetch 去要列表，而 fetch 走不完站点那一跳；
    现在是自己导航过去 —— 走的正是浏览器自己的路。
    """
    index = f"{bl.COOKIE_SITE}/"
    session = _ScriptedSession(
        {index: {"url": index, "kind": "other", "ids": []}, COOKIE_LIST_URL: _list_page_state("ccc")},
        [{"name": "userhash", "value": "ABC12345"}],
        url=index,
    )
    result = bl.fetch_leaf_cookie(session)  # type: ignore[arg-type]
    assert result.value == "ABC12345"
    assert session.navigations[0] == COOKIE_LIST_URL


def test_fetch_leaf_cookie_follows_the_sites_own_jump_page() -> None:
    """站点自己的「跳转提示」页：跟着它跳，而不是把它的 HTML 当结果。

    真机实测：请求 ``Member/User/Cookie/index.html`` 会回一张 1569 字节的
    ``<title>跳转提示</title>`` 页面，真正的落地页写在那张页面的 ``a#href`` 里，
    由页面自己的脚本等 3 秒再 ``location.href = href`` —— **不是** HTTP 重定向、
    也**不是** meta refresh，所以在页面里 ``fetch`` 永远走不到那一跳（v0.13.15 就卡在这）。
    """
    landed = f"{bl.COOKIE_SITE}/Member/User/Cookie/index.html?ok=1"
    session = _ScriptedSession(
        {
            bl.LOGIN_URL: {"url": bl.LOGIN_URL, "kind": "jump", "jump": landed, "ids": []},
            landed: _list_page_state("zzz"),
        },
        [{"name": "userhash", "value": "ABC12345"}],
        url=bl.LOGIN_URL,
    )
    result = bl.fetch_leaf_cookie(session)  # type: ignore[arg-type]
    assert result.value == "ABC12345"
    assert session.navigations[0] == landed


def test_fetch_leaf_cookie_reports_the_jump_back_to_the_login_page() -> None:
    """跟着跳转页落回登录页：既要报 navigated（界面层靠它缓一缓），也要说清是登录没成。

    真机上匿名走这一步就是这条：开「饼干」页 → 站点把页面跳到 ``Member/User/Index/login.html``。
    ``navigated`` 报错会让界面层以为「没动过用户的页面」，于是每 5 秒重来一次 —— 用户的标签页
    会被反复拽走，而窗口上什么原因都没写。
    """
    session = _ScriptedSession(
        {
            COOKIE_LIST_URL: {
                "url": COOKIE_LIST_URL,
                "kind": "jump",
                "jump": bl.LOGIN_URL,
                "ids": [],
            },
            bl.LOGIN_URL: _login_page_state(),
        },
        url=COOKIE_LIST_URL,
    )
    result = bl.fetch_leaf_cookie(session)  # type: ignore[arg-type]
    assert result.value is None
    assert result.navigated is True
    assert "登录页" in result.detail
    assert session.navigations == [bl.LOGIN_URL]


def test_fetch_leaf_cookie_says_so_when_the_account_has_no_cookie_yet() -> None:
    """「饼干」页是空的（这个账号还没领过）：给一句能照做的话，别静默。"""
    session = _ScriptedSession(
        {COOKIE_LIST_URL: {"url": COOKIE_LIST_URL, "kind": "empty", "ids": [], "rows": 0}},
        url=COOKIE_LIST_URL,
    )
    result = bl.fetch_leaf_cookie(session)  # type: ignore[arg-type]
    assert result.value is None
    assert result.navigated is True
    assert "饼干" in result.detail


def test_fetch_leaf_cookie_reports_a_list_shape_it_does_not_understand() -> None:
    """列表的写法认不出来：把「认出了几行、几个链接」报出来，好去查站点的改动。"""
    session = _ScriptedSession(
        {
            COOKIE_LIST_URL: {
                "url": COOKIE_LIST_URL,
                "kind": "list",
                "ids": [],
                "rows": 3,
                "links": 0,
            }
        },
        url=COOKIE_LIST_URL,
    )
    result = bl.fetch_leaf_cookie(session)  # type: ignore[arg-type]
    assert result.value is None
    assert "3 行" in result.detail


def test_fetch_leaf_cookie_falls_back_to_the_page_text_of_the_export_view() -> None:
    """跟着跳完了饼干罐里还是空的：再看一眼导出页的正文（三种返回形态都认）。"""
    export_url = f"{bl.COOKIE_SITE}/Member/User/Cookie/export/id/aaa.html"
    session = _ScriptedSession(
        {COOKIE_LIST_URL: _list_page_state("aaa")},
        [],
        export_text='{"cookie": "ABC12345"}',
        url=COOKIE_LIST_URL,
    )
    result = bl.fetch_leaf_cookie(session)  # type: ignore[arg-type]
    assert result.value == "ABC12345"
    assert export_url in session.navigations


class _SiteHoppingSession(_ScriptedSession):
    """站点自己的倒计时页：读第一眼只能看到「等待时间」，跳完之后才轮到落地页。

    真机现场（用户 m29500）：点下「应用」之后站点回的是它自己的倒计时页
    （「饼干切换成功!」+「页面自动跳转 等待时间：1」），那一页上既没有 ``a#href`` 也没有
    meta refresh —— 那是一跳**只能等**的跳转，读一眼是看不到目标的。
    """

    def __init__(
        self,
        countdown_url: str,
        countdown_state: dict,
        landed_url: str,
        landed_state: dict,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self.countdown_url = countdown_url
        self.countdown_state = countdown_state
        self.landed_url = landed_url
        self.landed_state = landed_state
        self.countdown_reads = 0

    def evaluate(self, expression: str, await_promise: bool = False) -> str:
        if expression == bl.build_find_apply_script() and self.url == self.countdown_url:
            self.countdown_reads += 1
            if self.countdown_reads == 1:
                return json.dumps(self.countdown_state)
            # 站点自己那一跳落地：地址换了，主站也在这一跳的响应里种了 userhash。
            self.url = self.landed_url
            self.pages[self.landed_url] = self.landed_state
            self.cookies = [{"name": "userhash", "value": "ABC12345", "domain": ".nmbxd1.com"}]
            return json.dumps(self.landed_state)
        return super().evaluate(expression, await_promise)


def _countdown_state(url: str) -> dict:
    return {"url": url, "login": False, "jump": "", "countdown": True, "kind": "jump", "ids": []}


def test_fetch_leaf_cookie_waits_for_the_sites_own_countdown_page(monkeypatch) -> None:
    """站点回的是它自己的倒计时页时要**等它跳**，不能读一眼就走（用户 m29500）。

    0.13.20 只读一眼页面状态：看不到 ``a#href`` 就判定「没什么可等的」，紧接着把标签页
    拖去导出页 —— 站点自己那一跳永远没发生，userhash 也就永远没种上，界面上只剩一句
    「还没登录」一路到超时（用户截图里标签页正停在那一页上）。
    """
    monkeypatch.setattr(bl, "NAVIGATE_POLL", 0.01)
    switch_url = f"{bl.COOKIE_SITE}/Member/User/Cookie/switchTo/id/aaa.html"
    landed_url = f"{bl.COOKIE_SITE}/Member/User/Index/index.html"
    session = _SiteHoppingSession(
        switch_url,
        _countdown_state(switch_url),
        landed_url,
        {"url": landed_url, "login": False, "jump": "", "kind": "other", "ids": []},
        pages={COOKIE_LIST_URL: _list_page_state("aaa")},
        url=COOKIE_LIST_URL,
    )
    result = bl.fetch_leaf_cookie(session)  # type: ignore[arg-type]
    assert result.value == "ABC12345"
    assert result.navigated is True
    assert session.countdown_reads >= 2, "倒计时页只读了一眼就走了"
    assert switch_url in session.navigations
    export_url = f"{bl.COOKIE_SITE}/Member/User/Cookie/export/id/aaa.html"
    assert export_url not in session.navigations, "还没等站点跳完就把标签页拽去导出页了"


def test_fetch_leaf_cookie_gives_up_on_a_countdown_page_that_never_lands(monkeypatch) -> None:
    """倒计时页要是永远不跳，最多等 ``JUMP_WAIT_SECONDS`` 那么久，不能干等下去。"""
    monkeypatch.setattr(bl, "JUMP_WAIT_SECONDS", 0.05)
    monkeypatch.setattr(bl, "NAVIGATE_POLL", 0.01)
    monkeypatch.setattr(bl, "COOKIE_WAIT_SECONDS", 0.01)
    monkeypatch.setattr(bl, "COOKIE_WAIT_POLL", 0.01)
    switch_url = f"{bl.COOKIE_SITE}/Member/User/Cookie/switchTo/id/aaa.html"
    session = _ScriptedSession(
        {COOKIE_LIST_URL: _list_page_state("aaa"), switch_url: _countdown_state(switch_url)},
        [],
        url=COOKIE_LIST_URL,
    )
    started = time.monotonic()
    result = bl.fetch_leaf_cookie(session)  # type: ignore[arg-type]
    assert result.value is None
    assert time.monotonic() - started < 5.0, "等那一页等太久了"
    assert "userhash" in result.detail


class _LateJarSession(_ScriptedSession):
    """饼干罐慢半拍：第 N 次读才出现 userhash（真机上就是落地那一跳的响应到得晚）。"""

    def __init__(self, put_on_read: int, **kwargs) -> None:
        super().__init__(**kwargs)
        self.put_on_read = put_on_read

    def read_cookies(self, urls: list[str] | None = None) -> list[dict]:
        if len(self.cookie_reads) + 1 >= self.put_on_read:
            self.cookies = [{"name": "userhash", "value": "ABC12345", "domain": ".nmbxd1.com"}]
        return super().read_cookies(urls)


def test_fetch_leaf_cookie_keeps_looking_for_the_cookie_to_land(monkeypatch) -> None:
    """种饼干的是落地那一跳的响应，读早半拍罐里就是空的：要再看两眼。

    这条钉住「不是只读一次」：这个假件只在第 3 次读罐子时才出现 userhash，而且导出页
    正文里什么都没有 —— 值只能是从**等出来**的那次读里拿到的。
    """
    monkeypatch.setattr(bl, "COOKIE_WAIT_POLL", 0.01)
    session = _LateJarSession(
        3, pages={COOKIE_LIST_URL: _list_page_state("aaa")}, url=COOKIE_LIST_URL
    )
    result = bl.fetch_leaf_cookie(session)  # type: ignore[arg-type]
    assert result.value == "ABC12345"
    assert len(session.cookie_reads) >= 3, "只读了一眼饼干罐"


def test_fetch_leaf_cookie_keeps_trying_after_the_page_was_bounced_to_login() -> None:
    """被弹回登录页之后（``waiting_for_login=False``）要接着去领饼干，不能就此停手。

    0.13.20 卡死的正是这一支：程序自己那一趟导出页导航把还没种上 userhash 的标签页弹到
    了登录页，下一次读到的就是「登录页」，早退分支一进来就再也领不到饼干 —— 界面上每 5 秒
    重复一句「这个窗口里还没登录（页面停在登录页）」，看着跟卡死没两样。
    """
    session = _ScriptedSession(
        {bl.LOGIN_URL: _login_page_state(), COOKIE_LIST_URL: _login_page_state()},
        [],
        url=bl.LOGIN_URL,
    )
    result = bl.fetch_leaf_cookie(session, waiting_for_login=False)  # type: ignore[arg-type]
    assert result.value is None
    assert result.navigated is True
    assert COOKIE_LIST_URL in session.navigations, "被弹回登录页之后连试都不试了"
    assert "登录" in result.detail

    # 默认那一档（用户可能正在登录页上打字）还是不许动他的页面。
    quiet = _ScriptedSession({bl.LOGIN_URL: _login_page_state()}, url=bl.LOGIN_URL)
    assert bl.fetch_leaf_cookie(quiet).navigated is False  # type: ignore[arg-type]
    assert quiet.navigations == []


def test_fetch_leaf_cookie_with_navigate_false_only_relooks_at_the_jar() -> None:
    """界面层固定走这条路（v0.13.23）：只看页面和饼干罐，绝不动用户的标签页。"""
    on_login = _ScriptedSession(
        {bl.LOGIN_URL: _login_page_state()}, url=bl.LOGIN_URL
    )
    result = bl.fetch_leaf_cookie(on_login, navigate=False)  # type: ignore[arg-type]
    assert result.value is None
    assert result.navigated is False
    assert on_login.navigations == []
    assert on_login.cookie_reads, "没读饼干罐"
    # 页面停在登录页时照旧如实说「还没登录」：不导航不等于不看页面。
    assert "还没登录" in result.detail

    elsewhere = "https://www.nmbxd1.com/Member/User/Index/index.html"
    on_home = _ScriptedSession(
        {elsewhere: {"url": elsewhere, "kind": "other", "ids": []}}, url=elsewhere
    )
    detail = bl.fetch_leaf_cookie(on_home, navigate=False).detail  # type: ignore[arg-type]
    assert "userhash" in detail
    assert on_home.navigations == []

    ready = _ScriptedSession(cookies=[{"name": "userhash", "value": "ABC12345"}], url=bl.LOGIN_URL)
    assert bl.fetch_leaf_cookie(ready, navigate=False).value == "ABC12345"  # type: ignore[arg-type]
    assert ready.navigations == []


def test_fetch_leaf_cookie_will_not_work_on_a_page_outside_the_site() -> None:
    """用户自己把那个窗口导到别处去了：说清楚，别在别人的站点上做动作。"""
    outside = "https://example.com/"
    session = _ScriptedSession(
        {outside: {"url": outside, "kind": "other", "ids": []}}, url=outside
    )
    result = bl.fetch_leaf_cookie(session)  # type: ignore[arg-type]
    assert result.value is None
    assert result.navigated is False
    assert session.navigations == []
    assert "example.com" in result.detail


def test_fetch_leaf_cookie_reports_a_broken_session_instead_of_raising() -> None:
    """会话断了：给一句人话（界面把它挂到窗口上），不要往上抛。"""

    class Broken:
        def current_url(self) -> str:
            raise bl.CdpError("连接断了")

        def evaluate(self, expression: str, await_promise: bool = False) -> str:
            raise bl.CdpError("连接断了")

        def call(self, method: str, params: dict | None = None, timeout: float = 15.0) -> dict:
            raise bl.CdpError("连接断了")

        def read_cookies(self, urls: list[str] | None = None) -> list[dict]:
            raise bl.CdpError("连接断了")

    result = bl.fetch_leaf_cookie(Broken())  # type: ignore[arg-type]
    assert result.value is None
    assert "连接断了" in result.detail
    # 只要值的老签名照样只回 None。
    assert bl.apply_leaf_cookie(Broken()) is None  # type: ignore[arg-type]


def test_read_site_cookies_hands_back_the_whole_jar() -> None:
    """v0.13.22：界面层要的不是 userhash，而是**整罐**会话饼干。

    HTTP 那条路（``XdaoClient.apply_cookie``）认站点会话，光有 userhash 跑不动
    「应用饼干」那一串跳转。
    """
    session = _ScriptedSession(
        cookies=[
            {"name": "PHPSESSID", "value": "abc123", "domain": ".nmbxd1.com"},
            {"name": "userhash", "value": "ABC12345", "domain": ".nmbxd1.com"},
        ],
        url=bl.LOGIN_URL,
    )
    cookies = bl.read_site_cookies(session)  # type: ignore[arg-type]
    assert [item["name"] for item in cookies] == ["PHPSESSID", "userhash"]
    assert session.cookie_reads, "没读饼干罐"
    assert session.cookie_reads[0], "读饼干要把问的地址带上（只看域名看不到路径限定的那块）"
    # 一个页面都不许动。
    assert session.navigations == []


def test_read_site_cookies_raises_so_the_caller_can_say_what_happened() -> None:
    """会话断了就抛（v0.13.23）：界面层要把这句话原样写进提示和运行日志。

    以前这里吞成空列表，界面层把「读不出来」和「罐里没登录」当成同一件事，
    HTTP 那条路一声不响地被跳过 —— 用户截图里只剩 CDP 那句「还没登录」（m30629）。
    """

    class Broken:
        def current_url(self) -> str:
            raise bl.CdpError("连接断了")

        def read_cookies(self, urls: list[str] | None = None) -> list[dict]:
            raise bl.CdpError("连接断了")

    with pytest.raises(bl.CdpError):
        bl.read_site_cookies(Broken())  # type: ignore[arg-type]


def test_read_site_cookies_keeps_an_empty_jar_as_an_empty_list() -> None:
    """罐里真的什么都没有：这是「没登录」，不是「读不到」，得能分开。"""
    session = _ScriptedSession(cookies=[], url=bl.LOGIN_URL)
    assert bl.read_site_cookies(session) == []  # type: ignore[arg-type]


def test_read_site_cookies_merges_what_the_address_filtered_read_missed() -> None:
    """v0.13.29：按地址那读漏掉的 userhash，要靠整罐读补回来。

    真机上的情形（2026-10-02 四张截图）：F12 的 Application 面板里 userhash
    明明白白在罐里，``Network.getCookies`` 却怎么问都不回它 —— 对话框名单里
    从头到尾没有 userhash，而用户拿同一块饼干发串发得出去。过滤维度定位不
    出来，那就两读都问：按地址的照旧，整罐的补漏。
    """
    hidden = {
        "name": "userhash",
        "value": "D-9691%04%02abc",
        "domain": "www.nmbxd1.com",
        "path": "/",
    }
    session = _ScriptedSession(
        cookies=[{"name": "memberUserspapapa", "value": "G%C8%8D", "domain": ".nmbxd1.com"}],
        url=bl.LOGIN_URL,
    )
    session.all_cookies = [
        dict(hidden),
        {"name": "memberUserspapapa", "value": "G%C8%8D", "domain": ".nmbxd1.com"},
        # 别家站点的饼干不许混进本站的罐子（整罐读回的是**所有**域）。
        {"name": "sid", "value": "xyz", "domain": ".example.com"},
    ]
    cookies = bl.read_site_cookies(session)  # type: ignore[arg-type]
    names = [item["name"] for item in cookies]
    assert names == ["memberUserspapapa", "userhash"], names
    assert bl.userhash_from_cookies(cookies) == "D-9691%04%02abc"
    assert session.all_cookie_reads == 1, "整罐读该问一次"


def test_read_site_cookies_survives_a_broken_whole_jar_read() -> None:
    """整罐读失败不拖累主路（v0.13.29）：按地址那读的结果照旧交出去。"""
    session = _ScriptedSession(
        cookies=[{"name": "PHPSESSID", "value": "abc123", "domain": ".nmbxd1.com"}],
        url=bl.LOGIN_URL,
    )
    session.read_all_error = bl.CdpError("浏览器不认 getAllCookies")
    cookies = bl.read_site_cookies(session)  # type: ignore[arg-type]
    assert [item["name"] for item in cookies] == ["PHPSESSID"]


def _unconnected_session(answers: dict) -> tuple["cdp.CDPSession", list[str]]:
    """造一条**没连过**的 CDPSession（构造函数只存参数），把 ``call`` 换成查表。"""
    session = cdp.CDPSession("ws://127.0.0.1:1/devtools/page/probe")
    asked: list[str] = []

    def fake_call(method: str, params: dict | None = None, timeout: float = 15.0) -> dict:
        asked.append(method)
        answer = answers[method]
        if isinstance(answer, Exception):
            raise answer
        return answer

    session.call = fake_call  # type: ignore[method-assign]
    return session, asked


def _partitioned_userhash(value: str = "D-9691%04%02abc", top: str = "https://www.nmbxd1.com") -> dict:
    return {
        "name": "userhash",
        "value": value,
        "domain": ".nmbxd1.com",
        "path": "/",
        "secure": True,
        "partitionKey": {"topLevelSite": top},
    }


def test_read_all_cookies_merges_the_storage_read_for_partitioned_cookies() -> None:
    """v0.13.42：``Storage.getCookies`` 是 F12 那一族的读法，与整罐读各问一次再并起来。

    真机 m39918：同一扇窗的 F12「应用程序 → Cookie」里躺着 ``userh…``（70 字节），
    对账行却写 ``全罐=2``、``原始userhash=无`` —— 某一条读法漏了（真机复验里，按地址
    读就是会漏掉带 ``Partitioned`` 的饼干；v0.13.44 复核：``Network.getAllCookies``
    在本机 Edge 上读得到，并集留着的理由是不赌浏览器版本与实现差异）。
    现在两读并起来，去重键带上分区键。
    """
    shared = {"name": "PHPSESSID", "value": "S", "domain": ".nmbxd1.com", "path": "/"}
    member = {"name": "memberUserspapapa", "value": "M", "domain": ".nmbxd1.com", "path": "/"}
    session, asked = _unconnected_session(
        {
            "Network.getAllCookies": {"cookies": [shared, member]},
            "Storage.getCookies": {"cookies": [dict(shared), _partitioned_userhash()]},
        }
    )
    merged = session.read_all_cookies()
    assert asked == ["Network.getAllCookies", "Storage.getCookies"], "两条老路都要问"
    assert [item["name"] for item in merged] == ["PHPSESSID", "memberUserspapapa", "userhash"]
    assert bl.userhash_from_cookies(merged) == "D-9691%04%02abc"


def test_read_all_cookies_keeps_the_surviving_read_when_one_side_fails() -> None:
    """一条路报错、另一条有货：用有货那条（老规矩，容不容忍由调用方决定）。"""
    session, asked = _unconnected_session(
        {
            "Network.getAllCookies": cdp.CdpError("浏览器不认 getAllCookies"),
            "Storage.getCookies": {"cookies": [_partitioned_userhash("ABCDEF12")]},
        }
    )
    assert [item["name"] for item in session.read_all_cookies()] == ["userhash"]
    assert asked == ["Network.getAllCookies", "Storage.getCookies"]


def test_read_all_cookies_raises_only_when_both_reads_fail() -> None:
    """两条路都失败才照抛（第一条的错误原样带出来，调用方一眼看出是什么命令）。"""
    session, _ = _unconnected_session(
        {
            "Network.getAllCookies": cdp.CdpError("认不得这个命令"),
            "Storage.getCookies": cdp.CdpError("这条也认不得"),
        }
    )
    with pytest.raises(cdp.CdpError, match="认不得这个命令"):
        session.read_all_cookies()


def test_read_all_cookies_keeps_two_partitions_of_one_name_apart() -> None:
    """去重键必须带分区键：同名同域同路的两块分区饼干不能当成一块丢掉。"""
    session, _ = _unconnected_session(
        {
            "Network.getAllCookies": {"cookies": []},
            "Storage.getCookies": {
                "cookies": [
                    _partitioned_userhash("AAAA1111", "https://a.example"),
                    _partitioned_userhash("BBBB2222", "https://b.example"),
                ]
            },
        }
    )
    assert len(session.read_all_cookies()) == 2


def test_read_site_cookies_takes_a_userhash_only_the_storage_read_sees() -> None:
    """v0.13.42 的整条链：只在 ``Storage.getCookies`` 里的 userhash 照样进合并罐、被认出来。

    真机 m39918 的形状就是「F12 有、三条老读法全无」——F12 走的就是 Storage 这一族。
    """
    session = _ScriptedSession(
        cookies=[], url=bl.COOKIE_SITE + "/Member/User/Cookie/index.html"
    )
    session.storage_cookies = [_partitioned_userhash()]
    cookies = bl.read_site_cookies(session)  # type: ignore[arg-type]
    assert [item["name"] for item in cookies] == ["userhash"]
    assert bl.read_userhash_cookie(session) == "D-9691%04%02abc"  # type: ignore[arg-type]


def test_jar_forensics_names_the_storage_read_apart_from_the_network_one() -> None:
    """对账行的 ``存储读=`` 段（v0.13.42）：下一张截图就能分清漏在哪条路。

    ``Storage`` 有、``Network`` 没有 = 两条读法名单不一致（按地址读会漏掉分区饼干
    就是这种形状）；两边都没有 = 读错了罐，该去查「程序接的是哪扇窗」。照样只报名字
    与属性，值一个字符都不写。
    """
    session = _ScriptedSession(cookies=[], url=bl.LOGIN_URL)
    session.all_cookies = [
        {"name": "PHPSESSID", "value": "S1", "domain": ".nmbxd1.com", "path": "/"}
    ]
    session.storage_cookies = [
        {"name": "PHPSESSID", "value": "S1", "domain": ".nmbxd1.com", "path": "/"},
        _partitioned_userhash("STORAGE-SECRET"),
    ]
    line = bl.jar_forensics(session)  # type: ignore[arg-type]
    assert "整罐读=PHPSESSID、userhash" in line, line
    assert "存储读=PHPSESSID、userhash" in line, line
    assert "全罐=2" in line, line
    assert "原始userhash=1块（域=.nmbxd1.com路=/分区）" in line, line
    assert "合并=" in line and "userhash" in line.split("合并=")[1], line
    assert "STORAGE-SECRET" not in line


def test_read_userhash_cookie_finds_a_userhash_only_the_whole_jar_sees() -> None:
    """只问「罐里有没有」的那条路（v0.13.17）也要吃到合并后的罐子。"""
    session = _ScriptedSession(cookies=[], url=bl.LOGIN_URL)
    session.all_cookies = [
        {"name": "userhash", "value": "ABCDEF12", "domain": ".nmbxd1.com", "path": "/"}
    ]
    assert bl.read_userhash_cookie(session) == "ABCDEF12"  # type: ignore[arg-type]


def test_parse_document_cookie_keeps_equals_inside_values() -> None:
    """v0.13.34：拆 ``名字=值`` 按**第一个**等号切，值里再出现等号不许被咬掉。"""
    entries = bl.parse_document_cookie(
        'userhash=D-abc==; PHPSESSID=s1 ; =novalue; noequals',
        "www.nmbxd1.com",
    )
    assert entries == [
        {"name": "userhash", "value": "D-abc==", "domain": "www.nmbxd1.com", "path": "/"},
        {"name": "PHPSESSID", "value": "s1", "domain": "www.nmbxd1.com", "path": "/"},
    ]


def test_read_site_cookies_adds_the_page_js_view_when_cdp_reads_miss_it() -> None:
    """第四读（v0.13.34）：三条 CDP 读法都漏了、可页面 JS 看得见 —— 照样进合并罐。

    真机 m36307：用户在被盯窗口（棕色横条那扇）里 F12 看得见 userhash，
    程序按地址/整罐两读却报「浏览器里还是没有 userhash」。本机探针
    （_scratch/probe_jar_read_v1334.py）证明 CDP 管道对任何 flag 组合都读得到，
    剩下的偏差只能出在存储上下文 —— document.cookie 是**页面自己的视角**，
    也就是 F12 的视角，把它接进合并罐，那种偏差当场被兜住。
    """
    session = _ScriptedSession(cookies=[], url=bl.COOKIE_SITE + "/Member/User/Cookie/index.html")
    session.document_cookie = "userhash=D-9691%04%02abc; PHPSESSID=sess1"
    cookies = bl.read_site_cookies(session)  # type: ignore[arg-type]
    assert [item["name"] for item in cookies] == ["userhash", "PHPSESSID"]
    assert cookies[0]["domain"] == "www.nmbxd1.com"
    assert bl.userhash_from_cookies(cookies) == "D-9691%04%02abc"
    assert bl.read_userhash_cookie(session) == "D-9691%04%02abc"  # type: ignore[arg-type]


def test_page_js_view_is_skipped_when_the_tab_is_off_site() -> None:
    """被盯页不在站内：一行 JS 都不许替程序去读人家的 document.cookie（v0.13.34）。"""
    session = _ScriptedSession(cookies=[], url="https://example.com/elsewhere")
    session.document_cookie = "userhash=D-NOPE12345"
    assert bl.read_site_cookies(session) == []  # type: ignore[arg-type]
    assert "document.cookie" not in session.evaluations


def test_page_js_view_never_breaks_the_main_reads() -> None:
    """第四读自己出问题（页面在换文档、evaluate 报错）：前三读的结果照旧交出去。"""
    session = _ScriptedSession(
        cookies=[{"name": "PHPSESSID", "value": "abc123", "domain": ".nmbxd1.com"}],
        url=bl.LOGIN_URL,
    )
    session.document_cookie_error = bl.CdpError("页面正在换文档")
    cookies = bl.read_site_cookies(session)  # type: ignore[arg-type]
    assert [item["name"] for item in cookies] == ["PHPSESSID"]


def test_page_js_entries_dedup_against_the_cdp_ones() -> None:
    """同一块饼干 CDP 读和页面读都看见：合并罐里只留一条，别把名单刷重（v0.13.34）。"""
    session = _ScriptedSession(
        cookies=[
            {"name": "PHPSESSID", "value": "abc", "domain": "www.nmbxd1.com", "path": "/"}
        ],
        url=bl.LOGIN_URL,
    )
    session.document_cookie = "PHPSESSID=abc"
    cookies = bl.read_site_cookies(session)  # type: ignore[arg-type]
    assert [item["name"] for item in cookies] == ["PHPSESSID"]


def test_jar_forensics_lines_up_the_reads_and_never_leaks_values() -> None:
    """「读罐对账」一行（v0.13.34）：四路各自看见哪些**名字**，值一个字符都不写。

    这行要进运行日志、日志会被用户直接截图外发 —— userhash 就是通行证本身，
    所以用例把值钉死在门外。
    """
    session = _ScriptedSession(
        cookies=[{"name": "PHPSESSID", "value": "SESS-SECRET-1", "domain": ".nmbxd1.com"}],
        url=bl.COOKIE_SITE + "/Member/User/Cookie/index.html",
    )
    session.all_cookies = [
        {"name": "PHPSESSID", "value": "SESS-SECRET-1", "domain": ".nmbxd1.com"},
        {"name": "memberUserspapapa", "value": "MEMBER-SECRET-2", "domain": ".nmbxd1.com"},
        {"name": "unrelated", "value": "X", "domain": ".example.com"},
    ]
    session.document_cookie = "userhash=HASH-SECRET-3; PHPSESSID=SESS-SECRET-1"
    line = bl.jar_forensics(session)  # type: ignore[arg-type]
    assert "挂=https://www.nmbxd1.com/Member/User/Cookie/index.html" in line
    assert "页=1（https://www.nmbxd1.com/Member/User/Cookie/index.html）" in line, \
        "默认替身就一页，页= 段要把「此刻有几页、挂在哪」写出来（v0.13.36）"
    assert "｜罐=" not in line, "没传罐名不许凭空造一段"
    assert "按地址读=PHPSESSID" in line
    assert "整罐读=PHPSESSID、memberUserspapapa" in line
    assert "unrelated" not in line, "别家域名的饼干不许进对账"
    assert "全罐=3" in line, "不滤域的原始条数（分清「真没有」和「被过滤吃掉」）"
    assert "原始userhash=无" in line
    assert "页面JS=userhash、PHPSESSID" in line
    assert "合并=" in line and "userhash" in line.split("合并=")[1]
    for secret in ("HASH-SECRET-3", "SESS-SECRET-1", "MEMBER-SECRET-2"):
        assert secret not in line, f"对账行漏了饼干值：{secret}"


def test_jar_forensics_names_the_jar_and_a_userhash_the_domain_filter_ate() -> None:
    """v0.13.35 的三新段：罐名、全罐数、以及**没滤域**的原始 userhash 长相。

    真机 m36897 定不了案的缺口：四路名单一致，可「读的是哪份罐」「域过滤有没有
    把 userhash 吃掉」「饼干是不是 CHIPS 分区（探针没测过的读法差异）」都没记录。
    这一段把三样都写进同一行 —— 照样只报属性，值一个字符都不写。
    """
    session = _ScriptedSession(
        cookies=[{"name": "PHPSESSID", "value": "S1", "domain": ".nmbxd1.com"}],
        url=bl.COOKIE_SITE + "/Member/User/Index/login.html",
    )
    session.all_cookies = [
        {"name": "PHPSESSID", "value": "S1", "domain": ".nmbxd1.com"},
        {
            "name": "userhash", "value": "RAW-SECRET", "domain": ".other.example",
            "path": "/forum", "httpOnly": True, "partitionKey": "https://nmbxd1.com",
        },
    ]
    line = bl.jar_forensics(
        session, jar_tag="xdao-export-browser-profile-4321-1727000000123-0"
    )  # type: ignore[arg-type]
    assert "罐=xdao-export-browser-profile-4321" in line
    assert "全罐=2" in line
    assert "整罐读=PHPSESSID" in line, "别家域照旧不进名单"
    assert "原始userhash=1块（域=.other.example路=/forumHttpOnly分区）" in line
    assert "userhash" not in line.split("合并=")[1], "合并读按域过滤，这块不吃"
    assert "RAW-SECRET" not in line


def test_jar_forensics_reports_broken_reads_instead_of_raising() -> None:
    """哪一路读挂了就在对账行里点名（v0.13.34）：这行本身绝不能把登录带崩。"""
    session = _ScriptedSession(cookies=[], url=bl.LOGIN_URL)
    session.read_all_error = bl.CdpError("浏览器不认 getAllCookies")
    line = bl.jar_forensics(session)  # type: ignore[arg-type]
    assert "整罐读=出错（CdpError）" in line
    assert "按地址读=空" in line


def test_jar_forensics_shows_every_attachable_tab_right_now() -> None:
    """页= 段（v0.13.36，真机 m37565）：此刻浏览器里有几页、各挂在哪个地址。

    僵局正是「可见窗口显示已登录的饼干页、对账里 挂= 却一直是 login.html」——
    光一个 挂= 分不出是双标签顺序翻转还是别的。把整页清单并排写进同一行，
    一张截图就能定案；地址不是秘密，照旧不写任何饼干值。
    """
    session = _ScriptedSession(
        cookies=[{"name": "PHPSESSID", "value": "SESS-VAL", "domain": ".nmbxd1.com"}],
        url=bl.COOKIE_SITE + "/Member/User/Cookie/index.html",
    )
    session.page_targets = [
        {"url": bl.LOGIN_URL, "type": "page"},
        {"url": bl.COOKIE_SITE + "/Member/User/Cookie/index.html", "type": "page"},
    ]
    line = bl.jar_forensics(session)  # type: ignore[arg-type]
    assert (
        "页=2（https://www.nmbxd1.com/Member/User/Index/login.html｜"
        "https://www.nmbxd1.com/Member/User/Cookie/index.html）"
    ) in line, line
    assert "SESS-VAL" not in line, "对账行永远不许带饼干值"


def test_jar_forensics_says_so_when_the_tab_reader_is_missing() -> None:
    """会话没有这个读法（旧替身/旧连接）：页= 明说读不到，别把对账带崩。"""
    import types

    session = types.SimpleNamespace(current_url=lambda: bl.LOGIN_URL)
    line = bl.jar_forensics(session)  # type: ignore[arg-type]
    assert "页=读不到（AttributeError）" in line, line


def test_jar_forensics_says_which_window_the_program_is_talking_to() -> None:
    """接= 段（v0.13.37）：对账行点名程序接的是哪一扇——端口一亮，两罐立分。

    真机 m38110：肉眼那扇窗和对账那扇窗是两个进程，光看罐名（都叫 browser-profile）
    分不出在读谁。连接标签由界面层从 browser.port 拿来传入——CDPSession 自己不知道。
    """
    session = _ScriptedSession(cookies=[], url=bl.LOGIN_URL)
    line = bl.jar_forensics(session, jar_tag="browser-profile", link_tag="端口53124")  # type: ignore[arg-type]
    assert "｜接=端口53124｜罐=browser-profile｜" in line, line
    plain = bl.jar_forensics(session)  # type: ignore[arg-type]
    assert "接=" not in plain, "没传连接标签就不许凭空造一段"


def test_jar_forensics_names_every_other_window_when_asked() -> None:
    """逐窗= 段（v0.13.45）：把**每一扇**活窗口里看见了什么名字并排写进对账行。

    真机 m40502：同一扇挂着程序横幅的窗，F12 有 三条饼干含 userhash，对账行却一路
    只报 PHPSESSID。这一行把别的窗口的名单也摆出来，「同一只罐读漏了」和
    「本来就两只罐」从此一张截图分得清。

    v0.13.46（m40810 的第二张截图）：这一段**总是在**（没有别的活窗就写「没有别的活窗口」）
    —— 真机上「逐窗=」一句都没有，用户和程序都读不出「是没别的窗，还是这一手没跑」。
    """
    session = _ScriptedSession(cookies=[], url=bl.LOGIN_URL)
    line = bl.jar_forensics(
        session,  # type: ignore[arg-type]
        jar_tag="browser-profile",
        windows_tag="端口7002 browser-profile-8-9：PHPSESSID、userhash（userhash 在这一扇）",
    )
    assert "｜逐窗=端口7002 browser-profile-8-9：PHPSESSID、userhash（userhash 在这一扇）" in line
    assert bl.jar_forensics(session).endswith("｜逐窗=没有别的活窗口")  # type: ignore[arg-type]
    long_tag = "端口1 罐：" + "名" * 400
    assert len(bl.jar_forensics(session, windows_tag=long_tag)) < 1000  # type: ignore[arg-type]


def test_jar_forensics_names_the_mute_and_shared_jar_hints() -> None:
    """哑窗=／同罐进程= 两段（v0.13.46）：把「另一扇窗」剩下的两种形状摆出来。

    真机 m40810：程序读的那只罐里只有一条 PHPSESSID、也没有别的活窗 —— 而用户在同一个
    目录名的另一扇窗里明明登着。两种形状只有这两段能分开：那扇窗的调试端口已经死了
    （``哑窗=``，端口普查看不见它、进程还在），或者还有第二个实例在共用这只罐
    （``同罐进程=N``，多于 1 就是它）。查不到就不写这两段，别写个空壳糊弄读者。
    """
    session = _ScriptedSession(cookies=[], url=bl.LOGIN_URL)
    line = bl.jar_forensics(  # type: ignore[arg-type]
        session,
        stale_tag="browser-profile-12268-1790850954",
        procs_tag="2",
    )
    assert "｜哑窗=browser-profile-12268-1790850954｜" in line
    assert line.endswith("｜同罐进程=2")
    plain = bl.jar_forensics(session)  # type: ignore[arg-type]
    assert "哑窗=" not in plain and "同罐进程=" not in plain
    assert len(bl.jar_forensics(session, stale_tag="名" * 400)) < 1000  # type: ignore[arg-type]


def test_named_userhash_entries_separates_present_from_plausible() -> None:
    """「有 userhash 但值长得不对」和「根本没有」要能分开（v0.13.29）。

    以前两者在界面上都写成「还没有 userhash」，名单里明明列着 userhash 却这么
    报，用户截图里自相矛盾 yet 查不出程序看见了什么。
    """
    short = {"name": "userhash", "value": "ab", "domain": ".nmbxd1.com"}
    cookies = [short, {"name": "userhash", "value": "别家的", "domain": ".example.com"}]
    entries = bl.named_userhash_entries(cookies)
    assert entries == [short], "只认名字对、域沾 nmbxd1 的那块"
    assert bl.userhash_from_cookies(cookies) is None, "值太短，粗筛不认"


def test_summarize_cookies_lists_names_only() -> None:
    """诊断行只报名字（值和 cookie 名之外的东西一律不写，日志要能直接贴给人看）。"""
    assert bl.summarize_cookies([]) == ""
    made_up = [
        {"name": "PHPSESSID", "value": "secret"},
        {"name": "memberUserspapapa", "value": "secret2"},
        {"name": "PHPSESSID", "value": "dup"},          # 重名只报一次
        {"name": "  ", "value": "x"},                    # 空名字跳过
        {"value": "no-name"},                            # 没名字跳过
        "不是 dict",                                     # 脏数据不能把诊断打崩
    ]
    summary = bl.summarize_cookies(made_up)  # type: ignore[arg-type]
    assert summary == "PHPSESSID、memberUserspapapa"
    assert "secret" not in summary and "dup" not in summary


class _HttpClient:
    """假客户端：只演 ``apply_cookie_over_http`` 用到的那两个方法。"""

    def __init__(self, value: str | None = None, error: Exception | None = None) -> None:
        self.value = value
        self.error = error
        self.imported: list[list] = []
        self.applied = 0

    def import_cookies(self, cookies) -> int:
        self.imported.append(list(cookies))
        return len(list(cookies))

    def apply_cookie(self) -> str | None:
        self.applied += 1
        if self.error is not None:
            raise self.error
        return self.value


def test_apply_leaf_cookie_over_http_pours_the_jar_in_and_asks_for_a_cookie() -> None:
    """整罐倒进客户端 → 调 ``apply_cookie()`` → 把值原样带回去。"""
    jar = [
        {"name": "PHPSESSID", "value": "abc123", "domain": ".nmbxd1.com"},
        {"name": "userhash", "value": "OLD12345", "domain": ".nmbxd1.com"},
    ]
    client = _HttpClient("NEW12345")
    result = bl.apply_leaf_cookie_over_http(jar, client=client)
    assert result.value == "NEW12345"
    assert result.detail == ""
    assert result.navigated is False
    assert client.imported == [jar]
    assert client.applied == 1


def test_apply_leaf_cookie_over_http_quotes_the_server_instead_of_raising() -> None:
    """服务端那句人话要原样带出来 —— 界面会把它写进「程序试过的几步」，是唯一的书面线索。"""
    from xdao.client import XdaoError

    client = _HttpClient(error=XdaoError("导出页里没有 userhash（这一块还没领过）"))
    result = bl.apply_leaf_cookie_over_http([{"name": "a", "value": "b"}], client=client)
    assert result.value is None
    assert result.detail == "导出页里没有 userhash（这一块还没领过）"


def test_apply_leaf_cookie_over_http_reports_unexpected_errors_as_a_line() -> None:
    """网络层的意外（不是 ``XdaoError``）也要变成一句人话，不能往上抛。"""
    client = _HttpClient(error=RuntimeError("连接被重置"))
    result = bl.apply_leaf_cookie_over_http([{"name": "a", "value": "b"}], client=client)
    assert result.value is None
    assert "走 HTTP 领饼干时出错" in result.detail
    assert "连接被重置" in result.detail


# ------------------------------------------------- 页面内 fetch 领饼干（v0.13.30）


def test_read_user_agent_lets_the_browser_describe_itself() -> None:
    """UA 就是从浏览器嘴里问的一句 ``navigator.userAgent``。"""
    session = _ScriptedSession(url=COOKIE_LIST_URL)
    session.user_agent = "Mozilla/5.0 (Windows NT 10.0) AppleWebKit/537.36 Edg/124.0.0.0"
    assert bl.read_user_agent(session) == session.user_agent  # type: ignore[arg-type]


def test_read_user_agent_silences_a_session_that_cannot_answer() -> None:
    """UA 是辅助：问不到就空串，它不该拦住领饼干本身。"""

    class NoEvaluate:
        def current_url(self) -> str:
            return bl.LOGIN_URL

    assert bl.read_user_agent(NoEvaluate()) == ""  # type: ignore[arg-type]


def test_apply_leaf_cookie_over_http_builds_its_client_with_the_browsers_ua(
    monkeypatch,
) -> None:
    """自建客户端那一支要把浏览器 UA 递给 :class:`XdaoClient`（v0.13.30）。

    真机 m34394/m34396：整罐重放被弹回登录页、同一时刻浏览器里回帖成功 —— 差的不是
    会话，是**嘴**。留空时仍传 ``None``：客户端按缺省 UA 走（老行为，替身与单测不变）。
    """
    built: dict = {}

    class _SpyClient:
        def __init__(self, timeout: float = 20.0, user_agent: str | None = None) -> None:
            built["timeout"] = timeout
            built["user_agent"] = user_agent

        def import_cookies(self, cookies) -> int:
            return len(list(cookies))

        def apply_cookie(self) -> str | None:
            return "ABC12345"

    monkeypatch.setattr("xdao.client.XdaoClient", _SpyClient)
    jar = [{"name": "PHPSESSID", "value": "abc", "domain": ".nmbxd1.com"}]
    result = bl.apply_leaf_cookie_over_http(jar, user_agent="Edg/124.0.0.0")
    assert result.value == "ABC12345"
    assert built["user_agent"] == "Edg/124.0.0.0"
    assert built["timeout"] == 20.0
    bl.apply_leaf_cookie_over_http(jar)
    assert built["user_agent"] is None


def test_apply_leaf_cookie_in_browser_walks_the_whole_chain_by_fetch() -> None:
    """列表 → switchTo → export 全程页面内 fetch：拿到值、一个标签页都不碰。

    协议逐条对照 :meth:`XdaoClient.apply_cookie`（v0.6.1 起线上跑通）：认 id 的同一把
    正则、``.html`` 剥后缀再拼、导出页正文优先、读罐兜底。区别只在**嘴**：这一趟是
    浏览器自己开口，UA/头/饼干罐全是原主的。
    """
    base = bl._cookie_action_base()
    session = _ScriptedSession(url=bl.LOGIN_URL)
    session.fetch_pages = {
        base + "index.html": {
            "url": base + "index.html",
            "status": 200,
            "text": (
                '<html><a href="/Member/User/Cookie/switchTo/id/40618.html">应用</a>'
                '<a href="/Member/User/Cookie/export/id/40618.html">导出</a></html>'
            ),
        },
        base + "switchTo/id/40618.html": {
            "url": base + "switchTo/id/40618.html",
            "status": 200,
            "text": "<html>饼干切换成功!</html>",
        },
        base + "export/id/40618.html": {
            "url": base + "export/id/40618.html",
            "status": 200,
            "text": '{"cookie": "ABC12345"}',
        },
    }
    result = bl.apply_leaf_cookie_in_browser(session)  # type: ignore[arg-type]
    assert result.value == "ABC12345"
    assert result.detail == ""
    assert result.navigated is False
    assert session.navigations == [], "这条路的全部意义就是不碰页面"


def test_apply_leaf_cookie_in_browser_follows_the_jump_hint_page() -> None:
    """ThinkPHP 的「跳转提示」页是 200 加 ``<a id="href">``：fetch 也要认、要跟。"""
    base = bl._cookie_action_base()
    session = _ScriptedSession(url=COOKIE_LIST_URL)
    session.fetch_pages = {
        base + "index.html": {
            "url": base + "index.html",
            "status": 200,
            "text": (
                "<html><title>跳转提示</title>"
                '<a id="href" href="/Member/User/Cookie/index.html?page=2"></a></html>'
            ),
        },
        base + "index.html?page=2": {
            "url": base + "index.html?page=2",
            "status": 200,
            "text": '<html><a href="/Member/User/Cookie/switchTo/id/7.html">应用</a></html>',
        },
        base + "switchTo/id/7.html": {
            "url": base + "switchTo/id/7.html",
            "status": 200,
            "text": "<html>饼干切换成功!</html>",
        },
        base + "export/id/7.html": {
            "url": base + "export/id/7.html",
            "status": 200,
            "text": "userhash=XYZ78901; path=/",
        },
    }
    result = bl.apply_leaf_cookie_in_browser(session)  # type: ignore[arg-type]
    assert result.value == "XYZ78901"


def test_apply_leaf_cookie_in_browser_reads_the_jar_when_the_export_page_stays_silent() -> None:
    """导出页没正文值时读罐：主站正是在 switchTo 那一趟把 userhash 种进罐的。"""
    base = bl._cookie_action_base()
    session = _ScriptedSession(
        url=COOKIE_LIST_URL,
        cookies=[{"name": "userhash", "value": "ABC12345", "domain": ".nmbxd1.com"}],
    )
    session.fetch_pages = {
        base + "index.html": {
            "url": base + "index.html",
            "status": 200,
            "text": '<html><a href="/Member/User/Cookie/switchTo/id/9.html">应用</a></html>',
        },
        base + "switchTo/id/9.html": {
            "url": base + "switchTo/id/9.html",
            "status": 200,
            "text": "<html>饼干切换成功!</html>",
        },
        base + "export/id/9.html": {
            "url": base + "export/id/9.html",
            "status": 200,
            "text": "<html>已应用</html>",
        },
    }
    result = bl.apply_leaf_cookie_in_browser(session)  # type: ignore[arg-type]
    assert result.value == "ABC12345"


def test_apply_leaf_cookie_in_browser_says_when_the_session_bounces_to_login() -> None:
    """fetch 也被弹回登录页 —— 这和 HTTP 重放被弹回是**不同**的诊断（会话真死了）。

    话术要直说「这个窗口里的登录没成」：m34394 里用户看到「请重新登录」时已经在登录页
    上打转，两条路的话摆在一起才分得清是嘴的问题还是会话的问题。
    """
    base = bl._cookie_action_base()
    session = _ScriptedSession(url=COOKIE_LIST_URL)
    session.fetch_pages = {
        base + "index.html": {
            "url": "https://www.nmbxd1.com/user/login/index.html",
            "status": 200,
            "text": "<html>请登录</html>",
        },
    }
    result = bl.apply_leaf_cookie_in_browser(session)  # type: ignore[arg-type]
    assert result.value is None
    assert "弹回了登录页" in result.detail, result.detail
    assert "重新登录" in result.detail, result.detail


def test_apply_leaf_cookie_in_browser_quotes_a_jump_page_without_login() -> None:
    """跳转提示页上有站点原话（「没有权限访问」之类）：原话要进诊断行。"""
    base = bl._cookie_action_base()
    session = _ScriptedSession(url=COOKIE_LIST_URL)
    session.fetch_pages = {
        base + "index.html": {
            "url": base + "index.html",
            "status": 200,
            "text": '<html><title>跳转提示</title><p class="error">没有权限访问</p></html>',
        },
    }
    result = bl.apply_leaf_cookie_in_browser(session)  # type: ignore[arg-type]
    assert result.value is None
    assert "没有权限访问" in result.detail, result.detail


def test_apply_leaf_cookie_in_browser_says_the_account_has_no_cookie() -> None:
    """会话活着、列表就是空的：说「没认出可应用的饼干」，别冤枉登录。"""
    base = bl._cookie_action_base()
    session = _ScriptedSession(url=COOKIE_LIST_URL)
    session.fetch_pages = {
        base + "index.html": {
            "url": base + "index.html",
            "status": 200,
            "text": "<html><table><tr><td>还没有领取过饼干</td></tr></table></html>",
        },
    }
    result = bl.apply_leaf_cookie_in_browser(session)  # type: ignore[arg-type]
    assert result.value is None
    assert "没认出可应用的饼干" in result.detail, result.detail


def test_apply_leaf_cookie_in_browser_refuses_pages_it_cannot_use() -> None:
    """窗口里没有可读页面、或者根本不在 X 岛：一句话，一个请求都不发。"""
    session = _ScriptedSession()  # url="" —— 页面还没加载出来
    result = bl.apply_leaf_cookie_in_browser(session)  # type: ignore[arg-type]
    assert result.value is None
    assert "没有可读的页面" in result.detail, result.detail
    assert session.evaluations == []

    session.url = "https://example.com/some/page"
    result = bl.apply_leaf_cookie_in_browser(session)  # type: ignore[arg-type]
    assert result.value is None
    assert "不在 X 岛页面" in result.detail and "example.com" in result.detail
    assert session.evaluations == [], "不该往别人家的页面里发 fetch"

    # v0.13.33：地址读得到、host 却是空的（about:blank 这类）以前会直通 ——
    # fetch 落在非站内文档里一块饼干也不发，站点必然弹回登录页，界面反过来
    # 谎报「这个窗口里的登录没成」。这一支现在也当场拦下、如实说是标签不对。
    session.url = "about:blank"
    result = bl.apply_leaf_cookie_in_browser(session)  # type: ignore[arg-type]
    assert result.value is None
    assert "不在 X 岛页面" in result.detail and "about:blank" in result.detail
    assert session.evaluations == [], "不该往空白页里发 fetch，更不该反咬「登录没成」"


def test_apply_leaf_cookie_in_browser_reports_a_fetch_it_could_not_even_send() -> None:
    """fetch 压根没送出去（CSP 拦脚本、页面正在换文档）：一句「没问动」，界面无感。"""
    session = _ScriptedSession(url=COOKIE_LIST_URL)
    result = bl.apply_leaf_cookie_in_browser(session)  # type: ignore[arg-type]
    assert result.value is None
    assert "没问动" in result.detail, result.detail
    assert session.navigations == []


def test_cookie_urls_for_asks_several_addresses() -> None:
    """读饼干要问好几个地址：``Network.getCookies`` 只回「会发给这个地址」的饼干。"""
    elsewhere = f"{bl.COOKIE_SITE}/Member/User/Index/index.html"
    session = _ScriptedSession(url=elsewhere)
    urls = bl.cookie_urls_for(session)  # type: ignore[arg-type]
    assert bl.COOKIE_SITE + "/" in urls
    assert COOKIE_LIST_URL in urls
    assert "https://nmbxd1.com/" in urls
    # 当前页也问一遍（只对那一页可见的饼干就靠它）。
    assert urls[-1] == elsewhere


def test_cookie_urls_for_does_not_ask_the_same_address_twice() -> None:
    """用户正好停在饼干页上时，别把这个地址又问一遍。"""
    session = _ScriptedSession(url=COOKIE_LIST_URL)
    urls = bl.cookie_urls_for(session)  # type: ignore[arg-type]
    assert urls.count(COOKIE_LIST_URL) == 1


def test_cookie_urls_for_survives_a_session_that_cannot_answer() -> None:
    """读不到当前地址（会话刚断）不该让整件事失败：那三个固定地址照样问。"""

    class Broken:
        def current_url(self) -> str:
            raise bl.CdpError("连接断了")

    urls = bl.cookie_urls_for(Broken())  # type: ignore[arg-type]
    assert len(urls) == 3


@pytest.mark.parametrize(
    "cookie",
    [
        {"name": "userhash", "value": "ABC12345"},
        {"name": "userhash", "value": "ABC12345", "domain": ".nmbxd1.com"},
        {"name": "userhash", "value": "ABC12345", "domain": "www.nmbxd1.com"},
    ],
)
def test_userhash_from_cookies_accepts_the_sites_own_cookie(cookie: dict) -> None:
    assert bl.userhash_from_cookies([cookie]) == "ABC12345"


@pytest.mark.parametrize(
    "cookie",
    [
        {"name": "userhash", "value": "abc"},  # 形状不对（太短）
        {"name": "userhash", "value": ""},  # 空值
        {"name": "userhash", "value": "ABC12345", "domain": ".example.com"},  # 别人家的域
        {"name": "PHPSESSID", "value": "ABC12345"},  # 名字不对
    ],
)
def test_userhash_from_cookies_rejects_everything_else(cookie: dict) -> None:
    assert bl.userhash_from_cookies([cookie]) is None


def test_userhash_from_cookies_ignores_junk_entries() -> None:
    cookies = ["nonsense", None, 42, {"name": "userhash", "value": "ABC12345"}]
    assert bl.userhash_from_cookies(cookies) == "ABC12345"  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "text",
    [
        '{"cookie": "ABC12345"}',
        '{"status": 1, "data": {"cookie": "ABC12345"}}',
        "userhash=ABC12345; path=/; domain=.nmbxd1.com",
        '<script>var x = {"cookie": "ABC12345"};</script>',
    ],
)
def test_userhash_from_export_text_reads_the_three_shapes(text: str) -> None:
    assert bl.userhash_from_export_text(text) == "ABC12345"


@pytest.mark.parametrize("text", ["", "   ", "<html>跳转提示</html>", '{"cookie": "abc"}'])
def test_userhash_from_export_text_rejects_junk(text: str) -> None:
    """页面正文里抠出来的值也要过形状检查：宁可再等一轮，也不喂给客户端一个怪值。"""
    assert bl.userhash_from_export_text(text) is None


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
        "build_find_apply_script",
        "fetch_leaf_cookie",
        "cookie_urls_for",
        "userhash_from_cookies",
        "userhash_from_export_text",
        "read_userhash_cookie",
        "LeafCookie",
        "apply_leaf_cookie",
        "ensure_login_page",
        "LoginBrowser",
        "CDPSession",
        "BrowserInfo",
        "BrowserLoginError",
    ):
        assert hasattr(bl, name), name
    # 界面层按这个签名调（多一个少一个都要在 review 里说清楚）。
    # `waiting_for_login` 是 v0.13.21 加的：界面层在「已经真去应用过」之后传 False，
    # 让「页面停在登录页」不再被当成「用户还在登录页打字」。
    leaf = inspect.signature(bl.fetch_leaf_cookie)
    assert list(leaf.parameters) == ["session", "urls", "navigate", "waiting_for_login"]
    assert leaf.parameters["navigate"].kind is inspect.Parameter.KEYWORD_ONLY
    assert leaf.parameters["navigate"].default is True
    assert leaf.parameters["waiting_for_login"].kind is inspect.Parameter.KEYWORD_ONLY
    assert leaf.parameters["waiting_for_login"].default is True
    assert [field.name for field in dataclasses.fields(bl.LeafCookie)] == [
        "value",
        "detail",
        "navigated",
    ]
    assert list(inspect.signature(bl.cookie_urls_for).parameters) == ["session"]
    assert list(inspect.signature(bl.userhash_from_cookies).parameters) == ["cookies"]
    assert inspect.signature(bl.read_userhash_cookie).parameters["urls"].default is None
    assert list(inspect.signature(bl.apply_leaf_cookie).parameters) == ["session"]
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
        "shutil",
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
    没登录时读饼干要优雅地返回 None（``navigate=False``：带导航那条要真联网，
    留给真机核验），临时目录要能被删掉。
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
                # 只重读饼干罐这条路不许导航（也就不会联网）：匿名时它必须优雅地回 None。
                assert bl.fetch_leaf_cookie(session, navigate=False).value is None
    finally:
        _remove_tree(profile)
    assert not profile.exists(), "停止浏览器后临时用户目录应当能删掉"
