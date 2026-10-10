"""把源码打成免安装包 `xdao-export-v<版本>-win64.zip`，并写一份配套的 `.sha256`。

为什么这一步要放进仓库（2026-10-02）：

1. 以前打包只有作者本机 `_scratch/` 里的脚本，别人想核对「Release 里那个包到底是
   怎么来的」只能来问。搬进仓库后，谁都能 `python tools/build_zip.py` 复现；
2. 申请免费代码签名（SignPath Foundation，面向开源项目）有一条硬要求：
   **要签名的产物必须由 CI 构建**。所以打包必须能在 GitHub 托管的 Windows runner 上跑通，
   见 `.github/workflows/build.yml`。

用法::

    python tools/build_zip.py                 # 产物写到 ./dist
    python tools/build_zip.py --out D:\\out   # 换个目录

跑完打印 zip 路径、条目数、字节数与 SHA256；`.sha256` 就写在 zip 旁边，内容是一行
``<摘要>  <文件名>``（`sha256sum -c` / `certutil` 那一类工具认这个格式）。
"""

from __future__ import annotations

import argparse
import hashlib
import os
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
#: 两个 .ps1/.cmd 是「把抓取与导出的每一步写进日志」的诊断脚本，出问题时让用户跑它。
EXTRA_FILES: tuple[tuple[str, str], ...] = (
    ("packaging/使用说明.txt", "使用说明.txt"),
    ("诊断写入.ps1", "诊断写入.ps1"),
    ("诊断写入-双击运行.cmd", "诊断写入-双击运行.cmd"),
)

#: 这两个是给 Windows 自己的工具打开的，BOM 掉了中文会乱码（`tests/test_text_hygiene.py`
#: 也盯着这一条）。打包前再核一次 —— 这个坑 2026-10-02 踩过一次，而且是肉眼看不出来的。
NEEDS_BOM = ("使用说明.txt", "诊断写入.ps1", "诊断写入-双击运行.cmd")

AUTHOR = "晓风"
PRODUCT = PRODUCT_NAME


def version_quad(version: str) -> tuple[int, int, int, int]:
    """``"0.13.29"`` -> ``(0, 13, 29, 0)``（PE 的 FixedFileInfo 要四段）。"""
    parts = version.split(".")
    if len(parts) != 3 or not all(part.isdigit() for part in parts):
        raise ValueError(f"版本号得是 x.y.z 三段数字，现在是 {version!r}")
    return (int(parts[0]), int(parts[1]), int(parts[2]), 0)


def version_file_text(version: str, author: str = AUTHOR, product: str = PRODUCT) -> str:
    """PyInstaller `--version-file` 要的 PE 版本信息。

    `CompanyName` 是这里唯一能真的把作者名写进二进制的地方：免费的代码签名证书
    签发给 SignPath Foundation，Windows 上显示的发布者不是作者本人，所以
    「右键属性 -> 详细信息」里的这一栏就是署名（2026-10-02 定的）。
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
    """开头是不是正好一个 UTF-8 BOM（多贴一个也是错的）。"""
    data = path.read_bytes()
    return data[:3] == b"\xef\xbb\xbf" and data[3:6] != b"\xef\xbb\xbf"


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


def kill_leftover_exe() -> None:
    """清掉还在跑的打包版进程。

    上一轮真机核验如果没关干净，`_internal` 里的 dll 会被占住，删目录会以
    `WinError 5` 失败（本机脚本踩过一次）。
    """
    if os.name != "nt":
        return
    subprocess.run(
        ["taskkill", "/F", "/IM", LAUNCHER_NAME],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )


def build_pyinstaller(repo: Path, python: str, version_file: Path, work: Path, out: Path) -> Path:
    """跑 PyInstaller，返回它产出的程序目录（`<out>/xdao-export`）。"""
    command = [
        python, "-m", "PyInstaller",
        "--onedir", "--windowed", "--clean", "--noconfirm",
        "--version-file", str(version_file),
        # spec 与中间产物都放到临时目录：仓库里的 *.spec 是历史文件，别让这次打包改到它
        "--specpath", str(work),
        "--workpath", str(work / "build"),
        "--distpath", str(out),
        "--name", "xdao-export",
        "main.py",
    ]
    done = subprocess.run(
        command, cwd=repo, capture_output=True, text=True, encoding="utf-8", errors="replace"
    )
    if done.returncode != 0:
        print((done.stdout or "")[-3000:])
        print((done.stderr or "")[-2000:], file=sys.stderr)
        raise SystemExit(f"PyInstaller 失败（退出码 {done.returncode}）")
    return out / "xdao-export"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="打免安装包并生成 .sha256")
    parser.add_argument("--out", default="dist", help="产物目录（默认 ./dist）")
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
            for extra in (["--version"], ["--selftest"]):
                done = subprocess.run(
                    [str(exe), *extra], capture_output=True, text=True,
                    encoding="utf-8", errors="replace",
                )
                tail = " | ".join((done.stdout or done.stderr or "").strip().splitlines()[-2:])
                print(f"  {' '.join(extra)} → 退出码 {done.returncode}：{tail}", flush=True)
        print("DONE", flush=True)
        return 0
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
