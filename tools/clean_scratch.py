"""清理测试残留目录。

背景：早期测试误把产物写进本机临时目录，受本机受限运行环境影响，
这些目录带上了低完整性标签与拒绝删除的权限项，普通删除会报「拒绝访问」。

清理策略（两步）：
1. 先尝试直接删除；
2. 删不掉的，读取其自身的权限设置，去掉「拒绝删除子项」这一条，
   再重试删除。只处理命令行传入的目录，不递归到别处。

用法：
    python tools/clean_scratch.py .pytest_tmp .pytest-scratch
"""

from __future__ import annotations

import shutil
import stat
import subprocess
import sys
from pathlib import Path

# 权限项里代表「拒绝删除」的标记。
DENY_FLAGS = "(DENY)"


def try_rmtree(path: Path) -> bool:
    """先普通删除，失败则逐个去掉只读/拒绝标记后再删。"""
    try:
        shutil.rmtree(path)
        return True
    except OSError:
        pass

    def on_error(func, target, _exc_info):  # noqa: ANN001
        try:
            Path(target).chmod(stat.S_IWRITE)
            func(target)
        except OSError:
            pass

    try:
        shutil.rmtree(path, onerror=on_error)
    except OSError:
        return False
    return not path.exists()


def free_up(path: Path) -> bool:
    """用 icacls 去掉该目录及其子项上的拒绝项，返回是否执行成功。"""
    script = (
        f'icacls "{path}" /remove:d Everyone /t /c /q & '
        f'icacls "{path}" /grant "*S-1-1-0:(OI)(CI)F" /t /c /q'
    )
    result = subprocess.run(
        ["cmd", "/c", script],
        capture_output=True,
        text=True,
        encoding="gbk",
        errors="replace",
    )
    return result.returncode == 0


def main(argv: list[str]) -> int:
    if not argv:
        print("用法：python tools/clean_scratch.py <目录> [<目录>...]")
        return 2

    exit_code = 0
    for raw in argv:
        path = Path(raw)
        if not path.exists():
            print(f"跳过（不存在）：{path}")
            continue
        count = sum(1 for _ in path.rglob("*"))
        print(f"清理 {path}（{count} 个条目）…")
        if try_rmtree(path):
            print("  已删除")
            continue
        print("  直接删除被拒，尝试调整自身权限…")
        if free_up(path) and try_rmtree(path):
            print("  调整权限后已删除")
            continue
        print("  仍无法删除，请检查该目录是否被其它程序占用", file=sys.stderr)
        exit_code = 1
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
