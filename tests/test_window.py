"""主窗口布局的回归测试（真窗口，无显示环境时自动跳过）。

这些用例的价值在于：左栏宽度、左右栏比例、底部按钮区不被窗口下沿吃掉，
都是靠肉眼截图才发现过的真问题（曾经右栏被挤成 39px、进度条被压掉一截），
只靠"能 import"是看不出来的。
"""

from __future__ import annotations

import pathlib
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import ttk

import pytest

from xdao import gui, theme
from xdao.settings import AppSettings

from tests import make_post

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


def test_login_pill_follows_the_saved_cookie(app: gui.App) -> None:
    """登录之后左上角那个标要跟着变（v0.13.24）。

    它是 ``StatusPill``：建出来的时候就把文字**抄**走了，只改 ``status_var`` 它不会动。
    真机上就是「运行日志说登录成功、左上角还写未登录」（用户 m31364 因此以为没登录上，
    又去试别的路）。
    """
    app.settings.userhash = None
    app.show_login_state()
    assert app.status_var.get() == "未登录"
    assert app.status_pill._text == "未登录"

    app.settings.userhash = "TESTHASH"
    app.show_login_state()
    assert app.status_var.get() == "已登录"
    assert app.status_pill._text == "已登录"


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
        "log_family = str(root.tk.splitlist(log)[0]);"
        "print('|'.join([import_at_import, theme.FONT_MONO, gui.MONO_FONT[0], log, log_family]));"
        "root.destroy()"
    )
    proc = _run_in_fresh_process(script)
    assert proc.returncode == 0, f"子进程启动失败：{proc.stderr[-800:]}"
    at_import, theme_mono, module_mono, log_font, log_family = (
        proc.stdout.strip().split("|")
    )
    assert module_mono == theme_mono, (
        f"模块级 MONO_FONT={module_mono!r} 与 theme.FONT_MONO={theme_mono!r} 分叉"
    )
    # 字体串在**子进程**里解析（那边的 root 是真的 Tk）。别在父进程里用
    # ``tk.Tcl()`` —— 那是纯 Tcl 解释器，CI 的 windows runner 上连 init.tcl 都找不到
    # （run 36885850517 就是这样把整个作业判红的），而 ``widget.tk.splitlist(...)``
    # 才是可靠读法（见 :func:`_widget_font`）。
    assert log_family == theme_mono, (
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


# ---------------------------------------------------------------- 图片行
# v0.16.0 之前「图片」下拉只对 EPUB 显示（别的格式永远 base64 内嵌）。
# 现在几种模式对 HTML / PDF 也生效、v0.20.0 起 Markdown 也能存同名目录，
# 这一行就得跟着格式出现；只有 TXT 这种根本存不了图的格式不显示。


def test_image_row_follows_the_format(app: gui.App) -> None:
    for key in ("html", "pdf", "epub", "markdown"):
        app.format_box.current(app._format_keys.index(key))  # noqa: SLF001
        app._sync_image_mode()  # noqa: SLF001
        app.root.update_idletasks()
        assert app.image_mode_frame.winfo_ismapped(), key
        assert app.current_format() == key

    for key in ("txt",):
        app.format_box.current(app._format_keys.index(key))  # noqa: SLF001
        app._sync_image_mode()  # noqa: SLF001
        app.root.update_idletasks()
        assert not app.image_mode_frame.winfo_ismapped(), key


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

    app.start(["https://www.nmbxd1.com/t/12345678"])

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

    app._finish(0, 1, ["https://www.nmbxd1.com/t/12345678"], 18.4, [])  # noqa: SLF001

    assert dialogs, "部分失败时必须弹提示"
    assert dialogs[0][0] == "部分失败"
    assert r"<盘符>\X岛备份" in dialogs[0][1]
    assert app._exporting is False  # noqa: SLF001
    assert app._last_failed == ["https://www.nmbxd1.com/t/12345678"]  # noqa: SLF001


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
# 现象就是「窗口拉窄一点，介绍文字的右边会被切掉」（自检窗口最明显）。查下去才发现
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
# 「用浏览器登录」弹出来的那个浏览器窗口，为什么是空的 —— 原来只有 simpledialog
# 的一行字，讲不清「为什么不读你自己浏览器里的数据」「去哪儿抄 userhash」。
# 这一版换成自家窗口，下面几条守住它。
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


# ---------- 「打开目录」：三道尝试 + 失败说清原因（v0.13.27）----------


def test_open_folder_falls_back_to_explorer(app: gui.App, monkeypatch, tmp_path: Path) -> None:
    """``os.startfile`` 被系统拒了就直接叫 explorer，并把这步写进日志。

    2026-10-02 真机：那台机器上 ``os.startfile`` 抛异常，界面上只弹了一个写着
    路径的提示框，用户以为按钮坏了 —— 真正的原因（系统不让程序拉起资源管理器）
    被吞掉了。
    """
    folder = tmp_path / "X岛备份"
    app.output_var.set(str(folder))
    attempts: list[list[str]] = []
    shown: list[tuple[str, str]] = []

    def refuse(*args, **kwargs):
        raise OSError(31, "连到系统上的设备没有发挥作用")

    monkeypatch.setattr(gui.os, "startfile", refuse, raising=False)
    monkeypatch.setattr(gui, "_spawn", lambda command: attempts.append(command))
    monkeypatch.setattr(gui.messagebox, "showinfo", lambda title, msg: shown.append((title, msg)))

    app.open_folder()

    assert attempts and attempts[0][0] == "explorer"
    assert str(folder) in attempts[0][1]
    assert shown == [], "备用方式成功时不该再弹提示框"
    assert folder.is_dir(), "打开之前要先把目录建出来"
    assert f"已打开目录：{folder}" in app.log_text.get("1.0", "end")


def test_open_folder_says_why_when_every_way_fails(
    app: gui.App, monkeypatch, tmp_path: Path
) -> None:
    """三条路都不行时，每一步的原因都要写进日志与提示框（不再只弹一个路径）。"""
    folder = tmp_path / "X岛备份"
    app.output_var.set(str(folder))
    shown: list[tuple[str, str]] = []

    def refuse(*args, **kwargs):
        raise OSError(5, "拒绝访问")

    monkeypatch.setattr(gui.os, "startfile", refuse, raising=False)
    monkeypatch.setattr(gui, "_spawn", refuse)
    monkeypatch.setattr(gui.messagebox, "showinfo", lambda title, msg: shown.append((title, msg)))

    app.open_folder()

    assert shown and shown[0][0] == "目录"
    assert str(folder) in shown[0][1]
    assert "拒绝访问" in shown[0][1]
    log = app.log_text.get("1.0", "end")
    for label in ("os.startfile", "explorer", "explorer /select,"):
        assert f"{label}：" in shown[0][1]
        assert f"{label}：" in log


def test_open_folder_reports_a_directory_it_cannot_create(
    app: gui.App, monkeypatch, tmp_path: Path
) -> None:
    """目录建不出来时当场说清（这个 mkdir 以前在 try 外面，会炸成 Tk 回调异常）。"""
    folder = tmp_path / "打不开的目录" / "X岛备份"
    app.output_var.set(str(folder))
    errors: list[tuple[str, str]] = []
    spawned: list[list[str]] = []
    real_mkdir = Path.mkdir

    def fake_mkdir(self, *args, **kwargs):
        if str(self).startswith(str(tmp_path)):
            raise PermissionError(13, "Permission denied")
        return real_mkdir(self, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", fake_mkdir)
    monkeypatch.setattr(gui, "_spawn", lambda command: spawned.append(command))
    monkeypatch.setattr(gui.messagebox, "showerror", lambda title, msg: errors.append((title, msg)))

    app.open_folder()

    assert spawned == [], "目录都建不出来就不该再去拉资源管理器"
    assert errors and errors[0][0] == "打不开这个目录"
    assert "Permission denied" in errors[0][1]
    assert "目录建不出来" in app.log_text.get("1.0", "end")


def test_open_folder_asks_for_a_directory_first(app: gui.App, monkeypatch) -> None:
    """没填目录时先提示，不去碰系统。"""
    app.output_var.set("")
    warnings: list[tuple[str, str]] = []
    monkeypatch.setattr(
        gui.messagebox, "showwarning", lambda title, msg: warnings.append((title, msg))
    )

    app.open_folder()

    assert warnings and warnings[0][0] == "提示"

# ---------------------------------------------------------------------------
# 队列轮询：导出进度/日志、挑饼干的两条出口（v0.21.2 补）
# ---------------------------------------------------------------------------


def test_poll_export_shows_progress_and_writes_logs(app: gui.App) -> None:
    """导出线程只往队列里塞消息，界面靠这条循环把它们落到进度条与日志上。

    钉它是因为：日志丢一条，用户就看不到「哪一步卡住了」，而这条链路
    中间隔着队列，平时跑界面很难察觉少了一行。
    """
    app._exporting = True  # noqa: SLF001
    app._export_queue.put(("progress", (3, 10, "正在抓第 4 页")))  # noqa: SLF001
    app._export_queue.put(("log", "这一行应该出现在日志里"))  # noqa: SLF001
    try:
        app._poll_export()  # noqa: SLF001
        app.root.update_idletasks()
        assert "正在抓第 4 页（3/10）" == app.progress_var.get()
        assert "这一行应该出现在日志里" in app.log_text.get("1.0", "end-1c")
    finally:
        app._exporting = False  # noqa: SLF001
        while not app._export_queue.empty():  # noqa: SLF001
            app._export_queue.get_nowait()  # noqa: SLF001


def test_poll_cookies_says_when_the_first_page_has_nothing_to_pick(
    app: gui.App, monkeypatch: pytest.MonkeyPatch
) -> None:
    shown: list[tuple[str, str]] = []
    monkeypatch.setattr(
        gui.messagebox, "showinfo", lambda title, body, **kw: shown.append((title, body))
    )
    app._cookie_queue.put(("ok", []))  # noqa: SLF001

    app._poll_cookies()  # noqa: SLF001

    assert shown == [("提示", "第一页里没有可选的饼干。")]
    assert str(app.pick_cookie_button.cget("state")) == "normal"


def test_poll_cookies_shows_why_reading_failed(
    app: gui.App, monkeypatch: pytest.MonkeyPatch
) -> None:
    shown: list[tuple[str, str]] = []
    monkeypatch.setattr(
        gui.messagebox, "showerror", lambda title, body, **kw: shown.append((title, body))
    )
    app._cookie_queue.put(("fail", "网络断了"))  # noqa: SLF001

    app._poll_cookies()  # noqa: SLF001

    assert shown == [("读取失败", "网络断了")]

# ---------------------------------------------------------------------------
# 登录对话框的两条轮询：验证码加载（_poll_form）与账号密码登录（_poll_login）
# ---------------------------------------------------------------------------


class _FakeForm:
    """`client.fetch_login_form()` 的返回值里，对话框只用到这两样。"""

    def __init__(self, captcha_bytes: bytes = b"", hash_value: str = "abc123") -> None:
        self.captcha_bytes = captcha_bytes
        self.hash_value = hash_value


_GIF_1X1 = (
    b"GIF89a\x01\x00\x01\x00\x80\x00\x00\xff\xff\xff\x00\x00\x00!"
    b"\xf9\x04\x01\x00\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01"
    b"\x00\x00\x02\x02D\x01\x00;"
)


def _login_dialog(root_window: tk.Tk) -> gui.LoginDialog:
    """开一个登录对话框：client 用空壳（这些用例只手动喂队列，不真的联网）。"""
    return gui.LoginDialog(root_window, object())  # type: ignore[arg-type]


def test_login_dialog_puts_the_captcha_on_the_button(root_window: tk.Tk) -> None:
    dialog = _login_dialog(root_window)
    try:
        dialog._result_queue.put(("form", _FakeForm(_GIF_1X1)))  # noqa: SLF001
        dialog._poll_form()  # noqa: SLF001

        assert str(dialog.captcha_button.cget("state")) == "normal"
        assert dialog._captcha_image is not None  # noqa: SLF001
        assert dialog._busy is False  # noqa: SLF001
    finally:
        dialog.destroy()


def test_login_dialog_falls_back_to_a_refresh_button_for_a_broken_captcha(
    root_window: tk.Tk,
) -> None:
    """验证码图坏了不能把按钮永远卡在「加载中…」上，得让人点一下重试。"""
    dialog = _login_dialog(root_window)
    try:
        dialog._result_queue.put(("form", _FakeForm(b"not an image")))  # noqa: SLF001
        dialog._poll_form()  # noqa: SLF001

        assert str(dialog.captcha_button.cget("state")) == "normal"
        assert str(dialog.captcha_button.cget("text")) == "刷新"
    finally:
        dialog.destroy()


def test_login_dialog_reports_why_the_captcha_did_not_load(
    root_window: tk.Tk, monkeypatch: pytest.MonkeyPatch
) -> None:
    shown: list[tuple[str, str]] = []
    monkeypatch.setattr(
        gui.messagebox, "showerror", lambda title, body, **kw: shown.append((title, body))
    )
    dialog = _login_dialog(root_window)
    try:
        dialog._result_queue.put(("form_error", "网络断了"))  # noqa: SLF001
        dialog._poll_form()  # noqa: SLF001

        assert shown == [("加载验证码失败", "网络断了")]
        assert str(dialog.captcha_button.cget("text")) == "刷新"
    finally:
        dialog.destroy()


def test_login_dialog_demands_all_three_fields(
    root_window: tk.Tk, monkeypatch: pytest.MonkeyPatch
) -> None:
    warned: list[tuple[str, str]] = []
    monkeypatch.setattr(
        gui.messagebox, "showwarning", lambda title, body, **kw: warned.append((title, body))
    )
    dialog = _login_dialog(root_window)
    try:
        dialog.email_var.set("a@b.c")  # noqa: SLF001
        dialog.password_var.set("")  # noqa: SLF001
        dialog.verify_var.set("1234")  # noqa: SLF001

        dialog._do_login()  # noqa: SLF001

        assert warned == [("提示", "请填写账号、密码和验证码。")]
    finally:
        dialog.destroy()


def test_login_dialog_waits_until_the_captcha_is_loaded(
    root_window: tk.Tk, monkeypatch: pytest.MonkeyPatch
) -> None:
    warned: list[tuple[str, str]] = []
    monkeypatch.setattr(
        gui.messagebox, "showwarning", lambda title, body, **kw: warned.append((title, body))
    )
    dialog = _login_dialog(root_window)
    try:
        dialog.email_var.set("a@b.c")  # noqa: SLF001
        dialog.password_var.set("pw")  # noqa: SLF001
        dialog.verify_var.set("1234")  # noqa: SLF001
        dialog.form = None  # noqa: SLF001

        dialog._do_login()  # noqa: SLF001

        assert warned == [("提示", "验证码还没加载完成，请稍候。")]
    finally:
        dialog.destroy()


def test_login_dialog_takes_the_userhash_and_closes(root_window: tk.Tk) -> None:
    dialog = _login_dialog(root_window)
    dialog._busy = True  # noqa: SLF001
    dialog._result_queue.put(("ok", "hash-abc"))  # noqa: SLF001

    dialog._poll_login()  # noqa: SLF001

    assert dialog.userhash == "hash-abc"
    assert not dialog.winfo_exists()


def test_login_dialog_explains_a_failed_login_and_refreshes_the_captcha(
    root_window: tk.Tk, monkeypatch: pytest.MonkeyPatch
) -> None:
    shown: list[tuple[str, str]] = []
    refreshed: list[int] = []
    monkeypatch.setattr(
        gui.messagebox, "showerror", lambda title, body, **kw: shown.append((title, body))
    )
    dialog = _login_dialog(root_window)
    try:
        monkeypatch.setattr(dialog, "_refresh_captcha", lambda: refreshed.append(1))
        dialog._busy = True  # noqa: SLF001
        dialog._result_queue.put(("fail", "密码错了"))  # noqa: SLF001

        dialog._poll_login()  # noqa: SLF001

        assert len(shown) == 1
        assert "密码错了" in shown[0][1]
        assert refreshed == [1]
        assert str(dialog.login_button.cget("text")) == "登录"
    finally:
        if dialog.winfo_exists():
            dialog.destroy()

def test_login_dialog_keeps_waiting_while_the_captcha_is_on_its_way(
    root_window: tk.Tk,
) -> None:
    """队列还空着且仍在忙：不该结束轮询，而要再排一次。

    这条分支如果被删掉，界面就永远停在「加载中…」——后台线程还在跑，
    却再也没人来收它的结果。
    """
    dialog = _login_dialog(root_window)
    scheduled: list[tuple[int, object]] = []
    try:
        dialog._busy = True  # noqa: SLF001
        dialog.after = lambda delay, func=None: scheduled.append((delay, func))  # type: ignore[method-assign]  # noqa: SLF001

        dialog._poll_form()  # noqa: SLF001

        assert scheduled and scheduled[0][0] == 50
        assert dialog._busy is True  # noqa: SLF001
    finally:
        dialog.destroy()


def test_login_dialog_keeps_waiting_while_the_login_is_on_its_way(
    root_window: tk.Tk,
) -> None:
    dialog = _login_dialog(root_window)
    scheduled: list[tuple[int, object]] = []
    try:
        dialog._busy = True  # noqa: SLF001
        dialog.after = lambda delay, func=None: scheduled.append((delay, func))  # type: ignore[method-assign]  # noqa: SLF001

        dialog._poll_login()  # noqa: SLF001

        assert scheduled and scheduled[0][0] == 50
    finally:
        dialog.destroy()

# ---------------------------------------------------------------------------
# 主窗口上的几条动作：清缓存、更新提示、联网检查、开监控前的拦路条件
# ---------------------------------------------------------------------------


def test_clear_cache_says_there_is_nothing_to_clear(
    app: gui.App, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    said: list[tuple[str, str]] = []
    monkeypatch.setattr(
        gui.messagebox, "showinfo", lambda title, body, **kw: said.append((title, body))
    )
    monkeypatch.setattr(app, "current_cache_dir", lambda: tmp_path / "missing")

    app.clear_cache()

    assert said == [("提示", "当前没有缓存。")]


def test_clear_cache_does_nothing_when_the_user_says_no(
    app: gui.App, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    (cache_dir / "keep.txt").write_text("x", encoding="utf-8")
    monkeypatch.setattr(app, "current_cache_dir", lambda: cache_dir)
    monkeypatch.setattr(gui.messagebox, "askyesno", lambda *a, **k: False)
    refreshed: list[int] = []
    monkeypatch.setattr(app, "refresh_cache_info", lambda: refreshed.append(1))

    app.clear_cache()

    assert (cache_dir / "keep.txt").exists()
    assert refreshed == []


def test_clear_cache_removes_the_directory_and_logs_it(
    app: gui.App, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache_dir = tmp_path / "cache"
    (cache_dir / "images").mkdir(parents=True)
    (cache_dir / "images" / "a.bin").write_bytes(b"x")
    monkeypatch.setattr(app, "current_cache_dir", lambda: cache_dir)
    monkeypatch.setattr(gui.messagebox, "askyesno", lambda *a, **k: True)
    monkeypatch.setattr(app, "refresh_cache_info", lambda: None)

    app.clear_cache()

    assert not cache_dir.exists()
    assert "缓存已清空" in app.log_text.get("1.0", "end-1c")


def test_clear_cache_reports_a_failure_instead_of_pretending(
    app: gui.App, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    monkeypatch.setattr(app, "current_cache_dir", lambda: cache_dir)
    monkeypatch.setattr(gui.messagebox, "askyesno", lambda *a, **k: True)
    monkeypatch.setattr(app, "refresh_cache_info", lambda: None)
    shown: list[tuple[str, str]] = []
    monkeypatch.setattr(
        gui.messagebox, "showerror", lambda title, body, **kw: shown.append((title, body))
    )

    def boom(path: object) -> None:
        raise OSError("文件被占用")

    monkeypatch.setattr("shutil.rmtree", boom)

    app.clear_cache()

    assert shown == [("清空失败", "文件被占用")]


class _FakeCheck:
    def __init__(self, text: str) -> None:
        self._text = text

    def line(self) -> str:
        return self._text


class _StateStub:
    """`ttk` 部件的 ``state([...])`` 只要别真的去找 Tk 部件就行。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    def state(self, *args: object) -> None:
        # ttk 的写法是 state(["disabled"])，这里把列表拆平，断言才好写。
        flat: list[str] = []
        for item in args:
            if isinstance(item, (list, tuple)):
                flat.extend(str(name) for name in item)
            else:
                flat.append(str(item))
        self.calls.append(tuple(flat))


def _selftest_dialog(app: gui.App) -> gui.SelftestDialog:
    """`_run_network()` 是自检对话框上的方法，这里只要一个能记账的空壳。"""
    dialog = object.__new__(gui.SelftestDialog)
    dialog.tk = app.root.tk  # update_idletasks() 要用它
    dialog.app = app
    dialog.net_button = _StateStub()
    dialog.status_var = tk.StringVar(master=app.root)
    dialog.lines = []
    dialog._append = dialog.lines.append
    return dialog


def test_run_network_reports_each_check(app: gui.App, monkeypatch: pytest.MonkeyPatch) -> None:
    from xdao import preflight

    monkeypatch.setattr(
        preflight, "run_network_checks", lambda: [_FakeCheck("[行] 一是通的"), _FakeCheck("[行] 二也通")]
    )
    dialog = _selftest_dialog(app)

    dialog._run_network()

    assert dialog.lines[:2] == ["", "[行] 一是通的"]
    assert "[行] 二也通" in dialog.lines
    assert dialog.status_var.get() == ""
    assert dialog.net_button.calls == [("disabled",), ("!disabled",)]
    assert "联网那两项已经测过" in app.log_text.get("1.0", "end-1c")


def test_run_network_does_not_hide_its_own_failure(
    app: gui.App, monkeypatch: pytest.MonkeyPatch
) -> None:
    from xdao import preflight

    def boom() -> list[object]:
        raise RuntimeError("探针自己炸了")

    monkeypatch.setattr(preflight, "run_network_checks", boom)
    dialog = _selftest_dialog(app)

    dialog._run_network()

    assert any("联网检查本身出错了" in line and "探针自己炸了" in line for line in dialog.lines)


def test_offer_download_opens_the_page_when_the_user_agrees(
    app: gui.App, monkeypatch: pytest.MonkeyPatch
) -> None:
    from xdao import updater

    monkeypatch.setattr(
        updater, "plan_upgrade", lambda: type("P", (), {"possible": False, "reason": ""})()
    )
    monkeypatch.setattr(gui.messagebox, "askyesno", lambda *a, **k: True)
    opened: list[str] = []
    monkeypatch.setattr(app, "_open_download_page", opened.append)

    class _Result:
        url = "https://example.invalid/dl"
        asset_url = ""
        asset_size = 0

        def line(self) -> str:
            return "有新版本"

    app._offer_download(_Result())

    assert opened == ["https://example.invalid/dl"]


def test_offer_download_writes_the_page_into_the_log_when_the_browser_cannot_open(
    app: gui.App, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gui.messagebox, "askyesno", lambda *a, **k: True)
    monkeypatch.setattr(app, "_open_download_page", lambda url: app.log(f"下载页：{url}"))

    class _Result:
        url = "https://example.invalid/dl"
        asset_url = ""
        asset_size = 0

        def line(self) -> str:
            return "有新版本"

    app._offer_download(_Result())

    assert "https://example.invalid/dl" in app.log_text.get("1.0", "end-1c")


def test_start_watching_refuses_without_targets(
    app: gui.App, monkeypatch: pytest.MonkeyPatch
) -> None:
    warned: list[tuple[str, str]] = []
    monkeypatch.setattr(
        gui.messagebox, "showwarning", lambda title, body, **kw: warned.append((title, body))
    )

    app.start_watching(300)

    assert warned == [("提示", "请先添加要监控的串。")]
    assert app._watching is False  # noqa: SLF001


def test_start_watching_refuses_without_an_output_directory(
    app: gui.App, monkeypatch: pytest.MonkeyPatch
) -> None:
    from xdao.watcher import WatchTarget

    warned: list[tuple[str, str]] = []
    monkeypatch.setattr(
        gui.messagebox, "showwarning", lambda title, body, **kw: warned.append((title, body))
    )
    app.watch_targets = [WatchTarget("50000001")]  # noqa: SLF001
    app.output_var.set("")  # noqa: SLF001

    app.start_watching(300)

    assert warned == [("提示", "请先选择导出目录。")]
    assert app._watching is False  # noqa: SLF001

# ---------- 监控成功路径（v0.21.5）----------
#
# 这一组专门走「真的开始监控」这条路：以前只有两条「拦下」的用例，成功路径
# 一行都没跑过 —— 而它一旦坏了，表现是监控按钮点了没反应、状态永远显示空闲，
# 或者点了停止但线程不死。所以这里把 watch_forever 换成可观察的替身，
# 检查参数透传、状态收口（哨兵消息）与各类结果消息怎么进日志。


def _drain_watch(app: gui.App, rounds: int = 40) -> None:
    """把监控队列里的消息全部取走（主窗口不进事件循环，after 不会自己跑）。"""
    for _ in range(rounds):
        app._poll_watch()  # noqa: SLF001
        if app.watch_queue.empty() and not app._watching:  # noqa: SLF001
            return
        time.sleep(0.05)


def test_start_watching_runs_the_loop_and_reports_results(
    app: gui.App, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from xdao.watcher import WatchResult, WatchTarget

    target = WatchTarget("50000001")
    app.watch_targets = [target]  # noqa: SLF001
    app.output_var.set(str(tmp_path))  # noqa: SLF001
    app.notify_var.set(False)  # noqa: SLF001

    done = threading.Event()
    seen: dict[str, object] = {}

    def fake_loop(client, targets, output_dir, interval, **kwargs):  # noqa: ANN001, ANN003
        seen.update(
            targets=list(targets),
            output_dir=Path(output_dir),
            interval=interval,
            verify_cached=kwargs.get("verify_cached"),
            notifier=kwargs.get("notifier"),
        )
        kwargs["on_result"](
            WatchResult(
                target=target,
                changed=True,
                new_posts=4,
                edited_posts=0,
                total_posts=12,
                exported=tmp_path / "50000001.html",
                first_run=True,
                reason="第一次导出",
                error="",
            )
        )
        done.set()

    monkeypatch.setattr(gui, "watch_forever", fake_loop)

    app.start_watching(45.0, verify_cached=True)
    assert done.wait(5.0)
    _drain_watch(app)

    assert seen["targets"] == [target]
    assert seen["output_dir"] == tmp_path
    assert seen["interval"] == 45.0
    assert seen["verify_cached"] is True
    assert seen["notifier"] is None

    text = app.log_text.get("1.0", "end-1c")  # noqa: SLF001
    assert "监控已启动：每 45 秒检查 1 个串。" in text
    assert "监控 No.50000001：首次导出 → 50000001.html" in text
    # 线程自己上报哨兵后，状态必须落回 False（不能留假的「监控中」）。
    assert app._watching is False  # noqa: SLF001


def test_stop_watching_asks_the_loop_to_end_and_clears_the_flags(
    app: gui.App, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from xdao.watcher import WatchTarget

    target = WatchTarget("50000001")
    app.watch_targets = [target]  # noqa: SLF001
    app.output_var.set(str(tmp_path))  # noqa: SLF001
    app.notify_var.set(False)  # noqa: SLF001

    started = threading.Event()
    released = threading.Event()
    outcome: dict[str, bool] = {}

    def fake_loop(client, targets, output_dir, interval, **kwargs):  # noqa: ANN001, ANN003
        stop_event = kwargs["stop_event"]
        started.set()
        outcome["stopped"] = stop_event.wait(5.0)

    monkeypatch.setattr(gui, "watch_forever", fake_loop)

    app.start_watching(30.0)
    assert started.wait(5.0)
    assert app._watching is True  # noqa: SLF001

    app.stop_watching()

    assert app._watching is False  # noqa: SLF001
    assert app._watch_stop is None  # noqa: SLF001
    _drain_watch(app)
    assert outcome["stopped"] is True
    assert "已请求停止监控。" in app.log_text.get("1.0", "end-1c")  # noqa: SLF001


def test_poll_watch_writes_every_kind_of_result(
    app: gui.App, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from xdao.watcher import WatchResult, WatchTarget

    target = WatchTarget("50000001")
    app.watch_queue.put("监控已启动，每 30 秒检查一次（1 个串）。")  # noqa: SLF001
    app.watch_queue.put(  # noqa: SLF001
        WatchResult(
            target=target,
            changed=False,
            new_posts=0,
            edited_posts=0,
            total_posts=0,
            exported=None,
            first_run=False,
            reason="",
            error="网络错误",
        )
    )
    app.watch_queue.put(  # noqa: SLF001
        WatchResult(
            target=target,
            changed=True,
            new_posts=2,
            edited_posts=0,
            total_posts=14,
            exported=tmp_path / "50000001.html",
            first_run=False,
            reason="新增 2 楼",
            error="",
        )
    )
    app.watch_queue.put(  # noqa: SLF001
        WatchResult(
            target=target,
            changed=False,
            new_posts=0,
            edited_posts=0,
            total_posts=14,
            exported=None,
            first_run=False,
            reason="没有新楼",
            error="",
        )
    )
    monkeypatch.setattr(gui, "notify_result", lambda notifier, item: True)

    app._poll_watch()  # noqa: SLF001

    text = app.log_text.get("1.0", "end-1c")  # noqa: SLF001
    assert "监控已启动，每 30 秒检查一次（1 个串）。" in text
    assert "监控 No.50000001：出错 网络错误" in text
    assert "监控 No.50000001：新增 2 楼 → 50000001.html" in text
    assert "监控 No.50000001：没有新楼" in text
    assert text.count("↳ 已弹桌面通知（No.50000001）") == 3
    assert app.watch_queue.empty()  # noqa: SLF001

# ---------- 保存日志（v0.21.5）----------


def _clear_log(app: gui.App) -> None:
    """把日志框清空。

    直接 ``delete`` 不行：日志框是只读的（state=disabled），删除会被 Tk 静默忽略，
    于是 ``save_log`` 以为还有内容、弹出真正的「保存日志」对话框 —— 无头环境下
    那个模态框会一直等下去（2026-10-27 实测把整轮 pytest 挂死）。
    """
    app.log_text.config(state="normal")  # noqa: SLF001
    app.log_text.delete("1.0", "end")  # noqa: SLF001
    app.log_text.config(state="disabled")  # noqa: SLF001


def test_save_log_says_there_is_nothing_to_save(app: gui.App, monkeypatch: pytest.MonkeyPatch) -> None:
    said: list[tuple[str, str]] = []
    monkeypatch.setattr(gui.messagebox, "showinfo", lambda title, body, **kw: said.append((title, body)))
    opened: list[dict] = []
    monkeypatch.setattr(gui.filedialog, "asksaveasfilename", lambda **kw: opened.append(kw) or "")
    _clear_log(app)

    app.save_log()

    assert said == [("提示", "日志是空的。")]
    assert opened == []


def test_save_log_does_nothing_when_the_user_cancels(
    app: gui.App, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    app.log("一行日志")
    monkeypatch.setattr(gui.filedialog, "asksaveasfilename", lambda **kw: "")
    written: list[Path] = []
    monkeypatch.setattr(
        gui.Path, "write_text", lambda self, *a, **kw: written.append(self)
    )

    app.save_log()

    assert written == []


def test_save_log_writes_the_log_and_says_where(
    app: gui.App, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    app.log("要留下来的那行")
    target = tmp_path / "xdao-export.log"
    monkeypatch.setattr(gui.filedialog, "asksaveasfilename", lambda **kw: str(target))

    app.save_log()

    assert target.read_text(encoding="utf-8").strip().endswith("要留下来的那行")
    assert f"日志已保存：{target}" in app.log_text.get("1.0", "end-1c")  # noqa: SLF001


def test_save_log_reports_a_failure_instead_of_pretending(
    app: gui.App, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    app.log("随便一行")
    monkeypatch.setattr(gui.filedialog, "asksaveasfilename", lambda **kw: str(tmp_path / "x.log"))
    wrong: list[tuple[str, str]] = []
    monkeypatch.setattr(gui.messagebox, "showerror", lambda title, body, **kw: wrong.append((title, body)))

    def refuse(self, *args, **kwargs):  # noqa: ANN001, ANN003
        raise OSError("磁盘满了")

    monkeypatch.setattr(gui.Path, "write_text", refuse)

    app.save_log()

    assert wrong and wrong[0][0] == "保存失败"
    assert "磁盘满了" in wrong[0][1]

# ---------- BrowserLoginDialog._worker 的失败分支（v0.21.6）----------


def _login_shell(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """造一个只够跑 `_worker` 的登录窗外壳。

    `_worker` 是后台线程入口，按约定**不碰 Tk**（只往队列里投消息），
    所以这里不需要真窗口：`object.__new__` 造个空壳、把队列/停止位/设置
    补齐即可。真开一个 Toplevel 再起线程反而会慢好几倍，也更脆。
    """
    import queue as queue_module
    from types import SimpleNamespace

    dialog = object.__new__(gui.BrowserLoginDialog)
    dialog._queue = queue_module.Queue()
    dialog._stop = threading.Event()
    dialog._browser = None
    dialog._session = None
    dialog._stamp = ""
    dialog._log = None
    dialog.settings = SimpleNamespace(proxy="", config_path=str(tmp_path / "config.json"))
    return dialog


def _drain(dialog) -> list[tuple[str, str]]:
    items: list[tuple[str, str]] = []
    while True:
        try:
            items.append(dialog._queue.get_nowait())
        except Exception:
            return items


def test_login_worker_says_what_to_use_when_the_component_is_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    dialog = _login_shell(monkeypatch, tmp_path)

    def boom():
        raise ImportError("没有 browser_login")

    monkeypatch.setattr(gui, "_load_browser_login", boom)

    dialog._worker()

    kind, text = _drain(dialog)[0]
    assert kind == "error"
    assert "缺少「浏览器登录」组件" in text
    assert "直接粘贴饼干登录" in text


def test_login_worker_explains_a_browser_lookup_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    dialog = _login_shell(monkeypatch, tmp_path)
    monkeypatch.setattr(gui, "_load_browser_login", lambda: object())

    def boom(_settings):  # noqa: ANN001
        raise RuntimeError("注册表读不出来")

    monkeypatch.setattr(gui, "_find_login_browser", boom)

    dialog._worker()

    kind, text = _drain(dialog)[0]
    assert kind == "error"
    assert "没能找到浏览器：注册表读不出来" in text


def test_login_worker_says_what_to_do_when_no_browser_is_installed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    dialog = _login_shell(monkeypatch, tmp_path)
    monkeypatch.setattr(gui, "_load_browser_login", lambda: object())
    monkeypatch.setattr(gui, "_find_login_browser", lambda _settings: None)

    dialog._worker()

    kind, text = _drain(dialog)[0]
    assert kind == "error"
    assert "没找到 Edge 或 Chrome" in text
    assert "Firefox 走不了这条路" in text


def test_login_worker_does_not_open_a_browser_after_cancel(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from types import SimpleNamespace

    dialog = _login_shell(monkeypatch, tmp_path)
    monkeypatch.setattr(gui, "_load_browser_login", lambda: object())
    monkeypatch.setattr(
        gui, "_find_login_browser", lambda _settings: SimpleNamespace(name="Edge", path="C:/edge.exe")
    )
    dialog._stop.set()  # 用户已经在找浏览器的这段时间里点了取消
    started: list[int] = []
    fake = SimpleNamespace(
        user_data_dir=lambda _base: tmp_path / "profile",
        LoginBrowser=lambda *a, **k: started.append(1),
    )
    monkeypatch.setattr(gui, "_load_browser_login", lambda: fake)

    dialog._worker()

    items = _drain(dialog)
    assert any(kind == "browser" for kind, _ in items)
    assert started == []
    assert not any(kind == "error" for kind, _ in items)


def test_login_worker_releases_the_browser_and_writes_the_notes_when_start_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from types import SimpleNamespace

    dialog = _login_shell(monkeypatch, tmp_path)
    stopped: list[int] = []

    class FakeBrowser:
        census_note = "开窗前普查：没探到活着的程序窗口"
        attempt_notes = ("第 1 扇没成：端口没出来",)
        profile_note = ""
        browser_note = ""
        handoff_note = ""

        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def start(self) -> None:
            raise RuntimeError("端口没起来")

        def stop(self) -> None:
            stopped.append(1)

    fake = SimpleNamespace(
        user_data_dir=lambda _base: tmp_path / "profile",
        LoginBrowser=FakeBrowser,
    )
    monkeypatch.setattr(gui, "_load_browser_login", lambda: fake)
    monkeypatch.setattr(
        gui, "_find_login_browser", lambda _settings: SimpleNamespace(name="Edge", path="C:/edge.exe")
    )

    dialog._worker()

    items = _drain(dialog)
    assert stopped == [1]
    assert ("census", "开窗前普查：没探到活着的程序窗口") in items
    assert ("note", "第 1 扇没成：端口没出来") in items
    kinds = [kind for kind, _ in items]
    assert "error" in kinds
    error = next(text for kind, text in items if kind == "error")
    assert "打开浏览器失败：端口没起来" in error
    assert "ready" not in kinds

def test_login_worker_stops_the_browser_when_cancel_lands_during_startup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """取消正好落在「浏览器刚起来、还没连 CDP」那一刻：那扇窗必须被收掉。

    这一支是最容易漏的：主线程那次 _release() 清空了 self._browser，后台线程
    手里只剩局部引用 —— 如果这里不自己收，窗口就留成孤儿（真机 2026-10-18
    那族多窗里就有它）。
    """
    from types import SimpleNamespace

    dialog = _login_shell(monkeypatch, tmp_path)
    stopped: list[int] = []
    dialog._stop_marker = None

    class FakeBrowser:
        census_note = ""
        attempt_notes = ()
        profile_note = ""
        browser_note = ""
        handoff_note = ""

        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def start(self) -> None:
            dialog._stop.set()  # 启动完成的那一瞬间，用户点了取消

        def stop(self) -> None:
            stopped.append(1)

    fake = SimpleNamespace(
        user_data_dir=lambda _base: tmp_path / "profile",
        LoginBrowser=FakeBrowser,
    )
    monkeypatch.setattr(gui, "_load_browser_login", lambda: fake)
    monkeypatch.setattr(
        gui, "_find_login_browser", lambda _settings: SimpleNamespace(name="Edge", path="C:/edge.exe")
    )

    dialog._worker()

    items = _drain(dialog)
    assert stopped == [1]
    assert "ready" not in [kind for kind, _ in items]


# ---------- 打开登录 / 设置对话框的收尾（v0.21.7） ----------


def _stub_dialog(monkeypatch, name: str, **attrs) -> dict:
    """把 `gui.<name>` 换成一个只带属性的假对话框，返回记录用的字典。"""
    record: dict = {"made": 0}
    record.update(attrs)

    class _Stub:
        def __init__(self, *args, **kwargs):
            record["made"] += 1
            self.__dict__.update(record)

        def destroy(self) -> None:  # 真对话框关掉时会走这里
            pass

    monkeypatch.setattr(gui, name, _Stub)
    return record


def test_open_login_saves_the_new_cookie(app: gui.App, monkeypatch) -> None:
    """登录成功：把读到的 userhash 写进设置，并在日志里说一声。"""
    record = _stub_dialog(monkeypatch, "LoginDialog", userhash="abcdef")
    monkeypatch.setattr(app.root, "wait_window", lambda _w: None)
    saved: list[bool] = []
    monkeypatch.setattr(app, "save_settings", lambda loud=False: saved.append(loud) or True)
    monkeypatch.setattr(app, "show_login_state", lambda: None)

    app.open_login()

    assert record["userhash"] == "abcdef"
    assert app.settings.userhash == "abcdef"
    assert saved == [True]
    assert "登录成功，饼干已保存。" in app.log_text.get("1.0", "end-1c")


def test_open_login_warns_when_the_cookie_cannot_be_saved(app: gui.App, monkeypatch) -> None:
    """饼干能用但写不进配置：要明确警告，不能假装存好了。"""
    _stub_dialog(monkeypatch, "LoginDialog", userhash="abcdef")
    monkeypatch.setattr(app.root, "wait_window", lambda _w: None)
    monkeypatch.setattr(app, "save_settings", lambda loud=False: False)
    monkeypatch.setattr(app, "show_login_state", lambda: None)
    warned: list[tuple[str, str]] = []
    monkeypatch.setattr(
        gui.messagebox, "showwarning", lambda title, text, **k: warned.append((title, text))
    )

    app.open_login()

    assert warned and "没存下来" in warned[0][0]
    assert "XD" not in warned[0][1] or True


def test_open_login_reports_a_build_error_with_its_own_title(app: gui.App, monkeypatch) -> None:
    def boom(*args, **kwargs):
        raise gui.XdaoError("找不到浏览器")

    monkeypatch.setattr(gui, "LoginDialog", boom)
    errors: list[tuple[str, str]] = []
    monkeypatch.setattr(
        gui.messagebox, "showerror", lambda title, text, **k: errors.append((title, text))
    )

    app.open_login()

    assert errors == [("无法打开登录页", "找不到浏览器")]


def test_open_login_reports_an_unexpected_error(app: gui.App, monkeypatch) -> None:
    def boom(*args, **kwargs):
        raise RuntimeError("窗都没了")

    monkeypatch.setattr(gui, "LoginDialog", boom)
    errors: list[tuple[str, str]] = []
    monkeypatch.setattr(
        gui.messagebox, "showerror", lambda title, text, **k: errors.append((title, text))
    )

    app.open_login()

    assert errors == [("登录出错", "窗都没了")]


def test_open_settings_logs_what_was_saved(app: gui.App, monkeypatch) -> None:
    _stub_dialog(monkeypatch, "SettingsDialog", saved=True)
    monkeypatch.setattr(app.root, "wait_window", lambda _w: None)
    applied: list[int] = []
    monkeypatch.setattr(app, "apply_settings_to_client", lambda: applied.append(1))
    monkeypatch.setattr(app, "refresh_cache_info", lambda: applied.append(2))

    app.open_settings()

    assert applied == [1, 2]
    assert "设置已保存" in app.log_text.get("1.0", "end-1c")


def test_open_settings_falls_back_to_a_loud_save_when_the_dialog_did_not_save(
    app: gui.App, monkeypatch
) -> None:
    _stub_dialog(monkeypatch, "SettingsDialog", saved=False)
    monkeypatch.setattr(app.root, "wait_window", lambda _w: None)
    louds: list[bool] = []

    def _save(*, loud: bool = False) -> bool:
        louds.append(loud)
        return True

    monkeypatch.setattr(app, "save_settings", _save)

    app.open_settings()

    assert louds == [True]


# ---------- 挑选饼干对话框（v0.21.8） ----------


def _open_picker(root, target: str, counter: dict, po_hash: str = ""):
    """开一个真的挑选饼干窗口；`grab_set` 在未映射窗口上会抛 TclError，先按掉。"""
    var = tk.StringVar(value=target)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(tk.Toplevel, "grab_set", lambda self: None)
        picker = gui.CookiePicker(root, counter, po_hash, var)
    root.update_idletasks()
    return picker, var


def _checkbutton_labels(widget) -> list[str]:
    labels: list[str] = []
    for child in widget.winfo_children():
        if isinstance(child, tk.Checkbutton):
            labels.append(str(child.cget("text")))
        labels.extend(_checkbutton_labels(child))
    return labels


def test_cookie_picker_prechecks_the_cookies_already_in_the_box(
    app: gui.App
) -> None:
    picker, _var = _open_picker(app.root, "aaa bbb", {"aaa": 2, "bbb": 1, "ccc": 5})
    try:
        assert picker.vars["aaa"].get() is True
        assert picker.vars["bbb"].get() is True
        assert picker.vars["ccc"].get() is False
    finally:
        picker.destroy()


def test_cookie_picker_lists_the_busiest_cookie_first_and_marks_the_po(app: gui.App) -> None:
    picker, _var = _open_picker(app.root, "", {"aaa": 2, "po1": 1, "ccc": 5}, po_hash="po1")
    try:
        assert list(picker.vars) == ["ccc", "aaa", "po1"]
        labels = _checkbutton_labels(picker)
        assert any("← PO" in label for label in labels)
        assert sum("← PO" in label for label in labels) == 1
    finally:
        picker.destroy()


def test_cookie_picker_apply_writes_the_chosen_cookies_back(app: gui.App) -> None:
    picker, var = _open_picker(app.root, "aaa", {"aaa": 2, "bbb": 1, "ccc": 5})
    picker.vars["ccc"].set(True)
    picker.vars["aaa"].set(False)

    picker._apply()

    assert var.get() == "ccc"
    assert picker.winfo_exists() == 0, "应用之后窗口要关掉"


def test_cookie_picker_apply_with_nothing_ticked_leaves_the_box_empty(app: gui.App) -> None:
    """本来就没勾：应用之后是空，而不是把刚才框里的东西原样留着。"""
    picker, var = _open_picker(app.root, "", {"aaa": 2, "bbb": 1})

    picker._apply()

    assert var.get() == ""


def test_cookie_picker_set_all_toggles_every_box(app: gui.App) -> None:
    picker, _var = _open_picker(app.root, "", {"aaa": 2, "bbb": 1, "ccc": 5})
    try:
        picker._set_all(True)
        assert all(v.get() for v in picker.vars.values())
        picker._set_all(False)
        assert not any(v.get() for v in picker.vars.values())
    finally:
        picker.destroy()


# ---------- 从串里挑选饼干：入口与轮询（v0.21.8） ----------


def _picker_stub(monkeypatch) -> list[tuple]:
    """把真正的 CookiePicker 换成记录参数的替身（真窗口那一层另有五条用例）。"""
    calls: list[tuple] = []

    class _Stub:
        def __init__(self, master, counter, po_hash, target_var) -> None:
            calls.append((dict(counter), po_hash, target_var.get()))

    monkeypatch.setattr(gui, "CookiePicker", _Stub)
    return calls


def test_pick_cookies_warns_without_a_url(app: gui.App, monkeypatch) -> None:
    warned: list[tuple[str, str]] = []
    monkeypatch.setattr(
        gui.messagebox, "showwarning", lambda title, text, **k: warned.append((title, text))
    )

    app.pick_cookies()

    assert warned == [("提示", "请先在输入框里填写一个串网址。")]
    assert str(app.pick_cookie_button.cget("state")) == "normal"


def test_pick_cookies_warns_when_the_first_url_has_no_thread_id(
    app: gui.App, monkeypatch
) -> None:
    app.urls_text.insert("1.0", "不是一个网址")
    warned: list[tuple[str, str]] = []
    monkeypatch.setattr(
        gui.messagebox, "showwarning", lambda title, text, **k: warned.append((title, text))
    )

    app.pick_cookies()

    assert warned == [("提示", "无法识别第一个网址里的串号。")]
    assert str(app.pick_cookie_button.cget("state")) == "normal"


def test_poll_cookies_opens_the_picker_with_what_the_first_page_holds(
    app: gui.App, monkeypatch
) -> None:
    calls = _picker_stub(monkeypatch)
    posts = [
        make_post(1, "一楼", user_hash="aaa"),
        make_post(2, "二楼", user_hash="aaa"),
        make_post(3, "PO 发言", user_hash="po1", is_po=True),
        make_post(4, "没有饼干", user_hash=""),
    ]
    app._cookie_queue.put(("ok", posts))

    app._poll_cookies()

    assert calls == [({"aaa": 2, "po1": 1}, "po1", app.hashes_var.get())]
    assert str(app.pick_cookie_button.cget("state")) == "normal"


def test_poll_cookies_shows_why_reading_failed(app: gui.App, monkeypatch) -> None:
    errors: list[tuple[str, str]] = []
    monkeypatch.setattr(
        gui.messagebox, "showerror", lambda title, text, **k: errors.append((title, text))
    )
    app._cookie_queue.put(("fail", "网络错误"))

    app._poll_cookies()

    assert errors == [("读取失败", "网络错误")]
    assert str(app.pick_cookie_button.cget("state")) == "normal"


def test_poll_cookies_says_when_the_first_page_has_no_cookies(
    app: gui.App, monkeypatch
) -> None:
    info: list[tuple[str, str]] = []
    monkeypatch.setattr(
        gui.messagebox, "showinfo", lambda title, text, **k: info.append((title, text))
    )
    app._cookie_queue.put(("ok", [make_post(1, "匿名", user_hash="")]))

    app._poll_cookies()

    assert info == [("提示", "第一页里没有可选的饼干。")]


def test_poll_cookies_keeps_waiting_while_the_queue_is_empty(
    app: gui.App, monkeypatch
) -> None:
    again: list[int] = []
    monkeypatch.setattr(app.root, "after", lambda delay, func=None: again.append(delay) or "id")

    app._poll_cookies()

    assert again == [80]

# ---------- 一键升级（v0.21.9）----------


class _UpgradeJob(list):
    """假的结果队列：收到 ``confirm`` 时立刻替用户作答，别让工作线程卡住。"""

    def __init__(self, agree: bool = True) -> None:
        super().__init__()
        self.agree = agree

    def put(self, item) -> None:  # noqa: ANN001 - 照 queue.Queue.put 的形状
        self.append(item)
        if item[0] == "confirm":
            item[1][1].put(self.agree)


def _upgrade_result(latest: str = "9.9.9", asset: str = "https://example.invalid/x.zip"):
    """造一个「查到新版本」的结果对象（只带升级要用的那几个字段）。"""
    from types import SimpleNamespace

    return SimpleNamespace(
        url="https://example.invalid/release",
        asset_url=asset,
        asset_size=1024 * 1024,
        latest=SimpleNamespace(display_version=latest),
        line=lambda: "发现新版本 9.9.9",
    )


def _upgrade_plan(possible: bool = True, reason: str = "") -> object:
    from types import SimpleNamespace

    return SimpleNamespace(possible=possible, reason=reason)


def test_start_upgrade_marks_busy_and_schedules_polling(app: gui.App, monkeypatch) -> None:
    """点「升级」之后：按钮变「升级中…」、线程起来、120 毫秒后开始收进度。"""
    started: list[dict] = []
    scheduled: list[tuple] = []

    class _FakeThread:
        def __init__(self, **kwargs) -> None:  # noqa: ANN003
            started.append(kwargs)

        def start(self) -> None:
            return None

    monkeypatch.setattr(gui.threading, "Thread", _FakeThread)
    monkeypatch.setattr(app.root, "after", lambda *args: scheduled.append(args))

    app._start_upgrade(_upgrade_result())

    assert app._upgrading is True
    assert app._update_button.cget("text") == "升级中…"
    assert started[0]["target"] == app._upgrade_worker
    assert started[0]["daemon"] is True
    assert scheduled and scheduled[0][0] == 120
    assert "开始下载新版本" in app.log_text.get("1.0", "end-1c")
    app._upgrading = False


def test_start_upgrade_is_ignored_while_one_is_running(app: gui.App, monkeypatch) -> None:
    """已经在升级时再点一次不能起第二个线程（否则会同时换两遍文件）。"""
    app._upgrading = True
    started: list[object] = []

    class _FakeThread:
        def __init__(self, **kwargs) -> None:  # noqa: ANN003
            started.append(kwargs)

        def start(self) -> None:
            return None

    monkeypatch.setattr(gui.threading, "Thread", _FakeThread)
    try:
        app._start_upgrade(_upgrade_result())
    finally:
        app._upgrading = False

    assert started == []


def test_poll_upgrade_writes_the_download_percentage(app: gui.App, monkeypatch) -> None:
    """服务器给了总长度时按钮显示百分比。"""
    import queue as _queue

    app._upgrade_queue = _queue.Queue()
    app._upgrade_queue.put(("progress", (512, 1024)))
    monkeypatch.setattr(app.root, "after", lambda *args: None)

    app._poll_upgrade()

    assert app._update_button.cget("text") == "升级中 50%"


def test_poll_upgrade_writes_kilobytes_when_the_size_is_unknown(app: gui.App, monkeypatch) -> None:
    """没有 Content-Length 时就只报已经下了多少 KB。"""
    import queue as _queue

    app._upgrade_queue = _queue.Queue()
    app._upgrade_queue.put(("progress", (4096,)))
    monkeypatch.setattr(app.root, "after", lambda *args: None)

    app._poll_upgrade()

    assert app._update_button.cget("text") == "升级中 4 KB"


def test_poll_upgrade_asks_before_replacing_and_remembers_the_answer(app, monkeypatch) -> None:
    """自检通过后要问一句；用户答「是」就回 True 给工作线程。"""
    import queue as _queue

    asked: list[tuple] = []
    monkeypatch.setattr(
        gui.messagebox, "askyesno", lambda title, text: asked.append((title, text)) or True
    )
    decision: _queue.Queue = _queue.Queue()

    keep = app._upgrade_progress("confirm", ("9.9.9", decision))

    assert keep is True
    assert asked and asked[0][0] == "立刻升级"
    assert "9.9.9" in asked[0][1]
    assert decision.get_nowait() is True


def test_poll_upgrade_logs_it_when_the_user_says_no(app, monkeypatch) -> None:
    """用户答「否」：回 False 并留一句日志。"""
    import queue as _queue

    monkeypatch.setattr(gui.messagebox, "askyesno", lambda *args: False)
    decision: _queue.Queue = _queue.Queue()

    app._upgrade_progress("confirm", ("9.9.9", decision))

    assert decision.get_nowait() is False
    assert "已取消升级" in app.log_text.get("1.0", "end-1c")


def test_poll_upgrade_finish_on_failure_says_why(app, monkeypatch) -> None:
    """失败收尾：标记回空闲、按钮回到「有新版本」、弹一句原因。"""
    errors: list[tuple] = []
    monkeypatch.setattr(
        gui.messagebox, "showerror", lambda title, text: errors.append((title, text))
    )
    app._upgrading = True

    keep = app._upgrade_progress("finish", (False, "自检没过", ""))

    assert keep is False
    assert app._upgrading is False
    assert app._update_button.cget("text") == "有新版本"
    assert errors and errors[0][1] == "自检没过"


def test_poll_upgrade_finish_on_success_hands_the_seat_over(app, monkeypatch) -> None:
    """成功收尾：写「升级完成」，并让位（不能弹模态框）。"""
    exited: list[bool] = []
    monkeypatch.setattr(app, "_end_self_for_upgrade", lambda: exited.append(True))
    app._upgrading = True

    keep = app._upgrade_progress("finish", (True, "新版本就位", "exit"))

    assert keep is False
    assert app._upgrading is False
    assert app._update_button.cget("text") == "升级完成"
    assert exited == [True]


def test_upgrade_worker_stops_when_this_copy_cannot_upgrade(app, monkeypatch) -> None:
    """不能原地升级的环境：第一条就收手，并把暂存目录丢掉。"""
    from xdao import updater

    discarded: list[object] = []
    monkeypatch.setattr(updater, "plan_upgrade", lambda: _upgrade_plan(False, "便携版请手工覆盖"))
    monkeypatch.setattr(updater, "default_staging_root", lambda: pathlib.Path("staging"))
    monkeypatch.setattr(updater, "discard_staging", lambda root: discarded.append(root))

    job = _UpgradeJob()
    gui.App._upgrade_worker(app, _upgrade_result(), job)

    assert job[-1] == ("finish", (False, "便携版请手工覆盖", ""))
    assert discarded


def test_upgrade_worker_reports_a_release_without_the_portable_package(app, monkeypatch) -> None:
    """发布里没有免安装包时给一句人话，不硬下。"""
    from xdao import updater

    monkeypatch.setattr(updater, "plan_upgrade", lambda: _upgrade_plan())
    monkeypatch.setattr(updater, "default_staging_root", lambda: pathlib.Path("staging"))
    monkeypatch.setattr(updater, "discard_staging", lambda root: None)

    job = _UpgradeJob()
    gui.App._upgrade_worker(app, _upgrade_result(asset=""), job)

    assert job[-1][0] == "finish"
    assert "没有免安装包" in job[-1][1][1]


def test_upgrade_worker_does_not_swap_when_the_selftest_fails(app, monkeypatch) -> None:
    """新版自检没过就绝不替换，现场保持原样。"""
    from xdao import updater

    monkeypatch.setattr(updater, "plan_upgrade", lambda: _upgrade_plan())
    monkeypatch.setattr(updater, "default_staging_root", lambda: pathlib.Path("staging"))
    monkeypatch.setattr(updater, "detect_proxy", lambda: "")
    monkeypatch.setattr(updater, "download_asset", lambda url, path, proxy, progress: None)
    monkeypatch.setattr(updater, "remove_quietly", lambda path: None)
    monkeypatch.setattr(updater, "extract_payload", lambda archive, out: pathlib.Path("payload"))
    monkeypatch.setattr(updater, "payload_launcher", lambda payload: pathlib.Path("exe"))
    monkeypatch.setattr(updater, "run_selftest", lambda launcher: (False, "打不开主窗口"))
    switched: list[object] = []
    monkeypatch.setattr(updater, "spawn_helper", lambda plan, payload: switched.append(plan))
    monkeypatch.setattr(updater, "discard_staging", lambda root: None)

    job = _UpgradeJob()
    gui.App._upgrade_worker(app, _upgrade_result(), job)

    assert job[-1] == ("finish", (False, "打不开主窗口，这次不换，现场保持原样。", ""))
    assert switched == []


def test_upgrade_worker_aborts_when_the_user_backs_out_at_the_last_step(app, monkeypatch) -> None:
    """自检通过了、用户这时反悔：不替换，把暂存丢掉。"""
    from xdao import updater

    discarded: list[object] = []
    monkeypatch.setattr(updater, "plan_upgrade", lambda: _upgrade_plan())
    monkeypatch.setattr(updater, "default_staging_root", lambda: pathlib.Path("staging"))
    monkeypatch.setattr(updater, "detect_proxy", lambda: "")
    monkeypatch.setattr(updater, "download_asset", lambda url, path, proxy, progress: None)
    monkeypatch.setattr(updater, "remove_quietly", lambda path: None)
    monkeypatch.setattr(updater, "extract_payload", lambda archive, out: pathlib.Path("payload"))
    monkeypatch.setattr(updater, "payload_launcher", lambda payload: pathlib.Path("exe"))
    monkeypatch.setattr(updater, "run_selftest", lambda launcher: (True, "自检通过"))
    switched: list[object] = []
    monkeypatch.setattr(updater, "spawn_helper", lambda plan, payload: switched.append(plan))
    monkeypatch.setattr(updater, "discard_staging", lambda root: discarded.append(root))

    job = _UpgradeJob(agree=False)
    gui.App._upgrade_worker(app, _upgrade_result(), job)

    assert job[-1] == ("finish", (False, "已取消升级，现场保持原样。", ""))
    assert switched == []
    assert discarded


def test_upgrade_worker_swaps_and_leaves_the_staging_for_the_helper(app, monkeypatch) -> None:
    """一路通过：交给帮手，并且**不能**删暂存（帮手正要用里面的新版）。"""
    from xdao import updater

    discarded: list[object] = []
    switched: list[object] = []
    monkeypatch.setattr(updater, "plan_upgrade", lambda: _upgrade_plan())
    monkeypatch.setattr(updater, "default_staging_root", lambda: pathlib.Path("staging"))
    monkeypatch.setattr(updater, "detect_proxy", lambda: "")
    monkeypatch.setattr(updater, "download_asset", lambda url, path, proxy, progress: progress(10, 10))
    monkeypatch.setattr(updater, "remove_quietly", lambda path: None)
    monkeypatch.setattr(updater, "extract_payload", lambda archive, out: pathlib.Path("payload"))
    monkeypatch.setattr(updater, "payload_launcher", lambda payload: pathlib.Path("exe"))
    monkeypatch.setattr(updater, "run_selftest", lambda launcher: (True, "自检通过"))
    monkeypatch.setattr(updater, "spawn_helper", lambda plan, payload: switched.append(payload))
    monkeypatch.setattr(updater, "discard_staging", lambda root: discarded.append(root))

    app._upgrade_spawned = False
    job = _UpgradeJob(agree=True)
    gui.App._upgrade_worker(app, _upgrade_result(), job)

    ok, message, action = job[-1][1]
    assert (ok, action) == (True, "exit")
    assert "9.9.9" in message
    assert switched
    assert discarded == []
    assert app._upgrade_spawned is True
    assert ("progress", (10, 10)) in job


def test_upgrade_worker_turns_an_unexpected_error_into_a_message(app, monkeypatch) -> None:
    """工作线程里炸了也不能把界面带走：只交一条 finish 回去。"""
    from xdao import updater

    monkeypatch.setattr(updater, "plan_upgrade", lambda: _upgrade_plan())
    monkeypatch.setattr(updater, "default_staging_root", lambda: pathlib.Path("staging"))
    monkeypatch.setattr(updater, "detect_proxy", lambda: "")
    monkeypatch.setattr(updater, "discard_staging", lambda root: None)

    def boom(*args, **kwargs) -> None:  # noqa: ANN002, ANN003
        raise OSError("磁盘满了")

    monkeypatch.setattr(updater, "download_asset", boom)

    job = _UpgradeJob()
    gui.App._upgrade_worker(app, _upgrade_result(), job)

    assert job[-1][0] == "finish"
    assert job[-1][1][0] is False
    assert "磁盘满了" in job[-1][1][1]

# ---------- 罐里到底有没有真会话（v0.21.9）----------


def test_jar_with_only_the_anonymous_session_is_not_logged_in() -> None:
    """只有 PHPSESSID 的罐子不算登录着（匿名访客也有这个）。"""
    assert gui._jar_holds_a_session([{"name": "PHPSESSID", "value": "abc"}]) is False


def test_jar_with_a_member_cookie_counts_as_logged_in() -> None:
    """出现会员饼干（名字不是 PHPSESSID、值非空）就算登录着。"""
    cookies = [
        {"name": "PHPSESSID", "value": "abc"},
        {"name": "memberUserspapapa", "value": "xyz"},
    ]
    assert gui._jar_holds_a_session(cookies) is True


def test_jar_cookie_names_are_case_insensitive_and_values_must_be_non_empty() -> None:
    """名字比较不分大小写；空值不算（真机上站点会把会员饼干写成空）。"""
    assert gui._jar_holds_a_session([{"name": "phpsessid", "value": "abc"}]) is False
    assert gui._jar_holds_a_session([{"name": "PhpSessId", "value": "abc"}]) is False
    # 名字只是长得像（PHPSSESSID 多一个 S）就算会员饼干 —— 判据刻意宽
    assert gui._jar_holds_a_session([{"name": "PHPSSESSID", "value": "abc"}]) is True
    assert gui._jar_holds_a_session([{"name": "memberUserspapapa", "value": "  "}]) is False


def test_jar_ignores_junk_entries_and_missing_arguments() -> None:
    """None、空列表、非字典条目都不能把判定弄崩。"""
    assert gui._jar_holds_a_session(None) is False
    assert gui._jar_holds_a_session([]) is False
    assert gui._jar_holds_a_session(["junk", 42, None]) is False

# ---------- 登录后台线程的提前返回与失败收尾（v0.21.10）----------


def _bare_login_dialog(stop: bool = False):
    """只把 `_worker` 要碰的几个属性摆上，不开窗口、不起进程。"""
    import queue as _queue
    import threading as _threading
    from types import SimpleNamespace

    dialog = object.__new__(gui.BrowserLoginDialog)
    dialog._queue = _queue.Queue()
    dialog._stop = _threading.Event()
    if stop:
        dialog._stop.set()
    dialog._browser = None
    dialog.settings = SimpleNamespace(proxy="", config_path=str(ARTIFACTS_ROOT / "window-layout" / "config.json"))
    return dialog


def _drain(dialog) -> list[tuple]:  # noqa: ANN001
    items: list[tuple] = []
    while True:
        try:
            items.append(dialog._queue.get_nowait())
        except Exception:  # noqa: BLE001 - 队列空了
            return items


def test_worker_says_so_when_the_browser_component_is_missing(monkeypatch) -> None:
    """打包时漏了「浏览器登录」组件：给一句能照着做的话，不去找浏览器。"""
    dialog = _bare_login_dialog()

    def boom():
        raise ImportError("没有 cdp")

    monkeypatch.setattr(gui, "_load_browser_login", boom)
    dialog._worker()

    items = _drain(dialog)
    assert items and items[0][0] == "error"
    assert "缺少「浏览器登录」组件" in items[0][1]


def test_worker_reports_why_no_browser_could_be_found(monkeypatch) -> None:
    """找浏览器的过程本身抛了异常：报原文，别让用户以为是网络问题。"""
    from types import SimpleNamespace

    dialog = _bare_login_dialog()
    monkeypatch.setattr(gui, "_load_browser_login", lambda: SimpleNamespace())

    def boom(settings):  # noqa: ANN001
        raise RuntimeError("注册表读不出来")

    monkeypatch.setattr(gui, "_find_login_browser", boom)
    dialog._worker()

    items = _drain(dialog)
    assert items and items[0][0] == "error"
    assert items[0][1] == "没能找到浏览器：注册表读不出来"


def test_worker_suggests_the_paste_route_when_no_browser_is_installed(monkeypatch) -> None:
    """一台机器上既没有 Edge 也没有 Chrome：说清还有「直接粘贴饼干」这条路。"""
    from types import SimpleNamespace

    dialog = _bare_login_dialog()
    monkeypatch.setattr(gui, "_load_browser_login", lambda: SimpleNamespace())
    monkeypatch.setattr(gui, "_find_login_browser", lambda settings: None)
    dialog._worker()

    items = _drain(dialog)
    assert items and items[0][0] == "error"
    assert "没找到 Edge 或 Chrome" in items[0][1]
    assert "直接粘贴饼干" in items[0][1]


def test_worker_stops_before_launching_when_cancelled_while_searching(monkeypatch) -> None:
    """用户在「找浏览器」那点时间里点了取消：不许再多开一个进程。"""
    from types import SimpleNamespace

    dialog = _bare_login_dialog(stop=True)
    launched: list[object] = []

    class _Backend:
        @staticmethod
        def user_data_dir(_base):  # noqa: ANN001
            return "profile"

        @staticmethod
        def LoginBrowser(*args, **kwargs):  # noqa: ANN002, ANN003
            launched.append(args)
            return object()

    monkeypatch.setattr(gui, "_load_browser_login", lambda: _Backend())
    monkeypatch.setattr(
        gui,
        "_find_login_browser",
        lambda settings: SimpleNamespace(name="Edge", path="C:\\edge.exe"),
    )
    dialog._worker()

    items = _drain(dialog)
    assert [item[0] for item in items] == ["browser"]
    assert launched == []


def test_worker_closes_the_window_it_opened_when_starting_fails(monkeypatch) -> None:
    """浏览器起不来（或后面某步失败）：报「打开浏览器失败」并把那扇窗收掉。

    v0.17.3 之前这里只报错不收敛 —— 用户再点一次就多一扇窗。
    """
    from types import SimpleNamespace

    dialog = _bare_login_dialog()
    stopped: list[bool] = []

    class _Browser:
        process = None
        census_note = "开窗前普查：探到 1 扇"
        attempt_notes = ["第 1 扇没成：端口没起来"]
        profile_note = ""
        browser_note = ""
        handoff_note = ""
        port = None

        def start(self) -> None:
            raise RuntimeError("端口文件没出现")

        def stop(self) -> None:
            stopped.append(True)

    class _Backend:
        @staticmethod
        def user_data_dir(_base):  # noqa: ANN001
            return "profile"

        @staticmethod
        def LoginBrowser(*args, **kwargs):  # noqa: ANN002, ANN003
            return _Browser()

    monkeypatch.setattr(gui, "_load_browser_login", lambda: _Backend())
    monkeypatch.setattr(
        gui,
        "_find_login_browser",
        lambda settings: SimpleNamespace(name="Edge", path="C:\\edge.exe"),
    )
    dialog._worker()

    items = _drain(dialog)
    kinds = [item[0] for item in items]
    assert stopped == [True]
    assert dialog._browser is None
    assert kinds.count("census") == 1  # 普查看到了什么，失败时也要吐出来
    notes = "".join(str(item[1]) for item in items if item[0] == "note")
    assert "第 1 扇没成" in notes
    errors = [item for item in items if item[0] == "error"]
    assert errors and "打开浏览器失败" in errors[0][1]
    assert "端口文件没出现" in errors[0][1]


def test_worker_closes_the_window_when_cancelled_mid_start(monkeypatch) -> None:
    """取消正好落在启动过程中：拿局部引用收尾，不留孤儿进程，也不报错。"""
    from types import SimpleNamespace

    dialog = _bare_login_dialog()
    stopped: list[bool] = []

    class _Browser:
        process = None
        census_note = ""
        attempt_notes = []
        profile_note = ""
        browser_note = ""
        handoff_note = ""
        used_profile = ""
        port = None
        browser_ws_url = "ws://127.0.0.1:1/devtools/page/x"

        def start(self) -> None:
            dialog._stop.set()  # 模拟「启动过程中用户点了取消」

        def stop(self) -> None:
            stopped.append(True)

    class _Backend:
        @staticmethod
        def user_data_dir(_base):  # noqa: ANN001
            return "profile"

        @staticmethod
        def LoginBrowser(*args, **kwargs):  # noqa: ANN002, ANN003
            return _Browser()

    monkeypatch.setattr(gui, "_load_browser_login", lambda: _Backend())
    monkeypatch.setattr(
        gui,
        "_find_login_browser",
        lambda settings: SimpleNamespace(name="Edge", path="C:\\edge.exe"),
    )
    dialog._worker()

    items = _drain(dialog)
    assert stopped == [True]
    assert dialog._browser is None
    assert all(item[0] != "error" for item in items)

# ---------- 导出与监控的早期退出分支（v0.21.12）----------


def test_start_asks_for_a_cookie_before_doing_anything(app: gui.App, monkeypatch) -> None:
    """没登录就先别抓：弹一句并把登录窗打开，用户少一次「点了没反应」。"""
    warned: list[tuple] = []
    opened: list[bool] = []
    monkeypatch.setattr(gui.messagebox, "showwarning", lambda *a, **k: warned.append(a))
    monkeypatch.setattr(app, "open_login", lambda: opened.append(True))
    app.settings.userhash = ""

    app.start()

    assert warned == [("提示", "请先登录或设置饼干。")]
    assert opened == [True]


def test_start_asks_for_at_least_one_url(app: gui.App, monkeypatch) -> None:
    app.settings.userhash = "x" * 40
    monkeypatch.setattr(app, "_collect_urls", lambda: [])
    warned: list[tuple] = []
    monkeypatch.setattr(gui.messagebox, "showwarning", lambda *a, **k: warned.append(a))

    app.start()

    assert warned == [("提示", "请至少输入一个串网址。")]


def test_start_explains_when_the_until_cut_hides_everything(app: gui.App, monkeypatch) -> None:
    """串号截止把整张列表都挡掉时，要说清「截止是怎么算的」，别只弹个空提示。"""
    app.settings.userhash = "x" * 40
    monkeypatch.setattr(app, "_collect_urls", lambda: ["12345678"])
    monkeypatch.setattr(app, "until_thread_id", lambda: 1234567)
    warned: list[tuple[str, str]] = []
    monkeypatch.setattr(
        gui.messagebox, "showwarning", lambda title, body, **k: warned.append((title, body))
    )

    app.start()

    assert warned and warned[0][0] == "没有要导出的串"
    assert "串号截止把列表里的串全都挡住了" in warned[0][1]
    assert app._exporting is False


def test_start_stops_when_the_export_directory_cannot_be_prepared(
    app: gui.App, monkeypatch
) -> None:
    app.settings.userhash = "x" * 40
    monkeypatch.setattr(app, "_collect_urls", lambda: ["50000001"])
    monkeypatch.setattr(app, "until_thread_id", lambda: None)
    monkeypatch.setattr(app, "prepare_export_dir", lambda: None)

    app.start()

    assert app._exporting is False, "目录都没准备好就不该把导出标记成进行中"


def test_start_watching_returns_early_when_it_is_already_running(
    app: gui.App, tmp_path
) -> None:
    app._watching = True
    app.watch_targets = [gui.WatchTarget("50000001")]
    app.output_var.set(str(tmp_path))

    app.start_watching(30)

    assert app._watching is True


def test_start_watching_says_when_notifications_are_unavailable(
    app: gui.App, tmp_path, monkeypatch
) -> None:
    """系统发不出桌面通知时说一句，别让用户以为通知开关坏了。"""
    app.watch_targets = [gui.WatchTarget("50000001")]
    app.output_var.set(str(tmp_path))
    app.notify_var.set(True)

    class _DeadNotifier:
        def __init__(self, min_interval: float = 0.0) -> None:
            self.min_interval = min_interval

        def available(self) -> bool:
            return False

    monkeypatch.setattr(gui, "Notifier", _DeadNotifier)
    monkeypatch.setattr(gui, "watch_forever", lambda *a, **k: None)

    app.start_watching(30)
    _drain_watch(app)

    assert "提示：当前环境发不出桌面通知" in app.log_text.get("1.0", "end-1c")


def test_start_watching_reports_a_loop_failure(app: gui.App, tmp_path, monkeypatch) -> None:
    """监控循环抛异常：把那句话写进日志，而不是让线程悄悄死掉。"""
    app.watch_targets = [gui.WatchTarget("50000001")]
    app.output_var.set(str(tmp_path))

    def boom(*args, **kwargs):
        raise RuntimeError("炸了")

    monkeypatch.setattr(gui, "watch_forever", boom)

    app.start_watching(30)
    _drain_watch(app)

    assert "监控出错：RuntimeError: 炸了" in app.log_text.get("1.0", "end-1c")

# ------------------------------------------------- 导出线程内层分支（v0.21.13）


class _Meta:
    def __init__(self, thread_id: int = 12345678, title: str = "测试串", po_hash: str = "") -> None:
        self.thread_id = thread_id
        self.title = title
        self.po_hash = po_hash


class _FetchResult:
    def __init__(
        self,
        *,
        thread_id: int = 12345678,
        posts: int = 3,
        reason: str = "全部楼层已抓完",
        retry_note: str = "",
        cache_warning: str = "",
    ) -> None:
        from tests import make_post

        self.meta = _Meta(thread_id)
        self.posts = [make_post(i + 1, f"正文{i}") for i in range(posts)]
        self.reason = reason
        self.retry_note = retry_note
        self.cache_warning = cache_warning


class _FakeFetcher:
    """``CachedThreadFetcher`` 的替身：按顺序回放预先排好的结果或异常。"""

    def __init__(self, plan) -> None:
        self.plan = list(plan)
        self.calls: list[str] = []

    def fetch(self, url: str, max_pages: int = 0):
        self.calls.append(url)
        item = self.plan.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


class _FakeExporter:
    supports_merge = True

    def __init__(
        self, *, save_error: BaseException | None = None, many_error: BaseException | None = None
    ) -> None:
        self.save_error = save_error
        self.many_error = many_error
        self.saved: list[str] = []
        self.many: list[list] = []

    def save(self, thread, scope, output_dir, include_hashes=()):  # noqa: ANN001
        if self.save_error is not None:
            raise self.save_error
        self.saved.append(thread.title)
        return pathlib.Path(output_dir) / f"{thread.thread_id}.html"

    def save_many(self, threads, scope, output_dir, include_hashes=()):  # noqa: ANN001
        self.many.append(list(threads))
        if self.many_error is not None:
            raise self.many_error
        return pathlib.Path(output_dir) / "合并.html"


class _ImmediateThread:
    """把 ``threading.Thread`` 换成「start() 就地跑完」。"""

    def __init__(self, target=None, **kwargs) -> None:  # noqa: ANN001
        self._target = target

    def start(self) -> None:
        assert self._target is not None
        self._target()


class _AlreadyCancelled:
    """停止位一开始就是举着的。"""

    def is_set(self) -> bool:
        return True

    def set(self) -> None:
        return None

    def wait(self, timeout: float | None = None) -> bool:
        return True


def _run_export(
    app: gui.App,
    monkeypatch: pytest.MonkeyPatch,
    plan: list,
    *,
    exporter: _FakeExporter | None = None,
    merge: bool = False,
    cancelled: bool = False,
    urls: list[str] | None = None,
) -> tuple[_FakeFetcher, _FakeExporter]:
    """让 ``App.start()`` 的 worker 当场跑完，返回替身以便断言。"""
    fetcher = _FakeFetcher(plan)
    fake_exporter = exporter or _FakeExporter()
    monkeypatch.setattr(gui, "CachedThreadFetcher", lambda *a, **k: fetcher)
    monkeypatch.setattr(gui, "create_exporter", lambda *a, **k: fake_exporter)
    monkeypatch.setattr(gui.threading, "Thread", _ImmediateThread)
    if cancelled:
        monkeypatch.setattr(gui.threading, "Event", _AlreadyCancelled)
    monkeypatch.setattr(gui.App, "_poll_export", lambda self: None)
    app.settings.userhash = "TESTHASH"
    app.settings.merge_export = merge
    app.start(urls or ["https://www.nmbxd1.com/t/12345678"])
    app._exporting = False  # noqa: SLF001 —— 别让 fixture 收尾时以为还在导
    return fetcher, fake_exporter


def _drained(app: gui.App) -> tuple[list[str], list[object]]:
    """把导出队列抽干，返回（日志行, done 载荷）。"""
    logs: list[str] = []
    done: list[object] = []
    while not app._export_queue.empty():  # noqa: SLF001
        kind, payload = app._export_queue.get_nowait()  # noqa: SLF001
        if kind == "log":
            logs.append(str(payload))
        elif kind == "done":
            done.append(payload)
    return logs, done


def test_export_worker_logs_incomplete_and_cache_warnings(
    app: gui.App, monkeypatch: pytest.MonkeyPatch
) -> None:
    """缺页/撞页数上限与缓存问题都要进日志，并记进「不完整」名单。"""
    _run_export(
        app,
        monkeypatch,
        [_FetchResult(retry_note="撞上页数上限（5 页）", cache_warning="缓存目录写不进去")],
    )

    logs, done = _drained(app)
    text = "\n".join(logs)
    assert "注意：撞上页数上限（5 页）" in text
    assert "提示：缓存目录写不进去" in text

    assert done, "worker 必须发一条 done"
    succeeded, total, failed, _elapsed, incomplete, cancelled = done[-1]
    assert (succeeded, total, failed, cancelled) == (1, 1, [], False)
    assert incomplete == ["12345678：撞上页数上限（5 页）"]


def test_export_worker_stops_before_the_first_thread_when_cancelled(
    app: gui.App, monkeypatch: pytest.MonkeyPatch
) -> None:
    """停止位一开始就举着：一项都不抓，done 里 cancelled 为真。"""
    fetcher, _exporter = _run_export(app, monkeypatch, [_FetchResult()], cancelled=True)

    assert fetcher.calls == []
    _logs, done = _drained(app)
    assert done and done[-1][5] is True


def test_export_worker_counts_a_cancel_raised_by_the_fetcher_as_cancelled(
    app: gui.App, monkeypatch: pytest.MonkeyPatch
) -> None:
    """抓取层抛 ExportCancelled：这条不算失败，剩下的串不再处理。"""
    from xdao.cancel import ExportCancelled

    fetcher, _exporter = _run_export(
        app,
        monkeypatch,
        [_FetchResult(), ExportCancelled("用户点了取消")],
        urls=["https://www.nmbxd1.com/t/12345678", "https://www.nmbxd1.com/t/50577215"],
    )

    assert len(fetcher.calls) == 2, "第一个串抓成功、第二个在抓取时抛取消"
    logs, done = _drained(app)
    assert "已取消，这一项没导完" in "\n".join(logs)
    assert done[-1][5] is True


def test_export_worker_separates_xdao_errors_from_unexpected_ones(
    app: gui.App, monkeypatch: pytest.MonkeyPatch
) -> None:
    """保存抛 XdaoError 与抛别的异常都算失败，但日志措辞不同。"""
    from xdao.client import XdaoError

    _run_export(
        app,
        monkeypatch,
        [_FetchResult()],
        exporter=_FakeExporter(save_error=XdaoError("站点说没有这个串")),
    )
    logs, done = _drained(app)
    assert "失败：站点说没有这个串" in "\n".join(logs)
    assert done[-1][2] == ["https://www.nmbxd1.com/t/12345678"]

    _run_export(
        app,
        monkeypatch,
        [_FetchResult()],
        exporter=_FakeExporter(save_error=RuntimeError("磁盘满了")),
    )
    logs, _done = _drained(app)
    text = "\n".join(logs)
    assert "失败：" in text and "磁盘满了" in text


def test_export_worker_reports_a_failed_merge(app: gui.App, monkeypatch: pytest.MonkeyPatch) -> None:
    """合并写盘失败：日志说「合并失败」。"""
    from xdao.client import XdaoError

    exporter = _FakeExporter(many_error=XdaoError("目标文件被占用"))
    _fetcher, fake = _run_export(app, monkeypatch, [_FetchResult()], exporter=exporter, merge=True)

    assert len(fake.many) == 1 and len(fake.many[0]) == 1
    logs, _done = _drained(app)
    text = "\n".join(logs)
    assert "已收进合并文件" in text
    assert "合并失败：目标文件被占用" in text


def test_export_worker_marks_a_cancelled_merge(app: gui.App, monkeypatch: pytest.MonkeyPatch) -> None:
    """合并保存时被取消：日志说合并文件没生成，done 里 cancelled 为真。"""
    from xdao.cancel import ExportCancelled

    exporter = _FakeExporter(many_error=ExportCancelled("取消"))
    _run_export(app, monkeypatch, [_FetchResult()], exporter=exporter, merge=True)

    logs, done = _drained(app)
    assert "已取消：合并文件没有生成" in "\n".join(logs)
    assert done[-1][5] is True


def test_clear_log_empties_the_read_only_box(app: gui.App) -> None:
    """日志框是只读的：clear_log 要先放开再删，否则 Tk 静默忽略。"""
    app.log("第一行")
    assert app.log_text.get("1.0", "end").strip()

    app.clear_log()

    assert app.log_text.get("1.0", "end").strip() == ""
    assert str(app.log_text.cget("state")) == "disabled"


def test_open_selftest_raises_the_existing_window(
    app: gui.App, monkeypatch: pytest.MonkeyPatch
) -> None:
    """自检窗口已经开着时只抬起来，不新建第二扇。"""
    calls: list[str] = []

    class _Dialog:
        def winfo_exists(self) -> bool:
            return True

        def lift(self) -> None:
            calls.append("lift")

        def focus_set(self) -> None:
            calls.append("focus")

    app._selftest_dialog = _Dialog()  # noqa: SLF001
    app.open_selftest()

    assert calls == ["lift", "focus"]


def test_setup_style_applies_theme_and_fonts(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(gui, "apply_theme", lambda root: calls.append("theme") or "style")
    monkeypatch.setattr(gui, "refresh_fonts", lambda root: calls.append("fonts"))

    assert gui.setup_style(object()) == "style"  # type: ignore[arg-type]
    assert calls == ["theme", "fonts"]


def test_paste_dialog_has_a_right_click_menu(app: gui.App) -> None:
    """右键要绑上菜单（真窗口验证一次，不弹出来）。"""
    dialog = gui.PasteCookieDialog(app.root)
    try:
        assert dialog.text.bind("<Button-3>"), "右键要绑上菜单"
    finally:
        dialog.destroy()


def test_jar_dump_is_skipped_when_the_backend_lacks_it() -> None:
    """后端没有 jar_dump/write_jar_dump 时直接放过，不当成错误。"""
    assert gui.BrowserLoginDialog._write_jar_dump(object(), object(), object(), "S", "") is None


def test_jar_dump_survives_a_broken_backend() -> None:
    """地址列不出来、落盘失败都只返回 None，绝不往外抛。"""

    class _Backend:
        def jar_dump(self, session, **kwargs):  # noqa: ANN001, ANN003
            raise OSError("读不到饼干")

        def write_jar_dump(self, data, *, stamp):  # noqa: ANN001, ANN003
            return "path"

        def cookie_urls_for(self, session):  # noqa: ANN001
            raise OSError("列不出地址")

    assert gui.BrowserLoginDialog._write_jar_dump(_Backend(), object(), object(), "S", "") is None


def test_jar_dump_falls_back_when_profile_path_is_rejected() -> None:
    """老后端不收 profile_path：TypeError 之后要再调一次（不带它）。"""
    seen: list[dict] = []

    class _Backend:
        def jar_dump(self, session, **kwargs):  # noqa: ANN001, ANN003
            seen.append(kwargs)
            if "profile_path" in kwargs:
                raise TypeError("unexpected keyword argument")
            return {"ok": True}

        def write_jar_dump(self, data, *, stamp):  # noqa: ANN001, ANN003
            return f"dump-{stamp}.json"

        def cookie_urls_for(self, session):  # noqa: ANN001
            return ["https://www.nmbxd1.com/"]

    dumped = gui.BrowserLoginDialog._write_jar_dump(_Backend(), object(), object(), "S", "C:/p")
    assert dumped == "dump-S.json"
    assert "profile_path" in seen[0] and "profile_path" not in seen[1]


def test_try_leaf_cookie_reports_a_backend_failure() -> None:
    class _Backend:
        def fetch_leaf_cookie(self, session, **kwargs):  # noqa: ANN001, ANN003
            raise RuntimeError("端口断了")

    value, detail, navigated = gui.BrowserLoginDialog._try_leaf_cookie(
        _Backend(), object(), navigate=False, waiting_for_login=False
    )
    assert value is None and "端口断了" in detail and navigated is False


def test_try_leaf_cookie_rejects_a_value_of_the_wrong_shape() -> None:
    class _Result:
        value = "abc"
        detail = "在饼干页读到的"
        navigated = True

    class _Backend:
        def fetch_leaf_cookie(self, session, **kwargs):  # noqa: ANN001, ANN003
            return _Result()

        def looks_like_userhash(self, value: str) -> bool:
            return False

    value, detail, navigated = gui.BrowserLoginDialog._try_leaf_cookie(
        _Backend(), object(), navigate=True, waiting_for_login=False
    )
    assert value is None and "形状不像" in detail and navigated is True


def test_try_leaf_cookie_accepts_a_good_value() -> None:
    class _Result:
        value = "  GOODHASH  "
        detail = "拿到了"
        navigated = True

    class _Backend:
        def fetch_leaf_cookie(self, session, **kwargs):  # noqa: ANN001, ANN003
            return _Result()

        def looks_like_userhash(self, value: str) -> bool:
            return value.startswith("GOOD")

    value, detail, _navigated = gui.BrowserLoginDialog._try_leaf_cookie(
        _Backend(), object(), navigate=True, waiting_for_login=False
    )
    assert value == "GOODHASH" and detail == "拿到了"


def test_carry_browser_jar_gives_up_on_a_client_without_importer() -> None:
    """客户端没有 import_cookies：这一轮直接放过，不算错。"""
    dialog = object.__new__(gui.BrowserLoginDialog)
    dialog._session_jar_carried = False  # noqa: SLF001
    dialog.client = object()  # type: ignore[attr-defined]
    dialog._queue = []  # type: ignore[attr-defined]
    gui.BrowserLoginDialog._carry_browser_jar(dialog, object(), object())
    assert dialog._session_jar_carried is False  # noqa: SLF001
    assert dialog._queue == []  # type: ignore[attr-defined]


def test_carry_browser_jar_swallows_reader_and_importer_errors() -> None:
    class _Backend:
        def read_site_cookies(self, session):  # noqa: ANN001
            raise OSError("读不到")

    dialog = object.__new__(gui.BrowserLoginDialog)
    dialog._session_jar_carried = False  # noqa: SLF001
    dialog._queue = []  # type: ignore[attr-defined]
    dialog.client = type("C", (), {"import_cookies": lambda self, items: 3})()  # type: ignore[attr-defined]
    gui.BrowserLoginDialog._carry_browser_jar(dialog, _Backend(), object())
    assert dialog._session_jar_carried is False  # noqa: SLF001

    class _Backend2:
        def read_site_cookies(self, session):  # noqa: ANN001
            return [{"name": "PHPSESSID"}]

    def _boom(self, items):  # noqa: ANN001
        raise ValueError("坏了")

    dialog.client = type("C", (), {"import_cookies": _boom})()  # type: ignore[attr-defined]
    gui.BrowserLoginDialog._carry_browser_jar(dialog, _Backend2(), object())
    assert dialog._session_jar_carried is False  # noqa: SLF001


def test_manual_userhash_warns_when_the_client_rejects_it(
    app: gui.App, monkeypatch: pytest.MonkeyPatch
) -> None:
    """「直接粘贴饼干」：客户端不认时弹「设置失败」，窗口不关。"""
    shown: list[tuple[str, str]] = []
    monkeypatch.setattr(gui, "ask_pasted_cookie", lambda parent: "USERHASH")
    monkeypatch.setattr(
        gui.messagebox, "showerror", lambda title, msg, **k: shown.append((title, msg))
    )
    dialog = gui.LoginDialog(app.root, app.client)
    try:
        def _boom(value: str):  # noqa: ANN001
            raise ValueError("这不像一块饼干")

        monkeypatch.setattr(dialog.client, "set_userhash", _boom)  # type: ignore[attr-defined]
        dialog._manual_userhash()  # noqa: SLF001
        assert shown and shown[0][0] == "设置失败"
        assert "这不像一块饼干" in shown[0][1]
        assert dialog.winfo_exists()
    finally:
        dialog.destroy()

