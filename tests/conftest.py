"""测试公共夹具。

本机运行在受限文件沙箱下：只有「命令启动时已存在」的目录才被授予写入权，
pytest 自己新建的 ``tmp_path`` 会在写入时报「拒绝访问」。
因此测试统一用 :func:`artifacts_dir`，把产物写进工作区内已提交的
``.test-artifacts``，并在用例结束后清理自己那部分。
"""

from __future__ import annotations

import shutil
import uuid
from pathlib import Path

import pytest

ARTIFACTS_ROOT = Path(__file__).resolve().parent.parent / ".test-artifacts"

# 「可以跳过的理由」白名单。除此之外任何跳过都算这条测试没跑到 ——
# 起因是实测过这么一次：全套报 `473 passed, 12 skipped, 0 failed`，看着全绿，
# 其实是 `tk.Tk()` 偶发抛 TclError，模块级夹具一挂就带走一整组界面用例
# （`test_theme.py` / `test_window.py` / `test_gui_browser_login.py`）。
# 最坏情况量到过 45 条无声消失，而报告里一个 failed 都没有。
# 所以这里盯的是「跳过的是不是只有那两条显式开关的真机用例」。
#
# 两条使用约定：
# 1) **跳过必须写 reason=**。白名单是按理由**文本**匹配的，`skipif(sys.platform != "win32")`
#    不写 reason 时理由是空串 → 会被判成「意外跳过」，CI 的 Linux 矩阵会红得莫名其妙。
# 2) 白名单内的整组跳过（无头机器）不算失败，但**一定会打印一行提醒** ——
#    实测过 Tk 在套件中途亚秒级初始化失败（`Can't find a usable tk.tcl …`），
#    每次刚好带走 `test_theme.py` 那 10 条：只跳 3~5 条时最容易被当成「全跑过了」，
#    所以除了「恰好就是那两条真机用例」以外，一律要说一声。
_SKIP_REASONS_ALLOWED = (
    "XDAO_BROWSER_TEST",
    "XDAO_LIVE_NOTIFY",
    "没有可用的显示环境",  # 无头机器上界面用例只能跳过（CI 的 Linux 矩阵就是这样）
    "只有 Windows",
    "只有 macOS",
    "需要 Windows",
)
_SKIP_SAFETY_LIMIT = 5

# 「恰好就是这两条真机用例」＝什么都不用说。其余任何跳过都要留下一行记录：
# 少跑 1~5 条时最容易被当成全跑过了（实测 Tk 中途抖动一次带走 test_theme 那 10 条，
# 也见过只带走个别函数级夹具的情况）。
_EXPECTED_SKIPS = ("browser_login", "live_notify")

# 本次运行里跳过的用例（nodeid, 理由），由下面的 hook 收集。
_skips: list[tuple[str, str]] = []


def pytest_report_teststatus(report, config):
    """收集跳过项。写成 hook 是因为它比 `-rs` 的文本解析稳。"""
    if report.when == "setup" and report.skipped:
        reason = ""
        if isinstance(report.longrepr, tuple) and len(report.longrepr) == 3:
            reason = str(report.longrepr[2])
        _skips.append((report.nodeid, reason))
    return None


def pytest_sessionfinish(session, exitstatus):  # noqa: ARG001 —— pytest 的签名就是这样
    """收尾时核对：跳过清单只允许出现白名单里的理由。

    允许无头机器把界面用例整组跳过，但**不许**悄悄多跳 —— 这条检查不会让本来
    能过的运行失败，只会在「有些用例其实没跑」时把话说出来。
    """
    if not _skips:
        return
    unexpected = [
        (node, reason)
        for node, reason in _skips
        if not any(token.lower() in reason.lower() for token in _SKIP_REASONS_ALLOWED)
    ]
    if unexpected:
        lines = "\n".join(f"  - {node}：{reason}" for node, reason in unexpected)
        raise pytest.UsageError(
            "有测试被意外跳过——这类跳过会让「0 failed」名不副实，请先查清原因：\n" + lines
        )
    if len(_skips) <= _SKIP_SAFETY_LIMIT and all(
        any(token in node for token in _EXPECTED_SKIPS) for node, _ in _skips
    ):
        return  # 就是那两条真机用例，本来就不该跑
    # 其余情况（无头机器整组跳过、或者只跳了少数几条）一律说一声：别当成「全跑过了」。
    print(f"\n[conftest] 本次跳过了 {len(_skips)} 条（界面/真机用例），未计入通过数。")


@pytest.fixture
def artifacts_dir() -> Path:
    """给单个用例一个干净的产物目录（用例结束后删除）。

    与 ``tmp_path`` 用法一致：``def test_x(artifacts_dir):`` 后直接当目录用。
    """
    path = ARTIFACTS_ROOT / f"case-{uuid.uuid4().hex[:12]}"
    path.mkdir(parents=True, exist_ok=True)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)
