"""诊断：这台机器上能不能用浏览器渲染 PDF。

用打包版（或源码）的命令行入口跑一份自检：
1. 显示它看到的浏览器路径、工作目录、关键环境变量、这次要补的附加参数；
2. 直接用与正式实现同一套参数启动浏览器，打印退出码与输出；
3. 分别测试「管道捕获输出」「重定向到文件」「最小 PATH」「分离进程」
   「干净环境」「干净环境去掉 Tcl/Tk」几种方式；
4. 最后收成一句结论：这台机器到底能不能导出 PDF，能的话是哪几种方式成的。

历史：v0.3.0 起打包版启动浏览器会拿到 ``STATUS_BREAKPOINT``，当时就是靠这个工具
一步步排除的；v0.10.0 由 ``xdao/browser_flags.py`` 自动补 ``--no-sandbox`` 修好。
2026-10-02：原来那项「经 cmd 启动」**一直是坏的** —— ``cmd /c "<命令>" > out 2> err``
这种写法会被 cmd 自己的引号规则拆坏（实测四种引号写法都返回 1：「The filename,
directory name, or volume label syntax is incorrect.」），而那时诊断工具不管成没成
都返回 0，所以坏了好几年也没人发现。现在换成「分离进程」：同样一条命令行，但用
``DETACHED_PROCESS`` 启动、输出写文件，让浏览器脱离本进程的控制台与句柄继承关系
（真机实测能出 PDF）。
现在它仍然有用 —— 换了浏览器、装了安全软件、遇到「导出 PDF 失败」时，
先看这里的第一行与「浏览器附加参数」。

用法（用打包版运行）：``xdao-export.exe --pdfdiag``（隐藏开关，见 main.py），
或者直接用源码运行本文件。

退出码：0 = 至少有一种启动方式真的生成了一份 PDF；1 = 一种都没成，或者这台机器上
根本找不到浏览器。**看到 1 就是「现在导出 PDF 会失败」**，别把它当成诊断工具自己出错。
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

if __package__ in (None, ""):
    # 直接 ``python tools/pdf_diag.py`` 跑时，sys.path[0] 是 tools/ 而不是仓库根目录，
    # 下面的 ``xdao`` 就导不到。经 main.py 的 --pdfdiag 进来时不需要这一步。
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from xdao.browser_flags import launch_flags
from xdao.exporters.pdf import find_browser

PDF_MAGIC = b"%PDF"
# 让浏览器脱离本进程的控制台与句柄继承关系。不用 ``cmd /c``：cmd 的引号规则会把
# 命令行拆坏（见模块开头的历史说明），实测四种写法都跑不起来。
DETACHED_PROCESS = getattr(subprocess, "DETACHED_PROCESS", 0x00000008)


@dataclass
class Attempt:
    """一种启动方式的结果。

    ``problem`` 是 None 才表示真的生成了一份 PDF；否则那句话就是失败原因
    （「没有生成文件」/「生成的文件是 0 字节」/「生成的文件开头不是 %PDF…」等）。
    """

    label: str
    problem: str | None
    code: int | None = None
    output: str = ""

    @property
    def ok(self) -> bool:
        return self.problem is None


def artifacts_root() -> Path:
    """诊断产物的根目录。

    放在仓库内的 ``.test-artifacts/pdfdiag`` 下：诊断会生成浏览器 profile 与崩溃
    转储，不能散落在系统临时目录里。用例会把它顶到临时目录，免得脏了仓库。
    """
    return Path(__file__).resolve().parent.parent / ".test-artifacts" / "pdfdiag"


def pdf_problem(target: Path) -> str | None:
    """这个文件算不算一份像样的 PDF。None = 没问题，否则是一句人话。"""
    if not target.exists():
        return "没有生成文件"
    size = target.stat().st_size
    if size == 0:
        return "生成的文件是 0 字节"
    head = target.open("rb").read(8)
    if not head.startswith(PDF_MAGIC):
        return f"生成的文件开头不是 %PDF，而是 {head!r}"
    return None


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
    detached: bool = False,
) -> Attempt:
    work = artifacts_root() / f"run-{label.replace(' ', '_').replace('/', '_')}"
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
        return Attempt(label=label, problem=f"找不到浏览器：{exc}")

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
        if detached:
            # 不用 cmd.exe：cmd 的引号规则会把命令行拆坏（模块开头有实测记录），
            # 改用 DETACHED_PROCESS 达到同一个目的 —— 浏览器不继承本进程的控制台
            # 与句柄，输出仍旧重定向到文件。
            with stdout_file.open("wb") as out, stderr_file.open("wb") as err:
                completed = subprocess.run(
                    command, stdout=out, stderr=err, timeout=180, env=env,
                    cwd=str(cwd or work),
                    creationflags=DETACHED_PROCESS if os.name == "nt" else 0,
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
        return Attempt(label=label, problem=f"启动失败 {type(exc).__name__}: {exc}")

    problem = pdf_problem(target)
    size = target.stat().st_size if target.exists() else 0
    print(f"  {label}: 退出码={code} 生成PDF={'是' if problem is None else '否'} 体积={size}")
    if problem is not None:
        print(f"    —— {problem}")
        if output.strip():
            print("    浏览器输出：", output.strip().replace("\n", " | ")[:300])
    return Attempt(label=label, problem=problem, code=code, output=output)


def summarize(attempts: list[Attempt]) -> int:
    """把几种方式的结果收成一句结论，并给出退出码。"""
    print("\n=== 结论 ===")
    good = [attempt.label for attempt in attempts if attempt.ok]
    if good:
        print(f"  ✓ 这台机器能渲染 PDF（成功的方式：{'、'.join(good)}）。")
        others = [attempt.label for attempt in attempts if not attempt.ok]
        if others:
            print(f"    另外 {len(others)} 种方式没成（{'、'.join(others)}）—— "
                  f"正式导出走的是能成的那条路，可以不管。")
        return 0

    missing = [a for a in attempts if (a.problem or "").startswith("找不到浏览器")]
    if len(missing) == len(attempts):
        print("  × 一种方式都没成：这台机器上找不到可用的浏览器。")
        print("    装一个 Chrome 或 Edge（或者在设置里把浏览器路径指过去），再跑一次这个诊断。")
        return 1

    print(f"  × {len(attempts)} 种启动方式都没能生成 PDF —— 这台机器现在导出 PDF 会失败，"
          f"不是「诊断没问题」。")
    wrong = [a for a in attempts if (a.problem or "").startswith("生成的文件开头")]
    if wrong:
        print(f"    其中 {len(wrong)} 种写出了文件但不是 PDF（{'、'.join(a.label for a in wrong)}）"
              f"—— 多半是浏览器被拦下之后写了错误页。")
    print("    上面每一行的退出码与浏览器输出就是线索；参数与正式实现是同一套"
          "（见「浏览器附加参数」那一行）。")
    return 1


def main() -> int:
    show_environment()
    print("\n=== 直接启动浏览器 ===")
    attempts = [
        try_launch("管道捕获输出", clean_env=False),
        try_launch("重定向到文件", clean_env=False, use_pipes=False),
        try_launch("最小PATH", clean_env=False, use_pipes=False, minimal_path=True),
        try_launch("分离进程", clean_env=False, use_pipes=False, detached=True),
        try_launch("干净环境", clean_env=True, use_pipes=False),
        try_launch("干净环境去Tcl", clean_env=True, drop_tcl=True, use_pipes=False),
    ]
    return summarize(attempts)


if __name__ == "__main__":
    raise SystemExit(main())
