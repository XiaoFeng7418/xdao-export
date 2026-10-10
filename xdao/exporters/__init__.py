"""导出器集合。

对外统一出口：抓取层和界面层只从这里取导出器，不必关心内部文件划分。

- ``html``     : 图片内嵌的单文件 HTML
- ``txt``      : 纯文本，图片保留链接
- ``markdown`` : 便于二次编辑的 Markdown
- ``epub``     : 可导入阅读器的 EPUB 电子书
- ``pdf``      : 交给本机浏览器渲染的 PDF
- ``base``     : 导出器基类（扩展名、文件名、保存流程只写一份）
- ``_shared``  : 文本处理、文件名推导、图片解析等公共逻辑
- ``..paths``  : 目录可写性与兜底（基础设施，缓存层与自检层也用）
"""

from __future__ import annotations

from ..paths import (
    DirChoice,
    OutputDirNotWritable,
    choose_writable_dir,
    ensure_writable,
    fallback_dirs,
)
from ._shared import (
    ThreadData,
    derive_filename,
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
from .base import Exporter
from .epub import EpubBuilder
from .html import HtmlBuilder
from .markdown import MarkdownBuilder
from .pdf import PdfBuilder, PdfError, find_browser, render_html_to_pdf
from .txt import TxtBuilder

# 支持的导出格式：格式键 → (显示名, 扩展名, 导出器类)。
# 显示名与扩展名取自导出器类自己的类属性（``Exporter.display`` / ``Exporter.suffix``），
# 不在这里再抄一份：2026-10-07 架构评审发现，「注册表里那个扩展名」与「save 里写死的
# 扩展名」本来是两份可以各自走样的真源。字典顺序即界面下拉框顺序。
EXPORTERS = {
    cls.key: (cls.display, cls.suffix, cls)
    for cls in (HtmlBuilder, PdfBuilder, TxtBuilder, MarkdownBuilder, EpubBuilder)
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

    所有参数一次性交给导出器，由它取自己认识的那几项，不认识的落进
    ``Exporter.options``。2026-10-07 架构评审：原先这里是「带上全部参数 → ``TypeError``
    就删一项再试」的五层阶梯，而阶梯本身丢过 ``browser_path`` / ``pdf_timeout`` /
    ``fallback_html`` / ``pdf_options``（v0.8.0 与 2026-10-02 各报过一次）；构造签名统一
    成 ``(client, progress, filename_template, **options)`` 之后，阶梯连同它的隐患一起删掉。
    """
    _, _, cls = EXPORTERS.get(format_key, EXPORTERS["html"])
    options = {
        "progress": progress,
        "filename_template": filename_template,
        "image_mode": image_mode,
        "browser_path": browser_path,
        "pdf_timeout": pdf_timeout,
        "fallback_html": fallback_html,
        "pdf_options": pdf_options,
    }
    # 只丢掉值为 None 的项：False 是有意义的取值（例如 fallback_html=False）。
    clean = {k: v for k, v in options.items() if v is not None}
    return cls(client, **clean)


__all__ = [
    "EXPORTERS",
    "DirChoice",
    "EpubBuilder",
    "Exporter",
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
