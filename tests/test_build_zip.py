"""`tools/build_zip.py` 的用例：打包这一步不许「打出来才发现文档没跟上」。

这个工具是从作者本机的 `_scratch/` 脚本搬进仓库的，理由有两条（2026-10-02）：

- 别人想核对 Release 里那个包是怎么来的，以前只能来问；放进仓库后谁都能复现；
- 申请免费代码签名（SignPath Foundation）要求**要签名的产物必须由 CI 构建**，
  所以这一步必须能在 GitHub 托管的 Windows runner 上跑通（见 `.github/workflows/build.yml`）。

下面只测纯函数（版本信息、校验文件、BOM、条目清单、文档自检）—— 真的跑一遍 PyInstaller
要好几分钟，那是 CI 的活，不是单元用例的活。最后一条盯着**本仓库当前这一版**：
`docs/RELEASE_NOTES_vX.md`、`packaging/使用说明.txt`、`xdao/__init__.py` 三处必须已经对齐，
否则发版时才会发现，而那时包已经打出来了。
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import build_zip  # noqa: E402


# ---------------------------------------------------------------------------
# PE 版本信息
# ---------------------------------------------------------------------------


def test_version_quad_pads_to_four_numbers() -> None:
    assert build_zip.version_quad("0.13.29") == (0, 13, 29, 0)
    assert build_zip.version_quad("1.0.0") == (1, 0, 0, 0)
    assert build_zip.version_quad("12.34.56") == (12, 34, 56, 0)


@pytest.mark.parametrize("bad", ["0.13", "0.13.29.1", "0.13.x", "", "v0.13.29"])
def test_version_quad_rejects_anything_that_is_not_three_numbers(bad: str) -> None:
    with pytest.raises(ValueError):
        build_zip.version_quad(bad)


def test_version_file_names_the_author_and_the_product() -> None:
    """证书拿不到「晓风」这个名字，属性里的公司名就是署名 —— 别让它空着。"""
    text = build_zip.version_file_text("0.13.29")

    assert "StringStruct('CompanyName', '晓风')" in text
    assert "StringStruct('ProductName', 'X岛串导出工具')" in text
    assert "StringStruct('FileVersion', '0.13.29')" in text
    assert "StringStruct('ProductVersion', '0.13.29')" in text
    assert "filevers=(0, 13, 29, 0)," in text
    assert "prodvers=(0, 13, 29, 0)," in text
    assert "LegalCopyright', '晓风 · MIT 许可'" in text


def test_version_file_can_be_built_from_an_arbitrary_version() -> None:
    """换版本只该换数字，别把结构写死。"""
    text = build_zip.version_file_text("1.2.3")

    assert "filevers=(1, 2, 3, 0)," in text
    assert "0.13.29" not in text


# ---------------------------------------------------------------------------
# .sha256 校验文件
# ---------------------------------------------------------------------------


def test_sidecar_line_matches_what_sha256_tools_expect() -> None:
    """两个空格 + 文件名 + 换行：`sha256sum -c` 与 `Get-FileHash` 的对照写法都认这个。"""
    line = build_zip.sidecar_text("a" * 64, "xdao-export-v0.13.29-win64.zip")

    assert line == f"{'a' * 64}  xdao-export-v0.13.29-win64.zip\n"


def test_sha256_file_matches_hashlib(tmp_path: Path) -> None:
    path = tmp_path / "blob.bin"
    payload = b"xdao" * 5000
    path.write_bytes(payload)

    assert build_zip.sha256_file(path) == hashlib.sha256(payload).hexdigest()


def test_has_bom_accepts_exactly_one_bom(tmp_path: Path) -> None:
    good = tmp_path / "good.txt"
    good.write_bytes(b"\xef\xbb\xbf" + "中文内容\n".encode("utf-8"))
    bare = tmp_path / "bare.txt"
    bare.write_bytes("中文内容\n".encode("utf-8"))
    double = tmp_path / "double.txt"
    double.write_bytes(b"\xef\xbb\xbf\xef\xbb\xbf" + "中文内容\n".encode("utf-8"))

    assert build_zip.has_bom(good) is True
    assert build_zip.has_bom(bare) is False
    assert build_zip.has_bom(double) is False, "两个 BOM 也是错的（2026-10-02 踩过一次）"


# ---------------------------------------------------------------------------
# zip 里的名字
# ---------------------------------------------------------------------------


def test_zip_members_are_sorted_and_keep_the_top_folder(tmp_path: Path) -> None:
    root = tmp_path / "xdao-export-v9.9.9"
    stage = root / "xdao-export-v9.9.9-win64"
    (stage / "_internal").mkdir(parents=True)
    (stage / "_internal" / "python312.dll").write_bytes(b"dll")
    (stage / "xdao-export.exe").write_bytes(b"exe")
    (stage / "使用说明.txt").write_bytes(b"\xef\xbb\xbf" + "说明\n".encode("utf-8"))
    (root / "不该进包.txt").write_bytes(b"nope")

    members = list(build_zip.zip_members(root, stage))

    assert [name for _, name in members] == [
        "xdao-export-v9.9.9-win64/_internal/python312.dll",
        "xdao-export-v9.9.9-win64/xdao-export.exe",
        "xdao-export-v9.9.9-win64/使用说明.txt",
    ]
    assert all(path.is_file() for path, _ in members)


def test_every_extra_file_exists_in_the_repo() -> None:
    """包内那三个文件（使用说明 + 两个诊断脚本）在仓库里得真的在。"""
    for source, _ in build_zip.EXTRA_FILES:
        assert (ROOT / source).is_file(), f"{source} 不在仓库里，打包时会 FileNotFoundError"


def test_bom_list_only_covers_files_that_go_into_the_zip() -> None:
    names = {name for _, name in build_zip.EXTRA_FILES}
    assert set(build_zip.NEEDS_BOM) <= names, "BOM 自检里出现了不进包的文件名，检查写错了"


# ---------------------------------------------------------------------------
# 文档自检
# ---------------------------------------------------------------------------


def _fake_repo(tmp_path: Path, version: str = "9.9.9", *, docs: bool = True,
               manual_has_version: bool = True, init_has_version: bool = True) -> Path:
    repo = tmp_path / "repo"
    (repo / "xdao").mkdir(parents=True)
    (repo / "docs").mkdir()
    (repo / "packaging").mkdir()
    body = f'__version__ = "{version if init_has_version else "0.0.0"}"\n'
    (repo / "xdao" / "__init__.py").write_text(body, encoding="utf-8")
    if docs:
        (repo / "docs" / f"RELEASE_NOTES_v{version}.md").write_text(
            f"# v{version}：测试\n", encoding="utf-8"
        )
    manual = f"X岛串导出工具 v{version if manual_has_version else '0.0.0'}（免安装版）\n"
    (repo / "packaging" / "使用说明.txt").write_text(manual, encoding="utf-8-sig")
    return repo


def test_check_docs_lists_every_thing_that_is_behind(tmp_path: Path) -> None:
    """三处都没跟上：发布说明没写、使用说明还写着旧版本、代码里也不是这个版本。"""
    repo = _fake_repo(tmp_path, docs=False, manual_has_version=False, init_has_version=False)

    problems = build_zip.check_docs(repo, "9.9.9")

    assert len(problems) == 3
    assert any("RELEASE_NOTES_v9.9.9.md" in text for text in problems)
    assert any("使用说明.txt" in text for text in problems)
    assert any("__init__.py" in text for text in problems)


def test_check_docs_is_quiet_when_everything_is_in_step(tmp_path: Path) -> None:
    assert build_zip.check_docs(_fake_repo(tmp_path), "9.9.9") == []


@pytest.mark.parametrize(
    "flags",
    [
        {"docs": False},
        {"manual_has_version": False},
        {"init_has_version": False},
    ],
)
def test_check_docs_catches_each_mismatch_on_its_own(tmp_path: Path, flags: dict) -> None:
    repo = _fake_repo(tmp_path, **flags)
    problems = build_zip.check_docs(repo, "9.9.9")

    assert len(problems) == 1, problems


def test_read_version_reads_the_package_version(tmp_path: Path) -> None:
    repo = _fake_repo(tmp_path)
    assert build_zip.read_version(repo) == "9.9.9"


def test_read_version_complains_when_there_is_none(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "xdao").mkdir(parents=True)
    (repo / "xdao" / "__init__.py").write_text('"""没有版本号。"""\n', encoding="utf-8")

    with pytest.raises(SystemExit):
        build_zip.read_version(repo)


def test_this_repository_is_ready_to_pack_its_current_version() -> None:
    """本仓库现在就打得出包：版本号、发布说明、使用说明三处得对齐。

    发版顺序是「先改文档再打包」，所以这条一旦红，就是文档还没补完 —— 别硬打。
    """
    version = build_zip.read_version(ROOT)
    assert build_zip.check_docs(ROOT, version) == []
