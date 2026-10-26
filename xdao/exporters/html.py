"""HTML 导出器：把串渲染成单文件、可离线打开与打印的网页。"""

from __future__ import annotations

import base64
import concurrent.futures
import html as html_lib
import threading
import urllib.parse
from pathlib import Path

from ..client import Post, XdaoClient
from ._shared import (
    ThreadData,
    fetch_image,
    guess_mime,
    iter_post_image_urls,
    plain_text,
    resolve_image_url,
    sanitize_filename,
)
from .base import Exporter

#: 正文的默认字号（px）。v0.15.0 之前它写死在下面的 CSS 里，**默认必须还是 14** ——
#: 老配置里没有 ``font_size`` 这个键，成品要和以前逐字节一致（有专门的用例钉着）。
DEFAULT_FONT_SIZE = 14


def _font_size(value, fallback: int = DEFAULT_FONT_SIZE) -> int:
    """认一下传进来的字号；非数字、0、负数一律回落默认。"""
    try:
        size = int(value)
    except (TypeError, ValueError):
        return fallback
    return size if size > 0 else fallback


def _font_px(font_size: int, base: int) -> int:
    """把原本写死的 ``base`` 号字，按 ``font_size`` / 默认字号 的比例换算。

    分母是默认字号（14）而不是某个魔数：字号为默认时，每个换算结果都原样等于
    以前写死的那个 px 值，CSS 一个字节都不变。
    """
    return max(1, int(round(font_size * base / DEFAULT_FONT_SIZE)))


#: 正文里图片的处理方式：内嵌 / 只留链接 / 丢掉 / 存到成品旁边的同名目录。
#: ``folder`` 是 HTML 独有的：图片写成 ``<成品名>.files/`` 下的文件，正文用相对路径引用。
IMAGE_MODES = ("embed", "url", "drop", "folder")


def _image_mode(value, fallback: str = "embed") -> str:
    """认一下图片处理方式；认不出就回落默认（内嵌）。"""
    mode = str(value or "").strip().lower()
    return mode if mode in IMAGE_MODES else fallback


def _style_block(body_px: int) -> str:
    """整份 CSS（不含 ``<style>`` 标签）：字号都从正文磅数等比换算。

    抽出来给 :meth:`HtmlBuilder.build` 与 :meth:`HtmlBuilder.build_many` 共用 ——
    单串导出的 HTML 必须与旧版逐字节一致，所以这里只搬字符串、不改内容。
    """
    h1_px = _font_px(body_px, 18)
    sub_px = _font_px(body_px, 12)
    meta_px = _font_px(body_px, 11)
    print_px = _font_px(body_px, 13)
    return f"""  :root {{ --po: #d97706; --po-bg: #fff7ed; --line: #e2e8f0; --text: #1f2430; --muted: #667085; }}
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; background: #f4f6fa; color: var(--text); font-family: "Segoe UI", "Microsoft YaHei", system-ui, sans-serif; line-height: 1.35; }}
  .wrap {{ max-width: 920px; margin: 0 auto; padding: 16px 14px 24px; }}
  h1 {{ font-size: {h1_px}px; margin: 0 0 4px; word-break: break-word; }}
  .sub {{ color: var(--muted); font-size: {sub_px}px; margin-bottom: 12px; }}
  article.post {{ background: #fff; border: 1px solid var(--line); border-left: 4px solid #cbd5e1; border-radius: 6px; padding: 6px 10px; margin: 5px 0; }}
  article.post.po {{ border-left-color: var(--po); background: var(--po-bg); }}
  .meta {{ display: flex; flex-wrap: wrap; gap: 5px; align-items: center; font-size: {meta_px}px; color: var(--muted); }}
  .floor {{ color: #cbd5e1; font-family: Consolas, monospace; }}
  .cookie {{ font-weight: 700; color: #15803d; font-family: Consolas, monospace; }}
  .aname {{ font-weight: 600; color: #334155; }}
  .badge-po {{ background: var(--po); color: #fff; padding: 1px 8px; border-radius: 4px; font-weight: 700; font-size: {meta_px}px; }}
  .badge-admin {{ background: #b91c1c; color: #fff; padding: 1px 8px; border-radius: 4px; font-weight: 700; font-size: {meta_px}px; }}
  .badge-sage {{ background: #6366f1; color: #fff; padding: 1px 8px; border-radius: 4px; font-weight: 700; font-size: {meta_px}px; }}
  .ptitle {{ font-weight: 600; color: #334155; }}
  .time, .no {{ color: #94a3b8; }}
  .content {{ margin-top: 3px; font-size: {body_px}px; word-break: break-word; white-space: normal; }}
  .content br {{ display: block; content: ""; margin: 1px 0; }}
  .post-img {{ max-width: 100%; height: auto; display: block; margin: 5px 0 0; border-radius: 4px; }}
  @media print {{
    body {{ background: #fff; }}
    .wrap {{ max-width: none; padding: 0; }}
    body {{ font-size: {print_px}px; line-height: 1.3; }}
    article.post {{ break-inside: avoid; margin: 3px 0; padding: 5px 8px; }}
  }}"""


class HtmlBuilder(Exporter):
    """生成单文件 HTML（图片怎么处理由 image_mode 决定）。

    - 图片默认以 data URI 内嵌，导出的文件可以单独转发、离线打开、直接打印成 PDF。
    - ``image_mode="url"`` 只留外链、``"drop"`` 直接丢掉，大图串的体积能降一个量级。
    - ``image_mode="folder"`` 把图片写成成品旁边的 ``.files`` 目录（单文件不再单文件，
      但体积小、打开快，图片还能单独取用）；PDF 在内存里渲染，没有同名成品，退回内嵌。
    - 单张图片失败时退回原始链接，不让整个串导出失败。
    - 同一张图片在多个楼层出现只下载一次。
    """

    key = "html"
    #: 合并导出：多个串写进同一份 HTML，每串一节。
    supports_merge = True
    display = "HTML（单文件）"
    suffix = ".html"
    save_message = "正在整理并保存 HTML 文件…"

    # 内嵌图片的内存缓存上限，超过就丢弃，避免超长串把内存吃满。
    MAX_CACHE_ENTRIES = 2000

    def __init__(
        self,
        client: XdaoClient,
        progress=None,
        filename_template: str | None = None,
        **options,
    ) -> None:
        super().__init__(client, progress, filename_template, **options)
        self._image_cache: dict[str, str] = {}
        # 图片怎么处理：embed＝内嵌（默认）、url＝只留外链、drop＝丢掉、folder＝存到同名目录。
        self.image_mode = _image_mode(options.get("image_mode"))
        # 存到同名目录时要用的两个值：目录本身、目录名（正文里写相对路径用）。
        # 没经过 save()（例如 PDF 在内存里渲染 HTML）时它们是 None/空串，按内嵌处理。
        self._image_dir: Path | None = None
        self._image_rel = ""
        self._folder_names: set[str] = set()
        self._folder_serial = 0
        # 正文字号（px）：用户设的那个值；没设过就是 14（＝v0.15.0 之前的写死值）
        self.font_size = _font_size(options.get("font_size"))
        # 没下到的图片地址（按出现顺序）：单张失败只在日志里提一次，
        # 截到 MAX_MISSING_LOGS 条为止，剩下留到 build 结束时汇总成一句。
        self.missing_images: list[str] = []
        # 并发预取期间由 _preload_images 置位：这时进度由它统一报「已下载 x/y」，
        # embed_image 不再逐张刷 URL（六条线程交错打，日志会变成一锅粥）。
        self._preloading = False

    #: 日志里逐张列出失败图片的上限，超过就只报总数（图多的串不至于刷屏）。
    MAX_MISSING_LOGS = 10

    #: 存到目录时按 MIME 给扩展名；认不出就写 .img（浏览器多半也能按内容猜出来）。
    _IMAGE_SUFFIXES = {
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/gif": ".gif",
        "image/webp": ".webp",
    }

    # ---------- 图片 ----------

    def prepare_output(self, path: Path) -> None:
        """记下成品的落盘位置（基类的 ``save()`` 在建 build 之前调）。"""
        if self.image_mode != "folder":
            return
        folder = path.with_name(path.stem + ".files")
        self._image_dir = folder
        self._image_rel = folder.name

    def resolve_image_url(self, src: str) -> str:
        return resolve_image_url(self._client, src)

    def embed_image(self, url: str) -> str:
        """按 image_mode 给出正文里这张图的 src；返回空串表示丢掉。"""
        if not url:
            return ""
        if url.startswith("data:"):
            return url
        if self.image_mode == "drop":
            return ""
        if self.image_mode == "url":
            return url
        # 「存到同名目录」得先有落盘位置；没有（PDF 内存渲染）就退回内嵌。
        folder_mode = self.image_mode == "folder" and self._image_dir is not None
        cached = self._image_cache.get(url)
        if cached is not None:
            return cached

        if not self._preloading:
            self._notify(f"正在下载图片：{url}")
        data = fetch_image(self._client, url)
        value = ""
        if data:
            if folder_mode:
                value = self._write_image_file(url, data)
            else:
                mime = guess_mime(url, data)
                b64 = base64.b64encode(data).decode("ascii")
                value = f"data:{mime};base64,{b64}"
        if not value:
            # 下载失败（或图片存不进目录）时保留原链接；日志里前几张逐条提，再多就汇总。
            if url not in self.missing_images:
                self.missing_images.append(url)
            if len(self.missing_images) <= self.MAX_MISSING_LOGS:
                reason = "图片下载失败" if data is None else "图片存不进目录"
                self._notify(f"{reason}，已保留链接：{url}")
            value = url

        if len(self._image_cache) < self.MAX_CACHE_ENTRIES:
            self._image_cache[url] = value
        return value

    def _image_file_name(self, url: str, data: bytes) -> str:
        """给要落盘的图起个不重名的文件名（保留原名的词根，便于翻找）。"""
        mime = guess_mime(url, data)
        suffix = self._IMAGE_SUFFIXES.get(mime, ".img")
        stem = ""
        try:
            stem = Path(urllib.parse.urlsplit(url).path).stem
        except (TypeError, ValueError):
            stem = ""
        stem = sanitize_filename(stem, fallback="") if stem else ""
        if not stem:
            stem = f"img{len(self._folder_names) + 1:03d}"
        name = f"{stem}{suffix}"
        # 同名不同图（不同域名、带不同参数）会互相覆盖，加序号区分。
        while name in self._folder_names:
            self._folder_serial += 1
            name = f"{stem}-{self._folder_serial}{suffix}"
        self._folder_names.add(name)
        return name

    def _write_image_file(self, url: str, data: bytes) -> str:
        """把图片写进成品旁边的同名目录，返回正文里要用的相对路径；失败返回空串。"""
        if self._image_dir is None:
            return ""
        try:
            self._image_dir.mkdir(parents=True, exist_ok=True)
            name = self._image_file_name(url, data)
            (self._image_dir / name).write_bytes(data)
        except OSError:
            return ""
        # 相对地址一律用正斜杠，空格/中文按 URL 规则转义（浏览器才不会把空格当分隔符）。
        return urllib.parse.quote(f"{self._image_rel}/{name}", safe="/")

    def _report_missing_images(self) -> None:
        """把没下到的图片汇总成一句（失败很多时日志不再逐条刷）。"""
        count = len(self.missing_images)
        if count <= self.MAX_MISSING_LOGS:
            return
        urls = "、".join(self.missing_images[:3])
        self._notify(
            f"一共 {count} 张图片没下到，已保留原链接（逐条日志只列了前 {self.MAX_MISSING_LOGS} 张）；"
            f"前三张是：{urls}"
        )

    # ---------- 正文 ----------

    def render_content(self, content: str, po_hash: str) -> str:
        """渲染一楼正文：保留换行与图片，丢弃其它标签与样式。"""
        if not content:
            return ""

        from html.parser import HTMLParser
        import re

        class _Renderer(HTMLParser):
            def __init__(self, owner: "HtmlBuilder") -> None:
                super().__init__(convert_charrefs=True)
                self.owner = owner
                self.out: list[str] = []

            def handle_starttag(self, tag, attrs) -> None:
                tag = tag.lower()
                if tag == "br":
                    self.out.append("<br>")
                elif tag == "img":
                    src = dict(attrs).get("src") or ""
                    url = self.owner.resolve_image_url(src)
                    embedded = self.owner.embed_image(url)
                    if embedded:
                        self.out.append(
                            f'<img class="post-img" src="{html_lib.escape(embedded, quote=True)}" alt="图片" loading="lazy">'
                        )
                else:
                    # 其它标签不保留样式，但标签本身要转义后留作可见文本：
                    # 存档工具不该让正文里写的 <script> 这类片段凭空消失。
                    self.out.append(html_lib.escape(self.get_starttag_text() or f"<{tag}>"))

            def handle_startendtag(self, tag, attrs) -> None:
                tag = tag.lower()
                if tag == "br":
                    self.out.append("<br>")
                elif tag == "img":
                    self.handle_starttag(tag, attrs)
                else:
                    self.out.append(html_lib.escape(self.get_starttag_text() or f"<{tag}/>"))

            def handle_endtag(self, tag) -> None:
                tag = tag.lower()
                if tag in ("br", "img"):
                    return
                self.out.append(html_lib.escape(f"</{tag}>"))

            def handle_data(self, data: str) -> None:
                self.out.append(html_lib.escape(data))

        renderer = _Renderer(self)
        renderer.feed(content)
        renderer.close()
        text = "".join(renderer.out)
        # 连续空行压缩，避免楼层之间出现大片空白。
        return re.sub(r"(?:\s*<br>\s*){3,}", "<br><br>", text).strip()

    def _post_meta_html(self, post: Post, is_po: bool) -> str:
        parts: list[str] = []
        if is_po:
            parts.append('<span class="badge-po">PO</span>')
        if post.admin:
            parts.append('<span class="badge-admin">红名</span>')
        if post.sage:
            parts.append('<span class="badge-sage">SAGE</span>')
        parts.append(f'<span class="cookie" title="user_hash">饼干 {html_lib.escape(post.user_hash)}</span>')
        if post.name and post.name not in ("无名氏", ""):
            parts.append(f'<span class="aname">作者 {html_lib.escape(plain_text(post.name))}</span>')
        if post.title and post.title not in ("无标题",):
            parts.append(f'<span class="ptitle">{html_lib.escape(plain_text(post.title))}</span>')
        parts.append(f'<span class="time">{html_lib.escape(post.now)}</span>')
        parts.append(f'<span class="no">No.{post.id}</span>')
        return "".join(parts)

    def build(
        self,
        thread: ThreadData,
        scope: str = "all",
        include_hashes: list[str] | None = None,
    ) -> str:
        """生成完整 HTML 文档。"""
        posts = thread.filter_posts(scope, include_hashes)
        self._preload_images(thread, posts)

        title_text = plain_text(thread.title)
        heading = title_text or plain_text(thread.posts[0].content if thread.posts else "")
        heading = heading or f"串 {thread.thread_id}"

        rows: list[str] = []
        for index, post in enumerate(posts, start=1):
            is_po = thread.is_po_post(post)
            body = self.render_content(post.content, thread.po_hash)
            if post.img and post.ext:
                url = self._client.image_url(post.img, post.ext)
                embedded = self.embed_image(url)
                if embedded:
                    body += f'<img class="post-img" src="{html_lib.escape(embedded, quote=True)}" alt="图片" loading="lazy">'

            cls = "post po" if is_po else "post"
            rows.append(
                f'<article class="{cls}" id="p{post.id}">'
                f'<header class="meta"><span class="floor">#{index}</span>{self._post_meta_html(post, is_po)}</header>'
                f'<div class="content">{body}</div>'
                "</article>"
            )

        posts_html = "\n".join(rows)
        body_px = self.font_size
        scope_label = "仅 PO 发言" if scope == "po" and not include_hashes else "全部发言"
        if include_hashes:
            scope_label = "指定饼干：" + "、".join(include_hashes)

        style_block = _style_block(body_px)
        document = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html_lib.escape(heading)}</title>
<style>
{style_block}
</style>
</head>
<body>
<div class="wrap">
<h1>{html_lib.escape(heading)}</h1>
<div class="sub">串号 No.{thread.thread_id} · 共 {len(rows)} 楼 · {html_lib.escape(scope_label)} · PO 饼干 {html_lib.escape(thread.po_hash or "未知")}</div>
{posts_html}
</div>
</body>
</html>
"""
        self._report_missing_images()
        return document

    def build_many(
        self,
        threads: list[ThreadData],
        scope: str = "all",
        include_hashes: list[str] | None = None,
    ) -> str:
        """把多个串写进同一份 HTML（v0.21.0 合并导出）。

        每串一节（``<section class="thread">``），标题带上串号与楼数；样式与单串
        完全同一份（:func:`_style_block`），只多两条给节标题用的规则。
        单串导出走的还是 :meth:`build`，本方法不碰它，所以旧成品逐字节不变。
        """
        if not threads:
            raise ValueError("合并导出至少要有一个串")

        scope_label = "仅 PO 发言" if scope == "po" and not include_hashes else "全部发言"
        if include_hashes:
            scope_label = "指定饼干：" + "、".join(include_hashes)

        sections: list[str] = []
        for index, thread in enumerate(threads, start=1):
            posts = thread.filter_posts(scope, include_hashes)
            self._preload_images(thread, posts)

            title_text = plain_text(thread.title)
            heading = title_text or plain_text(thread.posts[0].content if thread.posts else "")
            heading = heading or f"串 {thread.thread_id}"

            rows: list[str] = []
            for floor, post in enumerate(posts, start=1):
                is_po = thread.is_po_post(post)
                body = self.render_content(post.content, thread.po_hash)
                if post.img and post.ext:
                    url = self._client.image_url(post.img, post.ext)
                    embedded = self.embed_image(url)
                    if embedded:
                        body += (
                            f'<img class="post-img" src="{html_lib.escape(embedded, quote=True)}"'
                            ' alt="图片" loading="lazy">'
                        )
                cls = "post po" if is_po else "post"
                rows.append(
                    f'<article class="{cls}" id="p{post.id}">'
                    f'<header class="meta"><span class="floor">#{floor}</span>'
                    f"{self._post_meta_html(post, is_po)}</header>"
                    f'<div class="content">{body}</div>'
                    "</article>"
                )
            sections.append(
                f'<section class="thread" id="t{thread.thread_id}">'
                f'<h2 class="thread-title">{index}. {html_lib.escape(heading)}</h2>'
                f'<div class="sub">串号 No.{thread.thread_id} · 共 {len(rows)} 楼 · '
                f'{html_lib.escape(scope_label)} · PO 饼干 '
                f'{html_lib.escape(thread.po_hash or "未知")}</div>'
                f'{"\n".join(rows)}'
                "</section>"
            )

        first = plain_text(threads[0].title) or f"串 {threads[0].thread_id}"
        heading_title = f"{first} 等 {len(threads)} 个串"
        body_px = self.font_size
        style_block = _style_block(body_px)
        print_extra = (
            "  .thread-title { font-size: %d px; margin: 18px 0 2px; }\n"
            "  section.thread { margin-bottom: 16px; }\n"
            "  section.thread + section.thread { border-top: 2px solid var(--line); padding-top: 14px; }\n"
            % _font_px(body_px, 16)
        )
        document = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html_lib.escape(heading_title)}</title>
<style>
{style_block}
{print_extra}</style>
</head>
<body>
<div class="wrap">
<h1>{html_lib.escape(heading_title)}</h1>
<div class="sub">合并导出：共 {len(threads)} 个串 · {html_lib.escape(scope_label)}</div>
{"".join(sections)}
</div>
</body>
</html>
"""
        self._report_missing_images()
        return document

    # ---------- 图片预取 ----------

    def _preload_images(self, thread: ThreadData, posts: list[Post]) -> None:
        """并发预取图片，让后续渲染阶段不必串行等待。"""
        if self.image_mode not in ("embed", "folder"):
            # 只留外链 / 丢掉图片时根本不用下载，省掉整趟网络。
            return
        wanted: list[str] = []
        for post in posts:
            for url in iter_post_image_urls(self._client, post):
                if url and url not in self._image_cache:
                    wanted.append(url)
        if not wanted:
            return

        self._notify(f"正在下载 {len(wanted)} 张图片…")

        total = len(wanted)
        # 每完成 10% 报一次进度：图多的串里，从「正在下载 120 张」到结束之间
        # 原来一句提示都没有，看起来像卡住了。步长取整且至少 1，短列表也不会刷屏。
        step = max(1, total // 10)
        lock = threading.Lock()
        done = 0

        def load(url: str) -> None:
            nonlocal done
            # 取消时在下载线程里就抛出去，executor.map 会把它带回主线程。
            self.check_cancel()
            self.embed_image(url)
            with lock:
                done += 1
                finished = done
            if finished % step == 0 or finished == total:
                self._notify(f"正在下载图片：{finished}/{total}")

        self._preloading = True
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=6) as executor:
                list(executor.map(load, wanted))
        finally:
            self._preloading = False
