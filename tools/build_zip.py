"""Build a portable ``xdao-export-v<version>-win64.zip`` and its ``.sha256`` file.

The script is versioned with the project so release archives can be reproduced from source.
The Windows build workflow runs it on a hosted runner; this also supports the planned
code-signing workflow, which requires CI-built artifacts.

Usage::

    python tools/build_zip.py
    python tools/build_zip.py --out D:\\out
    python tools/build_zip.py --release-dir D:\\release

The script reports the archive path, entry count, size, and SHA256 digest. The sidecar file
contains ``<digest>  <filename>`` in the format accepted by common checksum utilities.

By default, artifacts are written to ``./dist``. ``--release-dir`` also copies the version
folder, archive, and sidecar to a download directory, replacing a same-named version folder.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# 产品名、启动器名、包名与版本号读法的真源都在 xdao/appinfo.py —— 更新检查与升级器
# 认的是同一张表，这里不再各拼一遍。脚本要能直接 `python tools/build_zip.py` 跑，
# 所以先把仓库根挂进 sys.path 再 import（和 tools/repo_info.py 一个路子）。
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from xdao.appinfo import (  # noqa: E402  （必须在上面那段 sys.path 之后）
    LAUNCHER_NAME,
    PRODUCT_NAME,
    SIDECAR_SUFFIX,
    asset_name,
    payload_dir_name,
    payload_root_name,
    read_version,
)

#: 打进包里的几个仓库文件（源文件 -> 包内文件名）。使用说明要给用户看，
#: 两个 .ps1/.cmd 是「把抓取与导出的每一步写进日志」的诊断脚本，出问题时让用户跑它，
#: LICENSE 是 MIT 协议全文 —— MIT 要求「版权声明与许可声明」随副本一起走，
#: 所以它必须跟着 exe 一起发出去（使用说明里也点明了包内有这一份）。
EXTRA_FILES: tuple[tuple[str, str], ...] = (
    ("packaging/使用说明.txt", "使用说明.txt"),
    ("诊断写入.ps1", "诊断写入.ps1"),
    ("诊断写入-双击运行.cmd", "诊断写入-双击运行.cmd"),
    ("LICENSE", "LICENSE.txt"),
)

#: Windows tools use these UTF-8 files; the BOM check also runs in
#: ``tests/test_text_hygiene.py`` and is repeated before archiving.
NEEDS_BOM = ("使用说明.txt", "诊断写入.ps1", "诊断写入-双击运行.cmd")

AUTHOR = "XiaoFeng7418"
PRODUCT = PRODUCT_NAME


def version_quad(version: str) -> tuple[int, int, int, int]:
    """``"0.13.29"`` -> ``(0, 13, 29, 0)``（PE 的 FixedFileInfo 要四段）。"""
    parts = version.split(".")
    if len(parts) != 3 or not all(part.isdigit() for part in parts):
        raise ValueError(f"版本号得是 x.y.z 三段数字，现在是 {version!r}")
    return (int(parts[0]), int(parts[1]), int(parts[2]), 0)


def version_file_text(version: str, author: str = AUTHOR, product: str = PRODUCT) -> str:
    """PyInstaller `--version-file` 要的 PE 版本信息。

    ``CompanyName`` and ``LegalCopyright`` carry the project publisher identifier and
    MIT notice in the executable's version resources.
    """
    quad = version_quad(version)
    return (
        "VSVersionInfo(\n"
        "  ffi=FixedFileInfo(\n"
        f"    filevers={quad},\n"
        f"    prodvers={quad},\n"
        "    mask=0x3f,\n    flags=0x0,\n    OS=0x40004,\n    fileType=0x1,\n    subtype=0x0,\n"
        "    date=(0, 0)\n    ),\n"
        "  kids=[\n"
        "    StringFileInfo([\n"
        "      StringTable(\n"
        "        '080404B0',\n"
        f"        [StringStruct('CompanyName', '{author}'),\n"
        f"         StringStruct('FileDescription', '{product}'),\n"
        f"         StringStruct('FileVersion', '{version}'),\n"
        "         StringStruct('InternalName', 'xdao-export'),\n"
        f"         StringStruct('LegalCopyright', '{author} · MIT 许可'),\n"
        f"         StringStruct('OriginalFilename', '{LAUNCHER_NAME}'),\n"
        f"         StringStruct('ProductName', '{product}'),\n"
        f"         StringStruct('ProductVersion', '{version}')]\n"
        "      )]),\n"
        "    VarFileInfo([VarStruct('Translation', [2052, 1200])])\n"
        "  ]\n"
        ")\n"
    )


def sidecar_text(digest: str, zip_name: str) -> str:
    """`.sha256` 的内容：一行 ``<摘要>  <文件名>``（两个空格是这类工具的约定）。"""
    return f"{digest}  {zip_name}\n"


def sha256_file(path: Path) -> str:
    """算文件摘要。分块读 —— 包有十几兆，别整份塞进内存。"""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def has_bom(path: Path) -> bool:
    """开头是否正好一个 UTF-8 BOM（多贴一个也是错的）。"""
    data = path.read_bytes()
    return data[:3] == b"\xef\xbb\xbf" and data[3:6] != b"\xef\xbb\xbf"


def license_environment(python: str) -> dict:
    """向实际执行 PyInstaller 的解释器查询版本和许可来源（不能用宿主解释器代替）。"""
    script = """
import importlib.metadata, json, ssl, sys
from pathlib import Path
dist = importlib.metadata.distribution('pyinstaller')
copying = next((dist.locate_file(p) for p in dist.files or [] if p.name == 'COPYING.txt'), None)
license = next((p for p in (Path(sys.base_prefix) / 'LICENSE.txt',
                           Path(sys.base_prefix) / 'LICENSE') if p.is_file()), None)
print(json.dumps({'python': sys.version.split()[0], 'openssl': ssl.OPENSSL_VERSION,
                  'pyinstaller': dist.version, 'python_license': str(license or ''),
                  'pyinstaller_license': str(copying or '')}))
"""
    done = subprocess.run(
        [python, "-c", script], capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=30,
    )
    if done.returncode:
        raise SystemExit(f"Cannot locate build environment licenses: {done.stderr.strip()}")
    return json.loads(done.stdout)


def copy_third_party_licenses(stage: Path, environment: dict) -> list[str]:
    """原样复制完整许可，并写入版本、来源与摘要；缺失项如实列在索引中。"""
    directory = stage / "THIRD_PARTY_LICENSES"
    directory.mkdir(parents=True, exist_ok=True)
    lines = [
        "Third-party license inventory", "",
        f"Python {environment['python']}; PyInstaller {environment['pyinstaller']}",
        environment["openssl"], "",
    ]
    missing: list[str] = []

    def copy(source: Path, name: str, origin: str) -> None:
        data = source.read_bytes()
        (directory / name).write_bytes(data)
        lines.append(f"{name}: {origin}; SHA256 {hashlib.sha256(data).hexdigest()}")

    for key, name, origin in (
        ("python_license", "Python-LICENSE.txt", "build Python/LICENSE.txt (PSF and bundled notices)"),
        ("pyinstaller_license", "PyInstaller-COPYING.txt", "build PyInstaller distribution/COPYING.txt (GPL and bootloader exception)"),
    ):
        path = Path(environment[key])
        if not path.is_file() or not path.stat().st_size:
            raise SystemExit(f"Required license missing: {key}")
        copy(path, name, origin)

    # 官方 Python 的组合许可也保留 Apache 条款；另附实际 OpenSSL 版本的完整上游
    # LICENSE 和含版权行的 README。只匹配确切版本，不将旧版原文冒充新版许可。
    match = re.match(r"OpenSSL (\d+\.\d+\.\d+[a-z]*)(?:\s|$)", environment["openssl"])
    originals = ROOT / "packaging" / "licenses" / f"openssl-{match[1] if match else 'unknown'}"
    source_index = originals / "sources.json"
    if source_index.is_file():
        sources = json.loads(source_index.read_text(encoding="utf-8"))
        for name in ("LICENSE.txt", "README.md"):
            source = originals / name
            if sha256_file(source) != sources[name]["sha256"]:
                raise SystemExit(f"Upstream license checksum mismatch: {source.name}")
            copy(source, "OpenSSL-" + name, sources[name]["url"])
    else:
        missing.append(f"MISSING: standalone upstream license/notice for {environment['openssl']}")

    for name in ("_tcl_data", "_tk_data"):
        relative = f"_internal/{name}/license.terms"
        path = stage / relative
        if path.is_file():
            lines.append(f"Preserved {relative}; SHA256 {sha256_file(path)}")
    lines.extend(["", "Tcl/Tk notices are also retained in the complete Python-LICENSE.txt.",
                  "Files above are copied byte for byte; no upstream license text is abridged."])
    lines.extend(missing)
    (directory / "README.txt").write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    return missing


def zip_members(root: Path, stage: Path):
    """``stage`` 下所有文件，配它在 zip 里的名字（相对 ``root``，用 ``/`` 分隔）。

    按**包内名字**排序，不按 Path 排 —— Windows 上 `Path` 的比较是大小写折叠过的，
    中英文混排时顺序会很奇怪（2026-10-02：`xdao-export.exe` 排到了 `使用说明.txt` 前面）。
    按名字排还有个好处：同一份源码两次打包的条目顺序一致，能直接对 diff。
    """
    entries = [
        (path, path.relative_to(root).as_posix())
        for path in stage.rglob("*")
        if path.is_file()
    ]
    for path, name in sorted(entries, key=lambda item: item[1]):
        yield path, name


def check_docs(repo: Path, version: str) -> list[str]:
    """打包前先把「文档跟上了没有」问一遍，返回问题清单（空就是没问题）。"""
    problems: list[str] = []
    notes = repo / "docs" / f"RELEASE_NOTES_v{version}.md"
    if not notes.exists():
        problems.append(f"缺少 {notes.relative_to(repo).as_posix()}（发布说明要先写）")
    elif version not in notes.read_text(encoding="utf-8-sig"):
        problems.append(f"{notes.name} 里没提到版本号 {version}")
    manual = repo / "packaging" / "使用说明.txt"
    if not manual.exists():
        problems.append("缺少 packaging/使用说明.txt")
    elif version not in manual.read_text(encoding="utf-8-sig"):
        problems.append(f"packaging/使用说明.txt 里没提到版本号 {version}")
    init = repo / "xdao" / "__init__.py"
    if f'__version__ = "{version}"' not in init.read_text(encoding="utf-8"):
        problems.append(f"xdao/__init__.py 里的 __version__ 不是 {version}")
    return problems


def pe_product_version(path: Path) -> str | None:
    """读 Windows PE 版本资源里的 ``ProductVersion``；读不到返回 None。

    A PyInstaller ``--windowed`` executable has no console in some CI environments.
    The PE version resource provides a fallback when stdout is unavailable.
    """
    if os.name != "nt":  # pragma: no cover - 只在 Windows 上有意义
        return None
    try:
        import ctypes
        from ctypes import wintypes

        api = ctypes.windll.version
        size = api.GetFileVersionInfoSizeW(str(path), None)
        if not size:
            return None
        buffer = ctypes.create_string_buffer(size)
        if not api.GetFileVersionInfoW(str(path), 0, size, buffer):
            return None
        value = ctypes.c_void_p()
        length = wintypes.UINT()
        # Translation 里的每一项是「langid（小端 WORD）+ 代码页（小端 WORD）」，
        # 例如 04 08 b0 04 表示 0x0804 / 0x04B0，块名要写成 080404B0。
        chips = [(0x0804, 0x04B0)]
        if api.VerQueryValueW(
            buffer, "\\VarFileInfo\\Translation",
            ctypes.byref(value), ctypes.byref(length),
        ) and value and length.value:
            table = ctypes.string_at(value, length.value)
            found = [
                (
                    int.from_bytes(table[i:i + 2], "little"),
                    int.from_bytes(table[i + 2:i + 4], "little"),
                )
                for i in range(0, len(table) - 3, 4)
            ]
            if found:
                chips = found
        for langid, codepage in chips:
            block = (
                f"\\StringFileInfo\\{langid:04X}{codepage:04X}\\ProductVersion"
            )
            ok = api.VerQueryValueW(
                buffer, block, ctypes.byref(value), ctypes.byref(length)
            )
            if ok and value and length.value:
                return ctypes.wstring_at(value, length.value).split("\x00")[0].strip()
    except Exception:  # pragma: no cover - 读 PE 失败不该拦住打包
        return None
    return None


def version_check(
    version: str, output: str, pe_version: str | None
) -> tuple[bool, str]:
    """判断包里程序的版本号对不对，返回 ``(是否通过, 一行说明)``。

    ``output`` 是 ``--version`` 抓到的输出，``pe_version`` 是 exe 里 PE 版本资源的
    ``ProductVersion``（抓不到就是 None）。有些环境（CI 的 runner）抓不到
    ``--windowed`` 程序的 stdout，那时只能按 PE 版本判。
    """
    if version in output:
        return True, ""
    if pe_version and pe_version.startswith(version):
        return True, f"抓不到 --version 的输出，改按 exe 的 ProductVersion 判：{pe_version}"
    return False, f"--version 没报出 {version}，exe 的 ProductVersion 是 {pe_version}"


def kill_leftover_exe() -> None:
    """清掉还在跑的打包版进程。

    An earlier packaged process can keep DLL files in ``_internal`` open and prevent
    the build directory from being removed.
    """
    if os.name != "nt":
        return
    subprocess.run(
        ["taskkill", "/F", "/IM", LAUNCHER_NAME],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )


def pyinstaller_command(python: str, version_file: Path, work: Path, out: Path) -> list[str]:
    """拼出这次打包要跑的 PyInstaller 命令行（纯函数，方便用例断言）。

    ``--pdfdiag`` is imported lazily by ``main.py`` and is not detected by PyInstaller's
    static analysis. Explicitly listing ``tools.pdf_diag`` includes it in the package.
    """
    return [
        python, "-m", "PyInstaller",
        "--onedir", "--windowed", "--clean", "--noconfirm",
        "--version-file", str(version_file),
        # 延迟导入的排障模块：不点名就不会被打进包
        "--hidden-import", "tools.pdf_diag",
        # spec 与中间产物都放到临时目录：仓库里的 *.spec 是历史文件，别让这次打包改到它
        "--specpath", str(work),
        "--workpath", str(work / "build"),
        "--distpath", str(out),
        "--name", "xdao-export",
        "main.py",
    ]


def build_pyinstaller(repo: Path, python: str, version_file: Path, work: Path, out: Path) -> Path:
    """跑 PyInstaller，返回它产出的程序目录（`<out>/xdao-export`）。"""
    command = pyinstaller_command(python, version_file, work, out)
    done = subprocess.run(
        command, cwd=repo, capture_output=True, text=True, encoding="utf-8", errors="replace"
    )
    if done.returncode != 0:
        print((done.stdout or "")[-3000:])
        print((done.stderr or "")[-2000:], file=sys.stderr)
        raise SystemExit(f"PyInstaller 失败（退出码 {done.returncode}）")
    return out / "xdao-export"


def publish_release(root: Path, zip_path: Path, sidecar: Path, release_dir: Path) -> list[Path]:
    """把这次的三件套再摆一份到 ``release_dir``：解开的版本目录 + zip + `.sha256`。

    `root` 是这次解开的目录（`xdao-export-v<版本>`，里面一层 `…-win64`），
    `zip_path` / `sidecar` 是打包目录里那两份文件。同名旧目录先删掉再摆，免得新旧
    文件混在一起；返回真正摆过去的那几样（`release_dir` 就是打包目录时返回空）。
    """
    release_dir = Path(release_dir).resolve()
    placed: list[Path] = []
    target_root = release_dir / root.name
    if target_root.resolve() == Path(root).resolve():
        return placed
    release_dir.mkdir(parents=True, exist_ok=True)
    if target_root.exists():
        shutil.rmtree(target_root)
    shutil.copytree(root, target_root)
    placed.append(target_root)
    for source in (zip_path, sidecar):
        target = release_dir / source.name
        if target.resolve() != Path(source).resolve():
            shutil.copy2(source, target)
            placed.append(target)
    return placed


def main(argv: list[str] | None = None) -> int:
    # Windows consoles may use a legacy code page that cannot encode the status symbols.
    # Configure UTF-8 output so build diagnostics remain printable.
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if stream is None:  # --windowed 打包版没有控制台
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001 —— 编码修不好也不该挡住打包
            pass
    parser = argparse.ArgumentParser(description="打免安装包并生成 .sha256")
    parser.add_argument("--out", default="dist", help="产物目录（默认 ./dist）")
    parser.add_argument(
        "--release-dir",
        default="",
        help="再摆一份到哪个目录（解开的版本目录 + zip + .sha256；省了就不摆）",
    )
    parser.add_argument("--python", default=sys.executable, help="用哪个解释器跑 PyInstaller")
    args = parser.parse_args(argv)

    repo = ROOT
    out = Path(args.out).resolve()
    version = read_version(repo)
    print(f"版本 {version}，产物目录 {out}", flush=True)

    problems = check_docs(repo, version)
    for problem in problems:
        print(f"  × {problem}", flush=True)
    if problems:
        return 1

    out.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="xdao-build-"))
    try:
        version_file = work / "version_info.txt"
        version_file.write_text(version_file_text(version), encoding="utf-8", newline="\n")
        print(f"PE 版本信息：CompanyName={AUTHOR} / ProductVersion={version}", flush=True)

        print("PyInstaller 打包中…", flush=True)
        dist = build_pyinstaller(repo, args.python, version_file, work, out / "pyinstaller")

        root = out / payload_root_name(version)
        stage = root / payload_dir_name(version)
        kill_leftover_exe()
        if root.exists():
            shutil.rmtree(root)
        stage.mkdir(parents=True)
        shutil.copytree(dist / "_internal", stage / "_internal")
        shutil.copy2(dist / LAUNCHER_NAME, stage / LAUNCHER_NAME)
        for source, name in EXTRA_FILES:
            shutil.copy2(repo / source, stage / name)

        missing_licenses = copy_third_party_licenses(stage, license_environment(args.python))
        print("第三方许可：THIRD_PARTY_LICENSES/README.txt（版本、来源和 SHA256）", flush=True)
        for note in missing_licenses:
            print(f"  ! {note}", flush=True)

        print("包内文件自检：", flush=True)
        for name in NEEDS_BOM:
            path = stage / name
            if not path.exists():
                print(f"  × {name} 没进包", flush=True)
                return 1
            if not has_bom(path):
                print(f"  × {name} 缺少（或多了）UTF-8 BOM，中文会乱码", flush=True)
                return 1
            print(f"  ✓ {name}：{path.stat().st_size} 字节，BOM 正常", flush=True)

        zip_path = out / asset_name(version)
        if zip_path.exists():
            zip_path.unlink()
        count = 0
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            for path, name in zip_members(root, stage):
                archive.write(path, name)
                count += 1

        digest = sha256_file(zip_path)
        sidecar = Path(str(zip_path) + SIDECAR_SUFFIX)
        sidecar.write_text(sidecar_text(digest, zip_path.name), encoding="utf-8", newline="\n")

        print(f"zip：{zip_path}", flush=True)
        print(f"  {count} 个条目 / {zip_path.stat().st_size} 字节", flush=True)
        print(f"  SHA256 {digest}", flush=True)
        print(f"校验文件：{sidecar.name}（{sidecar.stat().st_size} 字节）", flush=True)

        exe = stage / LAUNCHER_NAME
        if os.name == "nt":
            # --version 必须报出这次的版本号：打错了版本（比如忘了改 xdao/__init__.py）
            # 以前只是把它打印出来，谁也没看；现在它直接判失败。
            done = subprocess.run(
                [str(exe), "--version"], capture_output=True, text=True,
                encoding="utf-8", errors="replace",
            )
            text = (done.stdout or "") + (done.stderr or "")
            tail = " | ".join(text.strip().splitlines()[-2:])
            print(f"  --version → 退出码 {done.returncode}：{tail}", flush=True)
            if done.returncode != 0:
                print("  × 包里的程序起不来（--version 退出码不是 0）", flush=True)
                return 1
            # 抓不到 stdout 时（--windowed 的程序在 CI 上没有控制台）用 PE 版本
            # 资源兜底：ProductVersion 同样是 exe 里真实带着的版本号。
            ok, note = version_check(version, text, pe_product_version(exe))
            if ok:
                if note:
                    print(f"  （{note}）", flush=True)
            else:
                print(f"  × {note}", flush=True)
                return 1
            # The packaging smoke check is offline. Browser and security-software
            # environment checks are reported but do not fail the build.
            done = subprocess.run(
                [str(exe), "--selftest", "--offline"], capture_output=True, text=True,
                encoding="utf-8", errors="replace",
            )
            tail = " | ".join((done.stdout or done.stderr or "").strip().splitlines()[-2:])
            print(f"  --selftest --offline → 退出码 {done.returncode}：{tail}", flush=True)

        if args.release_dir:
            release_dir = Path(args.release_dir).resolve()
            placed = publish_release(root, zip_path, sidecar, release_dir)
            if placed:
                print(f"另外摆了一份到 {release_dir}：", flush=True)
                for path in placed:
                    print(f"  {path.name}", flush=True)
            else:
                print("产物目录就是 --release-dir，不用再摆一份。", flush=True)
        elif out == (ROOT / "dist").resolve() and not os.environ.get("GITHUB_ACTIONS"):
            # Hosted builds use ./dist; local builds may copy release artifacts elsewhere.
            print("提醒：这次产物留在仓库的 ./dist 里。", flush=True)
            print("      要让下载者在外层目录看到「版本目录 + zip + .sha256」，", flush=True)
            print("      Add --release-dir <directory> to copy the release artifacts elsewhere.", flush=True)
        print("DONE", flush=True)
        return 0
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
