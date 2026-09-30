"""诊断：这台机器上能不能用浏览器渲染 PDF。

用打包版（或源码）的命令行入口跑一份自检：
1. 显示它看到的浏览器路径、工作目录、关键环境变量、这次要补的附加参数；
2. 直接用与正式实现同一套参数启动浏览器，打印退出码与输出；
3. 分别测试「继承当前环境」「干净环境」「最小 PATH」「经 cmd 启动」几种方式。

历史：v0.3.0 起打包版启动浏览器会拿到 ``STATUS_BREAKPOINT``，当时就是靠这个工具
一步步排除的；v0.10.0 由 ``xdao/browser_flags.py`` 自动补 ``--no-sandbox`` 修好。
现在它仍然有用 —— 换了浏览器、装了安全软件、用户报「导出 PDF 失败」时，
先看这里的第一行与「浏览器附加参数」。

用法（用打包版运行）：``xdao-export.exe --pdfdiag``（隐藏开关，见 main.py），
或者直接用源码运行本文件。
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

if __package__ in (None, ""):
    # 直接 ``python tools/pdf_diag.py`` 跑时，sys.path[0] 是 tools/ 而不是仓库根目录，
    # 下面的 ``xdao`` 就导不到。经 main.py 的 --pdfdiag 进来时不需要这一步。
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from xdao.browser_flags import launch_flags
from xdao.exporters.pdf import find_browser


def show_environment() -> None:
    print("=== 运行环境 ===")
    print("  sys.executable :", sys.executable)
    print("  sys.frozen     :", getattr(sys, "frozen", False))
    print("  _MEIPASS       :", getattr(sys, "_MEIPASS", "(无)"))
    print("  浏览器附加参数  :", launch_flags() or "（无）")
    print("  当前工作目录    :", Path.cwd())
    print("  临时目录        :", tempfile.gettempdir())
    for key in ("TEMP", "TMP", "TCL_LIBRARY", "TK_LIBRARY", "PYTHONHOME", "PYTHONPATH", "PATH"):
        value = os.environ.get(key, "")
        if key == "PATH":
            value = value[:120] + ("…" if len(value) > 120 else "")
        print(f"  {key:14s} : {value}")


def try_launch(
    label: str,
    *,
    clean_env: bool,
    drop_tcl: bool = False,
    cwd: Path | None = None,
    use_pipes: bool = True,
    minimal_path: bool = False,
    via_cmd: bool = False,
) -> None:
    # 产物一律放在仓库内的 .test-artifacts 下：诊断会生成浏览器 profile 与
    # 崩溃转储，绝不能写到仓库里，也不该散落在系统临时目录。
    root = Path(__file__).resolve().parent.parent / ".test-artifacts" / "pdfdiag"
    work = root / f"run-{label.replace(' ', '_').replace('/', '_')}"
    work.mkdir(parents=True, exist_ok=True)
    source = work / "source.html"
    source.write_text("<!DOCTYPE html><html><body><h1>诊断</h1></body></html>", encoding="utf-8")
    target = work / "output.pdf"
    profile = work / "profile"
    target.unlink(missing_ok=True)

    try:
        browser = find_browser()
    except Exception as exc:
        print(f"  {label}: 找不到浏览器 —— {exc}")
        return

    env = {} if clean_env else dict(os.environ)
    if clean_env:
        # 干净环境也要保留最基本的系统变量，否则浏览器起不来
        for key in ("SystemRoot", "windir", "TEMP", "TMP", "USERPROFILE", "APPDATA", "LOCALAPPDATA",
                    "ProgramData", "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE", "COMSPEC",
                    "SystemDrive", "PATHEXT", "OS"):
            if key in os.environ:
                env[key] = os.environ[key]
    if drop_tcl:
        # 打包版会把捆绑的 Tcl/Tk 路径塞进环境变量，浏览器子进程继承后会加载
        # 版本不匹配的 DLL 而崩溃（STATUS_BREAKPOINT）。
        for key in ("TCL_LIBRARY", "TK_LIBRARY"):
            env.pop(key, None)
    if minimal_path:
        # 最小 PATH：让浏览器只用自己目录与系统目录，避开打包目录里
        # 那些同名运行库（python312.dll / VCRUNTIME140.dll 等）。
        browser_dir = str(browser.path.parent)
        system_root = os.environ.get("SystemRoot", r"C:\Windows")
        env["PATH"] = ";".join([
            browser_dir,
            os.path.join(system_root, "system32"),
            system_root,
            os.path.join(system_root, "system32", "Wbem"),
        ])

    command = [
        str(browser.path),
        "--headless=new",
        f"--user-data-dir={profile}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-gpu",
        "--disable-crash-reporter",
        "--disable-extensions",
        "--print-to-pdf-no-header",
        f"--print-to-pdf={target}",
        # 与正式实现保持一致：打包版不带这个开关时浏览器会被系统中断
        # （STATUS_BREAKPOINT），诊断工具漏掉它就会给出误导性的「启动失败」。
        *launch_flags(),
        source.as_uri(),
    ]
    stdout_file = work / "browser.out"
    stderr_file = work / "browser.err"
    try:
        if via_cmd:
            # 经由 cmd.exe 启动，让浏览器脱离本进程的句柄继承关系。
            inner = subprocess.list2cmdline(command)
            full = f'"{inner}" > "{stdout_file}" 2> "{stderr_file}"'
            completed = subprocess.run(
                ["cmd", "/c", full], timeout=180, env=env, cwd=str(cwd or work),
            )
            code = completed.returncode
            output = ""
            for path in (stderr_file, stdout_file):
                if path.exists():
                    output += path.read_text(encoding="utf-8", errors="replace")
        elif use_pipes:
            completed = subprocess.run(
                command, capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=180, env=env, cwd=str(cwd or work),
            )
            code = completed.returncode
            output = (completed.stderr or completed.stdout or "")
        else:
            # 不用管道：把浏览器输出重定向到文件。某些环境（例如冻结进程）里
            # 管道句柄会被子进程继承出问题，表现为浏览器直接崩掉。
            with stdout_file.open("wb") as out, stderr_file.open("wb") as err:
                completed = subprocess.run(
                    command, stdout=out, stderr=err, timeout=180,
                    env=env, cwd=str(cwd or work),
                )
            code = completed.returncode
            output = ""
            for path in (stderr_file, stdout_file):
                if path.exists():
                    output += path.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        print(f"  {label}: 启动失败 {type(exc).__name__}: {exc}")
        return

    produced = target.exists() and target.stat().st_size > 0
    print(f"  {label}: 退出码={code} 生成PDF={'是' if produced else '否'}"
          f" 体积={target.stat().st_size if produced else 0}")
    if not produced and output.strip():
        print("    浏览器输出：", output.strip().replace("\n", " | ")[:300])


def main() -> int:
    show_environment()
    print("\n=== 直接启动浏览器 ===")
    try_launch("管道捕获输出", clean_env=False)
    try_launch("重定向到文件", clean_env=False, use_pipes=False)
    try_launch("最小PATH", clean_env=False, use_pipes=False, minimal_path=True)
    try_launch("经cmd启动", clean_env=False, use_pipes=False, via_cmd=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
