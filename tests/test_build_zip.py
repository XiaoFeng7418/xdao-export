"""Tests for packaging metadata, checksums, BOM handling, archive members, and docs checks.

These tests cover deterministic helpers without running PyInstaller. A separate test verifies
that the current release notes, packaged manual, and package version agree before packaging.
The hosted Windows workflow performs the full build and smoke check.
"""

from __future__ import annotations

import hashlib
import json
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import build_zip  # noqa: E402


def _license_environment(tmp_path: Path) -> dict:
    python_license = tmp_path / "Python-LICENSE.txt"
    python_license.write_bytes(b"PSF original\r\nApache License\r\nEND\r\n")
    installer_license = tmp_path / "COPYING.txt"
    installer_license.write_bytes(b"GPL original\nBootloader Exception\nEND\n")
    return {
        "python": "3.12.9", "openssl": "OpenSSL 3.0.15 3 Sep 2024",
        "pyinstaller": "6.22.2", "python_license": str(python_license),
        "pyinstaller_license": str(installer_license),
    }


def test_license_probe_uses_the_selected_build_interpreter(monkeypatch) -> None:
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, json.dumps({"python": "selected"}), "")

    import subprocess

    monkeypatch.setattr(build_zip.subprocess, "run", run)
    assert build_zip.license_environment("build-python.exe") == {"python": "selected"}
    assert calls[0][:2] == ["build-python.exe", "-c"]


def test_third_party_originals_and_inventory_reach_the_zip(tmp_path: Path) -> None:
    environment = _license_environment(tmp_path)
    stage = tmp_path / "package"
    tk = stage / "_internal" / "_tk_data" / "license.terms"
    tk.parent.mkdir(parents=True)
    tk.write_bytes(b"Tcl/Tk original\r\nEND\r\n")
    build_zip.copy_third_party_licenses(stage, environment)
    archive_path = tmp_path / "package.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        for path, name in build_zip.zip_members(tmp_path, stage):
            archive.write(path, name)
    with zipfile.ZipFile(archive_path) as archive:
        prefix = "package/THIRD_PARTY_LICENSES/"
        for key, name in (("python_license", "Python-LICENSE.txt"),
                          ("pyinstaller_license", "PyInstaller-COPYING.txt")):
            original = Path(environment[key]).read_bytes()
            assert archive.read(prefix + name) == original
            assert hashlib.sha256(original).hexdigest() in archive.read(prefix + "README.txt").decode()
        assert archive.read("package/_internal/_tk_data/license.terms") == tk.read_bytes()
        for name in ("LICENSE.txt", "README.md"):
            original = ROOT / "packaging/licenses/openssl-3.0.15" / name
            assert archive.read(prefix + "OpenSSL-" + name) == original.read_bytes()


def test_missing_openssl_original_is_explicit_in_the_package(tmp_path: Path) -> None:
    environment = _license_environment(tmp_path)
    environment["openssl"] = "OpenSSL 99.99.99 test"
    stage = tmp_path / "package"
    build_zip.copy_third_party_licenses(stage, environment)
    inventory = (stage / "THIRD_PARTY_LICENSES/README.txt").read_text(encoding="utf-8")
    assert "MISSING" in inventory and "OpenSSL 99.99.99" in inventory
    assert not (stage / "THIRD_PARTY_LICENSES/OpenSSL-LICENSE.txt").exists()


@pytest.mark.parametrize("missing", ["python_license", "pyinstaller_license"])
def test_missing_required_license_stops_packaging(tmp_path: Path, missing: str) -> None:
    environment = _license_environment(tmp_path)
    environment[missing] = ""
    with pytest.raises(SystemExit, match="license"):
        build_zip.copy_third_party_licenses(tmp_path / "package", environment)


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
    """The executable metadata includes the project publisher and license notice."""
    text = build_zip.version_file_text("0.13.29")

    assert "StringStruct('CompanyName', 'XiaoFeng7418')" in text
    assert "StringStruct('ProductName', 'X岛串导出工具')" in text
    assert "StringStruct('FileVersion', '0.13.29')" in text
    assert "StringStruct('ProductVersion', '0.13.29')" in text
    assert "filevers=(0, 13, 29, 0)," in text
    assert "prodvers=(0, 13, 29, 0)," in text
    assert "LegalCopyright', 'XiaoFeng7418 · MIT 许可'" in text


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
    assert build_zip.has_bom(double) is False, "A file must not start with repeated BOM markers."


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
    """包内那几个文件（使用说明 + 两个诊断脚本 + LICENSE）在仓库里得真的在。"""
    for source, _ in build_zip.EXTRA_FILES:
        assert (ROOT / source).is_file(), f"{source} 不在仓库里，打包时会 FileNotFoundError"


def test_bom_list_only_covers_files_that_go_into_the_zip() -> None:
    names = {name for _, name in build_zip.EXTRA_FILES}
    assert set(build_zip.NEEDS_BOM) <= names, "BOM 自检里出现了不进包的文件名，检查写错了"


# ---------------------------------------------------------------------------
# 摆给下载者的那一份
# ---------------------------------------------------------------------------


def test_release_dir_gets_the_three_pieces_and_replaces_the_old_tree(tmp_path: Path) -> None:
    """`--release-dir` copies the archive set and replaces an existing version folder."""
    out = tmp_path / "dist"
    root = out / "xdao-export-v9.9.9"
    stage = root / "xdao-export-v9.9.9-win64"
    stage.mkdir(parents=True)
    (stage / "xdao-export.exe").write_bytes(b"exe")
    zip_path = out / "xdao-export-v9.9.9-win64.zip"
    zip_path.write_bytes(b"zip")
    sidecar = Path(str(zip_path) + build_zip.SIDECAR_SUFFIX)
    sidecar.write_text(f"{'a' * 64}  {zip_path.name}\n", encoding="utf-8")

    release = tmp_path / "release"
    stale = release / "xdao-export-v9.9.9" / "上一版留下的.txt"
    stale.parent.mkdir(parents=True)
    stale.write_text("旧文件", encoding="utf-8")

    placed = build_zip.publish_release(root, zip_path, sidecar, release)

    assert [path.name for path in placed] == [
        "xdao-export-v9.9.9",
        zip_path.name,
        sidecar.name,
    ]
    assert (release / root.name / stage.name / "xdao-export.exe").is_file()
    assert not stale.exists(), "同名旧目录没先删掉，新旧文件会混在一起"
    assert (release / zip_path.name).read_bytes() == b"zip"
    assert (release / sidecar.name).is_file()
    # 产物目录就是它自己时不用再摆，也不该报错
    assert build_zip.publish_release(root, zip_path, sidecar, out) == []


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


# ---------------------------------------------------------------------------
# 排障开关 --pdfdiag：包里得真打进去（v0.19.4 起）
# ---------------------------------------------------------------------------


def test_the_pyinstaller_command_bundles_the_pdf_diagnose_module(tmp_path: Path) -> None:
    """``--pdfdiag`` 在 main.py 里是延迟导入，PyInstaller 扫不到，必须在命令行点名。

    2026-10-25 实测：v0.19.3 的免安装包里没有 ``tools`` 这个模块，用户敲
    ``xdao-export.exe --pdfdiag`` 只会拿到 ModuleNotFoundError —— 开关等于不存在。
    """
    command = build_zip.pyinstaller_command("python.exe", tmp_path / "v.txt", tmp_path, tmp_path)

    assert "--hidden-import" in command
    assert command[command.index("--hidden-import") + 1] == "tools.pdf_diag"
    assert (ROOT / "tools" / "__init__.py").is_file()


def test_the_command_line_still_has_the_hidden_pdf_diagnose_switch() -> None:
    """开关与打包要成对：开关还在，那个模块就必须跟着打进去。"""
    import argparse

    from main import build_parser

    parser = build_parser()
    action = next(item for item in parser._actions if "--pdfdiag" in item.option_strings)
    assert action.help == argparse.SUPPRESS


# ---------------------------------------------------------------------------
# 版本号判定（CI 上抓不到 --windowed 程序的 stdout，见 v0.22.3 的 build job）


def test_version_check_passes_when_the_output_names_the_version() -> None:
    ok, note = build_zip.version_check("0.22.3", "X岛串导出工具 0.22.3\n", None)
    assert ok is True
    assert note == ""


def test_version_check_falls_back_to_the_pe_version_when_the_output_is_empty() -> None:
    # CI 上就是这个样子：退出码 0，但抓不到任何输出。
    ok, note = build_zip.version_check("0.22.3", "", "0.22.3")
    assert ok is True
    assert "ProductVersion" in note


def test_version_check_rejects_a_pe_version_that_does_not_match() -> None:
    ok, note = build_zip.version_check("0.22.3", "", "0.22.2")
    assert ok is False
    assert "0.22.2" in note
    assert "0.22.3" in note


def test_version_check_rejects_a_build_without_any_version_at_all() -> None:
    ok, note = build_zip.version_check("0.22.3", "", None)
    assert ok is False
    assert "None" in note


def test_version_check_accepts_the_output_even_when_the_pe_version_is_missing() -> None:
    ok, note = build_zip.version_check("0.22.3", "X岛串导出工具 0.22.3 ", None)
    assert ok is True
    assert note == ""


def test_pe_product_version_reads_the_exe_of_this_repository_when_there_is_one() -> None:
    # A clean checkout may not contain packaged artifacts. Inspect only the current
    # version directory because dist/ can also contain older builds.
    version = build_zip.read_version(ROOT)
    exe = next(
        iter(sorted((ROOT / "dist" / f"xdao-export-v{version}").glob("**/xdao-export.exe"))),
        None,
    )
    if exe is None:
        pytest.skip(f"No packaged artifact for v{version} is available to inspect.")
    found = build_zip.pe_product_version(exe)
    assert found is not None
    assert found.startswith(version)
