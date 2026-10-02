"""导出器集合。

对外统一出口：抓取层和界面层只从这里取导出器，不必关心内部文件划分。

- ``html``     : 图片内嵌的单文件 HTML
- ``txt``      : 纯文本，图片保留链接
- ``markdown`` : 便于二次编辑的 Markdown
- ``epub``     : 可导入阅读器的 EPUB 电子书
- ``_shared``  : 文本处理、文件名推导、图片解析等公共逻辑
"""

from __future__ import annotations

from ._shared import (
    DirChoice,
    OutputDirNotWritable,
    ThreadData,
    choose_writable_dir,
    derive_filename,
    ensure_writable,
    fallback_dirs,
    fetch_image,
    guess_mime,
    iter_post_image_urls,
    markdown_images_to_xhtml,
    mime_to_ext,
    output_path,
    plain_text,
    render_filename,
    render_inline_content,
    resolve_image_url,
    sanitize_filename,
)
from .epub import EpubBuilder
from .html import HtmlBuilder
from .markdown import MarkdownBuilder
from .pdf import PdfBuilder, PdfError, find_browser, render_html_to_pdf
from .txt import TxtBuilder

# 支持的导出格式：格式键 → (显示名, 扩展名, 导出器类)
EXPORTERS = {
    "html": ("HTML（图片嵌入）", ".html", HtmlBuilder),
    "pdf": ("PDF（浏览器渲染）", ".pdf", PdfBuilder),
    "txt": ("TXT（纯文本）", ".txt", TxtBuilder),
    "markdown": ("Markdown（.md）", ".md", MarkdownBuilder),
    "epub": ("EPUB（电子书）", ".epub", EpubBuilder),
}


def create_exporter(
    format_key: str,
    client,
    progress=None,
    filename_template: str | None = None,
    image_mode: str | None = None,
    browser_path: str | None = None,
    pdf_timeout: int | None = None,
    fallback_html: bool | None = None,
    pdf_options=None,
):
    """按格式键创建导出器实例；未知格式回退到 HTML。

    不同导出器接受的参数不同（例如只有 EPUB 有 ``image_mode``、只有 PDF 有
    ``browser_path`` 和 ``pdf_options``），这里按能力逐级回退，避免调用方为每种
    格式写分支。
    """
    _, _, cls = EXPORTERS.get(format_key, EXPORTERS["html"])
    attempts = [
        {
            "progress": progress,
            "filename_template": filename_template,
            "image_mode": image_mode,
            "browser_path": browser_path,
            "pdf_timeout": pdf_timeout,
            "fallback_html": fallback_html,
            "pdf_options": pdf_options,
        },
        # 第二层：**去掉 image_mode**（只有 EPUB 有它），其余照旧带着。
        # pdf_options 与 browser_path 必须在这一层就带上，不能等到下面几层：PdfBuilder
        # 的构造签名里没有 image_mode，所以第一层只要带了 image_mode 就整体 TypeError、
        # 一定会落到这一层；而用户设好的纸张/边距（v0.8.0 报过）与浏览器路径
        # （2026-10-02 发现）原来正是在这里被悄悄丢掉的 —— 后者在真机上的表现是
        # 「设置里指定的浏览器对 PDF 导出不起作用」，`pdf_timeout` 与 `fallback_html`
        # 同样在这一层丢。只钉「传给 create_exporter 的参数」看不出来，得把「导出器
        # 真正收到什么」也钉住（tests/test_watcher.py 与 tests/test_exporters.py）。
        {
            "progress": progress,
            "filename_template": filename_template,
            "browser_path": browser_path,
            "pdf_timeout": pdf_timeout,
            "fallback_html": fallback_html,
            "pdf_options": pdf_options,
        },
        {
            "progress": progress,
            "filename_template": filename_template,
            "image_mode": image_mode,
            "pdf_options": pdf_options,
        },
        {"progress": progress, "filename_template": filename_template},
        {"progress": progress},
    ]
    last_error: TypeError | None = None
    for kwargs in attempts:
        # 只丢掉值为 None 的项：False 是有意义的取值（例如 fallback_html=False）。
        clean = {k: v for k, v in kwargs.items() if v is not None}
        try:
            return cls(client, **clean)
        except TypeError as exc:
            last_error = exc
    raise last_error if last_error else TypeError(f"无法创建导出器：{format_key}")


__all__ = [
    "EXPORTERS",
    "DirChoice",
    "EpubBuilder",
    "HtmlBuilder",
    "MarkdownBuilder",
    "OutputDirNotWritable",
    "PdfBuilder",
    "PdfError",
    "ThreadData",
    "TxtBuilder",
    "choose_writable_dir",
    "create_exporter",
    "derive_filename",
    "ensure_writable",
    "fallback_dirs",
    "fetch_image",
    "find_browser",
    "guess_mime",
    "iter_post_image_urls",
    "markdown_images_to_xhtml",
    "mime_to_ext",
    "output_path",
    "plain_text",
    "render_filename",
    "render_html_to_pdf",
    "render_inline_content",
    "resolve_image_url",
    "sanitize_filename",
]
