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

    def __init__(self, master, client, settings) -> None:
        self.master = master
        self.client = client
        self.settings = settings
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

    def fake_dialog(master, client, settings):
        dialog = FakeBrowserLoginDialog(master, client, settings)
        dialog.failure = failure
        dialog.userhash = userhash
        return dialog

    monkeypatch.setattr(gui, "BrowserLoginDialog", fake_dialog)
    gui.LoginDialog._open_browser_login(holder)
    return holder, holder.logs, destroyed


def test_browser_login_failure_lands_in_the_run_log(monkeypatch):
    """失败原因不能只留在子窗口那行小字里。

    2026-10-01 那位用户报「登不上」时贴出来的运行日志里一条登录记录都没有 ——
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

    def fake_dialog(master, client, settings):
        dialog = FakeBrowserLoginDialog(master, client, settings)
        dialog.failure = "起不来"
        return dialog

    monkeypatch.setattr(gui, "BrowserLoginDialog", fake_dialog)
    gui.LoginDialog._open_browser_login(holder)

    assert holder.userhash is None
    assert logs == [], "没有主窗口就写不了日志，但也不许抛异常"


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
    """提示语本身就是这次改动的交付物（用户 2026-10-01 问「为什么不是同一个 Edge」）。

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
# 导出主流程：跑真的 ``App.start()``，但线程/抓取/导出全换替身（不用开窗口）
# ---------------------------------------------------------------------------


class _SyncThread:
    """把界面里起的后台线程换成「start() 就地跑」，用例才好断言。"""

    def __init__(self, target=None, daemon=None, **kwargs):
        self._target = target

    def start(self) -> None:
        self._target()


class _FakeWidget:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def config(self, **kwargs) -> None:
        self.calls.append(("config", kwargs))

    def start(self, interval=None) -> None:
        self.calls.append(("start", interval))


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

    def current_cache_dir(self):
        return self._output

    def current_image_mode(self) -> str:
        return "embed"

    def _poll_export(self) -> None:
        """``App.start()`` 结尾会 ``root.after(50, self._poll_export)``；这里什么都不做。"""
        pass


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

        def fetch(self, url):
            return SimpleNamespace(
                meta=SimpleNamespace(thread_id="69540387", title="测试串", po_hash="PO"),
                posts=[],
                reason="测试用抓取结果",
                retry_note="",
                cache_warning="",
            )

    monkeypatch.setattr(gui, "create_exporter", fake_create_exporter)
    monkeypatch.setattr(gui, "CachedThreadFetcher", _FakeFetcher)
    monkeypatch.setattr(gui.threading, "Thread", _SyncThread)

    app = _FakeApp(settings, artifacts_dir / "out")
    gui.App.start(app, ["https://www.nmbxd1.com/t/69540387"])

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
            fetch=lambda url: SimpleNamespace(
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
    gui.App.start(app, ["https://www.nmbxd1.com/t/69540387"])

    assert not any("PDF 设置" in line for line in app.logs)
    assert any(line.startswith("导出目录：") for line in app.logs)


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
    所以它验的是整条链路：按钮 → 后台线程 → ``browser_check.check_browsers``
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
    # 替身里再调 ``browser_check.check_browsers`` 会自己调自己（第一次写成那样，
    # 界面上显示的是 ``RecursionError: maximum recursion depth exceeded``）。
    real_check = browser_check.check_browsers

    def gated_check(*args, **kwargs):
        report = real_check(*args, **kwargs)
        gate.wait(20.0)  # 结果已经在手上，但先别交回主线程
        return report

    monkeypatch.setattr(browser_check, "check_browsers", gated_check)
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

    monkeypatch.setattr(browser_check, "check_browsers", boom)
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
