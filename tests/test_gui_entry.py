"""图形界面入口的兜底测试（不需要真的开窗口）。

用户报过：打包版弹「Unhandled exception in script」对话框。命令行入口已经堵住了，
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
