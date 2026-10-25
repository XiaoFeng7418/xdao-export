# -*- coding: utf-8 -*-
"""仓库自带小工具（:mod:`tools`）的离线用例。

只测纯函数与「用法不对时说人话」，不碰网络、不碰 git 仓库状态。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tools import cov_funcs, fix_tag_object, push_one_commit


def test_parse_ident_turns_a_git_ident_into_the_github_shape() -> None:
    out = push_one_commit.parse_ident("XiaoFeng7418 <dev@example.com> 1792932594 +0800")

    assert out["name"] == "XiaoFeng7418"
    assert out["email"] == "dev@example.com"
    expected = datetime.fromtimestamp(1792932594, tz=timezone(timedelta(hours=8))).isoformat()
    assert out["date"] == expected


def test_parse_ident_handles_a_negative_offset() -> None:
    out = push_one_commit.parse_ident("Someone <a@b> 1700000000 -0500")

    assert out["date"].endswith("-05:00")


def test_functions_of_lists_nested_functions_in_line_order(tmp_path: Path) -> None:
    source = tmp_path / "sample.py"
    source.write_text(
        "\n".join(
            [
                "def outer():",
                "    def inner():",
                "        return 1",
                "",
                "    return inner()",
                "",
                "",
                "async def later():",
                "    return 2",
                "",
            ]
        ),
        encoding="utf-8",
    )

    found = cov_funcs.functions_of(source)

    assert [name for _start, _end, name in found] == ["outer", "inner", "later"]


def test_enclosing_picks_the_innermost_function() -> None:
    fns = [(1, 10, "outer"), (2, 4, "inner")]

    assert cov_funcs.enclosing(fns, 3) == "inner"
    assert cov_funcs.enclosing(fns, 7) == "outer"
    assert cov_funcs.enclosing(fns, 99) == "<模块级>"


def test_fix_tag_object_refuses_to_run_without_a_tag_name(capsys: pytest.CaptureFixture[str]) -> None:
    try:
        fix_tag_object.main([])
    except SystemExit:
        pass

    assert "用法" in capsys.readouterr().out


def test_cov_audit_refuses_to_run_without_arguments(capsys: pytest.CaptureFixture[str]) -> None:
    from tools import cov_audit

    with pytest.raises((SystemExit, IndexError)):
        cov_audit.main(["cov_audit.py"])


def test_push_one_commit_only_needs_a_clean_worktree() -> None:
    """这个工具没有任何参数：它只推「本地 HEAD 一个提交」。"""
    import inspect

    signature = inspect.signature(push_one_commit.main)

    assert list(signature.parameters) == []
