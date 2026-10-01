"""把新版免安装包下下来、原地换上、重启。

「有没有新版本」那一半在 :mod:`xdao.update_check` 里；这里管另一半：
发现有新版之后，别再让用户自己去下载、解压、对着文件夹手工覆盖 ——
点一下「立即升级」，剩下的交给程序。

几条自我约束：

* **没把握就退回手工**：源码运行、目录里没有 exe、上级目录写不进去、
  下回来的包结构看不懂 —— 这些情况一律不硬上，改成打开下载页让用户自己来。
* **先自检再换、失败能回退**：下载解压后，先把新版本整个跑一遍 ``--selftest``，
  过了才动现场；动手时是把「整个程序目录」改名让位再搬新的进来，一次改名
  要么成功要么什么都没变，不存在「换了一半」的现场。
* **旧版本先留着**：换下来的目录改名成 ``<原名>.old-<时间戳>`` 留在原地，
  万一新版起不来，用户可以自己改回来。下次启动时程序会顺手清掉旧备份。

Windows 上「正在运行的程序不能删、不能改名」是常识，但实测不是这样：
执行映像能被改名（改名后进程照常跑），所以 :func:`spawn_helper` 才有戏。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from . import __version__
from .update_check import REPO, UpdateCheckError, detect_proxy

#: 下载超时（秒）。安装包十几 MB，给宽一点，但也不能挂死。
DOWNLOAD_TIMEOUT = 60.0
#: 一次最多往磁盘写多少（防着下回来个天文数字）。
MAX_DOWNLOAD_BYTES = 200 * 1024 * 1024
#: 升级包目录名（放在系统临时目录下，下完就用完即弃）。
STAGING_DIR_NAME = "xdao-export-update"
#: 帮手自己就住在暂存目录里，Windows 不许删正在运行的 exe —— 所以换完目录后
#: 帮手不试着删，直接给目录改成这个名字留个记号，等新版下次启动来收
#: （见 :func:`_retire_staging` 与 :func:`cleanup_staging_leftovers`）。
LEFTOVER_SUFFIX = ".leftover-"
#: 记号目录至少放多久才允许被清（秒）。记号名字里记着帮手的进程号，那个进程
#: 已经没了就能立刻清；万一拿不到进程号，就按这个时限兜底。
LEFTOVER_MIN_AGE = 30.0
#: 暂存目录最多留多久还删不掉就交给下次启动（秒）。给足帮手启动、解压、换目录的时间。
STAGING_STALE_SECONDS = 600.0
#: 解压出来的目录名必须长这样，免得把别的 zip 当升级包解了。
PAYLOAD_PREFIX = "xdao-export-v"
#: 换下来的旧目录后缀（``<目录名>.old-20261001-091500``）。
BACKUP_SUFFIX = ".old-"
#: 旧备份留多久（秒）之后由下次启动顺手清掉。
BACKUP_KEEP_SECONDS = 24 * 60 * 60
#: 等老进程退出最多等多久（秒）。
WAIT_FOR_EXIT_SECONDS = 60.0

LAUNCHER_NAME = "xdao-export.exe"
SELFTEST_TIMEOUT = 180.0


# ---------------------------------------------------------------- 环境判断


def is_frozen() -> bool:
    """是不是打包版（免安装包）。"""
    return bool(getattr(sys, "frozen", False))


def app_dir() -> Path:
    """程序自己的目录（``xdao-export.exe`` 与 ``_internal`` 都在这里）。"""
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def launcher_path() -> Path:
    return app_dir() / LAUNCHER_NAME


def can_write_dir(path: Path) -> bool:
    """探一下这个目录能不能写（只读介质、系统目录要提前发现）。"""
    probe = path / f".xdao-write-probe-{os.getpid()}"
    try:
        probe.write_text("", encoding="utf-8")
    except OSError:
        return False
    try:
        probe.unlink()
    except OSError:
        pass
    return True


# ---------------------------------------------------------------- 下载解压


def default_staging_root() -> Path:
    import tempfile

    return Path(tempfile.gettempdir()) / STAGING_DIR_NAME


def _download_size(asset_size: int) -> str:
    if asset_size <= 0:
        return "大小未知"
    return f"{asset_size / 1024 / 1024:.1f} MB"


def download_asset(
    url: str,
    target: Path,
    *,
    proxy: str = "",
    timeout: float = DOWNLOAD_TIMEOUT,
    progress: Callable[[int, int], None] | None = None,
    opener_factory: Any = None,
) -> Path:
    """把升级包下到 ``target``。失败抛 :class:`UpdateCheckError`。"""
    if not url:
        raise UpdateCheckError("没有下载地址")
    handlers: list[urllib.request.BaseHandler] = []
    handlers.append(
        urllib.request.ProxyHandler({"http": proxy, "https": proxy})
        if proxy
        else urllib.request.ProxyHandler({})
    )
    build = opener_factory or urllib.request.build_opener
    opener = build(*handlers)
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": f"xdao-export/{__version__} (+https://github.com/{REPO})",
            "Accept": "application/octet-stream",
        },
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        with opener.open(request, timeout=timeout) as response:
            total = int(response.headers.get("Content-Length") or 0)
            written = 0
            with open(target, "wb") as handle:
                while True:
                    chunk = response.read(256 * 1024)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > MAX_DOWNLOAD_BYTES:
                        raise UpdateCheckError("升级包太大了，放弃")
                    handle.write(chunk)
                    if progress is not None:
                        progress(written, total)
    except UpdateCheckError:
        remove_quietly(target)
        raise
    except urllib.error.HTTPError as exc:
        remove_quietly(target)
        raise UpdateCheckError(f"下载失败（HTTP {exc.code}）") from exc
    except urllib.error.URLError as exc:
        remove_quietly(target)
        raise UpdateCheckError(f"下载失败（{exc.reason}）") from exc
    except OSError as exc:
        remove_quietly(target)
        raise UpdateCheckError(f"下载失败（{exc}）") from exc
    return target


def extract_payload(archive: Path, into: Path) -> Path:
    """解压升级包，返回里面那个 ``xdao-export-v*`` 目录。

    只解压这一个顶层目录（升级包本来就只有它一个），顺便挡住
    「路径里带 ``..`` 往外写」这种坏包。
    """
    if into.exists():
        shutil.rmtree(into, ignore_errors=True)
    into.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(archive) as zipped:
            names = zipped.namelist()
            roots = {
                name.replace("\\", "/").split("/")[0]
                for name in names
                if name.replace("\\", "/").split("/")[0]
            }
            payload_names = sorted(n for n in roots if n.startswith(PAYLOAD_PREFIX))
            if len(payload_names) != 1:
                raise UpdateCheckError("升级包里没找到程序目录")
            payload = payload_names[0]
            members = []
            for name in names:
                normalised = name.replace("\\", "/")
                parts = normalised.split("/")
                if not parts or parts[0] != payload:
                    continue
                if any(part in ("..", "") for part in parts):
                    raise UpdateCheckError("升级包里有不安全的路径")
                members.append(name)
            if not members:
                raise UpdateCheckError("升级包是空的")
            for name in members:
                zipped.extract(name, into)
    except zipfile.BadZipFile as exc:
        raise UpdateCheckError("升级包不是有效的 zip") from exc
    except OSError as exc:
        raise UpdateCheckError(f"解压失败（{exc}）") from exc
    return into / payload


def payload_launcher(payload: Path) -> Path:
    return payload / LAUNCHER_NAME


def run_selftest(
    exe: Path,
    timeout: float = SELFTEST_TIMEOUT,
    *,
    prefix: list[str] | None = None,
) -> tuple[bool, str]:
    """先拿候选的新版本跑一遍 ``--selftest --offline``。

    返回 ``(过没过, 结论一行)``。跑不起来也算没过 —— 换上去更糟。

    ``prefix`` 是给用例留的（用 Python 脚本冒充 exe 时要在前面加解释器路径），
    生产路径永远是直接执行那个 exe；注意用脚本冒充时，拿不到 ``--selftest``
    的退出码，因为脚本会把参数当成数据、跑完就退 0。
    """
    if not exe.exists():
        return False, "升级包里没有 xdao-export.exe"
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    command = [*(prefix or []), str(exe), "--selftest", "--offline"]
    try:
        done = subprocess.run(
            command,
            capture_output=True,
            timeout=timeout,
            env=env,
            creationflags=creationflags,
        )
    except subprocess.TimeoutExpired:
        return False, "新版本自检超时"
    except OSError as exc:
        return False, f"新版本跑不起来（{exc}）"
    text = (done.stdout or b"").decode("utf-8", "replace")
    if done.returncode == 0:
        return True, "新版本自检通过"
    tail = [line for line in text.splitlines() if line.strip()]
    detail = tail[-1].strip() if tail else f"退出码 {done.returncode}"
    return False, f"新版本自检没过：{detail}"


# ---------------------------------------------------------------- 升级计划


@dataclass
class UpgradePlan:
    """换上去这件事该怎么做（拿不准就 ``possible`` 为假）。"""

    possible: bool
    reason: str = ""
    target: Path | None = None
    launcher: Path | None = None
    pid: int = 0

    @property
    def backup_dir(self) -> Path:
        if self.target is None:
            return Path()
        stamp = time.strftime("%Y%m%d-%H%M%S")
        return self.target.with_name(f"{self.target.name}{BACKUP_SUFFIX}{stamp}")


def plan_upgrade(
    *,
    target: Path | None = None,
    launcher: Path | None = None,
    frozen: bool | None = None,
    writable: Callable[[Path], bool] = can_write_dir,
) -> UpgradePlan:
    """判断「原地升级」这条路走不走得通。

    走不通的原因要能直接说给用户听，所以 ``reason`` 是中文的。
    """
    frozen_now = is_frozen() if frozen is None else frozen
    here = target or app_dir()
    exe = launcher or (here / LAUNCHER_NAME)
    if not frozen_now:
        return UpgradePlan(False, "源码运行时不能原地升级，请去下载页取免安装包", here, exe)
    if not exe.exists():
        return UpgradePlan(False, f"这个目录里没有 {LAUNCHER_NAME}，只好手工升级", here, exe)
    parent = here.parent
    if not writable(parent):
        return UpgradePlan(False, f"程序所在的上级目录写不进去（{parent}），只好手工升级", here, exe)
    if not writable(here):
        return UpgradePlan(False, f"程序目录写不进去（{here}），只好手工升级", here, exe)
    return UpgradePlan(True, "", here, exe, os.getpid())


# ---------------------------------------------------------------- 动手换


def _pid_running(pid: int) -> bool:
    """这个进程号现在还有主吗。

    **Windows 上 ``os.kill(pid, 0)`` 靠不住**：对已经退出的进程号它一样会
    正常返回（不抛 ``OSError``），于是「帮手还在不在」永远问不出「不在」。
    实测踩过：拿着一个早就退出的进程号做判断，清理逻辑以为帮手还活着，
    暂存目录就一直留着。所以这里在 Windows 上直接用 ``OpenProcess`` +
    ``WaitForSingleObject``：拿得到句柄、而且没进入「已结束」状态，才算活着。
    """
    if pid <= 0:
        return False
    if os.name == "nt":
        return _windows_pid_running(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _windows_pid_running(pid: int) -> bool:
    import ctypes

    SYNCHRONIZE = 0x00100000
    WAIT_TIMEOUT = 0x00000102
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(SYNCHRONIZE, False, ctypes.c_ulong(pid).value)
    if not handle:
        return False
    try:
        return kernel32.WaitForSingleObject(handle, 0) == WAIT_TIMEOUT
    finally:
        kernel32.CloseHandle(handle)


def wait_for_exit(pid: int, timeout: float = WAIT_FOR_EXIT_SECONDS) -> bool:
    """等某个进程退出（超时返回 False）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _pid_running(pid):
            return True
        time.sleep(0.2)
    return not _pid_running(pid)


def swap_in(payload: Path, plan: UpgradePlan) -> Path:
    """把 ``payload`` 搬到现场，返回留下的旧目录（出问题可以自己改回来）。

    先给旧目录改名（一次 O(1) 的改名，要么成、要么现场没动），再把新的搬进来。
    新目录搬不过来时，会把旧名字改回去。
    """
    if plan.target is None or not plan.possible:
        raise UpdateCheckError("这次升级没办法原地进行")
    target = plan.target
    backup = plan.backup_dir
    os.replace(target, backup)
    try:
        shutil.move(str(payload), str(target))
    except OSError:
        if not target.exists() and backup.exists():
            os.replace(backup, target)
        raise
    return backup


def cleanup_backups(
    parent: Path | None = None, *, keep_seconds: float = BACKUP_KEEP_SECONDS
) -> list[Path]:
    """清掉上次升级留下的旧目录（只删够旧的，删不掉就算了）。"""
    here = parent or app_dir()
    removed: list[Path] = []
    try:
        entries = list(here.parent.iterdir())
    except OSError:
        return removed
    now = time.time()
    for entry in entries:
        name = entry.name
        if BACKUP_SUFFIX not in name or not entry.is_dir():
            continue
        try:
            age = now - entry.stat().st_mtime
        except OSError:
            continue
        if age < keep_seconds:
            continue
        try:
            shutil.rmtree(entry)
        except OSError:
            continue
        removed.append(entry)
    return removed


# ---------------------------------------------------------------- 两种模式


def spawn_helper(
    plan: UpgradePlan,
    payload: Path,
    *,
    exe: Path | None = None,
    popen: Callable[..., Any] = subprocess.Popen,
) -> Any:
    """叫醒「另一个自己」去做替换，本进程随后就该退出了。

    帮手用的是**已经解压好的新版本**里那个 exe —— 它跟现场是同一个程序，
    所以不依赖系统里有没有 Python、也不依赖 PowerShell 策略，而且它一跑
    起来就带上了自己的 ``_internal``。
    """
    helper_exe = exe or payload_launcher(payload)
    if not helper_exe.exists():
        raise UpdateCheckError("升级包里没有可用的程序文件")
    if plan.target is None:
        raise UpdateCheckError("不知道要换到哪个目录")
    args = [
        str(helper_exe),
        "--apply-update",
        str(plan.target),
        str(payload),
        str(plan.pid or os.getpid()),
        str(plan.launcher or (plan.target / LAUNCHER_NAME)),
    ]
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    # 帮手的输出要留痕：它是从包里跑起来的窗口程序（没有控制台，print 等于扔了），
    # 万一换不动，用户和我们都得能回头看它说了什么。写不成就算了，别耽误升级。
    try:
        log = open(  # noqa: SIM115 —— 句柄要留给子进程用，不能关
            payload.parent.parent / "xdao-update.log", "a", encoding="utf-8"
        )
    except OSError:
        log = None
    if log is None:
        return popen(args, close_fds=True, creationflags=creationflags)
    return popen(args, close_fds=True, creationflags=creationflags, stdout=log, stderr=log)


def apply_update(
    target: Path,
    payload: Path,
    pid: int,
    launcher: Path,
    *,
    spawn: Callable[..., Any] = subprocess.Popen,
) -> int:
    """帮手模式：等老进程走人 → 换上去 → 启动新版 → 清理。

    返回给命令行用的退出码（0 表示换成功、新版已启动）。
    """
    out = sys.stdout
    if not wait_for_exit(pid):
        print(f"老进程（{pid}）一直没退出，这次不换了。", file=out)
        return 1
    if not payload.exists():
        print("升级包不见了，这次不换了。", file=out)
        return 1
    plan = UpgradePlan(True, "", target, launcher, 0)
    try:
        backup = swap_in(payload, plan)
    except OSError as exc:
        print(f"替换失败（{exc}），现场保持原样。", file=out)
        return 1
    print(f"已换上 {target.name}，旧版本留在 {backup.name}。", file=out)
    try:
        creationflags = getattr(subprocess, "DETACHED_PROCESS", 0)
        spawn(
            [str(launcher)],
            close_fds=True,
            cwd=str(target),
            creationflags=creationflags,
        )
    except OSError as exc:
        print(f"新版换好了，但没启动起来（{exc}），请自己打开 {launcher}。", file=out)
        return 1
    if _retire_staging(_staging_root_of(payload)) is not None:
        print("暂存目录已改名留记号，新版本下次启动会自己清掉。", file=out)
    return 0


def _remove_tree(path: Path, wait: float = 1.2) -> bool:
    """耐心试几遍删目录，等文件句柄松开。

    删不掉不算错 —— 调用方会改名字留记号，让下一次启动来收。

    在 ``wait`` 这段时间里每 0.25 秒试一次。**试到就立刻返回**，所以正常情况
    下不会有任何等待；只有真的删不掉才会耗满。默认 1.2 秒是给「刚下完、刚解压完」
    这类自己人用的，帮手那边要等老进程彻底退出（实测要几秒），会传更大的值。
    """
    deadline = time.monotonic() + max(wait, 0.0)
    while True:
        shutil.rmtree(path, ignore_errors=True)
        if not path.exists():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.25)


def _staging_root_of(payload: Path) -> Path:
    """从新版本目录往上找出它所在的暂存目录。

    **不能写死成 ``payload.parent`` 或 ``payload.parent.parent``**：解压出来的
    布局是 ``<暂存目录>/payload/<包名>``（界面那边把 ``into`` 传成 ``staging/'payload'``），
    但用例里可能只摆一层，包名也可能和目录名不一样。所以按名字往上找：
    谁叫 :data:`STAGING_DIR_NAME` 谁就是暂存目录。找不到就返回 ``payload.parent``，
    让调用方的名字检查去拒绝它。
    """
    for candidate in (payload.parent, *payload.parents):
        if candidate.name == STAGING_DIR_NAME:
            return candidate
    return payload.parent


def _cleanup_staging(payload: Path, wait: float = 1.2) -> None:
    """删掉临时目录里剩下的空壳（删不掉就改名留记号，交给下一次启动）。

    失败路径（用户取消、自检没过、换不动）用它 —— 这时帮手还没住进去，
    一般删得掉。
    """
    root = _staging_root_of(payload)
    if root.name != STAGING_DIR_NAME:
        return
    if _remove_tree(root, wait):
        return
    _retire_staging(root)


def _retire_staging(root: Path) -> Path | None:
    """给暂存目录改个「记号」名，好让下一次启动认出它、清掉它。

    换目录成功之后必须走这一步（见 :func:`apply_update`）：那一刻**我们自己
    正住在里面**（帮手跑的就是升级包里的 exe），Windows 删不掉正在运行的 exe，
    再耐心都是白等。所以干脆不试删，直接改名走人 —— 新版下一次启动时
    那个进程早就没了，一删就掉。
    """
    if root.name != STAGING_DIR_NAME:
        return None
    marker = root.with_name(f"{root.name}{LEFTOVER_SUFFIX}{os.getpid()}-{int(time.time())}")
    try:
        os.replace(root, marker)
    except OSError:
        return None  # 连名字都改不了就随它去，系统迟早会清临时目录
    return marker



def cleanup_staging_leftovers(
    root: Path | None = None, wait: float = 1.2, *, min_age: float | None = None
) -> int:
    """清掉上次升级留下的暂存目录（新版启动后调用）。

    返回清掉几个。两条规矩，都是为了不误伤正在进行的升级：

    - 普通暂存目录（``xdao-export-update``）要放够 :data:`STAGING_STALE_SECONDS`
      才动 —— 下载十几 MB、解压、跑自检都需要时间；
    - 记号目录（``…leftover-<帮手进程号>-<时间>``）名字里就记着帮手是谁，
      那个进程已经没了就能立刻清，只有拿不到进程号时才按
      :data:`LEFTOVER_MIN_AGE` 兜底。

    ``wait`` / ``min_age`` 是给用例留的（默认值就是真机要用的值）。
    """
    parent = (root if root is not None else default_staging_root()).parent
    removed = 0
    try:
        entries = list(parent.iterdir())
    except OSError:
        return 0
    now = time.time()
    for entry in entries:
        name = entry.name
        helper_pid = 0
        if name == STAGING_DIR_NAME:
            limit = STAGING_STALE_SECONDS
        elif name.startswith(f"{STAGING_DIR_NAME}{LEFTOVER_SUFFIX}"):
            limit = LEFTOVER_MIN_AGE
            head = name[len(f"{STAGING_DIR_NAME}{LEFTOVER_SUFFIX}"):].split("-", 1)[0]
            if head.isdigit():
                helper_pid = int(head)
        else:
            continue
        if min_age is not None:
            limit = min_age
        try:
            age = now - entry.stat().st_mtime
        except OSError:
            continue
        if helper_pid and not _pid_running(helper_pid):
            pass  # 帮手已经走了，立刻就能清
        elif age < limit:
            continue
        if _remove_tree(entry, wait):
            removed += 1
    return removed


def cleanup_staging_leftovers_at_startup(
    root: Path | None = None,
    *,
    wait: float = 3.0,
    attempts: int = 5,
    gap: float = 6.0,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """启动后台清理：盯一会儿，等帮手真的退出去了再收。

    为什么不能只试一次：新版刚起来的那几秒，**帮手可能还在**（它要等老进程
    走干净才换目录，换完才轮到我们启动），而它自己就住在那个记号目录里，
    Windows 谁也不许删它。实测第一次就试，十有八九删不掉，用户 `%TEMP%`
    里就会一直躺着几十 MB。所以隔一会儿再试几次，帮手一走立刻收掉。

    返回一共清掉几个。``attempts`` / ``gap`` / ``sleep`` 是给用例留的。
    """
    total = 0
    for attempt in range(max(1, attempts)):
        total += cleanup_staging_leftovers(root, wait)
        if attempt + 1 < attempts:
            sleep(gap)
    return total


def discard_staging(root: Path | None = None) -> None:
    """放弃这次升级：把暂存目录整个丢掉，别在用户 %TEMP% 里留几个 G。

    升级失败、用户取消、自检没过都会走这里 —— 换不成就当没发生过。
    """
    target = root if root is not None else default_staging_root()
    if target.name != STAGING_DIR_NAME:
        return
    shutil.rmtree(target, ignore_errors=True)


def remove_quietly(path: Path) -> None:
    """删个文件，删不掉也不吭声（比如已经被别的东西占住了）。"""
    try:
        path.unlink()
    except OSError:
        pass
