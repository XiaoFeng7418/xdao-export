"""文本卫生：文件开头的 BOM 与行尾风格。

起因：`packaging/使用说明.txt` 本该是一个 UTF-8 BOM，有一次变成了**两个**
（`EF BB BF EF BB BF`）—— 那是在本机脚本里读文件没剥掉原 BOM、写回又贴了一个，
肉眼看不见，一路进了 zip。行尾混用同理：`\r\n` 和裸 `\n` 混在同一个文件里，
diff 会整段变色、脚本按行处理时容易多出看不见的 `\r`。

这份用例把「查改动文件」的本机脚本（`check_text_hygiene.py`）升成对**仓库里所有被跟踪的
文本文件**的检查，跑在 CI 上：
① 开头不允许有第二个 BOM；
② 同一个文件里不允许 `\r\n` 与裸 `\n` 混用；
③ 三个要交给 Windows 工具打开的文件必须**保留**开头那一个 BOM（见下）；
④ 一个确实被改过的文件，工作区的行尾不能与库里（索引）的行尾是两种 —— 否则提交上去
   就是「每一行都变了」的假改动（2026-10-02：本机脚本真的把三个文档整份翻成了 LF）。
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

# 索引里就是 CRLF 的历史文件：它们是早年（.gitattributes 立规矩之前）进的库，所以
# `git ls-files --eol` 对它们报 `i/crlf`，而 .gitattributes 说全库 eol=lf —— 声明与存盘
# 一直没对齐（2026-10-02 查清楚：这些文件每次 `git add` 都被 stat 缓存短路，所以谁也没发现）。
# 它们在 Windows 上检出是 CRLF、在 Linux 上检出是 LF，两种都算正常，因此**只对这四放开**；
# 名单不许变长（下面那条用例会核对），别再让新的 CRLF 进库。
LEGACY_CRLF_IN_INDEX = (
    "HANDOFF.md",
    "MAINTENANCE.md",
    "README.md",
    "docs/RELEASE_NOTES_v0.4.0.md",
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


# ---------------------------------------------------------------------------
# 行尾风格：索引（= 将要提交的形态）与工作区必须一致
# ---------------------------------------------------------------------------
#
# 起因（2026-10-02）：一个改文档的本机脚本把 README/HANDOFF/MAINTENANCE 整份写成了 LF，
# 三个文件的每一行都「变了」，而且**没有任何用例拦得住** —— 只有肉眼看 `git diff --stat`
# 才发现。行尾混用（上面那条）管的是「同一个文件里两种混着」，管不了「整份翻掉」。
#
# `git ls-files --eol` 一条命令就能看出来：每条给 `i/<索引形态> w/<工作区形态> attr/<属性>`。


def _parse_eol_rows(raw: bytes) -> list[tuple[str, str, str, str]]:
    """把 `git ls-files --eol -z` 的输出解析成 [(路径, 索引形态, 工作区形态, 属性)]。

    一条长这样（字段之间是空格，属性后面还跟着一个空格）::

        i/lf    w/crlf  attr/text=auto eol=lf \tREADME.md

    认不出来的条目直接跳过，不让 git 各版本的格式差异把用例弄红。
    """
    rows: list[tuple[str, str, str, str]] = []
    for item in raw.split(b"\0"):
        if not item:
            continue
        meta, _, path = item.partition(b"\t")
        fields = meta.decode("utf-8", "replace").split()
        if len(fields) < 3:
            continue
        rows.append(
            (
                path.decode("utf-8", "replace"),
                fields[0].removeprefix("i/"),
                fields[1].removeprefix("w/"),
                # 属性本身可能带空格（`attr/text eol=crlf`），所以剩下的字段要合起来。
                " ".join(fields[2:]).removeprefix("attr/"),
            )
        )
    return rows


def _eol_rows() -> list[tuple[str, str, str, str]]:
    done = subprocess.run(
        ["git", "ls-files", "--eol", "-z"], cwd=ROOT, capture_output=True
    )
    assert done.returncode == 0, f"git ls-files --eol 失败：{done.stderr.decode('utf-8', 'replace')}"
    rows = _parse_eol_rows(done.stdout)
    assert len(rows) > 100, f"只解析出 {len(rows)} 条，像是在仓库外面跑的"
    return rows


def test_the_eol_report_parser_understands_git_output() -> None:
    raw = (
        b"i/lf    w/crlf  attr/text=auto eol=lf \tREADME.md\0"
        b"i/crlf  w/crlf  attr/text=auto eol=lf \tHANDOFF.md\0"
        b"i/lf    w/crlf  attr/text eol=crlf    \t\xe8\xaf\x8a\xe6\x96\xad\xe5\x86\x99\xe5\x85\xa5.ps1\0"
        b"i/lf    w/lf    attr/                 \txdao/gui.py\0"
        b"\0"
        b"\xe8\xbf\x99\xe4\xb8\x8d\xe6\x98\xaf\xe4\xb8\x80\xe6\x9d\xa1\xe8\x83\xbd\xe8\xae\xa4\xe5\x87\xba\xe6\x9d\xa5\xe7\x9a\x84\xe8\xae\xb0\xe5\xbd\x95\0"
    )
    assert _parse_eol_rows(raw) == [
        ("README.md", "lf", "crlf", "text=auto eol=lf"),
        ("HANDOFF.md", "crlf", "crlf", "text=auto eol=lf"),
        ("诊断写入.ps1", "lf", "crlf", "text eol=crlf"),
        ("xdao/gui.py", "lf", "lf", ""),
    ]


def _dirty_files() -> set[str]:
    """git 认为与 HEAD 有内容差异的文件（含已暂存的）。

    用 `git diff`（内容比较）而不是 `git status`：工作区行尾被改过、但过滤之后与库里一模一样时，
    git 只会在 stat 缓存里「看起来脏」，那不是我们关心的（提交上去不会变）。
    """
    names: set[str] = set()
    for args in (
        ["git", "diff", "--name-only", "-z"],
        ["git", "diff", "--cached", "--name-only", "-z"],
    ):
        done = subprocess.run(args, cwd=ROOT, capture_output=True)
        assert done.returncode == 0, f"{' '.join(args)} 失败：{done.stderr.decode('utf-8', 'replace')}"
        names.update(name for name in done.stdout.decode("utf-8", "replace").split("\0") if name)
    return names


def test_index_and_worktree_line_endings_agree() -> None:
    rows = _eol_rows()
    dirty = _dirty_files()
    bad: list[str] = []
    legacy_seen: list[str] = []
    for path, index_eol, worktree_eol, attr in rows:
        if "eol=crlf" in attr:
            # 属性要求工作区就是 CRLF（Windows 的 .ps1 / .cmd），索引里是 LF 才正常。
            if worktree_eol != "crlf":
                bad.append(f"{path}：属性要求工作区 CRLF，现在是 {worktree_eol}")
            continue
        if path in LEGACY_CRLF_IN_INDEX:
            legacy_seen.append(path)
            if index_eol != "crlf":
                bad.append(
                    f"{path}：索引里已经不是 CRLF 了（被重新规范化过？）—— "
                    "请把它从 LEGACY_CRLF_IN_INDEX 里删掉"
                )
        elif index_eol != "lf":
            bad.append(f"{path}：索引里是 {index_eol}，按 .gitattributes 新内容该以 LF 入库")
        if index_eol != worktree_eol and path in dirty:
            bad.append(
                f"{path}：索引里是 {index_eol}，工作区是 {worktree_eol}，而且这个文件确实和库里不一样 —— "
                "整份行尾被翻过，提交上去会变成「每一行都变了」"
            )
    missing = [path for path in LEGACY_CRLF_IN_INDEX if path not in legacy_seen]
    assert not missing, f"这几个文件不在被跟踪列表里了：{missing}"
    assert not bad, (
        "改文件时按它原来的行尾写：本机脚本读写要用二进制（`read_bytes` / `write_bytes`），"
        "别让编辑器或字符串替换把整份行尾换掉。\n  " + "\n  ".join(bad)
    )
