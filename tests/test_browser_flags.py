"""``xdao/browser_flags.py`` 的测试（全部离线）。

打包版（PyInstaller 冻结进程）直接启动 Chrome/Edge 会被系统中断（退出码
``2147483651`` = ``STATUS_BREAKPOINT``），唯一能绕过的开关是 ``--no-sandbox``；
源码运行不带它。这个模块就是「谁该带开关」的唯一落点，两条启动路径都问它。

这里只钉三件事：开关常量本身、``is_frozen`` 的判据、``launch_flags`` 的返回值。
"""

from __future__ import annotations

import sys

import pytest

from xdao import browser_flags as bf


def test_the_only_frozen_flag_is_no_sandbox() -> None:
    """2026-10-01 在冻结环境里逐一排除过，只有这个开关管用（排除表见模块 docstring）。"""
    assert bf.FROZEN_EXTRA_FLAGS == ("--no-sandbox",)


def test_launch_flags_are_empty_for_source_runs() -> None:
    assert bf.launch_flags(frozen=False) == []


def test_launch_flags_carry_no_sandbox_when_frozen() -> None:
    assert bf.launch_flags(frozen=True) == ["--no-sandbox"]


def test_launch_flags_return_a_fresh_list(monkeypatch: pytest.MonkeyPatch) -> None:
    """返回值是可改的列表，不能是模块常量本身，否则调用方一改就污染全局。"""
    first = bf.launch_flags(frozen=True)
    first.append("--whatever")
    assert bf.launch_flags(frozen=True) == ["--no-sandbox"]
    assert bf.FROZEN_EXTRA_FLAGS == ("--no-sandbox",)


def test_is_frozen_follows_sys_frozen(monkeypatch: pytest.MonkeyPatch) -> None:
    """只看 ``sys.frozen``（PyInstaller 引导程序会设），不看解释器的名字。"""
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert bf.is_frozen() is True
    monkeypatch.delattr(sys, "frozen", raising=False)
    assert bf.is_frozen() is False


def test_launch_flags_default_to_the_current_process(monkeypatch: pytest.MonkeyPatch) -> None:
    """不传 ``frozen`` 时现算：冻结进程给开关，源码进程不给。"""
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert bf.launch_flags() == ["--no-sandbox"]
    monkeypatch.delattr(sys, "frozen", raising=False)
    assert bf.launch_flags() == []
    # 测试进程本身当然不是冻结的，顺带把默认判据钉住。
    assert bf.launch_flags() == []
    assert bf.is_frozen() is False


def test_is_frozen_ignores_a_frozen_attribute_that_is_falsy(monkeypatch: pytest.MonkeyPatch) -> None:
    """``sys.frozen`` 被设成假值时不算冻结（别用 ``hasattr`` 判）。"""
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    assert bf.is_frozen() is False
    assert bf.launch_flags() == []
