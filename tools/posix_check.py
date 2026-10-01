"""在 Windows 上验证 test_pdf.make_fake_browser 的 POSIX 分支。

本机没有 Linux，也没有 WSL 发行版，但 Git 自带的 sh.exe 足够验证关键点：
包装脚本以 shebang 开头、shell 认为它可执行、参数原样透传、目标文件确实写出来。
真实 Linux 行为（真正的执行位、内核直接认 shebang）仍由 CI 的 ubuntu job 覆盖；
这里只是本地预警，免得每次都靠推 CI 才知道 —— 早先包装脚本只写 .cmd，
Linux 上 subprocess 直接 PermissionError，就是 CI 抓到的。

判定分六项，逐条打印 ✓/✗，任何一项不过就返回 1：
    返回码 / shebang / 可执行性 / flags.txt / 参数原样透传 / PDF 内容
其中「参数原样透传」是把假浏览器记下来的参数与真正传进去的逐条对比 ——
$@ 少写一层引号、路径被 shell 改写，都会在这里露出来。

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

# 造包装脚本的夹具单独取个名字，用例才好顶掉它（不必真去写文件）。
make_wrapper = tp.make_fake_browser


def _is_windows() -> bool:
    return os.name == "nt"


def _run(argv, *, env=None, timeout: int = 60):
    return subprocess.run(argv, capture_output=True, timeout=timeout, env=env)


def find_git_sh(candidates=GIT_SH_CANDIDATES, which=shutil.which) -> Path:
    """找到一个能用的 sh。

    先看写死的两个 Git 安装位置，再退到 PATH（sh / sh.exe），最后从 PATH 里的
    git.exe 反推 ``usr/bin/sh.exe`` —— Git 装在别处（scoop、用户目录）时也能用。
    都找不到就直说怎么办，别让调用方去猜。
    """
    for raw in candidates:
        path = Path(raw)
        if path.exists():
            return path
    for name in ("sh", "sh.exe"):
        found = which(name)
        if found:
            return Path(found)
    git = which("git")
    if git:
        guess = Path(git).resolve().parent.parent / "usr" / "bin" / "sh.exe"
        if guess.exists():
            return guess
    raise SystemExit(
        "找不到 sh，无法在 Windows 上验证 POSIX 分支："
        "装一个 Git for Windows，或把 sh.exe 放进 PATH。"
    )


def check_shebang(browser: Path) -> bool:
    """包装脚本必须以 ``#!`` 开头 —— Linux 内核就是靠它认解释器的。

    顺带挡住 BOM：``\\xef\\xbb\\xbf#!`` 在 Linux 上不算 shebang，
    执行起来是 ``Exec format error``，而 Windows 上完全看不出来。
    """
    try:
        head = browser.read_bytes().split(b"\n", 1)[0]
    except OSError:
        return False
    return head.startswith(b"#!")


def read_flags(flags_file: Path) -> list[str]:
    """读回假浏览器记下的参数（夹具用 ``'\\n'.join(argv[1:])`` 写的）。"""
    if not flags_file.exists():
        return []
    text = flags_file.read_text(encoding="utf-8")
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def shell_says_executable(sh: Path, browser: Path) -> bool:
    """问 shell 自己认不认为这个文件可执行。

    Windows 上 ``os.access(path, os.X_OK)`` 对任何存在的文件都返回 True，等于没查；
    交给 MSYS 的 sh（它按扩展名 / shebang 认可执行性）判定才有意义。
    真正的 POSIX 执行位只有 Linux 上验得了，那一项由 CI 的 ubuntu job 覆盖。
    """
    done = _run([str(sh), "-c", 'test -x "$1"', "sh", str(browser)])
    return done.returncode == 0


def main() -> int:
    work = ROOT / ".test-artifacts" / "posix-check"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)

    target_pdf = work / "out.pdf"
    flags = work / "flags.txt"
    print_args = ["--headless=new", f"--print-to-pdf={target_pdf}"]

    if _is_windows():
        sh = find_git_sh()
        browser = make_wrapper(
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
        sh = None
        browser = make_wrapper(work, is_windows=False)
        env = None
        argv = [str(browser), *print_args]

    print(f"包装脚本: {browser.name}")
    print(f"内容:\n{browser.read_text(encoding='utf-8')}")
    print(f"执行方式: {argv[0]}")
    if sh is None:
        print(f"os.access(X_OK): {os.access(browser, os.X_OK)}（Linux 上这一项算数）")
    else:
        print(
            "执行位: Windows 上 os.access(X_OK) 对任何存在的文件都是 True，"
            "等于没查 —— 改问 Git 的 sh 自己（test -x）"
        )

    checks: list[tuple[str, bool]] = []
    checks.append(("包装脚本以 #! 开头（Linux 认 shebang 的前提）", check_shebang(browser)))
    if sh is None:
        checks.append(
            (
                f"可执行位（os.access X_OK={os.access(browser, os.X_OK)}）",
                os.access(browser, os.X_OK),
            )
        )
    else:
        checks.append(("sh 认为它可执行（test -x）", shell_says_executable(sh, browser)))

    run_note = ""
    try:
        completed = _run(argv, env=env)
        returncode: int | None = completed.returncode
        if completed.stderr:
            print("stderr:", completed.stderr.decode("utf-8", "replace")[:400])
    except subprocess.TimeoutExpired:
        returncode = None
        run_note = "超时（60 秒还没结束）"
    except OSError as exc:
        returncode = None
        run_note = f"跑不起来：{type(exc).__name__}: {exc}"
    print(f"返回码: {returncode if returncode is not None else run_note}")
    checks.append(("返回码 == 0" + (f"（{run_note}）" if run_note else ""), returncode == 0))

    recorded = read_flags(flags)
    print(f"flags.txt: {recorded if recorded else '（没写出来）'}")
    checks.append(("flags.txt 写出来了", recorded != []))
    args_ok = recorded == print_args
    checks.append((f"参数原样透传（{len(print_args)} 条）", args_ok))
    if not args_ok:
        print(f"  期望: {print_args}")
        print(f"  实际: {recorded}")

    pdf_head = b""
    if target_pdf.exists():
        try:
            pdf_head = target_pdf.read_bytes()[:5]
        except OSError:
            pdf_head = b""
    print(f"PDF: {'写出' if target_pdf.exists() else '没写出'}（开头 {pdf_head!r}）")
    checks.append(("PDF 写出且以 %PDF 开头", pdf_head.startswith(b"%PDF")))

    failed = [name for name, good in checks if not good]
    print()
    for name, good in checks:
        print(f"  {'✓' if good else '✗'} {name}")
    if failed:
        print(f"POSIX 分支验证: 不通过 —— {len(failed)} 项没过")
        return 1
    print("POSIX 分支验证: 通过")
    if sh is not None:
        print(
            "（Windows 上验的是 sh 眼中的可执行性与参数透传；"
            "真正的执行位由 CI 的 ubuntu job 覆盖）"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
