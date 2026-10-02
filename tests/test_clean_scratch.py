"""``tools/clean_scratch.py`` 的护栏要有用例兜着。

这个工具是**删东西**的，而且删的是本机临时目录里那些「权限还坏着」的残留 ——
它出问题的样子全都很安静：

- 它以前对传进来的路径**没有任何挑拣**：盘符根目录、用户主目录、仓库目录、
  乃至当前工作目录本身，点谁删谁；icacls 那一步还会 `/t` 递归授一遍权限；
- 传进来的是符号链接或 Windows 目录联接（junction）时，``shutil.rmtree`` 会直接报
  「Cannot call rmtree on a symbolic link」，它却把这当成「删除被拒」，接着调权限、
  最后误报一句「请检查该目录是否被其它程序占用」—— 而真正管用的 ``os.rmdir(junction)``
  从来没试过（本机 2026-10-02 验过：``os.rmdir`` 只摘链接，目标目录里一个文件都不动）；
- 传进来的是文件时同样走目录那条路，删不掉也报「被其它程序占用」；
- 目录读不动时 ``rglob`` 直接抛出去，整个工具崩在半路，后面的目录一个都不处理；
- ``free_up`` 把两句 icacls 用 ``cmd /c … & …`` 串起来，只看最后一句的返回码，
  第一句（摘「拒绝」项）失败会被悄悄吞掉。

所以这里既验「该删的真删掉了」，也验「不该删的一个字节都没动」。
链接用例是真造链接：Windows 上用 ``mklink /J``（普通用户就能建 junction），
POSIX 上用 ``os.symlink`` —— 两个平台都真跑，不跳过。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import clean_scratch as cs  # noqa: E402


# ------------------------------------------------------------------ helpers


def _make_link(target: Path, link: Path, *, is_dir: bool = True) -> None:
    """造一个链接：Windows 上 junction，POSIX 上 symlink。

    造不出来就失败，不 skip —— 本仓的「跳过」要报备（``tests/conftest.py``），
    而这两条路（``mklink /J``、``os.symlink``）在本机与两个 CI 平台上都走得通。
    """
    if os.name == "nt":
        assert is_dir, "Windows 上普通用户建不了文件符号链接，这里只验目录联接"
        done = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True,
            text=True,
            encoding="gbk",
            errors="replace",
        )
        assert done.returncode == 0, f"mklink /J 失败：{done.stdout} {done.stderr}"
    else:
        os.symlink(target, link, target_is_directory=is_dir)


class _Result:
    """够用的 ``subprocess.CompletedProcess``：free_up 只看 returncode 与两路输出。"""

    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _fake_icacls(*, codes: tuple[int, ...] = (0, 0), stdout: str = "", stderr: str = "",
                 oserror: bool = False):
    """换掉 ``cs.subprocess.run``：记下每次调用的 argv，按 codes 依序给返回码。"""
    calls: list[list[str]] = []

    def run(argv, **_kwargs):  # noqa: ANN001
        calls.append(list(argv))
        if oserror:
            raise FileNotFoundError("icacls 不在 PATH 上")
        index = len(calls) - 1
        code = codes[index] if index < len(codes) else codes[-1]
        return _Result(code, stdout, stderr)

    return run, calls


def _fake_rmtree(results: list[bool]):
    """换掉 ``cs.try_rmtree``：按脚本给结果，最后一条一直重复。"""
    seen: list[Path] = []

    def fake(path: Path) -> bool:
        seen.append(path)
        index = len(seen) - 1
        return results[index] if index < len(results) else results[-1]

    return fake, seen


# --------------------------------------------------------------- refuse_reason


def test_the_filesystem_root_is_refused() -> None:
    root = Path(Path.cwd().anchor)
    reason = cs.refuse_reason(root)
    assert reason is not None
    assert "根目录" in reason


def test_the_home_directory_is_refused() -> None:
    reason = cs.refuse_reason(Path.home())
    assert reason is not None
    assert "主目录" in reason


def test_a_git_working_tree_is_refused(tmp_path: Path) -> None:
    repo = tmp_path / "somewhere"
    (repo / ".git").mkdir(parents=True)
    reason = cs.refuse_reason(repo)
    assert reason is not None
    assert "git 仓库" in reason


def test_the_current_working_directory_is_refused(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    reason = cs.refuse_reason(tmp_path)
    assert reason is not None
    assert "当前工作目录就是它" in reason


def test_an_ancestor_of_the_working_directory_is_refused(tmp_path: Path, monkeypatch) -> None:
    outer = tmp_path / "outer"
    inner = outer / "inner"
    inner.mkdir(parents=True)
    monkeypatch.chdir(inner)
    reason = cs.refuse_reason(outer)
    assert reason is not None
    assert "在它里面" in reason


def test_a_plain_residue_directory_is_allowed(tmp_path: Path) -> None:
    residue = tmp_path / ".pytest_tmp"
    residue.mkdir()
    assert cs.refuse_reason(residue) is None


# ------------------------------------------------------------------ link_kind


def test_link_kind_is_none_for_a_plain_directory(tmp_path: Path) -> None:
    assert cs.link_kind(tmp_path) is None


def test_link_kind_is_none_for_a_file(tmp_path: Path) -> None:
    plain = tmp_path / "notes.txt"
    plain.write_text("x", encoding="utf-8")
    assert cs.link_kind(plain) is None


def test_link_kind_names_the_real_link(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    _make_link(target, link)
    kind = cs.link_kind(link)
    assert kind is not None
    assert ("junction" in kind) if os.name == "nt" else ("符号链接" in kind)


def test_a_link_is_deleted_without_touching_its_target(tmp_path: Path, capsys) -> None:
    target = tmp_path / "target"
    target.mkdir()
    (target / "keep.txt").write_text("重要", encoding="utf-8")
    link = tmp_path / "link"
    _make_link(target, link)

    assert cs.clean_one(str(link)) == 0

    out = capsys.readouterr().out
    assert "只摘掉链接本身" in out
    assert not link.exists()
    assert (target / "keep.txt").read_text(encoding="utf-8") == "重要"


def test_a_parent_holding_a_link_is_deleted_while_the_target_survives(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    (target / "keep.txt").write_text("重要", encoding="utf-8")
    parent = tmp_path / "parent"
    parent.mkdir()
    (parent / "junk.txt").write_text("junk", encoding="utf-8")
    _make_link(target, parent / "linked")

    assert cs.clean_one(str(parent)) == 0

    assert not parent.exists()
    assert (target / "keep.txt").read_text(encoding="utf-8") == "重要"


def test_dry_run_on_a_link_keeps_it(tmp_path: Path, capsys) -> None:
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    _make_link(target, link)

    assert cs.clean_one(str(link), dry_run=True) == 0

    assert "[演练] 会删掉这个" in capsys.readouterr().out
    assert link.exists() and target.exists()


# ---------------------------------------------------------------------- files


def test_a_plain_file_is_deleted_with_an_honest_message(tmp_path: Path, capsys) -> None:
    plain = tmp_path / "leftover.txt"
    plain.write_text("x", encoding="utf-8")

    assert cs.clean_one(str(plain)) == 0

    assert not plain.exists()
    assert "这是个文件，不是目录" in capsys.readouterr().out


def test_dry_run_on_a_file_keeps_it(tmp_path: Path, capsys) -> None:
    plain = tmp_path / "leftover.txt"
    plain.write_text("x", encoding="utf-8")

    assert cs.clean_one(str(plain), dry_run=True) == 0

    assert plain.exists()
    assert "会删掉这个文件" in capsys.readouterr().out


# ------------------------------------------------------------------ directory


def test_a_normal_directory_is_deleted_and_entries_are_counted(tmp_path: Path, capsys) -> None:
    residue = tmp_path / ".pytest-scratch"
    (residue / "sub").mkdir(parents=True)
    (residue / "a.txt").write_text("a", encoding="utf-8")
    (residue / "sub" / "b.txt").write_text("b", encoding="utf-8")

    assert cs.clean_one(str(residue)) == 0

    out = capsys.readouterr().out
    assert "3 个条目" in out
    assert "已删除" in out
    assert not residue.exists()


def test_a_missing_path_is_skipped_and_counts_as_done(tmp_path: Path, capsys) -> None:
    assert cs.clean_one(str(tmp_path / "nope")) == 0
    assert "跳过（不存在）" in capsys.readouterr().out


def test_dry_run_deletes_nothing(tmp_path: Path, capsys) -> None:
    residue = tmp_path / ".pytest_tmp"
    residue.mkdir()
    (residue / "a.txt").write_text("a", encoding="utf-8")

    assert cs.clean_one(str(residue), dry_run=True) == 0

    assert (residue / "a.txt").exists()
    assert "[演练] 会删掉" in capsys.readouterr().out


def test_a_guarded_target_is_left_alone(tmp_path: Path, capsys) -> None:
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / "important.txt").write_text("重要", encoding="utf-8")

    assert cs.clean_one(str(repo)) == 1

    captured = capsys.readouterr()
    assert "不删" in captured.err
    assert (repo / "important.txt").exists()


def test_the_guard_wins_even_in_dry_run(tmp_path: Path, capsys) -> None:
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)

    assert cs.clean_one(str(repo), dry_run=True) == 1

    assert "不删" in capsys.readouterr().err
    assert repo.exists()


def test_permission_fix_that_works_says_so(tmp_path: Path, monkeypatch, capsys) -> None:
    residue = tmp_path / "stuck"
    residue.mkdir()
    fake, seen = _fake_rmtree([False, True])
    monkeypatch.setattr(cs, "try_rmtree", fake)
    monkeypatch.setattr(cs, "free_up", lambda path: (True, ""))

    assert cs.clean_one(str(residue)) == 0

    out = capsys.readouterr().out
    assert "直接删除被拒，尝试调整自身权限…" in out
    assert "调整权限后已删除" in out
    assert len(seen) == 2


def test_permission_fix_that_fails_is_reported(tmp_path: Path, monkeypatch, capsys) -> None:
    residue = tmp_path / "stuck"
    residue.mkdir()
    monkeypatch.setattr(cs, "try_rmtree", lambda path: False)
    monkeypatch.setattr(
        cs, "free_up", lambda path: (False, "去掉「拒绝」项失败（icacls 返回 5）")
    )

    assert cs.clean_one(str(residue)) == 1

    captured = capsys.readouterr()
    assert "调整权限没成功：去掉「拒绝」项失败" in captured.err
    assert "仍无法删除" in captured.err


def test_an_unreadable_directory_does_not_crash_the_tool(tmp_path: Path, monkeypatch, capsys) -> None:
    residue = tmp_path / "dark"
    residue.mkdir()
    monkeypatch.setattr(Path, "rglob", lambda self, pattern: (_ for _ in ()).throw(PermissionError("拒绝访问")))
    monkeypatch.setattr(cs, "try_rmtree", lambda path: True)

    assert cs.clean_one(str(residue)) == 0

    captured = capsys.readouterr()
    assert "条目数读不出来：PermissionError" in captured.err
    assert "条目数读不出来" in captured.out


# ------------------------------------------------------------- count_entries


def test_count_entries_counts_nested_items(tmp_path: Path) -> None:
    (tmp_path / "sub").mkdir()
    (tmp_path / "a.txt").write_text("a", encoding="utf-8")
    (tmp_path / "sub" / "b.txt").write_text("b", encoding="utf-8")
    assert cs.count_entries(tmp_path) == 3


def test_count_entries_returns_none_when_it_cannot_read(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(Path, "rglob", lambda self, pattern: (_ for _ in ()).throw(OSError("坏了")))
    assert cs.count_entries(tmp_path) is None
    assert "条目数读不出来" in capsys.readouterr().err


# ---------------------------------------------------------------- try_rmtree


def test_try_rmtree_removes_a_real_tree(tmp_path: Path) -> None:
    residue = tmp_path / "gone"
    (residue / "sub").mkdir(parents=True)
    (residue / "sub" / "a.txt").write_text("a", encoding="utf-8")
    assert cs.try_rmtree(residue) is True
    assert not residue.exists()


def test_try_rmtree_on_a_file_is_false_and_keeps_it(tmp_path: Path) -> None:
    plain = tmp_path / "a.txt"
    plain.write_text("a", encoding="utf-8")
    assert cs.try_rmtree(plain) is False
    assert plain.exists()


def test_try_rmtree_uses_the_callback_name_this_python_supports(
    tmp_path: Path, monkeypatch
) -> None:
    seen: list[dict] = []
    real_rmtree = cs.shutil.rmtree

    def fake(path, **kwargs):  # noqa: ANN001
        seen.append(kwargs)
        if len(seen) == 1:
            raise OSError("第一次删不动")
        return real_rmtree(path, ignore_errors=True)

    monkeypatch.setattr(cs.shutil, "rmtree", fake)
    residue = tmp_path / "gone"
    residue.mkdir()
    (residue / "a.txt").write_text("a", encoding="utf-8")

    assert cs.try_rmtree(residue) is True

    key = "onexc" if sys.version_info >= (3, 12) else "onerror"
    assert key in seen[-1], seen
    assert callable(seen[-1][key])


def test_the_retry_callback_still_calls_through(tmp_path: Path, monkeypatch) -> None:
    """回调必须真去调 func —— 掉了这一句，第二次 rmtree 就是白跑一趟。"""
    seen: list[dict] = []

    def fake(path, **kwargs):  # noqa: ANN001
        seen.append(kwargs)
        raise OSError("删不动")

    monkeypatch.setattr(cs.shutil, "rmtree", fake)
    plain = tmp_path / "a.txt"
    plain.write_text("a", encoding="utf-8")

    assert cs.try_rmtree(tmp_path / "gone") is False

    key = "onexc" if sys.version_info >= (3, 12) else "onerror"
    called: list[str] = []
    seen[-1][key](lambda target: called.append(str(target)), str(plain), OSError("x"))
    assert called == [str(plain)]


# -------------------------------------------------------------------- free_up


def test_free_up_runs_both_icacls_steps(tmp_path: Path, monkeypatch) -> None:
    run, calls = _fake_icacls()
    monkeypatch.setattr(cs.subprocess, "run", run)
    ok, detail = cs.free_up(tmp_path)
    assert (ok, detail) == (True, "")
    assert calls == [
        ["icacls", str(tmp_path), "/remove:d", "Everyone", "/t", "/c", "/q"],
        ["icacls", str(tmp_path), "/grant", "*S-1-1-0:(OI)(CI)F", "/t", "/c", "/q"],
    ]


def test_free_up_reports_which_step_failed(tmp_path: Path, monkeypatch) -> None:
    run, _ = _fake_icacls(codes=(5, 0), stderr="拒绝访问。\n再试一次也没用。\n")
    monkeypatch.setattr(cs.subprocess, "run", run)
    ok, detail = cs.free_up(tmp_path)
    assert ok is False
    assert "去掉「拒绝」项失败（icacls 返回 5）" in detail
    assert "再试一次也没用。" in detail


def test_free_up_names_the_second_step_when_it_fails(tmp_path: Path, monkeypatch) -> None:
    run, _ = _fake_icacls(codes=(0, 1), stdout="失败\n")
    monkeypatch.setattr(cs.subprocess, "run", run)
    ok, detail = cs.free_up(tmp_path)
    assert ok is False
    assert "重新授予完全控制失败" in detail
    assert "失败" in detail


def test_free_up_reports_a_missing_icacls(tmp_path: Path, monkeypatch) -> None:
    run, _ = _fake_icacls(oserror=True)
    monkeypatch.setattr(cs.subprocess, "run", run)
    ok, detail = cs.free_up(tmp_path)
    assert ok is False
    assert "icacls 跑不起来" in detail


# ---------------------------------------------------------------------- main


def test_no_arguments_prints_usage_and_returns_2(capsys) -> None:
    assert cs.main([]) == 2
    assert "用法：python tools/clean_scratch.py" in capsys.readouterr().out


def test_dry_run_covers_every_target_and_says_nothing_was_deleted(tmp_path: Path, capsys) -> None:
    first = tmp_path / "one"
    second = tmp_path / "two"
    first.mkdir()
    second.mkdir()

    assert cs.main(["--dry-run", str(first), str(second)]) == 0

    out = capsys.readouterr().out
    assert out.count("[演练] 会删掉") == 2
    assert "演练结束：没有删任何东西。" in out
    assert first.exists() and second.exists()


def test_an_unknown_flag_is_a_usage_error() -> None:
    with pytest.raises(SystemExit) as excinfo:
        cs.main(["--nope"])
    assert excinfo.value.code == 2


def test_help_exits_with_zero(capsys) -> None:
    with pytest.raises(SystemExit) as excinfo:
        cs.main(["--help"])
    assert excinfo.value.code == 0
    assert "--dry-run" in capsys.readouterr().out


def test_one_refused_target_among_good_ones_gives_1(tmp_path: Path, capsys) -> None:
    good = tmp_path / "good"
    good.mkdir()
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)

    assert cs.main([str(good), str(repo)]) == 1

    captured = capsys.readouterr()
    assert not good.exists()
    assert repo.exists()
    assert "不删" in captured.err
