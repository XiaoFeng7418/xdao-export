"""``tools/posix_check.py`` 的用例：它说「通过」时，那六项得真的查过。

这个脚本是**本机预演 POSIX 分支**的唯一手段（`MAINTENANCE.md` 里
「Python 3.12 判等没问题、解析时间戳却差 8 小时」那类跨平台坑，就是靠它提前抓的），
但它以前有三处「看着通过了」：

- 参数透传根本没验 —— 假浏览器把收到的参数写进 flags.txt，工具却只看文件在不在，
  ``$@`` 少一层引号、路径被 shell 改写，它照样印「通过」；
- 执行位只打印 ``os.access(X_OK)`` 的值、不算进判定，而 Windows 上这个值对
  任何存在的文件都是 True，等于没查（真正的执行位只有 CI 的 ubuntu job 验得了）；
- 包装脚本的 shebang 没查过，而 Linux 内核认的就是它。

所以下面既验「六项都查」，也验「每一项单独坏掉时它必须报错、并且指出是哪一项」。
做法是把 ``make_wrapper`` / ``_run`` / ``_is_windows`` / ``ROOT`` 换成测试自己的，
被测的判定逻辑是真的。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import posix_check as pc  # noqa: E402


# ---------------------------------------------------------------- find_git_sh


def test_find_git_sh_takes_the_first_existing_candidate(tmp_path: Path) -> None:
    first = tmp_path / "a" / "sh.exe"
    second = tmp_path / "b" / "sh.exe"
    second.parent.mkdir(parents=True)
    second.write_text("#!/bin/sh\n", encoding="utf-8")
    assert pc.find_git_sh((str(first), str(second)), which=lambda _n: None) == second


def test_find_git_sh_prefers_a_candidate_over_path(tmp_path: Path) -> None:
    candidate = tmp_path / "hardcoded" / "sh.exe"
    candidate.parent.mkdir(parents=True)
    candidate.write_text("#!/bin/sh\n", encoding="utf-8")
    on_path = tmp_path / "path" / "sh"
    on_path.parent.mkdir(parents=True)
    on_path.write_text("#!/bin/sh\n", encoding="utf-8")
    found = pc.find_git_sh((str(candidate),), which=lambda name: str(on_path))
    assert found == candidate


def test_find_git_sh_falls_back_to_path(tmp_path: Path) -> None:
    on_path = tmp_path / "usr" / "bin" / "sh.exe"
    on_path.parent.mkdir(parents=True)
    on_path.write_text("#!/bin/sh\n", encoding="utf-8")

    def which(name: str) -> str | None:
        return str(on_path) if name in ("sh", "sh.exe") else None

    assert pc.find_git_sh((str(tmp_path / "nope"),), which=which) == on_path


def test_find_git_sh_falls_back_to_the_git_install_dir(tmp_path: Path) -> None:
    """Git 装在别处（scoop、用户目录）时，从 git.exe 反推 usr/bin/sh.exe。"""
    git = tmp_path / "Git" / "cmd" / "git.exe"
    git.parent.mkdir(parents=True)
    git.write_text("", encoding="utf-8")
    sh = tmp_path / "Git" / "usr" / "bin" / "sh.exe"
    sh.parent.mkdir(parents=True)
    sh.write_text("#!/bin/sh\n", encoding="utf-8")

    def which(name: str) -> str | None:
        return str(git) if name == "git" else None

    assert pc.find_git_sh((), which=which) == sh


def test_find_git_sh_says_how_to_fix_it_when_nothing_is_found() -> None:
    with pytest.raises(SystemExit) as excinfo:
        pc.find_git_sh((), which=lambda _n: None)
    assert "装一个 Git for Windows" in str(excinfo.value)


# ------------------------------------------------------------- check_shebang


def test_check_shebang_accepts_a_shell_script(tmp_path: Path) -> None:
    wrapper = tmp_path / "fake_browser.sh"
    wrapper.write_bytes(b"#!/bin/sh\nexec python \"$@\"\n")
    assert pc.check_shebang(wrapper) is True


def test_check_shebang_rejects_a_missing_file(tmp_path: Path) -> None:
    assert pc.check_shebang(tmp_path / "nope.sh") is False


def test_check_shebang_rejects_a_bom(tmp_path: Path) -> None:
    """带 BOM 的 shebang 在 Linux 上是 ``Exec format error``，Windows 上看不出来。"""
    wrapper = tmp_path / "fake_browser.sh"
    wrapper.write_bytes(b"\xef\xbb\xbf#!/bin/sh\nexit 0\n")
    assert pc.check_shebang(wrapper) is False


def test_check_shebang_rejects_a_script_without_one(tmp_path: Path) -> None:
    wrapper = tmp_path / "fake_browser.sh"
    wrapper.write_bytes(b"exec python fake_browser_impl.py\n")
    assert pc.check_shebang(wrapper) is False


def test_check_shebang_tolerates_crlf(tmp_path: Path) -> None:
    """夹具在 Windows 上是文本模式写文件，首行会带 \\r —— 只看开头两个字节就还在。"""
    wrapper = tmp_path / "fake_browser.sh"
    wrapper.write_bytes(b"#!/bin/sh\r\nexit 0\r\n")
    assert pc.check_shebang(wrapper) is True


# ---------------------------------------------------------------- read_flags


def test_read_flags_reads_what_the_fake_browser_recorded(tmp_path: Path) -> None:
    flags = tmp_path / "flags.txt"
    flags.write_text("--headless=new\n--print-to-pdf=C:\\a b\\out.pdf", encoding="utf-8")
    assert pc.read_flags(flags) == ["--headless=new", "--print-to-pdf=C:\\a b\\out.pdf"]


def test_read_flags_tolerates_a_trailing_newline(tmp_path: Path) -> None:
    flags = tmp_path / "flags.txt"
    flags.write_text("--headless=new\n", encoding="utf-8")
    assert pc.read_flags(flags) == ["--headless=new"]


def test_read_flags_is_empty_before_the_browser_ran(tmp_path: Path) -> None:
    assert pc.read_flags(tmp_path / "nope.txt") == []
    empty = tmp_path / "empty.txt"
    empty.write_text("", encoding="utf-8")
    assert pc.read_flags(empty) == []


def test_read_flags_keeps_a_path_with_spaces_in_one_line(tmp_path: Path) -> None:
    flags = tmp_path / "flags.txt"
    target = "--print-to-pdf=D:\\我的 项目\\out.pdf"
    flags.write_text(target, encoding="utf-8")
    assert pc.read_flags(flags) == [target]


# ------------------------------------------------------- shell_says_executable


def _shell() -> Path:
    """找一把能用的 sh：先看 PATH，再借 tools/posix_check 的 Git 安装目录。

    不写成 skip 是有原因的：本仓的测试里「跳过」是要报备的（`tests/conftest.py`），
    而这两台机器（本机 Windows、CI 的 ubuntu）都有 sh —— Windows 上 PATH 里没有 `sh`，
    但 Git 装着呢，`find_git_sh()` 找得到（GitHub 的 windows runner 同样）。
    """
    found = shutil.which("sh")
    if found:
        return Path(found)
    try:
        return pc.find_git_sh()
    except SystemExit:  # pragma: no cover - 连 Git 都没装的机器
        pytest.skip("PATH 与 Git 安装目录里都没有 sh")


def test_shell_says_executable_for_a_script_with_the_exec_bit(tmp_path: Path) -> None:
    sh = _shell()
    wrapper = tmp_path / "fake_browser.sh"
    wrapper.write_bytes(b"#!/bin/sh\nexit 0\n")
    wrapper.chmod(0o755)
    assert pc.shell_says_executable(sh, wrapper) is True


def test_shell_rejects_a_file_without_the_exec_bit(tmp_path: Path) -> None:
    """POSIX 上这是真执行位；Windows 上由 MSYS 按扩展名/shebang 判定，结论一样。

    两个平台都得有结论，不能因为「Windows 没有执行位」就跳过 —— 跳过是会被
    `tests/conftest.py` 记一笔的，而且这里恰好有个真结论可验。
    """
    sh = _shell()
    plain = tmp_path / "notes.txt"
    plain.write_text("不是脚本\n", encoding="utf-8")
    plain.chmod(0o644)
    assert pc.shell_says_executable(sh, plain) is False


def test_shell_says_not_executable_for_a_missing_file(tmp_path: Path) -> None:
    assert pc.shell_says_executable(_shell(), tmp_path / "nope.sh") is False


# ---------------------------------------------------------------------- main


class _Done:
    def __init__(self, returncode: int, stderr: bytes = b"") -> None:
        self.returncode = returncode
        self.stderr = stderr


class _FakeRun:
    """替掉 ``subprocess.run``：按参数写出 flags.txt / PDF，返回给定返回码。"""

    def __init__(
        self,
        work: Path,
        *,
        rc: int = 0,
        write_flags: bool = True,
        recorded: list[str] | None = None,
        write_pdf: bool = True,
        content: bytes = b"%PDF-1.4 fake",
        stderr: bytes = b"",
        timeout: bool = False,
        oserror: bool = False,
    ) -> None:
        self.work = work
        self.rc = rc
        self.write_flags = write_flags
        self.recorded = recorded
        self.write_pdf = write_pdf
        self.content = content
        self.stderr = stderr
        self.timeout = timeout
        self.oserror = oserror
        self.calls: list[list[str]] = []
        self.envs: list[dict[str, str] | None] = []

    def __call__(self, argv, *, env=None, timeout: int = 60):  # noqa: ARG002
        argv = [str(a) for a in argv]
        self.calls.append(argv)
        self.envs.append(env)
        if self.timeout:
            raise subprocess.TimeoutExpired(argv, timeout)
        if self.oserror:
            raise FileNotFoundError(2, "No such file or directory")
        self.work.mkdir(parents=True, exist_ok=True)
        if self.write_flags:
            if self.recorded is None:
                args = [a for a in argv[1:] if a.startswith("--")]
            else:
                args = list(self.recorded)
            (self.work / "flags.txt").write_text("\n".join(args), encoding="utf-8")
        if self.write_pdf:
            target = next((a.split("=", 1)[1] for a in argv if a.startswith("--print-to-pdf=")), None)
            if target:
                Path(target).write_bytes(self.content)
        return _Done(self.rc, self.stderr)


def install_tool(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    wrapper: bytes = b"#!/bin/sh\nexit 0\n",
    windows: bool = False,
    executable: bool = True,
    **run_kw,
) -> SimpleNamespace:
    monkeypatch.setattr(pc, "ROOT", tmp_path)
    work = tmp_path / ".test-artifacts" / "posix-check"
    runs = _FakeRun(work, **run_kw)
    holder = SimpleNamespace(work=work, wrapper=None, runs=runs)

    def fake_make_wrapper(artifacts_dir: Path, **_kwargs) -> Path:
        # main() 会先把 work 目录整个删掉重建，所以包装脚本得在这里现写
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        path = artifacts_dir / "fake_browser.sh"
        path.write_bytes(wrapper)
        path.chmod(0o755)
        holder.wrapper = path
        return path

    monkeypatch.setattr(pc, "make_wrapper", fake_make_wrapper)
    monkeypatch.setattr(pc, "_run", runs)
    monkeypatch.setattr(pc, "_is_windows", lambda: bool(windows))
    monkeypatch.setattr(pc, "shell_says_executable", lambda _sh, _browser: executable)
    return holder


def test_main_passes_when_everything_lines_up(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    tool = install_tool(monkeypatch, tmp_path)
    assert pc.main() == 0
    out = capsys.readouterr().out
    assert "POSIX 分支验证: 通过" in out
    assert out.count("✓") == 6
    # POSIX 分支直接执行包装脚本，不经 sh
    assert tool.runs.calls[0][0] == str(tool.wrapper)
    assert tool.runs.calls[0][1:] == [
        "--headless=new",
        f"--print-to-pdf={tool.work / 'out.pdf'}",
    ]


def test_main_reports_which_item_failed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    install_tool(monkeypatch, tmp_path, wrapper=b"exit 0\n")
    assert pc.main() == 1
    out = capsys.readouterr().out
    assert "✗ 包装脚本以 #! 开头" in out
    assert "不通过 —— 1 项没过" in out


def test_main_fails_when_arguments_are_not_passed_through(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``$@`` 少一层引号、路径被 shell 改写时，flags.txt 里就不是原样两条。"""
    install_tool(monkeypatch, tmp_path, recorded=["--headless=new"])
    assert pc.main() == 1
    out = capsys.readouterr().out
    assert "✗ 参数原样透传" in out
    assert "期望:" in out and "实际:" in out


def test_main_fails_when_the_flags_file_is_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    install_tool(monkeypatch, tmp_path, write_flags=False)
    assert pc.main() == 1
    out = capsys.readouterr().out
    assert "✗ flags.txt 写出来了" in out
    assert "✗ 参数原样透传" in out


def test_main_fails_on_a_nonzero_return_code(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    install_tool(monkeypatch, tmp_path, rc=3, stderr="浏览器不认识这个参数".encode("utf-8"))
    assert pc.main() == 1
    out = capsys.readouterr().out
    assert "✗ 返回码 == 0" in out
    assert "浏览器不认识这个参数" in out


def test_main_fails_when_no_pdf_came_out(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    install_tool(monkeypatch, tmp_path, write_pdf=False)
    assert pc.main() == 1
    assert "✗ PDF 写出且以 %PDF 开头" in capsys.readouterr().out


def test_main_fails_when_the_output_is_not_a_pdf(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # 中文不能直接写进 bytes 字面量（SyntaxError），先 str 再 encode
    install_tool(monkeypatch, tmp_path, content="<html>Chromium 报错页</html>".encode("utf-8"))
    assert pc.main() == 1
    assert "✗ PDF 写出且以 %PDF 开头" in capsys.readouterr().out


def test_main_reports_a_timeout_instead_of_crashing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    install_tool(monkeypatch, tmp_path, timeout=True)
    assert pc.main() == 1
    out = capsys.readouterr().out
    assert "超时" in out
    assert "✗ 返回码 == 0" in out


def test_main_reports_a_missing_interpreter_instead_of_crashing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    install_tool(monkeypatch, tmp_path, oserror=True)
    assert pc.main() == 1
    out = capsys.readouterr().out
    assert "跑不起来：FileNotFoundError" in out
    assert "✗ 返回码 == 0" in out


def test_main_on_windows_asks_the_shell_instead_of_os_access(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Windows 上 ``os.access(X_OK)`` 对任何存在的文件都是 True，不能当判定项。"""
    install_tool(monkeypatch, tmp_path, windows=True, executable=False)
    assert pc.main() == 1
    out = capsys.readouterr().out
    assert "✗ sh 认为它可执行" in out
    assert "等于没查" in out
    assert "os.access(X_OK):" not in out  # 那一项根本没当判定用


def test_main_on_windows_says_what_it_did_not_verify(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    install_tool(monkeypatch, tmp_path, windows=True)
    assert pc.main() == 0
    out = capsys.readouterr().out
    assert "真正的执行位由 CI 的 ubuntu job 覆盖" in out


def test_main_on_windows_runs_the_wrapper_through_sh(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    tool = install_tool(monkeypatch, tmp_path, windows=True)
    fake_sh = Path("/fake/sh")
    monkeypatch.setattr(pc, "find_git_sh", lambda: fake_sh)
    assert pc.main() == 0
    # 路径字符串是平台相关的，比较时统一过一遍 str(Path(...))
    assert tool.runs.calls[0][:2] == [str(fake_sh), str(tool.wrapper)]
    # 把 sh 所在目录加进 PATH：夹具里 impl 脚本调用的是 python，别的路径可能靠它
    env = tool.runs.envs[0]
    assert env is not None
    assert env["PATH"].split(os.pathsep)[0] == str(Path("/fake/sh").parent)
