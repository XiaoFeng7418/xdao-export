"""PDF 导出器的测试。

全部用「假浏览器」替代真实浏览器：一个 .cmd 包一层 Python 脚本，
读取 --print-to-pdf 参数并写出文件，从而在不启动浏览器的前提下验证完整调用链。
真实浏览器的渲染结果由 tests/test_pdf_render.py 单独覆盖（需要浏览器，默认跳过）。
"""

from __future__ import annotations

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


@pytest.fixture
def out_dir() -> Path:
    import shutil

    path = ARTIFACTS / "pdf"
    shutil.rmtree(path, ignore_errors=True)
    path.mkdir(parents=True, exist_ok=True)
    return path


def make_fake_browser(
    artifacts_dir: Path, *, write_pdf: bool = True, content: bytes = b"%PDF-1.4 fake"
) -> Path:
    """造一个假的浏览器可执行文件，返回 .cmd 的路径。

    参数会记录到同目录的 flags.txt，供用例断言"到底传了什么给浏览器"。
    """
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
    wrapper = artifacts_dir / "fake_browser.cmd"
    wrapper.write_text(
        "@echo off\r\n" f'"{sys.executable}" "{script}" %*\r\n' "exit /b %ERRORLEVEL%\r\n",
        encoding="utf-8",
    )
    return wrapper


# ---------- find_browser ----------


def test_find_browser_uses_explicit_path(artifacts_dir):
    exe = artifacts_dir / "mybrowser.exe"
    exe.write_bytes(b"MZ")
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

    first = artifacts_dir / "chrome.exe"
    second = artifacts_dir / "msedge.exe"
    first.write_bytes(b"MZ")
    second.write_bytes(b"MZ")
    monkeypatch.setattr(pdf_module, "BROWSER_CANDIDATES", (str(first), str(second)))
    info = find_browser()
    assert info.path == first
    assert info.name == "Chrome"


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
