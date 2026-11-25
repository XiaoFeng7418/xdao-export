"""清理测试残留目录。

工具只处理命令行明确指定的目录。删除失败时，可按需调整目标目录权限后重试。
盘符根目录、用户主目录、包含 ``.git`` 的仓库目录，以及当前目录或其祖先均会被拒绝。

符号链接、目录联接与文件分别处理；目录联接通过 ``os.rmdir`` 移除链接本身，不跟随目标。
文件作为文件删除，目录按目录清理。权限更新仅作用于命令行指定的目标。

用法：
    python tools/clean_scratch.py .pytest_tmp .pytest-scratch
    python tools/clean_scratch.py --dry-run .pytest_tmp

退出码：0 表示指定目标已处理或不存在；1 表示目标未删除或命中安全限制；2 表示用法错误。

Python 3.12 起，``shutil.rmtree`` 的 ``onerror`` 已弃用；Python 3.14 将移除该参数。
因此 Python 3.12 及以上使用 ``onexc``，较旧版本使用 ``onerror``。
"""

from __future__ import annotations

import argparse
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

# 权限项里代表「拒绝删除」的标记。
DENY_FLAGS = "(DENY)"

USAGE = "用法：python tools/clean_scratch.py [--dry-run] <目录> [<目录>...]"

# icacls 的两步：先摘掉拒绝项，再把完全控制权授回去。
_ICACLS_STEPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("去掉「拒绝」项", ("/remove:d", "Everyone", "/t", "/c", "/q")),
    ("重新授予完全控制", ("/grant", "*S-1-1-0:(OI)(CI)F", "/t", "/c", "/q")),
)


def refuse_reason(path: Path) -> str | None:
    """该不该拒绝删除这个目标；不该删就返回原因，可以删返回 None。"""
    try:
        real = path.resolve()
    except OSError:
        real = path
    if real.parent == real:
        return "这是盘符（文件系统）根目录"
    try:
        home = Path.home().resolve()
    except (OSError, RuntimeError):
        home = None
    if home is not None and real == home:
        return "这是用户主目录"
    if (real / ".git").exists():
        return "这里是个 git 仓库（有 .git），不像测试残留"
    try:
        cwd = Path.cwd().resolve()
    except OSError:
        cwd = None
    if cwd is not None:
        if cwd == real:
            return "当前工作目录就是它"
        if cwd.is_relative_to(real):
            return "当前工作目录在它里面，删了会把现场一起删掉"
    return None


def link_kind(path: Path) -> str | None:
    """是链接就返回类型名（符号链接 / 目录联接），不是链接返回 None。"""
    if path.is_symlink():
        return "符号链接"
    is_junction = getattr(path, "is_junction", None)
    if is_junction is not None:
        try:
            if is_junction():
                return "目录联接（junction）"
        except OSError:
            return None
        return None
    if os.name == "nt":  # 3.10 / 3.11 没有 Path.is_junction，退回看 reparse 标记
        try:
            if os.lstat(path).st_reparse_tag:
                return "目录联接（junction）"
        except (AttributeError, OSError):
            return None
    return None


def remove_link_like(path: Path, kind: str) -> tuple[bool, str]:
    """只删链接本身：符号链接用 unlink，junction 用 rmdir（它指向的地方不动）。"""
    try:
        if path.is_symlink():
            path.unlink()
        else:
            os.rmdir(path)
    except OSError as exc:
        return False, f"删不掉这个{kind}：{type(exc).__name__}: {exc}"
    return True, f"已删除（这是个{kind}，只摘掉链接本身，它指向的地方没动）"


def count_entries(path: Path) -> int | None:
    """数一下里面有多少条目；读不动就返回 None（而不是让整个工具崩掉）。"""
    try:
        return sum(1 for _ in path.rglob("*"))
    except OSError as exc:
        print(f"  条目数读不出来：{type(exc).__name__}: {exc}", file=sys.stderr)
        return None


def try_rmtree(path: Path) -> bool:
    """先普通删除，失败则逐个去掉只读/拒绝标记后再删；最后以「目录还在不在」为准。"""
    try:
        shutil.rmtree(path)
        return True
    except OSError:
        pass

    def retry_remove(func, target, _exc):  # noqa: ANN001
        try:
            Path(target).chmod(stat.S_IWRITE)
            func(target)
        except OSError:
            pass

    kwargs = (
        {"onexc": retry_remove}
        if sys.version_info >= (3, 12)
        else {"onerror": retry_remove}
    )
    try:
        shutil.rmtree(path, **kwargs)
    except OSError:
        return False
    return not path.exists()


def _icacls(path: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["icacls", str(path), *args],
        capture_output=True,
        text=True,
        encoding="gbk",
        errors="replace",
    )


def free_up(path: Path) -> tuple[bool, str]:
    """用 icacls 去掉该目录及其子项上的拒绝项，返回 (是否两句都跑成, 说明)。"""
    for label, args in _ICACLS_STEPS:
        try:
            result = _icacls(path, *args)
        except OSError as exc:
            return False, f"{label}：icacls 跑不起来（{type(exc).__name__}: {exc}）"
        if result.returncode != 0:
            lines = (result.stderr or result.stdout or "").strip().splitlines()
            tail = f"：{lines[-1]}" if lines else ""
            return False, f"{label}失败（icacls 返回 {result.returncode}）{tail}"
    return True, ""


def clean_one(raw: str, *, dry_run: bool = False) -> int:
    """处理一个目标，返回 0（处理完/本来就不存在）或 1（没删成）。"""
    path = Path(raw)
    if not path.exists() and not path.is_symlink():
        print(f"跳过（不存在）：{path}")
        return 0

    reason = refuse_reason(path)
    if reason:
        print(f"× 不删 {path}：{reason}。", file=sys.stderr)
        return 1

    is_link = link_kind(path)
    if is_link or path.is_file():
        kind = is_link or "文件"
        if dry_run:
            print(f"[演练] 会删掉这个{kind}：{path}")
            return 0
        if kind == "文件":
            try:
                path.unlink()
            except OSError as exc:
                print(f"  删不掉这个{kind}：{type(exc).__name__}: {exc}", file=sys.stderr)
                return 1
            removed, note = True, "已删除（这是个文件，不是目录）"
        else:
            removed, note = remove_link_like(path, kind)
        if not removed:
            print(f"  {note}", file=sys.stderr)
            return 1
        print(f"  {note}")
        return 0

    count = count_entries(path)
    size_text = f"{count} 个条目" if count is not None else "条目数读不出来"
    if dry_run:
        print(f"[演练] 会删掉 {path}（{size_text}）")
        return 0

    print(f"清理 {path}（{size_text}）…")
    if try_rmtree(path):
        print("  已删除")
        return 0

    print("  直接删除被拒，尝试调整自身权限…")
    freed, detail = free_up(path)
    if not freed:
        print(f"  调整权限没成功：{detail}", file=sys.stderr)
    if try_rmtree(path):
        print("  调整权限后已删除")
        return 0
    print("  仍无法删除，请检查该目录是否被其它程序占用", file=sys.stderr)
    return 1


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="clean_scratch.py",
        description="清理测试残留目录（危险目标会被拦下）",
        usage=USAGE,
    )
    parser.add_argument("paths", nargs="*", metavar="目录")
    parser.add_argument(
        "-n",
        "--dry-run",
        action="store_true",
        help="只说要删什么，不真删",
    )
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    if not args.paths:
        print(USAGE)
        return 2

    exit_code = 0
    for raw in args.paths:
        if clean_one(raw, dry_run=args.dry_run):
            exit_code = 1
    if args.dry_run:
        print("演练结束：没有删任何东西。")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
