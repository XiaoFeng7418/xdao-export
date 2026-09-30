"""PDF 导出器的测试。

全部用「假浏览器」替代真实浏览器：一层平台对应的启动脚本包一层 Python 脚本，
读取 --print-to-pdf 参数并写出文件，从而在不启动浏览器的前提下验证完整调用链。
真实浏览器的渲染结果由 tests/test_pdf_render.py 单独覆盖（需要浏览器，默认跳过）。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests import FakeClient, sample_thread
from xdao.exporters import EXPORTERS, create_exporter
from xdao.exporters.pdf import (
    PdfBuilder,
    PdfError,
    find_browser,
    render_html_to_pdf,
)

ARTIFACTS = Path(__file__).resolve().parent.parent / ".test-artifacts"

IS_WINDOWS = os.name == "nt"


@pytest.fixture
def out_dir() -> Path:
    import shutil

    path = ARTIFACTS / "pdf"
    shutil.rmtree(path, ignore_errors=True)
    path.mkdir(parents=True, exist_ok=True)
    return path


def make_fake_exe(directory: Path, stem: str) -> Path:
    """造一个「看起来像可执行文件」的空壳，跨平台可用。

    Windows 要 .exe 后缀，POSIX 上只要求有执行位（真正的可执行性由
    render_html_to_pdf 的返回码校验兜住）。
    """
    if IS_WINDOWS:
        path = directory / f"{stem}.exe"
        path.write_bytes(b"MZ")
    else:
        path = directory / stem
        path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        path.chmod(0o755)
    return path


def make_fake_browser(
    artifacts_dir: Path,
    *,
    write_pdf: bool = True,
    content: bytes = b"%PDF-1.4 fake",
    is_windows: bool | None = None,
    python_exe: str | None = None,
    shell_stem: str = "sh",
) -> Path:
    """造一个假的浏览器可执行文件，返回可执行包装脚本的路径。

    Windows 上是 .cmd 批处理，其他平台上是带执行位的 sh 脚本 —— 早先只写
    .cmd，导致 Linux 上 subprocess 直接 PermissionError（CI 抓到的就是它）。

    参数会记录到同目录的 flags.txt，供用例断言"到底传了什么给浏览器"。
    ``is_windows`` / ``python_exe`` 允许在 Windows 上验证 POSIX 分支
    （见 tools/posix_check.py）。
    """
    if is_windows is None:
        is_windows = IS_WINDOWS
    exe = python_exe or sys.executable
    if shell_stem != "sh":
        # POSIX shebang 要按文件名找解释器，Windows 上验证时指向 Git 自带的 sh。
        exe_posix = str(exe).replace("\\", "/")
        interpreter = f"#!{exe_posix}\n"
    else:
        interpreter = "#!/bin/sh\n"

    script = artifacts_dir / "fake_browser_impl.py"
    flags_file = artifacts_dir / "flags.txt"
    script.write_text(
        "import sys, pathlib\n"
        "args = sys.argv[1:]\n"
        "target = None\n"
        "for a in args:\n"
        "    if a.startswith('--print-to-pdf='):\n"
        "        target = a.split('=', 1)[1]\n"
        f"pathlib.Path({str(flags_file)!r}).write_text('\\n'.join(args), encoding='utf-8')\n"
        f"WRITE = {write_pdf!r}\n"
        f"CONTENT = {content!r}\n"
        "if WRITE and target:\n"
        "    pathlib.Path(target).write_bytes(CONTENT)\n"
        "sys.exit(0)\n",
        encoding="utf-8",
    )
    if is_windows:
        wrapper = artifacts_dir / "fake_browser.cmd"
        wrapper.write_text(
            "@echo off\r\n" f'"{exe}" "{script}" %*\r\n' "exit /b %ERRORLEVEL%\r\n",
            encoding="utf-8",
        )
    else:
        wrapper = artifacts_dir / "fake_browser.sh"
        # 解释器路径可能带空格，用引号包住；SCRIPT 由 shell 原样展开。
        wrapper.write_text(
            interpreter + f'exec "{exe}" "{script}" "$@"\n',
            encoding="utf-8",
        )
        wrapper.chmod(0o755)
    return wrapper


# ---------- find_browser ----------


def test_find_browser_uses_explicit_path(artifacts_dir):
    exe = make_fake_exe(artifacts_dir, "mybrowser")
    info = find_browser(exe)
    assert info.path == exe
    assert "mybrowser" in info.name


def test_find_browser_rejects_missing_path(artifacts_dir):
    with pytest.raises(PdfError, match="不存在"):
        find_browser(artifacts_dir / "没有这个.exe")


def test_find_browser_reports_when_none_installed(monkeypatch):
    import xdao.exporters.pdf as pdf_module

    monkeypatch.setattr(pdf_module, "BROWSER_CANDIDATES", ())
    with pytest.raises(PdfError, match="Chrome 或 Edge"):
        find_browser()


def test_find_browser_prefers_first_candidate(monkeypatch, artifacts_dir):
    import xdao.exporters.pdf as pdf_module

    first = make_fake_exe(artifacts_dir, "chrome")
    second = make_fake_exe(artifacts_dir, "msedge")
    monkeypatch.setattr(pdf_module, "BROWSER_CANDIDATES", (str(first), str(second)))
    info = find_browser()
    assert info.path == first
    assert info.name == "Chrome"


def test_fake_browser_is_actually_executable(artifacts_dir):
    """守住这次 CI 抓到的坑：造出来的假浏览器必须真能被执行。"""
    browser = make_fake_browser(artifacts_dir)
    assert browser.exists()
    completed = subprocess.run(
        [str(browser), "--print-to-pdf=" + str(artifacts_dir / "probe.pdf")],
        capture_output=True,
        timeout=60,
    )
    assert completed.returncode == 0, completed.stderr.decode("utf-8", "replace")
    assert (artifacts_dir / "flags.txt").exists()


# ---------- render_html_to_pdf ----------


def test_render_writes_pdf_and_passes_expected_flags(out_dir, artifacts_dir):
    browser = make_fake_browser(artifacts_dir)
    target = out_dir / "结果.pdf"

    messages: list[str] = []
    result = render_html_to_pdf(
        "<html><body>你好</body></html>",
        target,
        browser_path=browser,
        progress=messages.append,
    )

    assert result == target
    assert target.read_bytes().startswith(b"%PDF")

    # 校验传给了浏览器的参数
    args_file = browser.parent / "flags.txt"
    flags = args_file.read_text(encoding="utf-8")
    assert "--headless=new" in flags
    assert "--print-to-pdf-no-header" in flags
    assert "--user-data-dir=" in flags
    assert "--print-to-pdf=" in flags
    assert "source.html" in flags
    # 临时文件必须用 ASCII 名，避免浏览器处理中文路径出问题
    assert "结果" not in flags
    assert any("渲染" in m for m in messages)
    assert any("完成" in m for m in messages)


def test_render_works_in_chinese_directory(out_dir, artifacts_dir):
    """导出目录与文件名都含中文时，临时文件名仍应是 ASCII，且结果落到正确位置。"""
    chinese_dir = out_dir / "中文目录"
    chinese_dir.mkdir(parents=True, exist_ok=True)
    browser = make_fake_browser(artifacts_dir)
    target = chinese_dir / "中文文件名.pdf"

    render_html_to_pdf("<html></html>", target, browser_path=browser)

    flags = (browser.parent / "flags.txt").read_text(encoding="utf-8")
    # 交给浏览器的临时文件名必须是 ASCII（路径可能含中文，那是导出目录本身）
    assert "source.html" in flags
    assert "output.pdf" in flags
    assert "中文文件名.pdf" not in flags
    # 最终文件用中文名落到用户指定的位置
    assert target.exists()
    assert target.read_bytes().startswith(b"%PDF")


def test_render_reports_missing_output(out_dir, artifacts_dir):
    browser = make_fake_browser(artifacts_dir, write_pdf=False)
    with pytest.raises(PdfError, match="没有生成 PDF"):
        render_html_to_pdf("<html></html>", out_dir / "x.pdf", browser_path=browser)


def test_render_rejects_non_pdf_output(out_dir, artifacts_dir):
    browser = make_fake_browser(artifacts_dir, content=b"<html>error page</html>")
    with pytest.raises(PdfError, match="不是有效的 PDF"):
        render_html_to_pdf("<html></html>", out_dir / "x.pdf", browser_path=browser)


def test_render_reports_timeout(out_dir, artifacts_dir, monkeypatch):
    browser = make_fake_browser(artifacts_dir)

    def fake_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="fake", timeout=1)

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(PdfError, match="超时"):
        render_html_to_pdf("<html></html>", out_dir / "x.pdf", browser_path=browser, timeout=1)


def test_render_cleans_up_temp_dir(out_dir, artifacts_dir):
    browser = make_fake_browser(artifacts_dir)
    render_html_to_pdf("<html></html>", out_dir / "x.pdf", browser_path=browser)
    leftovers = [p for p in out_dir.iterdir() if p.name.startswith(".xdao-pdf-")]
    assert leftovers == []


def test_render_overwrites_existing_pdf(out_dir, artifacts_dir):
    browser = make_fake_browser(artifacts_dir)
    target = out_dir / "same.pdf"
    target.write_bytes(b"old content")
    render_html_to_pdf("<html></html>", target, browser_path=browser)
    assert target.read_bytes().startswith(b"%PDF")


# ---------- PdfBuilder ----------


def test_builder_save_uses_template_and_embeds_images(out_dir, artifacts_dir):
    browser = make_fake_browser(artifacts_dir)
    builder = PdfBuilder(
        FakeClient(), filename_template="[{id}] {title}", browser_path=str(browser)
    )
    path = builder.save(sample_thread("标/题"), "all", out_dir)

    assert path.name == "[1001] 标_题.pdf"
    assert path.read_bytes().startswith(b"%PDF")

    # 交给浏览器的 HTML 里图片应当是内嵌的
    source = None
    for candidate in out_dir.rglob("source.html"):
        source = candidate
    # 临时目录已被清理，改从参数文件确认调用发生过
    assert (browser.parent / "flags.txt").exists()


def test_builder_output_name_without_template(out_dir, artifacts_dir):
    browser = make_fake_browser(artifacts_dir)
    builder = PdfBuilder(FakeClient(), browser_path=str(browser))
    assert builder.output_name(sample_thread("我的标题")) == "我的标题"


def test_builder_checks_directory_writable(out_dir, monkeypatch):
    from xdao.exporters import OutputDirNotWritable

    builder = PdfBuilder(FakeClient(), browser_path="不存在的浏览器.exe")
    monkeypatch.setattr(
        "xdao.exporters.pdf.ensure_writable",
        lambda *a, **k: (_ for _ in ()).throw(OutputDirNotWritable("目录不可写（测试）")),
    )
    with pytest.raises(OutputDirNotWritable):
        builder.save(sample_thread(), "all", out_dir)


def test_create_exporter_supports_pdf(out_dir, artifacts_dir):
    browser = make_fake_browser(artifacts_dir)
    exporter = create_exporter(
        "pdf", FakeClient(), filename_template="{id}", browser_path=str(browser)
    )
    path = exporter.save(sample_thread(), "all", out_dir)
    assert path.suffix == ".pdf"
    assert path.name == "1001.pdf"


def test_registry_pdf_entry_exists():
    assert "pdf" in EXPORTERS
    assert EXPORTERS["pdf"][2] is PdfBuilder


# ---------- 浏览器启不来时的降级 ----------


def test_save_falls_back_to_html_when_browser_fails(out_dir, monkeypatch):
    """打包版里浏览器可能起不来（STATUS_BREAKPOINT）。

    这种情况下不能让用户什么都拿不到：改存 HTML，并说明怎么自己打印成 PDF。
    """
    builder = PdfBuilder(FakeClient(), browser_path="C:/不存在.exe")

    with pytest.raises(PdfError):
        # 先确认不允许降级时确实会抛错
        PdfBuilder(FakeClient(), browser_path="C:/不存在.exe", fallback_html=False).save(
            sample_thread("降级测试"), "all", out_dir
        )

    path = builder.save(sample_thread("降级测试"), "all", out_dir)
    assert path.suffix == ".html"
    assert path.exists()
    assert path.read_text(encoding="utf-8").startswith("<!DOCTYPE html>")
    assert "降级测试.html" in builder.fallback_note
    assert "Ctrl+P" in builder.fallback_note


def test_fallback_reports_via_progress(out_dir):
    messages: list[str] = []
    builder = PdfBuilder(
        FakeClient(), progress=messages.append, browser_path="C:/不存在.exe"
    )
    builder.save(sample_thread(), "all", out_dir)
    assert any("改为保存 HTML" in m for m in messages)


def test_browser_launch_failure_hint_mentions_breakpoint():
    from xdao.exporters.pdf import browser_launch_failure_hint

    hint = browser_launch_failure_hint(0x80000003)
    assert "STATUS_BREAKPOINT" in hint
    assert "HTML" in hint


def test_browser_launch_failure_hint_for_other_codes():
    from xdao.exporters.pdf import browser_launch_failure_hint

    assert "STATUS_BREAKPOINT" not in browser_launch_failure_hint(1)


def test_is_frozen_reflects_interpreter(monkeypatch):
    import xdao.exporters.pdf as pdf_module

    monkeypatch.delattr(pdf_module.sys, "frozen", raising=False)
    assert pdf_module.is_frozen() is False
    monkeypatch.setattr(pdf_module.sys, "frozen", True, raising=False)
    assert pdf_module.is_frozen() is True
