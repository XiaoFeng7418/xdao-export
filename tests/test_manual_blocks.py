"""`packaging/使用说明.txt` 里的版本段结构（每发一版都会在最上面插一段）。

每一版都在「其它」那一节的末尾按「新的在上、旧的依次往下」追加一段，段与段之间用
`上一版（vX.Y.Z）的说明见下）` 这样一行隔开（更老的版本是一行写完的紧凑写法，没有隔行）。
这一步一直是拿本机脚本 `check_usage_txt.py` 核的，2026-10-02 把规则搬进仓库才发现：
那个脚本里负责「衔接」和「有没有夹一整份复制」的两条规则，正则只认 `上一版 vX 的说明见下`
（中间是空格）这种写法 —— 而最近十段用的是 `上一版（vX）的说明见下）`（全角括号、没有空格），
**两条规则在最新的十段上一直空转**，恰好是每次发版都会动的区域。所以这份用例按格式无关的
写法重写，并补一条「紧凑写法只许出现在最下面」。

规则：
① 至少 15 段，每段第一行都要能读出 `X.Y.Z`；
② 最新的那段排在最前，往下依次变老（严格降序）；
③ 文件第一行（`X岛串导出工具 vX.Y.Z（免安装版）`）里的版本号 == 最新那段的版本号；
④ 每一段最多带一行「上一版…的说明见下」，且它指的必须是紧接着的那一段；
⑤ 一段一旦没有隔行，它下面所有段都不能有（新写法在上、老写法在下）；
⑥ 每段的字数不超过 900（粗筛，用来盯住「又被复制了一份」这种翻倍）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANUAL = ROOT / "packaging" / "使用说明.txt"

BLOCK_START = re.compile(r"^- 本版版本号")
# 两种隔行写法都认：`上一版（v0.13.17）的说明见下）` 与 `上一版 v0.13.8 的说明见下）`
SEPARATOR = re.compile(r"^\s*上一版[（(]?\s*v?(\d+\.\d+\.\d+)\s*[）)]?\s*的说明见下")
VERSION = re.compile(r"(\d+\.\d+\.\d+)")
TAIL_MARK = "的说明见下"
MAX_BLOCK_CHARS = 900
MIN_BLOCKS = 15


@dataclass(frozen=True)
class Block:
    line_no: int          # 1 起算的行号
    version: str
    lines: tuple[str, ...]
    separators: tuple[tuple[int, str], ...]   # (行号, 它指的版本)

    @property
    def chars(self) -> int:
        return sum(len(line) for line in self.lines)


def _key(version: str) -> tuple[int, int, int]:
    return tuple(int(part) for part in version.split("."))  # type: ignore[return-value]


@lru_cache(maxsize=1)
def blocks() -> tuple[Block, ...]:
    lines = MANUAL.read_text(encoding="utf-8-sig").splitlines()
    starts = [index for index, line in enumerate(lines) if BLOCK_START.match(line)]
    result: list[Block] = []
    for position, start in enumerate(starts):
        end = starts[position + 1] if position + 1 < len(starts) else len(lines)
        body = list(lines[start:end])
        while body and not body[-1].strip():
            body.pop()
        found = VERSION.search(body[0])
        separators = tuple(
            (index + 1, SEPARATOR.search(line).group(1))  # type: ignore[union-attr]
            for index, line in enumerate(lines[start:end], start=start)
            if SEPARATOR.search(line)
        )
        result.append(
            Block(
                line_no=start + 1,
                version=found.group(1) if found else "?",
                lines=tuple(body),
                separators=separators,
            )
        )
    return tuple(result)


def test_the_scan_finds_the_version_blocks() -> None:
    found = blocks()
    assert len(found) >= MIN_BLOCKS, (
        f"只找到 {len(found)} 段「- 本版版本号」；这份说明应该有一大串版本段。"
        "要么文件被大改过，要么挑段的正则跟写法对不上了。"
    )
    unknown = [b for b in found if b.version == "?"]
    assert not unknown, "这些段的第一行读不出版本号：" + "、".join(f"第 {b.line_no} 行" for b in unknown)


def test_the_blocks_are_newest_first() -> None:
    found = blocks()
    bad = [
        f"第 {left.line_no} 行的 v{left.version} 排在 v{right.version} 前面"
        for left, right in zip(found, found[1:])
        if _key(left.version) <= _key(right.version)
    ]
    assert not bad, "版本段要按「新的在上」排：\n  " + "\n  ".join(bad)


def test_the_first_line_names_the_newest_block() -> None:
    first = MANUAL.read_text(encoding="utf-8-sig").splitlines()[0]
    newest = blocks()[0]
    assert f"v{newest.version}" in first, (
        f"第一行是「{first}」，但最新那段是 v{newest.version}；"
        "发新版时第一行的版本号要和最上面那段一起改。"
    )


def test_every_separator_names_the_next_block() -> None:
    found = blocks()
    problems: list[str] = []
    for position, block in enumerate(found):
        if len(block.separators) > 1:
            where = "、".join(f"第 {line_no} 行" for line_no, _ in block.separators)
            problems.append(f"v{block.version} 这一段带了 {len(block.separators)} 行「…的说明见下」（{where}）")
        for line_no, named in block.separators:
            if position + 1 >= len(found):
                problems.append(f"第 {line_no} 行说「上一版 v{named} 的说明见下」，但它下面没有别的段了")
            elif named != found[position + 1].version:
                problems.append(
                    f"第 {line_no} 行说「上一版 v{named} 的说明见下」，"
                    f"紧接着的却是 v{found[position + 1].version}"
                )
    assert not problems, "隔行的指向要和下一段对得上：\n  " + "\n  ".join(problems)


def test_the_compact_style_only_appears_at_the_bottom() -> None:
    found = blocks()
    problems: list[str] = []
    seen_compact = False
    for block in found:
        if block.separators:
            if seen_compact:
                problems.append(
                    f"第 {block.line_no} 行的 v{block.version} 又用了「上一版…的说明见下」这种隔行，"
                    "但它上面已经出现没隔行的段了"
                )
        else:
            seen_compact = True
    assert not problems, "新写法在上、老的一行写法连着排在下面，中间别又冒出隔行：\n  " + "\n  ".join(problems)


def test_no_block_looks_like_a_pasted_copy() -> None:
    found = blocks()
    problems: list[str] = []
    for block in found:
        marks = sum(line.count(TAIL_MARK) for line in block.lines)
        if marks > 1:
            problems.append(f"第 {block.line_no} 行的 v{block.version} 段里有 {marks} 处「{TAIL_MARK}」（正常只有 1 处）")
        if block.chars > MAX_BLOCK_CHARS:
            problems.append(
                f"第 {block.line_no} 行的 v{block.version} 段有 {block.chars} 字（{len(block.lines)} 行），"
                f"超过 {MAX_BLOCK_CHARS} —— 像是把一整段复制进来了"
            )
    assert not problems, "版本段别被复制粘贴：\n  " + "\n  ".join(problems)
