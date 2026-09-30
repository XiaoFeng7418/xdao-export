"""PDF 导出器：调用本机浏览器的无头模式把 HTML 渲染成 PDF。

思路：先复用 :class:`~xdao.exporters.html.HtmlBuilder` 生成**图片已内嵌**的单文件 HTML，
再交给无头浏览器打印成 PDF。这样 PDF 完全离线可用，不依赖网络。

为什么用浏览器而不是自己排版：中文字体、长文分页、图片缩放这些交给浏览器最稳妥，
也不必引入任何第三方依赖（项目一直保持纯标准库）。

已知限制：

- 需要本机装有 Chrome 或 Edge（Windows 上基本都有）；都没有时给出明确提示；
- 内容很多的串（例如内嵌上百张图）渲染会比较慢，且 PDF 体积可能很大；
- 页眉页脚由 `--print-to-pdf-no-header` 关闭，页面样式沿用 HTML 里的打印样式。
"""

from __future__ import annotations

import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from ..client import XdaoClient
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


def render_html_to_pdf(
    html: str,
    output_pdf: Path | str,
    *,
    browser_path: str | Path | None = None,
    timeout: int = DEFAULT_TIMEOUT,
    progress=None,
) -> Path:
    """把一段 HTML 渲染成 PDF，返回写入的路径。

    这是模块级的实现，:class:`PdfBuilder` 只是把它和 HTML 生成串起来，
    这样渲染部分可以单独调用与测试。
    """
    output_pdf = Path(output_pdf)
    browser = find_browser(browser_path)
    if progress:
        progress(f"正在用 {browser.name} 渲染 PDF…")

    # 浏览器对非 ASCII 路径支持不稳定，临时文件统一用 ASCII 名。
    token = uuid.uuid4().hex[:8]
    work_dir = output_pdf.parent
    work_dir.mkdir(parents=True, exist_ok=True)
    temp_dir = work_dir / f".xdao-pdf-{token}"
    temp_dir.mkdir(parents=True, exist_ok=True)
    source = temp_dir / "source.html"
    target = temp_dir / "output.pdf"
    profile = temp_dir / "profile"
    profile.mkdir(exist_ok=True)

    try:
        source.write_text(html, encoding="utf-8")
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
            source.as_uri(),
        ]
        started = time.monotonic()
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise PdfError(
                f"渲染 PDF 超时（超过 {timeout} 秒）。"
                "串太长或图片太多时可能发生，可以改用 HTML 格式。"
            ) from exc

        if not target.exists() or target.stat().st_size == 0:
            detail = (completed.stderr or completed.stdout or "").strip()[-400:]
            raise PdfError(
                f"{browser.name} 没有生成 PDF（退出码 {completed.returncode}）。\n{detail}\n"
                "可以改用 HTML 格式，或在设置里换一个浏览器路径。"
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


class PdfBuilder:
    """把串导出为 PDF。"""

    def __init__(
        self,
        client: XdaoClient,
        progress=None,
        filename_template: str | None = None,
        browser_path: str | None = None,
        pdf_timeout: int | None = None,
    ) -> None:
        self._client = client
        self._progress = progress
        self.filename_template = filename_template
        self.browser_path = browser_path
        self.timeout = int(pdf_timeout or DEFAULT_TIMEOUT)
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
        return self.build(thread, scope, path, include_hashes)
