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
import errno
import hashlib
import itertools
import json
import os
import queue
import re
import secrets
import shutil
import socket
import struct
import subprocess
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Mapping

from .browser_flags import launch_flags

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
# 删临时资料目录时最多等多久（浏览器刚退出时句柄还没松，删不掉就再试）。
# 和 updater._remove_tree 一样：删掉就立刻返回，只有真删不掉才耗满。
TEMP_PROFILE_WAIT = 3.0
#: 删临时目录前要不要先把「命令行里带这个目录」的浏览器进程收掉（见
#: :func:`_kill_processes_using_profile`）。用例会把它改成 False —— 那一手要真起
#: 一个 powershell，不该在每个用例里跑一遍。
_KILL_PROFILE_PROCESSES = _IS_WINDOWS
#: 普查要不要再问操作系统一遍「谁此刻用着我们家的资料目录」（见 :func:`program_profile_dirs`）。
#: 端口普查只认「名字猜得到」的目录（:func:`live_browser_dirs`）：备用目录的名字带着
#: **当时那个进程**的 PID 和时间戳，新进程猜不出 —— 真机 m38110 的「饼干窗硬刷新不弹回、
#: 对账却 页=1」就是这么来的。当时还有另一半原因：``_launch`` 会顺手把目标目录里那份
#: ``DevToolsActivePort`` 删掉（怕读到上次崩溃留下的过期端口），而那一眼可能正删在
#: **一扇还开着的窗**头上；v0.13.38 起不改别人的文件了，那份端口文件一律留着。
#: 用例会把它换成 False（或直接换掉函数本身）：这一手要真起一个 powershell。
_PROCESS_CENSUS = _IS_WINDOWS
#: :func:`fresh_profile_dir` 的进程内序号：同一个毫秒里连叫两次也要拿到不同的名字。
_FRESH_PROFILE_SEQ = 0
# 连调试端口时，等站点页面出现的最长时间。
# 浏览器是先写端口文件、再加载命令行给的地址的：端口一出来就连，可能只看到一个空标签
# （真机上还量到过 Edge 自带的 edge://sync-confirmation-dialog/）。连到那种页面上，
# 页面里的 fetch 属于别的源，读饼干会一律读空，所以这里给它几秒把登录页开出来。
_SITE_WAIT = 8.0
# 多扇窗里挑一扇接手时（见 :meth:`LoginBrowser._order_by_login`），问一扇窗「罐里有没有
# userhash」的预算：够连上、够读一次饼干，又不至于让「哪扇登录过」这个问题拖慢开窗。
# 只连本地调试端口、只读饼干，不动用户正看着的那一页。
WINDOW_PROBE_TIMEOUT = 3.0
# 判断「裸粘贴」的字符集：整段的粘贴不会只由这些字符组成，因此能挡掉 HTML/JSON 残渣。
_BARE_VALUE_RE = re.compile(r"^[A-Za-z0-9._~+/=-]+$")
# 「浏览器刚起来就退出了」这句错误文案里的固定部分：启动失败与读调试接口失败的
# 两处提示都带着它，``_dead_on_startup`` 靠它认出这一类失败（换目录、换浏览器重试）。
_DEAD_ON_STARTUP_MARKER = "刚起来就退出了"


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


# ---------------------------------------------------------------- 浏览器选谁
#
# 真机上量到的期望差：用户会把 Windows 默认浏览器改成 Chrome，可这里一直从 Edge
# 开始试 —— 于是他看到的是「我明明换成 Chrome 了，弹出来的还是 Edge」。所以先把
# 「系统默认的那个」排到最前面，再按 Edge → Chrome → … 的顺序补全。
#: Windows 注册表里记默认浏览器的位置（HKCU）。
_DEFAULT_BROWSER_KEY = (
    r"SOFTWARE\Microsoft\Windows\Shell\Associations\UrlAssociations\https\UserChoice"
)
#: 认默认浏览器**可执行文件**用的表：进程名（小写）→ 界面名。
_EXE_NAMES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("msedge.exe", "msedge", "edge.exe"), "Edge"),
    (("chrome.exe", "chrome"), "Chrome"),
    (("brave.exe", "brave", "brave-browser.exe"), "Brave"),
    (("chromium.exe", "chromium"), "Chromium"),
)
#: 打开注册表用的模块（真机上是 ``winreg``）。
#:
#: 写成模块级变量是为了**让测试能钉住「读默认浏览器」这条逻辑**：CI 的 Linux
#: 机器上没有 ``winreg``，用例若直接跳过，这段代码在那边就没人看着了。
#: 测试把它换成假的注册表实现，本地与 CI 跑的是同一份断言。
_REGISTRY_OPENER: Callable[[], object] | None = None


def _registry() -> object:
    """打开注册表的模块；没有 ``winreg``（非 Windows）就抛 ``ImportError``。"""
    if _REGISTRY_OPENER is not None:
        return _REGISTRY_OPENER()
    import winreg  # noqa: PLC0415 —— 只有 Windows 走这条路

    return winreg


def _name_of_executable(command: str) -> str:
    """从一条命令行/路径里认出浏览器名字；认不出返回空串。

    只按可执行文件名认（``--single-argument %1`` 这类尾巴要丢掉），这样
    「默认浏览器是哪一行命令」这种问题不必去解析整条命令行。
    """
    if not command:
        return ""
    token = command.strip().strip('"').split('"', 1)[0].strip()
    if not token:
        token = command.strip().strip('"')
    exe = token.replace("\\", "/").rsplit("/", 1)[-1].lower()
    for names, label in _EXE_NAMES:
        if exe in names:
            return label
    for names, label in _EXE_NAMES:
        if any(name.rsplit(".", 1)[0] in exe for name in names):
            return label
    return ""


def _windows_default_exe() -> str:
    """读 Windows 的默认浏览器设置，返回它的可执行文件路径；读不到返回空串。

    真机上量到过：用户在「设置 → 默认应用」里把 http/https 都改成了 Chrome，
    而这里原本固定从 Edge 开始试，用户看到的就是「我换了默认浏览器，弹出来的
    还是 Edge」。这一步只读注册表，不改任何东西；读不出来（没有这个键、注册表
    被锁、非 Windows）就当没有，按原来的固定顺序来。
    """
    try:
        winreg = _registry()
        with winreg.OpenKey(  # type: ignore[attr-defined]
            winreg.HKEY_CURRENT_USER,  # type: ignore[attr-defined]
            _DEFAULT_BROWSER_KEY,
        ) as key:
            prog_id = str(winreg.QueryValueEx(key, "ProgId")[0] or "")  # type: ignore[attr-defined]
        if not prog_id:
            return ""
        command = ""
        command_key = rf"SOFTWARE\Classes\{prog_id}\shell\open\command"
        # ProgId 的命令行可能只写在机器级（HKLM）里：用户只是「用某个浏览器打开过一次」，
        # HKCU 下就只多一个 UserChoice，什么都没有。两边都看一眼，谁有就用谁。
        for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):  # type: ignore[attr-defined]
            try:
                with winreg.OpenKey(hive, command_key) as key:  # type: ignore[attr-defined]
                    command = str(winreg.QueryValueEx(key, "")[0] or "")  # type: ignore[attr-defined]
            except OSError:
                continue
            if command:
                break
        if not command:
            return ""
        start = command.find('"')
        if start >= 0:
            end = command.find('"', start + 1)
            if end > start:
                return command[start + 1 : end]
        return command.split(" ", 1)[0].strip()
    except Exception:  # noqa: BLE001 —— 读不到默认浏览器不该拦住登录
        return ""


def _default_browser_label() -> str:
    """默认浏览器叫什么（"Edge" / "Chrome" / …）；认不出返回空串。"""
    return _name_of_executable(_windows_default_exe())


def ordered_browsers(env: Mapping[str, str] | None = None) -> list[BrowserInfo]:
    """可用的浏览器，**系统默认那个排最前**，其余按固定顺序跟在后面。

    认不出默认浏览器（或者它不在候选表里，比如 Firefox 和一堆国产壳浏览器）
    就按 :func:`known_paths` 的原顺序返回 —— 宁可用 Edge，也不能不登录。
    """
    installed = known_paths(env)
    if len(installed) < 2:
        return installed
    label = _default_browser_label()
    if not label:
        return installed
    preferred = [info for info in installed if info.name == label]
    if not preferred:
        return installed
    others = [info for info in installed if info.name != label]
    return [*preferred, *others]


def find_browser(
    explicit: str | None = None, env: Mapping[str, str] | None = None
) -> BrowserInfo | None:
    """挑一个浏览器：**系统默认的优先**，其次 Edge → Chrome → …。

    用户手动指定的路径最优先（他可能装在非默认位置）；指定的路径不存在时
   不报错，而是退回去自动找 —— 用户随手填个错路径不该让整条路走不下去。
    """
    if explicit:
        candidate = Path(explicit)
        if candidate.is_file():
            return BrowserInfo(name=_guess_name(candidate), path=str(candidate))
    found = ordered_browsers(env)
    return found[0] if found else None


def _guess_name(path: Path) -> str:
    """按可执行文件名猜浏览器名，只用于界面文案。"""
    # 只用字节名，不依赖宿主平台的分隔符：Windows 上 ``Path("C:\\x\\msedge.exe").stem``
    # 是 ``msedge``，Linux 上同一串会被当成**一个**文件名（反斜杠不是分隔符），
    # ``stem`` 变成 ``C:\\x\\msedge``，界面文案就会带上整段路径。
    stem = str(path).replace("\\", "/").rsplit("/", 1)[-1].lower()
    if "msedge" in stem or stem == "edge":
        return "Edge"
    if "brave" in stem:
        return "Brave"
    if "chromium" in str(path).lower():
        return "Chromium"
    if "chrome" in stem:
        return "Chrome"
    return (stem.rsplit(".", 1)[0] if "." in stem else stem) or "Chromium"


def browser_candidates(
    info: BrowserInfo | None = None, env: Mapping[str, str] | None = None
) -> list[BrowserInfo]:
    """按顺序列出「可以拿来试」的浏览器：``info`` 排最前，其余按默认优先级跟在后面。

    :meth:`LoginBrowser.start` 用它做「一个浏览器起不来就换下一个」的兜底。
    单独列一个函数是为了让测试能钉住这条顺序 —— 真机上它决定了用户看到的是哪个窗口。

    ``info`` 为 None（或者本机只装了它一个）时就是 :func:`known_paths` 的结果。
    """
    installed = ordered_browsers(env)
    if info is None:
        return installed
    if any(candidate.path == info.path for candidate in installed):
        head = [info]
        rest = [candidate for candidate in installed if candidate.path != info.path]
    else:
        head = [info]
        rest = installed
    return [*head, *rest]


def user_data_dir(config_dir: Path) -> Path:
    """浏览器用户目录在配置目录下的位置。

    为什么不复用用户自己的浏览器 profile：一是他可能正开着浏览器，
    同一个 profile 会被锁住（新进程只会把标签页丢给已开的窗口，不监听调试端口）；
    二是调试端口和临时登录态都不该落进他的日常浏览记录里。
    """
    return Path(config_dir) / USER_DATA_DIR_NAME


def fallback_profile_dirs(profile: Path) -> list[Path]:
    """``profile`` 用不了时的备用目录（按顺序试）。

    真机上遇到过这一条：配置目录里的 ``browser-profile`` 因为权限或占用，
    浏览器一碰就是「[WinError 5] 拒绝访问」，用户看到的就是一句
    「打开浏览器失败」，怎么点都打不开。这时候换到临时目录下的一个新 profile
    通常就能起来 —— 登录窗口本来就是临时的，放哪儿都能用。

    优先复用同一个父目录下带后缀的新目录（把浏览器状态留在用户自己的配置目录里），
    只有父目录本身也写不进去，才退到系统临时目录。
    """
    profile = Path(profile)
    parent = profile.parent
    options = [parent / f"{USER_DATA_DIR_NAME}-{os.getpid()}-{int(time.time())}", profile / "_new"]
    try:
        temp_root = Path(tempfile.gettempdir())
        options.append(temp_root / f"xdao-export-{USER_DATA_DIR_NAME}-{os.getpid()}")
    except OSError:  # pragma: no cover —— 拿不到临时目录就算了
        pass
    return [candidate for candidate in options if candidate != profile]


def fresh_profile_dir() -> Path:
    """一个新的、系统临时目录下的浏览器资料目录。

    为什么登录这条路要**先**用它（v0.13.9 起，见 :meth:`LoginBrowser.start`）：
    ``--check-browser`` 走的就是这个位置，真机上「登录点不动、可自检里浏览器又
    起得来」的时候，差别往往只在资料目录 —— 配置目录里那个是从上一次登录留到
    现在的，里面存着会话状态，坏掉之后浏览器一起来就退（典型现场是 Edge 退出码
    21），而每次换一个空的就没事。登录窗口本来就是临时的，用完就删。

    名字里除了毫秒还要带一个进程内的序号：同一个毫秒里连叫两次（用例就这么干）
    光靠毫秒会撞成同一个目录。
    """
    global _FRESH_PROFILE_SEQ
    _FRESH_PROFILE_SEQ += 1
    return Path(tempfile.gettempdir()) / (
        f"xdao-export-{USER_DATA_DIR_NAME}-{os.getpid()}-"
        f"{int(time.time() * 1000)}-{_FRESH_PROFILE_SEQ}"
    )


#: 我们建在系统临时目录下的目录名前缀：登录用的临时资料目录，以及 ``--check-browser``
#: 自检时那一份（``browser_check`` 用 ``mkdtemp(prefix="xdao-browser-check-")`` 建的）。
#: 两样都是临时的、都该在收尾时删掉，所以清理时要一起认。
TEMP_DIR_PREFIXES = (f"xdao-export-{USER_DATA_DIR_NAME}", "xdao-browser-check-")


def _is_temp_profile_dir(path: Path) -> bool:
    """这个资料目录是不是「我们建在系统临时目录下」的那种（是就该由我们负责删）。

    认位置 + 名字前缀：系统临时目录下的 ``xdao-export-browser-profile…`` 与
    ``xdao-browser-check-…``。备用候选里也有一个是这么命名的
    （见 :func:`fallback_profile_dirs`），它同样是我们建出来的，
    收尾得一起清。用户配置目录里的 ``browser-profile`` 不在此列 —— 那是他自己的资料，
    留着下次还能用，不能替他删。
    """
    try:
        root = Path(tempfile.gettempdir())
    except OSError:  # pragma: no cover —— 拿不到临时目录就算不是
        return False
    return path.parent == root and path.name.startswith(TEMP_DIR_PREFIXES)


#: 配置目录下「备用资料目录」的名字：``browser-profile-<PID>-<时间戳>``
#: （见 :func:`fallback_profile_dirs`）。两段都必须是纯数字 —— 认名字要认到这么死，
#: 才不会把持久的 ``browser-profile``、或者用户自己起的名字误收掉。
_FALLBACK_DIR_RE = re.compile(rf"^{re.escape(USER_DATA_DIR_NAME)}-(\d+)-(\d+)$")


def _is_fallback_profile_dir(path: Path, profile: Path) -> bool:
    """这个目录是不是我们建在**配置目录**里的备用资料目录（v0.13.39）。

    这一族曾经是没人收的垃圾：:func:`fallback_profile_dirs` 在配置目录这一级建的名字
    带着当时进程的 PID 和时间戳，下一个进程猜不出名字；而
    :meth:`LoginBrowser.cleanup_temp_profile` 当时只记 ``%TEMP%`` 那一族
    （:func:`_is_temp_profile_dir`）⇒ 程序被强杀（或者某次「起来就退」之后没走到收尾）
    时，它们就留在用户自己的配置目录里。真机 2026-10-01 量到过两个：
    ``browser-profile-12268-1790850954``（96 个文件 / 5.6MB）与
    ``browser-profile-12268-1790850956``（66 个文件 / 6.3MB）。v0.13.39 起两边都认。
    """
    path = Path(path)
    profile = Path(profile)
    return path.parent == profile.parent and bool(_FALLBACK_DIR_RE.match(path.name))


def _is_our_profile_dir(path: Path, profile: Path) -> bool:
    """这个资料目录是不是我们**自己建出来、该由我们收尾删掉**的那种。

    用户配置目录里那份持久的 ``browser-profile`` 靠名字/位置两道规则挡在外面
    （它既不在系统临时目录下，名字也不符合 ``browser-profile-<数字>-<数字>``），
    那是他下次还要用的登录状态，不能替他删。注意 ``--check-browser`` 那条路会把
    自建在 ``%TEMP%`` 里的那份**当成** ``profile`` 传进来，那一份照样算我们的。
    """
    path = Path(path)
    profile = Path(profile)
    return (
        _is_temp_profile_dir(path)
        or _is_fallback_profile_dir(path, profile)
        or path == profile / "_new"
    )


#: 启动时扫旧临时资料目录的年龄门槛（秒）：比这新的不碰（多半是别的实例正开着登录窗口）。
SWEEP_MIN_AGE = 3600.0

#: 扫的时候问一次调试端口的等待上限（秒）。只问一次 —— 这里不是在等浏览器起来。
SWEEP_PROBE_TIMEOUT = 0.5


def _profile_in_use(profile: Path) -> bool:
    """这一份临时资料目录是不是还有活着的浏览器在用？

    看 ``DevToolsActivePort`` 里那个端口答不答话：浏览器还开着这个目录时它一定答话
    （程序被强杀时浏览器自己会活下来），那一份就不能删。文件不在、端口是死的、
    接口报错，都算没人用。
    """
    try:
        text = (profile / "DevToolsActivePort").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    try:
        port = parse_devtools_port(text)
    except CdpError:
        return False
    from .cdp import _http_json_once

    try:
        _http_json_once(f"http://127.0.0.1:{port}/json/version", timeout=SWEEP_PROBE_TIMEOUT)
    except CdpError:
        return False
    return True


def _sweep_one_profile_dir(entry: Path, min_age: float, keep_set: set[Path]) -> bool:
    """试删一个「我们自己的」资料目录，真删掉了才回 True。

    两个扫法（临时目录族、配置目录下的备用族）共用这一段判据，见
    :func:`sweep_stale_temp_profiles` 里那三条。
    """
    if entry in keep_set:
        return False
    try:
        if entry.is_symlink() or not entry.is_dir():
            return False
        age = time.time() - entry.stat().st_mtime
    except OSError:  # pragma: no cover —— 刚被别人删掉了、或读不到属性
        return False
    if age < min_age:
        return False
    if _profile_in_use(entry):
        return False
    shutil.rmtree(entry, ignore_errors=True)
    return not entry.exists()


def sweep_stale_temp_profiles(
    *, min_age: float = SWEEP_MIN_AGE, keep: Iterable[Path] = ()
) -> int:
    """把以前留下的临时资料目录扫掉，返回删掉几个。

    :meth:`LoginBrowser.cleanup_temp_profile` 只在**程序自己收尾**时删。程序被强杀
    （任务管理器结束进程、停电、被安全软件拦下）就没人收尾，那些目录会一直躺在
    ``%TEMP%`` 里 —— 真机上量到过 147 个，多数是空壳，也有一次登录十几 MB 的。

    三条全中才删：在系统临时目录下且名字前缀是我们的（:data:`TEMP_DIR_PREFIXES`）；
    目录的修改时间比 ``min_age`` 秒还老（刚建出来的多半是别的实例正在用）；
    **调试端口没人答话**（见 :func:`_profile_in_use`）。``keep`` 里的路径一律不碰
    （这次会话自己建的那几份）。删不掉的留着，下次启动再说 —— 这里不跟文件较劲。
    """
    try:
        root = Path(tempfile.gettempdir())
    except OSError:  # pragma: no cover —— 拿不到临时目录就没什么可扫的
        return 0
    keep_set = {Path(item) for item in keep}
    try:
        entries = list(root.iterdir())
    except OSError:  # pragma: no cover —— 临时目录列不出来就算了
        return 0
    deleted = 0
    for entry in entries:
        if not _is_temp_profile_dir(entry):
            continue
        if _sweep_one_profile_dir(entry, min_age, keep_set):
            deleted += 1
    return deleted


def sweep_stale_fallback_profiles(
    profile: Path, *, min_age: float = SWEEP_MIN_AGE, keep: Iterable[Path] = ()
) -> int:
    """把配置目录下遗留的备用资料目录扫掉，返回删掉几个（v0.13.39）。

    扫的是 :func:`fallback_profile_dirs` 在配置目录这一级建的那一族
    （``browser-profile-<PID>-<时间戳>``，认名字认到纯数字为止），外加
    ``profile/_new``（同一族的「里面那份」）。判据与
    :func:`sweep_stale_temp_profiles` 完全一样：**名字严格对得上**、目录比
    ``min_age`` 秒还老、**调试端口没人答话**，三条全中才删。``keep`` 里的一律不碰。

    绝不碰 ``profile`` 本体：那是用户下次还要用的资料目录（里面存着他的登录状态），
    名字也不符合那条严格规则 —— 两道保险。
    """
    profile = Path(profile)
    keep_set = {Path(item) for item in keep} | {profile}
    try:
        entries = list(profile.parent.iterdir())
    except OSError:  # pragma: no cover —— 配置目录列不出来就算了
        return 0
    # profile/_new 不在 parent 那一层，单独算一个候选。
    candidates = [entry for entry in entries if _is_fallback_profile_dir(entry, profile)]
    candidates.append(profile / "_new")
    deleted = 0
    for entry in candidates:
        if _sweep_one_profile_dir(entry, min_age, keep_set):
            deleted += 1
    return deleted


def live_browser_dirs(profile: Path) -> list[Path]:
    """把「浏览器还开着的」资料目录列出来，新的排前面（v0.13.33）。

    要问的地方：现场目录、备用目录，还有 %TEMP% 里带我们前缀的那些临时目录 ——
    fresh 目录的名字里带着**上一个进程**的 PID，新进程猜不出名字，只能列出来挨个问。
    每个目录按 ``DevToolsActivePort`` 的端口答不答话判死活（见 :func:`_profile_in_use`）：
    答话的就是还开着的窗口。按端口文件的修改时间倒序 —— 最晚开的那个才是用户
    眼前看着的那扇窗。
    """
    candidates: list[Path] = [profile, *fallback_profile_dirs(profile)]
    try:
        root = Path(tempfile.gettempdir())
        candidates.extend(entry for entry in root.iterdir() if _is_temp_profile_dir(entry))
    except OSError:  # pragma: no cover —— 临时目录列不出来就只问已知的几个
        pass
    live: list[tuple[float, Path]] = []
    seen: set[Path] = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        try:
            mtime = (candidate / "DevToolsActivePort").stat().st_mtime
        except OSError:
            continue
        if not _profile_in_use(candidate):
            continue
        live.append((mtime, candidate))
    live.sort(key=lambda item: item[0], reverse=True)
    return [candidate for _, candidate in live]


def _parse_program_profile_lines(text: str, profile: Path) -> list[Path]:
    """从进程侧普查的输出里认出自家资料目录（v0.13.37）。

    每行形如 ``PID<TAB>整条命令行``；抠出所有 ``--user-data-dir=``（路径带空格时
    会被整体加引号，先剥掉）再筛「程序自己的」两族，口径和
    :func:`_is_temp_profile_dir` / :func:`fallback_profile_dirs` 一致：

    - 配置目录下名字以 ``browser-profile`` 开头的（现场目录和它的全部备用目录），
      外加现场目录里那个 ``_new`` 候选；
    - %TEMP% 下带我们前缀的（登录临时目录与 ``--check-browser`` 自检目录）。

    用户自己浏览器的 ``User Data`` 两族都不沾 —— 这是铁律：**普查永远不许碰他
    自己的窗**。比对不分大小写（Windows 路径本来就不分）。返回去重后的目录表。
    """
    own_parent = str(profile.parent).lower()
    prefix = USER_DATA_DIR_NAME.lower()
    temp_prefixes = tuple(item.lower() for item in TEMP_DIR_PREFIXES)
    try:
        temp_root = str(Path(tempfile.gettempdir())).lower()
    except OSError:  # pragma: no cover —— 拿不到临时目录就只认配置目录那族
        temp_root = ""
    found: list[Path] = []
    seen: set[str] = set()
    for line in text.splitlines():
        _, _, command_line = line.partition("\t")
        if not command_line:
            continue
        for raw in re.findall(r"--user-data-dir=(\"[^\"]*\"|\S+)", command_line, flags=re.IGNORECASE):
            raw = raw.strip('"').rstrip("\\/")
            if not raw:
                continue
            path = Path(raw)
            name = path.name.lower()
            parent = str(path.parent).lower()
            ours = (parent == own_parent and name.startswith(prefix)) or (
                parent == str(profile).lower() and name == "_new"
            ) or (bool(temp_root) and parent == temp_root and name.startswith(temp_prefixes))
            key = raw.lower()
            if ours and key not in seen:
                seen.add(key)
                found.append(path)
    return found


def program_profile_dirs(profile: Path) -> list[Path]:
    """进程侧普查：此刻有哪些**活进程**正用着我们家的资料目录（v0.13.37）。

    为什么端口普查不够（真机 m38110 的定案）：饼干窗硬刷新都不弹回登录页 ——
    那扇窗的进程罐里明明有一张有效 userhash；程序的对账却写着 页=1（只有登录页）、
    整罐只有 PHPSESSID。两边都是真的，因为它们是**两个浏览器进程**：旧那扇的
    ``DevToolsActivePort`` 被后来哪一轮 ``_launch`` 预删了（:meth:`_launch` 里那手
    unlink），端口普查（:func:`live_browser_dirs`）只能问「名字猜得到的目录」，
    备用目录的名字带着当时进程的 PID 和时间戳，新进程猜不出 —— 于是旧窗彻底隐形。

    这一手不看端口文件，直接问操作系统：列出所有 msedge/chrome 进程的命令行，
    认出自家目录（:func:`_parse_program_profile_lines`）。查不到、非 Windows、
    powershell 被拦，一律回空表 —— 它是普查的补刀，不该拦下登录的正路。
    """
    if not _PROCESS_CENSUS:  # 用例把它关掉：这一手要真起一个 powershell
        return []
    # 和 :func:`_kill_processes_using_profile` 同一个理由：当场 import 标准库，
    # 不走本模块顶部那个名字 —— 用例会把 subprocess.Popen 换成假浏览器。
    import subprocess as _subprocess

    script = (
        "$ErrorActionPreference='SilentlyContinue';"
        "Get-CimInstance Win32_Process -Filter \"Name='msedge.exe' or Name='chrome.exe'\" |"
        " ForEach-Object { if ($_.CommandLine -match '--user-data-dir') {"
        " \"$($_.ProcessId)`t$($_.CommandLine)\" } }"
    )
    try:
        done = _subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20.0,
            creationflags=getattr(_subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, _subprocess.SubprocessError):  # pragma: no cover —— 尽力而为
        return []
    return _parse_program_profile_lines(done.stdout or "", profile)


def _profile_failure(exc: BaseException) -> bool:
    """这次失败是不是「这个 profile 用不了」造成的（权限 / 占用）。

    Windows 上看错误码：5 = 拒绝访问，32 = 文件被占用。别的错（比如浏览器路径
    不对、启动就退出）换目录也没用，别白白重试一遍。

    顺带认一下 ``errno``：同一个错误在 Linux / macOS 上只带 ``errno``（分别是
    ``EACCES`` 和 ``EBUSY``），而且 Linux 上 ``OSError(..., winerror=5)`` 会把
    ``winerror`` 抹成 ``None`` —— 真机行为靠 Windows 那两个码，跨平台只认这两个。

    要顺着 ``__cause__`` / ``__context__`` 一起看：启动失败是包成
    :class:`CdpError` 抛出来的，只盯着最外层会漏掉里面那个 ``PermissionError``。
    """
    win_codes = (5, 32)  # ERROR_ACCESS_DENIED / ERROR_SHARING_VIOLATION
    posix_codes = (errno.EACCES, errno.EBUSY)
    seen: set[int] = set()
    pending: list[BaseException | None] = [exc]
    while pending:
        current = pending.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        if getattr(current, "winerror", None) in win_codes:
            return True
        if getattr(current, "errno", None) in posix_codes:
            return True
        pending.extend((current.__cause__, current.__context__))
    return False


def _devtools_read_failure(exc: BaseException) -> bool:
    """这次失败是不是「调试端口写了、口却连不上」。

    真机上的表现（用户截图）：
    ``打开浏览器失败：读取浏览器的调试接口失败：<urlopen error [WinError 10061]
    由于目标计算机积极拒绝，无法连接。>``

    ``cdp._http_json`` 自己会在这个时差里重试几秒（端口文件出现到调试服务
    开始收连接之间有三五百毫秒），所以走到这里还带着这个错，说明重试也没
    连上 —— 进程当场死了、或者本地调试端口被拦了。前一种换一个干净资料目录
    常常就能起来，因此 :meth:`LoginBrowser.start` 把它也归到「换目录再试」。

    只看这一种：别的错（浏览器路径不对、目录写不进去）换目录也没用，
    白白多起一次进程。
    """
    win_codes = (10060, 10061, 10065, 10054)
    posix_codes = (errno.ECONNREFUSED, errno.ECONNRESET, errno.ETIMEDOUT)
    seen: set[int] = set()
    pending: list[BaseException | None] = [exc]
    while pending:
        current = pending.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        if getattr(current, "winerror", None) in win_codes:
            return True
        if getattr(current, "errno", None) in posix_codes:
            return True
        pending.extend((current.__cause__, current.__context__))
    return False


def _dead_on_startup(exc: BaseException) -> bool:
    """这次失败是不是「浏览器刚起来就退出了」。

    真机上量到过：Edge 起来就退，退出码 21（用户装了安全软件）。这类失败和
    资料目录里的旧状态有关 —— 换一个空目录、或者换一个浏览器，常常就没事了，
    所以 :meth:`LoginBrowser.start` 会为它多试几轮，而不是立刻报错。

    认的是错误文案里那句固定的话（退出码在 ``__cause__`` 链上的每一层都放过）。
    """
    seen: set[int] = set()
    pending: list[BaseException | None] = [exc]
    while pending:
        current = pending.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        if _DEAD_ON_STARTUP_MARKER in str(current):
            return True
        pending.extend((current.__cause__, current.__context__))
    return False


def build_args(
    info: BrowserInfo,
    profile: Path,
    proxy: str = "",
    start_url: str = LOGIN_URL,
) -> list[str]:
    """拼启动参数。

    刻意不加的几项都有原因：``--headless`` 用户看不见窗口就没法登录；
    ``--guest`` / ``--incognito`` 用完即弃，留不住登录态，下次还得重来。

    ``start_url`` 默认是登录页；PDF 渲染这类「不需要人看」的场景会传本地文件地址。

    打包好的 exe 里还要补 ``--no-sandbox``（冻结环境里浏览器会以 STATUS_BREAKPOINT
    直接退出），补哪几个由 ``browser_flags`` 决定 —— 源码运行不带这个开关。
    """
    args = [
        info.path,
        "--remote-debugging-port=0",
        f"--user-data-dir={profile}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-features=Translate",
        *launch_flags(),
    ]
    if proxy:
        args.append(f"--proxy-server={proxy}")
    args.append(start_url)
    return args


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
    except CdpError:
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
        raise CdpError("浏览器的调试端口文件是空的，启动可能没成功。")
    try:
        port = int(lines[0])
    except ValueError as exc:
        raise CdpError(f"调试端口文件的内容不对：{lines[0][:40]}") from exc
    if not 0 < port < 65536:
        raise CdpError(f"调试端口超出范围：{port}")
    return port


def _parse_devtools_file(text: str) -> tuple[int, str]:
    """取（端口, 浏览器级 WebSocket 路径）。"""
    port = parse_devtools_port(text)
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return port, lines[1] if len(lines) > 1 else ""


def _port_file_fresh(
    text: str, mtime: float, stale_text: str | None, stale_mtime: float | None
) -> bool:
    """这份端口文件是不是我们这次启动**之后**写出来的（v0.13.38）。

    ``stale_*`` 是启动前记下的旧内容与旧时间戳（原本没有这个文件时都是 ``None``）。
    只要两者之一对不上（内容变了、或者时间戳变了），就说明浏览器新写了一份、可以认。
    有了这个判据，就不必像以前那样先删文件 —— 那一删很可能删在**另一扇还开着的
    窗**头上（见 :meth:`LoginBrowser._launch` 里那段说明）。
    """
    if stale_text is None or stale_mtime is None:
        return True
    return text != stale_text or mtime != stale_mtime


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


def _kill_processes_using_profile(profile: Path) -> None:
    """把命令行里带这个资料目录的浏览器进程收掉（只为让目录删得掉）。

    为什么要单独有这一手：浏览器是多进程的，**父进程退出不等于子进程都走了** ——
    真机上就是这么留下过一批还开着 ``--user-data-dir=<临时目录>`` 的 msedge 子进程，
    把目录里的文件句柄按着，界面层随后怎么删都删不干净。``taskkill /T`` 只收得住
    我们自己启动的那一棵树，收不住「父进程已经把活交出去、自己先退了」的那些。

    用绝对路径匹配命令行，免得把用户自己开着的浏览器窗口误伤；查不到就什么都不做。

    **这里的 ``subprocess`` 是当场 import 标准库拿的**，不走本模块顶部那个名字：
    用例会把 ``browser_login.subprocess.Popen`` 换成替身来假装浏览器，收尾时要是
    顺手用了那个替身，就会在测试里真去弹一个 powershell。收尾这一手没有别的意思，
    就是想删目录，不该被替身带着走。
    """
    if not _KILL_PROFILE_PROCESSES:  # 用例把它关掉：里面要真起一个 powershell
        return
    import subprocess as _subprocess

    script = (
        "$ErrorActionPreference='SilentlyContinue';"
        "Get-CimInstance Win32_Process -Filter \"Name='msedge.exe' or Name='chrome.exe'\" |"
        f" Where-Object {{ $_.CommandLine -like '*{profile}*' }} |"
        " ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"
    )
    try:
        _subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            stdin=_subprocess.DEVNULL,
            stdout=_subprocess.DEVNULL,
            stderr=_subprocess.DEVNULL,
            timeout=20.0,
            creationflags=getattr(_subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, _subprocess.SubprocessError):  # pragma: no cover —— 尽力而为
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
        start_url: str = LOGIN_URL,
    ) -> None:
        self.info = info
        self.profile = Path(profile)
        #: 这次真正用的浏览器资料目录（换了备用目录时和 ``profile`` 不一样）。
        self._profile = Path(profile)
        #: 换了目录时留给用户看的一句话（见 :attr:`profile_note`）。
        self._profile_note = ""
        #: 换了一个浏览器时留给用户看的一句话（见 :attr:`browser_note`）。
        self._browser_note = ""
        #: 开窗前「活着的程序窗口普查」结果一句话（见 :attr:`census_note`）。
        self._census_note = ""
        self.proxy = proxy
        self.timeout = timeout
        self.start_url = start_url or LOGIN_URL
        #: 现场目录起不来时要不要换备用资料目录 / 换浏览器。登录那条路要（见 :meth:`start`），
        #: 但 ``--check-browser`` 那种「如实回答这一个浏览器行不行」的探测里必须关掉。
        self.fallback_profiles = True
        #: 这次启动建出来的那个临时资料目录；没建就是 None。**建出来就记上**
        #: （不是等浏览器起来才记）：失败尝试也会把目录建出来，收尾时要一并删掉。
        self.temp_profile: Path | None = None
        #: 这次会话**建过的每一个**临时资料目录（换浏览器时会各拿一个新的）。
        #: 收尾时按这张清单挨个删 —— 见 :meth:`cleanup_temp_profile`。
        self._temp_dirs: list[Path] = []
        self.process: subprocess.Popen[bytes] | None = None
        self.port = 0
        self.ws_path = ""
        self.browser_ws_url = ""

    @property
    def profile_note(self) -> str:
        """启动时若换了 profile 目录，这里记着一句给用户看的话。"""
        return self._profile_note

    @property
    def browser_note(self) -> str:
        """启动时若换了一个浏览器，这里记着一句给用户看的话。"""
        return self._browser_note

    @property
    def census_note(self) -> str:
        """开窗前探到几扇「还活着的程序窗口」、接没接手（v0.13.35）。

        真机 m36897 的谜团只剩一种解释没被排除：用户登录的那扇窗和程序读罐的那扇
        不是同一扇（旧窗口带着**冻住的横幅**幸存）。这一句把普查结果写进运行日志，
        和「读罐对账」的罐名并排，一眼定案。没做普查（``fallback_profiles`` 关掉）
        时是空串。
        """
        return self._census_note

    @property
    def used_profile(self) -> Path:
        """这次**真正在用**的资料目录（复用/换目录后与 ``profile`` 可能不同）。"""
        return self._profile

    def start(self) -> "LoginBrowser":
        """启动浏览器并等它把调试端口写出来。

        **先试一个全新的临时资料目录**（v0.13.9 起，见 :func:`fresh_profile_dir`），
        再试配置目录里那个 ``browser-profile``，最后才是别的备用目录。

        为什么把顺序倒过来：真机上出现过「点『用浏览器登录』永远是『刚起来就退出
        （退出码 21）』，可自检里那个『试浏览器』明明说 Edge 起得来」。两边的差别
        就在资料目录 —— 自检每次都用新的临时目录，登录用的是配置目录里那个**从上
        一次登录留到现在的**目录，里面存着会话状态，坏掉之后每次都同一个下场。
        换个空目录就好，可那次要等到把 ``browser-profile`` 试完才轮到它，用户只看
        得见一句「打开浏览器失败」。登录窗口本来就是临时的，没必要赌那个旧目录。

        ``profile`` 仍然留在候选里：那是用户自己配置目录下的位置，能起来时用它
        （里面可能有他上次登录过的痕迹），起不来才跳过。

        另一种真机上见过的失败是「调试端口写了、口却连不上」——进程当场没了，
        或者安全软件把本地调试端口拦了。前一种换一个干净资料目录就能起来，
        所以``_devtools_read_failure`` 也归到「换目录重试」那一类。

        还有一种是「浏览器刚起来就退出」（真机上量到过 Edge 退出码 21，
        用户装的是安全软件）。这类失败跟资料目录的内容有关：现场那个
        ``browser-profile`` 里存着上一次的会话状态，坏掉之后**每次**都是同一个
        下场，换个空目录就没事了。所以这一种也换目录重试（备用目录都是新建的），
        而且**当前浏览器试不出来就接着试下一个**（Edge → Chrome → …）：
        单独一个浏览器被拦下，不该让用户彻底用不了浏览器登录。
        """
        if self.process is not None:
            return self
        # 先找「还开着的程序窗口」直接接上（v0.13.33，见 :meth:`_try_attach_live`）。
        # 只有登录这条路有「多扇窗互相错认」的问题；``--check-browser`` 那种
        # （fallback_profiles 为假）要如实回答现场目录行不行，不能去蹭别人的窗。
        # 探测必须赶在 :meth:`_launch` 里删旧端口文件之前 —— 删了就没处问死活。
        if self.fallback_profiles:
            attached = self._try_attach_live()
            if attached is not None:
                return attached
        # 登录这条路（``fallback_profiles`` 为真）：新的临时目录排在最前，
        # 然后是用户配置目录里那个，最后是别的备用目录。
        # ``fallback_profiles`` 关掉时只试现场目录（供 --check-browser 如实回答）。
        browsers = (
            browser_candidates(self.info) if self.fallback_profiles else [self.info]
        )
        first_error: BaseException | None = None
        dead_on_startup = False
        for browser in browsers:
            if browser != self.info:
                # 上一个浏览器起不来：换一个（用户机器上通常 Edge 与 Chrome 都有）。
                self.info = browser
                self._browser_note = f"{browsers[0].name} 起不来，这次改用 {browser.name}。"
            # 候选表**每个浏览器各算一次**，第一个候选每次都新建一个临时目录。
            # 为什么不共用一张表：真机上量到过「第一个浏览器把临时目录试败、目录当场
            # 删掉，换下一个浏览器时第一个候选还是那个路径，``_launch`` 的 mkdir 又把它
            # 建出来，可这时 ``temp_profile`` 已经清空了 —— 那一份空壳就永远没人删」。
            # 换浏览器就换个干净目录，既躲开这个坑，也更合「每次都试空目录」的本意。
            candidates = (
                [self._fresh_candidate(), self.profile, *fallback_profile_dirs(self.profile)]
                if self.fallback_profiles
                else [self.profile]
            )
            for index, chosen in enumerate(candidates):
                try:
                    return self._launch(chosen)
                except Exception as exc:  # noqa: BLE001 —— 下面按错误码决定要不要继续试
                    self._reset_process()
                    if first_error is None:
                        first_error = exc
                    if _dead_on_startup(exc):
                        # 这一种换目录 / 换浏览器都有可能救回来，别当场放弃。
                        dead_on_startup = True
                    elif not (_profile_failure(exc) or _devtools_read_failure(exc)):
                        raise
                    if index == 0:
                        # 头一次失败就换目录重试，用户只会在界面上看到一句说明。
                        self._profile = chosen
            if not self.fallback_profiles:
                # 只问「这一个浏览器行不行」时，别顺手把别的浏览器也启起来。
                break
        # 备用目录和其它浏览器都不行：如实把原始错误报上去，别拿最后一次的错盖掉真原因。
        # 报错之前再确认一次临时目录都收干净了 —— 失败路径上 ``_launch`` / ``_reset_process``
        # 已经各收过一轮，这里是兜底：以后新加失败分支时忘了收，也不会在 %TEMP% 里留东西。
        self.cleanup_temp_profile()
        if first_error is None:  # pragma: no cover —— browsers 至少有一个，走不到这儿
            raise CdpError(f"启动 {self.info.name} 失败：找不到可用的浏览器资料目录。")
        if dead_on_startup:
            raise CdpError(
                f"{first_error}\n\n"
                "试过的每个资料目录都是刚起来就退出（多半是目录里的旧状态坏了，"
                "或者被安全软件拦下）。可以试着关掉安全软件的浏览器防护再点一次，"
                "或者改用「直接粘贴饼干登录」。"
            ) from first_error
        raise first_error

    def _adopt_process_side_findings(self, live: list[Path]) -> list[Path]:
        """进程侧普查的补刀：端口普查只认「名字猜得到」的目录，这一手直接问操作系统（v0.13.37）。

        真机 m38110 的现场：用户硬刷新都不弹回的饼干窗，端口普查却报「没探到」——
        那扇窗的 DevToolsActivePort 早被哪一轮 ``_launch`` 预删了，备用目录的名字
        又带着**当时进程**的 PID，新进程猜不出。凡是进程侧探到的自家目录：
        端口还答话的（只是名字猜不到）补进接手名单；端口已经失联的僵尸窗**先收掉**
        ——留着它，用户在那扇窗里登录、点「应用」，程序这一轮的新罐永远看不见，
        两边又对不上。收掉之后照常开新窗，世界上重新只有一扇程序窗口、一个罐。

        返回真正被点去收掉的僵尸目录（活目录就地追加进 ``live``）。只读不改端口
        逻辑；杀进程复用 :func:`_kill_processes_using_profile`（受它的开关保护，
        测试里两个都能替身）。
        """
        known = {str(path).lower() for path in live}
        reaped: list[Path] = []
        for found in program_profile_dirs(self.profile):
            key = str(found).lower()
            if key in known:
                continue
            known.add(key)
            if _profile_in_use(found):
                live.append(found)  # 端口还答话：只是名字猜不到，补进接手名单
                continue
            _kill_processes_using_profile(found)
            reaped.append(found)
        return reaped

    def _try_attach_live(self) -> "LoginBrowser | None":
        """上次程序开的浏览器窗口还开着，就直接接上它（v0.13.33）。

        为什么非做不可：v0.13.9 起每次尝试都换一份新临时目录、开一扇新窗口。
        上一次那扇要是还开着（程序被强杀、或收尾没杀掉浏览器的场景），用户在
        眼前那扇里登录、点「应用」，程序盯的却是这一轮的新罐 —— 两边永远对不上，
        「每次都手动点了应用还是抓不到饼干」（真机 m35800）就是这个样子。
        复用之后世界上同一时刻只有一扇程序窗口、一个罐。
        接法跟 :meth:`_launch` 拿到端口后一模一样：端口和 WebSocket 路径都从
        那扇窗自己的 ``DevToolsActivePort`` 里读。接不上（中途退了、文件读不动）
        就回 None，照常新开。

        v0.13.35：无论接没接上，都把**普查结果**记进 :attr:`census_note`（探到几扇、
        目录叫什么、接的是哪扇）。真机 m36897 的教训：「程序读空罐、用户 F12 却有
        饼干」只要发生在两扇窗之间就永远解释不通，而旧窗口的横幅会**冻**在原地装
        成活的 —— 光看横幅分不清。日志里有了这句，配上「读罐对账」的罐名，一眼定案。

        v0.13.37 补刀：端口普查只认「名字猜得到」的目录，真机 m38110 现场那扇
        硬刷新都不弹回的饼干窗偏偏猜不到 —— 端口文件早被哪轮 ``_launch`` 预删了。
        于是普查瞎报「没探到」、又开一扇，世界上出现两个罐。现在问完端口再问进程
        （:meth:`_adopt_process_side_findings`）：端口失联的僵尸窗**先收掉**，
        名字猜不到但端口还答话的补进接手名单。

        v0.13.38 两处补刀。一是 :meth:`_launch` 不再删别人的端口文件了，m38110 的
        另一半成因就此消掉（那一手原本怕读到过期端口，代价是把还开着的窗弄瞎）。
        二是**多扇窗时先问罐**：不再是「按端口文件时间戳挑第一扇」——那跟「哪扇窗
        登录过」没有半点关系——而是挨个问一遍（:func:`window_userhash`），罐里真有
        userhash 的那扇优先接手（见 :meth:`_order_by_login`），普查句里也点名
        「几扇里几扇留着登录痕迹」。真机 m38110 那种「用户在 A 窗登录、程序接 B 窗
        空罐」的错认，从这一版起要么不发生、要么在日志里一眼看得见。
        """
        live = live_browser_dirs(self.profile)
        reaped = self._adopt_process_side_findings(live)
        if live:
            names = "、".join(path.name for path in live[:5])
            self._census_note = (
                f"开窗前普查：探到 {len(live)} 扇还活着的程序窗口（{names}）。"
            )
        else:
            self._census_note = "开窗前普查：没探到活着的程序窗口，这次是全新开的一扇。"
        if reaped:
            self._census_note += (
                f"进程侧探到 {len(reaped)} 扇端口失联的旧窗口（"
                f"{'、'.join(path.name for path in reaped[:5])}），已先收掉，"
                "免得它和这一轮程序用的窗口混在一起。"
            )
        # 端口文件读得动、里面真有端口的，才算能接上的候选（读不动＝探测和读之间它退了）。
        attachable: list[tuple[Path, int, str]] = []
        for chosen in live:
            try:
                port, ws_path = _parse_devtools_file(
                    (chosen / "DevToolsActivePort").read_text(encoding="utf-8", errors="replace")
                )
            except (OSError, CdpError):  # pragma: no cover —— 探测和读之间它退了
                continue
            if port:
                attachable.append((chosen, port, ws_path))
        # v0.13.38：多扇窗时先问罐、把有登录痕迹的那扇排前面（见 :meth:`_order_by_login`）。
        if len(attachable) > 1:
            attachable = self._order_by_login(attachable)
        for chosen, port, ws_path in attachable:
            self._profile = chosen
            self.port = port
            self.ws_path = ws_path
            self.browser_ws_url = f"ws://127.0.0.1:{port}{ws_path}"
            self._profile_note = (
                f"接到之前开着的程序窗口上了（资料目录 {chosen}）。"
                "那个窗口里登录过就接着用；要重新登录也请在【这个】窗口里做。"
            )
            self._census_note += f"接手了「{chosen.name}」这一扇。"
            return self
        if live:
            self._census_note += "挨个问了一遍都没接上，照常新开一扇。"
        return None

    def _order_by_login(
        self, candidates: list[tuple[Path, int, str]]
    ) -> list[tuple[Path, int, str]]:
        """多扇窗里把「罐里有 userhash」的排到前面（v0.13.38）。

        为什么：真机 m38110 现场两扇窗 —— 用户眼前那扇罐里有 userhash，程序却接上了
        另一扇空罐，于是「F12 里明明有饼干、程序说没有」，怎么点「应用」都对不上。
        旧逻辑是「按端口文件时间戳挑第一扇」，可「谁最后开的」和「谁登录过」毫无关系。
        现在直接问罐（:func:`window_userhash`），并在普查句里点名，一眼能看出接的是谁。

        都没有登录痕迹时**保持原顺序**（端口文件时间倒序，最新那扇在前）：这时候没有
        更好的判据，别自作主张重排 —— 每次开窗挑中另一扇会让横幅跟着乱跳。
        """
        scored: list[tuple[bool, tuple[Path, int, str]]] = []
        for item in candidates:
            port, ws_path = item[1], item[2]
            scored.append((bool(window_userhash(f"ws://127.0.0.1:{port}{ws_path}")), item))
        logged = [item for flag, item in scored if flag]
        if not logged:
            return [item for _flag, item in scored]
        names = "、".join(item[0].name for item in logged[:5])
        self._census_note += (
            f"其中 {len(logged)} 扇的罐里留着登录痕迹（{names}），先接这一扇。"
        )
        return logged + [item for flag, item in scored if not flag]

    def _fresh_candidate(self) -> Path:
        """这次要用的新临时资料目录；建不出来就退回现场目录（让 start() 照常跑）。

        **建出来就立刻记在 ``self.temp_profile`` 与 ``self._temp_dirs`` 上**（不是等
        浏览器起来才记）：真机上量过，「起来就退」的失败尝试也会把目录建出来、里面还
        落进一二十个文件，只在成功那一步才记的话，这些目录永远没人删 —— 一连失败几次
        就在 %TEMP% 里攒一堆空壳。记下来之后无论后面哪一步失败，收尾都会把它删掉。
        """
        try:
            fresh = fresh_profile_dir()
        except OSError:  # pragma: no cover —— 拿不到临时目录时不该把登录整条掐掉
            return self.profile
        self.temp_profile = fresh
        if fresh not in self._temp_dirs:
            self._temp_dirs.append(fresh)
        return fresh

    def devtools_failure_hint(self) -> str:
        """读调试接口重试期间的一句提示：浏览器已经没了就返回死因，否则返回空串。

        为什么需要它：读 ``/json/list`` 现在会在几秒预算内重试（端口文件出现
        和调试服务开始收连接之间有几百毫秒时差）。要是进程在这期间就退出了，
        等满预算是白等 —— 直接告诉用户浏览器没起来。返回空串表示「进程还在，
        继续等」。
        """
        process = self.process
        if process is None:
            return ""
        code = process.poll()
        if code is None:
            return ""
        return (
            f"{self.info.name} 刚起来就退出了（退出码 {code}），"
            "所以读不到它的调试接口。装了什么拦截软件的话先关掉再试，"
            "也可以改用「直接粘贴饼干登录」。"
        )

    def _reset_process(self) -> None:
        """把上一次没起来的进程收干净，好让同一个对象再试一个目录。"""
        if self.process is None:
            return
        try:
            self.stop()
        except Exception:  # noqa: BLE001 —— 收尾失败不该盖住启动的真正错误
            self.process = None

    def _launch(self, chosen: Path) -> "LoginBrowser":
        """真正启一次浏览器（``chosen`` 这次要用的 profile 目录）。"""
        try:
            chosen.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            # 连目录都建不出来（父目录也写不进去）也算「这个 profile 用不了」，
            # 交给 start() 去换下一个备用目录。
            raise CdpError(f"浏览器资料目录用不了：{exc}") from exc
        if _is_our_profile_dir(chosen, self.profile) and chosen not in self._temp_dirs:
            # 备用候选有两个是我们自己建的：建在系统临时目录下那个，以及建在配置目录下
            # 的 browser-profile-<PID>-<时间戳>（见 :func:`fallback_profile_dirs`）。
            # 两个都记进清单，收尾一起删 —— v0.13.38 之前只记 %TEMP% 那一族，配置目录
            # 下这一族就成了没人收的垃圾（真机 2026-10-01 留下过两个，5.6MB + 6.3MB）。
            # ``self.profile``（用户配置目录里那份持久的 browser-profile）永远不记：
            # :func:`_is_our_profile_dir` 认位置 + 认名字，它两条都不符合。那是用户
            # 下次还要用的登录状态，不能替他删。
            self._temp_dirs.append(chosen)
        port_file = chosen / "DevToolsActivePort"
        # v0.13.38：**不再**把端口文件删掉（v0.13.9 到 v0.13.37 是删的）。
        #
        # 当初的理由没错：上次崩溃留下的过期端口会让等待循环立刻「成功」，然后连到
        # 一个死端口。代价却一直没人看清 —— 要删的这个目录**可能正有一扇窗开着**：
        # ``self.profile``（配置目录那份持久资料目录）每一轮都在候选表里，哪一轮前面
        # 的目录都起不来、落到它头上时，这一删就删在了**还活着的那扇窗**头上。真机
        # m38110 现场正是如此：用户眼前那扇「饼干列表」窗硬刷新都还在登录态，可它
        # 自己的端口文件没了 ⇒ 端口普查永远探不到它 ⇒ 程序又开一扇新窗 ⇒ 同一时刻
        # 世上两个罐，用户在旧窗里登录、程序读新窗的空罐，怎么点「应用」都对不上。
        #
        # 现在的做法：先记下启动前的旧内容和旧时间戳，等待循环里只认「内容变了」或
        # 「时间戳变了」的那一份 —— 既不会认那个死端口，也不动任何别人的文件。
        try:
            stale_text: str | None = port_file.read_text(encoding="utf-8", errors="replace")
            stale_mtime: float | None = port_file.stat().st_mtime
        except OSError:
            stale_text, stale_mtime = None, None
        try:
            self.process = subprocess.Popen(
                build_args(self.info, chosen, self.proxy, self.start_url),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                # GUI 程序里启动浏览器时别弹一个黑框（这个常量只在 Windows 上有）。
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError as exc:
            # 这一条**不经过 start() 的重试分支**（进程都没起来，换目录也白搭），
            # 所以刚才建出来的临时目录要当场收掉，否则失败一次就在 %TEMP% 留一个空壳。
            self.cleanup_temp_profile()
            raise CdpError(f"启动 {self.info.name} 失败：{exc}") from exc

        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                # 先把真退出码抠出来再 stop()：stop() 会 terminate / taskkill，
                # 之后 returncode 就不是浏览器自己的死因了（真机上那句
                # 「刚起来就退出了（退出码 21）」里的 21 就是这么来的）。
                code = self.process.returncode
                message = (
                    f"{self.info.name} 刚起来就退出了（退出码 {code}）。"
                    "资料目录里的旧状态坏掉、或者被安全软件拦下时会这样；"
                    "程序会换一个干净目录、必要时再换一个浏览器重试。"
                )
                if code == 21:
                    # 真机上量到过的那个码：多半不是浏览器自己崩，而是它已经退出了
                    # 又被要求收尾（Windows ERROR_NOT_READY）。别让这句误导用户去猜。
                    message += "（这个码常常表示进程已经退出、收尾时才报出来，未必是崩溃原因。）"
                if code == 0 and stale_text is not None:
                    # v0.13.38 真机量到的另一种：目录启动前就有一扇窗开着（那份端口文件
                    # 是它留下的），第二个实例会把参数交给它、自己干干净净地退出（退出码
                    # 0，实测于 2026-10-04 的持久资料目录）。不点破的话，用户读到「刚起来
                    # 就退出了」只会往「资料目录坏了」猜，而这里的原因恰好相反：这一份
                    # **正被用着**。这一条也正好接上「地上两个罐」那个现场。
                    message += (
                        "（这份目录启动前就有一扇窗开着、留着端口文件；浏览器遇到这种"
                        "情况通常把要开的页面交给那一扇、自己就退出了，这一条多半如此。）"
                    )
                self.stop()
                raise CdpError(message)
            if port_file.exists():
                try:
                    text = port_file.read_text(encoding="utf-8", errors="replace")
                    text_mtime = port_file.stat().st_mtime
                except OSError:  # pragma: no cover —— 探测和读之间它被删了
                    text, text_mtime = "", 0.0
                # v0.13.38：只认这次启动**之后**写出来的那一份（见上面那段说明）。旧的
                # 那份原样留在盘上 —— 它属于另一扇还开着的窗，该由 _try_attach_live 去接，
                # 而不是拿它的端口号去连一个可能已经死掉的端口。
                if not _port_file_fresh(text, text_mtime, stale_text, stale_mtime):
                    time.sleep(_POLL_INTERVAL)
                    continue
                try:
                    # 文件刚建好时可能只写了一半，读到半截就当还没好，继续等。
                    self.port, self.ws_path = _parse_devtools_file(text)
                except CdpError:
                    self.port, self.ws_path = 0, ""
                if self.port:
                    self.browser_ws_url = f"ws://127.0.0.1:{self.port}{self.ws_path}"
                    if chosen == self.temp_profile:
                        # 登录这条路现在先试新建的临时目录（见 start()）。要在界面上
                        # 说一句，否则用户会以为「我上次登录的痕迹怎么没了」。
                        self._profile_note = (
                            f"这次用的是临时资料目录（{chosen}），"
                            "关掉登录窗口后会自动清掉，不影响你自己的浏览器。"
                        )
                    elif chosen == self.profile:
                        # v0.13.37：落到配置目录那份**持久**资料目录时不能一声不吭。
                        # 真机案例（2026-10-03 深夜）：临时目录在这台机器上起不来
                        # （Edge 退出码 21 一族），每次都悄悄退到 browser-profile；
                        # Edge 用同一份目录会把**上一次登录窗口那一页**原样恢复出来，
                        # 用户看到的「已经登录的饼干列表」其实是上回留下的画面，
                        # 罐里这时候只剩 PHPSESSID（见 MAINTENANCE.md 同日条目）。
                        # 不说清这一句，用户只会更困惑：「不是说每次都开全新目录吗？」
                        self._profile_note = (
                            f"这次用的是程序自己存的浏览器资料目录（{chosen}），"
                            "它留着我们以前用这扇窗登录时打开过的页面——你在窗口里"
                            "看到旧的『饼干列表』就是这么来的，跟你自己平时上网的"
                            "浏览器无关。这一份的饼干会跨窗口留着，登录成功一次之后"
                            "再开一般不用重登；要让这扇窗回到全新状态，关掉所有程序"
                            "开的登录窗口后删掉这个文件夹即可（只动程序自己这份）。"
                        )
                    else:
                        self._profile_note = f"配置目录里的浏览器资料用不了，这次改用了 {chosen}。"
                    return self
            time.sleep(_POLL_INTERVAL)

        self.stop()
        raise CdpError(
            f"等了 {self.timeout:g} 秒还没等到 {self.info.name} 的调试端口。"
            "请确认浏览器能正常打开；装了安全软件时也可能拦下调试端口。"
        )

    def devtools_http(self, path: str) -> str:
        """拼出 CDP 的 HTTP 地址（``/json/list``、``/json/version`` 都在它下面）。"""
        if not self.port:
            raise CdpError("浏览器还没启动，调试端口未知。")
        return f"http://127.0.0.1:{self.port}/{path.lstrip('/')}"

    def stop(self) -> None:
        """关掉浏览器。已经关掉了、或者它不肯走，都不抛异常。"""
        process, self.process = self.process, None
        if process is not None and process.poll() is None:
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
        # 临时资料目录**建出来过就一定要收掉**（不管这次起没起来）：失败尝试也会在里面
        # 落下一二十个文件，真机上量到「一连失败几次，%TEMP% 里就攒一堆空壳」。
        self.cleanup_temp_profile()

    def cleanup_temp_profile(self) -> bool:
        """删掉这次启动时**我们自己建**的资料目录（见 :func:`fresh_profile_dir`）。

        清单见 :attr:`_temp_dirs`：``%TEMP%`` 下的临时目录，以及 v0.13.39 起一并记进来的
        配置目录下那族备用目录（``browser-profile-<PID>-<时间戳>``，见
        :func:`_is_our_profile_dir`）。用户自己那份持久的 ``browser-profile`` 永远不在
        清单里 —— 那是他下次还要用的登录状态。

        这些目录一次登录就是几十上百 MB，不能留着攒。正常关窗时浏览器已经退出，
        直接删；删不掉（浏览器还没走干净、文件句柄没松）就每 0.25 秒再试，
        总共给 :data:`TEMP_PROFILE_WAIT` 秒，**删掉就立刻返回**，不白等。

        清的是 :attr:`_temp_dirs` 这张清单 —— 换浏览器时每个浏览器各拿一个新目录，
        所以可能不止一个。**清单只增不减**（删过的路径也留着）：真机上量到过
        「删掉之后那个路径又被下一次尝试重新建出来」，删一次就把路径忘掉的话，
        那一个就永远没人管了。删不掉的同样留在清单里，下次收尾接着试。

        这里**不改名留记号**（``updater`` 那套是给升级用的）：临时目录本来就在
        系统的临时目录里，坏掉也不会挡着下一次登录 —— 下一次用的是**新的**名字。

        没用到临时目录时什么也不做，返回 False。
        """
        targets = list(self._temp_dirs)
        if self.temp_profile is not None and self.temp_profile not in targets:
            targets.append(self.temp_profile)
        self.temp_profile = None
        if not targets:
            # 没用上临时目录（比如 ``_fresh_candidate`` 拿不到临时目录，退回现场目录）。
            return False
        deleted = False
        for profile in targets:
            if not profile.exists():
                # 目录本来就不在（已经删干净了，或者还没被建出来）：跳过。
                continue
            if _IS_WINDOWS:
                # 浏览器可能还没走干净（或者上一次留下的实例还开着这个目录），
                # 先把「命令行里带这个目录」的进程收掉，再删成功率才高。
                #
                # **这一步放后台线程**：它要起一个 powershell 去查进程表，真机上量到
                # 几百毫秒，在安全软件拦着的时候还会更久 —— 界面层是在主线程里调
                # stop() 的，让它等一个 powershell 会把窗口卡住。真正的删除在下面
                # 主线程里做（每 0.25 秒一次），两边同时进行，通常第一轮就删掉了。
                try:
                    threading.Thread(
                        target=_kill_processes_using_profile,
                        args=(profile,),
                        name="xdao-kill-profile-processes",
                        daemon=True,
                    ).start()
                except RuntimeError:  # pragma: no cover —— 起不了线程就算了
                    pass
            deadline = time.monotonic() + TEMP_PROFILE_WAIT
            while True:
                shutil.rmtree(profile, ignore_errors=True)
                if not profile.exists():
                    deleted = True
                    break
                if time.monotonic() >= deadline:
                    break
                time.sleep(0.25)
        return deleted

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
#
# 帧协议与 CDP 会话都在 ``xdao/cdp.py`` 里：浏览器登录与 PDF 渲染共用同一条通道，
# 这一层不再留在本模块。下面这组名字是**兼容出口**：本模块的既有调用方
# （界面层、测试、真机脚本）一直按这些名字引用，保留别名就不用全体改一遍。
from .cdp import (
    OPCODE_BINARY,
    OPCODE_CLOSE,
    OPCODE_CONTINUATION,
    OPCODE_PING,
    OPCODE_PONG,
    OPCODE_TEXT,
    CDPSession,
    CdpError,
    Frame,
    _FrameReader,
    _SITE_WAIT,
    _accept_key,
    _http_json,
    _split_ws_url,
    _ws_handshake,
    build_frame,
    http_json,  # noqa: F401  —— _http_json 的公开名
    read_frame,
)

# ``BrowserLoginError`` 是历史名字（报错文案没变，类也确实是同一个），
# 放在这里是因为它要等 cdp 的名字导入进来才能绑定。
BrowserLoginError = CdpError


# 站点地址前缀交给会话：``connect()`` 靠它挑对页面标签，
# 这样 cdp.py 自己不必知道业务站点是哪个。
SITE_URLS = (COOKIE_SITE, LOGIN_URL)


def _new_session(
    ws_url: str, timeout: float = 15.0, failure_hint: Callable[[], str] | None = None
) -> CDPSession:
    """建一条会话：带上「本站点」前缀，并让会话用本模块的 ``_http_json`` 读标签列表。

    两个名字都在**调用时**从本模块的名字表里取：
    * ``CDPSession`` —— 调用方（界面测试）会替换本模块的 ``CDPSession`` 来塞替身，
      必须取替换后的那个，不能是导入时就绑死的类对象；
    * ``_http_json`` —— 老代码里读 ``/json/list`` 就发生在本模块，把「怎么读」
      显式交给会话，替换这个名字才仍然换得掉会话的行为。

    ``failure_hint`` 一路交给会话：读调试接口会在预算内重试，重试期间用它问
    「浏览器进程还在吗」——不在了就当场报死因，别让用户干等（见
    :meth:`LoginBrowser.devtools_failure_hint`）。
    """
    names = globals()
    return names["CDPSession"](
        ws_url,
        timeout=timeout,
        site_urls=list(SITE_URLS),
        http_json=names["_http_json"],
        failure_hint=failure_hint,
    )


def _pick_page(pages: list[dict]) -> dict:
    """挑一个页面标签：优先本站点，其次任意非内部页，最后兜底第一个。"""
    from .cdp import pick_page, pick_site_page

    site = pick_site_page(pages, list(SITE_URLS))
    return site if site is not None else pick_page(pages)


def _pick_site_page(pages: list[dict]) -> dict | None:
    """挑出属于本站点的标签；没有就返回 None。"""
    from .cdp import pick_site_page

    return pick_site_page(pages, list(SITE_URLS))


# ---------------------------------------------------------------- 饼干


#: 「这明显是一句话，不是一个值」：中日韩文字与全角标点。
#:
#: 站点把人话（「没权限访问」之类）塞进正文时，粗筛得能挡住；除此之外一律放行。
_LOOKS_LIKE_PROSE = re.compile(r"[\u3000-\u303f\u4e00-\u9fff\uff00-\uffef]")


def looks_like_userhash(value: str) -> bool:
    """像不像一个 userhash：非空、至少 6 位、无空白、无 ``;``、不成句中文。

    X 岛的 userhash 是 8 位左右的不透明串。这里是给「用户粘贴了一堆东西」和
    「站点正文里抠出来的值」兜底用的粗筛，不要求它懂 X 岛的内部规则 ——
    真假由 :func:`xdao.gui.verify_userhash_live` 去问服务端。

    2026-10-02（v0.13.25）放宽：以前要求**全是可打印 ASCII**。真机上站点从导出页
    回给我们的值只要含一个非 ASCII 字符（正文里的字节被解码成替换字符，或高位字节），
    这里就判「不像」；而调用方当时是**一声不响**地丢掉，界面上只剩「还没有 userhash」。
    m31725 正是这样：用户自己在站点里看饼干列表一切正常，程序却一个字都不说。
    现在只挡一眼就不是值的东西（空白、``;``、尖括号、成句的中文）。
    """
    if not value or len(value) < 6:
        return False
    if ";" in value or "<" in value or ">" in value:
        return False
    # 空白要显式挡：``str.isprintable()`` 认为空格是可打印的，只靠它会把
    # 「ABC 12345」这种两句拼在一起的东西当成一个值。
    if any(character.isspace() for character in value):
        return False
    if _LOOKS_LIKE_PROSE.search(value):
        return False
    return any(character.isprintable() for character in value)


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


# 「应用一块饼干」这一段全部走**导航**，不在页面里复刻接口。
#
# 为什么不能用 fetch 复刻（v0.13.15 的做法，真机失败）：站点自己的「应用」是
# 跳转式的 —— 点下去先落到站点自己的「跳转提示」页（1496 字节、<title>跳转提示</title>、
# <a id="href" href="…">），再由那一页跳到真正的落地页，userhash 是在落地那一跳里
# 由主站种下的。页面里 fetch 拿回来的只是那张提示页的 HTML：它既不执行提示页里的
# meta refresh、也不会发出第二跳，饼干罐里始终没有 userhash —— 用户明明登录好了，
# 界面却一路等到超时（用户 m25142 的那张截图）。
# 导航走的是浏览器自己的路：跳转页自己会跳，饼干也就种上了。
_FIND_APPLY_JS = r"""
(() => {
  const COOKIE_BASE = __COOKIE_BASE__;
  const state = {
    url: location.href,
    login: /\/Member\/User\/Index\/login\.html/i.test(location.pathname),
    jump: '',
    countdown: false,
    kind: 'other',
    href: '',
    ids: [],
    rows: 0,
    links: 0,
  };
  // 站点自己的 success() 页：<a id="href" href="...">；也有写成 meta refresh 的。
  const anchor = document.querySelector('a#href') || document.getElementById('href');
  let jump = anchor ? (anchor.getAttribute('href') || '') : '';
  if (!jump) {
    const meta = document.querySelector('meta[http-equiv="refresh" i]');
    const content = meta ? (meta.getAttribute('content') || '') : '';
    const hit = content.match(/url\s*=\s*['"]?([^'";]+)/i);
    if (hit) jump = hit[1];
  }
  const absolute = (raw) => {
    try { return new URL(raw, location.href).href; } catch (err) { return raw; }
  };
  if (jump) state.jump = absolute(jump);
  // 站点自己的倒计时页（真机上是「饼干切换成功!」+「页面自动跳转 等待时间：1」）：
  // userhash 是在**它跳完之后**那一跳的响应里由主站种下的。这一页上既没有 a#href
  // 也没有 meta refresh，认不出来就等于「没什么可等的」——调用方会直接去读饼干罐、
  // 再去导出页，把还在倒数的标签页拽走，那一跳就永远不发生了（用户 m29500 的截图：
  // 标签页停在倒计时页，程序却报「还没登录」）。
  const bodyText = (document.body ? document.body.innerText : '') || '';
  state.countdown = /等待时间|自动\s*跳转|跳转提示/.test(bodyText);
  // 列表行的「应用」链接：同一个 id 会有 switchTo 与 export 两个链接，
  // 只记 switchTo（应用）那个；最新申领的饼干排在列表最后，所以取最后一个 id。
  const actions = {};
  for (const link of document.querySelectorAll('a[href]')) {
    const raw = link.getAttribute('href') || '';
    const hit = raw.match(/Cookie\/(?:switchTo|export)\/id\/([^\/\s"'<]+)/i);
    if (!hit) continue;
    const id = hit[1].replace(/\.html$/i, '');
    if (!id) continue;
    state.links += 1;
    if (state.ids.indexOf(id) < 0) state.ids.push(id);
    if (/switchTo/i.test(raw) && !actions[id]) actions[id] = absolute(raw);
  }
  if (state.ids.length === 0) {
    // 兼容只有表格、没有链接的写法：每行第二个单元格是 id。
    for (const row of document.querySelectorAll('tr')) {
      const cells = row.querySelectorAll('td');
      if (cells.length < 2) continue;
      state.rows += 1;
      const text = (cells[1].textContent || '').trim();
      if (text && state.ids.indexOf(text) < 0) state.ids.push(text);
    }
  }
  const last = state.ids.length ? state.ids[state.ids.length - 1] : '';
  if (last) {
    state.href = actions[last] ||
      (COOKIE_BASE + 'switchTo/id/' + encodeURIComponent(last) + '.html');
  }
  if (state.login) state.kind = 'login';
  else if (state.jump || state.countdown) state.kind = 'jump';
  else if (last) state.kind = 'list';
  else if (state.rows) state.kind = 'empty';
  return state;
})()
"""

# 跟着站点自己的「跳转提示」页最多跳几次：列表 → switchTo → 落地页，两次就够，
# 上限只是防站点把跳转写成一圈。
MAX_COOKIE_JUMPS = 3
# 站点自己的「跳转提示 / 倒计时」页要等多久（v0.13.21）。真机那一页上写着
# 「等待时间：1」，可那是站点自己的 JS 在倒数，慢一点的机器、慢一点的网都会拖几秒；
# 等不到就等不到，别把用户的一页停在那儿不管。
JUMP_WAIT_SECONDS = 8.0
# 应用之后等饼干罐里出现 userhash 等多久、隔多久看一眼（v0.13.21）：
# 种 userhash 的是站点自己那一跳的响应，读早半拍罐里就是空的。
COOKIE_WAIT_SECONDS = 4.0
COOKIE_WAIT_POLL = 0.5
# 一次导航最多等多久。等的是 ``document.readyState == 'complete'``：跳转提示页
# 要在文档加载完之后才跳，早一瞬去读，读到的还是那张提示页。
NAVIGATE_TIMEOUT = 10.0
# 导航期间轮询页面状态的间隔。
NAVIGATE_POLL = 0.25


def _cookie_action_base() -> str:
    """``switchTo`` / ``export`` 两个接口所在的目录，从常量推出来免得写两遍。"""
    prefix = COOKIE_LIST_PATH.rsplit("/", 1)[0]
    return f"{COOKIE_SITE}{prefix}/"


def build_find_apply_script() -> str:
    """返回「看看当前页有什么」的脚本（只读 DOM，不发请求）。

    返回值里有：当前地址、是不是登录页、跳转提示页的目标、列表里「应用」链接
    指向哪、认出来的 id 有哪些、表格行数与链接数（后两个是给诊断用的）。
    """
    return _FIND_APPLY_JS.replace("__COOKIE_BASE__", json.dumps(_cookie_action_base()))


@dataclass(frozen=True)
class LeafCookie:
    """领饼干的结果：成功给值，失败给一句能直接写进窗口的话。

    为什么要带 ``detail``：这条路以前是**静默**的（异常一律吞掉、返回 None），
    真机上表现为「登录好了，程序一直等到超时」，用户截的图里没有任何线索。
    ``navigated`` 表示这一次去动过用户的页面（导航过，或者读页面状态时就出错了）：
    界面层拿它决定下一次重试要不要缓一缓 —— 连着几秒把标签页弹来弹去，比多等一会儿糟。
    """

    value: str | None = None
    detail: str = ""
    navigated: bool = False


def cookie_urls_for(session: "CDPSession") -> list[str]:
    """读饼干时该问哪些地址。

    ``Network.getCookies`` 只回「会发给这些地址」的饼干，所以不能只问一个：
    站点可能在 ``www`` 之外的域上种 userhash，也可能带路径限定；当前页面地址也
    一起问上，免得漏掉只对那一页可见的那块。
    """
    urls = [f"{COOKIE_SITE}/", f"{COOKIE_SITE}{COOKIE_LIST_PATH}", "https://nmbxd1.com/"]
    try:
        current = (session.current_url() or "").strip()
    except Exception:  # noqa: BLE001 —— 读不到地址不该让整件事失败
        current = ""
    if current.startswith("http") and current not in urls:
        urls.append(current)
    return urls


def userhash_from_cookies(cookies: Iterable[dict]) -> str | None:
    """从饼干列表里挑 userhash：认值的形状，域只要沾 ``nmbxd1`` 就算。"""
    for cookie in cookies or []:
        if not isinstance(cookie, dict) or cookie.get("name") != "userhash":
            continue
        domain = str(cookie.get("domain") or "").strip().lower()
        if domain and "nmbxd1" not in domain:
            continue
        value = str(cookie.get("value") or "").strip()
        if value and looks_like_userhash(value):
            return value
    return None


def read_userhash_cookie(
    session: "CDPSession", urls: list[str] | None = None
) -> str | None:
    """读一次饼干罐（不问服务端）。读不到或读失败都只是 ``None``。"""
    try:
        cookies = read_site_cookies(session, urls)
    except Exception:  # noqa: BLE001 —— 这里只回答「罐里有没有」
        return None
    return userhash_from_cookies(cookies)


def _probe_http_json(url: str, timeout: float = 5.0) -> object:
    """读一次调试接口，**不重试** —— 多扇窗挑一扇时的「问一句就走」。

    为什么不直接用会话默认那个 ``_http_json``：它带着 ``cdp._DEVTOOLS_READ_BUDGET``
    （8 秒）的预算，那是留给「端口文件刚写出来、调试服务还没开始收连接」的时差的。
    挑窗时那扇窗的端口刚被 :func:`_profile_in_use` 验过是活的（用的是同一个不重试的
    读法），真读不通就不必替它等满预算 —— 否则「哪扇窗登录过」这一个问题能把开窗
    拖慢十几秒。
    """
    from .cdp import _http_json_once

    return _http_json_once(url, timeout)


def window_userhash(ws_url: str, timeout: float = WINDOW_PROBE_TIMEOUT) -> str:
    """问一扇**已经开着的**窗口：「你罐里有没有 userhash」（v0.13.38）。

    真机 m38110：世界上有过两扇程序窗，用户在 A 窗里登录着（罐里有 userhash），
    程序却接上了 B 窗（空罐）—— 于是「F12 里有饼干、程序说没有」，怎么点「应用」
    都对不上。两扇窗谁先开的、谁最新，跟「谁登录过」没有半点关系，能回答这个问题
    的只有罐本身。这里只跟那扇窗的调试端口说两句话：连上去、读一次饼干、断开；
    不导航、不点按钮、不看页面，用户正停着的那一页连闪都不会闪。

    读不到（连不上、罐是空的、超了预算）一律回空串，调用方按「这扇没有登录痕迹」处理。
    """
    names = globals()
    try:
        session = names["CDPSession"](
            ws_url,
            timeout=timeout,
            site_urls=list(SITE_URLS),
            http_json=_probe_http_json,
        )
    except Exception:  # noqa: BLE001 —— 连会话都建不起来，就当这扇问不出来
        return ""
    try:
        session.connect()
    except Exception:  # noqa: BLE001 —— 连不上、或者它一个页面标签都没有
        return ""
    try:
        return read_userhash_cookie(session) or ""
    except Exception:  # noqa: BLE001 —— 读的过程中那扇窗退了
        return ""
    finally:
        try:
            session.close()
        except Exception:  # noqa: BLE001 —— 关连接失败不值得往上抛
            pass


def _page_state(session: "CDPSession") -> dict:
    """读当前页面的状态；读不到就给空字典（调用方按「认不出来」处理）。"""
    raw = session.evaluate(build_find_apply_script())
    try:
        state = json.loads(raw) if raw else {}
    except Exception:  # noqa: BLE001 —— 浏览器回了意料之外的东西
        return {}
    return state if isinstance(state, dict) else {}


def _navigate(session: "CDPSession", url: str) -> str:
    """导航到 ``url`` 并等页面加载完，返回落地后的地址。

    导航期间 ``Runtime.evaluate`` 会因为「换了文档」而失败，这是正常的，接着
    轮询就是 —— 真正要等的是 ``readyState == 'complete'``。
    """
    session.call("Page.navigate", {"url": url}, timeout=NAVIGATE_TIMEOUT)
    deadline = time.monotonic() + NAVIGATE_TIMEOUT
    href = ""
    while time.monotonic() < deadline:
        time.sleep(NAVIGATE_POLL)
        try:
            ready = (session.evaluate("document.readyState") or "").strip()
            href = session.evaluate("location.href") or href
        except CdpError:
            continue
        if ready == "complete":
            return href
    return href


def _follow_jumps(
    session: "CDPSession", state: dict, hops: int, *, budget: float | None = None
) -> tuple[dict, int]:
    """等站点自己的「跳转提示」页跳完，并跟着跳（最多 :data:`MAX_COOKIE_JUMPS` 跳）。

    v0.13.21 之前这里是**一次性读**：读的那一刻页面上没有 ``a#href``/meta refresh 就
    什么都不做。真机上正好踩中 —— 导航到 ``switchTo`` 之后站点回的是它自己的倒计时页
    （「饼干切换成功!」+「页面自动跳转 等待时间：1」），那一刻页面上确实两个都没有，
    于是这一跳被跳过，调用方紧接着把标签页拖去导出页，站点自己的第二跳就再也没发生，
    userhash 永远种不上（用户 m29500）。

    现在分两种「还得等」的情况：页面上已经写好了目标（``jump``）就直接跟过去；只写着
    「等待时间」就**等它自己跳**，每 :data:`NAVIGATE_POLL` 秒重读一次页面状态，跳完
    （不再是跳转/倒计时页）或超过 ``budget`` 秒才收手。

    ``budget`` 缺省取 :data:`JUMP_WAIT_SECONDS`（**取在调用时**，用例把那个常量改小
    才拦得住这条路上的等待）。
    """
    if budget is None:
        budget = JUMP_WAIT_SECONDS
    deadline = time.monotonic() + budget
    while hops < MAX_COOKIE_JUMPS and time.monotonic() < deadline:
        jump = str(state.get("jump") or "")
        if jump:
            hops += 1
            _navigate(session, jump)
            state = _page_state(session)
            continue
        if not state.get("countdown"):
            return state, hops
        time.sleep(NAVIGATE_POLL)
        try:
            fresh = _page_state(session)
        except CdpError:
            continue  # 站点正在跳的时候读页面状态失败很正常，接着等
        if fresh:
            state = fresh
    return state, hops


def _wait_for_cookie(
    session: "CDPSession", urls: list[str] | None = None, *, budget: float | None = None
) -> str | None:
    """应用之后**等**饼干罐里出现 userhash（v0.13.21），不是只读一次。

    站点是在跳转落地那一跳的响应里把 userhash 种进饼干罐的，读早半拍就是空；以前这里
    只读一次，读空就判「应用了饼干，但浏览器里始终没出现 userhash」，然后去导出一趟 ——
    正好把还在跳的标签页拽走，越试越不成功（用户 m29500 就是这么卡住的）。

    ``budget`` 缺省取 :data:`COOKIE_WAIT_SECONDS`（取在调用时，方便用例调小）。
    """
    if budget is None:
        budget = COOKIE_WAIT_SECONDS
    deadline = time.monotonic() + budget
    while True:
        value = read_userhash_cookie(session, urls)
        if value or time.monotonic() >= deadline:
            return value
        time.sleep(COOKIE_WAIT_POLL)


def _page_text(session: "CDPSession") -> str:
    """页面上的可见文字（导出页可能把值直接印在页面上）。"""
    try:
        return session.evaluate("document.body ? document.body.innerText : ''") or ""
    except CdpError:
        return ""


def userhash_from_export_text(text: str) -> str | None:
    """导出接口的三种返回形态（照 ``client._extract_userhash_from_export`` 写）。"""
    body = (text or "").strip()
    if not body:
        return None
    try:
        data = json.loads(body)
    except Exception:  # noqa: BLE001 —— 不是 JSON 就往下走文本匹配
        data = None
    if isinstance(data, dict):
        candidate = str(data.get("cookie") or "").strip()
        if candidate and looks_like_userhash(candidate):
            return candidate
    for pattern in (r"userhash=([^;\"'\s]+)", r"\"cookie\"\s*:\s*\"([^\"]+)\""):
        hit = re.search(pattern, body)
        if hit:
            candidate = hit.group(1).strip()
            if candidate and looks_like_userhash(candidate):
                return candidate
    return None


def fetch_leaf_cookie(
    session: "CDPSession",
    urls: list[str] | None = None,
    *,
    navigate: bool = True,
    waiting_for_login: bool = True,
) -> LeafCookie:
    """登录之后去站点的「饼干」页领一块饼干，返回 :class:`LeafCookie`。

    每一步都可能没成，没成时带着一句人话回来（界面层把它挂在窗口上）：

      1. 还在登录页 → 只说「还没登录」，**绝不导航**：用户可能正在输验证码，
         把他从表单上拽走比多等一会儿糟得多；
      2. 当前页是站点自己的「跳转提示」页 → 等它跳完并跟着跳；
      3. 当前页不是「饼干」列表 → 导航到列表页（再跟着跳）；
      4. 列表里取最后一块（最新申领的权限最全）→ 导航到它的 ``switchTo`` 地址，
         这一步会让主站把 userhash 种进浏览器；
      5. 等饼干罐里出现 userhash；还是没有，就再导航一次导出页，从页面文字里找一遍。

    ``navigate=False`` 时**只读页面状态和饼干罐**，一个标签页都不碰（v0.13.23 起界面层
    固定走这一支）：站点那边「应用」是跳转式的，程序一导航，用户的标签页就会被留在
    站点自己的「饼干切换成功!」倒计时页上原地重载 —— 用户看到的就是「一直无限跳转」
    （m29953/m29954/m30629）。领饼干的正事交给 :func:`apply_leaf_cookie_over_http`
    （走 HTTP，见 ``XdaoClient.apply_cookie``）。

    ``waiting_for_login=False``（界面层在**已经真去应用过**之后传，v0.13.21）表示
    「页面停在登录页」不再按「用户正在打字」处理：程序自己那一趟导出页导航会把还没种上
    userhash 的标签页弹到登录页，要是照旧早退，就再也不会去应用饼干，界面上只会每隔
    几秒重复一句「这个窗口里还没登录（页面停在登录页）」直到超时（用户 m29500 卡住的
    正是这一支）。这时宁可继续去「饼干」页试，也要把真实结果报回来。
    """
    try:
        if not navigate:
            value = read_userhash_cookie(session, urls)
            if value:
                return LeafCookie(value, "")
            # 不导航，但**要看一眼页面**（v0.13.23）：用户还在登录页打字时，界面层得
            # 如实说「还没登录」，而不是一句笼统的「试过了」——那种话在真机上跟卡死
            # 没区别（v0.13.17 的教训）。看一眼不算打扰：只读 DOM，不动标签页。
            try:
                state = _page_state(session)
            except CdpError:
                state = {}
            if (state.get("kind") == "login" or state.get("login")) and waiting_for_login:
                return LeafCookie(None, "这个窗口里还没登录（页面停在登录页）：先在里面登录 X 岛。")
            page_url = str(state.get("url") or "")
            host = urllib.parse.urlsplit(page_url).hostname or ""
            if host and "nmbxd1" not in host:
                return LeafCookie(
                    None, f"浏览器窗口里现在打开的不是 X 岛（{host}），先在里面对 X 岛登录。"
                )
            return LeafCookie(
                None,
                # v0.13.36 补后半句（真机 m37565）：这一支正是「页面不是登录页、罐里却
                # 没有 userhash」——用户看到的多半是上次登录留下的「饼干列表」缓存画面，
                # 得说破，否则他会以为已经登进去了、剩下的是程序的锅。
                "自动取饼干这条路试过了：浏览器里还是没有 userhash。"
                "你现在看到的页面可能是上次登录留下的缓存——请在这个窗口里回登录页，"
                "重新提交账号密码验证码。",
            )
        state = _page_state(session)
        if (state.get("kind") == "login" or state.get("login")) and waiting_for_login:
            return LeafCookie(None, "这个窗口里还没登录（页面停在登录页）：先在里面登录 X 岛。")
        page_url = str(state.get("url") or "")
        host = urllib.parse.urlsplit(page_url).hostname or ""
        if host and "nmbxd1" not in host:
            return LeafCookie(None, f"浏览器窗口里现在打开的不是 X 岛（{host}），先在里面对 X 岛登录。")
        state, hops = _follow_jumps(session, state, 0)
        if state.get("login") and waiting_for_login:
            # 真机上走到的就是这一支（匿名开「饼干」页 → 站点把页面跳到登录页）：
            # `navigated` 必须按真跳没跳报，界面层拿它决定下次重试要不要缓一缓。
            # 已经真去应用过之后（waiting_for_login=False）不再从这里返回：那多半是
            # 程序自己那一趟导出页导航把标签页弹回来的，停在这里就再也领不到饼干了。
            return LeafCookie(
                None, "跟着站点的跳转回到了登录页 —— 这个窗口里的登录没成。", hops > 0
            )
        if not state.get("ids") and COOKIE_LIST_PATH not in str(state.get("url") or ""):
            # 用户停在论坛/用户首页都很正常：那些页面的 DOM 里没有列表，
            # 自己去「饼干」页要一份（导航，不是 fetch）。
            _navigate(session, f"{COOKIE_SITE}{COOKIE_LIST_PATH}")
            state, hops = _follow_jumps(session, _page_state(session), hops)
            if state.get("login"):
                return LeafCookie(
                    None, "打开「饼干」页被弹回了登录页 —— 这个窗口里的登录没成。", True
                )
        ids = [str(item) for item in (state.get("ids") or []) if str(item).strip()]
        if not ids:
            rows = int(state.get("rows") or 0)
            links = int(state.get("links") or 0)
            if state.get("kind") == "empty" or (rows == 0 and links == 0):
                return LeafCookie(
                    None, "「饼干」页里没有可以应用的饼干（这个账号可能还没领过一块）。", True
                )
            return LeafCookie(
                None, f"「饼干」页的写法没认出来（{rows} 行 / {links} 个链接）。", True
            )
        last = ids[-1]
        href = str(state.get("href") or "") or (
            f"{_cookie_action_base()}switchTo/id/{urllib.parse.quote(last)}.html"
        )
        _navigate(session, href)
        state, hops = _follow_jumps(session, _page_state(session), hops)
        # 等它种上（v0.13.21）：站点是在上面那一跳的**落地响应**里种 userhash 的，
        # 读早半拍罐里就是空的 —— 以前读空就往下走去导出了。
        value = _wait_for_cookie(session, urls)
        if value:
            return LeafCookie(value, "", True)
        _navigate(
            session, f"{_cookie_action_base()}export/id/{urllib.parse.quote(last)}.html"
        )
        state, hops = _follow_jumps(session, _page_state(session), hops)
        value = userhash_from_export_text(_page_text(session))
        if value:
            return LeafCookie(value, "", True)
        return LeafCookie(None, "应用了饼干，但浏览器里始终没出现 userhash。", True)
    except CdpError as exc:
        return LeafCookie(None, f"跟浏览器打交道时出错：{exc}", True)


def apply_leaf_cookie(session: "CDPSession") -> str | None:
    """只要值的老签名；要诊断就调 :func:`fetch_leaf_cookie`（界面层走那条）。"""
    return fetch_leaf_cookie(session).value


def named_userhash_entries(cookies: Iterable[dict]) -> list[dict]:
    """罐里所有叫 userhash、域沾 ``nmbxd1`` 的饼干（v0.13.29，诊断用）。

    与 :func:`userhash_from_cookies` 的差别只在**不认值的形状**：名字和域对上了就收。
    界面层拿它区分「罐里根本没有」和「有但值长得不对」—— 后一句以前只能写成
    「还没有 userhash」，跟真没有一模一样，用户截图里查无可查。
    """
    found: list[dict] = []
    for cookie in cookies or []:
        if not isinstance(cookie, dict) or cookie.get("name") != "userhash":
            continue
        domain = str(cookie.get("domain") or "").strip().lower()
        if domain and "nmbxd1" not in domain:
            continue
        found.append(cookie)
    return found


def read_site_cookies(session: "CDPSession", urls: list[str] | None = None) -> list[dict]:
    """把浏览器里属于本站点的饼干**整罐**读出来（不只是 userhash，v0.13.22）。

    界面层拿它去走 HTTP 那条熟路（:func:`apply_leaf_cookie_over_http`）：浏览器只负责
    让用户把验证码认过去，剩下的协议交给 ``XdaoClient``。

    v0.13.29 起这罐是**两读合并**：先按地址过滤的 ``Network.getCookies``，再补一次
    不问地址的整罐读（:meth:`CDPSession.read_all_cookies`），只留域沾 ``nmbxd`` 的、
    按（名字、域、路径）去重。起因是真机上 ``getCookies`` 会系统性漏掉存储里明明白白
    存在的 userhash（2026-10-02 的四张截图：对话框名单里从头到尾没有 userhash，同
    一个浏览器的 F12 Application 面板里却一直有），过滤维度定位不出来，那就干脆把
    不过滤的那一读也问一遍。整罐读失败不拖累主路，只用按地址那一读的结果。

    按地址那一读读不到就**抛**（v0.13.23）：以前这里把异常吞成空列表，界面层于是把
    「读不出来」和「罐里没登录」当成同一件事 —— 真机上表现为 HTTP 那条路一声不响地
    被跳过，用户截图里只剩 CDP 那句「还没登录」，查无可查（m30629）。现在调用方会把
    这句话原样说出来。
    """
    cookies = session.read_cookies(list(urls) if urls else cookie_urls_for(session))
    merged = [item for item in (cookies or []) if isinstance(item, dict)]
    seen = {
        (str(item.get("name")), str(item.get("domain")), str(item.get("path")))
        for item in merged
    }
    try:
        whole = session.read_all_cookies()
    except Exception:  # noqa: BLE001 —— 兜底路失败不该把主路也噤声
        whole = []
    for item in whole or []:
        if not isinstance(item, dict):
            continue
        domain = str(item.get("domain") or "").strip().lower()
        if "nmbxd" not in domain:
            continue
        key = (str(item.get("name")), str(item.get("domain")), str(item.get("path")))
        if key in seen:
            continue
        seen.add(key)
        merged.append(item)
    # v0.13.34 第四读：页面自己的 document.cookie（F12 同源视角）。真机 m36307：
    # 用户的 F12 面板里 userhash 明明躺着，程序三条 CDP 读法却都看不见 —— 本机探针
    # （_scratch/probe_jar_read_v1334.py）证明管道对任何 flag 组合都读得到，剩下的
    # 偏差只能出在「CDP 问的存储上下文 ≠ 页面所在的上下文」。页面 JS 看见的就是
    # 用户看见的，这一读直接把那个视角接进合并罐；HttpOnly 的饼干它看不见，所以
    # 只当补充源，不作废前三读。全程可失败：读不到就当没有，绝不打扰主路。
    for item in read_page_document_cookies(session):
        key = (str(item.get("name")), str(item.get("domain")), str(item.get("path")))
        if key in seen:
            continue
        seen.add(key)
        merged.append(item)
    return merged


def parse_document_cookie(text: str, host: str) -> list[dict]:
    """把 ``document.cookie`` 那串拆成和 CDP 读同形的条目（v0.13.34）。

    只拆「名=值」；值里允许再出现 ``=``（按第一个等号切）。域记成当前页的 host、
    路径记成 ``/`` —— JS 本来就不暴露这两样，这样合并去重时能对上 CDP 条目的键。
    """
    entries: list[dict] = []
    for chunk in str(text or "").split(";"):
        chunk = chunk.strip()
        if not chunk or "=" not in chunk:
            continue
        name, _, value = chunk.partition("=")
        name = name.strip()
        if not name:
            continue
        entries.append(
            {"name": name, "value": value.strip(), "domain": host, "path": "/"}
        )
    return entries


def read_page_document_cookies(session: "CDPSession") -> list[dict]:
    """读被盯页面自己的 ``document.cookie``（v0.13.34）。

    只在页面确实站在 X 岛站内时才读（别的域的 JS 罐与本程序无关），并且**一切
    异常都咽掉回空列表**：这是第四视角的补充源，它出任何问题都不许影响前三读。
    """
    try:
        current = str(session.current_url() or "")
    except Exception:  # noqa: BLE001
        return []
    host = urllib.parse.urlsplit(current).hostname or ""
    if "nmbxd1" not in host:
        return []
    try:
        raw = session.evaluate("document.cookie")
    except Exception:  # noqa: BLE001
        return []
    if not isinstance(raw, str) or not raw:
        return []
    return parse_document_cookie(raw, host)


def summarize_cookies(cookies: Iterable[dict]) -> str:
    """罐子里有哪些饼干：只报**名字**（值一律不写，日志要能直接贴给人看）。

    给界面层报诊断用（v0.13.23）：真机上「读不到饼干」和「只读了匿名会话号」看起来
    一模一样，把名字写进提示行和运行日志，用户截图就能说清卡在哪。
    """
    names: list[str] = []
    for item in cookies or ():
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if name and name not in names:
            names.append(name)
    return "、".join(names)


def jar_forensics(session: "CDPSession", jar_tag: str = "", link_tag: str = "") -> str:
    """「读罐对账」一行：挂着哪页、读的是哪份罐、四路各自看见哪些**名字**（v0.13.34）。

    为什么：真机 m36307 里 F12 看得见 userhash、程序四路合并读却报没有，而对话框
    只说合并结果 —— 分不出漏在哪一读。这一行把每一路的名单并排写进运行日志，
    下次一张截图就能定位（漏在按地址读？整罐读？还是挂错了上下文）。
    规矩与 :func:`summarize_cookies` 相同：**只报名字，值一个字符都不写** ——
    日志要能直接贴给人看，userhash 就是通行证本身。

    v0.13.35 补三段（真机 m36897 的教训：四路名单一致、却仍定不了案）：
    ``罐=`` 是这份资料目录的目录名 —— 和日志里「开窗前普查」那句对得上，就能排除
    「用户登录的窗口 ≠ 程序读的窗口」；``全罐=N`` 是**不滤域**的原始条数，用来分清
    「罐里真没有」和「被 nmbxd 过滤吃掉了」；``原始userhash=`` 直接在原始罐里按名字
    找，报它的域/路径/HttpOnly/是否分区（CHIPS 分区饼干是探针没测过的读法差异），
    照样绝不报值。``jar_tag`` 空就不写罐段。

    v0.13.36 补一段 ``页=``（真机 m37565 的僵局：可见窗口显示已登录的饼干列表页，
    可对账行里 ``挂=`` 一直是 login.html —— 一扇窗里到底是几页站点标签、程序每轮
    重挑时看见的顺序是什么，光看 ``挂=`` 定不了案）：报**此刻**浏览器里能挂上的
    页面标签条数与各自地址（每条截 60 字、最多列 4 条，地址不是秘密，可以进日志）。
    僵尸标签、双标签顺序翻转，从此一张截图就能看出来。

    v0.13.37 补一段 ``接=``（真机 m38110 的定案：饼干窗硬刷新都不弹回、程序对账
    却写 页=1 只有登录页 —— 用户和程序各看着一扇窗，两个罐永远对不上）：报程序
    此刻**接的是哪一扇**（调试端口）。端口号是 CDPSession 自己不知道的（它只知道
    WebSocket 地址），所以由调用方传：``link_tag`` 空就不写这段。
    """
    parts: list[str] = []
    try:
        url = str(session.current_url() or "")
    except Exception as exc:  # noqa: BLE001
        url = f"<读不到（{type(exc).__name__}）>"
    parts.append(f"挂={url[:70]}")
    try:
        pages = session.list_page_targets()
    except Exception as exc:  # noqa: BLE001 —— 替身没这个读法也走这里，明说读不到
        parts.append(f"页=读不到（{type(exc).__name__}）")
    else:
        shown = "｜".join(
            (str(item.get("url") or "").strip() or "空")[:60]
            for item in (pages if isinstance(pages, list) else [])[:4]
            if isinstance(item, dict)
        )
        total = len(pages) if isinstance(pages, list) else 0
        parts.append(f"页={total}（{shown or '空'}）")
    link = str(link_tag or "").strip()
    if link:
        parts.append(f"接={link[:40]}")
    tag = str(jar_tag or "").strip()
    if tag:
        parts.append(f"罐={tag[:40]}")
    try:
        names = summarize_cookies(session.read_cookies(cookie_urls_for(session)))
        parts.append(f"按地址读={names or '空'}")
    except Exception as exc:  # noqa: BLE001
        parts.append(f"按地址读=出错（{type(exc).__name__}）")
    raw: list[dict] | None = None
    try:
        raw = [
            item
            for item in (session.read_all_cookies() or [])
            if isinstance(item, dict)
        ]
    except Exception as exc:  # noqa: BLE001
        parts.append(f"整罐读=出错（{type(exc).__name__}）")
    if raw is not None:
        whole = [
            item
            for item in raw
            if "nmbxd" in str(item.get("domain") or "").strip().lower()
            or not str(item.get("domain") or "").strip()
        ]
        parts.append(f"整罐读={summarize_cookies(whole) or '空'}")
        parts.append(f"全罐={len(raw)}")
        leaves = [
            item for item in raw if str(item.get("name") or "") == "userhash"
        ]
        if not leaves:
            parts.append("原始userhash=无")
        else:
            desc = "、".join(
                "域={}路={}{}{}".format(
                    item.get("domain") or "空",
                    item.get("path") or "/",
                    "HttpOnly" if item.get("httpOnly") else "",
                    "分区" if item.get("partitionKey") else "",
                )
                for item in leaves[:3]
            )
            parts.append(f"原始userhash={len(leaves)}块（{desc}）")
    try:
        parts.append(f"页面JS={summarize_cookies(read_page_document_cookies(session)) or '空'}")
    except Exception as exc:  # noqa: BLE001
        parts.append(f"页面JS=出错（{type(exc).__name__}）")
    try:
        parts.append(f"合并={summarize_cookies(read_site_cookies(session)) or '空'}")
    except Exception as exc:  # noqa: BLE001
        parts.append(f"合并=出错（{type(exc).__name__}）")
    return "｜".join(parts)


def apply_leaf_cookie_over_http(
    cookies: Iterable[dict],
    *,
    client=None,
    timeout: float = 20.0,
    user_agent: str = "",
) -> LeafCookie:
    """拿浏览器里的饼干，走 HTTP 那条路「应用饼干」并读出 userhash（v0.13.22）。

    为什么不再让浏览器自己去跳站点的跳转页（v0.13.21 的做法）：站点给 userhash 的地方是
    **导出页的响应体**（``{"cookie": "…"}``），而浏览器那条路上，导出页会被弹回登录页、
    跳转页会自己原地重载 —— 用户看到的就是「停在『饼干切换成功!』一直跳」（m29953/m29954）。
    HTTP 这条路从 v0.6.1 起就在用（:meth:`XdaoClient.apply_cookie`）：认「跳转提示」页、
    跟着跳、相对地址补前缀、从导出页正文里抠值、读 cookie jar 兜底，全都是现成的。

    ``user_agent``（v0.13.30）：真机 m34394/m34396 上这条路「登录后没能进入用户系统
    （X 岛把请求弹回了登录页）」，而同一时刻用户的浏览器里回帖是成功的 —— 会话活着，
    被弹的是**重放**：饼干罐是 Edge 建立的，嘴却是 urllib 的缺省 UA（Chrome/124），
    站点把两张嘴认成了两个人。界面层把浏览器自报的 UA（:func:`read_user_agent`）传进来，
    这一趟才像在跟自己的会话说话。留空时按缺省 UA 走（老行为）。

    ``client`` 只给用例注入用；给 None 时自己建一个 :class:`XdaoClient`（用缺省网络设置，
    登录窗口本来就不带用户设置）。失败时把服务端/网络层那句话原样放进 ``detail`` ——
    界面层会把它写进「程序试过的几步」，是用户截图里唯一的书面线索。
    """
    from .client import XdaoClient, XdaoError

    session_client = (
        client
        if client is not None
        else XdaoClient(timeout=timeout, user_agent=user_agent or None)
    )
    try:
        session_client.import_cookies(cookies)
        value = session_client.apply_cookie()
    except XdaoError as exc:
        return LeafCookie(None, str(exc))
    except Exception as exc:  # noqa: BLE001 —— 网络层的意外也要变成一句人话
        return LeafCookie(None, f"走 HTTP 领饼干时出错：{exc}")
    return LeafCookie(value, "")


def read_user_agent(session: "CDPSession") -> str:
    """浏览器自报的 User-Agent（v0.13.30），读不到给空串。

    为什么要有它：HTTP 重放那条路（:func:`apply_leaf_cookie_over_http`）在真机上被弹回
    登录页，而同一时刻用户的浏览器里回帖是成功的（m34394/m34396）—— 会话活着，被弹的是
    **重放**：饼干是 Edge 建立的，嘴是 urllib 的缺省 UA。界面层把这里读到的 UA 传进 HTTP
    重放，让它用原主的嗓音说话。读不到就空串 —— 它是辅助，不该拦住领饼干本身。
    """
    try:
        return str(session.evaluate("navigator.userAgent") or "").strip()
    except Exception:  # noqa: BLE001 —— 页面正在换文档之类：没有就算了
        return ""


def build_page_fetch_script(url: str) -> str:
    """生成「在页面里用 fetch GET 一个同源地址」的脚本（v0.13.30）。

    为什么用 fetch：请求由浏览器自己发出 —— UA、头、TLS 指纹、饼干罐（含 HttpOnly/Secure）
    全是原主的，不存在 HTTP 重放那种「换一张嘴说话就被弹回登录页」的问题；而它是后台请求，
    **一个标签页都不碰**（v0.13.21 立的规矩是不动用户的页面，不是不能用浏览器发请求）。
    ``redirect:'follow'`` 让真 302 由浏览器自己跟完；站点的「跳转提示」页是 200 加 JS 倒数，
    得由调用方认正文里的目标再决定下一跳。``credentials:'same-origin'`` 带上整罐饼干。
    结果是一个 JSON 字符串：成功 ``{url, status, text}``；fetch 抛错
    ``{url:'', status:0, text:'', error:"…"}``。
    """
    return (
        "(async () => {"
        "  try {"
        f"    const r = await fetch({json.dumps(url)}, "
        "{credentials: 'same-origin', redirect: 'follow'});"
        "    const t = await r.text();"
        "    return JSON.stringify({url: r.url, status: r.status, text: t});"
        "  } catch (e) {"
        "    return JSON.stringify({url: '', status: 0, text: '', error: String(e)});"
        "  }"
        "})()"
    )


def _fetch_in_page(session: "CDPSession", url: str) -> dict | None:
    """页面内 GET 一趟：成回 ``{url,status,text}``，fetch 报错也回（带 ``error``）；意外回 None。"""
    try:
        raw = session.evaluate(build_page_fetch_script(url), True)
    except Exception:  # noqa: BLE001 —— CSP 拦脚本、换文档之类：这一趟就是「没问动」
        return None
    try:
        data = json.loads(raw)
    except Exception:  # noqa: BLE001 —— 浏览器回了个认不出的形状，按没回话处理
        return None
    if not isinstance(data, dict) or "text" not in data:
        return None
    return data


WATCH_BANNER_ID = "__xdaoWatchBanner"


def build_watch_banner_script(stamp: str = "") -> str:
    """给被盯的窗口钉一条「程序正在看这个窗口」的顶部横条（v0.13.33）。

    为什么：登录这条路历史上一直分不清「用户在操作哪扇窗」—— 程序读的是它挂着
    的那个实例的饼干罐，用户在另一扇长得一模一样的窗口里登录、点「应用」，程序
    这边就永远「取不到饼干」（真机 m35762/m35800）。横幅只出现在**程序正在读的那扇
    窗**里：窗口顶上有条棕色横条 = 对；没有 = 你正站在别的窗口里，别看这里了。

    v0.13.35 两处补强（真机 m36897：横幅在两扇窗里可能同时存在 —— 旧程序退出后
    它注入的横幅会**冻**在那扇幸存的窗里，用户分不清哪扇是活的）：

    * ``stamp`` 非空时文案尾带「【窗口号 XXXX】」，界面话术报同一个号 —— 肉眼一比
      就知道眼前这扇是不是程序此刻在盯的；
    * 已存在的横幅**也刷新文案**（旧横幅带着旧号/旧版话术，不刷就成了假信号）。

    规矩两条：**只在 X 岛站内的页面上出现**（横幅是给登录流程看的，别跑到别的
    网站顶上碍事），**幂等** —— 同一个 id 已经在就不重复钉（登录过程会刷好几页，
    worker 每一轮都注一次，绝不能越叠越厚）。全程吞异常：横幅是辅助说明，
    它出什么问题都不许把登录带崩。
    """
    text = (
        "串导出程序正在看这个窗口 —— 请在这里登录 X 岛；"
        "登录成功站点就自动带上你当前的饼干，程序自己会拿到；"
        "十几秒还没拿到就到「我的饼干」点一行『应用』。"
    )
    stamp = str(stamp or "").strip()
    if stamp:
        text = f"{text}【窗口号 {stamp}】"
    return (
        "(() => {\n"
        f"  const id = {json.dumps(WATCH_BANNER_ID)};\n"
        f"  const text = {json.dumps(text, ensure_ascii=False)};\n"
        "  try {\n"
        "    if (!document.documentElement) return 'skip';\n"
        "    if (location.protocol.indexOf('http') !== 0) return 'skip';\n"
        "    if (location.hostname.indexOf('nmbxd1') < 0) return 'off-site';\n"
        "    const existing = document.getElementById(id);\n"
        "    if (existing) {\n"
        "      if (existing.textContent !== text) existing.textContent = text;\n"
        "      return 'present';\n"
        "    }\n"
        "    const bar = document.createElement('div');\n"
        "    bar.id = id;\n"
        "    bar.textContent = text;\n"
        "    bar.setAttribute('style', 'position:fixed;top:0;left:0;right:0;z-index:2147483647;"
        "background:#b45309;color:#ffffff;font:13px/1.6 sans-serif;padding:6px 12px;"
        "text-align:center;box-shadow:0 2px 8px rgba(0,0,0,0.35);pointer-events:none;');\n"
        "    document.documentElement.appendChild(bar);\n"
        "    return 'added';\n"
        "  } catch (e) { return 'err'; }\n"
        "})()"
    )


def ensure_watch_banner(session: "CDPSession", stamp: str = "") -> str:
    """往被盯的标签注入横幅（带窗口号）；一切失败都咽掉，回一句状态码给测试用。"""
    try:
        return str(session.evaluate(build_watch_banner_script(stamp)))
    except Exception:  # noqa: BLE001 —— 横幅不许把登录带崩
        return ""


def apply_leaf_cookie_in_browser(
    session: "CDPSession", urls: list[str] | None = None
) -> LeafCookie:
    """在浏览器页面里把「列表 → 应用 → 导出」走一遍，全程 fetch、不导航（v0.13.30）。

    为什么加这条路（真机 m34394/m34396）：HTTP 重放被弹回登录页、浏览器里回帖却成功 ——
    差的是客户端长相（UA/头/TLS 指纹），不是会话。页面内 fetch 由浏览器自己发，身份**天然
    全等**，把这些变量一次消掉；它不导航，所以不会重演 v0.13.21 的「把用户拖去站点倒计时页
    原地重载」。协议照 :meth:`XdaoClient.apply_cookie`（v0.6.1 起线上跑通）逐条搬：
    GET「饼干」列表 → 认「跳转提示」页跟跳（相对地址按当前落点补全）→ 正则/``<td>`` 两样
    认 id → GET ``switchTo``（主站就是在这趟的响应里把 userhash 种进罐）→ GET ``export``
    从正文抠值 → 读罐兜底。任何一步没成都带一句人话回来，界面层照旧回落 HTTP 重放。
    """
    from .client import XdaoClient  # 局部导入：避开与 client 的循环依赖

    try:
        current = str(session.current_url() or "")
    except Exception:  # noqa: BLE001 —— 读地址的意外也算「这一趟没问动」
        current = ""
    if not current:
        return LeafCookie(None, "浏览器窗口里现在没有可读的页面，页面内领饼干这条路走不了。")
    host = urllib.parse.urlsplit(current).hostname or ""
    if "nmbxd1" not in host:
        # v0.13.33：地址读得到、可 host 为空（about:blank、data: 这类）以前会**直通** ——
        # fetch 落在非站内的文档里，`credentials:'same-origin'` 一块饼干也不发，
        # 站点必然把它当陌生人弹回登录页，界面就谎报「这个窗口里的登录没成」
        # （真机 m35762 排查时发现的口径漏洞：那句话得留着说真弹回的场景）。
        return LeafCookie(
            None,
            f"程序挂的这个标签现在不在 X 岛页面（{current[:120]}），先等它回到站内再领。",
        )

    index_url = f"{_cookie_action_base()}index.html"
    page = _fetch_in_page(session, index_url)
    if page is None:
        return LeafCookie(None, "在浏览器页面里问「饼干」列表没问动（页面可能正在换地址）。")
    final_url = str(page.get("url") or index_url)
    text = str(page.get("text") or "")
    hops = 0
    while hops < MAX_COOKIE_JUMPS:
        target = XdaoClient.jump_page_url(text)
        if not target:
            break
        nxt = _fetch_in_page(session, urllib.parse.urljoin(final_url, target))
        if nxt is None:
            return LeafCookie(None, "跟着站点的「跳转提示」页走时，页面内 fetch 没问动。")
        page = nxt
        final_url = str(page.get("url") or final_url)
        text = str(page.get("text") or "")
        hops += 1

    ids = re.findall(r"Cookie/(?:switchTo|export)/id/([^/\s\"'<]+)", text)
    if not ids:
        # 兼容只有表格、没有链接的写法：每行第二个单元格是 id（同 apply_cookie）。
        for row in re.findall(r"<tr[^>]*>(.*?)</tr>", text, flags=re.S):
            cells = re.findall(r"<td[^>]*>(.*?)</td>", row, flags=re.S)
            if len(cells) >= 2:
                candidate = re.sub(r"<[^>]+>", "", cells[1]).strip()
                if candidate:
                    ids.append(candidate)
    if not ids:
        if "login" in final_url:
            message = XdaoClient.jump_page_message(text)
            detail = f"X 岛回话：{message}。" if message else "X 岛把页面弹回了登录页。"
            return LeafCookie(
                None,
                f"在浏览器页面里问「饼干」列表也被弹回了登录页（{detail}）—— "
                "这个窗口里的登录没成。请重新登录。",
            )
        message = XdaoClient.jump_page_message(text)
        if message:
            return LeafCookie(None, f"浏览器页面里没能读到「饼干」列表（X 岛回话：{message}）。")
        return LeafCookie(
            None, "浏览器页面里的「饼干」列表没认出可应用的饼干（这个账号可能还没领过一块）。"
        )

    cookie_id = re.sub(r"\.html?$", "", ids[0], flags=re.IGNORECASE)
    # switchTo 的响应正是主站把 userhash 种进罐的那一趟；这一跳没问动也继续往下 ——
    # 值可能已经种上了，导出与读罐各认一遍再说。
    _fetch_in_page(session, f"{_cookie_action_base()}switchTo/id/{cookie_id}.html")
    export = _fetch_in_page(session, f"{_cookie_action_base()}export/id/{cookie_id}.html")
    value = userhash_from_export_text(str(export.get("text") or "")) if export else None
    if not value:
        value = read_userhash_cookie(session, urls)
    if value:
        return LeafCookie(value, "")
    return LeafCookie(None, "浏览器里应用了饼干，可导出页正文和罐里都没看到 userhash。")
