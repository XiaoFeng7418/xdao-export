"""EPUB 3 导出器：只用标准库手写 epub 结构。

为了让生成的 .epub 能被主流阅读器打开，这里严格按 EPUB 3 的约定组织：

    mimetype                     必须第一个写入，且不压缩
    META-INF/container.xml       指向 OEBPS/content.opf
    OEBPS/content.opf            包文档（metadata / manifest / spine）
    OEBPS/nav.xhtml              EPUB 3 目录
    OEBPS/toc.ncx                EPUB 2 兼容目录
    OEBPS/text/post-0001.xhtml   每楼一个文件
    OEBPS/images/img-0001.jpg    图片原始数据（供阅读器参考）

文本处理、文件名推导、图片解析都复用 ``._shared``，避免各导出器各写一套。
"""

from __future__ import annotations

import base64
import datetime as _datetime
import html as html_lib
import uuid
import zipfile
from pathlib import Path

from ..client import Post
from ._shared import (
    ThreadData,
    derive_filename,
    fetch_image,
    guess_mime,
    iter_post_image_urls,
    markdown_images_to_xhtml,
    mime_to_ext,
    plain_text,
    render_filename,
    render_inline_content,
    sanitize_filename,
)


# 没有 PO 饼干时给一个固定的作者名兜底。
DEFAULT_CREATOR = "X岛"
EPUB_MIMETYPE = "application/epub+zip"
LANGUAGE = "zh-CN"
TEXT_DIR = "OEBPS/text"
IMAGE_DIR = "OEBPS/images"

# EPUB 3 的固定外壳。
CONTAINER_XML = """<?xml version="1.0" encoding="utf-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>
"""

STYLE_CSS = """/* 尽量少做样式干预，交给阅读器处理排版。 */
body { font-family: "Microsoft YaHei", "Noto Sans CJK SC", serif; line-height: 1.6; }
h1 { font-size: 1.2em; }
.post-title { font-size: 1.15em; font-weight: bold; }
.post-meta { font-size: 0.8em; color: #666; margin-bottom: 0.6em; }
.post-meta span { margin-right: 0.6em; }
.badge-po { color: #b45309; font-weight: bold; }
.badge-admin { color: #b91c1c; font-weight: bold; }
.cookie { font-family: Consolas, monospace; }
.post-body p { margin: 0 0 0.6em; }
.post-body img { max-width: 100%; height: auto; }
.post { margin-bottom: 1.2em; }
"""


def _now_utc() -> _datetime.datetime:
    """当前 UTC 时间（秒级）。"""
    return _datetime.datetime.now(_datetime.timezone.utc).replace(microsecond=0)


def _iso_utc(moment: _datetime.datetime) -> str:
    """形如 2026-09-23T12:00:00Z 的时间串。"""
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _data_uri(url: str, data: bytes) -> str:
    """把图片字节编码成 data URI（MIME 按魔数判断）。"""
    mime = guess_mime(url, data)
    return f"data:{mime};base64," + base64.b64encode(data).decode("ascii")


def _epub_title(thread: ThreadData) -> str:
    """书名：优先标题，无标题时退回文件名推导结果。"""
    title = plain_text(thread.title)
    if title and title not in ("无标题",):
        return title
    return derive_filename(thread)


class EpubBuilder:
    """把串导出成 epub 文件。"""

    def __init__(
        self,
        client,
        progress=None,
        filename_template: str | None = None,
        image_mode: str | None = None,
    ) -> None:
        self._client = client
        self._progress = progress
        self.filename_template = filename_template
        # 构造函数里的设置作为 build()/save() 未显式传参时的默认值。
        self.image_mode = image_mode if image_mode in ("embed", "url", "drop") else "embed"
        # url -> 图片原始字节；None 表示下载失败，不再重试。
        self._data_cache: dict[str, bytes | None] = {}
        # url -> data URI。
        self._uri_cache: dict[str, str] = {}

    # ---------- 基础工具 ----------

    def _notify(self, message: str) -> None:
        if self._progress:
            self._progress(message)

    def build(
        self,
        thread: ThreadData,
        scope: str,
        output_path: Path,
        *,
        image_mode: str | None = None,
        include_hashes: list[str] | None = None,
    ) -> Path:
        """生成 .epub 文件，返回写入的路径。"""
        image_mode = image_mode or self.image_mode
        posts = thread.filter_posts(scope, include_hashes)
        images = self._collect_images(posts, image_mode)

        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        self._notify("正在生成 EPUB 文件…")
        with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as archive:
            self._write_epub(archive, thread, posts, images, image_mode)
        return output_path

    def output_name(self, thread: ThreadData) -> str:
        """按模板（若设置）推导文件名主体。"""
        return render_filename(self.filename_template, thread, derive_filename(thread))

    def save(
        self,
        thread: ThreadData,
        scope: str,
        output_dir: Path,
        *,
        image_mode: str | None = None,
        include_hashes: list[str] | None = None,
    ) -> Path:
        """在 output_dir 下按文件名模板（或推导结果）保存。"""
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        name = sanitize_filename(self.output_name(thread)) + ".epub"
        return self.build(
            thread,
            scope,
            output_dir / name,
            image_mode=image_mode,
            include_hashes=include_hashes,
        )

    # ---------- 图片 ----------

    def _load(self, url: str) -> bytes | None:
        """下载并缓存图片，失败返回 None。"""
        if not url:
            return None
        if url in self._data_cache:
            return self._data_cache[url]
        self._notify(f"正在下载图片：{url}")
        data = fetch_image(self._client, url)
        self._data_cache[url] = data
        return data

    def _data_uri_for(self, url: str) -> str:
        """取图片的 data URI，失败返回空串。"""
        if not url:
            return ""
        if url in self._uri_cache:
            return self._uri_cache[url]
        data = self._load(url)
        uri = _data_uri(url, data) if data else ""
        self._uri_cache[url] = uri
        return uri

    def _collect_images(
        self, posts: list[Post], image_mode: str
    ) -> list[tuple[str, bytes]]:
        """按出现顺序收集要落盘的图片：(entry_name, 数据)。"""
        if image_mode != "embed":
            return []
        images: list[tuple[str, bytes]] = []
        seen: set[str] = set()
        for post in posts:
            for url in iter_post_image_urls(self._client, post):
                if url in seen:
                    continue
                seen.add(url)
                data = self._load(url)
                if not data:
                    # 下载失败静默跳过，绝不让导出整体失败。
                    continue
                suffix = mime_to_ext(guess_mime(url, data))
                name = f"{IMAGE_DIR}/img-{len(images) + 1:04d}{suffix}"
                images.append((name, data))
        return images

    def _image_src(self, url: str, image_mode: str) -> str:
        """XHTML 里 <img src> 的取值；返回空串表示丢弃这张图。

        ``render_inline_content`` 在 embed 模式下会直接把 data URI 写进 Markdown，
        这里原样保留；普通的图片 URL 才按 image_mode 处理。
        """
        if not url:
            return ""
        if url.startswith("data:"):
            return url
        if image_mode == "drop":
            return ""
        if image_mode == "embed":
            return self._data_uri_for(url)
        return url

    def _attachment_url(self, post: Post) -> str:
        """post.img / post.ext 对应的附件图 URL，没有则空串。"""
        if not (post.img and post.ext):
            return ""
        try:
            url = self._client.image_url(post.img, post.ext)
        except Exception:
            # 附件图地址异常不该影响导出。
            return ""
        if not url:
            return ""
        # 以公共收集逻辑为准，避免正文里出现同名的相对地址时重复附图。
        return url if url in iter_post_image_urls(self._client, post) else ""

    # ---------- 正文 ----------

    def _render_body(self, post: Post, image_mode: str) -> str:
        """正文 HTML → Markdown → XHTML 片段（含附件图）。"""
        markdown = render_inline_content(
            self._client,
            post.content,
            image_mode=image_mode,
            embedder=self._data_uri_for,
        )
        attachment = self._attachment_url(post)
        if attachment:
            # 附件图不在正文里，单独作为一段图片附加在正文后面。
            markdown = (
                f"{markdown}\n\n![图片]({attachment})" if markdown else f"![图片]({attachment})"
            )
        if not markdown:
            return ""
        return markdown_images_to_xhtml(
            markdown, lambda url: self._image_src(url, image_mode)
        )

    def _render_post_xhtml(
        self, index: int, post: Post, thread: ThreadData, image_mode: str
    ) -> str:
        """一楼一页的 XHTML 5。"""
        is_po = thread.is_po_post(post)
        meta: list[str] = []
        if is_po:
            meta.append('<span class="badge-po">[PO]</span>')
        if post.admin:
            meta.append('<span class="badge-admin">[红名]</span>')
        meta.append(f'<span class="cookie">饼干 {html_lib.escape(post.user_hash or "")}</span>')
        name = plain_text(post.name or "")
        if name and name != "无名氏":
            meta.append(f'<span class="aname">{html_lib.escape(name)}</span>')
        title = plain_text(post.title or "")
        if title and title not in ("无标题",):
            meta.append(f'<span class="ptitle">{html_lib.escape(title)}</span>')
        meta.append(f'<span class="time">{html_lib.escape(post.now or "")}</span>')
        meta.append(f'<span class="no">No.{post.id}</span>')

        # 正文与附件图都在这里渲染，正文为空时也会输出图片。
        body = self._render_body(post, image_mode)

        heading = f"第 {index} 楼"
        return (
            '<?xml version="1.0" encoding="utf-8"?>\n'
            '<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="zh-CN" lang="zh-CN">\n'
            "<head>\n"
            '<meta charset="utf-8"/>\n'
            f"<title>{html_lib.escape(heading)}</title>\n"
            '<link rel="stylesheet" type="text/css" href="../style.css"/>\n'
            "</head>\n"
            '<body epub:type="bodymatter" xmlns:epub="http://www.idpf.org/2007/ops">\n'
            f'<section class="post{" po" if is_po else ""}" id="post-{index:04d}">\n'
            f'<h1 class="post-title">{html_lib.escape(heading)}</h1>\n'
            f'<p class="post-meta">{"".join(meta)}</p>\n'
            f'<div class="post-body">\n{body}\n</div>\n'
            "</section>\n"
            "</body>\n"
            "</html>\n"
        )

    # ---------- 目录与包文档 ----------

    @staticmethod
    def _entries(posts: list[Post], thread: ThreadData) -> list[tuple[str, str]]:
        """目录项：(标题, xhtml 文件名)。"""
        items: list[tuple[str, str]] = []
        for index, post in enumerate(posts, start=1):
            label = f"{index}. "
            if thread.is_po_post(post):
                label += "[PO] "
            label += f"饼干{post.user_hash or '未知'} · No.{post.id}"
            items.append((label, f"post-{index:04d}.xhtml"))
        return items

    def _render_nav(self, entries: list[tuple[str, str]]) -> str:
        items = "\n".join(
            f'<li><a href="text/{name}">{html_lib.escape(label)}</a></li>'
            for label, name in entries
        )
        return (
            '<?xml version="1.0" encoding="utf-8"?>\n'
            '<html xmlns="http://www.w3.org/1999/xhtml" '
            'xmlns:epub="http://www.idpf.org/2007/ops" xml:lang="zh-CN" lang="zh-CN">\n'
            "<head>\n"
            '<meta charset="utf-8"/>\n'
            "<title>目录</title>\n"
            '<link rel="stylesheet" type="text/css" href="style.css"/>\n'
            "</head>\n"
            "<body>\n"
            '<nav epub:type="toc" id="toc">\n'
            "<h1>目录</h1>\n"
            "<ol>\n"
            f"{items}\n"
            "</ol>\n"
            "</nav>\n"
            "</body>\n"
            "</html>\n"
        )

    def _render_ncx(
        self, thread: ThreadData, entries: list[tuple[str, str]], book_id: str
    ) -> str:
        points = "\n".join(
            "    <navPoint id=\"navPoint-{index}\" playOrder=\"{index}\">\n"
            "      <navLabel><text>{label}</text></navLabel>\n"
            "      <content src=\"text/{name}\"/>\n"
            "    </navPoint>".format(
                index=index, label=html_lib.escape(label), name=name
            )
            for index, (label, name) in enumerate(entries, start=1)
        )
        return (
            '<?xml version="1.0" encoding="utf-8"?>\n'
            '<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1" xml:lang="zh-CN">\n'
            "  <head>\n"
            f'    <meta name="dtb:uid" content="{html_lib.escape(book_id)}"/>\n'
            '    <meta name="dtb:depth" content="1"/>\n'
            '    <meta name="dtb:totalPageCount" content="0"/>\n'
            '    <meta name="dtb:maxPageNumber" content="0"/>\n'
            "  </head>\n"
            f"  <docTitle><text>{html_lib.escape(_epub_title(thread))}</text></docTitle>\n"
            "  <navMap>\n"
            f"{points}\n"
            "  </navMap>\n"
            "</ncx>\n"
        )

    def _render_opf(
        self,
        thread: ThreadData,
        entries: list[tuple[str, str]],
        images: list[tuple[str, bytes]],
        book_id: str,
        modified: str,
    ) -> str:
        manifest = [
            '    <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>',
            '    <item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>',
            '    <item id="css" href="style.css" media-type="text/css"/>',
        ]
        for index, (_, name) in enumerate(entries, start=1):
            manifest.append(
                f'    <item id="post-{index:04d}" href="text/{name}" '
                'media-type="application/xhtml+xml"/>'
            )
        for index, (name, data) in enumerate(images, start=1):
            mime = guess_mime(name, data)
            manifest.append(
                f'    <item id="img-{index:04d}" href="{name.split("/", 1)[1]}" '
                f'media-type="{mime}"/>'
            )

        spine = ['    <itemref idref="nav"/>']
        for index in range(1, len(entries) + 1):
            spine.append(f'    <itemref idref="post-{index:04d}"/>')

        creator = (thread.po_hash or "").strip() or DEFAULT_CREATOR
        return (
            '<?xml version="1.0" encoding="utf-8"?>\n'
            '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" '
            'unique-identifier="bookid" xml:lang="zh-CN">\n'
            '  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">\n'
            f'    <dc:identifier id="bookid">{html_lib.escape(book_id)}</dc:identifier>\n'
            f"    <dc:title>{html_lib.escape(_epub_title(thread))}</dc:title>\n"
            f"    <dc:language>{LANGUAGE}</dc:language>\n"
            f"    <dc:creator>{html_lib.escape(creator)}</dc:creator>\n"
            f'    <meta property="dcterms:modified">{modified}</meta>\n'
            "  </metadata>\n"
            "  <manifest>\n"
            + "\n".join(manifest)
            + "\n  </manifest>\n"
            "  <spine toc=\"ncx\">\n" + "\n".join(spine) + "\n  </spine>\n"
            "</package>\n"
        )

    # ---------- 写盘 ----------

    @staticmethod
    def _write(archive: zipfile.ZipFile, name: str, text: str, moment) -> None:
        """写一个文本条目（UTF-8、带固定时间戳，保证体积稳定）。"""
        info = zipfile.ZipInfo(name, date_time=moment.timetuple()[:6])
        info.compress_type = zipfile.ZIP_DEFLATED
        info.external_attr = 0o600 << 16
        archive.writestr(info, text.encode("utf-8"))

    @staticmethod
    def _write_bytes(archive: zipfile.ZipFile, name: str, data: bytes, moment) -> None:
        """写一个二进制条目（图片本身已压缩，用 STORED）。"""
        info = zipfile.ZipInfo(name, date_time=moment.timetuple()[:6])
        info.compress_type = zipfile.ZIP_STORED
        info.external_attr = 0o600 << 16
        archive.writestr(info, data)

    def _write_epub(
        self,
        archive: zipfile.ZipFile,
        thread: ThreadData,
        posts: list[Post],
        images: list[tuple[str, bytes]],
        image_mode: str,
    ) -> None:
        moment = _now_utc()
        book_id = f"urn:uuid:{uuid.uuid4()}"

        # mimetype 必须是第一个条目，且不压缩、内容不含换行。
        info = zipfile.ZipInfo("mimetype", date_time=moment.timetuple()[:6])
        info.compress_type = zipfile.ZIP_STORED
        info.external_attr = 0o600 << 16
        archive.writestr(info, EPUB_MIMETYPE.encode("ascii"))

        self._write(archive, "META-INF/container.xml", CONTAINER_XML, moment)
        self._write(archive, "OEBPS/style.css", STYLE_CSS, moment)

        entries = self._entries(posts, thread)
        for index, post in enumerate(posts, start=1):
            xhtml = self._render_post_xhtml(index, post, thread, image_mode)
            self._write(archive, f"{TEXT_DIR}/post-{index:04d}.xhtml", xhtml, moment)

        self._write(archive, "OEBPS/nav.xhtml", self._render_nav(entries), moment)
        self._write(
            archive,
            "OEBPS/toc.ncx",
            self._render_ncx(thread, entries, book_id),
            moment,
        )
        self._write(
            archive,
            "OEBPS/content.opf",
            self._render_opf(thread, entries, images, book_id, _iso_utc(moment)),
            moment,
        )
        for name, data in images:
            self._write_bytes(archive, name, data, moment)


__all__ = ["EpubBuilder"]
