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
        settings = cls()
        settings.output_dir = str(out_dir)
        settings.cache_dir = str(cache_dir)
        settings.use_cache = True
        settings.notify = False
        settings.watch_targets = []
        settings.userhash = None
        return settings

    original_load = AppSettings.load
    AppSettings.load = classmethod(fake_load)
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
    proc = subprocess.run(
        [sys.executable, "-X", "utf8", "-c", script],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=str(Path(__file__).resolve().parents[1]),
    )
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
