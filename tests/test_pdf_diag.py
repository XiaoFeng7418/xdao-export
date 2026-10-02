"""``tools/pdf_diag.py`` 的用例。

它要回答的问题只有一个：**这台机器现在到底能不能导出 PDF**。
所以这里钉死三件以前会骗人的事：

1. 光看「文件存在」不算数 —— 浏览器被拦下时也会写出一个非空的错误页，
   必须验文件头是不是 ``%PDF``；
2. 找不到浏览器、启动失败、一种方式都没成，都必须走进结论那句话里，
   而且退出码要是 1（``--pdfdiag`` 的返回值会一路带到命令行）；
3. 诊断用的参数必须与正式实现是同一套（含 ``launch_flags()``），
   否则它给出的「启动失败」是误导。

用例不真的启动浏览器：``find_browser`` 与 ``subprocess`` 全被顶掉，
诊断产物也被顶到临时目录（默认会写进仓库的 ``.test-artifacts/``）。
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools import pdf_diag
from xdao.exporters.pdf import BrowserInfo

PDF_BYTES = b"%PDF-1.4\n% fake\n"
HTML_BYTES = b"<html><body>Blocked</body></html>"
_REAL_ARTIFACTS_ROOT = pdf_diag.artifacts_root  # 上面那条 autouse 会把它顶掉


class _Done:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class _FakeRun:
    """假的 subprocess.run：按参数决定写出 PDF、写日志，还是失败。"""

    def __init__(
        self,
        *,
        rc: int = 0,
        write_pdf: bool = True,
        content: bytes = PDF_BYTES,
        stderr: bytes = b"",
        oserror: bool = False,
        timeout: bool = False,
    ) -> None:
        self.rc = rc
        self.write_pdf = write_pdf
        self.content = content
        self.stderr = stderr
        self.oserror = oserror
        self.timeout = timeout
        self.calls: list[tuple[list[str], dict]] = []

    # ---- 内部工具 ----
    @staticmethod
    def _target(argv: list[str]) -> str | None:
        for item in argv:
            match = re.search(r"--print-to-pdf=(.+?)(?:\"|\s|$)", str(item))
            if match:
                return match.group(1)
        return None

    @staticmethod
    def _log_files(argv: list[str], kwargs: dict) -> tuple[Path, Path] | None:
        """分离进程那条路：输出由工具自己重定向到文件，不是靠 cmd 的 ``>``。"""
        if not kwargs.get("creationflags"):
            return None
        out, err = kwargs.get("stdout"), kwargs.get("stderr")
        if hasattr(out, "name") and hasattr(err, "name"):
            return Path(str(out.name)), Path(str(err.name))
        return None

    def __call__(self, argv, **kwargs):
        self.calls.append(([str(item) for item in argv], kwargs))
        if self.oserror:
            raise OSError("[WinError 740] 请求的操作需要提升")
        if self.timeout:
            raise subprocess.TimeoutExpired(cmd=argv, timeout=180)

        target = self._target([str(item) for item in argv])
        if target and self.write_pdf:
            Path(target).write_bytes(self.content)

        logs = self._log_files([str(item) for item in argv], kwargs)
        if logs is not None:
            out_file, err_file = logs
            out_file.write_bytes(b"")
            err_file.write_bytes(self.stderr)
        for stream in (kwargs.get("stdout"), kwargs.get("stderr")):
            if hasattr(stream, "write"):
                stream.write(self.stderr)

        if kwargs.get("capture_output"):
            return _Done(self.rc, "", self.stderr.decode("utf-8", "replace"))
        return _Done(self.rc)


def use_browser(monkeypatch: pytest.MonkeyPatch, path: Path | None = None) -> Path:
    """让 find_browser() 返回一个假浏览器（path=None 表示假造一个）。"""
    exe = path or Path("C:/fake/msedge.exe")
    monkeypatch.setattr(pdf_diag, "find_browser", lambda *a, **k: BrowserInfo(path=exe, name="Edge"))
    return exe


def use_run(monkeypatch: pytest.MonkeyPatch, run: _FakeRun) -> _FakeRun:
    monkeypatch.setattr(
        pdf_diag,
        "subprocess",
        SimpleNamespace(run=run),
    )
    return run


def use_flags(monkeypatch: pytest.MonkeyPatch, flags: list[str]) -> None:
    monkeypatch.setattr(pdf_diag, "launch_flags", lambda: list(flags))


@pytest.fixture(autouse=True)
def no_real_launch(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """默认什么都不许真跑：产物进临时目录，浏览器与子进程都要用例自己顶掉。"""
    monkeypatch.setattr(pdf_diag, "artifacts_root", lambda: tmp_path / "artifacts")
    use_flags(monkeypatch, [])

    def refuse_browser(*a, **k):
        raise AssertionError("这条例用没顶掉 find_browser —— 别真去找本机的浏览器")

    def refuse_run(*a, **k):
        raise AssertionError("这条例用没顶掉 subprocess —— 别真去启动浏览器")

    monkeypatch.setattr(pdf_diag, "find_browser", refuse_browser)
    monkeypatch.setattr(pdf_diag, "subprocess", SimpleNamespace(run=refuse_run))
    yield


# ---------------------------------------------------------------- 文件头判定


def test_pdf_problem_flags_a_missing_file(tmp_path: Path) -> None:
    assert pdf_diag.pdf_problem(tmp_path / "没有这个.pdf") == "没有生成文件"


def test_pdf_problem_flags_an_empty_file(tmp_path: Path) -> None:
    target = tmp_path / "empty.pdf"
    target.write_bytes(b"")
    assert pdf_diag.pdf_problem(target) == "生成的文件是 0 字节"


def test_pdf_problem_flags_an_error_page(tmp_path: Path) -> None:
    target = tmp_path / "blocked.pdf"
    target.write_bytes(HTML_BYTES)
    problem = pdf_diag.pdf_problem(target)
    assert problem is not None
    assert "开头不是 %PDF" in problem


def test_pdf_problem_accepts_a_real_pdf(tmp_path: Path) -> None:
    target = tmp_path / "ok.pdf"
    target.write_bytes(PDF_BYTES)
    assert pdf_diag.pdf_problem(target) is None


def test_artifacts_default_to_the_repo_artifacts_dir() -> None:
    root = _REAL_ARTIFACTS_ROOT()
    assert root.name == "pdfdiag"
    assert root.parent.name == ".test-artifacts"
    assert (root.parent.parent / "main.py").exists()  # 落在仓库里，不散到临时目录


# ---------------------------------------------------------------- 参数与启动方式


def test_the_command_matches_the_real_exporter(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    exe = use_browser(monkeypatch, tmp_path / "Edge" / "msedge.exe")
    use_flags(monkeypatch, ["--no-sandbox"])
    run = use_run(monkeypatch, _FakeRun())

    attempt = pdf_diag.try_launch("管道捕获输出", clean_env=False)

    assert attempt.ok
    argv = run.calls[0][0]
    assert argv[0] == str(exe)
    assert argv[1] == "--headless=new"
    assert any(item.startswith("--user-data-dir=") for item in argv)
    assert any(item.startswith("--print-to-pdf=") for item in argv)
    assert argv[-1].startswith("file://") and argv[-1].endswith("source.html")
    # 打包版靠这个开关才不被系统中断：必须在正式实现同样的位置补上
    assert argv[-2] == "--no-sandbox"


def test_pipes_mode_takes_stderr_first(monkeypatch: pytest.MonkeyPatch) -> None:
    use_browser(monkeypatch)
    run = use_run(monkeypatch, _FakeRun(rc=1, write_pdf=False, stderr=b"sandbox failed"))
    attempt = pdf_diag.try_launch("管道捕获输出", clean_env=False)
    assert attempt.problem == "没有生成文件"
    assert attempt.output == "sandbox failed"
    assert run.calls[0][1]["capture_output"] is True


def test_redirect_mode_without_pipes(monkeypatch: pytest.MonkeyPatch) -> None:
    use_browser(monkeypatch)
    run = use_run(monkeypatch, _FakeRun(rc=0, write_pdf=False, stderr=b"crashed"))
    attempt = pdf_diag.try_launch("重定向到文件", clean_env=False, use_pipes=False)
    assert attempt.problem == "没有生成文件"
    assert "crashed" in attempt.output
    assert "capture_output" not in run.calls[0][1]


def test_detached_mode_uses_a_detached_process(monkeypatch: pytest.MonkeyPatch) -> None:
    """「分离进程」不再经 cmd：cmd 的引号规则会把命令行拆坏（实测四种写法都跑不起来）。

    这里盯的是替代做法本身：同一套参数直接启动，靠 creationflags 脱离控制台，
    输出由工具自己重定向到文件。
    """
    exe = use_browser(monkeypatch)
    run = use_run(monkeypatch, _FakeRun(write_pdf=True, stderr=b"detached log"))
    attempt = pdf_diag.try_launch("分离进程", clean_env=False, use_pipes=False, detached=True)
    argv, kwargs = run.calls[0]
    assert argv[0] == str(exe)
    assert not any("cmd" in item.lower() for item in argv)
    assert kwargs["creationflags"] == (pdf_diag.DETACHED_PROCESS if os.name == "nt" else 0)
    assert Path(str(kwargs["stdout"].name)).name == "browser.out"
    assert Path(str(kwargs["stderr"].name)).name == "browser.err"
    assert attempt.ok
    assert "detached log" in attempt.output


def test_main_no_longer_tries_a_shell_launch(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    """六种方式里不再有「经 cmd 启动」：cmd 的引号规则会把命令行拆坏，永远出不了 PDF。"""
    use_browser(monkeypatch)
    use_run(monkeypatch, _FakeRun(write_pdf=True))
    assert pdf_diag.main() == 0
    out = capsys.readouterr().out
    assert "分离进程" in out
    assert "经cmd" not in out


def test_minimal_path_keeps_only_browser_and_system_dirs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    exe = use_browser(monkeypatch, tmp_path / "Edge" / "msedge.exe")
    run = use_run(monkeypatch, _FakeRun())
    monkeypatch.setenv("SystemRoot", r"C:\Windows")
    pdf_diag.try_launch("最小PATH", clean_env=False, use_pipes=False, minimal_path=True)
    path = run.calls[0][1]["env"]["PATH"]
    parts = path.split(";")
    assert parts[0] == str(exe.parent)
    # 期望值也用 os.path.join 现拼：在 ubuntu 上它给的是 "C:\Windows/system32"，
    # 写死反斜杠的断言只在 Windows 上过得去（CI 上就是这么红过一次）。
    assert parts[1].lower() == os.path.join(r"C:\Windows", "system32").lower()
    assert parts[2].lower() == r"C:\Windows".lower()
    assert parts[3].lower() == os.path.join(r"C:\Windows", "system32", "Wbem").lower()
    assert all("python" not in part.lower() for part in parts)


def test_clean_env_keeps_only_system_variables(monkeypatch: pytest.MonkeyPatch) -> None:
    use_browser(monkeypatch)
    run = use_run(monkeypatch, _FakeRun())
    monkeypatch.setenv("XDAO_PROBE_MARKER", "1")
    monkeypatch.setenv("PATH", r"C:\somewhere\python")
    pdf_diag.try_launch("干净环境", clean_env=True, use_pipes=False)
    env = run.calls[0][1]["env"]
    assert "XDAO_PROBE_MARKER" not in env
    assert "PATH" not in env  # 干净环境不继承 PATH，浏览器只靠自己与系统目录
    for key in ("SystemRoot", "TEMP", "COMSPEC"):
        if key in os.environ:
            assert key in env


def test_drop_tcl_removes_the_bundled_tcl_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    use_browser(monkeypatch)
    run = use_run(monkeypatch, _FakeRun())
    monkeypatch.setenv("TCL_LIBRARY", r"C:\bundle\tcl")
    monkeypatch.setenv("TK_LIBRARY", r"C:\bundle\tk")
    pdf_diag.try_launch("干净环境去Tcl", clean_env=True, drop_tcl=True, use_pipes=False)
    env = run.calls[0][1]["env"]
    assert "TCL_LIBRARY" not in env and "TK_LIBRARY" not in env


def test_each_mode_gets_its_own_work_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    use_browser(monkeypatch)
    run = use_run(monkeypatch, _FakeRun())
    pdf_diag.try_launch("最小PATH", clean_env=False, use_pipes=False)
    work = Path(run.calls[0][1]["cwd"])
    assert work.name == "run-最小PATH"
    assert (work / "source.html").exists()


def test_a_stale_pdf_from_an_earlier_run_is_deleted_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    use_browser(monkeypatch)
    use_run(monkeypatch, _FakeRun(write_pdf=False))
    work = pdf_diag.artifacts_root() / "run-管道捕获输出"
    work.mkdir(parents=True, exist_ok=True)
    (work / "output.pdf").write_bytes(PDF_BYTES)  # 上一次跑剩下的
    attempt = pdf_diag.try_launch("管道捕获输出", clean_env=False)
    assert not attempt.ok  # 没有被上一次的残留文件骗过去


def test_a_nonzero_exit_still_counts_if_a_real_pdf_came_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    use_browser(monkeypatch)
    use_run(monkeypatch, _FakeRun(rc=1))
    assert pdf_diag.try_launch("管道捕获输出", clean_env=False).ok


def test_an_error_page_is_not_a_success(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    use_browser(monkeypatch)
    use_run(monkeypatch, _FakeRun(content=HTML_BYTES, stderr=b"Blocked by policy"))
    attempt = pdf_diag.try_launch("管道捕获输出", clean_env=False)
    out = capsys.readouterr().out
    assert not attempt.ok
    assert "生成PDF=否" in out
    assert "开头不是 %PDF" in out
    assert "Blocked by policy" in out


def test_a_missing_browser_is_reported_per_attempt(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    def boom(*a, **k):
        raise RuntimeError("没有找到 Chrome 或 Edge")

    monkeypatch.setattr(pdf_diag, "find_browser", boom)
    attempt = pdf_diag.try_launch("管道捕获输出", clean_env=False)
    assert attempt.problem is not None
    assert attempt.problem.startswith("找不到浏览器")
    assert "找不到浏览器" in capsys.readouterr().out


def test_a_crash_is_reported_not_raised(monkeypatch: pytest.MonkeyPatch) -> None:
    use_browser(monkeypatch)
    use_run(monkeypatch, _FakeRun(oserror=True))
    attempt = pdf_diag.try_launch("管道捕获输出", clean_env=False)
    assert attempt.problem is not None and "启动失败" in attempt.problem
    assert "OSError" in attempt.problem


def test_a_timeout_is_reported_not_raised(monkeypatch: pytest.MonkeyPatch) -> None:
    use_browser(monkeypatch)
    use_run(monkeypatch, _FakeRun(timeout=True))
    attempt = pdf_diag.try_launch("管道捕获输出", clean_env=False)
    assert attempt.problem is not None and "TimeoutExpired" in attempt.problem


# ---------------------------------------------------------------- 结论与退出码


def test_a_successful_run_needs_no_second_chance(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    use_browser(monkeypatch)
    use_run(monkeypatch, _FakeRun())
    codes = pdf_diag.main()
    out = capsys.readouterr().out
    assert codes == 0
    assert "✓ 这台机器能渲染 PDF" in out
    assert "=== 结论 ===" in out


def test_main_says_one_working_mode_is_enough(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    use_browser(monkeypatch)
    calls = {"n": 0}

    def only_first(argv, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            target = _FakeRun._target([str(item) for item in argv])
            Path(target).write_bytes(PDF_BYTES)
        return _Done(0)

    monkeypatch.setattr(
        pdf_diag,
        "subprocess",
        SimpleNamespace(run=only_first),
    )
    assert pdf_diag.main() == 0
    out = capsys.readouterr().out
    assert "成功的方式：管道捕获输出" in out
    assert "可以不管" in out
    assert calls["n"] == 6  # 六种方式都试过


def test_main_fails_when_no_mode_produced_a_pdf(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    use_browser(monkeypatch)
    use_run(monkeypatch, _FakeRun(write_pdf=False, stderr=b"sandbox"))
    assert pdf_diag.main() == 1
    out = capsys.readouterr().out
    assert "6 种启动方式都没能生成 PDF" in out
    assert "不是「诊断没问题」" in out


def test_main_tells_you_to_install_a_browser_when_there_is_none(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    def boom(*a, **k):
        raise RuntimeError("没有找到 Chrome 或 Edge")

    monkeypatch.setattr(pdf_diag, "find_browser", boom)
    assert pdf_diag.main() == 1
    out = capsys.readouterr().out
    assert "找不到可用的浏览器" in out
    assert "装一个 Chrome 或 Edge" in out


def test_main_points_at_error_pages(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    use_browser(monkeypatch)
    use_run(monkeypatch, _FakeRun(content=HTML_BYTES))
    assert pdf_diag.main() == 1
    out = capsys.readouterr().out
    assert "写出了文件但不是 PDF" in out
    assert "错误页" in out


def test_summarize_is_honest_when_every_mode_crashed(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    use_browser(monkeypatch)
    use_run(monkeypatch, _FakeRun(oserror=True))
    attempts = [pdf_diag.try_launch(f"方式{i}", clean_env=False) for i in range(2)]
    assert pdf_diag.summarize(attempts) == 1
    out = capsys.readouterr().out
    assert "2 种启动方式都没能生成 PDF" in out


def test_show_environment_prints_the_launch_flags(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    use_flags(monkeypatch, ["--no-sandbox"])
    pdf_diag.show_environment()
    out = capsys.readouterr().out
    assert "浏览器附加参数" in out
    assert "--no-sandbox" in out
    assert "sys.frozen" in out
