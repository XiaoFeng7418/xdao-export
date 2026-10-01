"""测试公共夹具。

本机运行在受限文件沙箱下：只有「命令启动时已存在」的目录才被授予写入权，
pytest 自己新建的 ``tmp_path`` 会在写入时报「拒绝访问」。
因此测试统一用 :func:`artifacts_dir`，把产物写进工作区内已提交的
``.test-artifacts``，并在用例结束后清理自己那部分。
"""

from __future__ import annotations

import gc
import os
import shutil
import sys
import threading
import uuid
from pathlib import Path

import pytest

ARTIFACTS_ROOT = Path(__file__).resolve().parent.parent / ".test-artifacts"

# 「可以跳过的理由」白名单。除此之外任何跳过都算这条测试没跑到 ——
# 起因是实测过这么一次：全套报 `473 passed, 12 skipped, 0 failed`，看着全绿，
# 其实是 `tk.Tk()` 偶发抛 TclError，模块级夹具一挂就带走一整组界面用例
# （`test_theme.py` / `test_window.py` / `test_gui_browser_login.py`）。
# 最坏情况量到过 45 条无声消失，而报告里一个 failed 都没有。
# 所以这里盯的是「跳过的是不是只有那两条显式开关的真机用例」。
#
# 两条使用约定：
# 1) **跳过必须写 reason=**。白名单是按理由**文本**匹配的，`skipif(sys.platform != "win32")`
#    不写 reason 时理由是空串 → 会被判成「意外跳过」，CI 的 Linux 矩阵会红得莫名其妙。
# 2) 白名单内的整组跳过（无头机器）不算失败，但**一定会打印一行提醒** ——
#    实测过 Tk 在套件中途亚秒级初始化失败（`Can't find a usable tk.tcl …`），
#    每次刚好带走 `test_theme.py` 那 10 条：只跳 3~5 条时最容易被当成「全跑过了」，
#    所以除了「恰好就是那两条真机用例」以外，一律要说一声。
_SKIP_REASONS_ALLOWED = (
    "XDAO_BROWSER_TEST",
    "XDAO_LIVE_NOTIFY",
    "XDAO_PDF_TEST",
    "没有可用的显示环境",  # 无头机器上界面用例只能跳过（CI 的 Linux 矩阵就是这样）
    "只有 Windows",
    "只有 macOS",
    "需要 Windows",
)
# 这个上限只管「真机用例的数量」：`_EXPECTED_SKIPS` 里那几个文件全是显式开关控制的，
# 以后往里加真机用例（比如 PDF 渲染）会让跳过数自然变多，不该因此判成「不干净的运行」。
# 2026-10-01 就因为上限还是 5、而 PDF 真机用例从 0 加到 4 条，全量跑出 6 条跳过被判红。
# 真要防的「整组静默跳过界面用例」由上面那条 all() 兜住 —— 界面用例的 nodeid 不在
# `_EXPECTED_SKIPS` 里，跳过时会走打印 + 退出码 1 那条路。
_SKIP_SAFETY_LIMIT = 20

# 「恰好就是这几条显式开关的真机用例」＝什么都不用说。其余任何跳过都要留下一行记录：
# 少跑 1~5 条时最容易被当成全跑过了（实测 Tk 中途抖动一次带走 test_theme 那 10 条，
# 也见过只带走个别函数级夹具的情况）。
_EXPECTED_SKIPS = ("browser_login", "live_notify", "pdf_render")

# 本次运行里跳过的用例（nodeid, 理由），由下面的 hook 收集。
_skips: list[tuple[str, str]] = []


def pytest_report_teststatus(report, config):
    """收集跳过项。写成 hook 是因为它比 `-rs` 的文本解析稳。"""
    if report.when == "setup" and report.skipped:
        reason = ""
        if isinstance(report.longrepr, tuple) and len(report.longrepr) == 3:
            reason = str(report.longrepr[2])
        _skips.append((report.nodeid, reason))
    return None


def pytest_sessionfinish(session, exitstatus):  # noqa: ARG001 —— pytest 的签名就是这样
    """收尾时核对：跳过清单只允许出现白名单里的理由。

    两条规矩：
    - **白名单外的跳过**：直接报错（pytest 的 UsageError 会退出码 4），不能只打印；
    - 白名单内但**不是那几条显式开关的真机用例**（无头机器整组跳过界面用例）：打印一句，
      并把退出码改成 1 —— 2026-10-01 实测过一次：界面用例因显示环境抖了一下整组跳过
      （上一秒还全绿），报告仍是「0 failed」；只打印的话 CI 依旧一片绿，没人会看见。
    """
    if not _skips:
        return
    unexpected = [
        (node, reason)
        for node, reason in _skips
        if not any(token.lower() in reason.lower() for token in _SKIP_REASONS_ALLOWED)
    ]
    if unexpected:
        lines = "\n".join(f"  - {node}：{reason}" for node, reason in unexpected)
        raise pytest.UsageError(
            "有测试被意外跳过——这类跳过会让「0 failed」名不副实，请先查清原因：\n" + lines
        )
    if len(_skips) <= _SKIP_SAFETY_LIMIT and all(
        any(token in node for token in _EXPECTED_SKIPS) for node, _ in _skips
    ):
        return  # 就是那两条真机用例，本来就不该跑
    # 其余情况（界面用例整组跳过、或者只跳了少数几条）一律说一声，并且让这次运行算失败。
    # 例外：CI 的 Linux 矩阵本来就跑不了界面用例（没有 $DISPLAY），由工作流显式声明
    # ``XDAO_HEADLESS=1``；那种「按预期跳过」不算不干净，但话还是要说。
    print(
        f"\n[conftest] 本次跳过了 {len(_skips)} 条（界面/真机用例），未计入通过数。"
    )
    if os.environ.get("XDAO_HEADLESS") == "1":
        return
    # 把「跳了哪几条」一并列出来：2026-10-01 真的抖过一次（界面用例整组没开起来），
    # 当时只有一句「不是一次干净的运行」，看不出是哪些用例、也就没法判断原因。
    # 用例不多时直接列 nodeid，太多就只列前几条（全列会把日志刷满）。
    listed = "\n".join(f"  - {node}" for node, _ in _skips[:8])
    more = f"\n  …另有 {len(_skips) - 8} 条" if len(_skips) > 8 else ""
    print(f"[conftest] 被跳过的用例：\n{listed}{more}")
    print("[conftest] 这不是一次干净的运行（只有声明了 XDAO_HEADLESS=1 的无头环境才允许），请重跑。")
    session.exitstatus = 1


def _real_user_config_path() -> Path | None:
    """用户真实配置文件的位置；算不出来时返回 None。

    **必须照抄 ``xdao.settings.app_config_dir()`` 的规则**（有 ``APPDATA`` 用它，
    否则回到 ``~/.xdao-export``）：2026-10-01 的 CI 上 Linux 两个矩阵红了三条用例，
    原因就是这个函数原先「没有 ``APPDATA`` 就返回 None」—— 在 Linux 上 APPDATA
    本来就不存在，于是守卫整个没装，``test_config_isolation.py`` 那三条等着报错的
    用例自然 `DID NOT RAISE`（**本地 Windows 永远复现不出来**）。
    走库自己的函数而不是手写一遍，也顺手保证两边不会各自漂移。

    故意在**每次调用时**重新算，而不是复用 ``xdao.settings`` 里那个模块级路径：
    这样就算用例临时改了 ``APPDATA``，这里说的仍然是本机真实路径。
    """
    try:
        from xdao.settings import app_config_dir

        return (app_config_dir() / "config.json").resolve()
    except OSError:  # pragma: no cover - 路径不可解析时宁可不拦
        return None


@pytest.fixture(autouse=True)
def _forbid_writing_the_real_user_config():
    """任何用例都不许读写用户自己的 ``%APPDATA%\\xdao-export\\config.json``。

    为什么要有这条守卫：2026-10-01 真出过一次事故 —— 新增的用例没隔离配置路径
    （``monkeypatch`` 改 ``_default_config_path`` 没用，``_path`` 的 default_factory
    在类创建时就绑定了），测试于是直接读写用户的真实配置：把里头的登录饼干
    （``userhash``）连同偏好一起改成了测试值，用户那份再也拿不回来。
    「测试只写工作区内的 ``.test-artifacts``」这条约定，光靠自觉是不够的。

    需要配置的用例请用 ``AppSettings(_path=<临时目录>/config.json)``，
    或像 ``tests/test_settings.py`` 那样派生出 ``_path`` 指向临时目录的子类。

    拦的就是**写入与读取**：``AppSettings(_path=真实路径).save()`` 与
    ``AppSettings.load()`` 两条路都会在动文件之前报错。构造本身不拦 ——
    「默认路径指向用户配置」是要被测的行为（``tests/test_settings.py`` 里就有一条
    断言它），把构造拦死只会让正常用例没法写，却挡不住任何真实写入。

    **守卫本身在模块导入时就装好、之后不再摘**（``_install_config_guard()``，见下），
    这个夹具只是把同一件事做成 autouse 以保证「任何用例都在守卫之下」的语义清晰。
    """
    _install_config_guard()
    yield


def _install_config_guard() -> None:
    """把守卫装到 ``AppSettings.load`` / ``AppSettings.save`` 上。

    为什么用模块级安装而不是在夹具里装、夹具里拆（2026-10-01 踩过，别改回去）：

    * 只装不拆，就不再受夹具作用域顺序影响 —— ``tests/test_window.py`` 有个 module
      作用域的 autouse 夹具，它在函数作用域夹具之前建立、之后才拆，早先版本会在
      每次 teardown 时把它的假 ``load`` 覆盖掉，24 条界面用例全红、守卫自己还悄悄失效。
    * 更关键的是：``monkeypatch`` 也会在夹具 teardown 阶段还原类属性，等它把
      ``AppSettings.load`` 拨回真实现时，函数作用域的守卫早就拆完了 —— 那种写法下
      ``AppSettings.load()`` 会**真的去读用户的配置文件**，守卫形同虚设。
    * 幂等：已经在位就直接返回，重复调用不会把守卫套成两层。

    别指望「patch 掉 ``_default_config_path`` 名字就等于隔离了」：dataclass 在
    **类创建时**就把 factory 绑进字段默认值了，事后改模块名字对 ``AppSettings._path``
    完全无效 —— 2026-10-01 的事故就是这么来的。
    """
    from xdao import settings as settings_module

    real = _real_user_config_path()
    if real is None:
        return

    current_load = settings_module.AppSettings.load.__func__
    if getattr(current_load, "_xdao_config_guard", False):
        return  # 已经在位
    if current_load.__name__ != "load":
        # 别人（某个文件的隔离夹具）已经把 load 换成读临时目录的实现了，别去顶掉它：
        # 它自己就是隔离措施，而且它的替身会写临时配置，不需要我们拦。
        # 这里不能自作聪明地「再包一层」—— 包的时机在人家换之后，拆的时机却在人家之前，
        # 一包一拆就把人家的替身永久留在了类上（2026-10-01 实测：24 条界面用例全红）。
        return

    def _is_real(path) -> bool:
        try:
            return Path(path).resolve() == real
        except OSError:  # pragma: no cover
            return False

    def guarded_load(cls):
        settings = cls()
        if _is_real(settings._path):
            raise AssertionError(
                "测试要读用户真实配置了，这是被禁止的：\n"
                f"  {settings._path}\n"
                "请把配置落到临时目录：AppSettings(_path=...)，"
                "或像 tests/test_settings.py 那样派生出 _path 指向临时目录的子类。"
            )
        return current_load(cls)

    guarded_load._xdao_config_guard = True  # type: ignore[attr-defined]

    real_save = settings_module.AppSettings.save

    def guarded_save(self) -> None:
        if _is_real(self._path):
            raise AssertionError(
                "测试要写用户真实配置了，这是被禁止的：\n"
                f"  {self._path}\n"
                "上一次这么干直接毁掉了用户的登录状态。请把配置落到临时目录。"
            )
        return real_save(self)

    guarded_save._xdao_config_guard = True  # type: ignore[attr-defined]

    settings_module.AppSettings.load = classmethod(guarded_load)
    settings_module.AppSettings.save = guarded_save


@pytest.fixture
def artifacts_dir() -> Path:
    """给单个用例一个干净的产物目录（用例结束后删除）。

    与 ``tmp_path`` 用法一致：``def test_x(artifacts_dir):`` 后直接当目录用。
    """
    path = ARTIFACTS_ROOT / f"case-{uuid.uuid4().hex[:12]}"
    path.mkdir(parents=True, exist_ok=True)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def _tk_module():
    """延迟导入 tkinter：无头机器上它可能根本起不来，不能拖累纯逻辑用例。"""
    import tkinter

    return tkinter


def _tk_objects() -> list:
    """把还活着的 Tk 部件/变量都捞出来（要自己先 gc 一次）。"""
    tkinter = _tk_module()
    found = []
    for obj in gc.get_objects():
        try:
            if isinstance(obj, (tkinter.Misc, tkinter.Variable)):
                found.append(obj)
        except Exception:  # pragma: no cover - 取属性本身炸掉的怪对象
            continue
    return found


def _destroy_leftover_tk(limit: int = 5) -> int:
    """退出前把界面用例留下的 Tk 部件就地销毁，返回销毁了几个。

    为什么非要在这里动手：部件是**在主线程还活着的时候就销毁**，``destroy()``
    里的 Tcl 调用是正常的；等解释器收尾时才轮到它们的 ``__del__``，那时主线程
    已经不在、Tcl 解释器也拆了，``__del__`` 必然抛异常。
    """
    tkinter = _tk_module()
    destroyed = 0
    for _ in range(limit):
        gc.collect()
        leftovers = _tk_objects()
        if not leftovers:
            break
        for obj in leftovers:
            try:
                obj.destroy()
                destroyed += 1
            except Exception:  # pragma: no cover - 已经销毁过的再 destroy 会报错
                pass
    # 光销毁还不够：tkinter 把自己的默认根窗口记在模块全局里，那个引用会一直
    # 攥着根窗口（连带一串 Variable）不放，谁也 GC 不走 —— 必须自己松手。
    tkinter._default_root = None
    tkinter._default_root_set = None
    gc.collect()
    return destroyed


def _silence_tk_variables() -> None:
    """给 ``tkinter.Variable.__del__`` 换一个不碰 Tcl 的实现。

    ``destroy()`` 之后还会剩几个变量（销毁根窗口时它们被别的对象引用着，GC 收
    不走），解释器收尾时它们的 ``__del__`` 仍会去调 ``info exists``；此时 Tcl
    解释器已经拆了，调用抛 ``RuntimeError``，异常文本又要往 stderr 写 —— 正是
    把 CI 弄红的那条路径。这个兜底只是让收尾阶段安静，不影响任何用例。
    """

    def _quiet_del(self) -> None:  # pragma: no cover - 只在退出时可能被调到
        return None

    try:
        _tk_module().Variable.__del__ = _quiet_del
    except Exception:  # pragma: no cover - 这机器上没有 tkinter
        pass


@pytest.fixture(autouse=True)
def _collect_garbage_on_the_main_thread():
    """每个用例跑完，在主线程上收一次垃圾。

    2026-10-01 本机实测（能稳定复现，值得写下来）：`tests/test_window.py` 会真开
    一个根窗口造一大堆部件，模块跑完虽然 `root.destroy()` 了，那些部件的引用环还
    躺在堆里。接着跑到 `tests/test_gui_entry.py::test_upgrade_worker_runs_the_whole_five_steps`
    时，主线程阻塞在 `decision.get()` 上等回复，**是后台升级线程的那次分配触发了
    自动 GC** —— 于是 Tk 部件在非主线程上被析构、去调 Tcl，进程当场被 Windows
    打断：

        Windows fatal exception: code 0x80000003
        Current thread …: Garbage-collecting
        File "…\\Lib\\pathlib.py", line 366 in __init__

    崩点每次都不一样（`pathlib`/`ntpath`/`threading`/`queue` 都出现过），因为
    「哪次分配触发 GC」是随机的；但「`test_window.py` + `test_gui_entry.py` 连跑
    必崩、各自单跑都不崩」是可复现的。在**主线程**上把这些环收掉，后台线程就再也
    碰不到它们了 —— 这也正是单文件跑不崩的原因：那里的 GC 都落在主线程上。
    """
    yield
    gc.collect()


@pytest.fixture(scope="session", autouse=True)
def _make_tk_variables_safe_to_collect():
    """整套测试一开始就把 ``Variable.__del__`` 换成不碰 Tcl 的实现。

    这是给收尾兜底的：界面用例真开过 Tk 之后，总会有几个 ``StringVar``/
    ``BooleanVar`` 到解释器收尾时才被 GC 到，那时 Tcl 解释器已经拆了，``__del__``
    必抛 ``RuntimeError``，异常文本再往 stderr 写就撞上后台线程占着输出锁 ——
    正是 CI run 36817861762 那条 `Fatal Python error: _enter_buffered_busy`。
    换掉 ``__del__`` 之后这条路根本不存在了（变量不再向 Tcl 打招呼；根窗口本来
    就随用例销毁），对任何用例的行为都没有影响。

    注意它**只管收尾**：本机那个「后台线程里触发 GC 就硬崩」的问题是另一个
    成因，由 `_collect_garbage_on_the_main_thread` 解决（实测单靠这个兜底仍会崩）。
    """
    _silence_tk_variables()
    yield


@pytest.fixture(scope="session", autouse=True)
def _settle_threads_before_interpreter_shutdown():
    """整套跑完先收拾干净再让解释器退出。

    2026-10-01 实测两次（CI run 36817861762 与一次本机全量）：全套跑完、汇总行
    都打出来了，退出码却是 1，报

        Exception ignored in: <function Variable.__del__ …>
        File "…\\tkinter\\__init__.py", line 414, in __del__
        RuntimeError: main thread is not in main loop
        Fatal Python error: _enter_buffered_busy: could not acquire lock for
        <_io.BufferedWriter name='<stderr>'> at interpreter shutdown,
        possibly due to daemon threads
        ##[error]Process completed with exit code 1

    —— 界面用例真开过 Tk（``App`` 会造一大把 ``StringVar``/``BooleanVar``），
    退出时那些变量才被 GC 到，而这时主线程已经不在了，于是 ``__del__`` 抛异常、
    异常又要往 stderr 写，正好撞上后台线程占着输出锁：**测试全绿，CI 却红**，
    而且只在 windows 矩阵上偶发（ubuntu 上 Tk 用例整组跳过）。

    原来这里只是 ``gc.collect()`` + 有界 ``join``，**拦不住**（CI 又红了一次）。
    现在改成三步：①主线程还在、解释器还完好的时候就把遗留 Tk 部件就地销毁；
    ②等后台线程；③把两个流冲干净，再给 ``Variable.__del__`` 套一层不碰 Tcl 的
    兜底 —— 这样解释器收尾时既没有 Tcl 调用，也没有 stderr 竞争。
    """
    yield
    destroyed = _destroy_leftover_tk()

    for _ in range(3):
        alive = [
            thread
            for thread in threading.enumerate()
            if thread is not threading.main_thread()
        ]
        if not alive:
            break
        for thread in alive:
            thread.join(timeout=1.0)
        gc.collect()
    gc.collect()

    _silence_tk_variables()
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except Exception:  # pragma: no cover - 流已经关了就算了
            pass
    if destroyed:
        print(f"[conftest] 退出前收掉 {destroyed} 个遗留 Tk 部件。", file=sys.stderr)
