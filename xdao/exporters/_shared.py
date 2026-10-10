"""导出器公共基础：文本处理、文件名推导、图片解析。

所有导出器（HTML / TXT / EPUB / Markdown）都复用这里的函数，
避免同一套逻辑在多处实现后渐渐走样。

目录与写权限那部分（``ensure_writable`` / ``can_write_dir`` /
``choose_writable_dir`` 等）**不在这里实现**，而是住在 :mod:`xdao.paths`——
它是基础设施，缓存层与自检层也要用；文件末尾只做同名转发，兼容既有导入写法。
"""

from __future__ import annotations

import html as html_lib
import re
from dataclasses import dataclass
from pathlib import Path

from ..client import Post, XdaoClient

_TAG_RE = re.compile(r"<[^>]+>")
# 文件名里 Windows 不允许出现的字符。
_ILLEGAL_FILENAME_CHARS = re.compile(r'[\\/:*?"<>|]')
# 一句话的结束符，中英文都要覆盖。
_SENTENCE_SPLIT = re.compile(r"[\n。！？!?]")
# 正文里 <img src="..."> 的提取。
_IMG_SRC_RE = re.compile(r'<img[^>]+src="([^"]+)"', re.IGNORECASE)
# Markdown 里图片链接的还原（供 EPUB 的 XHTML 转换使用）。
_MD_IMAGE_RE = re.compile(r"!\[[^\]]*\]\(([^)]+)\)")


def plain_text(value: str) -> str:
    """去掉 HTML 标记，保留可读文本；``<br>`` 视为换行。"""
    value = re.sub(r"<br\s*/?>", "\n", value, flags=re.IGNORECASE)
    value = _TAG_RE.sub("", value)
    value = html_lib.unescape(value)
    return value.strip()


def sanitize_filename(name: str, fallback: str = "xdao-thread") -> str:
    """把任意字符串规整成安全的 Windows 文件名（不含扩展名）。"""
    cleaned = _ILLEGAL_FILENAME_CHARS.sub("_", name).strip()
    cleaned = cleaned.strip(" .")
    if len(cleaned) > 80:
        cleaned = cleaned[:80].strip(" .")
    return cleaned or fallback


def render_filename(template: str | None, thread: "ThreadData", fallback: str) -> str:
    """按模板渲染文件名主体（不含扩展名）。

    可用占位符：``{title}`` 标题、``{id}`` 串号、``{date}`` 导出日期（YYYYMMDD）、
    ``{po}`` PO 饼干、``{count}`` 楼层数。模板为空或渲染失败时回退到默认标题推导。
    """
    if not template:
        return fallback
    import datetime

    try:
        rendered = template.format(
            title=fallback,
            id=thread.thread_id,
            date=datetime.date.today().strftime("%Y%m%d"),
            po=thread.po_hash or "",
            count=len(thread.posts),
        )
    except (KeyError, IndexError, ValueError):
        # 模板写错不该导致导出失败，静默回退。
        return fallback
    return sanitize_filename(rendered) or fallback


@dataclass
class ThreadData:
    """一个串的完整数据。"""

    thread_id: int
    title: str
    po_hash: str
    posts: list[Post]

    @property
    def reply_count(self) -> int:
        """回复数（不含主帖）。"""
        return max(0, len(self.posts) - 1)

    def is_po_post(self, post: Post) -> bool:
        """主帖本身，或与发串人同一饼干，都算 PO。"""
        return bool(post.is_po or (self.po_hash and post.user_hash == self.po_hash))

    def filter_posts(
        self,
        scope: str = "all",
        include_hashes: list[str] | None = None,
    ) -> list[Post]:
        """按范围筛选楼层。

        - ``scope="all"``：全部楼层；``scope="po"``：仅 PO（主帖始终保留）。
        - ``include_hashes``：非空时只保留这些饼干的楼层；此时主帖也遵守该筛选，
          因为它同样属于某个饼干。
        """
        wanted = {h.strip() for h in include_hashes or [] if h and h.strip()}
        selected: list[Post] = []
        for post in self.posts:
            if wanted and post.user_hash not in wanted:
                continue
            if not wanted and scope == "po" and not self.is_po_post(post):
                continue
            selected.append(post)
        return selected


def derive_filename(thread: ThreadData) -> str:
    """默认文件名推导：优先标题，无标题时取第一楼正文的第一句话。"""
    title = plain_text(thread.title)
    if title and title not in ("无标题",):
        return title
    for post in thread.posts:
        text = plain_text(post.content)
        if not text:
            continue
        first = _SENTENCE_SPLIT.split(text)[0].strip()
        if first:
            return first
    return f"串{thread.thread_id}"


def resolve_image_url(client: XdaoClient, src: str) -> str:
    """把正文里的图片地址补全为绝对 URL。"""
    if not src:
        return ""
    src = src.strip()
    if src.startswith("http://") or src.startswith("https://"):
        return src
    if src.startswith("data:"):
        return src
    base = client.cdn_path or ""
    if base and not base.endswith("/"):
        base += "/"
    return base + src.lstrip("/")


def iter_post_image_urls(client: XdaoClient, post: Post) -> list[str]:
    """收集一楼涉及的全部图片绝对 URL（去重、保序）。"""
    urls: list[str] = []
    if post.img and post.ext:
        urls.append(client.image_url(post.img, post.ext))
    for match in _IMG_SRC_RE.findall(post.content or ""):
        urls.append(resolve_image_url(client, html_lib.unescape(match)))

    seen: set[str] = set()
    unique: list[str] = []
    for url in urls:
        if url and url not in seen:
            seen.add(url)
            unique.append(url)
    return unique


def attachment_image_url(client: XdaoClient, post: Post) -> str:
    """一楼的附件图（``post.img`` / ``post.ext``）地址，没有则空串。

    Markdown 与 EPUB 两处原先各抄了一份逐字相同的实现（2026-10-07 架构评审 §6.2-2），
    收在这里。两个细节都是有意的：

    * 附件图地址算不出来时返回空串 —— 导出流程不该因为一张附件图崩掉；
    * 地址必须以 :func:`iter_post_image_urls` 的公共收集结果为准，否则正文里出现同名
      图片时会把同一张图附两遍。
    """
    if not (post.img and post.ext):
        return ""
    try:
        url = client.image_url(post.img, post.ext)
    except Exception:
        # 附件图地址异常不该影响导出。
        return ""
    if not url:
        return ""
    return url if url in iter_post_image_urls(client, post) else ""


def fetch_image(client: XdaoClient, url: str) -> bytes | None:
    """下载图片，失败返回 None（导出流程绝不因单张图片中断）。"""
    if not url or url.startswith("data:"):
        return None
    try:
        return client.download_image(url)
    except Exception:
        return None


def guess_mime(url: str, data: bytes | None = None) -> str:
    """判断图片 MIME：优先看魔数，其次看扩展名。"""
    if data:
        if data[:3] == b"\xff\xd8\xff":
            return "image/jpeg"
        if data[:8] == b"\x89PNG\r\n\x1a\n":
            return "image/png"
        if data[:6] in (b"GIF87a", b"GIF89a"):
            return "image/gif"
        if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            return "image/webp"
    lowered = (url or "").lower().split("?")[0]
    for ext, mime in (
        (".jpg", "image/jpeg"),
        (".jpeg", "image/jpeg"),
        (".png", "image/png"),
        (".gif", "image/gif"),
        (".webp", "image/webp"),
    ):
        if lowered.endswith(ext):
            return mime
    return "image/jpeg"


_MIME_EXT = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
}


def mime_to_ext(mime: str) -> str:
    """MIME 对应的文件扩展名。"""
    return _MIME_EXT.get(mime, ".jpg")


def collapse_blank_lines(text: str) -> str:
    """把 3 个以上连续空行折叠成 2 个，并去掉首尾空白。"""
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def render_inline_content(
    client: XdaoClient,
    content: str,
    image_mode: str = "url",
    embedder=None,
) -> str:
    """把一楼正文渲染成干净的 Markdown 文本。

    - ``<br>`` 变成换行，其它标签只保留文字内容（并转义 Markdown 里有歧义的字符）。
    - ``image_mode="url"``：图片输出为 ``![图片](绝对URL)``。
    - ``image_mode="drop"``：丢弃图片。
    - ``image_mode="embed"``：调用 ``embedder(url)`` 取 data URI，失败则回退成 URL 形式。
    """
    if not content:
        return ""

    from html.parser import HTMLParser

    class _Renderer(HTMLParser):
        def __init__(self) -> None:
            super().__init__(convert_charrefs=True)
            self.parts: list[str] = []

        def handle_starttag(self, tag, attrs) -> None:
            tag = tag.lower()
            if tag == "br":
                self.parts.append("\n")
            elif tag == "img" and image_mode != "drop":
                src = dict(attrs).get("src") or ""
                url = resolve_image_url(client, src)
                target = url
                if image_mode == "embed" and embedder is not None:
                    embedded = embedder(url) if url else None
                    if embedded:
                        target = embedded
                if target:
                    self.parts.append(f"\n![图片]({target})\n")

        def handle_startendtag(self, tag, attrs) -> None:
            self.handle_starttag(tag, attrs)

        def handle_endtag(self, tag) -> None:
            if tag.lower() == "br":
                self.parts.append("\n")

        def handle_data(self, data) -> None:
            self.parts.append(data)

    renderer = _Renderer()
    renderer.feed(content)
    renderer.close()
    text = "".join(renderer.parts)
    # 行尾空白清掉，但保留段落结构。
    text = "\n".join(line.rstrip() for line in text.split("\n"))
    return collapse_blank_lines(text)


def markdown_images_to_xhtml(text: str, image_attr) -> str:
    """把 Markdown 文本转成 XHTML 片段（EPUB 用）。

    ``image_attr(url) -> str`` 由调用方给出 ``<img>`` 的 src（可能是内嵌 data URI），
    返回空串表示该图片无法使用，则丢弃。
    """
    blocks: list[str] = []
    paragraph: list[str] = []

    def flush() -> None:
        if paragraph:
            blocks.append("<p>" + "<br/>".join(paragraph) + "</p>")
            paragraph.clear()

    for raw_line in text.split("\n"):
        line = raw_line.strip()
        if not line:
            flush()
            continue
        match = _MD_IMAGE_RE.fullmatch(line)
        if match:
            flush()
            src = image_attr(match.group(1))
            if src:
                blocks.append(
                    f'<div class="img"><img src="{html_lib.escape(src, quote=True)}" alt="图片"/></div>'
                )
            continue
        paragraph.append(_escape_xhtml_line(line))
    flush()
    return "\n".join(blocks)


def _escape_xhtml_line(line: str) -> str:
    """转义文本，同时保留行内 Markdown 图片为后续处理让路（此处只做纯文本转义）。"""
    return html_lib.escape(line, quote=False)


def output_path(output_dir: Path, name: str, suffix: str) -> Path:
    """拼出导出文件路径，文件名安全化后加上扩展名。"""
    return Path(output_dir) / (sanitize_filename(name) + suffix)


# ---------------------------------------------------------------------------
# 目录与写权限（能不能写、写不进去怎么兜底）已搬到 ``xdao/paths.py``。
# 2026-10-07 架构评审：缓存层与自检层不该为了探一次目录去依赖「导出格式」子包，
# ``cache.py`` 与 ``preflight.py`` 现在直接 ``from ..paths import …``；
# 这里保留同名转发，只为兼容既有的导入写法（测试与文档里仍有
# ``xdao.exporters._shared.PROBE_NAME`` 这类引用）。
# ---------------------------------------------------------------------------
from ..paths import (  # noqa: F401  （转发，真源在 xdao/paths.py）
    CLASSIC_BLOCK_HINT,
    PROBE_ALT_NAME,
    PROBE_NAME,
    DirChoice,
    OutputDirNotWritable,
    can_write_dir,
    choose_writable_dir,
    ensure_writable,
    fallback_dirs,
    probe_writable,
)

__all__ = [
    "CLASSIC_BLOCK_HINT",
    "DirChoice",
    "OutputDirNotWritable",
    "PROBE_ALT_NAME",
    "PROBE_NAME",
    "ThreadData",
    "can_write_dir",
    "attachment_image_url",
    "choose_writable_dir",
    "collapse_blank_lines",
    "derive_filename",
    "ensure_writable",
    "fallback_dirs",
    "fetch_image",
    "guess_mime",
    "iter_post_image_urls",
    "markdown_images_to_xhtml",
    "mime_to_ext",
    "output_path",
    "plain_text",
    "probe_writable",
    "render_filename",
    "render_inline_content",
    "resolve_image_url",
    "sanitize_filename",
]
