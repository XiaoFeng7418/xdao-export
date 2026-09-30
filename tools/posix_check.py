"""在 Windows 上验证 test_pdf.make_fake_browser 的 POSIX 分支。

本机没有 Linux，也没有 WSL 发行版，但 Git 自带的 sh.exe 足够验证关键点：
包装脚本能被 shell 执行、执行位生效、参数原样透传、目标文件确实写出来。
真实 Linux 行为仍由 CI 的 ubuntu job 覆盖；这里只是本地预警，免得每次都靠推 CI 才知道。

用法：
    python tools/posix_check.py
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import tests.test_pdf as tp  # noqa: E402

GIT_SH_CANDIDATES = (
    r"C:\Program Files\Git\usr\bin\sh.exe",
    r"C:\Program Files (x86)\Git\usr\bin\sh.exe",
)


def find_git_sh() -> Path:
    for raw in GIT_SH_CANDIDATES:
        path = Path(raw)
        if path.exists():
            return path
    raise SystemExit("找不到 Git 自带的 sh.exe，无法在 Windows 上验证 POSIX 分支")


def main() -> int:
    work = ROOT / ".test-artifacts" / "posix-check"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)

    target_pdf = work / "out.pdf"
    flags = work / "flags.txt"
    print_args = ["--headless=new", f"--print-to-pdf={target_pdf}"]

    if os.name == "nt":
        sh = find_git_sh()
        browser = tp.make_fake_browser(
            work,
            is_windows=False,
            python_exe=sys.executable,
            shell_stem="sh",
        )
        env = dict(os.environ)
        env["PATH"] = str(sh.parent) + os.pathsep + env.get("PATH", "")
        # Windows 内核不认 shebang，没法直接 CreateProcess 一个 sh 脚本；
        # 交给 Git 的 sh 解释执行，$@、引号、参数透传这些逻辑照样能验到。
        argv = [str(sh), str(browser), *print_args]
    else:
        browser = tp.make_fake_browser(work, is_windows=False)
        env = None
        argv = [str(browser), *print_args]

    print(f"包装脚本: {browser.name}")
    print(f"内容:\n{browser.read_text(encoding='utf-8')}")
    print(f"可执行位: {os.access(browser, os.X_OK)}")
    print(f"执行方式: {argv[0]}")

    completed = subprocess.run(argv, capture_output=True, timeout=60, env=env)
    print(f"返回码: {completed.returncode}")
    if completed.stderr:
        print("stderr:", completed.stderr.decode("utf-8", "replace")[:400])
    print(f"flags.txt 存在: {flags.exists()}")
    print(f"PDF 写出: {target_pdf.exists()}")

    ok = (
        completed.returncode == 0
        and flags.exists()
        and target_pdf.exists()
        and target_pdf.read_bytes().startswith(b"%PDF")
    )
    print("POSIX 分支验证:", "通过" if ok else "不通过")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
