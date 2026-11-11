"""人话版时长：进度条上的「约剩 3 分 20 秒」。

边界就三处：不足一分钟、跨小时、以及坏输入（负数 / None / 字符串）——
它们都会出现在真实调用点里（刚开始导出的前几秒、配置里手改出来的怪值）。
"""

from __future__ import annotations

import pytest

from xdao.human import duration


@pytest.mark.parametrize(
    "seconds,expected",
    (
        (0, "0 秒"),
        (0.4, "0 秒"),
        (1, "1 秒"),
        (35, "35 秒"),
        (59, "59 秒"),
        (60, "1 分"),
        (61, "1 分 1 秒"),
        (200, "3 分 20 秒"),
        (3600, "1 小时"),
        (3660, "1 小时 1 分"),
        (7500, "2 小时 5 分"),
    ),
)
def test_duration_formats(seconds: float, expected: str) -> None:
    assert duration(seconds) == expected


@pytest.mark.parametrize("bad", (None, "", "abc", -5, -0.2, [], {}))
def test_duration_never_raises_on_junk(bad: object) -> None:
    # 估时是「锦上添花」：算不出来就当 0，绝不能因为一个坏值把导出弄崩。
    assert duration(bad) == "0 秒"
