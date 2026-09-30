"""桌面通知的单元测试。

这里**不真的弹通知、不真的响铃**：全部通过注入 ``runner`` / ``beep`` /
``platform`` 来观察外部调用，所以离线也能跑，CI 上也不会弹窗。
"""

from __future__ import annotations

import base64
import sys
from pathlib import Path

import pytest

from xdao.notifications import (
    SENTINEL_AUMID,
    Notifier,
    _applescript_quote,
    _encode_command,
    _ps_quote,
    _xml_escape,
    message_for,
    toast_xml,
)
from xdao.watcher import WatchResult, WatchTarget, notify_result


class RecordingRunner:
    """记录被调用的命令行，永远"成功"。"""

    def __init__(self, returncode: int = 0) -> None:
        self.calls: list[list[str]] = []
        self.returncode = returncode

    def __call__(self, argv, timeout):  # noqa: ANN001
        self.calls.append(list(argv))
        return self.returncode == 0


@pytest.fixture()
def windows_notifier():
    runner = RecordingRunner()
    beeps: list[int] = []
    notifier = Notifier(
        app_id="测试程序",
        min_interval=0.0,
        runner=runner,
        platform="win32",
        beep=lambda: (beeps.append(1), True)[1],
    )
    return notifier, runner, beeps


# ---------- 转义 ----------


def test_xml_escape_handles_markup():
    assert _xml_escape("A&B <C> \"D\" 'E'") == "A&amp;B &lt;C&gt; &quot;D&quot; &apos;E&apos;"


def test_toast_xml_escapes_both_texts():
    xml = toast_xml("标题 & 副标题", "正文 <b>不该被当标签</b>")
    assert "&amp;" in xml and "&lt;b&gt;" in xml
    assert xml.startswith("<toast>") and xml.endswith("</toast>")


def test_ps_quote_doubles_single_quotes():
    assert _ps_quote("it's") == "'it''s'"


def test_applescript_quote_escapes_quotes_and_backslash():
    assert _applescript_quote('a"b\\c') == '"a\\"b\\\\c"'


def test_encode_command_is_utf16le_base64():
    script = "write-host 你好"
    assert base64.b64decode(_encode_command(script)).decode("utf-16-le") == script


# ---------- Windows ----------


def test_windows_notify_calls_powershell_with_encoded_toast(windows_notifier, monkeypatch):
    notifier, runner, _ = windows_notifier
    monkeypatch.setattr("shutil.which", lambda name: r"C:\ps\powershell.exe")
    assert notifier.notify("标题", "正文") is True
    assert len(runner.calls) == 1
    argv = runner.calls[0]
    assert argv[0] == r"C:\ps\powershell.exe"
    assert "-EncodedCommand" in argv
    script = base64.b64decode(argv[-1]).decode("utf-16-le")
    assert "ToastNotificationManager" in script
    # app_id 原样传给 CreateToastNotifier；标题里带上程序名，用户才知道是谁发的
    assert "CreateToastNotifier('测试程序')" in script
    assert "X岛串导出工具：标题" in script


def test_windows_toast_is_followed_by_a_beep(windows_notifier, monkeypatch):
    """Toast 默认静音，配一声提示音；响不出来也不算失败。"""
    notifier, runner, beeps = windows_notifier
    monkeypatch.setattr("shutil.which", lambda name: r"C:\ps\powershell.exe")
    assert notifier.notify("标题", "正文") is True
    assert beeps == [1]


def test_beep_failure_does_not_undo_the_toast(monkeypatch):
    def boom():
        raise OSError("没有音频设备")

    notifier = Notifier(runner=RecordingRunner(), platform="win32", beep=boom)
    monkeypatch.setattr("shutil.which", lambda name: r"C:\ps\powershell.exe")
    assert notifier.notify("标题", "正文") is True


def test_windows_falls_back_to_beep_without_powershell(windows_notifier, monkeypatch):
    notifier, runner, beeps = windows_notifier
    monkeypatch.setattr("shutil.which", lambda name: None)
    assert notifier.notify("标题", "正文") is True
    assert runner.calls == []
    assert beeps == [1]


def test_windows_returns_false_when_toast_and_beep_both_fail(monkeypatch):
    notifier = Notifier(runner=RecordingRunner(returncode=1), platform="win32", beep=lambda: False)
    monkeypatch.setattr("shutil.which", lambda name: r"C:\ps\powershell.exe")
    assert notifier.notify("标题", "正文") is False


def test_default_app_id_is_the_windows_sentinel():
    """实测：用应用名当 app_id 时 Windows 什么都不显示，必须用哨兵 AUMID。"""
    assert Notifier().app_id == SENTINEL_AUMID
    assert SENTINEL_AUMID.startswith("{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}")


def test_default_toast_uses_the_sentinel_aumid(monkeypatch):
    runner = RecordingRunner()
    notifier = Notifier(runner=runner, platform="win32", beep=lambda: True)
    monkeypatch.setattr("shutil.which", lambda name: r"C:\ps\powershell.exe")
    assert notifier.notify("标题", "正文") is True
    script = base64.b64decode(runner.calls[0][-1]).decode("utf-16-le")
    assert "{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}" in script


def test_windows_returncode_nonzero_is_failure(monkeypatch):
    notifier = Notifier(runner=RecordingRunner(returncode=1), platform="win32", beep=lambda: False)
    monkeypatch.setattr("shutil.which", lambda name: r"C:\ps\powershell.exe")
    assert notifier.notify("标题", "正文") is False


# ---------- 其它平台 ----------


def test_macos_uses_osascript(monkeypatch):
    runner = RecordingRunner()
    notifier = Notifier(runner=runner, platform="darwin")
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/osascript")
    assert notifier.notify("标题", "正文") is True
    assert runner.calls[0][0] == "osascript"
    assert "display notification" in runner.calls[0][2]


def test_linux_uses_notify_send(monkeypatch):
    runner = RecordingRunner()
    notifier = Notifier(runner=runner, platform="linux")
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/notify-send")
    assert notifier.notify("标题", "正文") is True
    assert runner.calls[0] == ["notify-send", "标题", "正文"]


def test_linux_without_notify_send_returns_false(monkeypatch):
    runner = RecordingRunner()
    notifier = Notifier(runner=runner, platform="linux")
    monkeypatch.setattr("shutil.which", lambda name: None)
    assert notifier.notify("标题", "正文") is False
    assert runner.calls == []


# ---------- 开关、节流、异常 ----------


def test_disabled_notifier_never_calls_out(monkeypatch):
    runner = RecordingRunner()
    notifier = Notifier(enabled=False, runner=runner, platform="linux")
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/notify-send")
    assert notifier.notify("标题", "正文") is False
    assert runner.calls == []


def test_same_key_is_throttled(monkeypatch):
    runner = RecordingRunner()
    notifier = Notifier(min_interval=900.0, runner=runner, platform="linux")
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/notify-send")
    assert notifier.notify("标题", "正文", key="k") is True
    assert notifier.notify("标题", "正文", key="k") is False
    assert len(runner.calls) == 1
    # 换个 key 不受影响
    assert notifier.notify("标题", "正文", key="other") is True
    # 不带 key 就等同于不节流
    assert notifier.notify("标题", "正文") is True


def test_reset_clears_throttle(monkeypatch):
    runner = RecordingRunner()
    notifier = Notifier(min_interval=900.0, runner=runner, platform="linux")
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/notify-send")
    notifier.notify("a", "b", key="k")
    notifier.reset()
    assert notifier.notify("a", "b", key="k") is True


def test_runner_exception_is_swallowed(monkeypatch):
    def boom(argv, timeout):  # noqa: ANN001
        raise OSError("模拟命令启动失败")

    notifier = Notifier(runner=boom, platform="linux")
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/notify-send")
    assert notifier.notify("标题", "正文") is False


def test_available_uses_platform_tools(monkeypatch):
    notifier = Notifier(platform="linux")
    monkeypatch.setattr("shutil.which", lambda name: None)
    assert notifier.available() is False
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/notify-send")
    assert notifier.available() is True
    notifier.enabled = False
    assert notifier.available() is False


def test_default_platform_is_current():
    assert Notifier()._platform == sys.platform


# ---------- 通知正文 ----------


def test_message_for_new_posts_with_export():
    text = message_for("123", 3, 100, Path("D:/out/串123.html"))
    assert "新增 3 楼" in text and "串123.html" in text


def test_message_for_update_without_new_posts():
    assert message_for("123", 0, 100, None) == "No.123 有更新（共 100 楼）"


# ---------- 与监控的衔接 ----------


def _target() -> WatchTarget:
    target = WatchTarget("50000001", format_key="html")
    return target


def test_notify_result_skips_unchanged_and_first_run():
    seen: list[tuple] = []
    notifier = Notifier(min_interval=0.0, runner=lambda a, t: seen.append((a, t)) or True, platform="linux")
    unchanged = WatchResult(target=_target(), changed=False)
    assert notify_result(notifier, unchanged) is False
    first = WatchResult(target=_target(), changed=True, first_run=True, new_posts=0, total_posts=5)
    assert notify_result(notifier, first) is False
    errored = WatchResult(target=_target(), changed=True, new_posts=2, error="炸了")
    assert notify_result(notifier, errored) is False
    assert seen == []


def test_notify_result_sends_for_new_posts(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/notify-send")
    runner = RecordingRunner()
    notifier = Notifier(min_interval=0.0, runner=runner, platform="linux")
    result = WatchResult(
        target=_target(),
        changed=True,
        new_posts=4,
        total_posts=20,
        exported=Path("D:/out/串50000001.html"),
    )
    assert notify_result(notifier, result) is True
    assert runner.calls[0][0] == "notify-send"
    assert "新增 4 楼" in runner.calls[0][2]


def test_notify_result_is_throttled_per_target(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/notify-send")
    runner = RecordingRunner()
    notifier = Notifier(min_interval=900.0, runner=runner, platform="linux")
    result = WatchResult(target=_target(), changed=True, new_posts=1, total_posts=9)
    assert notify_result(notifier, result) is True
    assert notify_result(notifier, result) is False
    assert len(runner.calls) == 1


def test_notify_result_without_notifier():
    assert notify_result(None, WatchResult(target=_target(), changed=True, new_posts=1)) is False
