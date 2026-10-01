"""主窗口布局的回归测试（真窗口，无显示环境时自动跳过）。

这些用例的价值在于：左栏宽度、左右栏比例、底部按钮区不被窗口下沿吃掉，
都是靠肉眼截图才发现过的真问题（曾经右栏被挤成 39px、进度条被压掉一截），
只靠"能 import"是看不出来的。
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import time
import tkinter as tk
from pathlib import Path
from tkinter import ttk

import pytest

from xdao import gui, theme
from xdao.settings import AppSettings

from .conftest import ARTIFACTS_ROOT


def make_root(timeout: float = 5.0) -> tk.Tk:
    """建一个根窗口；失败时重试几秒再放弃。

    本机（DSH 沙箱 + 1920x1080 桌面）实测连开/关十几个 Tk 根窗口时，
    偶发一两次 ``TclError``；这是环境抖动，不是代码问题，重试就能过。
    真的没有显示环境（无头 CI）时重试也救不回来，交给调用方 skip。
    """
    deadline = time.monotonic() + timeout
    last: Exception | None = None
    while True:
        try:
            return tk.Tk()
        except tk.TclError as exc:  # pragma: no cover - 取决于运行环境
            last = exc
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.2)


@pytest.fixture(scope="module", autouse=True)
def isolated_settings():
    """把 ``AppSettings.load()`` 换成读隔离配置，绝不碰用户真实的 ``%APPDATA%``。"""
    try:
        probe = make_root()
    except tk.TclError as exc:  # pragma: no cover - 取决于运行环境
        pytest.skip(f"没有可用的显示环境：{exc}")
    probe.destroy()

    base = ARTIFACTS_ROOT / "window-layout"
    if base.exists():
        shutil.rmtree(base, ignore_errors=True)
    out_dir = base / "out"
    cache_dir = base / "cache"
    out_dir.mkdir(parents=True, exist_ok=True)
    cache_dir.mkdir(parents=True, exist_ok=True)

    def fake_load(cls):  # noqa: ANN001 - 模拟 classmethod
        settings = cls(_path=base / "config.json")
        settings.output_dir = str(out_dir)
        settings.cache_dir = str(cache_dir)
        settings.use_cache = True
        settings.notify = False
        settings.watch_targets = []
        settings.userhash = None
        return settings

    original_load = AppSettings.load
    AppSettings.load = classmethod(fake_load)
    # 配置落点是临时目录（上面传了 _path），`persist_prefs()` 里的 `save()` 写的是
    # 自己家，不必再换成空壳；`tests/conftest.py` 的守卫只拦「落点是用户真实配置」
    # 的写入，正好不会误伤这条路径。
    try:
        yield
    finally:
        AppSettings.load = original_load
        shutil.rmtree(base, ignore_errors=True)


@pytest.fixture(scope="module")
def root_window():
    """整组用例共用一个根窗口。

    为什么不复用而不是每个用例新建：本机实测"连开/关十几个 Tk 根窗口"时，
    Windows 偶发创建失败（表现为整组用例被 skip），而窗口重建本身很贵。
    这里改成只建一次、用例之间清空子控件 —— 用例只看布局，不需要干净的事件循环。
    """
    try:
        root = make_root()
    except tk.TclError as exc:  # pragma: no cover - 取决于运行环境
        pytest.skip(f"没有可用的显示环境：{exc}")
    yield root
    root.destroy()


@pytest.fixture
def app(root_window: tk.Tk) -> gui.App:
    """每个用例在同一个根窗口上重建一次界面。

    必须真的显示（不能 withdraw）：窗口隐藏时 Tk 不算几何，左右栏宽度、
    ``winfo_ismapped()`` 全是 0，测出来没意义。所以窗口挪到屏幕外，
    并且不进事件循环，只在用例里 ``update()``。
    """
    for child in root_window.winfo_children():
        child.destroy()
    root_window.geometry("1060x760+3000+3000")
    instance = gui.App(root_window)
    for _ in range(3):
        root_window.update_idletasks()
        root_window.update()
    return instance


def _grid_child(parent: tk.Misc, row: int, column: int) -> tk.Misc:
    for child in parent.grid_slaves(row=row, column=column):
        return child
    raise AssertionError(f"row={row} column={column} 没有子控件")


@pytest.fixture
def columns(app: gui.App) -> tuple[tk.Misc, tk.Misc]:
    outer = app.root.winfo_children()[0]
    return _grid_child(outer, 1, 0), _grid_child(outer, 1, 1)


def wait_visible(widget: tk.Misc, width: int = 1, timeout: float = 3.0) -> bool:
    """等控件真的被映射出来（宽度够）。

    窗口是挪到屏幕外的，Windows 偶尔要过一拍才把它映射上，``winfo_width()``
    会先报 0。硬断言宽度的用例在那种时候会莫名其妙地挂，所以这里给它一点
    时间（并顺手 ``update()`` 推进事件），超时再交给断言去失败。
    """
    deadline = time.monotonic() + timeout
    while True:
        widget.update_idletasks()
        widget.update()
        if widget.winfo_ismapped() and widget.winfo_width() >= width:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)


# ---------------------------------------------------------------- 窗口基本形态


def test_window_title_and_geometry(app: gui.App) -> None:
    assert app.root.title() == "X岛串导出"
    # 实际尺寸是"请求值被系统夹到屏幕内"的结果（CI 的 Windows runner 只有
    # 1024x768，1060 会被夹成 1028），所以这里只断言不小于最小尺寸。
    default = [int(part) for part in app.root.geometry().split("+")[0].split("x")]
    assert default[0] >= 940 and default[1] >= 680
    assert app.root.minsize() == (940, 680)


def test_window_uses_the_theme_background(app: gui.App) -> None:
    assert app.root.cget("bg").lower() == theme.PALETTE.bg.lower()


# ---------------------------------------------------------------- 左右两栏


def test_left_column_keeps_its_fixed_width(columns) -> None:  # noqa: ANN001
    left, right = columns
    # 左栏是"固定宽度"的：宽度由 grid 的 minsize / frame width 决定，
    # 不能用 winfo_reqwidth 判断（关掉 pack_propagate 后它报的是画布默认宽度）。
    assert abs(left.winfo_width() - theme.SETTINGS_COLUMN_WIDTH) <= 10
    # 500 是 1060 宽窗口下的值；CI 的 Windows runner 只有 1024x768，
    # 窗口被夹到 1028，右栏约 540 —— 取 480 兼容两种环境（历史上曾只剩 39px）。
    assert right.winfo_width() >= 480, "右栏被左栏挤扁了（历史上曾只剩 39px）"


def test_right_column_gets_all_the_spare_width(columns) -> None:  # noqa: ANN001
    left, right = columns
    assert right.winfo_width() > left.winfo_width()
    assert right.winfo_width() >= 480


def test_columns_do_not_overlap(columns) -> None:  # noqa: ANN001
    left, right = columns
    assert left.winfo_x() + left.winfo_width() <= right.winfo_x() + 1


def test_scroll_area_does_not_show_a_scrollbar_when_it_fits(app: gui.App) -> None:
    assert app._scroll_needed is False  # noqa: SLF001


def test_layout_fits_at_the_minimum_window_size(app: gui.App) -> None:
    """最小尺寸（1366x768 笔记本也要能用）下按钮区仍然完整可见。"""
    app.root.geometry("940x680")
    for _ in range(3):
        app.root.update_idletasks()
        app.root.update()
    bar = app.progress_bar
    bottom = bar.winfo_rooty() - app.root.winfo_rooty() + bar.winfo_height()
    assert bar.winfo_ismapped()
    assert bottom <= app.root.winfo_height(), (
        f"最小尺寸下进度条底边 {bottom} 超出了窗口高度 {app.root.winfo_height()}"
    )
    # 装不下时应该出现滚动条（而不是把内容裁掉）
    assert app._scroll_needed is False  # noqa: SLF001


# ---------------------------------------------------------------- 底部按钮区


def test_start_button_and_progress_are_inside_the_window(app: gui.App) -> None:
    """底部整块（按钮 + 进度）必须完整落在客户区里，不能被窗口下沿切掉。"""
    outer = app.root.winfo_children()[0]
    footer = outer.grid_slaves(row=1, column=0)[0].winfo_children()[-1]
    bottom = footer.winfo_rooty() - app.root.winfo_rooty() + footer.winfo_height()
    assert bottom <= app.root.winfo_height(), (
        f"按钮区底边 {bottom} 超过了窗口高度 {app.root.winfo_height()}"
    )


def test_progress_bar_is_visible(app: gui.App) -> None:
    assert app.progress_bar.winfo_ismapped()
    assert app.progress_bar.winfo_width() > 100
    assert app.progress_bar.winfo_height() <= theme.gap(2)


def test_start_button_spans_the_column(app: gui.App) -> None:
    assert wait_visible(app.start_button), "开始按钮一直没被映射出来"
    assert app.start_button.winfo_width() >= 250


# ---------------------------------------------------------------- 日志区


def _widget_font(widget: tk.Misc) -> tuple[str, int]:
    """读控件的真实字体族名与字号。

    ``cget("font")`` 给回来的是 Tcl 列表字符串：族名带空格时会加花括号，
    例如 ``'{Cascadia Mono} 9'``。直接 ``split()`` 会把族名截成 ``'{Cascadia'``,
    必须按 Tcl 列表解析。
    """
    parts = widget.tk.splitlist(str(widget.cget("font")))
    return str(parts[0]), int(parts[1])


def test_log_text_is_read_only_and_monospace(app: gui.App) -> None:
    assert str(app.log_text.cget("state")) == "disabled"
    # 族名不钉死某一个（本机是 Consolas、CI 上可能是别的），但必须跟主题
    # 此刻认定的等宽字体一致：曾经导入期与有窗口期各探一次，绑出两个族名，
    # 在 windows-latest 上表现成 assert 'Consolas' == 'Cascadia Mono'。
    family, size = _widget_font(app.log_text)
    assert family == theme.FONT_MONO, (
        f"日志字体 {family!r} 与主题认定的 {theme.FONT_MONO!r} 不一致"
    )
    assert family in set(theme.MONO_FONT_CANDIDATES) | {theme.FALLBACK_MONO_FONT}
    assert len(family) <= 16, f"字体名 {family!r} 不像是真的"
    assert size == theme.SIZE_SMALL
    assert wait_visible(app.log_text, width=400), "日志框宽度一直不到 400"
    assert app.log_text.winfo_width() > 400


#: 子进程里 Tk 起不来时报的话。CI 的 windows 作业实测抖过一次：
#: `_tkinter.TclError: Can't find a usable init.tcl in the following directories:
#: C:/hostedtoolcache/windows/Python/3.12.10/x64/tcl/tcl8.6/init.tcl` —— 同 sha 的另一次
#: 运行是全绿的，属于环境抖动，不该把整个作业判红。
_TK_UNAVAILABLE_MARKERS = ("Can't find a usable", "no display name", "no $DISPLAY")


def _tk_broke_in_child(stderr: str) -> bool:
    """子进程失败是不是「Tk 起不来」（而不是我们真测出了回归）。"""
    return any(marker in stderr for marker in _TK_UNAVAILABLE_MARKERS)


def _run_in_fresh_process(script: str) -> subprocess.CompletedProcess[str]:
    """在**全新解释器**里跑一段脚本，跑之前先确认这个解释器里 Tk 能用。

    为什么要先确认：这段脚本的失败会以「子进程退出码非 0」的形式报出来，而
    「Tk 起不来」和「我们真发现了回归」看起来一模一样。所以先探一次（跟
    :func:`make_root` 一样重试几秒），探不成才认输 —— 那种情况下这条用例**没法得到
    答案**，如实跳过胜过把环境抖动报成回归。真出现了回归（Tk 好、断言没过）时
    子进程会正常退出，随后的断言照样会红。
    """
    probe = (
        "import tkinter as tk;"
        "root = tk.Tk();root.withdraw();root.destroy();print('tk ok')"
    )
    ready = False
    last: subprocess.CompletedProcess[str] | None = None
    for _ in range(3):
        last = subprocess.run(
            [sys.executable, "-X", "utf8", "-c", probe],
            capture_output=True,
            text=True,
            timeout=120,
            cwd=str(Path(__file__).resolve().parents[1]),
        )
        if last.returncode == 0:
            ready = True
            break
        if not _tk_broke_in_child(last.stderr):
            break
        time.sleep(0.5)
    if not ready:
        detail = (last.stderr if last else "").strip()[-300:]
        if last is not None and _tk_broke_in_child(last.stderr):
            pytest.skip(f"没有可用的显示环境（子进程里 Tk 起不来）：{detail}")
        raise AssertionError(f"子进程里的 Tk 探测以非预期方式失败：{detail}")

    return subprocess.run(
        [sys.executable, "-X", "utf8", "-c", script],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=str(Path(__file__).resolve().parents[1]),
    )


def test_widget_fonts_match_the_theme_after_startup() -> None:
    """界面控件的字体族必须与 ``theme`` 报的一致 —— 在**全新进程**里验证。

    为什么另起进程：``xdao.gui`` 导入期就算好了模块级字体别名，而那时还没有
    根窗口，``theme.resolve_fonts`` 只能给出候选里的第一个、且（这一版起）
    不落缓存。CI 上曾因此出现"控件用 Consolas、主题报 Cascadia Mono"。
    在同进程里测是不可靠的：别的用例可能早就替我们把字体探测做了。子进程
    则是干净的导入顺序 —— 跟 `python main.py` 一模一样。
    """
    script = (
        "import tkinter as tk;"
        "from xdao import gui, theme;"
        "import_at_import = gui.MONO_FONT[0];"
        "root = tk.Tk();root.geometry('900x700+3000+3000');"
        "app = gui.App(root);"
        "root.update_idletasks();root.update();"
        "log = str(app.log_text.cget('font'));"
        "print('|'.join([import_at_import, theme.FONT_MONO, gui.MONO_FONT[0], log]));"
        "root.destroy()"
    )
    proc = _run_in_fresh_process(script)
    assert proc.returncode == 0, f"子进程启动失败：{proc.stderr[-800:]}"
    at_import, theme_mono, module_mono, log_font = proc.stdout.strip().split("|")
    assert module_mono == theme_mono, (
        f"模块级 MONO_FONT={module_mono!r} 与 theme.FONT_MONO={theme_mono!r} 分叉"
    )
    # 子进程里拿不到 Tcl 解释器，用 tk 的列表解析器读那段字体串
    tk_family = tk.Tcl().splitlist(log_font)[0]
    assert tk_family == theme_mono, (
        f"日志控件字体 {log_font!r} 与主题 {theme_mono!r} 不一致"
    )
    # 导入期没有窗口，给的是候选里的第一个 —— 允许与运行期不同，
    # 但不许被"粘住"（上面两条断言就是在查这件事）。
    assert at_import in set(theme.MONO_FONT_CANDIDATES)


def test_module_font_aliases_follow_the_theme(app: gui.App) -> None:
    """``setup_style`` 之后，``gui`` 的模块级字体别名必须已经重算过。"""
    assert gui.MONO_FONT[0] == theme.FONT_MONO
    assert gui.BODY_FONT[0] == theme.FONT_UI
    assert gui.SMALL_FONT[0] == theme.FONT_UI
    assert gui.SECTION_FONT[0] == theme.FONT_UI
    assert _widget_font(app.log_text)[0] == theme.FONT_MONO


def test_log_widget_is_wired_to_the_log_method(app: gui.App) -> None:
    app.log("测试用的一行日志")
    app.root.update_idletasks()
    content = app.log_text.get("1.0", "end")
    assert "测试用的一行日志" in content


# ---------------------------------------------------------------- EPUB 图片行


def test_epub_image_row_follows_the_format(app: gui.App) -> None:
    app.format_box.current(app._format_keys.index("html"))  # noqa: SLF001
    app._sync_image_mode()  # noqa: SLF001
    app.root.update_idletasks()
    assert not app.image_mode_frame.winfo_ismapped()

    app.format_box.current(app._format_keys.index("epub"))  # noqa: SLF001
    app._sync_image_mode()  # noqa: SLF001
    app.root.update_idletasks()
    assert app.image_mode_frame.winfo_ismapped()
    assert app.current_format() == "epub"

    app.format_box.current(app._format_keys.index("html"))  # noqa: SLF001
    app._sync_image_mode()  # noqa: SLF001
    app.root.update_idletasks()
    assert not app.image_mode_frame.winfo_ismapped()


# ------------------------------------------------- 导出目录写不进去时的自动换地方


def _stub_export_thread(monkeypatch) -> None:
    """别让 start() 真的开线程跑导出：只验它进门前那几步。"""
    monkeypatch.setattr(
        gui.threading, "Thread", lambda *a, **k: type("T", (), {"start": lambda self: None})()
    )
    monkeypatch.setattr(gui.App, "_poll_export", lambda self: None)


def test_start_wires_the_resolved_directory_into_the_progress(
    app: gui.App, monkeypatch, tmp_path: Path
) -> None:
    """``start()`` 拿 ``prepare_export_dir()`` 的结果开工，并把目录写进日志。

    这里只验"进门那几步"，不验导出本身：线程被换成空壳，``_poll_export`` 也被挡掉。
    """
    chosen = tmp_path / "导出"
    chosen.mkdir()
    app.output_var.set(str(chosen))
    app.settings.userhash = "TESTHASH"
    monkeypatch.setattr(gui, "ensure_writable", lambda *a, **k: chosen)
    monkeypatch.setattr(gui, "choose_writable_dir", lambda path, **k: _keep(path))
    _stub_export_thread(monkeypatch)

    app.start(["https://www.nmbxd1.com/t/69540387"])

    assert app._exporting is True  # noqa: SLF001
    content = app.log_text.get("1.0", "end")
    assert f"导出目录：{chosen}" in content
    assert "开始导出" in content
    app._exporting = False  # noqa: SLF001


def _keep(path: str):
    from xdao.exporters._shared import DirChoice

    return DirChoice(Path(path), [], False)


def test_start_moves_to_a_writable_directory(app: gui.App, monkeypatch, tmp_path: Path) -> None:
    """导出目录写不进去时，界面要自动换到能写的位置并说清楚。

    实际遇到的场景：目录设在桌面下的新文件夹（受控文件夹访问的保护范围），
    抓取全部成功、写文件时 ``[Errno 13]``，整趟白跑。
    """
    from xdao.exporters._shared import DirChoice

    fallback_dir = tmp_path / "兜底" / "导出"
    blocked = tmp_path / "桌面" / "被拦的新文件夹"
    blocked.mkdir(parents=True)
    app.output_var.set(str(blocked))
    app.settings.userhash = "TESTHASH"  # 跳过"请先登录"的检查
    note = f"导出目录 {blocked} 写不进去，已自动改用 {fallback_dir}。"

    def fake_choose(requested, *, kind="导出", allow_fallback=True, probe=None):
        assert allow_fallback is True  # 没在「更改…」里选过，允许换地方
        return DirChoice(fallback_dir, [note], True)

    shown: list[tuple[str, str]] = []
    monkeypatch.setattr(gui, "choose_writable_dir", fake_choose)
    monkeypatch.setattr(gui, "ensure_writable", lambda *a, **k: blocked)
    monkeypatch.setattr(gui.messagebox, "showwarning", lambda title, msg: shown.append((title, msg)))
    monkeypatch.setattr(gui.messagebox, "showerror", lambda title, msg: shown.append((title, msg)))

    resolved = app.prepare_export_dir()

    assert resolved == str(fallback_dir)
    assert app.output_var.get() == str(fallback_dir)
    assert fallback_dir.is_dir(), "兜底目录要当场建出来，否则第一次导出就写不进去"
    assert shown and shown[0][0] == "导出目录已自动改到能写的位置"
    assert str(fallback_dir) in shown[0][1]
    assert str(fallback_dir) in app.log_text.get("1.0", "end")


def test_start_keeps_a_user_chosen_directory(app: gui.App, monkeypatch, tmp_path: Path) -> None:
    """在「更改…」里选过的目录不换：只报错，不动他的选择。"""
    from xdao.exporters._shared import DirChoice

    blocked = tmp_path / "用户选的目录"
    app.output_var.set(str(blocked))
    app.settings.userhash = "TESTHASH"
    app._dir_pinned = True  # noqa: SLF001  —— 等价于用户点过「更改…」

    captured: dict[str, bool] = {}

    def fake_choose(requested, *, kind="导出", allow_fallback=True, probe=None):
        captured["allow_fallback"] = allow_fallback
        return DirChoice(blocked, [f"{kind}目录 {blocked} 写不进去。"], False)

    monkeypatch.setattr(gui, "choose_writable_dir", fake_choose)
    monkeypatch.setattr(gui, "ensure_writable", lambda *a, **k: blocked)
    monkeypatch.setattr(gui.messagebox, "showwarning", lambda title, msg: None)

    resolved = app.prepare_export_dir()

    assert captured["allow_fallback"] is False
    assert resolved == str(blocked)
    assert app.output_var.get() == str(blocked), "用户选的目录不能被偷偷改掉"
    assert str(blocked) in app.log_text.get("1.0", "end")


def test_prepare_export_dir_stops_when_the_directory_cannot_be_created(
    app: gui.App, monkeypatch
) -> None:
    """连目录都建不出来时返回 None（``start()`` 靠它决定不往下走）。"""
    from xdao.exporters._shared import OutputDirNotWritable

    warnings: list[tuple[str, str]] = []

    def refuse(path):
        raise OutputDirNotWritable("导出目录不可用：权限拒绝")

    monkeypatch.setattr(gui, "ensure_writable", refuse)
    monkeypatch.setattr(gui.messagebox, "showerror", lambda title, msg: warnings.append((title, msg)))

    assert app.prepare_export_dir() is None
    assert warnings and warnings[0][0] == "导出目录不可用"


# --------------------------------------------------------------- 收尾不能崩


def test_finish_reports_the_directory_without_a_local_output_dir(
    app: gui.App, monkeypatch
) -> None:
    """``_finish()`` 不许引用 ``start()`` 的局部变量 ``output_dir``。

    2026-09-30 实测：一趟导出结束后弹「NameError: name 'output_dir' is not
    defined」—— ``_finish`` 是 ``_poll_export`` 从队列回调里调的，那里根本
    看不到 ``start()`` 的局部变量，只能读实例上的值。
    """
    dialogs: list[tuple[str, str]] = []
    monkeypatch.setattr(
        gui.messagebox, "showwarning", lambda title, msg: dialogs.append((title, msg))
    )
    monkeypatch.setattr(
        gui.messagebox, "showinfo", lambda title, msg: dialogs.append((title, msg))
    )
    app._output_dir = r"<盘符>\X岛备份"  # noqa: SLF001

    app._finish(0, 1, ["https://www.nmbxd1.com/t/68204233"], 18.4, [])  # noqa: SLF001

    assert dialogs, "部分失败时必须弹提示"
    assert dialogs[0][0] == "部分失败"
    assert r"<盘符>\X岛备份" in dialogs[0][1]
    assert app._exporting is False  # noqa: SLF001
    assert app._last_failed == ["https://www.nmbxd1.com/t/68204233"]  # noqa: SLF001


def test_finish_falls_back_to_the_output_box_when_the_instance_value_is_missing(
    app: gui.App, monkeypatch
) -> None:
    """实例值万一没落下，收尾也要给出一个目录，而不是抛异常。"""
    app.output_var.set(r"<盘符>\某人选的目录")
    app._output_dir = ""  # noqa: SLF001

    assert app._resolved_output_dir() == r"<盘符>\某人选的目录"  # noqa: SLF001


# ------------------------------------------------------------------ 暗色模式


def test_theme_switch_repaints_and_keeps_what_the_user_typed(
    app: gui.App, monkeypatch
) -> None:
    """切主题：配色真的换掉、界面重画、日志与串网址都还在。"""
    requests: list[tuple[str, str, object]] = []
    monkeypatch.setattr(
        gui.messagebox, "showinfo", lambda title, msg: requests.append((title, msg, None))
    )
    app.urls_text.insert("1.0", "https://www.nmbxd1.com/t/12345678")
    app.log("切主题之前的一行日志")
    app.root.update_idletasks()
    light_card = gui.CARD
    light_log_bg = app.log_text.cget("bg")

    changed = app.switch_theme("dark")
    app.root.update_idletasks()

    assert changed is True
    assert app._theme_name == "dark"  # noqa: SLF001
    assert app.settings.theme_name == "dark"
    assert theme.PALETTE.name == "dark"
    assert gui.CARD == theme.DARK.surface
    assert gui.CARD != light_card
    assert app.log_text.cget("bg") == theme.DARK.surface_sunken
    assert app.log_text.cget("bg") != light_log_bg
    assert app.style.lookup("TFrame", "background") == theme.DARK.bg
    assert "切主题之前的一行日志" in app.log_text.get("1.0", "end")
    assert "https://www.nmbxd1.com/t/12345678" in app.urls_text.get("1.0", "end")
    assert app._theme_var.get() == "dark"  # noqa: SLF001
    assert "深色" in app.log_text.get("1.0", "end")

    # 切回浅色，别把深色主题漏给后面的用例（根窗口是本模块共用的）
    assert app.switch_theme("light") is True
    app.root.update_idletasks()
    assert gui.CARD == theme.LIGHT.surface
    assert not requests, "没在忙，不该弹「正在忙」"


def test_theme_switch_is_skipped_for_the_same_palette(app: gui.App) -> None:
    """已经是这个配色就不用重画（返回 False）。"""
    app.switch_theme("light")

    assert app.switch_theme("light") is False
    assert app._theme_name == "light"  # noqa: SLF001


def test_theme_selector_refuses_to_switch_while_exporting(app: gui.App, monkeypatch) -> None:
    """导出/监控跑着的时候不换配色：提示一句，下拉框回退，界面不动。"""
    notices: list[tuple[str, str]] = []
    monkeypatch.setattr(
        gui.messagebox, "showinfo", lambda title, msg: notices.append((title, msg))
    )
    before = gui.CARD
    app._exporting = True  # noqa: SLF001
    app._theme_var.set("dark")  # noqa: SLF001

    app._on_theme_selected()  # noqa: SLF001

    assert notices and notices[0][0] == "正在忙"
    assert app._theme_name == "light"  # noqa: SLF001
    assert app._theme_var.get() == "light"  # noqa: SLF001
    assert gui.CARD == before
    app._exporting = False  # noqa: SLF001


def test_open_dialogs_follow_the_theme(app: gui.App, monkeypatch) -> None:
    """已经打开的对话框也要跟着换色（它们不在界面重建范围内）。"""
    dialog = tk.Toplevel(app.root)
    frame = tk.Frame(dialog, bg=theme.LIGHT.surface)
    frame.pack()
    app.root.update_idletasks()
    try:
        app.switch_theme("dark")
        app.root.update_idletasks()

        assert frame.cget("bg") == theme.DARK.surface
    finally:
        dialog.destroy()
        app.switch_theme("light")


# ---------- 说明文字与按钮排：窗口小的时候也不能缺字 ----------
#
# 现象就是「自检窗口的介绍在窗口比较小的情况下无法显示全」。查下去才发现
# 同一类毛病有好几处：介绍标签没有 wraplength（整句 1188px 摊开等着被裁）、
# 缓存那行的两个按钮被挤到卡片外面、日志卡片里最后一枚按钮只剩半个。
# 下面这些用例盯的就是「控件拿到的位置装不装得下它要显示的字」。


def _widgets(widget: tk.Misc):
    """深度优先遍历整棵控件树。"""
    yield widget
    for child in widget.winfo_children():
        yield from _widgets(child)


def _settle(app: gui.App, rounds: int = 4) -> None:
    """让 Configure 事件走完（换行宽度靠它算）。"""
    for _ in range(rounds):
        app.root.update_idletasks()
        app.root.update()


def _holder(app: gui.App, width: int, height: int = 200) -> tk.Frame:
    """一个定宽容器，用来逼出窄布局。

    ``pack_propagate(False)`` 是关键：否则容器会被子控件的自然宽度撑开，
    永远测不到「装不下」的情形。
    """
    holder = tk.Frame(app.root, width=width, height=height)
    holder.pack_propagate(False)
    holder.pack()
    _settle(app)
    return holder


def test_wrap_to_width_follows_the_label(app: gui.App) -> None:
    """换行宽度跟着控件实际宽度走。"""
    holder = _holder(app, 300)
    try:
        label = ttk.Label(holder, text="一" * 60)
        label.pack(fill="x")
        gui.wrap_to_width(label, minimum=60)
        _settle(app)
        assert int(str(label.cget("wraplength"))) == 300 - theme.gap(1)

        holder.configure(width=560)
        _settle(app)
        assert int(str(label.cget("wraplength"))) == 560 - theme.gap(1)
    finally:
        holder.destroy()


def test_wrap_to_width_keeps_a_floor_for_collapsed_windows(app: gui.App) -> None:
    """窗口被拖到只剩几十像素时也要留个下限，别算成 0（那就又变回不换行了）。"""
    holder = _holder(app, 30)
    try:
        label = ttk.Label(holder, text="一" * 60)
        label.pack(fill="x")
        gui.wrap_to_width(label, minimum=120)
        _settle(app)
        assert int(str(label.cget("wraplength"))) == 120
    finally:
        holder.destroy()


def test_fold_buttons_when_narrow_keeps_the_primary_row_on_top(app: gui.App) -> None:
    """窄了只把次要按钮挪到第二行，主按钮留在第一行（第一行不能空着）。"""
    holder = _holder(app, 700)
    row = ttk.Frame(holder)
    row.pack(fill="x")
    primary = [ttk.Button(row, text=text) for text in ("重新自检", "检查联网", "试浏览器")]
    secondary = [ttk.Button(row, text=text) for text in ("复制结果", "关闭")]
    try:
        gui.fold_buttons_when_narrow(row, primary, secondary)
        _settle(app)
        assert len({button.winfo_y() for button in (*primary, *secondary)}) == 1

        holder.configure(width=400)
        _settle(app)
        assert len({button.winfo_y() for button in primary}) == 1, "主按钮该在同一行"
        assert len({button.winfo_y() for button in secondary}) == 1, "次要按钮该在同一行"
        assert min(b.winfo_y() for b in secondary) > max(
            b.winfo_y() for b in primary
        ), "次要按钮该在主按钮下面"
        for button in (*primary, *secondary):
            assert button.winfo_width() + 1 >= button.winfo_reqwidth(), button.cget("text")
    finally:
        holder.destroy()


def test_buttons_stay_inside_their_parent_in_the_main_window(app: gui.App) -> None:
    """主窗口拉到最小尺寸时，按钮也不能被挤到父容器外面。

    缓存那行原来就是「标签先 pack、按钮后 pack」，940 宽时两个按钮被推到
    卡片外面 —— 看不见也点不到。
    """
    app.root.geometry("940x682")
    _settle(app)

    checked = 0
    for widget in _widgets(app.root):
        if widget.winfo_class() != "TButton" or not widget.winfo_ismapped():
            continue
        parent = widget.nametowidget(widget.winfo_parent())
        assert widget.winfo_x() + widget.winfo_width() <= parent.winfo_width() + 1, (
            f"{widget.cget('text')} 跑到 {parent} 外面（x={widget.winfo_x()}）"
        )
        assert widget.winfo_y() + widget.winfo_height() <= parent.winfo_height() + 1, (
            f"{widget.cget('text')} 掉到 {parent} 下面（y={widget.winfo_y()}）"
        )
        checked += 1
    assert checked > 5, "没量到几个按钮，用例本身可能坏了"


def test_selftest_intro_wraps_at_the_minimum_size(app: gui.App) -> None:
    """自检窗口缩到最小尺寸时，介绍文字整句都看得见（这一类毛病的一处）。"""
    app.open_selftest()
    dialog = app._selftest_dialog  # noqa: SLF001
    assert dialog is not None
    try:
        dialog.geometry("520x460")
        _settle(app)

        intro = dialog.intro  # noqa: SLF001
        assert int(str(intro.cget("wraplength"))) <= intro.winfo_width() + 1
        assert intro.winfo_height() > 30, "整句 1188px 塞进 456px，该换成好几行"
        for widget in _widgets(dialog):
            if widget.winfo_class() != "TButton" or not widget.winfo_ismapped():
                continue
            assert widget.winfo_width() + 1 >= widget.winfo_reqwidth(), widget.cget("text")
            assert (
                widget.winfo_rooty() + widget.winfo_height()
                <= dialog.winfo_rooty() + dialog.winfo_height() + 1
            ), f"{widget.cget('text')} 被窗口下沿切掉了"
    finally:
        dialog.destroy()


def test_selftest_minimum_size_fits_the_folded_buttons(app: gui.App) -> None:
    """520 宽时按钮排折成两行，最小高度（460）要装得下整块。"""
    app.open_selftest()
    dialog = app._selftest_dialog  # noqa: SLF001
    assert dialog is not None
    try:
        assert dialog.minsize() == (520, 460)
        dialog.geometry("520x300")  # 比最小高度还矮
        _settle(app)
        assert dialog.winfo_height() >= 460
    finally:
        dialog.destroy()


# ---------------------------------------------------------------------------
# 「直接粘贴饼干登录」窗口
#
# 用户 2026-10-01 问「为什么程序打开的 Edge 和我平时用的不是同一个软件」——
# 原来的提示只有 simpledialog 的一行字，讲不清「为什么不读你的浏览器数据」
# 和「去哪儿抄 userhash」。这一版换成自家窗口，下面几条守住它。
# ---------------------------------------------------------------------------


def test_paste_dialog_picks_the_value_out_of_a_whole_cookie_string(app: gui.App) -> None:
    """用户复制到的是整段 cookie，不该逼他自己找 userhash= 在哪。"""
    dialog = gui.PasteCookieDialog(app.root)
    try:
        dialog.text.insert("1.0", "other=1; userhash=ABCD1234; sid=xyz; theme=dark")
        dialog._confirm()  # noqa: SLF001

        assert dialog.userhash == "ABCD1234"
    finally:
        if dialog.winfo_exists():
            dialog.destroy()


def test_paste_dialog_keeps_the_window_open_when_there_is_no_userhash(
    app: gui.App, monkeypatch
) -> None:
    """粘错了不能抛异常、不能把垃圾值当饼干，也不能把窗口关掉让他重粘。"""
    warnings: list[tuple] = []
    monkeypatch.setattr(gui.messagebox, "showwarning", lambda *a, **k: warnings.append(a))

    dialog = gui.PasteCookieDialog(app.root)
    try:
        dialog.text.insert("1.0", "我复制了一段别的东西")
        dialog._confirm()  # noqa: SLF001

        assert dialog.userhash is None
        assert warnings, "解析失败要弹一次提示"
        assert "userhash" in warnings[0][1]
        assert dialog.winfo_exists(), "窗口要留着，让人重新粘一次"
    finally:
        dialog.destroy()


def test_paste_dialog_explains_itself_at_the_minimum_size(app: gui.App) -> None:
    """最小尺寸下两段说明都要完整可读（这是这一版真正交付的东西）。"""
    dialog = gui.PasteCookieDialog(app.root)
    try:
        dialog.geometry("560x430")
        _settle(app)

        labels = [
            widget
            for widget in _widgets(dialog)
            if widget.winfo_class() == "Label" and str(widget.cget("text")).strip()
        ]
        texts = "\n".join(str(label.cget("text")) for label in labels)
        assert "不会去翻你自己浏览器的数据" in texts
        assert "F12" in texts and "应用程序 / Application" in texts
        for label in labels:
            assert int(str(label.cget("wraplength"))) <= label.winfo_width() + 1, str(
                label.cget("text")
            )[:24]
    finally:
        if dialog.winfo_exists():
            dialog.destroy()


def test_ask_pasted_cookie_returns_what_the_window_collected(app: gui.App, monkeypatch) -> None:
    """wait_window 那条路：窗口自己关掉之后，函数要把摘好的值交出来。"""
    made: list[gui.PasteCookieDialog] = []

    class Spy(gui.PasteCookieDialog):
        def __init__(self, master) -> None:
            super().__init__(master)
            made.append(self)
            self.after(50, self._finish)

        def _finish(self) -> None:
            self.userhash = "ABCD1234"
            self.destroy()

    monkeypatch.setattr(gui, "PasteCookieDialog", Spy)

    assert gui.ask_pasted_cookie(app.root) == "ABCD1234"
    assert len(made) == 1


def test_ask_pasted_cookie_returns_none_when_the_window_is_closed(
    app: gui.App, monkeypatch
) -> None:
    """用户直接关窗：不能抛异常，也不能把 None 当成饼干。"""

    class Spy(gui.PasteCookieDialog):
        def __init__(self, master) -> None:
            super().__init__(master)
            self.after(50, self.destroy)

    monkeypatch.setattr(gui, "PasteCookieDialog", Spy)

    assert gui.ask_pasted_cookie(app.root) is None

