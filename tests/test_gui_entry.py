"""图形界面入口的兜底测试。

实测过：打包版弹「Unhandled exception in script」对话框。命令行入口已经堵住了，
界面这边也必须堵 —— 尤其是打包版（--windowed）**没有 stderr**，
任何漏出去的异常都会变成 PyInstaller 的错误对话框。

文件末尾两组是 PDF 纸张/边距的接线：前一组是纯逻辑（不开窗口），
后一组要真的建 Tk 窗口（设置对话框、导出主流程），没有显示环境时跳过。
"""

from __future__ import annotations

import gc
import json
import queue
import threading
import time
import tkinter as tk
from pathlib import Path
from types import SimpleNamespace

import pytest

import xdao.gui as gui
from xdao import pdf_opts
from xdao.settings import AppSettings


class FakeRoot:
    """最小可用的 Tk 替身：只记录 after/mainloop 有没有被调用。"""

    def __init__(self) -> None:
        self.after_calls: list[tuple] = []
        self.mainloop_called = False
        self.report_callback_exception = None

    def after(self, delay, func=None, *args):
        self.after_calls.append((delay, func, args))
        return "after#1"

    def mainloop(self) -> None:
        self.mainloop_called = True

    def destroy(self) -> None:  # pragma: no cover - 替身
        pass


def test_run_reports_fatal_when_tk_cannot_start(monkeypatch):
    """连 tk.Tk() 都建不出来时，要给对话框而不是抛出未处理异常。"""
    calls: list[tuple[str, BaseException]] = []

    def fake_report(title, exc):
        calls.append((title, exc))

    def boom() -> None:
        raise RuntimeError("模拟：没有可用的显示环境")

    monkeypatch.setattr(gui.tk, "Tk", boom)
    monkeypatch.setattr(gui, "_report_fatal", fake_report)

    with pytest.raises(SystemExit) as excinfo:
        gui.run()

    assert excinfo.value.code == 1
    assert calls and calls[0][0] == "启动界面失败"
    assert "没有可用的显示环境" in str(calls[0][1])


def test_run_reports_fatal_when_app_init_fails(monkeypatch):
    """App 初始化失败（例如配置损坏）也要走同一条兜底路径。"""
    calls: list[tuple[str, BaseException]] = []
    monkeypatch.setattr(gui.tk, "Tk", FakeRoot)
    monkeypatch.setattr(gui, "_report_fatal", lambda title, exc: calls.append((title, exc)))

    def boom(root) -> None:
        raise ValueError("模拟：配置文件损坏")

    monkeypatch.setattr(gui, "App", boom)

    with pytest.raises(SystemExit) as excinfo:
        gui.run()

    assert excinfo.value.code == 1
    assert calls and "配置文件损坏" in str(calls[0][1])



def test_run_exits_quietly_when_ctrl_c_breaks_the_event_loop(monkeypatch) -> None:
    """Ctrl+C 打断 mainloop：安静退出，不抛、不弹「Unhandled exception」。"""
    roots: list[object] = []

    class _Root:
        def __init__(self) -> None:
            self.report_callback_exception = None
            self.protocol_calls: list[tuple] = []
            roots.append(self)

        def protocol(self, name, func) -> None:  # noqa: ANN001
            self.protocol_calls.append((name, func))

        def mainloop(self) -> None:
            raise KeyboardInterrupt

    class _App:
        def __init__(self, root) -> None:  # noqa: ANN001
            self.root = root

        def on_close(self) -> None:
            pass

    monkeypatch.setattr(gui.tk, "Tk", _Root)
    monkeypatch.setattr(gui, "App", _App)

    gui.run()  # 不该往外抛

    assert roots and roots[0].protocol_calls[0][0] == "WM_DELETE_WINDOW"

def test_describe_export_failure_explains_permission_errors(tmp_path):
    """写盘被拒时不能只甩一句 errno —— 要说清哪个目录、往哪换、还能查什么。"""
    target = tmp_path / "输出目录" / "某串.html"
    text = gui.describe_export_failure(PermissionError(13, "Permission denied", str(target)))
    assert "PermissionError" in text
    assert "输出目录" in text  # 指到具体目录，而不是笼统的"导出失败"
    assert "文档" in text  # 给出一个大概率能写的地方
    assert "安全软件" in text or "受控文件夹" in text  # 换目录仍失败时的原因


def test_describe_export_failure_passes_other_errors_through():
    """非权限类异常保持原样，别加无关的建议。"""
    text = gui.describe_export_failure(ValueError("模板占位符写错了"))
    assert text == "ValueError: 模板占位符写错了"


def test_describe_export_failure_explains_a_rejected_cookie():
    """接口回「必须登入领取饼干」时，要告诉用户去重新登录，而不是只贴原文。"""
    from xdao.client import XdaoError

    text = gui.describe_export_failure(XdaoError("必须登入领取饼干后才可以访问"))
    assert "必须登入领取饼干后才可以访问" in text  # 原文保留
    assert "登录" in text  # 指明下一步动作
    assert "饼干" in text


def test_describe_login_failure_says_what_to_do_for_a_session_that_did_not_stick():
    """实测过：邮箱登录后只看到「未找到可用的饼干」。

    真相是跳转提示页被当成了饼干列表。文案必须说清是"这次登录没生效"，
    并给出两个能马上做的动作（重登 / 用浏览器登录）。
    """
    title, body = gui.describe_login_failure(
        "登录后没能进入用户系统（X 岛把请求弹回了登录页。）请重新登录：确认密码正确、"
        "验证码是刚刷新出来的那一张。"
    )
    assert title == "登录没有生效"
    assert "重新" in body
    assert "用浏览器登录" in body
    assert "未找到可用的饼干" not in title  # 不能再把话盖回那句误导性的提示


def test_describe_login_failure_keeps_the_captcha_and_password_hints():
    title, body = gui.describe_login_failure("验证码错误，请点击验证码图片刷新后重试。")
    assert "验证码" in title
    assert "新" in body  # 提示验证码已经换了一张

    title2, body2 = gui.describe_login_failure("密码错误，请重新输入。")
    assert "密码" in title2
    assert "用浏览器登录" in body2


def test_describe_login_failure_passes_unknown_errors_through():
    title, body = gui.describe_login_failure("网络错误：连接被重置")
    assert title == "登录失败"
    assert body == "网络错误：连接被重置"


def test_run_installs_a_callback_exception_handler(monkeypatch):
    """Tk 回调异常不能依赖 stderr —— 打包版没有 stderr。"""
    root = FakeRoot()
    logged: list[str] = []
    shown: list[tuple] = []

    class FakeApp:
        def __init__(self, r) -> None:
            assert r is root

        def log(self, message: str) -> None:
            logged.append(message)

    monkeypatch.setattr(gui.tk, "Tk", lambda: root)
    monkeypatch.setattr(gui, "App", FakeApp)
    monkeypatch.setattr(
        gui.messagebox, "showerror", lambda title, text: shown.append((title, text))
    )

    gui.run()

    assert root.mainloop_called is True
    handler = root.report_callback_exception
    assert callable(handler), "必须给 root 装回调异常处理器"

    try:
        raise KeyError("模拟：某个按钮的回调炸了")
    except KeyError:
        import sys

        handler(*sys.exc_info())

    assert logged, "界面出错时要写进日志面板"
    assert "模拟：某个按钮的回调炸了" in logged[0]
    assert shown and "KeyError" in shown[0][1]


# ---- 「直接粘贴饼干登录」：把整段 cookie 贴进来就行，解析交给 browser_login ----


class _FakePasteClient:
    def __init__(self) -> None:
        self.userhash: str | None = None

    def set_userhash(self, value: str) -> None:
        self.userhash = value


class FakeLoginDialog:
    """只提供 _manual_userhash 用得到的那几样东西，免得真开一个 Tk 窗口。"""

    def __init__(self) -> None:
        self.client = _FakePasteClient()
        self.userhash = ""
        self.destroyed = False
        self.released = 0
        self.grabs = 0

    def grab_release(self) -> None:
        self.released += 1

    def grab_set(self) -> None:
        self.grabs += 1

    def winfo_exists(self) -> bool:
        return True

    def destroy(self) -> None:
        self.destroyed = True


class FakeBrowserLoginDialog:
    """顶掉真子窗口：_open_browser_login 只从它身上读 userhash / failure。"""

    made: list["FakeBrowserLoginDialog"] = []

    def __init__(self, master, client, settings, log=None) -> None:
        self.master = master
        self.client = client
        self.settings = settings
        self.log = log  # v0.13.26：真对话框拿它把「领饼干那条路」的结论写进运行日志
        self.userhash: str | None = None
        self.failure = ""
        FakeBrowserLoginDialog.made.append(self)


def _run_open_browser_login(monkeypatch, *, failure: str, userhash: str | None = None):
    """把「用浏览器登录」的收尾逻辑跑一遍，收集日志与关窗动作。"""
    destroyed: list[bool] = []
    holder = SimpleNamespace(
        client=object(),
        settings=object(),
        userhash=None,
        grab_release=lambda: None,
        grab_set=lambda: None,
        wait_window=lambda dialog: None,
        winfo_exists=lambda: True,
        destroy=lambda: destroyed.append(True),
        logs=[],
    )
    # holder 就是「主窗口」：_open_browser_login 会通过 self.app.log 写运行日志，
    # 这里把 app 指回自己，日志就落在 holder.logs 里。
    holder.app = holder
    holder.log = holder.logs.append
    FakeBrowserLoginDialog.made.clear()

    def fake_dialog(master, client, settings, log=None):
        dialog = FakeBrowserLoginDialog(master, client, settings, log=log)
        dialog.failure = failure
        dialog.userhash = userhash
        return dialog

    monkeypatch.setattr(gui, "BrowserLoginDialog", fake_dialog)
    gui.LoginDialog._open_browser_login(holder)
    return holder, holder.logs, destroyed


def test_browser_login_failure_lands_in_the_run_log(monkeypatch):
    """失败原因不能只留在子窗口那行小字里。

    出问题时贴出来的运行日志里一条登录记录都没有 ——
    错误只写进了对话框的 status_var，点掉就没了。子窗口关掉之后补写一笔，
    用户下次贴日志就能带上真正的原因。
    """
    holder, logs, _ = _run_open_browser_login(
        monkeypatch, failure="Edge 刚起来就退出了（退出码 21）"
    )

    assert holder.userhash is None, "没成功就不该把饼干当成功"
    assert len(logs) == 1
    assert logs[0].endswith("Edge 刚起来就退出了（退出码 21）")
    assert "浏览器登录没成" in logs[0]
    # v0.13.26：子窗口还要拿主窗口的日志出口，好把「领饼干那条路」的结论当场写进去
    # （遇到问题时贴的是运行日志，那条路的原话以前只活在子窗口那行小字里）。
    assert FakeBrowserLoginDialog.made[-1].log is holder.log


def test_browser_login_success_does_not_log_a_failure(monkeypatch):
    """成功那条路只写登录成功，不许多写一句「没成」。"""
    holder, logs, _ = _run_open_browser_login(monkeypatch, failure="", userhash="ABCD1234")

    assert holder.userhash == "ABCD1234"
    assert logs == []


def test_browser_login_without_an_app_still_works(monkeypatch):
    """老的调用方没有 app：拿不到日志对象也不许炸，登录本身照常。"""
    logs: list[str] = []
    holder = SimpleNamespace(
        client=object(),
        settings=object(),
        userhash=None,
        app=None,  # 旧调用方没传主窗口
        grab_release=lambda: None,
        grab_set=lambda: None,
        wait_window=lambda dialog: None,
        winfo_exists=lambda: True,
        destroy=lambda: None,
        log=lambda message: logs.append(message),
    )

    def fake_dialog(master, client, settings, log=None):
        dialog = FakeBrowserLoginDialog(master, client, settings, log=log)
        dialog.failure = "起不来"
        return dialog

    monkeypatch.setattr(gui, "BrowserLoginDialog", fake_dialog)
    FakeBrowserLoginDialog.made.clear()
    gui.LoginDialog._open_browser_login(holder)

    assert holder.userhash is None
    assert logs == [], "没有主窗口就写不了日志，但也不许抛异常"
    assert FakeBrowserLoginDialog.made[-1].log is None, "没有主窗口时不该凭空造一个日志出口"


def _paste(monkeypatch, text):
    """把 _manual_userhash 跑一遍，收集它弹过什么框。

    2026-10-01 起粘贴走的是自家窗口（``gui.PasteCookieDialog``，提示文字会换行、
    还会写清去哪儿抄 userhash），摘 userhash 的活在那个窗口里做完了 —— 所以这里
    顶掉的是 ``ask_pasted_cookie``，不再是 ``simpledialog.askstring``。
    """
    asked: list[object] = []
    warnings: list[tuple] = []
    errors: list[tuple] = []

    def fake_ask(master):
        asked.append(master)
        return text

    monkeypatch.setattr(gui, "ask_pasted_cookie", fake_ask)
    monkeypatch.setattr(gui.messagebox, "showwarning", lambda *a, **k: warnings.append(a))
    monkeypatch.setattr(gui.messagebox, "showerror", lambda *a, **k: errors.append(a))

    dialog = FakeLoginDialog()
    gui.LoginDialog._manual_userhash(dialog)
    return dialog, asked, warnings, errors


def test_manual_userhash_uses_the_paste_window(monkeypatch):
    """粘贴这条路要开自家窗口，并且把窗口交出来的 userhash 设进去。"""
    dialog, asked, warnings, errors = _paste(monkeypatch, "ABCD1234")

    assert asked == [dialog], "必须把对话框自己当父窗口传给粘贴窗口"
    assert dialog.client.userhash == "ABCD1234"
    assert dialog.userhash == "ABCD1234"
    assert dialog.destroyed is True
    assert warnings == [] and errors == []


def test_manual_userhash_gives_the_grab_back(monkeypatch):
    """本窗口 grab_set 过，子窗口要用鼠标：先放开，关掉再收回来。"""
    dialog, _, _, _ = _paste(monkeypatch, "ABCD1234")

    assert dialog.released == 1
    assert dialog.grabs == 1, "子窗口关掉之后要把 grab 收回来，否则主窗口点不动"


def test_manual_userhash_does_nothing_when_the_paste_window_is_cancelled(monkeypatch):
    dialog, _, warnings, errors = _paste(monkeypatch, None)

    assert dialog.client.userhash is None
    assert dialog.destroyed is False
    assert warnings == [] and errors == []


def test_paste_window_explains_why_and_where_to_copy(monkeypatch):
    """提示语本身就是这次改动的交付物（讲清「为什么弹出来的浏览器是空的」）。

    要讲清两件事：①程序不碰你自己浏览器的数据；②去哪儿抄 userhash（含 F12 那条路）。
    """
    why = gui.PASTE_WHY
    steps = gui.PASTE_STEPS

    assert "不会去翻你自己浏览器的数据" in why
    assert "密码" in why and "插件" in why
    for needle in ("F12", "应用程序 / Application", "Cookie", "userhash", "nmbxd1"):
        assert needle in steps, needle
    assert "复制" in steps


# ---------------------------------------------------------------------------
# PDF 纸张/边距：界面上那几个小工具（纯逻辑，不用开窗口）
# ---------------------------------------------------------------------------


def test_choice_key_maps_every_label_back_to_its_key():
    """下拉框显示的是中文标签，存回配置的必须是 pdf_opts 认得的取值键。"""
    keys = list(pdf_opts.PAPER_LABELS)
    for key in keys:
        assert gui._choice_key(pdf_opts.PAPER_LABELS, keys, pdf_opts.PAPER_LABELS[key]) == key


def test_choice_key_falls_back_to_the_first_entry():
    """下拉框里出现了表外的文字（配置被手改坏）时取默认项，不能抛。"""
    keys = list(pdf_opts.MARGIN_PRESETS)
    assert gui._choice_key(pdf_opts.MARGIN_PRESETS, keys, "窄边距（乱写的）") == keys[0]


def test_choice_text_shows_the_label_and_survives_a_broken_config():
    assert gui._choice_text(pdf_opts.PAPER_LABELS, "a4", "兜底") == pdf_opts.PAPER_LABELS["a4"]
    assert gui._choice_text(pdf_opts.PAPER_LABELS, "b5", "兜底") == "兜底"


def test_scale_text_never_shows_float_noise():
    """配置里存的是字符串，界面要显示 1 / 0.8，而不是 1.0 / 0.8000000000000001。"""
    assert gui._scale_text("1.0") == "1"
    assert gui._scale_text(0.8) == "0.8"
    assert gui._scale_text("坏值") == "1"
    assert gui._scale_text(None) == "1"


def test_pdf_scale_text_is_validated_by_pdf_opts():
    """校验规则只有 pdf_opts 一份：越界/认不出的输入保留原值，不悄悄改成 1。"""
    assert gui._pdf_scale_text(" 1.2 ", "1") == "1.2"
    assert gui._pdf_scale_text("0.5", "1") == "0.5"
    assert gui._pdf_scale_text("2.5", "1") == "1"
    assert gui._pdf_scale_text("abc", "1") == "1"
    assert gui._pdf_scale_text("", "1") == "1"


def test_pdf_margin_mm_text_keeps_good_values_and_original_text():
    assert gui._pdf_margin_mm_text(" 22.5 ", "") == "22.5"
    assert gui._pdf_margin_mm_text("0", "18") == "0"
    assert gui._pdf_margin_mm_text("51", "18") == "18", "超出 0~50 毫米"
    assert gui._pdf_margin_mm_text("abc", "18") == "18"
    assert gui._pdf_margin_mm_text("", "18") == "", "清空＝改回用左边的预设"


def test_pdf_page_ranges_text_normalizes_and_keeps_good_values():
    assert gui._pdf_page_ranges_text("1-3, 5", "") == "1-3,5"
    assert gui._pdf_page_ranges_text("5-2", "1-3") == "1-3"
    assert gui._pdf_page_ranges_text("", "1-3") == ""


# ---------------------------------------------------------------------------
# 设置对话框：真窗口用例（无显示环境时跳过，理由在白名单里）
# ---------------------------------------------------------------------------


def _make_root(timeout: float = 5.0) -> tk.Tk:
    """建根窗口；偶发 ``TclError`` 是环境抖动，重试几秒再放弃。"""
    deadline = time.monotonic() + timeout
    while True:
        try:
            root = tk.Tk()
        except tk.TclError:  # pragma: no cover - 取决于运行环境
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.2)
            continue
        root.geometry("900x700+3000+3000")
        root.update()
        return root


def _close(widget) -> None:
    try:
        if widget.winfo_exists():
            widget.destroy()
    except tk.TclError:  # pragma: no cover - 窗口已经没了
        pass


@pytest.fixture
def dialog_root():
    try:
        root = _make_root()
    except tk.TclError as exc:  # pragma: no cover - 取决于运行环境
        pytest.skip(f"没有可用的显示环境：{exc}")
    try:
        yield root
    finally:
        _close(root)


def _open_settings(root, settings) -> gui.SettingsDialog:
    dialog = gui.SettingsDialog(root, settings)
    root.update()
    return dialog


def test_settings_dialog_starts_from_a_clean_config(dialog_root, artifacts_dir):
    """老配置（没有 pdf_* 键）打开时应当是「跟随网页样式 + 100% + 有背景」。"""
    settings = AppSettings(_path=artifacts_dir / "config.json")
    dialog = _open_settings(dialog_root, settings)
    try:
        assert dialog.pdf_paper_var.get() == pdf_opts.PAPER_LABELS["default"]
        assert dialog.pdf_orientation_var.get() == pdf_opts.ORIENTATION_LABELS["portrait"]
        assert dialog.pdf_margin_var.get() == pdf_opts.MARGIN_PRESETS["default"]
        assert dialog.pdf_margin_mm_var.get() == ""
        assert dialog.pdf_scale_var.get() == "1"
        assert dialog.pdf_pages_var.get() == ""
        assert dialog.pdf_background_var.get() is True
    finally:
        _close(dialog)


def test_settings_dialog_saves_pdf_options_without_exporting(
    dialog_root, artifacts_dir, monkeypatch
):
    """点「保存」只写配置：选了什么存什么，且绝不触发一次真导出。"""
    settings = AppSettings(_path=artifacts_dir / "config.json")
    exported: list[tuple] = []
    monkeypatch.setattr(gui, "create_exporter", lambda *a, **k: exported.append((a, k)))

    dialog = _open_settings(dialog_root, settings)
    try:
        dialog.pdf_paper_var.set(pdf_opts.PAPER_LABELS["a4"])
        dialog.pdf_orientation_var.set(pdf_opts.ORIENTATION_LABELS["landscape"])
        dialog.pdf_margin_var.set(pdf_opts.MARGIN_PRESETS["wide"])
        dialog.pdf_margin_mm_var.set("15.5")
        dialog.pdf_scale_var.set("0.75")
        dialog.pdf_pages_var.set("1-3, 7")
        dialog.pdf_background_var.set(False)
        dialog._save()
    finally:
        _close(dialog)

    assert dialog.saved is True
    assert settings.pdf_paper == "a4"
    assert settings.pdf_orientation == "landscape"
    assert settings.pdf_margin == "wide"
    assert settings.pdf_margin_mm == "15.5"
    assert settings.pdf_scale == "0.75"
    assert settings.pdf_page_ranges == "1-3,7"
    assert settings.pdf_background is False
    assert exported == []

    saved = json.loads((artifacts_dir / "config.json").read_text(encoding="utf-8"))
    assert saved["pdf_paper"] == "a4"
    assert saved["pdf_scale"] == "0.75"
    assert saved["pdf_background"] is False


def test_settings_dialog_keeps_good_values_when_the_input_is_bad(dialog_root, artifacts_dir):
    """手滑填错时保留原值：一次误输入不该把调好的设置抹掉。"""
    settings = AppSettings(_path=artifacts_dir / "config.json")
    settings.pdf_scale = "0.8"
    settings.pdf_page_ranges = "1-3"
    settings.pdf_margin_mm = "18"

    dialog = _open_settings(dialog_root, settings)
    try:
        dialog.pdf_scale_var.set("飞快")
        dialog.pdf_pages_var.set("5-2")
        dialog.pdf_margin_mm_var.set("999")
        dialog._save()
    finally:
        _close(dialog)

    assert settings.pdf_scale == "0.8"
    assert settings.pdf_page_ranges == "1-3"
    assert settings.pdf_margin_mm == "18"


# ---------------------------------------------------------------------------
# 正文字号：设置对话框那一个框（导出成品的一半）
# ---------------------------------------------------------------------------


def test_settings_dialog_starts_from_the_saved_font_size(dialog_root, artifacts_dir):
    settings = AppSettings(_path=artifacts_dir / "config.json")
    settings.export_font_size = 20

    dialog = _open_settings(dialog_root, settings)
    try:
        assert dialog.export_font_var.get() == "20"
    finally:
        _close(dialog)


def test_settings_dialog_saves_the_font_size(dialog_root, artifacts_dir):
    """改了字号要写进配置——不然用户拖完重启又回到 14。"""
    settings = AppSettings(_path=artifacts_dir / "config.json")

    dialog = _open_settings(dialog_root, settings)
    try:
        dialog.export_font_var.set("22")
        dialog._save()
    finally:
        _close(dialog)

    assert settings.export_font_size == 22
    saved = json.loads((artifacts_dir / "config.json").read_text(encoding="utf-8"))
    assert saved["export_font_size"] == 22


@pytest.mark.parametrize("bad", ("abc", "", "9", "29", "0", "-4"))
def test_settings_dialog_keeps_a_good_font_size_when_the_input_is_bad(
    dialog_root, artifacts_dir, bad
):
    settings = AppSettings(_path=artifacts_dir / "config.json")
    settings.export_font_size = 16

    dialog = _open_settings(dialog_root, settings)
    try:
        dialog.export_font_var.set(bad)
        dialog._save()
    finally:
        _close(dialog)

    assert settings.export_font_size == 16


# ---------------------------------------------------------------------------
# 正文字号：导出主流程（界面这条路）
# ---------------------------------------------------------------------------


def test_gui_export_passes_font_size_to_the_exporter(artifacts_dir, monkeypatch):
    """界面导出要把配置里的字号交给导出器，并在日志里说一声。"""
    _patch_gui_export(monkeypatch)
    recorded: list[dict] = []
    monkeypatch.setattr(
        gui, "create_exporter", lambda *a, **k: recorded.append(k) or _RecordingExporter()
    )
    settings = AppSettings(_path=artifacts_dir / "config.json")
    settings.userhash = "TESTHASH"
    settings.export_font_size = 20

    app = _FakeApp(settings, artifacts_dir / "out")
    gui.App.start(app, ["https://www.nmbxd1.com/t/50000001"])

    assert [kwargs["font_size"] for kwargs in recorded] == [20]
    assert any("正文字号：20 磅" in line for line in app.logs)


def test_gui_export_keeps_quiet_about_the_font_size_for_plain_text(
    artifacts_dir, monkeypatch
):
    """TXT / Markdown 是纯文本，没有字号可言 —— 别打那一行误导人。"""
    _patch_gui_export(monkeypatch)
    recorded: list[dict] = []
    monkeypatch.setattr(
        gui, "create_exporter", lambda *a, **k: recorded.append(k) or _RecordingExporter()
    )
    settings = AppSettings(_path=artifacts_dir / "config.json")
    settings.userhash = "TESTHASH"

    app = _FakeApp(settings, artifacts_dir / "out")
    monkeypatch.setattr(_FakeApp, "current_format", lambda self: "txt")
    gui.App.start(app, ["https://www.nmbxd1.com/t/50000001"])

    assert recorded, "导出主流程没跑起来"
    assert not any("正文字号" in line for line in app.logs)


# ---------------------------------------------------------------------------
# 界面字号：顶栏那档 + 重建界面
# ---------------------------------------------------------------------------


@pytest.fixture
def live_app(dialog_root, artifacts_dir, monkeypatch):
    """真的把主界面建起来（隐藏窗口），用完拆掉并把全局比例还原。"""
    from xdao import theme

    _isolate_settings(monkeypatch, artifacts_dir)
    original = theme.FONT_SCALE
    try:
        app = gui.App(dialog_root)
    except tk.TclError as exc:  # pragma: no cover - 取决于运行环境
        pytest.skip(f"没有可用的显示环境：{exc}")
    try:
        yield app
    finally:
        theme.set_font_scale(original)
        try:
            app.root.destroy()
        except tk.TclError:  # pragma: no cover - 已经拆了
            pass


def test_header_offers_every_font_scale_choice(live_app):
    from xdao import theme

    assert live_app._font_scale_var.get() == "标准"
    box = live_app._font_scale_box
    assert tuple(box.cget("values")) == tuple(label for label, _ in theme.FONT_SCALE_CHOICES)


def test_switching_the_font_scale_rebuilds_the_window(live_app):
    """换档要立刻生效：全局比例、下拉框显示、配置、日志、左栏宽度都得跟上。"""
    from xdao import theme

    live_app.urls_text.insert("1.0", "https://www.nmbxd1.com/t/50000001")

    assert live_app.switch_font_scale(theme.font_scale_for_label("更大")) is True

    assert theme.FONT_SCALE == pytest.approx(1.3)
    assert live_app.settings.ui_font_scale == pytest.approx(1.3)
    assert live_app._font_scale_var.get() == "更大"
    assert theme.settings_column_width() > theme.SETTINGS_COLUMN_WIDTH
    assert "界面字号已切换为「更大」" in live_app.log_text.get("1.0", "end-1c")
    # 重建界面时输入框里的串网址要还在（不然换个字号就得重贴一遍）。
    assert "50000001" in live_app.urls_text.get("1.0", "end-1c")


def test_switching_to_the_same_scale_changes_nothing(live_app):
    assert live_app.switch_font_scale(1.0) is False


def test_font_scale_switch_is_refused_while_exporting(live_app, monkeypatch):
    """导出/监控进行中不许重建界面：进度与日志会错位。"""
    from xdao import theme

    calls = _messagebox_calls(monkeypatch)
    live_app._exporting = True
    live_app._font_scale_var.set("特大")

    live_app._on_font_scale_selected()

    assert calls and calls[0][0] == "showinfo"
    assert live_app._font_scale_var.get() == "标准"  # 选项回滚
    assert theme.FONT_SCALE == 1.0


# ---------------------------------------------------------------------------
# 忙闲：导出与监控互斥（v0.17.1）
# ---------------------------------------------------------------------------


def test_is_busy_tracks_both_flags(live_app):
    assert live_app.is_busy() is False
    live_app._exporting = True
    assert live_app.is_busy() is True
    live_app._exporting = False
    live_app._watching = True
    assert live_app.is_busy() is True


def test_start_is_refused_while_watching(live_app, monkeypatch):
    """监控在跑时点「开始导出」要直接拦下，不能两条写盘的路同时走。"""
    calls = _messagebox_calls(monkeypatch)
    live_app._watching = True
    live_app.urls_text.insert("1.0", "https://www.nmbxd1.com/t/50000001")

    live_app.start()

    assert calls and calls[0][0] == "showinfo"
    assert calls[0][1] == "正在忙"
    assert "监控" in calls[0][2]
    # 一点都没开：没有停止位、没有线程、界面也没被切到「取消导出」。
    assert live_app._exporting is False
    assert live_app._export_stop is None


def test_watching_is_refused_while_exporting(live_app, monkeypatch):
    calls = _messagebox_calls(monkeypatch)
    live_app._exporting = True

    live_app.start_watching(60.0)

    assert calls and calls[0][1] == "正在忙"
    assert "导出" in calls[0][2]
    assert live_app._watching is False


def test_checking_watch_once_is_refused_while_exporting(live_app, monkeypatch):
    calls = _messagebox_calls(monkeypatch)
    live_app._exporting = True

    live_app.check_watch_once()

    assert calls and calls[0][1] == "正在忙"


def test_watch_thread_end_clears_the_watching_flag(live_app):
    """监控线程收手后必须有人把标志落回 False，否则界面一直写着「监控中」。"""
    live_app._watching = True
    live_app._watch_notifier = object()  # 收尾时应该被清掉

    live_app.watch_queue.put(gui._WATCH_STOPPED)
    live_app._poll_watch()

    assert live_app._watching is False
    assert live_app._watch_notifier is None


# ---------------------------------------------------------------------------
# 导出主流程：跑真的 ``App.start()``，但线程/抓取/导出全换替身（不用开窗口）
# ---------------------------------------------------------------------------


class _SyncThread:
    """把界面里起的后台线程换成「start() 就地跑」，用例才好断言。"""

    def __init__(self, target=None, daemon=None, **kwargs):
        self._target = target

    def start(self) -> None:
        self._target()


def _messagebox_calls(monkeypatch) -> list[tuple[str, str, str]]:
    """记下 ``messagebox`` 弹了什么（函数名, 标题, 正文），并把弹窗换成不阻塞的假函数。"""
    calls: list[tuple[str, str, str]] = []
    for name in ("showwarning", "showerror", "showinfo"):
        monkeypatch.setattr(
            gui.messagebox,
            name,
            lambda title, message, _name=name, **kw: calls.append((_name, title, message)),
        )
    return calls



class _FakeWidget:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def config(self, **kwargs) -> None:
        self.calls.append(("config", kwargs))

    def configure(self, **kwargs) -> None:
        self.calls.append(("configure", kwargs))

    def start(self, interval=None) -> None:
        self.calls.append(("start", interval))

    def stop(self) -> None:
        self.calls.append(("stop", None))

    def set_value(self, value, maximum) -> None:
        self.calls.append(("set_value", value, maximum))


class _FakeVar:
    def __init__(self, value) -> None:
        self._value = value

    def get(self):
        return self._value

    def set(self, value) -> None:
        self._value = value


class _FakeApp:
    """只实现 ``App.start()`` 会用到的那点东西，替代整个真窗口。"""

    def __init__(self, settings, output_dir: Path) -> None:
        self.settings = settings
        self._exporting = False
        self._output_dir = None
        self._export_queue: queue.Queue = queue.Queue()
        self._last_failed: list[str] = []
        self.client = SimpleNamespace(image_cache_dir=None)
        self.start_button = _FakeWidget()
        self.retry_button = _FakeWidget()
        self._export_stop = None
        self.progress_bar = _FakeWidget()
        self.progress_var = _FakeVar("")
        self.scope_var = _FakeVar("all")
        self.use_cache_var = _FakeVar(False)
        self.root = SimpleNamespace(after=lambda delay, func=None: None)
        self.logs: list[str] = []
        self._output = output_dir

    def log(self, message: str) -> None:
        self.logs.append(message)

    def prepare_export_dir(self):
        return self._output

    def persist_prefs(self) -> None:
        pass

    def apply_settings_to_client(self) -> None:
        pass

    def current_format(self) -> str:
        return "pdf"

    def parse_hashes(self) -> bool:
        return False

    def max_pages_limit(self) -> int:
        """导出范围：0＝不限页数（与真 App 上那条路径同一个语义）。"""
        return 0

    def until_thread_id(self):
        """串号截止：None＝不截止。"""
        return None

    def current_cache_dir(self):
        return self._output

    def current_image_mode(self) -> str:
        return "embed"

    def _log_image_mode_note(self) -> None:
        """真窗口在 folder + 非 HTML 时会补一句日志；假窗口里由专门用例覆盖。"""

    def _resolved_output_dir(self) -> Path:
        """``App._finish()`` 要把「文件在哪」写进汇总弹窗。"""
        return self._output

    def cancel_export(self) -> None:
        """``App._set_export_controls()`` 会把按钮指到 ``self.cancel_export``。"""
        pass

    def _refuse_when_busy(self, doing: str) -> bool:
        """真 ``App`` 上这条会弹「正在忙」；假窗口永远不忙，放行。"""
        return False

    def start(self, urls=None) -> None:
        """``App._set_export_controls()`` 收尾时会把按钮指回 ``self.start``。"""
        pass

    def refresh_cache_info(self) -> None:
        pass

    def _set_export_controls(self, *, running: bool) -> None:
        """``App.start()`` 会切换按钮文案；假窗口里只记下状态，按钮语义另有用例。"""
        self._exporting = running

    def _poll_export(self) -> None:
        """``App.start()`` 结尾会 ``root.after(50, self._poll_export)``；这里什么都不做。"""
        pass


class _FakeAppWithRange(_FakeApp):
    """``_FakeApp`` 的导出范围版本：页数上限与串号截止由用例指定。

    真 ``App`` 上这两个值来自两个输入框（见下面那组 ``_max_pages_text`` /
    ``_until_thread_text`` / ``App.max_pages_limit`` / ``App.until_thread_id``
    的用例）；这里只把它们喂进 ``start()``，核对界面这条路接得对不对。
    """

    def __init__(self, settings, output_dir: Path, *, max_pages: int = 0, until=None) -> None:
        super().__init__(settings, output_dir)
        self._max_pages = max_pages
        self._until = until

    def max_pages_limit(self) -> int:
        return self._max_pages

    def until_thread_id(self):
        return self._until


class _RecordingExporter:
    """只记下被要求写哪几个串，不真写文件。"""

    def __init__(self) -> None:
        self.saved: list[object] = []

    def save(self, thread, scope, path, include_hashes=False):
        self.saved.append(thread)
        return Path(path) / "结果.html"


def test_gui_export_passes_pdf_options_to_the_exporter(artifacts_dir, monkeypatch):
    """界面导出时必须把设置里那套纸张/边距交给导出器，并在日志里说一声。"""
    settings = AppSettings(_path=artifacts_dir / "config.json")
    settings.userhash = "TESTHASH"
    settings.pdf_paper = "a4"
    settings.pdf_margin = "narrow"
    settings.pdf_scale = "0.8"
    settings.pdf_page_ranges = "1-3"
    recorded: list[dict] = []

    class _FakeExporter:
        def save(self, thread, scope, path, include_hashes=False):
            return path / "结果.pdf"

    def fake_create_exporter(*args, **kwargs):
        recorded.append(kwargs)
        return _FakeExporter()

    class _FakeFetcher:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def fetch(self, url, **kwargs):
            return SimpleNamespace(
                meta=SimpleNamespace(thread_id="12345678", title="测试串", po_hash="PO"),
                posts=[],
                reason="测试用抓取结果",
                retry_note="",
                cache_warning="",
            )

    monkeypatch.setattr(gui, "create_exporter", fake_create_exporter)
    monkeypatch.setattr(gui, "CachedThreadFetcher", _FakeFetcher)
    monkeypatch.setattr(gui.threading, "Thread", _SyncThread)

    app = _FakeApp(settings, artifacts_dir / "out")
    gui.App.start(app, ["https://www.nmbxd1.com/t/12345678"])

    assert recorded, "start() 必须真的走到导出器那一步"
    assert recorded[0]["pdf_options"] == pdf_opts.from_settings(settings)
    assert recorded[0]["pdf_options"].paper == "a4"
    assert recorded[0]["pdf_options"].margin == "narrow"
    assert any(line.startswith("PDF 设置：") for line in app.logs)


def test_gui_export_does_not_mention_pdf_options_when_they_are_default(
    artifacts_dir, monkeypatch
):
    """全默认（跟随网页样式）时不要多打一行，老配置的日志保持原样。"""
    settings = AppSettings(_path=artifacts_dir / "config.json")
    settings.userhash = "TESTHASH"

    class _FakeExporter:
        def save(self, thread, scope, path, include_hashes=False):
            return path / "结果.html"

    monkeypatch.setattr(gui, "create_exporter", lambda *a, **k: _FakeExporter())
    monkeypatch.setattr(
        gui,
        "CachedThreadFetcher",
        lambda *a, **k: SimpleNamespace(
            fetch=lambda url, **kwargs: SimpleNamespace(
                meta=SimpleNamespace(thread_id="1", title="测试串", po_hash="PO"),
                posts=[],
                reason="测试用抓取结果",
                retry_note="",
                cache_warning="",
            )
        ),
    )
    monkeypatch.setattr(gui.threading, "Thread", _SyncThread)

    app = _FakeApp(settings, artifacts_dir / "out")
    gui.App.start(app, ["https://www.nmbxd1.com/t/12345678"])

    assert not any("PDF 设置" in line for line in app.logs)
    assert any(line.startswith("导出目录：") for line in app.logs)


# ---------------------------------------------------------------------------
# 导出范围：页数上限 / 串号截止
# ---------------------------------------------------------------------------


def _patch_gui_export(monkeypatch) -> list[tuple[str, dict]]:
    """把界面的抓取与导出换成替身，返回每次 fetch 的 ``(网址, 参数)``。"""
    calls: list[tuple[str, dict]] = []

    class RecordingFetcher:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def fetch(self, url, **kwargs):
            calls.append((url, kwargs))
            return SimpleNamespace(
                meta=SimpleNamespace(thread_id="50000001", title="测试串", po_hash="PO"),
                posts=[],
                reason="测试用抓取结果",
                retry_note="",
                cache_warning="",
            )

    exporter = _RecordingExporter()
    monkeypatch.setattr(gui, "CachedThreadFetcher", RecordingFetcher)
    monkeypatch.setattr(gui, "create_exporter", lambda *a, **k: exporter)
    monkeypatch.setattr(gui.threading, "Thread", _SyncThread)
    return calls


def test_untils_hint_says_the_cutoff_thread_is_included():
    """界面文案必须写明「含它」—— 这是这个功能最容易误解的地方。

    界面上那行说明是 ``MAX_PAGES_HINT + UNTIL_THREAD_ID_HINT``（左栏高度紧，
    两段拼一行），所以这里按拼好的原文检查。
    """
    hint = gui.MAX_PAGES_HINT + gui.UNTIL_THREAD_ID_HINT
    assert "含它" in hint
    assert "含它" in gui.UNTIL_THREAD_ID_UNIT
    assert "不限" in hint  # 页数上限留空/0 = 不限
    assert "跳过" in hint  # 截止后面的串会被跳过


def test_max_pages_text_parses_user_input():
    """页数上限框：留空/0/坏值都当"不限"（0），正数原样。"""
    assert gui._max_pages_text("") == 0
    assert gui._max_pages_text("  ") == 0
    assert gui._max_pages_text("0") == 0
    assert gui._max_pages_text("12") == 12
    assert gui._max_pages_text(" 7 ") == 7
    for bad in ("abc", "-3", "1.5"):
        assert gui._max_pages_text(bad) == 0


def test_until_thread_text_keeps_parseable_values_only():
    """串号截止框：能解析出串号就留原文（网址原样），否则当没填。"""
    assert gui._until_thread_text("") == ""
    assert gui._until_thread_text("50000001") == "50000001"
    url = "https://www.nmbxd1.com/t/50000001"
    assert gui._until_thread_text(url) == url
    for bad in ("不是网址", "abc123xyz", "0"):
        assert gui._until_thread_text(bad) == ""


def test_app_reads_the_range_inputs_from_its_own_variables():
    """真 ``App`` 的两个取值入口读的就是界面上那两个输入框（极简替身即可验证）。"""

    class Stub:
        max_pages_var = SimpleNamespace(get=lambda: "9")
        until_thread_var = SimpleNamespace(get=lambda: "https://www.nmbxd1.com/t/50000001")

    assert gui.App.max_pages_limit(Stub()) == 9
    assert gui.App.until_thread_id(Stub()) == 50000001

    class Empty:
        max_pages_var = SimpleNamespace(get=lambda: "")
        until_thread_var = SimpleNamespace(get=lambda: "")

    assert gui.App.max_pages_limit(Empty()) == 0
    assert gui.App.until_thread_id(Empty()) is None


def test_gui_export_passes_max_pages_to_the_fetcher(artifacts_dir, monkeypatch):
    """界面上的页数上限要交给抓取层；没填时传 0（不限，安全阀仍在抓取层）。"""
    calls = _patch_gui_export(monkeypatch)
    settings = AppSettings(_path=artifacts_dir / "config.json")
    settings.userhash = "TESTHASH"

    app = _FakeAppWithRange(settings, artifacts_dir / "out", max_pages=3)
    gui.App.start(app, ["https://www.nmbxd1.com/t/50000001"])

    assert [kwargs["max_pages"] for _, kwargs in calls] == [3]
    assert any("最多 3 页" in line for line in app.logs)


def test_gui_export_leaves_the_limit_unlimited_by_default(artifacts_dir, monkeypatch):
    """没填页数上限：传 0 下去，日志里写"全部页"。"""
    calls = _patch_gui_export(monkeypatch)
    settings = AppSettings(_path=artifacts_dir / "config.json")
    settings.userhash = "TESTHASH"

    app = _FakeAppWithRange(settings, artifacts_dir / "out", max_pages=0)
    gui.App.start(app, ["https://www.nmbxd1.com/t/50000001"])

    assert [kwargs["max_pages"] for _, kwargs in calls] == [0]
    assert any("全部页" in line for line in app.logs)


def test_gui_export_skips_threads_after_the_cutoff(artifacts_dir, monkeypatch):
    """串号截止：等于截止值的那个串照导（含它），它后面的一个都不请求。"""
    calls = _patch_gui_export(monkeypatch)
    settings = AppSettings(_path=artifacts_dir / "config.json")
    settings.userhash = "TESTHASH"

    app = _FakeAppWithRange(settings, artifacts_dir / "out", until=7003)
    gui.App.start(
        app,
        [
            "https://www.nmbxd1.com/t/7001",
            "https://www.nmbxd1.com/t/7003",
            "https://www.nmbxd1.com/t/7005",
        ],
    )

    assert [url for url, _ in calls] == [
        "https://www.nmbxd1.com/t/7001",
        "https://www.nmbxd1.com/t/7003",
    ]
    assert any("含它" in line for line in app.logs)
    assert any("跳过它之后的 1 个串" in line for line in app.logs)


def test_gui_export_warns_when_the_cutoff_blocks_every_thread(
    artifacts_dir, monkeypatch
):
    """截止把列表挡光时：明确弹一句（含它规则），并且一个串都不去抓。"""
    calls = _patch_gui_export(monkeypatch)
    popped = _messagebox_calls(monkeypatch)
    settings = AppSettings(_path=artifacts_dir / "config.json")
    settings.userhash = "TESTHASH"

    gui.App.start(
        _FakeAppWithRange(settings, artifacts_dir / "out", until=7003),
        ["https://www.nmbxd1.com/t/7005"],
    )

    assert calls == []
    assert popped and popped[0][0] == "showwarning"
    assert "含它" in popped[0][2]


class _FakeWatchApp:
    """只实现 ``App.check_watch_once()`` 会用到的那点东西。"""

    def __init__(self, settings, output_dir: Path, target) -> None:
        self.settings = settings
        self.client = SimpleNamespace(image_cache_dir=None)
        self.output_var = _FakeVar(str(output_dir))
        self.use_cache_var = _FakeVar(False)
        self.watch_targets = [target]
        self.watch_queue: queue.Queue = queue.Queue()

    def persist_prefs(self) -> None:
        pass

    def apply_settings_to_client(self) -> None:
        pass

    def current_cache_dir(self):
        return Path(self.output_var.get()) / ".cache"

    def log(self, message: str) -> None:
        pass

    def _refuse_when_busy(self, doing: str) -> bool:
        """真 ``App`` 上这条会弹「正在忙」；假窗口永远不忙，放行。"""
        return False

    def _poll_watch(self) -> None:
        """真实现会 ``after`` 排一次轮询；这里什么都不做。"""


def test_gui_watch_check_hands_pdf_options_to_every_check(artifacts_dir, monkeypatch):
    """串监控的「立即检查一轮」也要用上设置里的纸张/边距。

    v0.8.0 的漏洞就是这里：手动导出接了 PDF 选项，监控那条路没接，
    用户改完设置只有一部分功能生效。所以界面这条路也要有契约断言。
    """
    settings = AppSettings(_path=artifacts_dir / "config.json")
    settings.pdf_paper = "a3"
    settings.pdf_margin = "none"
    settings.pdf_browser = "C:/假浏览器.exe"
    calls: list[dict] = []

    def fake_check(client, target, output_dir, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(target=target, error="", exported=None)

    monkeypatch.setattr(gui, "check_once", fake_check)
    monkeypatch.setattr(gui.threading, "Thread", _SyncThread)

    app = _FakeWatchApp(settings, artifacts_dir / "out", gui.WatchTarget("7001", format_key="pdf"))
    gui.App.check_watch_once(app)

    assert calls, "检查一轮都没跑"
    options = calls[0]["pdf_options"]
    assert (options.paper, options.margin) == ("a3", "none")
    assert calls[0]["browser_path"] == "C:/假浏览器.exe"

# ---------- 监控列表的导入 / 导出（界面这条路） ----------


class _WatchApp:
    """只实现 ``WatchDialog._export_list()/_import_list()`` 会用到的那点东西。"""

    def __init__(self, settings, targets, root=None) -> None:
        self.settings = settings
        self.watch_targets = list(targets)
        self.persisted = 0
        self._watching = False
        self.root = root

    def persist_watch_targets(self) -> None:
        self.settings.watch_targets = [t.to_dict() for t in self.watch_targets]
        self.persisted += 1

    def _collect_urls(self):
        return []

    def stop_watching(self) -> None:
        self._watching = False

    def start_watching(self, interval, verify_cached) -> None:  # pragma: no cover
        self._watching = True


def _open_watch(root, app) -> gui.WatchDialog:
    app.root = root
    dialog = gui.WatchDialog(app)
    root.update()
    return dialog


class _AddTargetApp(_WatchApp):
    """给「添加输入框中的串」用的：带上取当前设置的那几个方法。"""

    def __init__(self, settings, targets, root=None, max_pages: int = 5) -> None:
        super().__init__(settings, targets, root)
        self._max_pages = max_pages
        self.scope_var = _FakeVar("all")

    def _collect_urls(self):
        return ["https://www.nmbxd1.com/t/7001337"]

    def current_format(self) -> str:
        return "html"

    def parse_hashes(self):
        return []

    def current_image_mode(self) -> str:
        return "url"

    def max_pages_limit(self) -> int:
        return self._max_pages


def test_watch_dialog_add_from_input_snapshots_current_settings(dialog_root, artifacts_dir):
    """加进监控列表的那一条要把「此刻」的格式/图片/页数存下来。

    以前页数上限根本没地方存：用户在输入框填了「最多 5 页」再加进列表，
    后台每轮还是整串抓完 —— 页数上限只对手动导出生效。
    """
    settings = AppSettings(_path=artifacts_dir / "config.json")
    app = _AddTargetApp(settings, [], dialog_root, max_pages=5)
    dialog = _open_watch(dialog_root, app)
    dialog._add_from_input()
    dialog_root.update_idletasks()

    assert len(app.watch_targets) == 1
    target = app.watch_targets[0]
    assert target.thread_id == 7001337
    assert target.max_pages == 5
    assert target.image_mode == "url"
    assert app.persisted == 1  # 加完要立刻存盘


def test_watch_dialog_shows_the_page_cap_in_the_table(dialog_root, artifacts_dir):
    """列表里得看得见「最多 N 页」：不然用户没法确认这一条到底抓多少页。"""
    settings = AppSettings(_path=artifacts_dir / "config.json")
    app = _WatchApp(
        settings,
        [
            gui.WatchTarget("7001234", format_key="html", max_pages=7),
            gui.WatchTarget("7002000", format_key="html"),
        ],
        dialog_root,
    )
    dialog = _open_watch(dialog_root, app)
    dialog_root.update_idletasks()
    rows = [dialog.tree.item(iid, "values") for iid in dialog.tree.get_children()]
    assert "最多 7 页" in rows[0][1]
    assert "最多" not in rows[1][1]


def test_watch_dialog_names_the_hash_filtered_and_po_only_scopes(dialog_root, artifacts_dir):
    """范围列要把三种范围分清楚：只盯指定饼干的、只盯 PO 的、全都收的。"""
    settings = AppSettings(_path=artifacts_dir / "config.json")
    app = _WatchApp(
        settings,
        [
            gui.WatchTarget("7001234", format_key="html", include_hashes=["abc123"]),
            gui.WatchTarget("7002000", format_key="html", scope="po"),
            gui.WatchTarget("7003000", format_key="html"),
        ],
        dialog_root,
    )
    dialog = _open_watch(dialog_root, app)
    dialog_root.update_idletasks()
    rows = [dialog.tree.item(iid, "values") for iid in dialog.tree.get_children()]
    assert [row[1] for row in rows] == ["指定饼干", "仅 PO", "全部"]


def test_watch_dialog_exports_the_list_to_the_chosen_file(dialog_root, artifacts_dir, monkeypatch):
    settings = AppSettings(_path=artifacts_dir / "config.json")
    app = _WatchApp(
        settings,
        [gui.WatchTarget("https://www.nmbxd1.com/t/7001234", format_key="txt")],
    )
    out = artifacts_dir / "监控列表.json"
    monkeypatch.setattr(gui.filedialog, "asksaveasfilename", lambda **k: str(out))
    monkeypatch.setattr(gui.messagebox, "showinfo", lambda *a, **k: None)

    dialog = _open_watch(dialog_root, app)
    try:
        dialog._export_list()
        assert "已导出 1 个监控串" in dialog.status_var.get()
    finally:
        _close(dialog)

    payload = json.loads(out.read_text(encoding="utf-8"))
    assert [t["thread_id"] for t in payload["targets"]] == [7001234]
    assert payload["targets"][0]["hashes"] == []


def test_watch_dialog_export_of_an_empty_list_changes_nothing(dialog_root, artifacts_dir, monkeypatch):
    """空列表不导出：免得用户拿到一份空文件还以为备份好了。"""
    settings = AppSettings(_path=artifacts_dir / "config.json")
    app = _WatchApp(settings, [])
    asked: list = []
    monkeypatch.setattr(gui.filedialog, "asksaveasfilename", lambda **k: asked.append(k) or "")
    monkeypatch.setattr(gui.messagebox, "showinfo", lambda *a, **k: None)

    dialog = _open_watch(dialog_root, app)
    try:
        dialog._export_list()
    finally:
        _close(dialog)

    assert asked == [], "空列表不该弹出保存对话框"


def test_watch_dialog_import_merges_and_persists(dialog_root, artifacts_dir, monkeypatch):
    settings = AppSettings(_path=artifacts_dir / "config.json")
    app = _WatchApp(settings, [gui.WatchTarget("7001111")])
    source = artifacts_dir / "来.json"
    source.write_text(
        json.dumps({"targets": [{"url_or_id": "7002222"}, {"url_or_id": "7003333"}]}),
        encoding="utf-8",
    )
    shown: list[str] = []
    monkeypatch.setattr(gui.filedialog, "askopenfilename", lambda **k: str(source))
    monkeypatch.setattr(gui.messagebox, "showinfo", lambda title, text, **k: shown.append(text))

    dialog = _open_watch(dialog_root, app)
    try:
        dialog._import_list()
        assert "导入完成" in dialog.status_var.get()
        assert [t.thread_id for t in app.watch_targets] == [7001111, 7002222, 7003333]
        assert app.persisted == 1, "导入之后必须写回配置，否则关掉窗口就白导了"
    finally:
        _close(dialog)

    assert settings.watch_targets[1]["url_or_id"] == "7002222"
    assert shown and "新增 2 条" in shown[0]


def test_watch_dialog_import_shows_why_entries_were_skipped(dialog_root, artifacts_dir, monkeypatch):
    settings = AppSettings(_path=artifacts_dir / "config.json")
    app = _WatchApp(settings, [])
    source = artifacts_dir / "来.json"
    source.write_text(
        json.dumps({"targets": [{"url_or_id": "7002222", "format_key": "docx"}]}),
        encoding="utf-8",
    )
    shown: list[str] = []
    monkeypatch.setattr(gui.filedialog, "askopenfilename", lambda **k: str(source))
    monkeypatch.setattr(gui.messagebox, "showinfo", lambda title, text, **k: shown.append(text))

    dialog = _open_watch(dialog_root, app)
    try:
        dialog._import_list()
    finally:
        _close(dialog)

    assert shown and "格式认不出：docx" in shown[0]
    assert app.watch_targets == []


def test_watch_dialog_import_of_a_broken_file_shows_an_error(dialog_root, artifacts_dir, monkeypatch):
    settings = AppSettings(_path=artifacts_dir / "config.json")
    app = _WatchApp(settings, [])
    broken = artifacts_dir / "坏.json"
    broken.write_text("不是 JSON", encoding="utf-8")
    errors: list[str] = []
    monkeypatch.setattr(gui.filedialog, "askopenfilename", lambda **k: str(broken))
    monkeypatch.setattr(gui.messagebox, "showerror", lambda title, text, **k: errors.append(text))

    dialog = _open_watch(dialog_root, app)
    try:
        dialog._import_list()
    finally:
        _close(dialog)

    assert errors and "不是有效的 JSON" in errors[0]
    assert app.persisted == 0, "导入失败不该动配置"

def _walk(widget):
    yield widget
    for child in widget.winfo_children():
        yield from _walk(child)


def _text_of(widget) -> str:
    try:
        return str(widget.cget("text"))
    except tk.TclError:  # pragma: no cover - 不是所有控件都有 text
        return ""


def _content_root(dialog):
    """找到真正装内容的那个框（对话框里是个可滚动的 Card）。"""
    assert len(dialog.winfo_children()) == 1, "对话框结构变了，这条断言要跟着改"
    box = dialog.winfo_children()[0]
    assert len(box.winfo_children()) == 1, "对话框结构变了，这条断言要跟着改"
    return box.winfo_children()[0]


def test_watch_dialog_leaves_room_for_every_row(dialog_root, artifacts_dir):
    """对话框得留够高度，不能让底下那行按钮被裁掉。

    2026-10-01 真机截图抓到的：导入 / 导出挤在「添加 / 移除 / 间隔 / 校验」那一行右边，
    被整条裁到窗口外 —— 控件对象都在、用例全绿，用户却看不见。

    ⚠️ 说清楚这条断言的力度：本机 Tk 缩放下（``tk scaling`` ≈ 1.33）内容只要 415 逻辑像素，
    把窗口改回 480 它照样过 —— **它不是那次裁切的复现器**，真正的元凶是显示缩放
    （Tk 用逻辑像素、内容按缩放变大）。所以这里只钉住「今天这个布局在本机量得下」，
    谁要再往对话框里加一行，请自觉同步 ``self.geometry``；别把它当成能抓裁切的哨兵。
    """
    settings = AppSettings(_path=artifacts_dir / "config.json")
    app = _WatchApp(settings, [gui.WatchTarget("7001234")])

    dialog = _open_watch(dialog_root, app)
    try:
        for _ in range(10):
            dialog_root.update()
        assert dialog.winfo_height() > 1, "对话框没量到尺寸"
        content = _content_root(dialog)
        needed = content.winfo_reqheight()
        room = content.winfo_height()
        assert needed > 100, f"内容高度量得不对劲（{needed}）"
        assert needed <= room, f"窗口装不下监控列表的内容（需要 {needed}，只给了 {room}）"
    finally:
        _close(dialog)


def test_watch_dialog_shows_import_export_on_their_own_row(dialog_root, artifacts_dir):
    """导入 / 导出单独占一行，而且在最底下那行按钮的上面。

    事件顺序：最底下是「开始监控 / 关闭 / 立即检查一次」，它上面那行才是「导入 / 导出」。
    挤回「添加 / 移除 / 间隔 / 校验」那一行右边时，导入 / 导出会跟「移除选中」同高 → 红。
    """
    settings = AppSettings(_path=artifacts_dir / "config.json")
    app = _WatchApp(settings, [])

    dialog = _open_watch(dialog_root, app)
    try:
        for _ in range(10):
            dialog_root.update()
        rows: dict[int, set[str]] = {}
        for widget in _walk(dialog):
            text = _text_of(widget)
            if text in {"开始监控", "关闭", "立即检查一次", "导出列表", "导入列表", "移除选中"}:
                rows.setdefault(widget.winfo_rooty(), set()).add(text)
        assert len(rows) >= 3, f"按钮挤成了 {len(rows)} 行：{rows}"
        by_y = [rows[key] for key in sorted(rows)]
        last, before_last = by_y[-1], by_y[-2]
        assert "开始监控" in last, f"最底下那行应当是开始监控：{by_y}"
        assert {"导出列表", "导入列表"} <= before_last, f"导入/导出应当单独占一行：{by_y}"
        assert "移除选中" not in before_last, f"导入/导出又挤回添加那一行了：{by_y}"
    finally:
        _close(dialog)


def test_watch_dialog_has_both_list_io_buttons(dialog_root, artifacts_dir):
    settings = AppSettings(_path=artifacts_dir / "config.json")
    app = _WatchApp(settings, [])

    dialog = _open_watch(dialog_root, app)
    try:
        labels = {_text_of(w) for w in _walk(dialog)}
    finally:
        _close(dialog)

    assert "导出列表" in labels
    assert "导入列表" in labels


# ---------- 环境自检（界面这条路） ----------


class _SelftestApp:
    """只实现 ``SelftestDialog`` 会用到的那点东西。"""

    def __init__(self, root) -> None:
        self.root = root
        self.logged: list[str] = []

    def log(self, message: str) -> None:
        self.logged.append(message)


def _isolate_settings(monkeypatch, artifacts_dir) -> None:
    """自检会 ``AppSettings.load()``，这里换成读临时目录，别碰用户真实配置。"""
    monkeypatch.setattr(
        AppSettings,
        "load",
        classmethod(lambda cls: cls(_path=artifacts_dir / "config.json")),
    )


def _open_selftest(root, app) -> gui.SelftestDialog:
    dialog = gui.SelftestDialog(app)
    dialog.update()
    return dialog


def test_selftest_dialog_survives_a_broken_config(
    dialog_root, artifacts_dir, monkeypatch
):
    """配置写不出来时，自检要给出「不行」而不是抛出来。"""
    from xdao import preflight

    _isolate_settings(monkeypatch, artifacts_dir)
    monkeypatch.setattr(preflight, "can_write_dir", lambda directory: False)
    app = _SelftestApp(dialog_root)

    dialog = _open_selftest(dialog_root, app)
    try:
        content = dialog.text.get("1.0", "end")
    finally:
        _close(dialog)

    assert "[不行]" in content
    assert "配置目录" in content, "自检报告没打出来"
    assert "程序版本" in content, "对话框没打抬头"


def test_selftest_dialog_grows_with_the_window(dialog_root, artifacts_dir, monkeypatch):
    """文本区要跟着窗口长，不能只占「请求高度」在卡片里留一大块空白。

    ``Card(stretch=True)`` 之前，画布里的 body 只有请求尺寸：窗口拉到 620
    高，文本区还是 206px（真机截图里下面空一大块）。
    """
    _isolate_settings(monkeypatch, artifacts_dir)
    app = _SelftestApp(dialog_root)
    dialog = _open_selftest(dialog_root, app)
    try:
        dialog.geometry("660x620")
        dialog.update()
        tall = dialog.text.winfo_height()
        dialog.geometry("660x400")
        dialog.update()
        short = dialog.text.winfo_height()
    finally:
        _close(dialog)

    assert tall > 400, f"窗口 620 高时文本区只有 {tall}px"
    assert short < tall, f"窗口变矮文本区没跟着缩：{short} vs {tall}"


def test_selftest_dialog_copy_puts_the_report_on_the_clipboard(
    dialog_root, artifacts_dir, monkeypatch
):
    _isolate_settings(monkeypatch, artifacts_dir)
    app = _SelftestApp(dialog_root)
    dialog = _open_selftest(dialog_root, app)
    try:
        dialog._copy()
        status = dialog.status_var.get()
        clipboard = dialog.clipboard_get()
    finally:
        _close(dialog)

    assert "已复制" in status
    assert "本机自检" in clipboard
    assert app.logged, "自检结果没有写进运行日志"


def _wait_until(root, predicate, timeout: float = 20.0) -> bool:
    """等界面把后台线程的结果搬上来（Tk 只能在主线程里 update）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        root.update()
        if predicate():
            return True
        time.sleep(0.02)
    return bool(predicate())


def test_selftest_dialog_has_a_way_to_find_out_if_the_browser_even_starts(
    dialog_root, artifacts_dir, monkeypatch
):
    """自检里要能回答「浏览器到底起不起得来」——不是「装没装」。

    这条路真的会把假浏览器启一次（假货自己写端口文件、假调试服务答一句话），
    所以它验的是整条链路：按钮 → 后台线程 → ``browser_check.check_all``
    → 结论写回文本区。假货用闸门拦住到**真结果已经回到队列里**为止，这样
    「跑着的按钮是禁用的」不是靠抢时间断言出来的。
    """
    from http.server import HTTPServer

    from .test_browser_check import _VersionHandler, _fake_browser

    from xdao import browser_check, browser_login

    _isolate_settings(monkeypatch, artifacts_dir)
    server = HTTPServer(("127.0.0.1", 0), _VersionHandler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    info = _fake_browser(artifacts_dir, "live", str(port))
    monkeypatch.setattr(
        browser_login, "find_browser", lambda explicit=None, env=None: info
    )
    monkeypatch.setattr(
        browser_check, "find_browser", lambda explicit=None, env=None: info
    )
    gate = threading.Event()
    # 先把真实现存下来：monkeypatch 之后模块属性就是替身了，
    # 替身里再调 ``browser_check.check_all`` 会自己调自己（第一次写成那样，
    # 界面上显示的是 ``RecursionError: maximum recursion depth exceeded``）。
    real_check = browser_check.check_all

    def gated_check(*args, **kwargs):
        report = real_check(*args, **kwargs)
        gate.wait(20.0)  # 结果已经在手上，但先别交回主线程
        return report

    monkeypatch.setattr(browser_check, "check_all", gated_check)
    app = _SelftestApp(dialog_root)
    dialog = _open_selftest(dialog_root, app)
    try:
        labels = {_text_of(w) for w in _walk(dialog)}
        dialog._run_browser_check()
        dialog.update_idletasks()
        busy_text = dialog.browser_button.instate(["disabled"])
        busy_status = dialog.status_var.get()
        gate.set()
        got = _wait_until(dialog_root, lambda: "答得上话" in dialog.text.get("1.0", "end"))
        content = dialog.text.get("1.0", "end")
        ready = dialog.browser_button.instate(["!disabled"])
    finally:
        gate.set()
        _close(dialog)
        server.shutdown()
        server.server_close()

    assert "试浏览器" in labels, "自检对话框里没有这个按钮"
    assert busy_text is True, "试浏览器的时候按钮该禁用，免得点出两条后台线程"
    assert "正在试" in busy_status, f"跑的时候该有状态提示，实际是 {busy_status!r}"
    assert got, f"没等到浏览器结论：{content!r}"
    assert "[可以]" in content
    assert "假浏览器" in content
    assert ready, "跑完了按钮该恢复"
    assert any("浏览器可以用" in line for line in app.logged), app.logged


def test_selftest_browser_check_says_so_when_the_probe_itself_blows_up(
    dialog_root, artifacts_dir, monkeypatch
):
    """探测自己出错也要说人话，不能让异常在后台线程里消失。"""
    from xdao import browser_check

    _isolate_settings(monkeypatch, artifacts_dir)

    def boom(*args, **kwargs):
        raise RuntimeError("探测炸了")

    monkeypatch.setattr(browser_check, "check_all", boom)
    app = _SelftestApp(dialog_root)
    dialog = _open_selftest(dialog_root, app)
    try:
        dialog._run_browser_check()
        got = _wait_until(dialog_root, lambda: "探测炸了" in dialog.text.get("1.0", "end"))
        content = dialog.text.get("1.0", "end")
        ready = dialog.browser_button.instate(["!disabled"])
    finally:
        _close(dialog)

    assert got, f"没等到出错说明：{content!r}"
    assert "[不行]" in content
    assert ready, "出错也要把按钮放开"


# ---------- 新版本检查（界面这条路） ----------


class _FakeUpdateButton:
    def __init__(self) -> None:
        self.text = "检查更新"

    def config(self, **kwargs) -> None:
        if "text" in kwargs:
            self.text = kwargs["text"]

    def winfo_exists(self) -> bool:
        return True


class _UpdateApp:
    """只实现新版本检查这条路的 App 会用到的那点东西。

    ``_update_done`` / ``_poll_update`` / ``_offer_download`` / 升级那一串
    都直接借用真实现（它们的逻辑就是要测的），其余属性用假的顶上。
    """

    def __init__(self, root) -> None:
        self.root = root
        self.logged: list[str] = []
        self._update_result = None
        self._update_button = _FakeUpdateButton()
        self._checking_update = True
        self._update_queue = queue.Queue()
        # 一键升级的状态：真实现会读它，缺了会 AttributeError
        self._upgrading = False
        self._upgrade_spawned = False
        self._upgrade_queue = queue.Queue()
        self.upgrades_started: list[object] = []
        self.finished: list[tuple[bool, str, str]] = []
        self.quits: list[bool] = []

    def log(self, message: str) -> None:
        self.logged.append(message)

    def _start_upgrade(self, result) -> None:
        self.upgrades_started.append(result)

    def _upgrade_finish(self, ok: bool, message: str, action: str) -> None:
        """只记下来：真实现会弹对话框、还会**结束进程**，用例里不能让它跑。"""
        self.finished.append((ok, message, action))
        self._upgrading = False

    _update_done = gui.App._update_done
    _poll_update = gui.App._poll_update
    _offer_download = gui.App._offer_download
    _upgrade_worker = gui.App._upgrade_worker
    _upgrade_progress = gui.App._upgrade_progress
    _upgrade_progress_text = gui.App._upgrade_progress_text
    _update_button_text = gui.App._update_button_text
    _end_self_for_upgrade = gui.App._end_self_for_upgrade


def _call_update_worker(app, monkeypatch, result) -> None:
    """跑一遍后台线程那半边 + 主线程收结果那半边（不真起线程）。"""
    from xdao import update_check

    monkeypatch.setattr(update_check, "check_for_update", lambda **kwargs: result)
    gui.App._update_worker(app, app._update_queue)
    gui.App._poll_update(app)


def test_update_check_logs_the_newer_release_and_marks_the_button(
    dialog_root, monkeypatch
):
    from xdao import update_check

    app = _UpdateApp(dialog_root)
    result = update_check.UpdateResult(
        ok=True,
        newer=True,
        current="0.11.0",
        latest=update_check.ReleaseInfo(tag="v9.9.9", url="https://example.invalid/9"),
    )

    _call_update_worker(app, monkeypatch, result)

    assert app._checking_update is False
    assert app._update_button.text == "有新版本"
    assert any("9.9.9" in line for line in app.logged), "有新版本却没写进日志"


def test_update_check_says_being_current_without_marking_the_button(
    dialog_root, monkeypatch
):
    from xdao import update_check

    app = _UpdateApp(dialog_root)
    result = update_check.UpdateResult(ok=True, newer=False, current="0.11.0")

    _call_update_worker(app, monkeypatch, result)

    assert app._update_button.text == "检查更新"
    assert any("已是最新版本" in line for line in app.logged)
    assert app._update_result is result


def test_update_check_survives_a_broken_checker(dialog_root, monkeypatch):
    """查版本失败（连模块都炸了）也要把按钮恢复，不能卡在「检查中…」。"""
    app = _UpdateApp(dialog_root)
    app._update_button.config(text="检查中…")
    from xdao import update_check

    def boom(**kwargs):
        raise RuntimeError("天知道")

    monkeypatch.setattr(update_check, "check_for_update", boom)

    gui.App._update_worker(app, app._update_queue)
    gui.App._poll_update(app)

    assert app._checking_update is False
    assert app._update_button.text == "检查更新"


def test_app_schedules_one_update_check_after_startup():
    """窗口起来之后要自己问一次「有没有新版本」，而且不弹窗（silent）。"""
    import inspect
    import re

    from xdao.gui import App

    source = inspect.getsource(App.__init__)
    pattern = r"root\.after\(\s*\d+,\s*self\.check_update\(silent=True\)|root\.after\(\s*\d+,\s*lambda:\s*self\.check_update\(silent=True\)"
    assert re.search(pattern, source), "App 起来之后没有安排那次新版本检查"
    assert source.count("check_update(silent=True)") == 1, "启动时只该问一次"


def test_startup_sweeps_the_old_temp_profiles(monkeypatch):
    """启动时要顺手把 %TEMP% 里的旧资料目录扫掉（被强杀留下的那些）。

    正常收尾由 ``LoginBrowser.cleanup_temp_profile()`` 当场删；程序被强杀就没人收尾，
    所以得在启动时补一刀。放后台线程里做，而且清不干净也不许影响启动。
    """
    import inspect
    import re

    from xdao import browser_login
    from xdao.gui import App

    source = inspect.getsource(App.__init__)
    assert re.search(r"Thread\(target=self\._sweep_temp_profiles", source), (
        "启动时没有安排那次临时资料目录清扫"
    )

    calls: list[str] = []
    monkeypatch.setattr(
        browser_login,
        "sweep_stale_temp_profiles",
        lambda **kwargs: (calls.append("sweep"), 0)[1],
    )
    app = App.__new__(App)
    app._sweep_temp_profiles()
    assert calls == ["sweep"]

    def boom(**kwargs):
        raise OSError("被安全软件拦下了")

    monkeypatch.setattr(browser_login, "sweep_stale_temp_profiles", boom)
    app._sweep_temp_profiles()  # 不该往外抛


def test_startup_sweeps_the_config_dir_spare_profiles_too(monkeypatch, tmp_path):
    """启动清扫要连配置目录下那族备用目录一起扫（v0.13.39）。

    真机 2026-10-01 在配置目录里量到过两个没人收的空壳
    （``browser-profile-12268-1790850954`` 96 个文件 / 5.6MB、
    ``browser-profile-12268-1790850956`` 66 个文件 / 6.3MB）。
    """
    from xdao import browser_login
    from xdao.gui import App

    config_dir = tmp_path / "xdao-export"
    config_dir.mkdir()
    calls: list[Path] = []
    monkeypatch.setattr(browser_login, "sweep_stale_temp_profiles", lambda **kwargs: 0)
    monkeypatch.setattr(
        browser_login,
        "sweep_stale_fallback_profiles",
        lambda profile, **kwargs: (calls.append(Path(profile)), 0)[1],
    )
    app = App.__new__(App)
    app.settings = SimpleNamespace(config_path=str(config_dir / "config.json"))
    app._sweep_temp_profiles()
    assert calls == [browser_login.user_data_dir(config_dir)], (
        "要按配置目录算出持久资料目录，再用它去扫那族备用目录"
    )

    def boom(profile, **kwargs):
        raise OSError("被安全软件拦下了")

    monkeypatch.setattr(browser_login, "sweep_stale_fallback_profiles", boom)
    app._sweep_temp_profiles()  # 不该往外抛


# ---------- 一键升级（界面这条路） ----------


def _newer_result(*, asset: bool = True):
    from xdao import update_check

    return update_check.UpdateResult(
        ok=True,
        newer=True,
        current="0.12.0",
        latest=update_check.ReleaseInfo(
            tag="v9.9.9",
            url="https://example.invalid/9",
            asset_url="https://example.invalid/9/xdao-export-v9.9.9-win64.zip" if asset else "",
            asset_name="xdao-export-v9.9.9-win64.zip" if asset else "",
            asset_size=11 * 1024 * 1024 if asset else 0,
        ),
    )


class _UpgradeSpy:
    """升级那一路的替身：记下被调了什么，一次都不碰网络和文件系统。

    ``confirm`` 决定「自检通过后那次二次确认」用户答什么；``test_ok``
    决定候选版本的 ``--selftest`` 过不过；``boom`` 让下载直接炸。
    """

    def __init__(self, *, staging, test_ok=True, confirm=True, boom=False) -> None:
        from xdao import updater

        self.calls: list[str] = []
        self.staging = staging
        self.plans = [
            updater.UpgradePlan(True, "", staging / "app", staging / "app" / "xdao-export.exe", 4242)
        ]
        self.test_ok = test_ok
        self.confirm = confirm
        self.boom = boom
        self.spawned = False
        self.discarded: list[object] = []

    def plan_upgrade(self, **kwargs):
        self.calls.append("plan")
        return self.plans[0]

    def default_staging_root(self):
        return self.staging

    def detect_proxy(self):
        return ""

    def download_asset(self, url, target, **kwargs):
        self.calls.append("download")
        if self.boom:
            raise OSError("网络断了")
        Path(target).parent.mkdir(parents=True, exist_ok=True)
        Path(target).write_bytes(b"zip")
        if kwargs.get("progress"):
            kwargs["progress"](10, 100)

    def extract_payload(self, archive, into):
        self.calls.append("extract")
        Path(into).mkdir(parents=True, exist_ok=True)
        return Path(into)

    def payload_launcher(self, payload):
        return Path(payload) / "xdao-export.exe"

    def run_selftest(self, exe, **kwargs):
        self.calls.append("selftest")
        if self.test_ok:
            return True, "本机自检全部通过。"
        return False, "有 1 项没过：配置目录"

    def spawn_helper(self, plan, payload):
        self.calls.append("spawn")
        self.spawned = True

    def discard_staging(self, root=None):
        self.calls.append("discard")
        self.discarded.append(root)


def _drive_upgrade(app, result, monkeypatch, spy, *, agree=True, seconds=30.0):
    """像真界面那样跑一遍升级：后台线程干活，这里扮演主线程收发消息。

    **必须真开一个线程**：``_upgrade_worker`` 会在「二次确认」那一步阻塞等
    主线程的回答（``decision.get()``），同步调用它会把自己等死。
    """
    import threading

    _patch_updater(monkeypatch, spy)
    # 升级成功后真实现会结束进程（`os._exit`）——用例里必须挡掉，否则整套测试当场没了
    monkeypatch.setattr(gui.os, "_exit", lambda code=0: None)
    worker = threading.Thread(target=app._upgrade_worker, args=(result, app._upgrade_queue))
    worker.daemon = True
    worker.start()
    messages: list[tuple] = []
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            message = app._upgrade_queue.get(timeout=1.0)
        except queue.Empty:
            if not worker.is_alive() and app._upgrade_queue.empty():
                break
            continue
        messages.append(message)
        kind, payload = message
        if kind == "confirm":
            payload[1].put(bool(agree))  # type: ignore[index]
        elif kind == "finish":
            break
    worker.join(timeout=5.0)
    assert not worker.is_alive(), "升级线程没退出来"
    # 后台线程碰过 Tk，垃圾回收趁还在主线程时做掉；否则会在别的线程里析构
    # Tk 变量并抛 RuntimeError（表现成 PytestUnraisableExceptionWarning）。
    gc.collect()
    time.sleep(0.2)
    gc.collect()
    return messages


def _patch_updater(monkeypatch, spy) -> None:
    from xdao import updater

    monkeypatch.setattr(updater, "plan_upgrade", spy.plan_upgrade)
    monkeypatch.setattr(updater, "default_staging_root", spy.default_staging_root)
    monkeypatch.setattr(updater, "detect_proxy", spy.detect_proxy)
    monkeypatch.setattr(updater, "download_asset", spy.download_asset)
    monkeypatch.setattr(updater, "extract_payload", spy.extract_payload)
    monkeypatch.setattr(updater, "payload_launcher", spy.payload_launcher)
    monkeypatch.setattr(updater, "run_selftest", spy.run_selftest)
    monkeypatch.setattr(updater, "spawn_helper", spy.spawn_helper)
    monkeypatch.setattr(updater, "discard_staging", spy.discard_staging)


def test_upgrade_worker_runs_the_whole_five_steps(dialog_root, tmp_path, monkeypatch):
    """下载 → 解压 → 自检 → 二次确认 → 交给帮手，五步一步都不能少。"""
    from xdao import updater

    app = _UpdateApp(dialog_root)
    spy = _UpgradeSpy(staging=tmp_path / updater.STAGING_DIR_NAME)

    messages = _drive_upgrade(app, _newer_result(), monkeypatch, spy)

    assert spy.calls[:5] == ["plan", "download", "extract", "selftest", "spawn"]
    assert spy.spawned is True
    assert app._upgrade_spawned is True, "交给帮手之后不能再删暂存（帮手要用）"
    assert spy.discarded == [], "已经交给帮手了，暂存不能删"
    kinds = [kind for kind, _ in messages]
    assert kinds[-1] == "finish"
    ok, message, action = messages[-1][1]
    assert ok is True
    assert action == "exit", "帮手在等我们退出，得真的退"
    assert "9.9.9" in message


def test_upgrade_worker_keeps_the_old_copy_when_the_selftest_fails(
    dialog_root, tmp_path, monkeypatch
):
    """新版自检没过就什么都别换，而且要把暂存清掉。"""
    from xdao import updater

    app = _UpdateApp(dialog_root)
    spy = _UpgradeSpy(staging=tmp_path / updater.STAGING_DIR_NAME, test_ok=False)

    messages = _drive_upgrade(app, _newer_result(), monkeypatch, spy)

    assert "spawn" not in spy.calls, "自检没过还想换？"
    assert app._upgrade_spawned is False
    assert spy.discarded == [spy.staging], "没换成，暂存要清掉"
    ok, message, _ = messages[-1][1]
    assert ok is False
    assert "不换" in message


def test_upgrade_worker_does_not_swap_when_the_user_says_no(
    dialog_root, tmp_path, monkeypatch
):
    """自检过了、但用户在第二次确认上答「不」—— 一样什么都不换。"""
    from xdao import updater

    app = _UpdateApp(dialog_root)
    spy = _UpgradeSpy(staging=tmp_path / updater.STAGING_DIR_NAME)

    messages = _drive_upgrade(app, _newer_result(), monkeypatch, spy, agree=False)

    assert "spawn" not in spy.calls
    assert spy.discarded == [spy.staging]
    ok, message, _ = messages[-1][1]
    assert ok is False
    assert "取消" in message


def test_upgrade_worker_survives_a_dead_network(dialog_root, tmp_path, monkeypatch):
    from xdao import updater

    app = _UpdateApp(dialog_root)
    spy = _UpgradeSpy(staging=tmp_path / updater.STAGING_DIR_NAME, boom=True)

    messages = _drive_upgrade(app, _newer_result(), monkeypatch, spy)

    assert "spawn" not in spy.calls
    assert spy.discarded == [spy.staging]
    ok, message, _ = messages[-1][1]
    assert ok is False
    assert "网络断了" in message, "失败原因要如实写出来"


def test_upgrade_worker_stops_when_the_release_has_no_package(
    dialog_root, tmp_path, monkeypatch
):
    """发布里没挂 zip 就别去下，直接告诉用户手工下载。"""
    from xdao import updater

    app = _UpdateApp(dialog_root)
    spy = _UpgradeSpy(staging=tmp_path / updater.STAGING_DIR_NAME)

    messages = _drive_upgrade(app, _newer_result(asset=False), monkeypatch, spy)

    assert spy.calls == ["plan", "discard"]
    assert spy.discarded == [spy.staging]
    ok, message, _ = messages[-1][1]
    assert ok is False
    assert "手工下载" in message


def test_upgrade_progress_asks_once_more_before_swapping(dialog_root, monkeypatch):
    """自检通过之后还要再问一句才动手 —— 用户答「是」就继续。"""
    app = _UpdateApp(dialog_root)
    ask = {"called": 0}

    def fake_ask(*args, **kwargs):
        ask["called"] += 1
        return True

    monkeypatch.setattr(gui.messagebox, "askyesno", fake_ask)
    decision: queue.Queue = queue.Queue()

    keep_going = app._upgrade_progress("confirm", ("v9.9.9", decision))

    assert keep_going is True
    assert ask["called"] == 1
    assert decision.get() is True


def test_upgrade_progress_records_a_cancel(dialog_root, monkeypatch):
    app = _UpdateApp(dialog_root)
    monkeypatch.setattr(gui.messagebox, "askyesno", lambda *a, **k: False)
    decision: queue.Queue = queue.Queue()

    assert app._upgrade_progress("confirm", ("v9.9.9", decision)) is True
    assert decision.get() is False
    assert any("取消" in line for line in app.logged)


def test_upgrade_progress_finishes_and_stops_polling(dialog_root):
    app = _UpdateApp(dialog_root)
    app._upgrading = True

    assert app._upgrade_progress("finish", (False, "没换成", "")) is False
    assert app.finished == [(False, "没换成", "")]
    assert app._upgrading is False


def test_upgrade_progress_text_shows_a_percentage(dialog_root):
    app = _UpdateApp(dialog_root)

    app._upgrade_progress_text((512, 1024))

    assert app._update_button.text == "升级中 50%"


def test_upgrade_progress_text_falls_back_to_kilobytes(dialog_root):
    app = _UpdateApp(dialog_root)

    app._upgrade_progress_text((2048,))

    assert app._update_button.text == "升级中 2 KB"


def test_upgrade_progress_text_ignores_junk(dialog_root):
    """后台线程万一塞了看不懂的东西，按钮文字也不该被写坏。"""
    app = _UpdateApp(dialog_root)

    app._upgrade_progress_text("不知道这是什么")
    app._upgrade_progress_text(())


def test_end_self_for_upgrade_quits_the_loop_and_ends_the_process(
    dialog_root, monkeypatch
):
    """换完之后必须**真的结束进程**：帮手在等老进程消失，只 quit() 不够。

    实测（`_scratch\\probe_quit_exits.py`）真机上 `root.quit()` 之后进程还活着
    （哪怕只剩一个主线程），帮手等满 60 秒就放弃 —— 用户看到「升级完成」，
    目录其实没换。
    """
    app = _UpdateApp(dialog_root)
    exited: list[int] = []
    monkeypatch.setattr(gui.os, "_exit", lambda code=0: exited.append(code))

    app.root.quit = lambda: app.quits.append(True)  # type: ignore[method-assign]
    gui.App._end_self_for_upgrade(app)

    assert app.quits == [True], "先正常退出事件循环"
    assert exited == [0], "再把进程结束掉，不然帮手永远等不到"
    assert any("让给新版本" in line for line in app.logged)


@pytest.fixture
def no_popups(monkeypatch):
    """把消息框钉住，别让用例卡在真对话框上。

    这两条用例要跑**真的** ``App._upgrade_finish``，而真实现成功时会
    ``messagebox.showinfo`` 弹一个模态框 —— 平机上就是「窗口开着等点确定」，
    表现为 pytest 永远不返回（CI 的 windows 矩阵就是这样卡了 45 分钟），
    所以这里把会阻塞的几样统一钉住。
    """
    quiet = lambda *a, **k: None  # noqa: E731
    for name in (
        "showinfo",
        "showwarning",
        "showerror",
        "askyesno",
        "askokcancel",
        "askretrycancel",
    ):
        monkeypatch.setattr(gui.messagebox, name, quiet)
    return quiet


def test_upgrade_finish_calls_the_end_self_step_on_success(dialog_root, no_popups):
    app = _UpdateApp(dialog_root)
    app.end_self_calls = []

    def fake_end_self(self=app):
        app.end_self_calls.append(True)

    app._end_self_for_upgrade = fake_end_self  # type: ignore[method-assign]

    gui.App._upgrade_finish(app, True, "新版本 0.13.0 已就位。", "exit")

    assert app.end_self_calls == [True], "换成功了就必须走「结束自己」这一步"
    assert app._update_button.text == "升级完成"


def test_upgrade_finish_does_not_end_the_process_when_it_failed(
    dialog_root, monkeypatch, no_popups
):
    app = _UpdateApp(dialog_root)
    exited: list[int] = []
    monkeypatch.setattr(gui.os, "_exit", lambda code=0: exited.append(code))

    gui.App._upgrade_finish(app, False, "没换成", "")

    assert exited == [], "没成功就别退，用户还得接着用"
    assert app._update_button.text == "有新版本", "没换成，按钮得回到「有新版本」让人再试"


# ---------- 导出可取消（v0.17.0）----------


def _fake_app(tmp_path: Path) -> _FakeApp:
    settings = AppSettings(_path=tmp_path / "config.json")
    return _FakeApp(settings, tmp_path)


def test_cancel_export_lifts_the_stop_flag(tmp_path):
    """点「取消导出」：举起停止位，按钮先变成「正在取消…」。"""
    app = _fake_app(tmp_path)
    event = threading.Event()
    app._exporting = True
    app._export_stop = event

    gui.App.cancel_export(app)

    assert event.is_set(), "停止位没举起来，后台根本看不见这次取消"
    assert app.progress_var.get() == "正在取消…"
    assert app.start_button.calls[-1] == (
        "config",
        {"state": "disabled", "text": "正在取消…"},
    )
    assert "正在取消" in app.logs[-1]
    assert "已经导出的文件会保留" in app.logs[-1]


def test_cancel_export_does_nothing_when_nothing_is_running(tmp_path):
    app = _fake_app(tmp_path)
    event = threading.Event()
    app._exporting = False
    app._export_stop = event

    gui.App.cancel_export(app)

    assert not event.is_set()
    assert app.logs == []


def test_set_export_controls_swaps_start_into_cancel(tmp_path):
    """导出期间同一个按钮变「取消导出」，结束后变回「开始导出」。"""
    app = _fake_app(tmp_path)

    gui.App._set_export_controls(app, running=True)
    kwargs = app.start_button.calls[-1][1]
    assert kwargs["text"] == "取消导出"
    assert kwargs["command"].__self__ is app, "按钮要指回这个窗口自己的取消方法"
    assert kwargs["command"].__func__.__name__ == "cancel_export"
    assert app.retry_button.calls[-1] == ("configure", {"state": "disabled"})

    app._last_failed = ["https://www.nmbxd1.com/t/7001234"]
    gui.App._set_export_controls(app, running=False)
    assert app.start_button.calls[-1][1]["text"] == "开始导出"
    assert app.start_button.calls[-1][1]["command"].__func__.__name__ == "start"
    assert app.retry_button.calls[-1] == ("configure", {"state": "normal"})


def test_finish_after_cancel_says_what_was_kept(tmp_path, monkeypatch):
    """取消后的汇总：说清「完成几个 / 一共几个」以及已经导出的文件在哪。"""
    app = _fake_app(tmp_path)
    app._last_failed = ["https://www.nmbxd1.com/t/7001"]
    shown: list[tuple[str, str]] = []
    monkeypatch.setattr(
        gui.messagebox, "showinfo", lambda title, text: shown.append((title, text))
    )

    gui.App._finish(app, 2, 5, [], 63.0, None, cancelled=True)

    assert shown and shown[0][0] == "已取消导出"
    assert "取消前完成 2 / 5 个串" in shown[0][1]
    assert "已经导出的文件" in shown[0][1]
    assert app.progress_var.get() == "已取消：完成 2 / 5 个串，用时 63 秒"
    assert app._exporting is False
    assert app._export_stop is None
    assert app.progress_bar.calls == [("stop", None), ("set_value", 2, 5)]
    assert any("已导出的文件都还在" in line for line in app.logs)

def test_finish_after_failures_lists_what_failed_and_where_the_rest_went(tmp_path, monkeypatch):
    """有失败项时的汇总：弹「部分失败」、日志里点名哪几个失败、文件在哪。"""
    app = _fake_app(tmp_path)
    warnings: list[tuple[str, str]] = []
    monkeypatch.setattr(
        gui.messagebox, "showwarning", lambda title, text: warnings.append((title, text))
    )

    gui.App._finish(
        app,
        3,
        5,
        ["https://www.nmbxd1.com/t/7001234", "https://www.nmbxd1.com/t/7002345"],
        91.0,
        None,
    )

    assert warnings and warnings[0][0] == "部分失败"
    assert "成功 3 / 5 个串" in warnings[0][1]
    assert "失败 2 个" in warnings[0][1]
    assert str(tmp_path) in warnings[0][1], "弹窗要告诉用户文件写在哪"
    assert app._last_failed == [
        "https://www.nmbxd1.com/t/7001234",
        "https://www.nmbxd1.com/t/7002345",
    ], "失败清单要留着，重试按钮靠它"
    assert any("失败清单" in line for line in app.logs)
    assert any("7001234" in line for line in app.logs), "点不到具体是哪个串就白记了"
    assert app.progress_var.get() == "完成：成功 3 / 5 个串，用时 91 秒"


def test_finish_warns_about_threads_that_were_only_partly_fetched(tmp_path, monkeypatch):
    """没失败、但有串只抓到一部分：也要单独弹窗，别让「缺页」混在成功里。"""
    app = _fake_app(tmp_path)
    warnings: list[tuple[str, str]] = []
    monkeypatch.setattr(
        gui.messagebox, "showwarning", lambda title, text: warnings.append((title, text))
    )
    notes = [f"第 {index} 串只抓到一半" for index in range(1, 8)]

    gui.App._finish(app, 8, 8, [], 40.0, notes)

    assert warnings and warnings[0][0] == "有串没抓全"
    body = warnings[0][1]
    assert "其中 7 个" in body
    assert "第 1 串只抓到一半" in body
    assert "…" in body, "只列前五条，剩下的用省略号收尾"
    assert "第 7 串只抓到一半" not in body
    assert any("有 7 个串没能抓全" in line for line in app.logs)
    assert sum(1 for line in app.logs if "只抓到一半" in line) == 7, "日志里要逐条记全"


def test_finish_after_a_clean_run_reports_nothing_but_success(tmp_path, monkeypatch):
    """一切顺利：只弹「完成」，不弹警告、日志里不出现失败字样。"""
    app = _fake_app(tmp_path)
    infos: list[tuple[str, str]] = []
    warnings: list[tuple[str, str]] = []
    monkeypatch.setattr(
        gui.messagebox, "showinfo", lambda title, text: infos.append((title, text))
    )
    monkeypatch.setattr(
        gui.messagebox, "showwarning", lambda title, text: warnings.append((title, text))
    )

    gui.App._finish(app, 4, 4, [], 12.5, None)

    assert warnings == []
    assert infos and infos[0][0] == "完成"
    assert "成功导出 4 / 4 个串" in infos[0][1]
    assert str(tmp_path) in infos[0][1]
    assert not any("失败" in line for line in app.logs)


# ---------- 监控条目可编辑（v0.17.0）----------


def _open_watch_edit(dialog_root, artifacts_dir, targets):
    settings = AppSettings(_path=artifacts_dir / "config.json")
    app = _AddTargetApp(settings, list(targets), dialog_root)
    dialog = _open_watch(dialog_root, app)
    target = app.watch_targets[0]
    edit = gui.WatchEditDialog(dialog, target)
    dialog_root.update()
    return app, dialog, edit, target


def test_watch_edit_dialog_saves_scope_format_image_and_pages(dialog_root, artifacts_dir):
    """编辑一条监控：范围、格式、图片处理、页数上限都要落到目标上并存盘。"""
    target = gui.WatchTarget("7001234", format_key="txt", image_mode="embed")
    app, dialog, edit, target = _open_watch_edit(dialog_root, artifacts_dir, [target])
    try:
        edit.scope_box.current(1)  # 仅 PO 发言
        edit.format_box.current(edit._format_keys.index("html"))
        edit.image_box.current(1)  # 仅保留链接
        edit.pages_var.set("6")

        edit._save()

        assert target.scope == "po"
        assert target.format_key == "html"
        assert target.image_mode == "url"
        assert target.max_pages == 6
        assert target.thread_id == 7001234, "串号不该被编辑改动"
        assert app.persisted == 1, "改完要立刻存盘"
    finally:
        _close(edit)
        _close(dialog)


def test_watch_edit_dialog_rejects_a_bad_page_cap(dialog_root, artifacts_dir, monkeypatch):
    """页数上限填坏值时不许存盘，窗口也要留着让人改。"""
    target = gui.WatchTarget("7001234", format_key="html", max_pages=3)
    app, dialog, edit, target = _open_watch_edit(dialog_root, artifacts_dir, [target])
    warnings: list[tuple] = []
    monkeypatch.setattr(
        gui.messagebox, "showwarning", lambda *a, **kw: warnings.append(a)
    )
    try:
        edit.pages_var.set("很多")

        edit._save()

        assert warnings, "填坏了要提示，不能悄悄忽略"
        assert "页数上限认不出" in warnings[0][1]
        assert target.max_pages == 3, "坏值不该写进目标"
        assert app.persisted == 0
        assert edit.winfo_exists(), "窗口要留着，别把人填的其它项一起丢掉"
    finally:
        _close(edit)
        _close(dialog)


def test_watch_edit_dialog_greys_the_image_box_for_formats_without_images(
    dialog_root, artifacts_dir
):
    """TXT 用不上图片处理：下拉框灰掉，免得用户以为能选。"""
    target = gui.WatchTarget("7001234", format_key="txt")
    app, dialog, edit, target = _open_watch_edit(dialog_root, artifacts_dir, [target])
    try:
        assert str(edit.image_box.cget("state")) == "disabled"

        edit.format_box.current(edit._format_keys.index("epub"))
        edit.format_box.event_generate("<<ComboboxSelected>>")
        dialog_root.update()

        assert str(edit.image_box.cget("state")) == "readonly"

        # v0.20.0 起 Markdown 也能「存到同名目录」，所以它同样算带图格式。
        edit.format_box.current(edit._format_keys.index("markdown"))
        edit.format_box.event_generate("<<ComboboxSelected>>")
        dialog_root.update()

        assert str(edit.image_box.cget("state")) == "readonly"
    finally:
        _close(edit)
        _close(dialog)


# ---------------------------------------------------------------------------
# v0.17.3：主窗口关掉时，登录对话框与它开的浏览器一起收掉
# ---------------------------------------------------------------------------


def _open_browser_login(live_app):
    """在主窗口下开一个「用浏览器登录」对话框（不会真的起浏览器：没有事件循环）。"""
    live_app.root.app = live_app  # 生产里这一层是 LoginDialog；这里用主窗口顶替
    return gui.BrowserLoginDialog(
        live_app.root, client=None, settings=live_app.settings
    )


def test_run_binds_the_close_button_to_the_app_close_handler(monkeypatch):
    """点 × 要走 App.on_close（先收活再退），不是直接退出进程。"""
    closed: list[int] = []

    class ClosingRoot(FakeRoot):
        def __init__(self) -> None:
            super().__init__()
            self.protocol_calls: list[tuple] = []

        def protocol(self, name, func=None):
            self.protocol_calls.append((name, func))

    class ClosingApp:
        def __init__(self, root) -> None:
            pass

        def on_close(self) -> None:
            closed.append(1)

    root = ClosingRoot()
    monkeypatch.setattr(gui.tk, "Tk", lambda: root)
    monkeypatch.setattr(gui, "App", ClosingApp)

    gui.run()

    assert root.protocol_calls and root.protocol_calls[0][0] == "WM_DELETE_WINDOW"
    assert callable(root.protocol_calls[0][1])


def test_browser_login_dialog_registers_itself_on_the_app(live_app):
    """对话框要把自己登记到主窗口：主窗口被关掉时才有机会喊它收尾（v0.17.3）。"""
    dialog = _open_browser_login(live_app)
    try:
        assert dialog in live_app._login_dialogs
        dialog.destroy()
        assert dialog not in live_app._login_dialogs, "销毁后要摘掉，别留死引用"
    finally:
        try:
            if dialog.winfo_exists():
                dialog.destroy()
        except tk.TclError:  # pragma: no cover
            pass


def test_on_close_cancels_login_dialogs_and_defers_destroy(live_app, monkeypatch):
    """关主窗口：先让登录对话框走正常收尾（停线程 + 关浏览器），再延后销毁主窗。"""
    dialog = _open_browser_login(live_app)
    scheduled: list[tuple] = []
    monkeypatch.setattr(
        live_app.root, "after", lambda ms, func=None, *a: scheduled.append((ms, func))
    )
    try:
        live_app.on_close()
        assert dialog._closing is True, "对话框要按「取消」这条正路收尾"
        assert dialog not in live_app._login_dialogs
        assert live_app._closing is True
        assert scheduled and scheduled[0][0] == 1200, "给导出线程留一点收手时间再销毁"
    finally:
        try:
            if dialog.winfo_exists():
                dialog.destroy()
        except tk.TclError:  # pragma: no cover
            pass
        live_app._closing = False


def test_login_worker_releases_the_browser_when_starting_fails(live_app, monkeypatch):
    """启动失败也要把已经建出来的浏览器句柄收掉 —— 否则那扇窗留在屏幕上（v0.17.3）。"""
    from types import SimpleNamespace

    from xdao import browser_login as bl

    dialog = _open_browser_login(live_app)
    created: list = []

    class FailingBrowser:
        def __init__(self, *args, **kwargs):
            self.stopped = False
            created.append(self)

        def start(self) -> None:
            raise RuntimeError("端口一直没接上")

        def stop(self) -> None:
            self.stopped = True

        def cleanup_temp_profile(self) -> None:
            pass

    backend = SimpleNamespace(
        user_data_dir=lambda base: Path(base) / "browser-profile",
        LoginBrowser=FailingBrowser,
    )
    monkeypatch.setattr(gui, "_load_browser_login", lambda: backend)
    monkeypatch.setattr(
        gui,
        "_find_login_browser",
        lambda settings: bl.BrowserInfo("假浏览器", "C:/假.exe"),
    )
    try:
        gui.BrowserLoginDialog._worker(dialog)
        assert created and created[0].stopped is True, "失败分支必须释放浏览器"
        assert dialog._browser is None
        queued = []
        while True:
            try:
                queued.append(dialog._queue.get_nowait())
            except queue.Empty:
                break
        assert ("error", "打开浏览器失败：端口一直没接上") in queued
    finally:
        try:
            if dialog.winfo_exists():
                dialog.destroy()
        except tk.TclError:  # pragma: no cover
            pass

def test_app_registers_itself_on_the_root_window(live_app):
    """子窗口靠 master.app 找到主窗口（v0.17.3）：这条断了登录窗就不再登记。"""
    assert live_app.root.app is live_app


def test_login_worker_writes_each_failed_attempt_into_the_log(live_app, monkeypatch):
    """v0.17.5：登录线程要把「哪几扇没成」逐行写进运行日志。

    真机上「一次点击弹出好几扇窗」时，界面上只有最后成的那扇；没有这几行，
    事后对不出候选表里试过几个目录、每一扇为什么被放弃。
    """
    from types import SimpleNamespace

    from xdao import browser_login as bl

    dialog = _open_browser_login(live_app)

    class NotesBrowser:
        def __init__(self, *args, **kwargs):
            self.attempt_notes = (
                "第 1 扇没成：Edge 开在 临时资料目录 A，刚起来就退出了（退出码 21）",
                "第 2 扇没成：Edge 开在 持久资料目录 B，调试端口一直没写出来",
            )
            self.census_note = ""
            self.profile_note = ""
            self.browser_note = ""
            self.handoff_note = ""
            self.browser_ws_url = "ws://127.0.0.1:9/devtools/browser/x"
            self.devtools_failure_hint = ""

        def start(self) -> None:
            # 起得来：候选表里的失败行在 start() 返回之后才入队（v0.17.5），
            # 所以这里要让流程走到「日志行已入队、后一步才坏」。
            pass

        def stop(self) -> None:
            pass

        def cleanup_temp_profile(self) -> None:
            pass

    def _boom(*args, **kwargs):
        raise RuntimeError("连不上调试接口")

    backend = SimpleNamespace(
        user_data_dir=lambda base: Path(base) / "browser-profile",
        LoginBrowser=NotesBrowser,
        _new_session=_boom,
    )
    monkeypatch.setattr(gui, "_load_browser_login", lambda: backend)
    monkeypatch.setattr(
        gui,
        "_find_login_browser",
        lambda settings: bl.BrowserInfo("假浏览器", "C:/假.exe"),
    )
    try:
        gui.BrowserLoginDialog._worker(dialog)
        queued = []
        while True:
            try:
                queued.append(dialog._queue.get_nowait())
            except queue.Empty:
                break
        notes = [text for kind, text in queued if kind == "note"]
        assert (
            "第 1 扇没成：Edge 开在 临时资料目录 A，刚起来就退出了（退出码 21）" in notes
        ), "每一扇失败的窗都要落进运行日志"
        assert "第 2 扇没成：Edge 开在 持久资料目录 B，调试端口一直没写出来" in notes
        assert any(kind == "error" for kind, _ in queued)
    finally:
        try:
            if dialog.winfo_exists():
                dialog.destroy()
        except tk.TclError:  # pragma: no cover
            pass


# ------------------------------------------- v0.17.6：失败时也要吐日志


def test_login_worker_writes_the_attempt_notes_when_every_candidate_fails(
    live_app, monkeypatch
):
    """v0.17.6：候选表全栽的时候，日志里更要有「哪几扇没成」和普查看到了什么。

    v0.17.5 只把这些行放在成功路径上调队 —— 越是整条路都失败，日志里反而越空，
    恰好丢掉事后定位最需要的那段（哪一步、第几个目录、什么死因）。
    """
    from types import SimpleNamespace

    from xdao import browser_login as bl

    dialog = _open_browser_login(live_app)
    created: list = []

    class DeadAllBrowser:
        def __init__(self, *args, **kwargs):
            self.attempt_notes = (
                "第 1 扇没成：Edge 开在 临时资料目录 A，刚起来就退出了（退出码 21）",
                "第 2 扇没成：Chrome 开在 持久资料目录 B，调试端口一直没写出来",
            )
            self.census_note = "开窗前普查：探到 1 扇还活着的程序窗口（A），挨个问了一遍都没接上。"
            self.stopped = False
            created.append(self)

        def start(self) -> None:
            raise RuntimeError("每个候选目录都没起来")

        def stop(self) -> None:
            self.stopped = True

        def cleanup_temp_profile(self) -> None:
            pass

    backend = SimpleNamespace(
        user_data_dir=lambda base: Path(base) / "browser-profile",
        LoginBrowser=DeadAllBrowser,
    )
    monkeypatch.setattr(gui, "_load_browser_login", lambda: backend)
    monkeypatch.setattr(
        gui,
        "_find_login_browser",
        lambda settings: bl.BrowserInfo("假浏览器", "C:/假.exe"),
    )
    try:
        gui.BrowserLoginDialog._worker(dialog)
        queued = []
        while True:
            try:
                queued.append(dialog._queue.get_nowait())
            except queue.Empty:
                break
        notes = [payload for kind, payload in queued if kind == "note"]
        assert created and created[0].stopped is True, "失败分支仍要释放浏览器"
        first = next(n for n in notes if n.startswith("第 1 扇没成"))
        second = next(n for n in notes if n.startswith("第 2 扇没成"))
        assert notes.index(first) < notes.index(second), "顺序要与真发生的顺序一致"
        assert (
            "census",
            "开窗前普查：探到 1 扇还活着的程序窗口（A），挨个问了一遍都没接上。",
        ) in queued
        assert any(kind == "error" for kind, _ in queued)
    finally:
        try:
            if dialog.winfo_exists():
                dialog.destroy()
        except tk.TclError:  # pragma: no cover
            pass


# ---------- 批量断点续跑（v0.18.0）----------


def test_settings_dialog_saves_skip_existing(dialog_root, artifacts_dir):
    """勾上「成品已存在就跳过」要写进配置——不然下次启动又变回关闭。"""
    settings = AppSettings(_path=artifacts_dir / "config.json")

    dialog = _open_settings(dialog_root, settings)
    try:
        assert dialog.skip_existing_var.get() is False
        dialog.skip_existing_var.set(True)
        dialog._save()
    finally:
        _close(dialog)

    assert settings.skip_existing is True
    saved = json.loads((artifacts_dir / "config.json").read_text(encoding="utf-8"))
    assert saved["skip_existing"] is True


def test_gui_export_passes_skip_existing_and_says_so(artifacts_dir, monkeypatch):
    """开关打开时：交给导出器，并在日志里留一句，别让人误以为全都重导了。"""
    _patch_gui_export(monkeypatch)
    recorded: list[dict] = []
    monkeypatch.setattr(
        gui, "create_exporter", lambda *a, **k: recorded.append(k) or _RecordingExporter()
    )
    settings = AppSettings(_path=artifacts_dir / "config.json")
    settings.userhash = "TESTHASH"
    settings.skip_existing = True

    app = _FakeApp(settings, artifacts_dir / "out")
    gui.App.start(app, ["https://www.nmbxd1.com/t/50000001"])

    assert [kwargs["skip_existing"] for kwargs in recorded] == [True]
    assert any("已存在就跳过" in line for line in app.logs)


def test_gui_export_keeps_quiet_when_skip_existing_is_off(artifacts_dir, monkeypatch):
    _patch_gui_export(monkeypatch)
    recorded: list[dict] = []
    monkeypatch.setattr(
        gui, "create_exporter", lambda *a, **k: recorded.append(k) or _RecordingExporter()
    )
    settings = AppSettings(_path=artifacts_dir / "config.json")
    settings.userhash = "TESTHASH"

    app = _FakeApp(settings, artifacts_dir / "out")
    gui.App.start(app, ["https://www.nmbxd1.com/t/50000001"])

    assert [kwargs["skip_existing"] for kwargs in recorded] == [False]
    assert not any("已存在就跳过" in line for line in app.logs)

# ------------------------------------------------------ 图片存到同名目录（v0.19.0）


def test_the_image_dropdown_offers_the_folder_mode(live_app):
    """四个选项：内嵌 / 仅链接 / 丢弃 / 存到同名目录（顺序与 IMAGE_MODES 一致）。"""
    values = tuple(live_app.image_mode_box.cget("values"))
    assert values == gui.IMAGE_MODE_LABELS
    assert len(values) == 4
    assert values[3] == "存到同名目录"
    assert live_app._image_mode_keys[3] == "folder"


def test_choosing_the_folder_mode_sticks_in_the_settings(live_app):
    live_app.image_mode_box.current(3)
    live_app.persist_prefs()
    assert live_app.settings.image_mode == "folder"


def test_the_edit_dialog_accepts_the_folder_mode():
    """编辑监控的下拉框与主窗口共用一份选项，别漏了第四个。"""
    assert "folder" in gui.WatchEditDialog.IMAGE_KEYS


def test_folder_mode_with_a_non_html_format_says_it_falls_back(live_app):
    """PDF / EPUB 没有同名成品可写：开跑时说清楚这次按内嵌处理。"""
    live_app.image_mode_box.current(3)
    live_app.format_box.current(live_app._format_keys.index("pdf"))
    live_app._log_image_mode_note()
    logs = live_app.log_text.get("1.0", "end-1c")
    assert "「存到同名目录」只对 HTML 与 Markdown 生效" in logs


def test_folder_mode_with_html_says_nothing(live_app):
    """HTML 是能用这个模式的格式，别多打一行废话。"""
    live_app.image_mode_box.current(3)
    live_app.format_box.current(live_app._format_keys.index("html"))
    live_app._log_image_mode_note()
    assert "只对 HTML 生效" not in live_app.log_text.get("1.0", "end-1c")


# ---------------------------------------------------------------------------
# 从文件读串号清单（v0.20.0）
# ---------------------------------------------------------------------------


def _listing(tmp_path: Path, body: str) -> Path:
    target = tmp_path / "清单.txt"
    target.write_text(body, encoding="utf-8")
    return target


def test_load_thread_list_appends_what_the_file_lists(
    live_app, tmp_path, monkeypatch
) -> None:
    chosen = _listing(tmp_path, "# 攒的\n7001002\n7001003\n")
    monkeypatch.setattr(gui.filedialog, "askopenfilename", lambda **kwargs: str(chosen))

    live_app.load_thread_list()

    text = live_app.urls_text.get("1.0", "end-1c")
    assert "7001002" in text
    assert "7001003" in text
    assert "从清单读到 2 个串：新增 2 个，已有 0 个" in live_app.log_text.get("1.0", "end-1c")


def test_load_thread_list_keeps_a_newline_before_the_new_entries(
    live_app, tmp_path, monkeypatch
) -> None:
    """输入框里已有内容且末尾没有换行时，别把新串号粘在过去那一行后面。"""
    live_app.urls_text.insert("1.0", "https://www.nmbxd1.com/t/50000001")
    chosen = _listing(tmp_path, "7001002\n")
    monkeypatch.setattr(gui.filedialog, "askopenfilename", lambda **kwargs: str(chosen))

    live_app.load_thread_list()

    lines = [line for line in live_app.urls_text.get("1.0", "end").splitlines() if line.strip()]
    assert lines == ["https://www.nmbxd1.com/t/50000001", "7001002"]


def test_load_thread_list_skips_entries_that_are_already_there(
    live_app, tmp_path, monkeypatch
) -> None:
    live_app.urls_text.insert("1.0", "50000001\n")
    chosen = _listing(tmp_path, "50000001\n7001002\n")
    monkeypatch.setattr(gui.filedialog, "askopenfilename", lambda **kwargs: str(chosen))

    live_app.load_thread_list()

    body = live_app.urls_text.get("1.0", "end-1c")
    assert body.count("50000001") == 1
    assert "新增 1 个，已有 1 个" in live_app.log_text.get("1.0", "end-1c")


def test_load_thread_list_says_so_when_nothing_new_is_added(
    live_app, tmp_path, monkeypatch
) -> None:
    live_app.urls_text.insert("1.0", "50000001\n")
    chosen = _listing(tmp_path, "50000001\n")
    monkeypatch.setattr(gui.filedialog, "askopenfilename", lambda **kwargs: str(chosen))

    live_app.load_thread_list()

    assert "都已经在输入框里了" in live_app.log_text.get("1.0", "end-1c")


def test_load_thread_list_reports_a_bad_file_and_keeps_the_box(
    live_app, tmp_path, monkeypatch
) -> None:
    chosen = _listing(tmp_path, "50000001\n不是串号\n")
    monkeypatch.setattr(gui.filedialog, "askopenfilename", lambda **kwargs: str(chosen))
    calls = _messagebox_calls(monkeypatch)

    live_app.load_thread_list()

    assert calls and calls[-1][0] == "showerror"
    assert "第 2 行" in calls[-1][2]
    assert live_app.urls_text.get("1.0", "end-1c").strip() == ""


def test_load_thread_list_does_nothing_when_the_dialog_is_cancelled(
    live_app, monkeypatch
) -> None:
    monkeypatch.setattr(gui.filedialog, "askopenfilename", lambda **kwargs: "")

    live_app.load_thread_list()

    assert live_app.urls_text.get("1.0", "end-1c").strip() == ""

# ---------------------------------------------------------------------------
# 合并导出（v0.21.0）：默认一个串一份文件，勾了才合并
# ---------------------------------------------------------------------------


def _drain_logs(app) -> list[str]:
    """把导出线程塞进队列的日志取出来（假窗口的 ``after`` 不会自己轮询）。"""
    lines: list[str] = []
    while not app._export_queue.empty():
        kind, payload = app._export_queue.get_nowait()
        if kind == "log":
            lines.append(payload)
    return lines


class _MergeExporter:
    """记录 save / save_many 各被叫了几次。"""

    supports_merge = True

    def __init__(self) -> None:
        self.saved: list[object] = []
        self.merged: list[int] = []

    def save(self, thread, scope, path, include_hashes=False):
        self.saved.append(thread)
        return Path(path) / "结果.html"

    def save_many(self, threads, scope, path, include_hashes=None):
        self.merged.append(len(threads))
        return Path(path) / "合并.html"


def test_settings_dialog_starts_from_the_saved_merge_flag(dialog_root, artifacts_dir):
    settings = AppSettings(_path=artifacts_dir / "config.json")
    settings.merge_export = True

    dialog = _open_settings(dialog_root, settings)
    try:
        assert dialog.merge_export_var.get() is True
    finally:
        _close(dialog)


def test_settings_dialog_saves_the_merge_flag(dialog_root, artifacts_dir):
    settings = AppSettings(_path=artifacts_dir / "config.json")

    dialog = _open_settings(dialog_root, settings)
    try:
        assert dialog.merge_export_var.get() is False  # 默认关
        dialog.merge_export_var.set(True)
        dialog._save()
    finally:
        _close(dialog)

    assert settings.merge_export is True


def test_gui_export_merges_when_the_setting_is_on(artifacts_dir, monkeypatch):
    _patch_gui_export(monkeypatch)
    exporter = _MergeExporter()
    monkeypatch.setattr(gui, "create_exporter", lambda *a, **k: exporter)
    settings = AppSettings(_path=artifacts_dir / "config.json")
    settings.userhash = "TESTHASH"
    settings.merge_export = True

    app = _FakeApp(settings, artifacts_dir / "out")
    gui.App.start(
        app,
        [
            "https://www.nmbxd1.com/t/50000001",
            "https://www.nmbxd1.com/t/7001002",
        ],
    )

    assert exporter.merged == [2]
    assert exporter.saved == []
    assert any("合并完成" in line for line in _drain_logs(app))
    assert any("合并导出：这次导出的串写进同一份文件" in line for line in app.logs)


def test_gui_export_keeps_one_file_per_thread_when_merge_is_off(artifacts_dir, monkeypatch):
    _patch_gui_export(monkeypatch)
    exporter = _MergeExporter()
    monkeypatch.setattr(gui, "create_exporter", lambda *a, **k: exporter)
    settings = AppSettings(_path=artifacts_dir / "config.json")
    settings.userhash = "TESTHASH"

    app = _FakeApp(settings, artifacts_dir / "out")
    gui.App.start(
        app,
        [
            "https://www.nmbd1.com/t/50000001",
            "https://www.nmbd1.com/t/7001002",
        ],
    )

    assert exporter.merged == []
    assert len(exporter.saved) == 2
    assert not any("合并导出" in line for line in app.logs)


def test_gui_export_says_why_a_format_cannot_merge(artifacts_dir, monkeypatch):
    _patch_gui_export(monkeypatch)

    class _PlainExporter:
        supports_merge = False

        def save(self, thread, scope, path, include_hashes=False):
            return Path(path) / "一份.epub"

    monkeypatch.setattr(gui, "create_exporter", lambda *a, **k: _PlainExporter())
    settings = AppSettings(_path=artifacts_dir / "config.json")
    settings.userhash = "TESTHASH"
    settings.merge_export = True

    app = _FakeApp(settings, artifacts_dir / "out")
    gui.App.start(app, ["https://www.nmbxd1.com/t/50000001"])

    assert any("不支持合并导出" in line for line in _drain_logs(app))



# ---------- 监控开关与移除选中（v0.21.7） ----------


class _WatchLoopApp(_WatchApp):
    """记下「开始监控」被怎么调用的，另加 `check_watch_once`。"""

    def __init__(self, settings, targets, root=None) -> None:
        super().__init__(settings, targets, root)
        self.started: list[tuple[float, bool]] = []
        self.stopped = 0
        self.checked = 0

    def start_watching(self, interval, verify_cached) -> None:
        self.started.append((interval, verify_cached))
        self._watching = True

    def stop_watching(self) -> None:
        self.stopped += 1
        self._watching = False

    def check_watch_once(self) -> None:
        self.checked += 1


def _watch_loop_app(artifacts_dir, targets) -> _WatchLoopApp:
    settings = AppSettings(_path=artifacts_dir / "config.json")
    return _WatchLoopApp(settings, targets)


def _two_watch_targets() -> list:
    return [
        gui.WatchTarget("https://www.nmbxd1.com/t/50000001"),
        gui.WatchTarget("https://www.nmbxd1.com/t/7002222", format_key="markdown"),
    ]


def test_watch_dialog_start_records_the_interval_and_turns_the_switch_on(
    dialog_root, artifacts_dir
) -> None:
    app = _watch_loop_app(artifacts_dir, _two_watch_targets())
    dialog = _open_watch(dialog_root, app)
    try:
        dialog.interval_var.set("90")
        dialog.verify_var.set(True)
        dialog._start()

        assert app.started == [(90.0, True)]
        assert app.settings.watch_interval == 90.0
        assert app.settings.verify_cached is True
        assert app.persisted >= 1, "开始监控前要把列表写回配置"
        assert dialog.toggle_button.cget("text") == "停止监控"
    finally:
        _close(dialog)


def test_watch_dialog_start_rounds_a_short_interval_up_to_fifteen_seconds(
    dialog_root, artifacts_dir
) -> None:
    app = _watch_loop_app(artifacts_dir, _two_watch_targets())
    dialog = _open_watch(dialog_root, app)
    try:
        dialog.interval_var.set("2")
        dialog._start()
        assert app.started == [(15.0, False)]
    finally:
        _close(dialog)


def test_watch_dialog_start_falls_back_to_five_minutes_when_the_input_is_junk(
    dialog_root, artifacts_dir
) -> None:
    app = _watch_loop_app(artifacts_dir, _two_watch_targets())
    dialog = _open_watch(dialog_root, app)
    try:
        dialog.interval_var.set("半小时")
        dialog._start()
        assert app.started == [(300.0, False)]
    finally:
        _close(dialog)


def test_watch_dialog_start_refuses_an_empty_list(
    dialog_root, artifacts_dir, monkeypatch
) -> None:
    app = _watch_loop_app(artifacts_dir, [])
    dialog = _open_watch(dialog_root, app)
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        gui.messagebox, "showwarning", lambda title, text, **kwargs: calls.append((title, text))
    )
    try:
        dialog._start()
    finally:
        _close(dialog)

    assert app.started == []
    assert calls and "先添加" in calls[0][1]


def test_watch_dialog_toggle_starts_then_stops(dialog_root, artifacts_dir) -> None:
    app = _watch_loop_app(artifacts_dir, _two_watch_targets())
    dialog = _open_watch(dialog_root, app)
    try:
        dialog._toggle()
        running = app._watching
        dialog._toggle()
        text = dialog.toggle_button.cget("text")
    finally:
        _close(dialog)

    assert running is True
    assert app._watching is False
    assert app.stopped == 1
    assert text == "开始监控"


def test_watch_dialog_remove_selected_drops_the_rows_and_persists(
    dialog_root, artifacts_dir
) -> None:
    targets = _two_watch_targets()
    app = _watch_loop_app(artifacts_dir, targets)
    dialog = _open_watch(dialog_root, app)
    try:
        dialog.tree.selection_set(str(id(targets[0])))
        dialog._remove_selected()
        left = [t.thread_id for t in app.watch_targets]
        persisted = len(app.settings.watch_targets)
        stopped = app.stopped
    finally:
        _close(dialog)

    assert left == [7002222]
    assert persisted == 1
    assert stopped == 0, "还剩一条时不该把监控停掉"


def test_watch_dialog_removing_the_last_row_stops_a_running_loop(
    dialog_root, artifacts_dir
) -> None:
    targets = _two_watch_targets()
    app = _watch_loop_app(artifacts_dir, targets)
    app._watching = True
    dialog = _open_watch(dialog_root, app)
    try:
        dialog.tree.selection_set(str(id(targets[0])), str(id(targets[1])))
        dialog._remove_selected()
        empty = app.watch_targets == []
        stopped = app.stopped
    finally:
        _close(dialog)

    assert empty is True
    assert stopped == 1


def test_watch_dialog_check_now_refuses_an_empty_list(
    dialog_root, artifacts_dir, monkeypatch
) -> None:
    app = _watch_loop_app(artifacts_dir, [])
    dialog = _open_watch(dialog_root, app)
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        gui.messagebox, "showwarning", lambda title, text, **kwargs: calls.append((title, text))
    )
    try:
        dialog._check_now()
    finally:
        _close(dialog)

    assert app.checked == 0
    assert calls and "先添加" in calls[0][1]


def test_watch_dialog_check_now_triggers_one_round(dialog_root, artifacts_dir) -> None:
    app = _watch_loop_app(artifacts_dir, _two_watch_targets())
    dialog = _open_watch(dialog_root, app)
    try:
        dialog._check_now()
        text = dialog.status_var.get()
    finally:
        _close(dialog)

    assert app.checked == 1
    assert "已触发" in text

# ---------- 监控窗口：编辑选中（v0.21.9）----------


def test_edit_selected_asks_to_pick_one_first(dialog_root, artifacts_dir, monkeypatch) -> None:
    """一行都没选中时只提示，不开编辑窗。"""
    settings = AppSettings(_path=artifacts_dir / "config.json")
    app = _AddTargetApp(settings, [], dialog_root)
    dialog = _open_watch(dialog_root, app)
    opened: list[object] = []
    monkeypatch.setattr(gui, "WatchEditDialog", lambda *args: opened.append(args))
    calls = _messagebox_calls(monkeypatch)
    try:
        dialog.tree.selection_remove(dialog.tree.selection())
        dialog._edit_selected()
    finally:
        _close(dialog)

    assert opened == []
    assert calls and calls[0][0] == "showinfo"
    assert "请先选中" in calls[0][2]


def test_edit_selected_opens_the_editor_for_the_chosen_row(
    dialog_root, artifacts_dir, monkeypatch
) -> None:
    """选中一行：按 id 找到那条目标，把对话框和它一起交出去。"""
    target = gui.WatchTarget("7001234", format_key="html")
    settings = AppSettings(_path=artifacts_dir / "config.json")
    app = _AddTargetApp(settings, [target], dialog_root)
    dialog = _open_watch(dialog_root, app)
    opened: list[tuple] = []
    monkeypatch.setattr(gui, "WatchEditDialog", lambda *args: opened.append(args))
    try:
        dialog.tree.selection_set(str(id(target)))
        dialog._edit_selected()
    finally:
        _close(dialog)

    assert len(opened) == 1
    parent, chosen = opened[0]
    assert parent is dialog
    assert chosen is target


def test_edit_selected_ignores_a_row_that_is_no_longer_in_the_list(
    dialog_root, artifacts_dir, monkeypatch
) -> None:
    """列表刚被刷新、树里还留着一个旧 iid 时：安静返回，不抛异常。"""
    settings = AppSettings(_path=artifacts_dir / "config.json")
    app = _AddTargetApp(settings, [], dialog_root)
    dialog = _open_watch(dialog_root, app)
    opened: list[object] = []
    monkeypatch.setattr(gui, "WatchEditDialog", lambda *args: opened.append(args))
    try:
        dialog.tree.insert("", "end", iid="999999", values=("7001999", "全部", "HTML", "内嵌图片", ""))
        dialog.tree.selection_set("999999")
        dialog._edit_selected()
    finally:
        _close(dialog)

    assert opened == []

# ---------- 主窗口几个动作的角落分支（v0.21.10）----------


class _Var:
    """最小的 StringVar / BooleanVar 替身：只记值。"""

    def __init__(self, value="") -> None:
        self._value = value

    def get(self):  # noqa: ANN201
        return self._value

    def set(self, value):  # noqa: ANN001, ANN201
        self._value = value


def _bare_app(**attrs):  # noqa: ANN003, ANN202
    app = object.__new__(gui.App)
    for name, value in attrs.items():
        setattr(app, name, value)
    return app


def test_retry_failed_says_there_is_nothing_to_retry(monkeypatch) -> None:
    app = _bare_app(_last_failed=[])
    calls: list[tuple] = []
    monkeypatch.setattr(gui.messagebox, "showinfo", lambda *a, **k: calls.append(a))

    app.retry_failed()

    assert calls == [("提示", "没有失败项需要重试。")]


def test_retry_failed_hands_the_list_back_to_start(monkeypatch) -> None:
    started: list[object] = []
    logs: list[str] = []
    app = _bare_app(_last_failed=["50000001", "12345678"])
    app.start = lambda urls=None: started.append(urls)  # noqa: E731
    app.log = logs.append

    app.retry_failed()

    assert started == [["50000001", "12345678"]]
    assert logs == ["重试 2 个失败项。"]
    # 传下去的是副本：start() 里再改也不会动到 _last_failed
    assert started[0] is not app._last_failed


def test_poll_export_swallows_a_broken_log_and_keeps_polling() -> None:
    """日志写入抛异常不该让整条轮询断掉；还在导出就继续排下一次。"""
    import queue

    scheduled: list[tuple] = []

    class _Root:
        def after(self, delay, callback):  # noqa: ANN001, ANN201
            scheduled.append((delay, callback.__name__))

    app = _bare_app(
        _export_queue=queue.Queue(),
        _exporting=True,
        _export_stop=None,
        root=_Root(),
        progress_bar=_FakeWidget(),
        progress_var=_Var(),
    )

    def boom(_text):  # noqa: ANN001
        raise RuntimeError("日志框没了")

    app.log = boom
    app._export_queue.put(("log", "这一句会炸"))

    app._poll_export()

    assert scheduled == [(50, "_poll_export")]


def test_check_watch_once_asks_for_a_target_first(monkeypatch) -> None:
    warned: list[tuple] = []
    app = _bare_app(watch_targets=[], _exporting=False, _watching=False)
    monkeypatch.setattr(gui.messagebox, "showwarning", lambda *a, **k: warned.append(a))

    app.check_watch_once()

    assert warned == [("提示", "请先添加要监控的串。")]


def test_check_watch_once_asks_for_an_output_dir_next(monkeypatch) -> None:
    from xdao.watcher import WatchTarget

    warned: list[tuple] = []
    app = _bare_app(
        watch_targets=[WatchTarget("50000001", format_key="html")],
        output_var=_Var("   "),
        _exporting=False,
        _watching=False,
    )
    monkeypatch.setattr(gui.messagebox, "showwarning", lambda *a, **k: warned.append(a))

    app.check_watch_once()

    assert warned == [("提示", "请先选择导出目录。")]


def test_choose_folder_keeps_the_current_dir_when_cancelled(monkeypatch) -> None:
    refreshed: list[bool] = []
    app = _bare_app(output_var=_Var("D:\\XY"), _dir_pinned=False)
    app.refresh_cache_info = lambda: refreshed.append(True)
    monkeypatch.setattr(gui.filedialog, "askdirectory", lambda **kwargs: "")

    app.choose_folder()

    assert app.output_var.get() == "D:\\XY"
    assert app._dir_pinned is False
    assert refreshed == []


def test_choose_folder_pins_the_new_dir_and_refreshes_the_cache_line(monkeypatch) -> None:
    refreshed: list[bool] = []
    app = _bare_app(output_var=_Var("D:\\XY"), _dir_pinned=False)
    app.refresh_cache_info = lambda: refreshed.append(True)
    monkeypatch.setattr(gui.filedialog, "askdirectory", lambda **kwargs: "E:\\新目录")

    app.choose_folder()

    assert app.output_var.get() == "E:\\新目录"
    assert app._dir_pinned is True
    assert refreshed == [True]


def test_refresh_cache_info_counts_threads_and_images(tmp_path, monkeypatch) -> None:
    cache_dir = tmp_path / "cache"
    (cache_dir / "threads").mkdir(parents=True)
    (cache_dir / "threads" / "50000001.json").write_text("{}", encoding="utf-8")
    (cache_dir / "threads" / "12345678.json").write_text("{}", encoding="utf-8")
    (cache_dir / "images" / "sub").mkdir(parents=True)
    (cache_dir / "images" / "sub" / "a.jpg").write_bytes(b"x" * 1024)
    (cache_dir / "images" / "b.jpg").write_bytes(b"x" * 1024)

    from types import SimpleNamespace

    info = _Var()
    app = _bare_app(
        cache_info_var=info,
        output_var=_Var("D:\\XY"),
        settings=SimpleNamespace(resolved_cache_dir=lambda _chosen: cache_dir),
    )
    monkeypatch.setattr(gui, "resolve_cache_dir", lambda _requested: (cache_dir, ""))

    app.refresh_cache_info()

    text = info.get()
    assert text.startswith("缓存：2 个串 · 2 张图片 · ")
    assert str(cache_dir) in text
    assert "已自动改用这里" not in text

# ---------- 致命错误兜底、监控窗复用、下载页（v0.21.10）----------


def test_report_fatal_puts_the_original_error_in_a_plain_dialog(monkeypatch) -> None:
    shown: list[tuple] = []
    import tkinter.messagebox as _mb

    monkeypatch.setattr(_mb, "showerror", lambda *a, **k: shown.append(a))

    gui._report_fatal("界面起不来", RuntimeError("配置坏了"))

    assert len(shown) == 1
    title, text = shown[0]
    assert title == "界面起不来"
    assert "RuntimeError: 配置坏了" in text
    assert "配置文件损坏" in text
    assert "详细信息" in text


def test_report_fatal_never_raises_even_if_the_dialog_is_broken(monkeypatch) -> None:
    def boom(*args, **kwargs):  # noqa: ANN002, ANN003, ANN201
        raise RuntimeError("连对话框都弹不出来")

    import tkinter.messagebox as _mb

    monkeypatch.setattr(_mb, "showerror", boom)

    gui._report_fatal("界面起不来", ValueError("随便什么错"))  # 不该抛


def test_open_watch_reuses_the_dialog_that_is_already_on_screen() -> None:
    calls: list[str] = []

    class _Dialog:
        def winfo_exists(self) -> bool:
            return True

        def lift(self) -> None:
            calls.append("lift")

        def focus_set(self) -> None:
            calls.append("focus")

    app = object.__new__(gui.App)
    app._watch_dialog = _Dialog()

    app.open_watch()

    assert calls == ["lift", "focus"]
    assert isinstance(app._watch_dialog, _Dialog)


def test_open_watch_creates_a_dialog_when_there_is_none(monkeypatch) -> None:
    made: list[object] = []

    class _Dialog:
        def __init__(self, app):  # noqa: ANN001
            made.append(app)

    monkeypatch.setattr(gui, "WatchDialog", _Dialog)
    app = object.__new__(gui.App)
    app._watch_dialog = None

    app.open_watch()

    assert made == [app]
    assert isinstance(app._watch_dialog, _Dialog)


def test_open_download_page_hands_the_url_to_the_browser(monkeypatch) -> None:
    opened: list[str] = []
    import webbrowser as _wb

    monkeypatch.setattr(_wb, "open", lambda url: opened.append(url))
    app = object.__new__(gui.App)
    app.log = lambda _text: None

    app._open_download_page("https://example.invalid/releases")

    assert opened == ["https://example.invalid/releases"]


def test_open_download_page_logs_the_url_when_the_browser_refuses(monkeypatch) -> None:
    logs: list[str] = []

    def boom(_url):  # noqa: ANN001
        raise OSError("没有默认浏览器")

    import webbrowser as _wb

    monkeypatch.setattr(_wb, "open", boom)
    app = object.__new__(gui.App)
    app.log = logs.append

    app._open_download_page("https://example.invalid/releases")

    assert logs == ["下载页：https://example.invalid/releases"]

# ---------- 工作线程内层函数的成败两分支（v0.21.11）----------


class _ImmediateThread:
    """替身线程：start() 就地跑完，用例里不用等、也不用 join。"""

    def __init__(self, target=None, daemon=None, **kwargs) -> None:  # noqa: ANN001
        self._target = target

    def start(self) -> None:
        if self._target is not None:
            self._target()


def _no_threads(monkeypatch) -> None:
    from types import SimpleNamespace

    monkeypatch.setattr(gui, "threading", SimpleNamespace(Thread=_ImmediateThread))


class _Button:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def config(self, **kwargs) -> None:  # noqa: ANN003
        self.calls.append(kwargs)


class _Form:
    hash_value = "form-hash"
    captcha_bytes = b"not-an-image"


def test_refresh_captcha_reports_the_load_failure(monkeypatch) -> None:
    import queue as _queue
    from types import SimpleNamespace

    _no_threads(monkeypatch)
    dialog = object.__new__(gui.LoginDialog)
    dialog._busy = False
    dialog._result_queue = _queue.Queue()
    dialog.captcha_button = _Button()
    dialog.after = lambda *a: None

    def boom():  # noqa: ANN202
        raise OSError("连不上站点")

    dialog.client = SimpleNamespace(fetch_login_form=boom)

    dialog._refresh_captcha()

    assert dialog._result_queue.get_nowait() == ("form_error", "连不上站点")


def test_refresh_captcha_hands_the_form_over_on_success(monkeypatch) -> None:
    import queue as _queue
    from types import SimpleNamespace

    _no_threads(monkeypatch)
    dialog = object.__new__(gui.LoginDialog)
    dialog._busy = False
    dialog._result_queue = _queue.Queue()
    dialog.captcha_button = _Button()
    dialog.after = lambda *a: None
    form = _Form()
    dialog.client = SimpleNamespace(fetch_login_form=lambda: form)

    dialog._refresh_captcha()

    assert dialog._result_queue.get_nowait() == ("form", form)


def test_do_login_reports_the_failure_and_leaves_the_button_alone(monkeypatch) -> None:
    import queue as _queue
    from types import SimpleNamespace

    _no_threads(monkeypatch)
    dialog = object.__new__(gui.LoginDialog)
    dialog._busy = False
    dialog._result_queue = _queue.Queue()
    dialog.login_button = _Button()
    dialog.after = lambda *a: None
    dialog.update_idletasks = lambda: None
    dialog.email_var = SimpleNamespace(get=lambda: "a@b.c")
    dialog.password_var = SimpleNamespace(get=lambda: "pw")
    dialog.verify_var = SimpleNamespace(get=lambda: "1234")
    dialog.form = _Form()

    def boom(*args, **kwargs):  # noqa: ANN002, ANN003, ANN201
        raise RuntimeError("账号或密码不对")

    dialog.client = SimpleNamespace(login=boom)

    dialog._do_login()

    assert dialog._result_queue.get_nowait() == ("fail", "账号或密码不对")


def test_do_login_passes_the_form_hash_and_returns_the_userhash(monkeypatch) -> None:
    import queue as _queue
    from types import SimpleNamespace

    _no_threads(monkeypatch)
    seen: list[tuple] = []
    dialog = object.__new__(gui.LoginDialog)
    dialog._busy = False
    dialog._result_queue = _queue.Queue()
    dialog.login_button = _Button()
    dialog.after = lambda *a: None
    dialog.update_idletasks = lambda: None
    dialog.email_var = SimpleNamespace(get=lambda: "a@b.c")
    dialog.password_var = SimpleNamespace(get=lambda: "pw")
    dialog.verify_var = SimpleNamespace(get=lambda: "1234")
    dialog.form = _Form()
    dialog.client = SimpleNamespace(login=lambda *a: seen.append(a) or "hash-xyz")

    dialog._do_login()

    assert seen == [("a@b.c", "pw", "1234", "form-hash")]
    assert dialog._result_queue.get_nowait() == ("ok", "hash-xyz")


def test_pick_cookies_turns_a_text_reply_into_an_error(monkeypatch) -> None:
    import queue as _queue
    from types import SimpleNamespace

    _no_threads(monkeypatch)
    app = object.__new__(gui.App)
    app._cookie_queue = _queue.Queue()
    app.pick_cookie_button = _Button()
    app.root = SimpleNamespace(after=lambda *a: None)
    app.apply_settings_to_client = lambda: None
    app._collect_urls = lambda: ["https://www.nmbxd1.com/t/50000001"]
    app.client = SimpleNamespace(fetch_thread_page=lambda *a: "串不存在")

    app.pick_cookies()

    assert app._cookie_queue.get_nowait() == ("fail", "串不存在")


def test_pick_cookies_reports_the_site_side_error(monkeypatch) -> None:
    import queue as _queue
    from types import SimpleNamespace

    _no_threads(monkeypatch)
    app = object.__new__(gui.App)
    app._cookie_queue = _queue.Queue()
    app.pick_cookie_button = _Button()
    app.root = SimpleNamespace(after=lambda *a: None)
    app.apply_settings_to_client = lambda: None
    app._collect_urls = lambda: ["https://www.nmbxd1.com/t/50000001"]
    app.client = SimpleNamespace(
        fetch_thread_page=lambda *a: {"success": False, "error": "需要登录"}
    )

    app.pick_cookies()

    assert app._cookie_queue.get_nowait() == ("fail", "需要登录")


def test_pick_cookies_passes_the_parsed_posts_up(monkeypatch) -> None:
    import queue as _queue
    from types import SimpleNamespace

    _no_threads(monkeypatch)
    posts = [object()]
    app = object.__new__(gui.App)
    app._cookie_queue = _queue.Queue()
    app.pick_cookie_button = _Button()
    app.root = SimpleNamespace(after=lambda *a: None)
    app.apply_settings_to_client = lambda: None
    app._collect_urls = lambda: ["https://www.nmbxd1.com/t/50000001"]
    app.client = SimpleNamespace(
        fetch_thread_page=lambda *a: {"data": "ok"},
        parse_thread_page=lambda payload, page: {"posts": posts},
    )

    app.pick_cookies()

    assert app._cookie_queue.get_nowait() == ("ok", posts)

# ---------- 监控窗与设置窗的角落分支（v0.21.12）----------


class _SilentApp(_AddTargetApp):
    """能改「输入框里有什么」「饼干框里有什么」的假 App。"""

    def __init__(
        self,
        settings,
        targets,
        root=None,
        urls=None,
        hashes=None,
        max_pages: int = 0,
    ) -> None:
        super().__init__(settings, targets, root, max_pages=max_pages)
        self._urls = list(urls or [])
        self._hashes = list(hashes or [])

    def _collect_urls(self):
        return list(self._urls)

    def parse_hashes(self):
        return list(self._hashes)


def test_watch_add_from_input_refuses_with_an_empty_box(dialog_root, artifacts_dir, monkeypatch):
    """输入框空着就点「添加」：提示一句，别往列表里塞空气。"""
    settings = AppSettings(_path=artifacts_dir / "config.json")
    app = _SilentApp(settings, [], dialog_root, urls=[])
    calls = _messagebox_calls(monkeypatch)

    dialog = _open_watch(dialog_root, app)
    try:
        dialog._add_from_input()
    finally:
        _close(dialog)

    assert calls == [("showwarning", "提示", "请先在主窗口的输入框里填写串网址。")]
    assert app.persisted == 0


def test_watch_add_from_input_skips_junk_and_duplicates(dialog_root, artifacts_dir):
    """一锅乱炖的输入：认不出的跳过、已经在列表里的跳过、剩下的照加。"""
    settings = AppSettings(_path=artifacts_dir / "config.json")
    existing = gui.WatchTarget("https://www.nmbxd1.com/t/7001234")
    app = _SilentApp(
        settings,
        [existing],
        dialog_root,
        urls=[
            "不是网址",
            "https://www.nmbxd1.com/t/7001234",
            "https://www.nmbxd1.com/t/7009999",
        ],
    )

    dialog = _open_watch(dialog_root, app)
    try:
        dialog._add_from_input()
    finally:
        _close(dialog)

    assert [t.thread_id for t in app.watch_targets] == [7001234, 7009999]
    assert app.persisted == 1, "加完要存盘"


def test_watch_add_from_input_says_when_nothing_was_added(dialog_root, artifacts_dir, monkeypatch):
    settings = AppSettings(_path=artifacts_dir / "config.json")
    app = _SilentApp(settings, [], dialog_root, urls=["垃圾", "还是垃圾"])
    calls = _messagebox_calls(monkeypatch)

    dialog = _open_watch(dialog_root, app)
    try:
        dialog._add_from_input()
    finally:
        _close(dialog)

    assert calls == [("showinfo", "提示", "没有新增（可能已存在，或网址无法识别）。")]
    assert app.watch_targets == []


def test_watch_export_does_nothing_when_the_save_dialog_is_cancelled(
    dialog_root, artifacts_dir, monkeypatch
):
    settings = AppSettings(_path=artifacts_dir / "config.json")
    app = _WatchApp(settings, [gui.WatchTarget("7001234")])
    asked: list[dict] = []
    monkeypatch.setattr(
        gui.filedialog, "asksaveasfilename", lambda **k: asked.append(k) or ""
    )
    monkeypatch.setattr(gui.messagebox, "showinfo", lambda *a, **k: None)

    dialog = _open_watch(dialog_root, app)
    try:
        before = dialog.status_var.get()
        dialog._export_list()
        after = dialog.status_var.get()
    finally:
        _close(dialog)

    assert asked, "空列表以外的情况本该弹出保存对话框"
    assert after == before, "取消保存不该改状态栏"


def test_watch_export_reports_a_write_failure(dialog_root, artifacts_dir, monkeypatch):
    from xdao import watch_list

    settings = AppSettings(_path=artifacts_dir / "config.json")
    app = _WatchApp(settings, [gui.WatchTarget("7001234")])
    calls = _messagebox_calls(monkeypatch)
    monkeypatch.setattr(gui.filedialog, "asksaveasfilename", lambda **k: str(artifacts_dir / "x.json"))

    def boom(*args, **kwargs):
        raise watch_list.WatchListError("磁盘满了")

    monkeypatch.setattr(watch_list, "export_targets", boom)

    dialog = _open_watch(dialog_root, app)
    try:
        dialog._export_list()
    finally:
        _close(dialog)

    assert ("showerror", "导出失败", "磁盘满了") in calls


def test_watch_import_does_nothing_when_the_open_dialog_is_cancelled(
    dialog_root, artifacts_dir, monkeypatch
):
    settings = AppSettings(_path=artifacts_dir / "config.json")
    app = _WatchApp(settings, [gui.WatchTarget("7001234")])
    monkeypatch.setattr(gui.filedialog, "askopenfilename", lambda **k: "")

    dialog = _open_watch(dialog_root, app)
    try:
        dialog._import_list()
    finally:
        _close(dialog)

    assert [t.thread_id for t in app.watch_targets] == [7001234]
    assert app.persisted == 0


def test_watch_import_lists_at_most_ten_skipped_entries(dialog_root, artifacts_dir, monkeypatch):
    """跳过 12 条时只列前 10 条，末尾交代还剩几条——免得对话框被撑到屏幕外。"""
    import json

    settings = AppSettings(_path=artifacts_dir / "config.json")
    app = _WatchApp(settings, [])
    source = artifacts_dir / "来.json"
    source.write_text(
        json.dumps({"targets": [{"url_or_id": f"70012{i}0", "format_key": "docx"} for i in range(12)]}),
        encoding="utf-8",
    )
    shown: list[str] = []
    monkeypatch.setattr(gui.filedialog, "askopenfilename", lambda **k: str(source))
    monkeypatch.setattr(gui.messagebox, "showinfo", lambda title, text, **k: shown.append(text))

    dialog = _open_watch(dialog_root, app)
    try:
        dialog._import_list()
    finally:
        _close(dialog)

    assert shown and "…另有 2 条。" in shown[0]
    assert shown[0].count("格式认不出：docx") == 10


def test_watch_import_of_an_empty_file_says_so(dialog_root, artifacts_dir, monkeypatch):
    import json

    settings = AppSettings(_path=artifacts_dir / "config.json")
    app = _WatchApp(settings, [])
    source = artifacts_dir / "空.json"
    source.write_text(json.dumps({"targets": []}), encoding="utf-8")
    shown: list[str] = []
    monkeypatch.setattr(gui.filedialog, "askopenfilename", lambda **k: str(source))
    monkeypatch.setattr(gui.messagebox, "showinfo", lambda title, text, **k: shown.append(text))

    dialog = _open_watch(dialog_root, app)
    try:
        dialog._import_list()
    finally:
        _close(dialog)

    assert shown and "（文件里一条都没有。）" in shown[0]


def test_watch_edit_refuses_the_hashes_scope_without_any_hash(dialog_root, artifacts_dir, monkeypatch):
    """选了「指定饼干」但主窗口一个串号都没填：当场说清楚，别存下去。"""
    target = gui.WatchTarget("7001234", format_key="html")
    app, dialog, edit, target = _open_watch_edit(dialog_root, artifacts_dir, [target])
    calls = _messagebox_calls(monkeypatch)
    try:
        edit.scope_box.current(2)
        edit._save()

        assert calls and calls[0][0] == "showwarning"
        assert "「指定饼干」要用主窗口「只看指定饼干」里填的串号" in calls[0][2]
        assert app.persisted == 0
        assert edit.winfo_exists(), "没保存就不该关窗，用户还要改回来"
    finally:
        _close(edit)
        _close(dialog)


def test_watch_edit_refuses_a_negative_page_cap(dialog_root, artifacts_dir, monkeypatch):
    target = gui.WatchTarget("7001234", format_key="html")
    app, dialog, edit, target = _open_watch_edit(dialog_root, artifacts_dir, [target])
    calls = _messagebox_calls(monkeypatch)
    try:
        edit.pages_var.set("-3")
        edit._save()

        assert ("showwarning", "提示", "页数上限不能是负数（0 表示不限）。") in calls
        assert app.persisted == 0
    finally:
        _close(edit)
        _close(dialog)


def test_settings_dialog_keeps_good_numbers_when_the_input_is_junk(dialog_root, artifacts_dir):
    """超时/重试填了非数字：回落到默认值，而不是把配置写成 0 或 NaN。"""
    from xdao.settings import DEFAULT_RETRIES, DEFAULT_TIMEOUT

    settings = AppSettings(_path=artifacts_dir / "config.json")
    settings.timeout = 33.0
    settings.retries = 7
    dialog = _open_settings(dialog_root, settings)
    try:
        dialog.timeout_var.set("很久")
        dialog.retries_var.set("几次")
        dialog._save()
    finally:
        _close(dialog)

    assert settings.timeout == DEFAULT_TIMEOUT
    assert settings.retries == DEFAULT_RETRIES


def test_settings_dialog_picks_the_cache_and_browser_folders(dialog_root, artifacts_dir, monkeypatch):
    settings = AppSettings(_path=artifacts_dir / "config.json")
    dialog = _open_settings(dialog_root, settings)
    try:
        monkeypatch.setattr(gui.filedialog, "askdirectory", lambda **k: "E:\\缓存目录")
        monkeypatch.setattr(gui.filedialog, "askopenfilename", lambda **k: "C:\\浏览器.exe")
        dialog._choose_cache()
        dialog._choose_browser()
        assert dialog.cache_var.get() == "E:\\缓存目录"
        assert dialog.browser_var.get() == "C:\\浏览器.exe"

        # 取消（返回空串）时保留原值
        monkeypatch.setattr(gui.filedialog, "askdirectory", lambda **k: "")
        monkeypatch.setattr(gui.filedialog, "askopenfilename", lambda **k: "")
        dialog._choose_cache()
        dialog._choose_browser()
        assert dialog.cache_var.get() == "E:\\缓存目录"
    finally:
        _close(dialog)


def test_update_button_text_survives_a_missing_or_dead_button(monkeypatch):
    """按钮没了（窗口关了）或底层窗口已销毁：写文案要悄悄放过，不能抛。"""
    import tkinter as tk

    app = _bare_app(_update_button=None)
    app._update_button_text("随便")  # 没有按钮：直接返回

    class _Dead:
        def winfo_exists(self) -> bool:
            return False

        def config(self, **kwargs):  # pragma: no cover - 不该被调用
            raise AssertionError("按钮不存在时不该写文案")

    app._update_button = _Dead()
    app._update_button_text("随便")

    class _Gone:
        def winfo_exists(self) -> bool:
            return True

        def config(self, **kwargs):
            raise tk.TclError("窗口已经销毁")

    app._update_button = _Gone()
    app._update_button_text("随便")


def test_refresh_cache_info_survives_unreadable_files(tmp_path, monkeypatch):
    """统计缓存大小时个别文件 stat 失败（被占用/权限）：跳过它，别让整行消失。"""
    import pathlib as _pathlib

    cache_dir = tmp_path / "cache"
    (cache_dir / "threads").mkdir(parents=True)
    (cache_dir / "threads" / "50000001.json").write_text("{}", encoding="utf-8")
    (cache_dir / "images").mkdir()
    (cache_dir / "images" / "a.jpg").write_bytes(b"x")

    real_stat = _pathlib.Path.stat
    seen: dict[str, int] = {}

    def flaky(self, *args, **kwargs):
        key = str(self)
        seen[key] = seen.get(key, 0) + 1
        # .json 只被显式 stat 一次（统计大小）→ 直接失败；
        # .jpg 先被 is_file() 问一次（必须成功，否则连图片都数不出来），
        # 第二次（统计大小）再失败 —— 正好走到两个 except OSError 分支。
        if self.suffix == ".json" or (self.suffix == ".jpg" and seen[key] > 1):
            raise OSError("读不了")
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr(_pathlib.Path, "stat", flaky)
    info = _Var()
    app = _bare_app(
        cache_info_var=info,
        output_var=_Var("D:\\XY"),
        settings=__import__("types").SimpleNamespace(resolved_cache_dir=lambda _chosen: cache_dir),
    )
    monkeypatch.setattr(gui, "resolve_cache_dir", lambda _requested: (cache_dir, ""))

    app.refresh_cache_info()

    assert info.get().startswith("缓存：1 个串 · 1 张图片 · 0.0 MB")


def test_refresh_cache_info_explains_a_fallback_directory(tmp_path, monkeypatch):
    cache_dir = tmp_path / "temp-cache"
    cache_dir.mkdir()
    info = _Var()
    app = _bare_app(
        cache_info_var=info,
        output_var=_Var("D:\\XY"),
        settings=__import__("types").SimpleNamespace(resolved_cache_dir=lambda _chosen: cache_dir),
    )
    monkeypatch.setattr(
        gui,
        "resolve_cache_dir",
        lambda _requested: (cache_dir, "原目录写不进去"),
    )

    app.refresh_cache_info()

    assert "（原目录" in info.get()
    assert "已自动改用这里" in info.get()


def test_offer_download_offers_the_in_place_upgrade(dialog_root, monkeypatch):
    """能原地升级时先问一句「现在下载并安装吗」，同意就走升级流程。"""
    from types import SimpleNamespace

    from xdao import updater

    monkeypatch.setattr(updater, "plan_upgrade", lambda: SimpleNamespace(possible=True, reason=""))
    monkeypatch.setattr(gui.messagebox, "askyesno", lambda *a, **k: True)
    app = _UpdateApp(dialog_root)

    class _Result:
        url = "https://example.invalid/dl"
        asset_url = "https://example.invalid/pkg.zip"
        asset_size = 12 * 1024 * 1024

        def line(self) -> str:
            return "有新版本 1.2.3"

    gui.App._offer_download(app, _Result())

    assert len(app.upgrades_started) == 1


def test_offer_download_says_why_it_cannot_upgrade_in_place(dialog_root, monkeypatch):
    from types import SimpleNamespace

    from xdao import updater

    monkeypatch.setattr(
        updater,
        "plan_upgrade",
        lambda: SimpleNamespace(possible=False, reason="当前不是免安装版，不能原地升级。"),
    )
    monkeypatch.setattr(gui.messagebox, "askyesno", lambda *a, **k: False)
    app = _UpdateApp(dialog_root)

    class _Result:
        url = "https://example.invalid/dl"
        asset_url = ""
        asset_size = 0

        def line(self) -> str:
            return "有新版本 1.2.3"

    gui.App._offer_download(app, _Result())

    assert any("不能原地升级" in line for line in app.logged)


def test_offer_download_logs_the_line_when_there_is_no_url(dialog_root, monkeypatch):
    from types import SimpleNamespace

    from xdao import updater

    monkeypatch.setattr(updater, "plan_upgrade", lambda: SimpleNamespace(possible=False, reason=""))
    app = _UpdateApp(dialog_root)

    class _Result:
        url = ""
        asset_url = ""
        asset_size = 0

        def line(self) -> str:
            return "有新版本，但找不到下载地址"

    gui.App._offer_download(app, _Result())

    assert app.logged == ["有新版本，但找不到下载地址"]
