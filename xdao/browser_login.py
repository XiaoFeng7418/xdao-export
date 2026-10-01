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
from typing import Callable, Mapping

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
# 连调试端口时，等站点页面出现的最长时间。
# 浏览器是先写端口文件、再加载命令行给的地址的：端口一出来就连，可能只看到一个空标签
# （真机上还量到过 Edge 自带的 edge://sync-confirmation-dialog/）。连到那种页面上，
# 页面里的 fetch 属于别的源，读饼干会一律读空，所以这里给它几秒把登录页开出来。
_SITE_WAIT = 8.0
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
        self.proxy = proxy
        self.timeout = timeout
        self.start_url = start_url or LOGIN_URL
        #: 现场目录起不来时要不要换备用资料目录 / 换浏览器。登录那条路要（见 :meth:`start`），
        #: 但 ``--check-browser`` 那种「如实回答这一个浏览器行不行」的探测里必须关掉。
        self.fallback_profiles = True
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

    def start(self) -> "LoginBrowser":
        """启动浏览器并等它把调试端口写出来。

        默认用配置目录里的 ``browser-profile``。**这个目录坏了不该让整个登录
        功能不可用**：真机上出现过「[WinError 5] 拒绝访问」——用户点多少次
        「用浏览器登录」都是一句「打开浏览器失败」。所以碰上「权限 / 占用」
        这类目录问题就换一个 profile 再试（见 :func:`fallback_profile_dirs`），
        实在不行才把原始错误抛上去。

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
        # 现场资料目录坏了 / 被拦下时，同一个浏览器换几个干净目录试；
        # 都不行再换下一个浏览器（同样从干净目录开始）。
        # ``fallback_profiles`` 关掉时只试现场目录（供 --check-browser 如实回答）。
        candidates = (
            [self.profile, *fallback_profile_dirs(self.profile)]
            if self.fallback_profiles
            else [self.profile]
        )
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
        if first_error is None:  # pragma: no cover —— browsers 至少有一个，走不到这儿
            raise CdpError(f"启动 {self.info.name} 失败：找不到可用的浏览器资料目录。")
        if dead_on_startup:
            raise CdpError(
                f"{first_error}\n\n"
                "每个浏览器都是刚起来就退出（多半是资料目录里的旧状态坏了，"
                "或者被安全软件拦下）。可以试着关掉安全软件的浏览器防护再点一次，"
                "或者改用「直接粘贴饼干登录」。"
            ) from first_error
        raise first_error

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
        port_file = chosen / "DevToolsActivePort"
        # 上次崩溃可能留下过期端口：留着它会让等待立刻「成功」，然后连到一个死端口。
        try:
            port_file.unlink()
        except OSError:
            pass
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
            raise CdpError(f"启动 {self.info.name} 失败：{exc}") from exc

        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                code = self.process.returncode
                self.stop()
                raise CdpError(
                    f"{self.info.name} 刚起来就退出了（退出码 {code}）。"
                    "资料目录里的旧状态坏掉、或者被安全软件拦下时会这样；"
                    "程序会换一个干净目录、必要时再换一个浏览器重试。"
                )
            if port_file.exists():
                try:
                    text = port_file.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    text = ""
                try:
                    # 文件刚建好时可能只写了一半，读到半截就当还没好，继续等。
                    self.port, self.ws_path = _parse_devtools_file(text)
                except CdpError:
                    self.port, self.ws_path = 0, ""
                if self.port:
                    self.browser_ws_url = f"ws://127.0.0.1:{self.port}{self.ws_path}"
                    self._profile_note = (
                        ""
                        if chosen == self.profile
                        else f"配置目录里的浏览器资料用不了，这次改用了 {chosen}。"
                    )
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
    except CdpError:
        return None
    value = (value or "").strip()
    return value if looks_like_userhash(value) else None
