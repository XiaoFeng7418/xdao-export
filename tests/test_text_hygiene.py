"""文本卫生：文件开头的 BOM 与行尾风格。

起因：`packaging/使用说明.txt` 本该是一个 UTF-8 BOM，有一次变成了**两个**
（`EF BB BF EF BB BF`）—— 那是在本机脚本里读文件没剥掉原 BOM、写回又贴了一个，
肉眼看不见，一路进了 zip。行尾混用同理：`\r\n` 和裸 `\n` 混在同一个文件里，
diff 会整段变色、脚本按行处理时容易多出看不见的 `\r`。

这份用例把「查改动文件」的本机脚本（`check_text_hygiene.py`）升成对**仓库里所有被跟踪的
文本文件**的检查，跑在 CI 上：
① 开头不允许有第二个 BOM；
② 同一个文件里不允许 `\r\n` 与裸 `\n` 混用；
③ 三个要交给 Windows 工具打开的文件必须**保留**开头那一个 BOM（见下）。
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BOM = b"\xef\xbb\xbf"
SNIFF = 8192

# 这三个文件是给 Windows 自己的工具打开的，BOM 不能掉：
#   使用说明.txt        —— 记事本/写字板按 BOM 认出 UTF-8，中文才不乱码（它会随包发出去）
#   诊断写入.ps1        —— Windows PowerShell 5.1 读不带 BOM 的脚本会按 ANSI 解，中文注释变乱码
#   诊断写入-双击运行.cmd —— cmd 读批处理里的中文同理
KEEP_ONE_BOM = (
    "packaging/使用说明.txt",
    "诊断写入.ps1",
    "诊断写入-双击运行.cmd",
)


def _tracked_files() -> list[str]:
    done = subprocess.run(
        ["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, text=True, encoding="utf-8"
    )
    assert done.returncode == 0, f"git ls-files 失败：{done.stderr.strip()}"
    names = [name for name in done.stdout.split("\0") if name]
    assert len(names) > 100, f"只拿到 {len(names)} 个被跟踪文件，像是在仓库外面跑的"
    return names


def _text_files() -> list[tuple[str, bytes]]:
    """((相对路径, 内容))，跳过二进制。"""
    result: list[tuple[str, bytes]] = []
    for name in _tracked_files():
        data = (ROOT / name).read_bytes()
        if b"\0" in data[:SNIFF]:
            continue
        result.append((name, data))
    assert len(result) > 90, f"只读到 {len(result)} 个文本文件，太少了"
    return result


def _bom_count(data: bytes) -> int:
    count = 0
    while data[count * 3 : count * 3 + 3] == BOM:
        count += 1
    return count


def _endings(data: bytes) -> tuple[int, int]:
    crlf = data.count(b"\r\n")
    return crlf, data.count(b"\n") - crlf


def test_no_file_starts_with_two_boms() -> None:
    bad = [
        f"{name}：开头有 {count} 个 BOM"
        for name, data in _text_files()
        if (count := _bom_count(data)) > 1
    ]
    assert not bad, (
        "一个文件开头最多一个 BOM（读的时候先用 utf-8-sig 剥掉，写回时别再贴一个）：\n  "
        + "\n  ".join(bad)
    )


def test_no_file_mixes_crlf_and_bare_lf() -> None:
    bad = []
    for name, data in _text_files():
        crlf, lf = _endings(data)
        if crlf and lf:
            bad.append(f"{name}：CRLF {crlf} 处 / 裸 LF {lf} 处")
    assert not bad, (
        "同一个文件里别把 CRLF 与裸 LF 混着用（改文件时按它原来的行尾写）：\n  " + "\n  ".join(bad)
    )


@pytest.mark.parametrize("name", KEEP_ONE_BOM)
def test_the_files_windows_tools_open_keep_their_bom(name: str) -> None:
    data = (ROOT / name).read_bytes()
    count = _bom_count(data)
    assert count == 1, (
        f"{name} 开头应当正好一个 BOM，现在是 {count} 个。"
        "这个文件是给 Windows 的记事本 / PowerShell 5.1 / cmd 打开的，去掉 BOM 中文会乱码。"
    )
