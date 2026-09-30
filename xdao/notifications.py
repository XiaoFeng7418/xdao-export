"""桌面通知：监控到串有更新时提醒一下。

刻意**不引入第三方依赖**（项目一直只依赖标准库）：

- Windows：调 PowerShell 的 WinRT ``Windows.UI.Notifications`` 弹一条 Toast，
  不需要安装任何模块；老系统（没有 WinRT）退回一声提示音。
- macOS：``osascript -e 'display notification ...'``。
- Linux：``notify-send``（没装就退回终端提示音）。

所有外部命令都带超时，并且**任何失败都只返回 False**——通知发不出去绝不该影响导出。
测试通过注入 ``runner`` / ``platform`` / ``beep`` 来避免真的弹窗或真的响。
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

#: 通知里显示的程序名（跟窗口标题一致）。
APP_TITLE = "X岛串导出工具"

# PowerShell 里拼 XML 再交给 WinRT；用 -EncodedCommand 是为了避开引号转义问题。
_PS_TEMPLATE = """[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null;
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] > $null;
$xml = New-Object Windows.Data.Xml.Dom.XmlDocument;
$xml.LoadXml({xml});
$toast = New-Object Windows.UI.Notifications.ToastNotification $xml;
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier({app_id}).Show($toast);
"""

# Windows 只给「注册过 AppUserModelID」的程序显示通知。这个哨兵 ID 是系统自带的
# PowerShell 注册项，用它不需要给 exe 建开始菜单快捷方式。**实测（2026-09-30）**：
# 用应用名当 app_id 时通知中心什么都没收到，用这个哨兵 ID 才真的弹出来。
SENTINEL_AUMID = (
    r"{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe"
)


@dataclass
class Notifier:
    """带节流的通知发送器。

    ``min_interval`` 秒内同一个 key 只提醒一次：监控是循环跑的，
    同一个串每轮都有更新时不能把用户烦死。
    """

    enabled: bool = True
    app_id: str = SENTINEL_AUMID
    app_title: str = APP_TITLE
    min_interval: float = 900.0
    runner: object | None = None  # 给测试注入
    platform: str | None = None
    beep: object | None = None
    _last: dict[str, float] = field(default_factory=dict, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    # ---------- 节流 ----------

    def should_notify(self, key: str) -> bool:
        """这个 key 现在允许提醒吗（同时更新内部计时）。"""
        now = time.time()
        with self._lock:
            previous = self._last.get(key)
            if previous is not None and (now - previous) < self.min_interval:
                return False
            self._last[key] = now
            return True

    def reset(self) -> None:
        with self._lock:
            self._last.clear()

    # ---------- 发送 ----------

    @property
    def _platform(self) -> str:
        return self.platform or sys.platform

    def available(self) -> bool:
        """当前环境能不能发出通知（不影响 should_notify 的节流判断）。"""
        if not self.enabled:
            return False
        plat = self._platform
        if plat.startswith("win"):
            return shutil.which("powershell") is not None or shutil.which("pwsh") is not None
        if plat == "darwin":
            return shutil.which("osascript") is not None
        return shutil.which("notify-send") is not None

    def notify(self, title: str, message: str, *, key: str | None = None) -> bool:
        """发一条通知；被节流或发送失败时返回 False。"""
        if not self.enabled:
            return False
        if key is not None and not self.should_notify(key):
            return False

        plat = self._platform
        try:
            if plat.startswith("win"):
                if self._notify_windows(title, message):
                    # Windows 的 Toast 默认是静音的，配一声提示音才不会被漏掉。
                    # 响不出来也不影响"已通知"这个结论。
                    try:
                        self._play_beep()
                    except Exception:  # noqa: BLE001
                        pass
                    return True
                return self._play_beep()
            if plat == "darwin":
                return self._notify_macos(title, message)
            return self._notify_linux(title, message)
        except Exception:  # noqa: BLE001 —— 通知失败绝不能影响导出
            return False

    # ---------- 各平台实现 ----------

    def _call(self, argv: list[str], timeout: float = 20.0) -> bool:
        runner = self.runner
        if runner is not None:
            return bool(runner(argv, timeout))
        completed = subprocess.run(
            argv,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
        )
        return completed.returncode == 0

    def _notify_windows(self, title: str, message: str) -> bool:
        power = shutil.which("powershell") or shutil.which("pwsh")
        if not power:
            return False
        script = _PS_TEMPLATE.format(
            xml=_ps_quote(toast_xml(f"{self.app_title}：{title}", message)),
            app_id=_ps_quote(self.app_id),
        )
        encoded = _encode_command(script)
        return self._call(
            [
                power,
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-EncodedCommand",
                encoded,
            ]
        )

    def _play_beep(self) -> bool:
        beep = self.beep
        if beep is not None:
            return bool(beep())
        try:
            import winsound

            winsound.MessageBeep()
            return True
        except Exception:  # noqa: BLE001
            return False

    def _notify_macos(self, title: str, message: str) -> bool:
        if not shutil.which("osascript"):
            return False
        script = f'display notification {_applescript_quote(message)} with title {_applescript_quote(title)}'
        return self._call(["osascript", "-e", script])

    def _notify_linux(self, title: str, message: str) -> bool:
        if not shutil.which("notify-send"):
            return False
        return self._call(["notify-send", title, message])


def toast_xml(title: str, message: str) -> str:
    """拼出 Toast 的 XML（文本必须转义，否则一个 & 就让整条通知发不出去）。"""
    return (
        "<toast><visual><binding template=\"ToastGeneric\">"
        f"<text>{_xml_escape(title)}</text>"
        f"<text>{_xml_escape(message)}</text>"
        "</binding></visual></toast>"
    )


def _xml_escape(text: str) -> str:
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


def _ps_quote(text: str) -> str:
    """PowerShell 单引号字符串：里面的单引号写成两个。"""
    return "'" + str(text).replace("'", "''") + "'"


def _applescript_quote(text: str) -> str:
    return '"' + str(text).replace("\\", "\\\\").replace('"', '\\"') + '"'


def _encode_command(script: str) -> str:
    import base64

    return base64.b64encode(script.encode("utf-16-le")).decode("ascii")


def message_for(thread_label: str, new_posts: int, total: int, exported: Path | None) -> str:
    """给监控结果生成一句人能读的通知正文。"""
    if new_posts > 0:
        head = f"No.{thread_label} 新增 {new_posts} 楼（共 {total} 楼）"
    else:
        head = f"No.{thread_label} 有更新（共 {total} 楼）"
    if exported is not None:
        return f"{head}\n已保存：{Path(exported).name}"
    return head
