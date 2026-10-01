"""文档里写的命令行开关必须真的存在，解析器里的开关也得在文档里露过面。

为什么要有这份检查：``README.md`` 的「命令行」一节与随包发出去的
``packaging/使用说明.txt``，是用户唯一会照着抄的两个地方。开关改名、删掉、
或者新加一个却忘了写进文档 —— 这两种漂移都不会让别的用例变红，只会在用户那里
变成「照着文档抄一遍，程序说没有这个开关」。2026-10-02 起由这份用例把关，
``main.build_parser()`` 是唯一的事实来源。

两条规则：

1. 两份文档里的**命令行示例行**（以 ``python main.py`` / ``main.py`` /
   ``xdao-export.exe`` 开头那几行）用到的每个开关，解析器里都得有；
2. 解析器里每个不是内部开关的选项，至少要在其中一份文档里露过面
   （长写或短写有一个就行）。

只扫示例行是有意的：文档里还会出现浏览器和 PyInstaller 的开关
（``--no-sandbox``、``--windowed``），那些不是本程序的东西。
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

import main

ROOT = Path(__file__).resolve().parents[1]

#: 两份面向用户的文档，相对仓库根。
DOCS = ("README.md", "packaging/使用说明.txt")

#: 命令行示例行的开头。文档里就这三种写法（``python main.py`` 是源码运行，
#: ``xdao-export.exe`` 是免安装版）。
EXAMPLE_START = re.compile(r"^(?:python\s+main\.py|main\.py|xdao-export\.exe)\b")

#: 长开关。
LONG_OPTION = re.compile(r"--[a-z][a-z0-9-]*")

#: 单字母短开关（``-f``、``-o``）；前后不能再有 ``-`` 或词字符，
#: 免得把 ``--format`` 里的 ``-f`` 也算进去。
SHORT_OPTION = re.compile(r"(?<![\w-])-[A-Za-z](?![\w-])")

#: 示例行至少要有这么多条。文档被大改、扫描失效时要红，
#: 而不是「一条都没扫到，于是全绿」。
MIN_EXAMPLE_LINES = 8


def _read_doc(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8-sig")


def _known_options() -> set[str]:
    """解析器认得的全部开关写法（长写 + 短写）。"""
    return {opt for action in main.build_parser()._actions for opt in action.option_strings}


def _example_lines(name: str) -> list[tuple[int, str]]:
    """文档里的命令行示例行：``(行号, 行内容)``。"""
    found: list[tuple[int, str]] = []
    for lineno, line in enumerate(_read_doc(name).splitlines(), 1):
        stripped = line.strip().lstrip(">").strip().strip("`").strip()
        if EXAMPLE_START.match(stripped):
            found.append((lineno, stripped))
    return found


def _options_in(line: str) -> set[str]:
    return set(LONG_OPTION.findall(line)) | set(SHORT_OPTION.findall(line))


def _mentions(text: str, option: str) -> bool:
    """文档里有没有单独出现这个开关（``-f`` 不该匹配到 ``--format`` 里的那几个字符）。"""
    return re.search(r"(?<![\w-])" + re.escape(option) + r"(?![\w-])", text) is not None


def test_the_scan_actually_finds_the_examples() -> None:
    """先确认扫到了东西：改坏正则或文档结构时，别让下面两条变成空转。"""
    lines = [(name, lineno, line) for name in DOCS for lineno, line in _example_lines(name)]

    assert len(lines) >= MIN_EXAMPLE_LINES, f"只扫到 {len(lines)} 条示例行，检查一下扫描规则"
    all_options = set().union(*(_options_in(line) for _, _, line in lines))
    for expected in ("--scope", "--watch", "-f", "-o"):
        assert expected in all_options, f"示例行里应该出现 {expected}"


def test_every_option_used_in_the_examples_exists() -> None:
    """示例行里用到的开关都真的存在（文档没写错）。"""
    known = _known_options()
    unknown: list[str] = []
    for name in DOCS:
        for lineno, line in _example_lines(name):
            for option in sorted(_options_in(line)):
                if option not in known:
                    unknown.append(f"{name}:{lineno} 写着 {option}，解析器里没有（{line}）")

    assert unknown == [], "文档里的命令行示例用了不存在的开关：\n" + "\n".join(unknown)


def test_every_option_is_mentioned_in_the_docs() -> None:
    """解析器里的开关都得在文档里露过面（新加开关忘了写文档就红在这里）。"""
    docs = {name: _read_doc(name) for name in DOCS}
    missing: list[str] = []
    for action in main.build_parser()._actions:
        if not action.option_strings:
            continue  # 位置参数（串号或网址）
        if action.help is argparse.SUPPRESS:
            continue  # 内部开关（例如 --apply-update），本来就不给用户看
        if any(_mentions(text, option) for text in docs.values() for option in action.option_strings):
            continue
        missing.append("/".join(action.option_strings))

    assert missing == [], (
        "这些开关在 README.md 和 packaging/使用说明.txt 里都没出现过，"
        "要么写进文档、要么在解析器里标成 argparse.SUPPRESS：\n  " + "\n  ".join(missing)
    )
