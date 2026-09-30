"""启动打包版 GUI，读它的窗口标题，用来判断界面到底有没有起来。

本机 `Start-Process -PassThru` 对 GUI 进程会卡住不返回，所以改成这个带超时的脚本：
启动 → 等若干秒 → 枚举该进程的顶层窗口标题 → 强制结束 → 打印结论。
标题是 PyInstaller 的错误对话框「Unhandled exception in script」就说明启动失败。

用法：python tools/gui_probe.py --exe <exe 路径> [--wait 8]
"""

from __future__ import annotations

import argparse
import ctypes
import subprocess
import sys
import time
from ctypes import wintypes


def window_titles(pid: int) -> list[str]:
    """枚举属于该进程的所有顶层窗口标题。"""
    user32 = ctypes.windll.user32
    titles: list[str] = []
    EnumWindowsProc = ctypes.WINFUNCTYPE(
        wintypes.BOOL, wintypes.HWND, wintypes.LPARAM
    )

    def callback(hwnd, _lparam):  # pragma: no cover - 需要真实桌面
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value != pid:
            return True
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        if buf.value:
            titles.append(buf.value)
        return True

    user32.EnumWindows(EnumWindowsProc(callback), 0)
    return titles


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="检查打包版 GUI 能否正常启动")
    parser.add_argument("--exe", required=True)
    parser.add_argument("--wait", type=float, default=8.0)
    args = parser.parse_args(argv)

    proc = subprocess.Popen([args.exe])
    time.sleep(args.wait)

    exited = proc.poll() is not None
    titles = [] if exited else window_titles(proc.pid)
    if not exited:
        proc.kill()
        proc.wait(timeout=10)

    print(f"exe: {args.exe}")
    print(f"等待 {args.wait:.0f} 秒后：{'已退出' if exited else '仍在运行'}")
    if exited:
        print(f"退出码: {proc.returncode}")
        print("结论：启动失败（界面没起来就退出了）")
        return 1

    print(f"窗口标题: {titles or '(无可见窗口)'}")
    bad = [t for t in titles if "unhandled exception" in t.lower() or "fatal" in t.lower()]
    if bad:
        print(f"结论：启动失败（出现 PyInstaller 错误对话框：{bad}）")
        return 1
    if not titles:
        print("结论：进程活着但没有窗口 —— 需要人工确认")
        return 2
    print("结论：界面已正常启动")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
