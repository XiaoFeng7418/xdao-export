"""人话版的时长格式（界面进度、命令行剩余时间都用它）。

单独拎出来是因为两处都要用，而 ``xdao/gui.py`` 里面有 Tk 的导入，命令行那边
不能顺着它把 tkinter 拖进来。
"""

from __future__ import annotations


def duration(seconds: float) -> str:
    """把秒数写成「3 分 20 秒」这样的说法；不足一秒按一秒算，负数按 0 算。"""
    try:
        total = int(round(float(seconds)))
    except (TypeError, ValueError):
        return "0 秒"
    total = max(0, total)
    if total < 60:
        return f"{total} 秒"
    minutes, rest = divmod(total, 60)
    if minutes < 60:
        return f"{minutes} 分 {rest} 秒" if rest else f"{minutes} 分"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} 小时 {minutes} 分" if minutes else f"{hours} 小时"


__all__ = ["duration"]
