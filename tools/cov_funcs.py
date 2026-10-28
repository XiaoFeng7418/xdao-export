# -*- coding: utf-8 -*-
"""把 coverage json 的「没跑到的行」归到所在函数，输出每个函数漏了多少行。

用法：python -X utf8 cov_funcs.py <cov.json> <仓库目录> [文件名...]
"""
import ast
import json
import sys
from pathlib import Path


def functions_of(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    fns = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            fns.append((node.lineno, node.end_lineno or node.lineno, node.name))
    fns.sort()
    return fns


def enclosing(fns, lineno):
    best = None
    for start, end, name in fns:
        if start <= lineno <= end:
            if best is None or (end - start) < (best[1] - best[0]):
                best = (start, end, name)
    return best[2] if best else "<模块级>"


def main(argv):
    data = json.loads(Path(argv[1]).read_text(encoding="utf-8"))
    root = Path(argv[2])
    only = set(argv[3:])
    for fname, info in sorted(data["files"].items()):
        rel = Path(fname).name
        if only and rel not in only:
            continue
        path = Path(fname)
        if not path.is_absolute():
            path = root / fname
        if not path.exists():
            continue
        missing = sorted(set(info["missing_lines"]))
        fns = functions_of(path)
        stats = {}
        for line in missing:
            name = enclosing(fns, line)
            stats.setdefault(name, []).append(line)
        total = len(info["executed_lines"]) + len(info["missing_lines"])
        hit = len(info["executed_lines"])
        print(f"\n=== {rel}  覆盖 {hit}/{total} = {100.0*hit/total:.0f}%")
        for name, lines in sorted(stats.items(), key=lambda kv: -len(kv[1])):
            if len(lines) < 3:
                continue
            spans = []
            cur = [lines[0]]
            for n in lines[1:]:
                if n == cur[-1] + 1:
                    cur.append(n)
                else:
                    spans.append(cur)
                    cur = [n]
            spans.append(cur)
            text = ", ".join(
                f"{s[0]}-{s[-1]}" if len(s) > 1 else str(s[0]) for s in spans[:6]
            )
            print(f"  {len(lines):4d} 行漏  {name:38s} 行 {text}")


if __name__ == "__main__":
    main(sys.argv)
