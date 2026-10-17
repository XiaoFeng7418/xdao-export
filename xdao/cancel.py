"""导出中途取消用的停止位与异常（v0.17.0）。

为什么单独开一个模块：抓取层（``cache.py``）、三个导出器（``exporters/*``）和
界面（``gui.py``）都要认这件事，而这些模块之间不宜互相 import。这里只放一个
``threading.Event`` 上的薄封装，谁都能引。

约定很朴素：调用方在开始一趟导出时造一个 ``threading.Event`` 交给各层；
用户点「取消」就 ``set()``；各层在**能停下来的地方**（每抓一页、每下一张图、
开始渲染前）调一次 :func:`check`，见到停止位就抛 :class:`ExportCancelled`。
异常一路往上抛，最后由界面统一收尾 —— 不在中途各处各写一套收尾逻辑。
"""

from __future__ import annotations

import threading


class ExportCancelled(Exception):
    """用户取消了这一趟导出。"""


def check(stop: threading.Event | None) -> None:
    """停止位举起来了就抛 :class:`ExportCancelled`；没传停止位等于永不取消。"""
    if stop is not None and stop.is_set():
        raise ExportCancelled("导出已取消")
