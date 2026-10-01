# -*- coding: utf-8 -*-
"""文档里写死的「有多少个用例」必须和真实收集数一致。

为什么要有这份检查：这类数字是「当前状态」式声明，改起来又没人提醒 —— 2026-10-02 查的时候，
``HANDOFF.md`` 那张测试表里有 5 个文件根本没进表、7 个文件的项数还是旧的，加起来差了 145 项；
``README.md`` 的目录树里还写着「1119 个离线单元测试（1113 通过，另 6 个真机用例默认跳过）」，
那时实际已经是 1266 项。谁都不会因为「地图少了一页」而报错，所以这里拿
``pytest --collect-only -q`` 的真实收集数逐处核一遍。

数字怎么来的：跑一次 ``--collect-only``（只收集、不执行），按文件归组数一遍。
收集数与平台无关（唯一一处 ``if os.name == "nt"`` 只是选了一个错误对象，不是条件定义用例）。
能被跨平台核对的只有「总数」与「通过 + 跳过 = 总数」这类算术关系 ——
本机 Windows 上到底通过多少，Linux CI 上核不了（Tk 相关用例在无显示环境下会多跳过一些）。
"""

from __future__ import annotations

import re
import subprocess
import sys
from functools import lru_cache
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HANDOFF = ROOT / "HANDOFF.md"
README = ROOT / "README.md"

#: 表里的一行：``│  ├─ test_cache.py   说明（48）`` / ``│  └─ test_window.py 说明（36，需真 Tk）``
TREE_LINE = re.compile(r"^│\s*[├└]─\s*(test_[a-z0-9_]+\.py)\s+(.*)$")
#: 项数的两种写法：``（48）`` / ``PDF 真机渲染 4 条（……）``
COUNT_PAREN = re.compile(r"（(\d+)")
COUNT_TIAO = re.compile(r"(\d+) 条")
#: 表头那句：``单元测试 N 项（……）``
SUMMARY_COUNT = re.compile(r"单元测试\s*(\d+)\s*项")
#: ``README.md`` 目录树里那句：``tests/   1119 个离线单元测试（1113 通过，另 6 个真机用例默认跳过）``
README_TESTS_LINE = re.compile(r"tests/\s+(\d+) 个离线单元测试（(\d+) 通过，另 (\d+) 个真机用例默认跳过）")

#: 表里至少该有这么多行 —— 防「一行都没解析到」的假绿
MIN_ENTRIES = 25


@lru_cache(maxsize=1)
def _collected_counts() -> dict[str, int]:
    """跑一次 ``pytest --collect-only -q``，按文件数用例。"""
    finished = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    counts: dict[str, int] = {}
    for line in finished.stdout.splitlines():
        if "::" not in line:
            continue
        relative = line.split("::", 1)[0].strip()
        if relative.endswith(".py"):
            name = Path(relative).name
            counts[name] = counts.get(name, 0) + 1
    assert counts, f"没收集到任何用例，检查本身不可信：\n{finished.stdout[-2000:]}"
    return counts


def _documented_table() -> dict[str, int]:
    """解析 ``HANDOFF.md`` 的测试表，返回 ``{文件名: 表里写的项数}``。"""
    documented: dict[str, int] = {}
    problems: list[str] = []
    for number, line in enumerate(HANDOFF.read_text(encoding="utf-8").splitlines(), 1):
        match = TREE_LINE.match(line)
        if not match:
            continue
        name, rest = match.group(1), match.group(2)
        count = COUNT_PAREN.search(rest) or COUNT_TIAO.search(rest)
        if count is None:
            problems.append(f"  HANDOFF.md:{number} 看不出项数：{line.strip()}")
            continue
        documented[name] = int(count.group(1))
    assert not problems, "表里的行得写明有多少项（`（48）` 或 `4 条`）：\n" + "\n".join(problems)
    assert len(documented) >= MIN_ENTRIES, f"只解析到 {len(documented)} 行，这张表不对，检查本身不可信"
    return documented


def test_the_handoff_test_table_lists_every_test_file_with_the_right_count() -> None:
    documented = _documented_table()
    real = _collected_counts()

    missing = sorted(set(real) - set(documented))
    extra = sorted(set(documented) - set(real))
    wrong = sorted(
        f"  {name}：表里写 {documented[name]} 项，实际收集到 {real[name]} 项"
        for name in set(documented) & set(real)
        if documented[name] != real[name]
    )

    assert not (missing or extra or wrong), (
        "HANDOFF.md 的测试表和真实收集数对不上：\n"
        + ("\n".join(f"  表里没写：{name}" for name in missing) + "\n" if missing else "")
        + ("\n".join(f"  多出来的行：{name}" for name in extra) + "\n" if extra else "")
        + ("\n".join(wrong) + "\n" if wrong else "")
        + "改法：跑 `& $py -X utf8 -m pytest --collect-only -q`，按文件数一遍，把表里的数字改对。"
    )


def test_the_handoff_summary_count_matches_the_collected_total() -> None:
    text = HANDOFF.read_text(encoding="utf-8")
    found = SUMMARY_COUNT.search(text)
    assert found, "HANDOFF.md 的第二节应当写明一共多少项用例（「单元测试 N 项」）"
    documented = int(found.group(1))
    total = sum(_collected_counts().values())
    assert documented == total, (
        f"HANDOFF.md 写的是「单元测试 {documented} 项」，实际收集到 {total} 项。改法同上。"
    )


def test_the_readme_test_count_line_matches_the_collected_total() -> None:
    found = README_TESTS_LINE.search(README.read_text(encoding="utf-8"))
    assert found, "README.md 的目录树里应当有一行写明 tests/ 有多少个离线用例、默认跳过几个"
    total, passed, skipped = (int(group) for group in found.groups())
    real = sum(_collected_counts().values())

    assert total == real, (
        f"README.md 写的是「{total} 个离线单元测试」，实际收集到 {real} 个。"
        "改法：跑 `& $py -X utf8 -m pytest --collect-only -q` 拿总数，再跑一次全量拿通过数。"
    )
    assert passed + skipped == total, (
        f"README.md 那句自己就不自洽：{passed} 通过 + {skipped} 跳过 = {passed + skipped}，"
        f"但写的是 {total} 个用例。"
    )


if __name__ == "__main__":  # pragma: no cover - 手动跑时给个人看的汇总
    for name, count in sorted(_documented_table().items()):
        print(f"{name}  表里 {count}  实际 {_collected_counts().get(name, '—')}")
    print(f"一共 {sum(_collected_counts().values())} 项")
    raise SystemExit(pytest.main([__file__, "-q"]))
