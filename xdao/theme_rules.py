"""配置与界面共用的主题规则，不依赖 tkinter 或窗口状态。"""

from __future__ import annotations


PALETTE_NAMES = frozenset({"light", "dark"})

FONT_SCALE_CHOICES: tuple[tuple[str, float], ...] = (
    ("小", 0.85),
    ("标准", 1.0),
    ("大", 1.15),
    ("更大", 1.3),
    ("特大", 1.5),
)
FONT_SCALE_MIN = 0.7
FONT_SCALE_MAX = 2.0


def normalize_font_scale(value: object, fallback: float = 1.0) -> float:
    """把读来的比例收进 ``[FONT_SCALE_MIN, FONT_SCALE_MAX]``。

    认不出来（``None``／空串／非数字／``NaN``）时给 ``fallback`` —— 配置是纯
    文本、用户会手改，坏值只该回落，不该让程序起不来。
    """
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return fallback
    if number != number:  # NaN
        return fallback
    return min(max(number, FONT_SCALE_MIN), FONT_SCALE_MAX)
