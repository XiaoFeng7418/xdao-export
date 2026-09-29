"""把抓到的串渲染成独立 HTML 文件。"""

from __future__ import annotations

import base64
import concurrent.futures
import html as html_lib
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path

from .client import Post, XdaoClient


_TAG_RE = re.compile(r"<[^>]+>")


def plain_text(value: str) -> str:
    """去掉 HTML 标记，保留可读文本。"""
    value = re.sub(r"<br\s*/?>", "\n", value, flags=re.IGNORECASE)
    value = _TAG_RE.sub("", value)
    value = html_lib.unescape(value)
    return value.strip()


def sanitize_filename(name: str, fallback: str = "xdao-thread") -> str:
    cleaned = re.sub(r'[\\/:*?"<>|]', "_", name).strip()
    cleaned = cleaned.strip(" .")
    if len(cleaned) > 80:
        cleaned = cleaned[:80]
    return cleaned or fallback


@dataclass
class ThreadData:
    thread_id: int
    title: str
    po_hash: str
    posts: list[Post]


class HtmlBuilder:
    def __init__(self, client: XdaoClient, progress=None) -> None:
        self._client = client
        self._progress = progress
        self._image_cache: dict[str, str] = {}

    def _notify(self, message: str) -> None:
        if self._progress:
            self._progress(message)

    def resolve_image_url(self, src: str) -> str:
        if not src:
            return ""
        if src.startswith("http://") or src.startswith("https://"):
            return src
        # 相对路径补全 CDN 前缀。
        base = self._client.cdn_path
        if not base.endswith("/"):
            base += "/"
        return base + src.lstrip("/")

    def embed_image(self, url: str) -> str:
        if not url:
            return ""
        if url in self._image_cache:
            return self._image_cache[url]
        self._notify(f"正在下载图片：{url}")
        try:
            data = self._client.download_image(url)
            mime = "image/gif" if url.lower().endswith(".gif") else "image/jpeg"
            if url.lower().endswith(".png"):
                mime = "image/png"
            b64 = base64.b64encode(data).decode("ascii")
            self._image_cache[url] = f"data:{mime};base64,{b64}"
        except Exception:
            # 单张图下载失败时保留原链接，不让整个串导出失败。
            self._image_cache[url] = url
        return self._image_cache[url]

    def render_content(self, content: str, po_hash: str) -> str:
        """渲染正文，把内部图片替换为内嵌数据。"""
        if not content:
            return ""

        class _Renderer(HTMLParser):
            def __init__(self, owner: "HtmlBuilder") -> None:
                super().__init__(convert_charrefs=True)
                self.owner = owner
                self.out: list[str] = []

            def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
                tag = tag.lower()
                if tag == "br":
                    self.out.append("<br>")
                elif tag == "img":
                    src = dict(attrs).get("src") or ""
                    url = self.owner.resolve_image_url(src)
                    embedded = self.owner.embed_image(url)
                    self.out.append(
                        f'<img class="post-img" src="{html_lib.escape(embedded)}" alt="图片">'
                    )
                # 其它标签只保留文本内容，不保留样式，保证干净、可打印。

            def handle_data(self, data: str) -> None:
                self.out.append(html_lib.escape(data))

        renderer = _Renderer(self)
        renderer.feed(content)
        renderer.close()
        return "".join(renderer.out)

    def build(self, thread: ThreadData, scope: str) -> str:
        """scope: 'all' 或 'po'。"""
        self._preload_images(thread, scope)
        title_text = plain_text(thread.title)
        heading = title_text or plain_text(thread.posts[0].content if thread.posts else "")

        rows: list[str] = []
        for index, post in enumerate(thread.posts):
            if scope == "po" and not post.is_po:
                continue
            # 与发串人同一饼干也算 PO。
            is_po = post.is_po or post.user_hash == thread.po_hash
            if scope == "po" and not is_po:
                continue

            meta_parts = []
            if is_po:
                meta_parts.append('<span class="badge-po">PO</span>')
            if post.admin:
                meta_parts.append('<span class="badge-admin">红名</span>')
            meta_parts.append(f'<span class="cookie">饼干 {html_lib.escape(post.user_hash)}</span>')
            if post.name and post.name not in ("无名氏", ""):
                meta_parts.append(f'<span class="aname">作者 {html_lib.escape(plain_text(post.name))}</span>')
            if post.title and post.title not in ("无标题",):
                meta_parts.append(f'<span class="ptitle">{html_lib.escape(plain_text(post.title))}</span>')
            meta_parts.append(f'<span class="time">{html_lib.escape(post.now)}</span>')
            meta_parts.append(f'<span class="no">No.{post.id}</span>')
            meta_html = "".join(meta_parts)

            body = self.render_content(post.content, thread.po_hash)
            if post.img and post.ext:
                url = self._client.image_url(post.img, post.ext)
                embedded = self.embed_image(url)
                if embedded.startswith("data:"):
                    body += f'<img class="post-img" src="{embedded}" alt="图片">'
                else:
                    body += f'<img class="post-img" src="{html_lib.escape(embedded)}" alt="图片">'

            cls = "post po" if is_po else "post"
            rows.append(
                f'<article class="{cls}">'
                f'<header class="meta">{meta_html}</header>'
                f'<div class="content">{body}</div>'
                "</article>"
            )

        posts_html = "\n".join(rows)
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
  .cookie {{ font-weight: 700; color: #15803d; font-family: Consolas, monospace; }}
  .aname {{ font-weight: 600; color: #334155; }}
  .badge-po {{ background: var(--po); color: #fff; padding: 1px 8px; border-radius: 4px; font-weight: 700; font-size: 11px; }}
  .badge-admin {{ background: #b91c1c; color: #fff; padding: 1px 8px; border-radius: 4px; font-weight: 700; font-size: 11px; }}
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
<div class="sub">串号 No.{thread.thread_id} · 共 {len(rows)} 楼 · {html_lib.escape(thread.po_hash and f"PO 饼干 {thread.po_hash}" or "PO")}</div>
{posts_html}
</div>
</body>
</html>
"""

    def _iter_post_images(self, post: Post) -> list[str]:
        urls: list[str] = []
        if post.img and post.ext:
            urls.append(self._client.image_url(post.img, post.ext))
        for match in re.findall(r'<img[^>]+src="([^"]+)"', post.content, flags=re.IGNORECASE):
            urls.append(self.resolve_image_url(html_lib.unescape(match)))
        return urls

    def _preload_images(self, thread: ThreadData, scope: str) -> None:
        wanted: list[tuple[Post, str]] = []
        for post in thread.posts:
            if scope == "po" and not (post.is_po or post.user_hash == thread.po_hash):
                continue
            for url in self._iter_post_images(post):
                if url and url not in self._image_cache:
                    wanted.append((post, url))
        if not wanted:
            return

        def load(item: tuple[Post, str]) -> None:
            _, url = item
            self.embed_image(url)

        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as executor:
            list(executor.map(load, wanted))

    def save(self, thread: ThreadData, scope: str, output_dir: Path) -> Path:
        output_dir.mkdir(parents=True, exist_ok=True)
        self._notify("正在整理并保存 HTML 文件…")
        filename = sanitize_filename(self._derive_filename(thread)) + ".html"
        path = output_dir / filename
        html_doc = self.build(thread, scope)
        path.write_text(html_doc, encoding="utf-8")
        return path

    @staticmethod
    def _derive_filename(thread: ThreadData) -> str:
        title = plain_text(thread.title)
        if title and title not in ("无标题",):
            return title
        # 无标题时，取第一楼正文的第一句话。
        for post in thread.posts:
            text = plain_text(post.content)
            if not text:
                continue
            first = re.split(r"[\n。！？!?]", text)[0].strip()
            if first:
                return first
        return f"串{thread.thread_id}"


def derive_filename(thread: ThreadData) -> str:
    """共享的文件名推导逻辑。"""
    title = plain_text(thread.title)
    if title and title not in ("无标题",):
        return title
    for post in thread.posts:
        text = plain_text(post.content)
        if not text:
            continue
        first = re.split(r"[\n。！？!?]", text)[0].strip()
        if first:
            return first
    return f"串{thread.thread_id}"


class TxtBuilder:
    """纯文本导出器。"""

    def __init__(self, client: XdaoClient, progress=None) -> None:
        self._client = client
        self._progress = progress

    def _notify(self, message: str) -> None:
        if self._progress:
            self._progress(message)

    def _content_images(self, content: str) -> list[str]:
        urls = []
        for match in re.findall(r'<img[^>]+src="([^"]+)"', content, flags=re.IGNORECASE):
            src = html_lib.unescape(match)
            if src.startswith("http://") or src.startswith("https://"):
                urls.append(src)
            else:
                base = self._client.cdn_path
                if not base.endswith("/"):
                    base += "/"
                urls.append(base + src.lstrip("/"))
        return urls

    def build(self, thread: ThreadData, scope: str) -> str:
        self._notify("正在生成 TXT 文件…")
        lines: list[str] = []
        heading = derive_filename(thread)
        lines.append(f"串号 No.{thread.thread_id}")
        lines.append(f"标题：{heading}")
        lines.append(f"PO 饼干：{thread.po_hash or '未知'}")
        lines.append("")
        lines.append("=" * 48)
        lines.append("")

        for post in thread.posts:
            is_po = post.is_po or post.user_hash == thread.po_hash
            if scope == "po" and not is_po:
                continue
            tag = "[PO]" if is_po else "    "
            parts = [tag, f"饼干 {post.user_hash}"]
            if post.name and post.name not in ("无名氏", ""):
                parts.append(f"作者 {post.name}")
            if post.title and post.title not in ("无标题", ""):
                parts.append(f"标题 {plain_text(post.title)}")
            parts.append(str(post.now))
            parts.append(f"No.{post.id}")
            meta = "  |  ".join(parts)
            lines.append(meta)
            content = plain_text(post.content)
            if content:
                lines.append(content)
            for url in self._content_images(post.content):
                lines.append(f"[图片] {url}")
            if post.img and post.ext:
                lines.append(f"[图片] {self._client.image_url(post.img, post.ext)}")
            lines.append("-" * 48)
            lines.append("")
        return "\n".join(lines)

    def save(self, thread: ThreadData, scope: str, output_dir: Path) -> Path:
        output_dir.mkdir(parents=True, exist_ok=True)
        filename = sanitize_filename(derive_filename(thread)) + ".txt"
        path = output_dir / filename
        text = self.build(thread, scope)
        path.write_text(text, encoding="utf-8")
        return path
