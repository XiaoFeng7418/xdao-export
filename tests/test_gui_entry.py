"""图形界面入口的兜底测试。

实测过：打包版弹「Unhandled exception in script」对话框。命令行入口已经堵住了，
界面这边也必须堵 —— 尤其是打包版（--windowed）**没有 stderr**，
任何漏出去的异常都会变成 PyInstaller 的错误对话框。

文件末尾两组是 PDF 纸张/边距的接线：前一组是纯逻辑（不开窗口），
后一组要真的建 Tk 窗口（设置对话框、导出主流程），没有显示环境时跳过。
"""

from __future__ import annotations

import json
import queue
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
    """只提供 _manual_userhash 用得到的三样东西，免得真开一个 Tk 窗口。"""

    def __init__(self) -> None:
        self.client = _FakePasteClient()
        self.userhash = ""
        self.destroyed = False

    def destroy(self) -> None:
        self.destroyed = True


def _paste(monkeypatch, text):
    """把 _manual_userhash 跑一遍，收集它弹过什么框。"""
    prompts: list[tuple] = []
    warnings: list[tuple] = []
    errors: list[tuple] = []

    def fake_askstring(title, prompt, parent=None):
        prompts.append((title, prompt))
        return text

    monkeypatch.setattr(gui.simpledialog, "askstring", fake_askstring)
    monkeypatch.setattr(gui.messagebox, "showwarning", lambda *a, **k: warnings.append(a))
    monkeypatch.setattr(gui.messagebox, "showerror", lambda *a, **k: errors.append(a))

    dialog = FakeLoginDialog()
    gui.LoginDialog._manual_userhash(dialog)
    return dialog, prompts, warnings, errors


def test_manual_userhash_picks_the_value_out_of_a_whole_cookie_string(monkeypatch):
    """用户从浏览器里复制到的是整段 cookie，不该逼他自己找 userhash= 在哪。"""
    dialog, _, warnings, errors = _paste(
        monkeypatch, "other=1; userhash=ABCD1234; sid=xyz; theme=dark"
    )

    assert dialog.client.userhash == "ABCD1234"
    assert dialog.userhash == "ABCD1234"
    assert dialog.destroyed is True
    assert warnings == [] and errors == []


def test_manual_userhash_accepts_a_quoted_value_with_whitespace(monkeypatch):
    """从浏览器复制出来的值常带引号、换行和空格。"""
    dialog, _, warnings, errors = _paste(monkeypatch, '  "userhash=EF567890; path=/"  \n')

    assert dialog.client.userhash == "EF567890"
    assert dialog.destroyed is True
    assert warnings == [] and errors == []


def test_manual_userhash_explains_when_there_is_no_userhash(monkeypatch):
    """粘错了不能抛异常，也不能把垃圾值当饼干设进去。"""
    dialog, _, warnings, errors = _paste(monkeypatch, "我复制了一段别的东西")

    assert dialog.client.userhash is None
    assert dialog.destroyed is False
    assert errors == []
    assert warnings, "解析失败要弹一次提示"
    assert "userhash" in warnings[0][1]


def test_manual_userhash_does_nothing_when_the_dialog_is_cancelled(monkeypatch):
    dialog, _, warnings, errors = _paste(monkeypatch, None)

    assert dialog.client.userhash is None
    assert dialog.destroyed is False
    assert warnings == [] and errors == []


def test_manual_userhash_prompt_tells_users_not_to_open_devtools(monkeypatch):
    """提示语本身就是这次改动的交付物：整段粘贴 + 不用开发者工具。"""
    _, prompts, _, _ = _paste(monkeypatch, None)

    assert prompts, "必须先问一次要粘贴的内容"
    title, prompt = prompts[0]
    assert "粘贴" in title
    assert "整段" in prompt
    assert "不需要打开开发者工具" in prompt


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

    ``_update_done`` / ``_poll_update`` / ``_offer_download`` 直接借用真实现
    （它们的逻辑就是要测的），其余属性用假的顶上。
    """

    def __init__(self, root) -> None:
        self.root = root
        self.logged: list[str] = []
        self._update_result = None
        self._update_button = _FakeUpdateButton()
        self._checking_update = True
        self._update_queue = queue.Queue()

    def log(self, message: str) -> None:
        self.logged.append(message)

    _update_done = gui.App._update_done
    _poll_update = gui.App._poll_update
    _offer_download = gui.App._offer_download


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
    pattern = r"root\.after\(\s*\d+,\s*lambda:\s*self\.check_update\(silent=True\)"
    assert re.search(pattern, source), "App 起来之后没有安排那次新版本检查"
    assert source.count("check_update(silent=True)") == 1, "启动时只该问一次"
