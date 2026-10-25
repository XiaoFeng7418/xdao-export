# -*- coding: utf-8 -*-
"""覆盖率体检：把 coverage json 变成「哪个文件、哪个函数没被测到」的清单。

用法：python -X utf8 cov_audit.py <cov.json> <源码头> [--top N]
"""
import ast
import json
import sys
from pathlib import Path


def main(argv):
    cov_path = Path(argv[1])
    src_root = Path(argv[2])          # 仓库目录（放 xdao/ 的那一层）
    top = 25
    if "--top" in argv:
        top = int(argv[argv.index("--top") + 1])

    data = json.loads(cov_path.read_text(encoding="utf-8"))
    files = data["files"]

    rows = []
    for fname, info in sorted(files.items()):
        path = Path(fname)
        if not path.is_absolute():
            path = src_root / fname
        if not path.exists():
            continue
        executed = set(info["executed_lines"])
        stmts = set(info["executed_lines"]) | set(info["missing_lines"])
        total = len(stmts)
        if total == 0:
            continue
        hit = len(stmts & executed)
        pct = 100.0 * hit / total
        rel = path.relative_to(src_root) if path.is_relative_to(src_root) else path
        rows.append((pct, hit, total, rel, path, stmts, executed))

    rows.sort(key=lambda r: (r[0], -r[2]))

    print("=" * 78)
    print("一、按文件：语句覆盖率从低到高（只列 < 85% 的）")
    print("=" * 78)
    for pct, hit, total, rel, path, stmts, executed in rows:
        if pct >= 85:
            continue
        print(f"{str(rel):40s} {pct:5.1f}%  {hit:4d}/{total:4d}  缺 {total - hit:4d}")

    print()
    print("=" * 78)
    print(f"二、完全没跑到任何一条语句的函数（按文件列，最多 {top} 个文件）")
    print("=" * 78)
    listed = 0
    for pct, hit, total, rel, path, stmts, executed in rows:
        if listed >= top:
            break
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except Exception as exc:                       # noqa: BLE001
            print(f"{rel}: 解析失败 {exc}")
            continue
        dead = []
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            body_lines = set(range(node.lineno, (node.end_lineno or node.lineno) + 1))
            body_stmts = body_lines & stmts
            if not body_stmts:
                continue                                # 只有签名/docstring 的空函数不算
            if not (body_stmts & executed):
                dead.append((node.lineno, node.name))
        if dead:
            dead.sort()
            listed += 1
            print(f"\n-- {rel}（{len(dead)} 个函数一行都没走到）")
            for lineno, name in dead:
                print(f"   {lineno:5d}  {name}")

    print()
    print("=" * 78)
    print("三、整块没跑到的行段（连续 >= 6 行，按文件）")
    print("=" * 78)
    for pct, hit, total, rel, path, stmts, executed in rows:
        if pct >= 85:
            continue
        missing = sorted(stmts - executed)
        runs, cur = [], [missing[0]] if missing else []
        for n in missing[1:]:
            if n == cur[-1] + 1:
                cur.append(n)
            else:
                runs.append(cur)
                cur = [n]
        if cur:
            runs.append(cur)
        big = [r for r in runs if len(r) >= 6]
        if not big:
            continue
        print(f"\n-- {rel}")
        for r in big[:6]:
            print(f"   行 {r[0]:5d}-{r[-1]:5d}（{len(r)} 行）")


if __name__ == "__main__":
    main(sys.argv)
