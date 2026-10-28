# -*- coding: utf-8 -*-
"""仓库自带小工具（:mod:`tools`）的离线用例。

只测纯函数与「用法不对时说人话」，不碰网络、不碰 git 仓库状态。
"""

from __future__ import annotations

import ast
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tools import cov_funcs, fix_tag_object, patch_release_body, push_one_commit, push_tag_via_api


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


def test_push_tag_via_api_parses_a_git_ident() -> None:
    out = push_tag_via_api.parse_ident("XiaoFeng7418 <dev@example.com> 1792932594 +0800")

    assert out["name"] == "XiaoFeng7418"
    assert out["email"] == "dev@example.com"
    assert out["date"].endswith("+08:00")


def test_push_tag_via_api_splits_a_tag_object() -> None:
    raw = "\n".join(
        [
            "object 0123456789abcdef0123456789abcdef01234567",
            "type commit",
            "tag v9.9.9",
            "tagger Someone <a@b> 1700000000 +0000",
            "",
            "标题行",
            "",
        ]
    )

    target, tagger, message = push_tag_via_api.split_tag(raw)

    assert target == "0123456789abcdef0123456789abcdef01234567"
    assert tagger["name"] == "Someone"
    assert message == "标题行"  # 结尾换行要去掉：GitHub 建对象时就是这样


def test_push_tag_via_api_rejects_a_broken_tag_header() -> None:
    with pytest.raises(ValueError):
        push_tag_via_api.split_tag("type commit\n\n标题")


def test_push_tag_via_api_refuses_to_run_without_a_tag_name() -> None:
    with pytest.raises(SystemExit) as caught:
        push_tag_via_api.main([])

    assert "用法" in str(caught.value)


def test_patch_release_body_reads_utf8_notes(tmp_path: Path) -> None:
    notes = tmp_path / "notes.md"
    notes.write_text("# v9.9.9\n\n这是免安装包。\n", encoding="utf-8")

    assert patch_release_body.read_notes(notes).startswith("# v9.9.9")


def test_patch_release_body_reports_a_missing_file(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        patch_release_body.read_notes(tmp_path / "没有这个文件.md")


# ---------------------------------------------------------------------------
# 静态检查：同一文件里别把名字定义两遍（v0.21.19）
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parents[1]
#: 这些目录不是源码：``.test-artifacts`` 是用例自己写的临时文件，别去扫。
_SKIP_PARTS = {"__pycache__", "build", "dist", ".git", ".test-artifacts", "_scratch"}


def _repo_python_files() -> list[Path]:
    """仓库里真正属于源码的 ``.py``（跳过隐藏目录、构建产物与临时资料）。"""
    found: list[Path] = []
    for path in sorted(_REPO_ROOT.rglob("*.py")):
        relative = path.relative_to(_REPO_ROOT)
        if any(part in _SKIP_PARTS or part.startswith(".") for part in relative.parts):
            continue
        found.append(path)
    return found


def _top_level_bindings(path: Path) -> list[tuple[str, int]]:
    """模块顶层绑定了哪些名字、在哪一行。

    只认「定义」与「导入」两类：函数、类、``名字 = 值``、``名字: 类型 = 值``、
    ``import`` / ``from ... import ...``。块里（``if`` / ``try`` / ``with``）的不算：
    那些是有意按条件覆盖的写法。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    bindings: list[tuple[str, int]] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bindings.append((node.name, node.lineno))
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    bindings.append((target.id, node.lineno))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value is not None:
            bindings.append((node.target.id, node.lineno))
        elif isinstance(node, ast.Import):
            for alias in node.names:
                # ``import urllib.parse`` 绑的是 ``urllib``，但 ``urllib.request``
                # 再导一次不该算重名，所以按完整点号记。
                bindings.append((alias.asname or alias.name, node.lineno))
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name != "*":
                    bindings.append((alias.asname or alias.name, node.lineno))
    return bindings


def test_no_module_binds_the_same_top_level_name_twice() -> None:
    """同一个文件里别把同一个名字定义两遍：后一份会静默盖掉前一份。

    盖掉的后果是**没有任何工具会报**：编译通过、导入通过、跑用例也通过，只是前一份
    从来没被用过。v0.21.18 就这样丢过一条用例（``test_window.py`` 里同名用例写了两遍，
    前一份从没跑过），v0.21.19 又发现 ``test_client.py`` 的 ``_FakeResponse``、
    ``test_window.py`` 的 ``_drain`` 各写了两份，``browser_login.py`` 里那份本地
    ``Frame`` 被模块末尾的兼容出口盖住。所以这里拿标准库 ``ast`` 把仓库扫一遍。
    """
    duplicates: list[str] = []
    for path in _repo_python_files():
        seen: dict[str, int] = {}
        for name, line in _top_level_bindings(path):
            if name in seen:
                relative = path.relative_to(_REPO_ROOT).as_posix()
                duplicates.append(f"{relative}: 「{name}」在第 {seen[name]} 行与第 {line} 行各绑一次")
            else:
                seen[name] = line

    assert duplicates == []


def test_sync_from_api_parses_the_response_as_json(monkeypatch: pytest.MonkeyPatch) -> None:
    """``api()`` 得真的把回包解析成字典。

    v0.21.19 之前这份脚本忘了 ``import json``：``api()`` 一调就 ``NameError``，而所有
    用例都把 ``sync_from_api.api`` 整个顶掉，所以从来没人碰到过那一行 —— 与 v0.21.15
    的 ``derive_filename`` 是同一类「没人调过的代码里藏着 NameError」。
    """
    from tools import sync_from_api

    calls: list[tuple[str, str]] = []

    def fake_request_bytes(method: str, path: str, **kwargs: object) -> bytes:
        calls.append((method, path))
        return b'{"sha": "abc123", "commit": {"message": "title"}}'

    monkeypatch.setattr(sync_from_api, "request_bytes", fake_request_bytes)

    assert sync_from_api.api("t", "/repos/o/r/commits/master") == {
        "sha": "abc123",
        "commit": {"message": "title"},
    }
    assert calls == [("GET", "/repos/o/r/commits/master")]
