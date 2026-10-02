"""启动打包版 GUI，看主窗口到底有没有起来。

本机 `Start-Process -PassThru` 对 GUI 进程会卡住不返回，所以改成这个带超时的脚本：
启动 → 每 0.5 秒看一眼窗口标题（最多等到 `--wait` 用完）→ 结束进程树 → 打印结论。

**为什么不能只看「有没有窗口」**：出错时 PyInstaller 会弹一个对话框，那也是标题非空的
窗口。以前只把标题里带「Unhandled exception」「fatal」的当失败，别的标题一律报「界面已
正常启动」—— 于是「Error」「Tk」这类对话框（甚至别的程序留下的窗口）都能骗过发布清单的
最后一步。现在认主窗口标题 `EXPECTED_TITLE`（与 `xdao/gui.py` 里 `root.title("X岛串导出")`
是同一个串）：看见了才算成功；只看见别的窗口记「需要人工确认」；看见错误对话框算失败。

退出码：0 = 看见了主窗口；1 = 启动失败（路径不对、进程提前退出、出现错误对话框）；
2 = 进程活着但没看见主窗口，需要人工确认。

用法：python tools/gui_probe.py --exe <exe 路径> [--wait 8]
"""

from __future__ import annotations

import argparse
import ctypes
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path

# 主窗口标题，必须与 xdao/gui.py 里 root.title("X岛串导出") 一致
EXPECTED_TITLE = "X岛串导出"

# 出错时可能出现的对话框标题（都按小写比）。PyInstaller 的未捕获异常对话框是
# 「Unhandled exception in script」，bootloader 层的致命错误是「Fatal error detected」，
# 再老的版本写「Failed to execute script …」；后两条是 Windows 自己的崩溃对话框。
ERROR_TITLES = (
    "unhandled exception",
    "fatal error",
    "failed to execute script",
    "应用程序错误",
    "已停止工作",
)

IS_WINDOWS = sys.platform == "win32"
POLL_INTERVAL = 0.5
KILL_TIMEOUT = 10.0


def window_titles(pid: int) -> list[str]:
    """枚举属于该进程的所有可见顶层窗口标题。"""
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


def is_error_title(title: str) -> bool:
    """这个标题像不像「启动出错」的对话框。"""
    low = title.lower()
    return any(bad in low for bad in ERROR_TITLES)


def verdict(titles: list[str]) -> tuple[int, str]:
    """按看到的窗口标题给结论，返回（退出码，一句话原因）。"""
    errors = [t for t in titles if is_error_title(t)]
    if errors:
        return 1, f"启动失败（出现错误对话框：{'、'.join(errors)}）"
    mains = [t for t in titles if EXPECTED_TITLE in t]
    if mains:
        return 0, f"界面已正常启动（主窗口：{'、'.join(mains)}）"
    if titles:
        return 2, (
            f"没有看到主窗口「{EXPECTED_TITLE}」，只看到：{'、'.join(titles)}"
            " —— 需要人工确认"
        )
    return 2, "进程活着但没有可见窗口 —— 需要人工确认"


def watch(
    pid: int,
    timeout: float,
    *,
    alive=None,
    interval: float = POLL_INTERVAL,
    clock=time.monotonic,
    sleep=time.sleep,
) -> tuple[list[str], float]:
    """盯到超时为止：一看见主窗口或错误对话框就提前收工。

    返回（看到的标题，按先后顺序去重）与实际等了多久。``alive`` 给了的话，它返回
    False（进程已经没了）也提前收工。``clock`` / ``sleep`` 可以换掉，测试里不用真等。
    """
    start = clock()
    deadline = start + timeout
    seen: list[str] = []
    while True:
        if alive is not None and not alive():
            break
        for title in window_titles(pid):
            if title not in seen:
                seen.append(title)
        if any(EXPECTED_TITLE in t for t in seen) or any(is_error_title(t) for t in seen):
            break
        if clock() >= deadline:
            break
        sleep(min(interval, max(0.0, deadline - clock())))
    return seen, clock() - start


def kill_tree(proc) -> bool:
    """结束进程树；返回 True 表示它还在跑（没杀干净）。

    只 kill 主进程不够：GUI 进程可能带起子进程，win 上用 ``taskkill /T`` 连子树一起收。
    """
    if proc.poll() is not None:
        return False
    if IS_WINDOWS:
        try:
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                capture_output=True,
                check=False,
                timeout=15,
            )
        except (OSError, subprocess.TimeoutExpired):
            proc.kill()  # taskkill 用不了就退回杀主进程
    else:
        proc.kill()
    try:
        proc.wait(timeout=KILL_TIMEOUT)
    except subprocess.TimeoutExpired:
        return True
    return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="检查打包版 GUI 能否正常启动")
    parser.add_argument("--exe", required=True)
    parser.add_argument("--wait", type=float, default=8.0, help="最多等多少秒（默认 8）")
    args = parser.parse_args(argv)

    exe = Path(args.exe)
    if args.wait <= 0:
        print(f"× --wait 要给正数（现在给的是 {args.wait}）")
        print("结论：没验成（参数不对）")
        return 1
    if not exe.is_file():
        print(f"exe: {args.exe}")
        print(f"结论：启动失败（这个路径不是文件：{exe}）")
        print("      --exe 要给解压出来那个 xdao-export.exe 的完整路径，别把 <占位符> 直接抄进来")
        return 1

    proc = subprocess.Popen([str(exe)])
    titles, waited = watch(proc.pid, args.wait, alive=lambda: proc.poll() is None)
    exited = proc.poll() is not None
    leftover = kill_tree(proc)

    print(f"exe: {args.exe}")
    print(f"等了 {waited:.1f} 秒（上限 {args.wait:.0f} 秒）后：{'已退出' if exited else '仍在运行'}")
    print(f"窗口标题: {'、'.join(titles) if titles else '(无可见窗口)'}")
    if exited:
        print(f"退出码: {proc.returncode}")
        print("结论：启动失败（界面没起来就退出了）")
        print("      包可能不完整，或者被安全软件拦下了 —— 解压时要把整个文件夹一起解开")
        return 1

    code, why = verdict(titles)
    print(f"结论：{why}")
    if leftover:
        print(f"! 进程没能在 {KILL_TIMEOUT:.0f} 秒内结束，去任务管理器看一眼，别留着")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
