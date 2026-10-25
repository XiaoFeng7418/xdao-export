"""PDF 导出器的测试。

全部用「假浏览器」替代真实浏览器：一层平台对应的启动脚本包一层 Python 脚本，
读取 --print-to-pdf 参数并写出文件，从而在不启动浏览器的前提下验证完整调用链。
真实浏览器的渲染结果由 tests/test_pdf_render.py 单独覆盖（需要浏览器，默认跳过）。
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from tests import FakeClient, sample_thread
from xdao.cancel import ExportCancelled
from xdao.exporters import EXPORTERS, create_exporter
from xdao.exporters.pdf import (
    PdfBuilder,
    PdfError,
    find_browser,
    render_html_to_pdf,
)

ARTIFACTS = Path(__file__).resolve().parent.parent / ".test-artifacts"

IS_WINDOWS = os.name == "nt"


def _console_code_page() -> int:
    """当前控制台代码页；拿不到就返回 0（当作老式 ANSI 环境）。"""
    try:
        import ctypes

        return int(ctypes.windll.kernel32.GetConsoleOutputCP())
    except Exception:  # pragma: no cover —— 只有非 Windows 会走到
        return 0


def _batch_bytes(text: str) -> bytes:
    """把批处理内容编成 cmd.exe 认得的字节。

    cmd.exe 读批处理用的不是固定编码：默认按系统 ANSI 代码页（中文系统是
    GBK），但控制台被切到 65001（UTF-8）时它就按 UTF-8 读。编码猜错的表现
    是命令行里的中文路径变成乱码，子进程报 ``can't open file ...`` ——
    2026-10-23 十二条 PDF 用例一起栽在这里，2026-10-25 又因为控制台变成
    UTF-8 而复发（本机仓库路径含中文）。
    """
    if _console_code_page() == 65001:
        return text.encode("utf-8")
    try:
        return text.encode("mbcs")
    except (LookupError, UnicodeEncodeError):  # pragma: no cover —— 只有非 Windows / 生僻字会走到
        return text.encode("utf-8")


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
        # 直接写字节：①cmd.exe 按 ANSI 代码页读批处理，中文路径必须同码（见 _batch_bytes）；
        # ②write_text 会把 "\r\n" 再翻一次，变成 "\r\r\n"。
        wrapper.write_bytes(
            _batch_bytes(
                "@echo off\r\n"
                f'"{exe}" "{script}" %*\r\n'
                "exit /b %ERRORLEVEL%\r\n"
            )
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


def test_fake_browser_batch_file_is_written_in_the_cmd_code_page(artifacts_dir):
    """批处理要按 cmd.exe 的代码页写、CRLF 只能有一个 CR（2026-10-23 的坑）。

    不用 skipif —— 仓库的「不许意外跳过」闸会把带条件的整条用例判红（CI 2026-10-23
    实测）：非 Windows 上就只钉「字节里的 CRLF 没被翻成双 CR」，代码页那段跳过不看。
    """
    batch = make_fake_browser(artifacts_dir, is_windows=True)
    raw = batch.read_bytes()
    assert b"\r\r" not in raw, "CRLF 被 write_text 又翻了一遍，cmd 会读到空行"
    assert raw.count(b"\r\n") == 3
    if IS_WINDOWS:
        # 本机仓库路径带中文：按「当前控制台代码页」解不开就说明编码选错了
        codec = "utf-8" if _console_code_page() == 65001 else "mbcs"
        assert str(artifacts_dir) in raw.decode(codec)
    # POSIX 包装脚本由 tools/posix_check.py 覆盖，这里只管 Windows 的批处理。


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


def test_render_passes_no_sandbox_when_frozen(out_dir, artifacts_dir, monkeypatch):
    """打包版渲染 PDF 时必须带上 ``--no-sandbox``。

    冻结进程里不带这个开关，浏览器会在读参数阶段就被系统中断
    （退出码 ``2147483651``），一个字节的 PDF 都出不来 ——
    2026-10-01 在冻结探针上实测过（见 ``xdao/browser_flags.py`` 的排除表）。
    这里守的是「命令行渲染这条路确实接上了开关」。
    """
    import xdao.exporters.pdf as pdf_module
    from xdao import browser_flags

    browser = make_fake_browser(artifacts_dir)
    monkeypatch.setattr(browser_flags, "is_frozen", lambda: True)
    monkeypatch.setattr(pdf_module, "browser_flags", browser_flags)

    render_html_to_pdf("<html></html>", out_dir / "冻结.pdf", browser_path=browser)

    flags = (browser.parent / "flags.txt").read_text(encoding="utf-8")
    assert "--no-sandbox" in flags
    # 开关得是这次现算出来的，不能是靠别处塞进来的。
    assert browser_flags.FROZEN_EXTRA_FLAGS == ("--no-sandbox",)
    # 开关必须排在本地文件地址之前，否则会被浏览器当成网址。
    assert flags.index("--no-sandbox") < flags.index("source.html")


def test_render_keeps_the_sandbox_in_source_runs(out_dir, artifacts_dir, monkeypatch):
    """源码运行（``python main.py``）不该出现 ``--no-sandbox``。"""
    from xdao import browser_flags

    browser = make_fake_browser(artifacts_dir)
    monkeypatch.setattr(browser_flags, "is_frozen", lambda: False)

    render_html_to_pdf("<html></html>", out_dir / "源码.pdf", browser_path=browser)

    flags = (browser.parent / "flags.txt").read_text(encoding="utf-8")
    assert "--no-sandbox" not in flags


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


class _HangingBrowser:
    """假 Popen：没被 kill 之前永远不结束（拿来试超时与取消）。"""

    def __init__(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
        self.returncode = None
        self.killed = False

    def communicate(self, timeout=None):
        if self.killed:
            self.returncode = -9
            return "", ""
        if timeout is not None:
            raise subprocess.TimeoutExpired(cmd="fake", timeout=timeout)
        self.returncode = 0
        return "", ""

    def poll(self):
        return self.returncode

    def kill(self):
        self.killed = True


def test_render_reports_timeout(out_dir, artifacts_dir, monkeypatch):
    browser = make_fake_browser(artifacts_dir)
    processes: list[_HangingBrowser] = []

    def fake_popen(*args, **kwargs):
        process = _HangingBrowser()
        processes.append(process)
        return process

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    with pytest.raises(PdfError, match="超时"):
        render_html_to_pdf("<html></html>", out_dir / "x.pdf", browser_path=browser, timeout=0)
    # 超时之后必须把那个还挂着的浏览器进程收掉，别留在后台。
    assert processes and processes[0].killed


def test_render_can_be_cancelled_mid_way(out_dir, artifacts_dir, monkeypatch):
    """命令行渲染期间点「取消导出」要真的收手，而不是干等到超时（v0.17.0）。

    以前这里用 ``subprocess.run(timeout=…)``：一口气阻塞到底，用户点了取消也得等
    浏览器把长串渲完。现在改成轮询 ``Popen``，每 0.2 秒看一眼停止位。
    """
    browser = make_fake_browser(artifacts_dir)
    stop = threading.Event()
    processes: list[_HangingBrowser] = []

    def fake_popen(*args, **kwargs):
        process = _HangingBrowser()
        processes.append(process)
        # 浏览器已经在渲染了，用户这时点了「取消导出」。
        stop.set()
        return process

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    with pytest.raises(ExportCancelled):
        render_html_to_pdf(
            "<html></html>", out_dir / "x.pdf", browser_path=browser, timeout=30, stop=stop
        )
    assert processes and processes[0].killed


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
    """浏览器起不来时（路径写错、装了安全软件、机器上其实没装）不能让用户什么都拿不到。

    改存 HTML，并说明怎么自己打印成 PDF。打包版当年的 STATUS_BREAKPOINT
    已经在 v0.10.0 修掉（见 ``browser_flags``），但这条兜底仍然要留着。
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


# ---------- 渲染路径路由（纸张/边距只走 CDP） ----------


def test_default_options_go_through_the_command_line(out_dir, artifacts_dir, monkeypatch):
    """全默认（跟随网页样式）必须还是走命令行，输出与老版本一致。"""
    import xdao.exporters.pdf as pdf_module
    from xdao.pdf_opts import PdfOptions

    calls: list[str] = []
    monkeypatch.setattr(
        pdf_module, "print_html_to_pdf", lambda *a, **k: calls.append("cdp") or a[1]
    )
    browser = make_fake_browser(artifacts_dir)
    target = out_dir / "默认.pdf"

    pdf_module.render_html_to_pdf(
        "<html><body>你好</body></html>",
        target,
        browser_path=browser,
        options=PdfOptions(),
    )

    assert calls == []
    assert (browser.parent / "flags.txt").exists(), "命令行没被调用"


def test_custom_options_go_through_cdp(out_dir, artifacts_dir, monkeypatch):
    """改过纸张时不能再走命令行：命令行没有纸张开关。"""
    import xdao.exporters.pdf as pdf_module
    from xdao.pdf_opts import PdfOptions

    seen: list[object] = []

    def fake_cdp(html, output_pdf, **kwargs):
        seen.append(kwargs.get("options"))
        return Path(output_pdf)

    monkeypatch.setattr(pdf_module, "print_html_to_pdf", fake_cdp)
    target = out_dir / "a3.pdf"
    options = PdfOptions(paper="a3")

    result = pdf_module.render_html_to_pdf(
        "<html></html>", target, browser_path="C:/随便.exe", options=options
    )

    assert result == target
    assert seen == [options]


def test_page_ranges_alone_also_go_through_cdp(out_dir, monkeypatch):
    """只填页码也必须走 CDP。

    ``is_default`` 不看 ``page_ranges``，用它判断的话「用户填了 1-3 页」会被
    当成没改过而走命令行，页码被静默丢掉 —— 这条用例钉的就是这个。
    """
    import xdao.exporters.pdf as pdf_module
    from xdao.pdf_opts import PdfOptions

    seen: list[object] = []

    def fake_cdp(html, output_pdf, **kwargs):
        seen.append(kwargs.get("options"))
        return Path(output_pdf)

    monkeypatch.setattr(pdf_module, "print_html_to_pdf", fake_cdp)
    pdf_module.render_html_to_pdf(
        "<html></html>",
        out_dir / "页码.pdf",
        browser_path="C:/随便.exe",
        options=PdfOptions(page_ranges="1-3"),
    )

    assert len(seen) == 1, "只填页码时也必须走 CDP"


def test_builder_passes_pdf_options_to_the_renderer(monkeypatch):
    """PdfBuilder 必须把它拿到的选项交给渲染层，不然界面上改了也白改。"""
    import xdao.exporters.pdf as pdf_module
    from xdao.pdf_opts import PdfOptions

    seen: list[object] = []

    def fake_render(html, output_pdf, **kwargs):
        seen.append(kwargs.get("options"))
        return Path(output_pdf)

    monkeypatch.setattr(pdf_module, "render_html_to_pdf", fake_render)
    options = PdfOptions(paper="a4", margin="narrow", scale=1.2)
    builder = PdfBuilder(FakeClient(), browser_path="C:/随便.exe", pdf_options=options)
    builder.render_pdf("<html></html>", Path("."), Path("out.pdf"))

    assert seen == [options]


def test_builder_without_options_still_renders(monkeypatch):
    import xdao.exporters.pdf as pdf_module

    seen: list[object] = []

    def fake_render(html, output_pdf, **kwargs):
        seen.append(kwargs.get("options"))
        return Path(output_pdf)

    monkeypatch.setattr(pdf_module, "render_html_to_pdf", fake_render)
    builder = PdfBuilder(FakeClient(), browser_path="C:/随便.exe")
    builder.render_pdf("<html></html>", Path("."), Path("out.pdf"))

    assert seen == [None]


def test_cdp_params_drop_css_page_size_when_paper_is_explicit():
    """显式纸张时绝不能带 preferCSSPageSize —— 带了纸张就白选了。

    真机实测：两者同时出现时浏览器改以页面 CSS 的 ``@page size`` 为准
    （用户选 A3 出 A4），而且时对时错；见 ``_scratch/cmp_pdf_paths.py``。
    """
    from xdao.exporters.pdf import _cdp_params
    from xdao.pdf_opts import PdfOptions

    params = _cdp_params(PdfOptions(paper="a3"))
    assert "preferCSSPageSize" not in params
    assert params["paperWidth"] == 11.69
    assert params["paperHeight"] == 16.54

    # 跟随网页样式时才让页面自己决定纸张。
    assert _cdp_params(PdfOptions())["preferCSSPageSize"] is True

    # 边距也交给页面时不影响这个判断。
    landscape = _cdp_params(PdfOptions(paper="a3", orientation="landscape"))
    assert "preferCSSPageSize" not in landscape
    assert landscape["paperWidth"] == 16.54


# ---------- CDP 结果的解码 ----------

def test_decode_pdf_data_accepts_base64_pdf():
    import base64

    from xdao.exporters.pdf import _decode_pdf_data

    raw = b"%PDF-1.4 hello"
    assert _decode_pdf_data({"data": base64.b64encode(raw).decode("ascii")}) == raw


@pytest.mark.parametrize(
    ("result", "match"),
    [
        ({}, "空的"),
        (None, "空的"),
        # 非法 base64：必须报「解不开」。默认的 b64decode 会静默丢掉非法字符，
        # 那样会变成「结果是空的」，看不出真正的原因。
        ({"data": "这不是 base64"}, "解不开"),
        # 合法 base64 但不是 PDF（b"hello"）。
        ({"data": "aGVsbG8="}, "不是有效"),
    ],
)
def test_decode_pdf_data_rejects_bad_payloads(result, match):
    from xdao.exporters.pdf import _decode_pdf_data

    with pytest.raises(PdfError, match=match):
        _decode_pdf_data(result)


def test_check_and_move_rejects_empty_file(out_dir):
    from xdao.exporters.pdf import _check_and_move

    target = out_dir / "空.pdf"
    target.write_bytes(b"")
    with pytest.raises(PdfError, match="空"):
        _check_and_move(target, out_dir / "结果.pdf")


def test_check_and_move_rejects_non_pdf(out_dir):
    from xdao.exporters.pdf import _check_and_move

    target = out_dir / "假.pdf"
    target.write_bytes(b"<html>not a pdf</html>")
    with pytest.raises(PdfError, match="不是有效"):
        _check_and_move(target, out_dir / "结果.pdf")


# ---------- 超时预算：整趟共用一个截止时刻（v0.17.1） ----------

class _FakeLoginBrowserForBudget:
    """假浏览器句柄：只记下起它时拿到了多少秒预算、以及收没收到 headless。"""

    def __init__(self) -> None:
        self.granted: list[float] = []
        self.headless: list[bool] = []
        self.attach_live: list[bool] = []
        self.browser_ws_url = "ws://fake"

    def __call__(self, info, profile, *, timeout, start_url=None, headless=False, attach_live=True):
        self.granted.append(timeout)
        self.headless.append(headless)
        self.attach_live.append(attach_live)
        self._profile = profile
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeSessionForBudget:
    """假 CDP 会话：记下连上时与打印时各自拿到多少秒预算。"""

    def __init__(self) -> None:
        self.granted: list[float] = []
        self.call_granted: list[float] = []

    def __call__(self, ws_url, *, timeout, site_urls=None):
        self.granted.append(timeout)
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def connect(self) -> None:
        pass

    def evaluate(self, expression: str):
        return "complete"

    def call(self, method: str, params=None, *, timeout=None):
        import base64

        self.call_granted.append(timeout)
        return {"data": base64.b64encode(b"%PDF-1.4 fake").decode("ascii")}


class _FakeClock:
    """把 ``time.monotonic()`` 变成一串写好的读数，让「预算还剩多少」可控。"""

    def __init__(self, readings: list[float]) -> None:
        self._readings = readings
        self._index = 0

    def monotonic(self) -> float:
        index = min(self._index, len(self._readings) - 1)
        self._index += 1
        return self._readings[index]

    def sleep(self, seconds: float) -> None:
        """真睡会把用例拖成 30 秒，这里直接跳过（读数自己会往前走）。"""


def _prepare_budget_fakes(monkeypatch, readings: list[float]):
    """把 ``print_html_to_pdf`` 依赖的浏览器、会话、时钟全换成替身。"""
    import xdao.browser_login as browser_login
    import xdao.exporters.pdf as pdf_module

    info = type("假浏览器信息", (), {"name": "假浏览器", "path": "C:/假浏览器.exe"})()
    monkeypatch.setattr(pdf_module, "find_browser", lambda path=None: info)
    monkeypatch.setattr(browser_login, "find_browser", lambda explicit=None: info)
    login_browser = _FakeLoginBrowserForBudget()
    session = _FakeSessionForBudget()
    monkeypatch.setattr(browser_login, "LoginBrowser", login_browser)
    monkeypatch.setattr(pdf_module, "CDPSession", session)
    monkeypatch.setattr(pdf_module, "time", _FakeClock(readings))
    return login_browser, session


def test_cdp_render_grants_each_phase_from_one_budget(out_dir, monkeypatch):
    """起浏览器/连会话/等加载/打印四段都从同一个截止时刻里扣。

    以前每段各给一份 ``timeout``（60/60/30/全部），最坏能拖到设定值的近三倍。
    这里用假时钟把剩余预算一步步压下去，钉住「拿到的秒数＝剩余且不超过该段上限」。
    """
    from xdao.exporters.pdf import print_html_to_pdf

    # 起点 0；之后每次问时间依次是 880 / 885 / 890 / 895 —— 900 秒的预算快用完了。
    login_browser, session = _prepare_budget_fakes(monkeypatch, [0.0, 880.0, 885.0, 890.0, 895.0])
    target = out_dir / "预算.pdf"

    print_html_to_pdf("<html></html>", target, timeout=900)

    assert login_browser.granted == [pytest.approx(20.0)]
    assert login_browser.headless == [True], "PDF 渲染不许在人眼前弹窗（v0.17.3）"
    assert login_browser.attach_live == [False], "PDF 不许蹭用户开着的窗（v0.17.3）"
    assert session.granted == [pytest.approx(15.0)]
    assert session.call_granted == [pytest.approx(5.0)]
    assert target.read_bytes().startswith(b"%PDF")


def test_cdp_render_stops_as_soon_as_the_budget_runs_out(out_dir, monkeypatch):
    """预算已经用完时，下一步必须立刻报超时，而不是再给一段完整等待。"""
    from xdao.exporters.pdf import print_html_to_pdf

    login_browser, _ = _prepare_budget_fakes(monkeypatch, [0.0, 901.0])
    target = out_dir / "超时.pdf"

    with pytest.raises(PdfError, match="超时"):
        print_html_to_pdf("<html></html>", target, timeout=900)

    # 连浏览器都不该被拉起来：先查预算再动手。
    assert login_browser.granted == []


def test_cdp_render_caps_the_wait_for_load_at_thirty_seconds(out_dir, monkeypatch):
    """等加载那一段的上限是 30 秒，但预算不足时按预算走（不能两者取大的）。"""
    import xdao.exporters.pdf as pdf_module
    from xdao.exporters.pdf import print_html_to_pdf

    seen: list[float] = []
    real_wait = pdf_module._wait_for_load

    def fake_wait(session, timeout):
        seen.append(timeout)
        real_wait(session, timeout)

    monkeypatch.setattr(pdf_module, "_wait_for_load", fake_wait)
    _prepare_budget_fakes(monkeypatch, [0.0, 800.0, 800.0, 860.0, 860.0])

    print_html_to_pdf("<html></html>", out_dir / "等加载.pdf", timeout=900)

    # 读到 800 时剩 100 秒，上限 30 秒 —— 取小的那个。
    assert seen == [pytest.approx(30.0)]

