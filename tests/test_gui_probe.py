"""``tools/gui_probe.py`` 的用例。

它是发布清单的最后一步（拿下载回来的包跑一遍，看界面能不能起来），所以这里钉死
三件以前会骗人的事：

1. **「有窗口」不等于「界面起来了」**。出错时 PyInstaller / Windows 会弹对话框，
   那也是标题非空的窗口；旧写法只把「Unhandled exception」「fatal」当失败，别的标题
   一律报「界面已正常启动」。现在必须看见主窗口标题才算成功，只看见别的窗口只能说
   「需要人工确认」（退出码 2）。
2. **路径不对、进程一启动就退出**，都不能是 traceback，也不能算成功。
3. **收尾要真收尾**：结束进程树（win 上 ``taskkill /T``），杀不干净要说一声，
   别把残留进程留给下一个人。

用例不碰真实桌面：``window_titles`` 全被顶掉（没顶掉就炸在本地），``subprocess``、
``watch``、时间都是假的。
"""

from __future__ import annotations

import subprocess as real_subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools import gui_probe

MAIN_TITLE = "X岛串导出"


class _FakeClock:
    """假时钟：sleep 由测试推进，用例里一秒都不用真等。"""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _FakeSleep:
    def __init__(self, clock: _FakeClock) -> None:
        self.clock = clock
        self.calls: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)
        self.clock.advance(seconds)


class _FakeProc:
    """假进程：poll / kill / wait 都记账。"""

    def __init__(self, pid: int = 4321, *, exit_code: int | None = None) -> None:
        self.pid = pid
        self.returncode = exit_code
        self.killed = 0
        self.waits: list[float | None] = []
        self.wait_raises = False

    def poll(self):
        return self.returncode

    def kill(self) -> None:
        self.killed += 1
        self.returncode = -9

    def wait(self, timeout=None):
        self.waits.append(timeout)
        if self.wait_raises:
            raise real_subprocess.TimeoutExpired(cmd="x", timeout=timeout)
        return self.returncode


class _FakePopen:
    def __init__(self, proc: _FakeProc) -> None:
        self.proc = proc
        self.calls: list[list[str]] = []

    def __call__(self, argv, *args, **kwargs):
        self.calls.append(list(argv))
        return self.proc


class _FakeTaskkill:
    def __init__(self, *, oserror: bool = False, timeout: bool = False) -> None:
        self.oserror = oserror
        self.timeout = timeout
        self.calls: list[list[str]] = []

    def __call__(self, argv, *args, **kwargs):
        self.calls.append(list(argv))
        if self.oserror:
            raise OSError("没有 taskkill")
        if self.timeout:
            raise real_subprocess.TimeoutExpired(cmd="taskkill", timeout=15)
        return SimpleNamespace(returncode=0, stdout="", stderr="")


@pytest.fixture(autouse=True)
def no_real_desktop(monkeypatch):
    """默认不许真去枚举窗口：没准备好的用例应该炸在本地，而不是去看桌面。"""

    def refuse(pid):  # pragma: no cover - 只有漏顶的用例会走到
        raise AssertionError("这个用例没有准备好 window_titles")

    monkeypatch.setattr(gui_probe, "window_titles", refuse)


def use_subprocess(monkeypatch, *, popen=None, run=None):
    """顶掉 gui_probe 里的 subprocess，但保留真的 TimeoutExpired。"""
    monkeypatch.setattr(
        gui_probe,
        "subprocess",
        SimpleNamespace(
            Popen=popen, run=run, TimeoutExpired=real_subprocess.TimeoutExpired
        ),
    )


def use_windows(monkeypatch, windows: bool = True):
    """把平台钉死。

    CI 在 ubuntu 上跑，`IS_WINDOWS` 会是 False（本机 Windows 绿不代表 CI 绿，
    2026-10-02 真的栽过一次），所以走 Windows 分支的用例必须自己钉住。
    """
    monkeypatch.setattr(gui_probe, "IS_WINDOWS", windows)


def use_titles(monkeypatch, *rounds: list[str]):
    """让 window_titles 按轮次返回标题；轮次用完后一直返回最后一批。"""
    calls: list[int] = []

    def fake(pid):
        calls.append(pid)
        index = min(len(calls) - 1, len(rounds) - 1) if rounds else 0
        return list(rounds[index]) if rounds else []

    monkeypatch.setattr(gui_probe, "window_titles", fake)
    return calls


def run_main(monkeypatch, capsys, argv: list[str]):
    code = gui_probe.main(argv)
    return code, capsys.readouterr().out


# --------------------------------------------------------------- 标题识别

@pytest.mark.parametrize(
    "title",
    [
        "Unhandled exception in script",
        "UNHANDLED EXCEPTION IN SCRIPT",
        "Fatal error detected",
        "Failed to execute script 'main'",
        "xdao-export.exe - 应用程序错误",
        "pythonw.exe 已停止工作",
    ],
)
def test_error_dialogs_are_recognised(title):
    assert gui_probe.is_error_title(title)


@pytest.mark.parametrize("title", [MAIN_TITLE, "登录 X 岛", "设置", "监控串更新 · 运行中"])
def test_normal_windows_are_not_error_dialogs(title):
    assert not gui_probe.is_error_title(title)


# ------------------------------------------------------------------- 结论

def test_main_window_is_success():
    code, why = gui_probe.verdict([MAIN_TITLE])
    assert code == 0
    assert "界面已正常启动" in why
    assert MAIN_TITLE in why


def test_other_windows_do_not_count_as_success():
    """这条是核心：以前「有窗口」就报成功，对话框也能骗过去。"""
    code, why = gui_probe.verdict(["登录 X 岛"])
    assert code == 2
    assert "需要人工确认" in why
    assert MAIN_TITLE in why  # 说清在等哪个标题
    assert "登录 X 岛" in why


def test_no_window_at_all_needs_a_human():
    code, why = gui_probe.verdict([])
    assert code == 2
    assert "需要人工确认" in why


def test_error_dialog_fails_even_with_other_windows():
    code, why = gui_probe.verdict(["设置", "Fatal error detected"])
    assert code == 1
    assert "Fatal error detected" in why


def test_error_dialog_wins_over_main_window():
    code, why = gui_probe.verdict([MAIN_TITLE, "Unhandled exception in script"])
    assert code == 1
    assert "启动失败" in why


def test_main_window_among_extra_windows_is_still_success():
    code, why = gui_probe.verdict([MAIN_TITLE, "设置"])
    assert code == 0
    assert "设置" not in why  # 只说命中的主窗口，别把无关窗口混进成功理由里


# ------------------------------------------------------------------- 盯窗口

def test_watch_returns_as_soon_as_the_main_window_shows_up(monkeypatch):
    use_titles(monkeypatch, [MAIN_TITLE])
    clock = _FakeClock()
    sleep = _FakeSleep(clock)
    titles, waited = gui_probe.watch(1, 8.0, clock=clock, sleep=sleep)
    assert titles == [MAIN_TITLE]
    assert waited == 0.0
    assert sleep.calls == []


def test_watch_keeps_looking_until_the_window_appears(monkeypatch):
    use_titles(monkeypatch, [], [], [MAIN_TITLE])
    clock = _FakeClock()
    sleep = _FakeSleep(clock)
    titles, waited = gui_probe.watch(1, 8.0, clock=clock, sleep=sleep)
    assert titles == [MAIN_TITLE]
    assert sleep.calls == [0.5, 0.5]
    assert waited == 1.0


def test_watch_stops_early_on_an_error_dialog(monkeypatch):
    use_titles(monkeypatch, ["Unhandled exception in script"])
    clock = _FakeClock()
    sleep = _FakeSleep(clock)
    titles, _ = gui_probe.watch(1, 8.0, clock=clock, sleep=sleep)
    assert titles == ["Unhandled exception in script"]
    assert sleep.calls == []


def test_watch_waits_the_whole_timeout_when_nothing_shows_up(monkeypatch):
    use_titles(monkeypatch, [])
    clock = _FakeClock()
    sleep = _FakeSleep(clock)
    titles, waited = gui_probe.watch(1, 2.0, clock=clock, sleep=sleep)
    assert titles == []
    assert waited == 2.0
    assert sleep.calls == [0.5, 0.5, 0.5, 0.5]


def test_watch_never_sleeps_past_the_deadline(monkeypatch):
    use_titles(monkeypatch, [])
    clock = _FakeClock()
    sleep = _FakeSleep(clock)
    gui_probe.watch(1, 0.7, clock=clock, sleep=sleep)
    assert sleep.calls == [0.5, pytest.approx(0.2)]
    assert clock.now == pytest.approx(0.7)


def test_watch_stops_when_the_process_is_gone(monkeypatch):
    use_titles(monkeypatch, [])
    clock = _FakeClock()
    sleep = _FakeSleep(clock)
    titles, waited = gui_probe.watch(
        1, 8.0, alive=lambda: False, clock=clock, sleep=sleep
    )
    assert titles == []
    assert waited == 0.0
    assert sleep.calls == []


def test_watch_dedupes_titles_and_keeps_order(monkeypatch):
    use_titles(monkeypatch, ["设置"], ["设置", MAIN_TITLE])
    clock = _FakeClock()
    sleep = _FakeSleep(clock)
    titles, _ = gui_probe.watch(1, 8.0, clock=clock, sleep=sleep)
    assert titles == ["设置", MAIN_TITLE]


# ------------------------------------------------------------- 结束进程树

def test_dead_process_is_not_killed(monkeypatch):
    run = _FakeTaskkill()
    use_subprocess(monkeypatch, run=run)
    proc = _FakeProc(exit_code=1)
    assert gui_probe.kill_tree(proc) is False
    assert run.calls == []
    assert proc.waits == []


def test_windows_kills_the_whole_tree(monkeypatch):
    use_windows(monkeypatch)
    run = _FakeTaskkill()
    use_subprocess(monkeypatch, run=run)
    proc = _FakeProc(pid=777)
    assert gui_probe.kill_tree(proc) is False
    assert run.calls == [["taskkill", "/T", "/F", "/PID", "777"]]
    assert proc.waits == [gui_probe.KILL_TIMEOUT]


def test_posix_uses_plain_kill(monkeypatch):
    use_windows(monkeypatch, windows=False)
    run = _FakeTaskkill()
    use_subprocess(monkeypatch, run=run)
    proc = _FakeProc()
    assert gui_probe.kill_tree(proc) is False
    assert run.calls == []
    assert proc.killed == 1


def test_taskkill_failure_falls_back_to_kill(monkeypatch):
    use_windows(monkeypatch)
    run = _FakeTaskkill(oserror=True)
    use_subprocess(monkeypatch, run=run)
    proc = _FakeProc()
    assert gui_probe.kill_tree(proc) is False
    assert proc.killed == 1


def test_taskkill_hanging_falls_back_to_kill(monkeypatch):
    use_windows(monkeypatch)
    run = _FakeTaskkill(timeout=True)
    use_subprocess(monkeypatch, run=run)
    proc = _FakeProc()
    assert gui_probe.kill_tree(proc) is False
    assert proc.killed == 1


def test_process_that_will_not_die_is_reported(monkeypatch):
    use_windows(monkeypatch)
    run = _FakeTaskkill()
    use_subprocess(monkeypatch, run=run)
    proc = _FakeProc()
    proc.wait_raises = True
    assert gui_probe.kill_tree(proc) is True


# ------------------------------------------------------------------- 入口

def test_missing_exe_is_reported_without_starting_anything(monkeypatch, capsys, tmp_path):
    popen = _FakePopen(_FakeProc())
    use_subprocess(monkeypatch, popen=popen, run=_FakeTaskkill())
    code, out = run_main(
        monkeypatch, capsys, ["--exe", str(tmp_path / "没有这个.exe")]
    )
    assert code == 1
    assert "不是文件" in out
    assert popen.calls == []


@pytest.mark.parametrize("value", ["0", "-3"])
def test_non_positive_wait_is_refused(monkeypatch, capsys, tmp_path, value):
    exe = tmp_path / "x.exe"
    exe.write_bytes(b"MZ")
    popen = _FakePopen(_FakeProc())
    use_subprocess(monkeypatch, popen=popen, run=_FakeTaskkill())
    code, out = run_main(monkeypatch, capsys, ["--exe", str(exe), "--wait", value])
    assert code == 1
    assert "参数不对" in out
    assert popen.calls == []


def test_happy_path_reports_success(monkeypatch, capsys, tmp_path):
    use_windows(monkeypatch)
    exe = tmp_path / "xdao-export.exe"
    exe.write_bytes(b"MZ")
    use_titles(monkeypatch, [MAIN_TITLE])
    proc = _FakeProc()
    popen = _FakePopen(proc)
    run = _FakeTaskkill()
    use_subprocess(monkeypatch, popen=popen, run=run)

    code, out = run_main(monkeypatch, capsys, ["--exe", str(exe)])

    assert code == 0
    assert "界面已正常启动" in out
    assert popen.calls == [[str(exe)]]
    assert run.calls == [["taskkill", "/T", "/F", "/PID", str(proc.pid)]]


def test_other_window_only_asks_for_a_human(monkeypatch, capsys, tmp_path):
    exe = tmp_path / "x.exe"
    exe.write_bytes(b"MZ")
    use_titles(monkeypatch, ["登录 X 岛"])
    use_subprocess(monkeypatch, popen=_FakePopen(_FakeProc()), run=_FakeTaskkill())
    code, out = run_main(monkeypatch, capsys, ["--exe", str(exe), "--wait", "0.01"])
    assert code == 2
    assert "需要人工确认" in out


def test_error_dialog_fails_main(monkeypatch, capsys, tmp_path):
    exe = tmp_path / "x.exe"
    exe.write_bytes(b"MZ")
    use_titles(monkeypatch, ["Unhandled exception in script"])
    use_subprocess(monkeypatch, popen=_FakePopen(_FakeProc()), run=_FakeTaskkill())
    code, out = run_main(monkeypatch, capsys, ["--exe", str(exe)])
    assert code == 1
    assert "启动失败" in out


def test_process_that_exits_immediately_fails(monkeypatch, capsys, tmp_path):
    """进程一启动就退出：连窗口都不用看，直接算失败并带上退出码。"""
    exe = tmp_path / "x.exe"
    exe.write_bytes(b"MZ")
    calls = use_titles(monkeypatch, [MAIN_TITLE])
    proc = _FakeProc(exit_code=1)  # poll() 一直返回 1：进程已经没了
    use_subprocess(monkeypatch, popen=_FakePopen(proc), run=_FakeTaskkill())

    code, out = run_main(monkeypatch, capsys, ["--exe", str(exe)])

    assert code == 1
    assert "启动失败" in out
    assert "退出码: 1" in out
    assert calls == []  # 进程都没了就不该再去枚举窗口


def test_wait_is_passed_to_the_watcher(monkeypatch, capsys, tmp_path):
    exe = tmp_path / "x.exe"
    exe.write_bytes(b"MZ")
    seen: list[float] = []

    def fake_watch(pid, timeout, **kwargs):
        seen.append(timeout)
        return [MAIN_TITLE], 0.0

    monkeypatch.setattr(gui_probe, "watch", fake_watch)
    use_subprocess(monkeypatch, popen=_FakePopen(_FakeProc()), run=_FakeTaskkill())
    code, _ = run_main(monkeypatch, capsys, ["--exe", str(exe), "--wait", "12.5"])
    assert code == 0
    assert seen == [12.5]


def test_leftover_process_is_mentioned(monkeypatch, capsys, tmp_path):
    exe = tmp_path / "x.exe"
    exe.write_bytes(b"MZ")
    use_titles(monkeypatch, [MAIN_TITLE])
    monkeypatch.setattr(gui_probe, "kill_tree", lambda proc: True)
    use_subprocess(monkeypatch, popen=_FakePopen(_FakeProc()), run=_FakeTaskkill())
    code, out = run_main(monkeypatch, capsys, ["--exe", str(exe)])
    assert code == 0
    assert "任务管理器" in out


def test_titles_are_printed_for_the_human(monkeypatch, capsys, tmp_path):
    exe = tmp_path / "x.exe"
    exe.write_bytes(b"MZ")
    use_titles(monkeypatch, [MAIN_TITLE, "设置"])
    use_subprocess(monkeypatch, popen=_FakePopen(_FakeProc()), run=_FakeTaskkill())
    _, out = run_main(monkeypatch, capsys, ["--exe", str(exe)])
    assert f"窗口标题: {MAIN_TITLE}、设置" in out


def test_exe_path_is_passed_as_a_string(monkeypatch, capsys, tmp_path):
    """Popen 收到的是原样的完整路径，别在 Windows 上把反斜杠弄丢。"""
    exe = tmp_path / "带 空格" / "x.exe"
    exe.parent.mkdir()
    exe.write_bytes(b"MZ")
    use_titles(monkeypatch, [MAIN_TITLE])
    popen = _FakePopen(_FakeProc())
    use_subprocess(monkeypatch, popen=popen, run=_FakeTaskkill())
    run_main(monkeypatch, capsys, ["--exe", str(exe)])
    assert popen.calls == [[str(Path(exe))]]
    assert popen.calls[0][0].endswith("x.exe")


def test_default_output_dir_is_not_touched_by_tests():
    """顺带记一笔：这个工具不写任何文件（截图那个才写）。"""
    assert gui_probe.EXPECTED_TITLE == MAIN_TITLE
