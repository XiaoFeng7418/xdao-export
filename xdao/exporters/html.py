"""HTML 导出器：把串渲染成单文件、可离线打开与打印的网页。"""

from __future__ import annotations

import base64
import concurrent.futures
import html as html_lib

from ..client import Post, XdaoClient
from ._shared import (
    ThreadData,
    fetch_image,
    guess_mime,
    iter_post_image_urls,
    plain_text,
    resolve_image_url,
)
from .base import Exporter


class HtmlBuilder(Exporter):
    """生成图片内嵌的 HTML。

    - 图片以 data URI 内嵌，导出的文件可以单独转发、离线打开、直接打印成 PDF。
    - 单张图片失败时退回原始链接，不让整个串导出失败。
    - 同一张图片在多个楼层出现只下载一次。
    """

    key = "html"
    display = "HTML（图片嵌入）"
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

    # ---------- 图片 ----------

    def resolve_image_url(self, src: str) -> str:
        return resolve_image_url(self._client, src)

    def embed_image(self, url: str) -> str:
        """把图片转成 data URI；失败时返回原始 URL。"""
        if not url:
            return ""
        if url.startswith("data:"):
            return url
        cached = self._image_cache.get(url)
        if cached is not None:
            return cached

        self._notify(f"正在下载图片：{url}")
        data = fetch_image(self._client, url)
        if data:
            mime = guess_mime(url, data)
            b64 = base64.b64encode(data).decode("ascii")
            value = f"data:{mime};base64,{b64}"
        else:
            # 单张图下载失败时保留原链接，仅提示一次。
            self._notify(f"图片下载失败，已保留链接：{url}")
            value = url

        if len(self._image_cache) < self.MAX_CACHE_ENTRIES:
            self._image_cache[url] = value
        return value

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
        scope_label = "仅 PO 发言" if scope == "po" and not include_hashes else "全部发言"
        if include_hashes:
            scope_label = "指定饼干：" + "、".join(include_hashes)

        return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html_lib.escape(heading)}</title>
<style>
  :root {{ --po: #d97706; --po-bg: #fff7ed; --line: #e2e8f0; --text: #1f2430; --muted: #667085; }}
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; background: #f4f6fa; color: var(--text); font-family: "Segoe UI", "Microsoft YaHei", system-ui, sans-serif; line-height: 1.35; }}
  .wrap {{ max-width: 920px; margin: 0 auto; padding: 16px 14px 24px; }}
  h1 {{ font-size: 18px; margin: 0 0 4px; word-break: break-word; }}
  .sub {{ color: var(--muted); font-size: 12px; margin-bottom: 12px; }}
  article.post {{ background: #fff; border: 1px solid var(--line); border-left: 4px solid #cbd5e1; border-radius: 6px; padding: 6px 10px; margin: 5px 0; }}
  article.post.po {{ border-left-color: var(--po); background: var(--po-bg); }}
  .meta {{ display: flex; flex-wrap: wrap; gap: 5px; align-items: center; font-size: 11px; color: var(--muted); }}
  .floor {{ color: #cbd5e1; font-family: Consolas, monospace; }}
  .cookie {{ font-weight: 700; color: #15803d; font-family: Consolas, monospace; }}
  .aname {{ font-weight: 600; color: #334155; }}
  .badge-po {{ background: var(--po); color: #fff; padding: 1px 8px; border-radius: 4px; font-weight: 700; font-size: 11px; }}
  .badge-admin {{ background: #b91c1c; color: #fff; padding: 1px 8px; border-radius: 4px; font-weight: 700; font-size: 11px; }}
  .badge-sage {{ background: #6366f1; color: #fff; padding: 1px 8px; border-radius: 4px; font-weight: 700; font-size: 11px; }}
  .ptitle {{ font-weight: 600; color: #334155; }}
  .time, .no {{ color: #94a3b8; }}
  .content {{ margin-top: 3px; font-size: 14px; word-break: break-word; white-space: normal; }}
  .content br {{ display: block; content: ""; margin: 1px 0; }}
  .post-img {{ max-width: 100%; height: auto; display: block; margin: 5px 0 0; border-radius: 4px; }}
  @media print {{
    body {{ background: #fff; }}
    .wrap {{ max-width: none; padding: 0; }}
    body {{ font-size: 13px; line-height: 1.3; }}
    article.post {{ break-inside: avoid; margin: 3px 0; padding: 5px 8px; }}
  }}
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

    # ---------- 图片预取 ----------

    def _preload_images(self, thread: ThreadData, posts: list[Post]) -> None:
        """并发预取图片，让后续渲染阶段不必串行等待。"""
        wanted: list[str] = []
        for post in posts:
            for url in iter_post_image_urls(self._client, post):
                if url and url not in self._image_cache:
                    wanted.append(url)
        if not wanted:
            return

        self._notify(f"正在下载 {len(wanted)} 张图片…")

        def load(url: str) -> None:
            self.embed_image(url)

        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as executor:
            list(executor.map(load, wanted))
