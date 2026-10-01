"""``tools/repo_check.py`` 的体检逻辑要有用例兜着。

为什么要有这份检查：``tools/repo_check.py`` 是维护这个仓库时最先跑的「体检」，
发布流程第 7 步、每周例行都靠它给结论。它自己出问题的样子又特别难看 ——
**读不到东西时照样印「一切正常」**：

- 读不到 ``xdao.__version__`` 时，旧代码会顺手补一句「与最新 Release … 一致」，
  同一件事既报错又说没问题；
- ``git ls-tree`` 读空时，旧代码会说「本地与远端逐文件一致（0 个文件）」；
- 它一直只比「本地 HEAD 的树 ↔ 远端分支的树」，工作区里按着的改动根本不进结论。

这三条都不会让别的用例变红，只会在某天让人以为仓库是干净的、同步的。
所以这里既验「有东西时会报出来」，也验「读不到的时候不许说没问题」。

做法是给 ``main()`` 喂一份假 API：``gh_token`` / ``request_json`` / ``local_commits``
/ ``local_tree`` / ``worktree_changes`` 都换成测试自己的，只有被测的判断逻辑是真的。
"""

from __future__ import annotations

import os
import subprocess
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import repo_check  # noqa: E402
import xdao  # noqa: E402
from repo_info import expected_description, expected_topics  # noqa: E402

REPO = "owner/name"
REF_SHA = "a" * 40

#: 临时仓库里跑 git 用的环境：给上提交者身份，并把全局/系统配置指到空文件，
#: 免得继承本机（或 CI）的 user.name / core.autocrlf 之类设置。
GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "tester",
    "GIT_AUTHOR_EMAIL": "tester@example.com",
    "GIT_COMMITTER_NAME": "tester",
    "GIT_COMMITTER_EMAIL": "tester@example.com",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
}


def _release(tag: str | None = None) -> dict:
    tag = tag or f"v{xdao.__version__}"
    return {
        "tag_name": tag,
        "body": "免安装包在最下面。",
        "assets": [{"name": f"xdao-export-{tag}-win64.zip"}],
    }


def _routes(releases: list[dict] | None = None) -> dict[str, object]:
    """假 API：按路径前缀找应答。更具体的路径写在前面，``/repos/owner/name`` 放最后。"""
    return {
        f"/repos/{REPO}/git/ref/heads/": {"object": {"sha": REF_SHA}},
        f"/repos/{REPO}/git/trees/": {
            "tree": [{"path": "a.py", "sha": "1", "type": "blob"}],
            "truncated": False,
        },
        f"/repos/{REPO}/releases": releases if releases is not None else [_release()],
        f"/repos/{REPO}/issues": [],
        f"/repos/{REPO}": {
            "full_name": REPO,
            "visibility": "public",
            "default_branch": "master",
            "description": expected_description(),
            "topics": list(expected_topics()),
            "license": {"spdx_id": "MIT"},
        },
    }


def _run_main(monkeypatch, capsys, routes=None, **patches) -> tuple[int, str]:
    """跑一次 ``main()``，网络与仓库读取都换成假的；返回 (退出码, 输出)。"""

    def fake_request(method, path, *args, **kwargs):
        for prefix, answer in (routes or _routes()).items():
            if path.startswith(prefix):
                if isinstance(answer, Exception):
                    raise answer
                return answer
        raise AssertionError(f"用例没准备这个请求：{path}")

    monkeypatch.setattr(repo_check, "gh_token", lambda: "fake-token")
    monkeypatch.setattr(repo_check, "request_json", fake_request)
    monkeypatch.setattr(repo_check, "local_commits", lambda: [{"sha": REF_SHA, "message": "init"}])
    monkeypatch.setattr(repo_check, "local_tree", lambda _dir: {"a.py": "1"})
    monkeypatch.setattr(repo_check, "worktree_changes", lambda _dir: [])
    for name, value in patches.items():
        monkeypatch.setattr(repo_check, name, value)

    code = repo_check.main(["--repo", REPO])
    return code, capsys.readouterr().out


def test_the_report_only_counts_warnings_and_errors_as_problems() -> None:
    """✓ 不算问题；! 与 ✗ 才算 —— 结论行与退出码都看这个。"""
    report = repo_check.Report()
    report.add(repo_check.OK, "设置", "描述没问题")
    assert report.problems == []

    report.add(repo_check.WARN, "工作区", "有没提交的改动")
    assert len(report.problems) == 1

    report.add(repo_check.BAD, "文件", "读不到本地树")
    assert len(report.problems) == 2


def test_worktree_changes_reports_edits_and_untracked_files(tmp_path: Path) -> None:
    """工作区改动：改过的文件、新加没跟踪的文件都要列出来；干净时为空。"""
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, env=GIT_ENV)

    git("init", "-q")
    (repo / "tracked.txt").write_text("one\n", encoding="utf-8")
    git("add", "tracked.txt")
    git("commit", "-q", "-m", "init")
    assert repo_check.worktree_changes(repo) == []

    (repo / "tracked.txt").write_text("two\n", encoding="utf-8")
    (repo / "untracked.txt").write_text("new\n", encoding="utf-8")
    assert sorted(repo_check.worktree_changes(repo)) == ["tracked.txt", "untracked.txt"]


def test_a_clean_run_says_everything_is_fine(monkeypatch, capsys) -> None:
    code, out = _run_main(monkeypatch, capsys)
    assert code == 0
    assert "结论：一切正常" in out
    assert "干净，没有没提交的改动" in out
    assert f"与最新 Release v{xdao.__version__} 一致" in out


def test_a_missing_version_is_not_reported_as_consistent(monkeypatch, capsys) -> None:
    """读不到版本号时：只能报错，不许同时说「与最新 Release 一致」。"""
    monkeypatch.setitem(sys.modules, "xdao", types.ModuleType("xdao"))
    code, out = _run_main(monkeypatch, capsys)
    assert code == 1
    assert "读不到 xdao.__version__" in out
    assert "与最新 Release" not in out


def test_an_empty_local_tree_is_not_reported_as_consistent(monkeypatch, capsys) -> None:
    """本地树读空 == 没查成，不许印「逐文件一致」。"""
    code, out = _run_main(monkeypatch, capsys, local_tree=lambda _dir: {})
    assert code == 1
    assert "本地树是空的" in out
    assert "逐文件一致" not in out


def test_uncommitted_changes_show_up_in_the_conclusion(monkeypatch, capsys) -> None:
    """按着没提交的改动时要说出来（但只是警告，退出码仍是 0）。"""
    code, out = _run_main(monkeypatch, capsys, worktree_changes=lambda _dir: ["a.py"])
    assert code == 0
    assert "1 个没提交的改动" in out
    assert "1 项需要处理" in out


def test_an_unknown_path_in_the_fake_api_is_an_error(monkeypatch, capsys) -> None:
    """用例自己的底线：假 API 没准备的请求要炸，别静悄悄地走默认分支。"""
    with pytest.raises(AssertionError):
        _run_main(monkeypatch, capsys, routes={f"/repos/{REPO}/releases": [_release()]})
