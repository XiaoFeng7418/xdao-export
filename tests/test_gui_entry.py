"""图形界面入口的兜底测试（不需要真的开窗口）。

实测过：打包版弹「Unhandled exception in script」对话框。命令行入口已经堵住了，
界面这边也必须堵 —— 尤其是打包版（--windowed）**没有 stderr**，
任何漏出去的异常都会变成 PyInstaller 的错误对话框。
"""

from __future__ import annotations

import pytest

import xdao.gui as gui


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
