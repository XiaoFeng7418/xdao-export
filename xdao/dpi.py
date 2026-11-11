"""Windows 高分屏（125% / 150% / 200%）下别把界面糊成一张拉伸的图。

Tk 自己不会声明 DPI 感知，Windows 于是按「程序不知道自己是高清屏」处理：
先把窗口按低分辨率画一遍，再把整张位图按缩放比例放大 —— 字和线条全发虚。
这里在进程启动时声明一次「按显示器缩放」，再回报系统缩放比例，界面据此把
像素尺寸（间距、左栏宽度、窗口大小）一起放大；**字号不用动**，因为声明感知
之后 Windows 会按真实 DPI 把「点」换算成像素，字号自动就对了。

两个环境变量：

* ``XDAO_DPI_SCALE``：强制指定缩放（测试与「看着不对想手动改」时用）；
* ``XDAO_NO_DPI_AWARE``：设成任意非空值就跳过声明（万一某个系统上声明后
  界面反而更糟，还能退回去）。
"""

from __future__ import annotations

import os

#: 手动指定缩放比例的环境变量（``1.25`` 之类）。认不出来就当没设。
SCALE_ENV = "XDAO_DPI_SCALE"
#: 设成非空值就不声明 DPI 感知。
SKIP_ENV = "XDAO_NO_DPI_AWARE"

#: 取 DPI 用的常量：``LOGPIXELSX`` ＝ 88（一英寸有多少像素）。
_LOGPIXELSX = 88


def enable_dpi_awareness() -> bool:
    """声明本进程「按显示器缩放」。真的声明上了返回 ``True``。

    已经声明过的进程再调一次会失败（``E_ACCESSDENIED``），这不是错误，
    所以只看有没有抛异常，不检查返回值。
    """
    if os.name != "nt":
        return False
    if os.environ.get(SKIP_ENV):
        return False
    try:
        import ctypes

        # Win8.1+：2 ＝ PROCESS_PER_MONITOR_DPI_AWARE
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
            return True
        except (AttributeError, OSError):
            pass
        # 老系统退回系统级感知（1.0 时代就有）。
        ctypes.windll.user32.SetProcessDPIAware()
        return True
    except Exception:  # noqa: BLE001 —— 声明失败只是画面糊一点，绝不能影响启动
        return False


def system_scale() -> float:
    """系统缩放比例：125% → ``1.25``，拿不到就当 ``1.0``。

    环境变量优先；否则读「一英寸多少像素」（96 是 100%）。结果收在
    ``[1.0, 3.0]`` 之间 —— 0.5 倍的显示器不存在，而超过 300% 才真是算错了。
    """
    override = os.environ.get(SCALE_ENV, "").strip()
    if override:
        try:
            value = float(override)
        except ValueError:
            value = 1.0
        return value if value >= 1.0 else 1.0
    if os.name != "nt":
        return 1.0
    try:
        import ctypes

        user32 = ctypes.windll.user32
        gdi32 = ctypes.windll.gdi32
        dc = user32.GetDC(0)
        try:
            dpi = gdi32.GetDeviceCaps(dc, _LOGPIXELSX)
        finally:
            user32.ReleaseDC(0, dc)
        return max(1.0, min(3.0, float(dpi) / 96.0))
    except Exception:  # noqa: BLE001
        return 1.0


def describe(scale: float) -> str:
    """把比例写成「125%」，日志里好念。"""
    try:
        percent = int(round(float(scale) * 100))
    except (TypeError, ValueError):
        percent = 100
    return f"{percent}%"


__all__ = ["SCALE_ENV", "SKIP_ENV", "enable_dpi_awareness", "system_scale", "describe"]
