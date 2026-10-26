"""语法层面的老版本兼容守卫：源码必须能被 Python 3.10 编译。

为什么需要这一条：CI 在 ubuntu-latest / py3.10 上会跑一次「全部源码必须能编译」。
2026-10-26 的 v0.21.0 就在这里翻车过一次 —— 合并导出里写了
``f'{"\\n".join(rows)}'``，而「f-string 表达式里带反斜杠」是 Python 3.12（PEP 701）
才允许的写法。3.10 用户装不上源码版，Windows 打包版又看不出来，所以拿用例兜住。

这里不追求完整语法检查（那要靠真解释器），只钉住最容易悄悄溜进来的那一类。
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SKIP_DIRS = {".git", "dist", "build", ".test-artifacts", "__pycache__"}


def _sources() -> list[Path]:
    files: list[Path] = []
    for path in ROOT.rglob("*.py"):
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        files.append(path)
    return sorted(files)


def test_no_fstring_expression_uses_a_backslash() -> None:
    """f-string 的表达式部分不许出现反斜杠（3.12 之前的语法限制）。"""
    offenders: list[str] = []
    for path in _sources():
        source = path.read_text(encoding="utf-8-sig")
        tree = ast.parse(source, filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.FormattedValue):
                continue
            segment = ast.get_source_segment(source, node.value) or ""
            if "\\" in segment:
                rel = path.relative_to(ROOT)
                offenders.append(f"{rel}:{node.lineno}: {segment.strip()[:60]}")

    assert not offenders, "f-string 表达式里有反斜杠，3.10 编译不过：\n" + "\n".join(offenders)


def test_no_fstring_expression_spans_multiple_lines() -> None:
    """同理，3.12 之前 f-string 的表达式也不能跨多行写。"""
    offenders: list[str] = []
    for path in _sources():
        source = path.read_text(encoding="utf-8-sig")
        tree = ast.parse(source, filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.FormattedValue):
                continue
            segment = ast.get_source_segment(source, node.value) or ""
            if "\n" in segment.strip():
                rel = path.relative_to(ROOT)
                offenders.append(f"{rel}:{node.lineno}: {segment.strip().splitlines()[0][:60]}")

    assert not offenders, "f-string 表达式跨行了，3.10 编译不过：\n" + "\n".join(offenders)


@pytest.mark.parametrize(
    "snippet",
    [
        'value = f\'{"a" + "b"}\'',
        'value = f"{1 + 2}"',
    ],
)
def test_the_guard_accepts_normal_fstrings(snippet: str) -> None:
    """普通 f-string 当然要放行 —— 不然这条守卫会把正常代码也判成红。"""
    ast.parse(snippet)
