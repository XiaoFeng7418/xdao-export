"""版本号不能各写各的：一个版本号同时躺在四处纯文本里，错一处用户就看见矛盾。

为什么要有这份检查：发一个版本要手改四处的**文字**——

1. ``xdao/__init__.py`` 的注释（``v0.13.18 ↔ 0.13.18``）与 ``__version__`` 本身；
2. ``README.md`` 的「最新版 **vX** 提供免安装包」加上附件表里的 zip 名；
3. ``packaging/使用说明.txt`` 的第 1 行，以及「- 本版版本号：X」那条（会随包发出去）；
4. ``docs/RELEASE_NOTES_vX.md`` —— 文件得存在，标题得是 ``# vX…``，还得写明附件叫什么。

CI 里那个 `版本号与最新 Release 一致` 只比 tag 与 ``xdao.__version__``，
上面这四处文档落点没人管：漏改一处，程序和包里就写着两个版本号，
而所有用例照样全绿。2026-10-02 起由这份用例把关，事实来源只有
``xdao.__version__``。

改版本号时这些检查红掉是**预期**的（它们本来就是提醒清单），
跟着红的地方把四处一起改掉就绿了。
"""

from __future__ import annotations

import re
from pathlib import Path

from xdao import __version__

ROOT = Path(__file__).resolve().parents[1]

VERSION = __version__

#: 免安装包的名字，README、使用说明与发布说明里都按这个名字写。
ZIP_NAME = f"xdao-export-v{VERSION}-win64.zip"

#: 文档里出现过的所有免安装包名（用来抓「表里还留着上一个版本」）。
ANY_ZIP = re.compile(r"xdao-export-v(\d+\.\d+\.\d+)-win64\.zip")

#: 使用说明里那条版本说明，最新的在最上面。
MANUAL_VERSION_LINE = re.compile(r"^- 本版版本号：(\d+\.\d+\.\d+)", re.MULTILINE)

#: ``xdao/__init__.py`` 里那句「与 GitHub Release 的标签保持一致」。
MODULE_NOTE = "版本号与 GitHub Release"

SEMVER = re.compile(r"^\d+\.\d+\.\d+$")


def _read(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8-sig")


def _lines(name: str) -> list[str]:
    return _read(name).splitlines()


def _first_nonblank(lines: list[str]) -> str:
    for line in lines:
        if line.strip():
            return line.strip()
    return ""


def test_the_version_is_a_plain_semver() -> None:
    """下面几条规则都按 ``数字.数字.数字`` 写；版本号写成别的形状就先红在这里。"""
    assert SEMVER.match(VERSION), f"版本号长得不像 x.y.z：{VERSION!r}"


def test_module_comment_matches_the_version() -> None:
    """``xdao/__init__.py`` 里那句注释要跟着版本号一起改。"""
    hits = [line for line in _lines("xdao/__init__.py") if MODULE_NOTE in line]

    assert hits, f"xdao/__init__.py 里找不到「{MODULE_NOTE}」那句注释"
    assert any(f"v{VERSION}" in line and VERSION in line for line in hits), (
        f"xdao/__init__.py 的注释没跟上版本号 {VERSION}：\n  " + "\n  ".join(hits)
    )


def test_readme_download_section_matches_the_version() -> None:
    """README 的「最新版」那句与附件表里的 zip 名，都得是当前版本。"""
    text = _read("README.md")

    assert f"最新版 **v{VERSION}**" in text, (
        f"README.md 里没有「最新版 **v{VERSION}**」这句（改版本号时漏了，或者措辞被改动了）"
    )

    found = ANY_ZIP.findall(text)
    assert found, "README.md 的附件表里没扫到免安装包名，检查一下表格是不是改了写法"
    stale = sorted({name for name in found if name != VERSION})
    assert stale == [], f"README.md 里还写着别的版本：{stale}（当前版本是 {VERSION}）"
    assert ZIP_NAME in text, f"README.md 里没有 {ZIP_NAME}"


def test_manual_header_and_version_note_match() -> None:
    """随包发出去的那份说明：第 1 行的版本号，以及最上面那条版本说明。"""
    lines = _lines("packaging/使用说明.txt")

    first = _first_nonblank(lines)
    assert first == f"X岛串导出工具 v{VERSION}（免安装版）", (
        f"packaging/使用说明.txt 第 1 行是 {first!r}，应当写 v{VERSION}"
    )

    versions = MANUAL_VERSION_LINE.findall(_read("packaging/使用说明.txt"))
    assert versions, "使用说明里一条「- 本版版本号：」都没扫到，检查一下是不是改了写法"
    assert versions[0] == VERSION, (
        f"使用说明最上面那条版本说明是 {versions[0]}，当前版本是 {VERSION}"
        "（新版本要写在最上面）"
    )


def test_release_notes_for_this_version_exist() -> None:
    """发布说明：文件要在、标题要对、还得写明附件叫什么（发版脚本靠它）。"""
    path = ROOT / "docs" / f"RELEASE_NOTES_v{VERSION}.md"

    assert path.exists(), f"缺少 docs/RELEASE_NOTES_v{VERSION}.md（发版前要写一份）"

    lines = path.read_text(encoding="utf-8-sig").splitlines()
    title = _first_nonblank(lines)
    assert title.startswith(f"# v{VERSION}"), f"{path.name} 的标题是 {title!r}，应当以 # v{VERSION} 开头"

    assert ZIP_NAME in path.read_text(encoding="utf-8-sig"), (
        f"{path.name} 里没写明附件 {ZIP_NAME}"
    )
