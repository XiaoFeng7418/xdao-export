"""高分屏（125% / 150%）相关的测试。

这里能测的是「算得对不对」，测不到「真的清晰了」——后者要真机上看。所以用例分两块：

* :mod:`xdao.dpi` 的比例归一化、环境变量覆盖、读取失败时的回落；
* :mod:`xdao.theme` 把比例折进间距 / 左栏宽度 / 窗口尺寸（``scaled`` 不折，
  因为声明 DPI 感知之后 Windows 会按真实 DPI 换算字号，再乘一次就重了）。
"""

from __future__ import annotations

import pytest

from xdao import dpi, theme


@pytest.fixture
def restore_scales():
    """用例改完把两个比例还原，别影响同进程里后面的用例。"""
    original_font = theme.FONT_SCALE
    original_dpi = theme.DPI_SCALE
    yield
    theme.FONT_SCALE = original_font
    theme.DPI_SCALE = original_dpi


def test_normalize_dpi_scale_clamps_and_falls_back():
    assert theme.normalize_dpi_scale(1.25) == 1.25
    assert theme.normalize_dpi_scale("1.5") == 1.5
    assert theme.normalize_dpi_scale(0.5) == 1.0  # 比 100% 还小的缩放不存在
    assert theme.normalize_dpi_scale(9.0) == 3.0
    assert theme.normalize_dpi_scale(None) == 1.0
    assert theme.normalize_dpi_scale("abc") == 1.0
    assert theme.normalize_dpi_scale(float("nan")) == 1.0


def test_set_and_read_dpi_scale(restore_scales):
    assert theme.set_dpi_scale(1.5) == 1.5
    assert theme.dpi_scale() == 1.5
    assert theme.set_dpi_scale("2") == 2.0
    assert theme.dpi_scale() == 2.0


def test_gap_and_column_width_follow_the_dpi_scale(restore_scales):
    base_gap = theme.gap(2)
    base_width = theme.settings_column_width()
    theme.set_dpi_scale(2.0)
    assert theme.gap(2) == base_gap * 2
    assert theme.settings_column_width() == base_width * 2


def test_font_scale_and_dpi_scale_multiply_together(restore_scales):
    theme.set_font_scale(1.5)
    theme.set_dpi_scale(1.25)
    width = theme.settings_column_width()
    assert width == int(round(theme.SETTINGS_COLUMN_WIDTH * 1.5 * 1.25))


def test_fonts_are_not_scaled_by_the_dpi_scale(restore_scales):
    """字号只跟界面字号比例走：DPI 感知之后 Windows 自己会把点换算成像素。"""
    theme.set_font_scale(1.0)
    plain = theme.font()
    theme.set_dpi_scale(2.0)
    assert theme.font() == plain


def test_system_scale_reads_the_environment_override(monkeypatch):
    monkeypatch.setenv(dpi.SCALE_ENV, "1.5")
    assert dpi.system_scale() == 1.5


def test_system_scale_ignores_a_bad_override(monkeypatch):
    monkeypatch.setenv(dpi.SCALE_ENV, "abc")
    assert dpi.system_scale() == 1.0
    monkeypatch.setenv(dpi.SCALE_ENV, "0.5")
    assert dpi.system_scale() == 1.0


def test_system_scale_without_override_is_never_below_one(monkeypatch):
    monkeypatch.delenv(dpi.SCALE_ENV, raising=False)
    assert 1.0 <= dpi.system_scale() <= 3.0


def test_dpi_awareness_can_be_skipped_by_the_environment(monkeypatch):
    monkeypatch.setenv(dpi.SKIP_ENV, "1")
    assert dpi.enable_dpi_awareness() is False


def test_describe_writes_a_percentage():
    assert dpi.describe(1.0) == "100%"
    assert dpi.describe(1.25) == "125%"
    assert dpi.describe(None) == "100%"
