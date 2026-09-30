"""PDF 导出器：调用本机浏览器的无头模式把 HTML 渲染成 PDF。

思路：先复用 :class:`~xdao.exporters.html.HtmlBuilder` 生成**图片已内嵌**的单文件 HTML，
再交给无头浏览器打印成 PDF。这样 PDF 完全离线可用，不依赖网络。

两条渲染路径：

1. **命令行**（``--print-to-pdf``）：默认走这条。命令行的打印选项是写死的，
   输出与老版本逐字节一致，纸张沿用网页自己的 ``@page`` 样式。
2. **CDP**（``Page.printToPDF``）：用户改了纸张/页边距/方向/缩放/背景/页码范围时走这条
   —— 命令行没有这些开关，只有 CDP 能设。

为什么用浏览器而不是自己排版：中文字体、长文分页、图片缩放这些交给浏览器最稳妥，
也不必引入任何第三方依赖（项目一直保持纯标准库）。

已知限制：

- 需要本机装有 Chrome 或 Edge（Windows 上基本都有）；都没有时给出明确提示；
- 内容很多的串（例如内嵌上百张图）渲染会比较慢，且 PDF 体积可能很大；
- 页眉页脚由 `--print-to-pdf-no-header` 关闭。
"""

from __future__ import annotations

import base64
import os
import shutil
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from .. import browser_flags
from ..browser_flags import launch_flags
from ..cdp import CDPSession, CdpError
from ..client import XdaoClient
from ..pdf_opts import DEFAULT_PAPER, PdfOptions
from ._shared import (
    ThreadData,
    derive_filename,
    ensure_writable,
    render_filename,
    sanitize_filename,
)
from .html import HtmlBuilder

# 常见安装位置，按顺序探测（用户自己装的优先于系统自带的 Edge）。
BROWSER_CANDIDATES = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
)

# 单次渲染的等待上限。图多的大串需要更久，留足余量。
DEFAULT_TIMEOUT = 900


class PdfError(Exception):
    """PDF 导出失败。"""


@dataclass
class BrowserInfo:
    path: Path
    name: str

    def __str__(self) -> str:
        return f"{self.name}（{self.path}）"


def find_browser(explicit: str | Path | None = None) -> BrowserInfo:
    """找出可用的浏览器。显式指定的路径优先。"""
    if explicit:
        path = Path(explicit)
        if not path.exists():
            raise PdfError(f"指定的浏览器不存在：{path}")
        return BrowserInfo(path=path, name=path.stem)

    for raw in BROWSER_CANDIDATES:
        path = Path(raw)
        if path.exists():
            name = "Edge" if "msedge" in path.name.lower() else "Chrome"
            return BrowserInfo(path=path, name=name)
    raise PdfError(
        "没有找到 Chrome 或 Edge，无法导出 PDF。\n"
        "可以改用 HTML 格式（浏览器里打开后按 Ctrl+P 也能另存为 PDF），"
        "或在设置里指定浏览器的完整路径。"
    )


def is_frozen() -> bool:
    """当前是否运行在 PyInstaller 打好的可执行文件里。

    判据只有一处（``browser_flags``），免得两处各写一份、哪天改歪一处。
    """
    return browser_flags.is_frozen()


def browser_launch_failure_hint(returncode: int) -> str:
    """针对浏览器启动失败给出更有用的解释。

    实测：打包版里的 Chrome/Edge 会在读参数阶段被系统中断
    （``STATUS_BREAKPOINT``，0x80000003）。v0.10.0 起冻结环境会自动补
    ``--no-sandbox`` 绕开它（见 ``browser_flags``），所以这个提示只在
    「补了开关还是起不来」时才出现 —— 那种情况多半是浏览器路径不对、
    或者安全软件拦下了进程。
    """
    hint = ""
    unsigned = returncode & 0xFFFFFFFF
    if unsigned == 0x80000003:
        hint = "（浏览器被系统中断：STATUS_BREAKPOINT）"
    elif unsigned == 0x80000004:
        hint = "（浏览器被系统中断：STATUS_SINGLE_STEP）"
    lines = [f"可以改用 HTML 格式，或在设置里换一个浏览器路径。{hint}"]
    if is_frozen():
        lines.append(
            "另外：打包版启动浏览器时如果被安全软件拦下，"
            "可以改用 HTML 格式，或从源码运行（python main.py -f pdf）。"
        )
    return "\n" + "\n".join(lines)


def render_html_to_pdf(
    html: str,
    output_pdf: Path | str,
    *,
    browser_path: str | Path | None = None,
    options: PdfOptions | None = None,
    timeout: int = DEFAULT_TIMEOUT,
    progress=None,
    work_dir: Path | None = None,
) -> Path:
    """把一段 HTML 渲染成 PDF，返回写入的路径。

    这是模块级的实现，:class:`PdfBuilder` 只是把它和 HTML 生成串起来，
    这样渲染部分可以单独调用与测试。

    走哪条路取决于 ``options``：没给、或者「旧命令行能表达的那些项都没动过」时走命令行，
    输出与老版本逐字节一致；用户真改了纸张/边距/缩放，或者只填了页码时改走 CDP。

    判断用的是 :attr:`PdfOptions.needs_cdp` 而**不是** ``is_default``：
    ``is_default`` 不看 ``page_ranges``，只填页码时会误判成「没改过」，
    于是用户填的页码范围被静默丢掉。
    """
    if options is None or not options.needs_cdp:
        return _render_with_command_line(
            html,
            output_pdf,
            browser_path=browser_path,
            timeout=timeout,
            progress=progress,
            work_dir=work_dir,
        )
    return print_html_to_pdf(
        html,
        output_pdf,
        browser_path=browser_path,
        options=options,
        timeout=timeout,
        progress=progress,
        work_dir=work_dir,
    )


def _prepare_staging(
    html: str, output_pdf: Path, work_dir: Path | None
) -> tuple[Path, Path, Path]:
    """开一个临时目录，把 HTML 写进去。

    浏览器对非 ASCII 路径支持不稳定，临时文件统一用 ASCII 名。
    返回 ``(temp_dir, source, profile)``。
    """
    token = uuid.uuid4().hex[:8]
    staging = Path(work_dir) if work_dir else output_pdf.parent
    staging.mkdir(parents=True, exist_ok=True)
    temp_dir = staging / f".xdao-pdf-{token}"
    temp_dir.mkdir(parents=True, exist_ok=True)
    source = temp_dir / "source.html"
    profile = temp_dir / "profile"
    profile.mkdir(exist_ok=True)
    source.write_text(html, encoding="utf-8")
    return temp_dir, source, profile


def _check_and_move(target: Path, output_pdf: Path) -> None:
    """校验产物确实是 PDF，再挪到最终位置。"""
    if not target.exists() or target.stat().st_size == 0:
        raise PdfError("渲染结果为空，没有生成 PDF。")
    with target.open("rb") as handle:
        if handle.read(4) != b"%PDF":
            raise PdfError("渲染结果不是有效的 PDF 文件")
    if output_pdf.exists():
        output_pdf.unlink()
    shutil.move(str(target), str(output_pdf))


def _render_with_command_line(
    html: str,
    output_pdf: Path | str,
    *,
    browser_path: str | Path | None = None,
    timeout: int = DEFAULT_TIMEOUT,
    progress=None,
    work_dir: Path | None = None,
) -> Path:
    """命令行渲染路径（``--print-to-pdf``）：默认走这条，纸张沿用网页样式。"""
    output_pdf = Path(output_pdf)
    browser = find_browser(browser_path)
    if progress:
        progress(f"正在用 {browser.name} 渲染 PDF…")

    temp_dir, source, profile = _prepare_staging(html, output_pdf, work_dir)
    target = temp_dir / "output.pdf"
    try:
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
            # 打包版必须补的开关（冻结环境里浏览器会被系统中断，见 browser_flags 的说明）。
            *launch_flags(),
            source.as_uri(),
        ]
        # 浏览器不要继承本进程的 Tcl/Tk 变量：打包版会设它们，指向与系统版本
        # 不匹配的 DLL，子进程加载后会直接崩掉。
        env = dict(os.environ)
        for key in ("TCL_LIBRARY", "TK_LIBRARY"):
            env.pop(key, None)

        started = time.monotonic()
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                env=env,
            )
        except subprocess.TimeoutExpired as exc:
            raise PdfError(
                f"渲染 PDF 超时（超过 {timeout} 秒）。"
                "串太长或图片太多时可能发生，可以改用 HTML 格式。"
            ) from exc

        if not target.exists() or target.stat().st_size == 0:
            detail = (completed.stderr or completed.stdout or "").strip()[-400:]
            raise PdfError(
                f"{browser.name} 没有生成 PDF（退出码 {completed.returncode}）。\n{detail}{browser_launch_failure_hint(completed.returncode)}"
            )

        # 校验确实是 PDF，避免把错误页当成结果交出去。
        with target.open("rb") as handle:
            if handle.read(4) != b"%PDF":
                raise PdfError("渲染结果不是有效的 PDF 文件")

        if output_pdf.exists():
            output_pdf.unlink()
        shutil.move(str(target), str(output_pdf))
        if progress:
            elapsed = time.monotonic() - started
            progress(
                f"PDF 完成：{output_pdf.name}"
                f"（{output_pdf.stat().st_size / 1024 / 1024:.1f} MB，用时 {elapsed:.0f} 秒）"
            )
        return output_pdf
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def print_html_to_pdf(
    html: str,
    output_pdf: Path | str,
    *,
    browser_path: str | Path | None = None,
    options: PdfOptions | None = None,
    timeout: int = DEFAULT_TIMEOUT,
    progress=None,
    work_dir: Path | None = None,
) -> Path:
    """用 CDP 的 ``Page.printToPDF`` 渲染：只有这条路能设纸张与页边距。

    为什么不用命令行：``--print-to-pdf`` 没有任何纸张/边距开关，选项只能从 CDP 传。
    走 CDP 要先起浏览器（复用浏览器登录那条 ``LoginBrowser``），再连上它的调试端口；
    页面里的 ``document.readyState`` 变成 ``complete`` 之后才打印，否则会打出空白页。
    """
    from ..browser_login import LoginBrowser, find_browser as find_login_browser

    output_pdf = Path(output_pdf)
    opts = options or PdfOptions()
    info = find_browser(browser_path)
    started = time.monotonic()
    if progress:
        progress(f"正在用 {info.name} 渲染 PDF（{opts.describe()}）…")

    temp_dir, source, profile = _prepare_staging(html, output_pdf, work_dir)
    target = temp_dir / "output.pdf"
    try:
        # 用登录模块的浏览器句柄：它已经把「等调试端口 → 独立 profile → 不弹黑框」
        # 这些实战细节做完了，PDF 渲染没必要再实现一遍。
        login_info = find_login_browser(explicit=str(info.path))
        if login_info is None:  # pragma: no cover - find_browser 刚确认过它存在
            raise PdfError(f"指定的浏览器不存在：{info.path}")
        browser = LoginBrowser(
            login_info, profile, timeout=min(60.0, float(timeout)), start_url=source.as_uri()
        )
        with browser:
            session = CDPSession(
                browser.browser_ws_url, timeout=min(60.0, float(timeout)), site_urls=["file://"]
            )
            with session:
                session.connect()
                _wait_for_load(session, timeout)
                params = _cdp_params(opts)
                try:
                    result = session.call("Page.printToPDF", params, timeout=float(timeout))
                except CdpError as exc:
                    raise PdfError(f"浏览器没能生成 PDF：{exc}") from exc
        data = _decode_pdf_data(result)
        target.write_bytes(data)
        _check_and_move(target, output_pdf)
        if progress:
            elapsed = time.monotonic() - started
            progress(
                f"PDF 完成：{output_pdf.name}"
                f"（{output_pdf.stat().st_size / 1024 / 1024:.1f} MB，用时 {elapsed:.0f} 秒）"
            )
        return output_pdf
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def _cdp_params(options: PdfOptions) -> dict:
    """把选项翻成 ``Page.printToPDF`` 的参数，并堵住一个会白选纸张的组合。

    ``preferCSSPageSize`` 与显式 ``paperWidth``/``paperHeight`` **不能同时出现**：
    真机实测（见 ``_scratch/cmp_pdf_paths.py``）这种情况下浏览器改以页面 CSS 的
    ``@page size`` 为准，用户选的纸张被静默忽略；而且它还是竞态 —— 同一份 HTML、
    同一组参数连跑三次，只有 ``@page`` 还没解析完的那一次才按显式纸张出纸
    （A3＝841.92×1191.12），其余两次都出成页面里写的 A4（594.96×841.92）。
    即「选了 A3，出来 A4」，用户看到的还是时对时错。

    所以只要纸张是显式指定的，就一律丢掉这个键：显式选择优先于页面样式。
    """
    params = dict(options.to_cdp_params())
    if options.paper != DEFAULT_PAPER:
        params.pop("preferCSSPageSize", None)
    return params


def _wait_for_load(session: CDPSession, timeout: int) -> None:
    """等页面加载完成；等不到也给一枪（CDP 自己会按当前状态打印）。"""
    deadline = time.monotonic() + min(30.0, float(timeout))
    while time.monotonic() < deadline:
        try:
            if session.evaluate("document.readyState") == "complete":
                return
        except CdpError:
            return  # 页面还没建好，交给下面的打印去报错
        time.sleep(0.2)


def _decode_pdf_data(result: dict) -> bytes:
    """把 ``Page.printToPDF`` 的结果解成 PDF 字节。

    ``validate=True`` 是故意的：默认的 ``b64decode`` 会**静默丢掉**非法字符
    （中文之类的会变成空串），那样错误会以「渲染结果为空」的面目出现，
    看不出真正的原因。
    """
    payload = result.get("data") if isinstance(result, dict) else None
    if not payload:
        raise PdfError("浏览器返回的 PDF 是空的。")
    try:
        data = base64.b64decode(payload, validate=True)
    except (ValueError, TypeError) as exc:
        raise PdfError("浏览器返回的 PDF 数据解不开。") from exc
    if not data.startswith(b"%PDF"):
        raise PdfError("浏览器返回的不是有效的 PDF 数据。")
    return data


class PdfBuilder:
    """把串导出为 PDF。"""

    def __init__(
        self,
        client: XdaoClient,
        progress=None,
        filename_template: str | None = None,
        browser_path: str | None = None,
        pdf_timeout: int | None = None,
        fallback_html: bool = True,
        pdf_options: PdfOptions | None = None,
    ) -> None:
        self._client = client
        self._progress = progress
        self.filename_template = filename_template
        self.browser_path = browser_path
        self.timeout = int(pdf_timeout or DEFAULT_TIMEOUT)
        # 浏览器启不来时是否退而保存 HTML（对用户总比什么都没有强）
        self.fallback_html = fallback_html
        self.fallback_note = ""
        # 纸张/页边距这些打印选项；没给或全默认时走命令行，输出与老版本一致。
        self.pdf_options = pdf_options
        # 复用 HTML 导出器：图片内嵌、正文渲染这些逻辑不再重复实现。
        self._html = HtmlBuilder(client, progress=progress, filename_template=filename_template)

    def _notify(self, message: str) -> None:
        if self._progress:
            self._progress(message)

    def output_name(self, thread: ThreadData) -> str:
        return render_filename(self.filename_template, thread, derive_filename(thread))

    def render_pdf(self, html: str, _work_dir: Path, output_pdf: Path) -> Path:
        return render_html_to_pdf(
            html,
            output_pdf,
            browser_path=self.browser_path,
            options=self.pdf_options,
            timeout=self.timeout,
            progress=self._progress,
        )

    def build(
        self,
        thread: ThreadData,
        scope: str = "all",
        output_pdf: Path | str = "output.pdf",
        include_hashes: list[str] | None = None,
    ) -> Path:
        output_pdf = Path(output_pdf)
        html = self._html.build(thread, scope, include_hashes)
        return self.render_pdf(html, output_pdf.parent, output_pdf)

    def save(
        self,
        thread: ThreadData,
        scope: str = "all",
        output_dir: Path | str = ".",
        include_hashes: list[str] | None = None,
    ) -> Path:
        # PDF 与 HTML 一样，先确认目录可写，别等渲染完才失败。
        output_dir = ensure_writable(output_dir)
        self._notify("正在整理 HTML（图片内嵌）…")
        path = output_dir / (sanitize_filename(self.output_name(thread)) + ".pdf")
        try:
            return self.build(thread, scope, path, include_hashes)
        except PdfError:
            if not self.fallback_html:
                raise
            # 浏览器启不来时，把抓到并渲染好的内容存成 HTML ——
            # 用户在浏览器里打开后按 Ctrl+P 即可另存为 PDF。
            self._notify("PDF 渲染失败，改为保存 HTML（可在浏览器里打印成 PDF）…")
            html_path = output_dir / (sanitize_filename(self.output_name(thread)) + ".html")
            html_path.write_text(
                self._html.build(thread, scope, include_hashes), encoding="utf-8"
            )
            self.fallback_note = (
                f"PDF 渲染失败，已改存 {html_path.name}；"
                "在浏览器里打开它，按 Ctrl+P 选「另存为 PDF」即可得到同样的结果。"
            )
            self._notify(self.fallback_note)
            return html_path
