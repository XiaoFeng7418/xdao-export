"""Markdown 导出器：生成便于二次编辑、粘贴到博客的纯文本。

文本处理、文件名推导、图片解析都复用 ``._shared``，与其它导出器保持一致。
图片处理方式（``image_mode``）与 HTML 对齐：``url`` 只留链接、``drop`` 丢掉、
``folder`` 存到成品旁边的同名目录。Markdown 是纯文本，嵌 base64 只会把文件
撑成几兆的文本，所以 X 岛的 ``embed`` 仍按 ``url`` 处理；
归一化多图站点的 ``embed`` 则内嵌完整图片，便于离线留存。
"""

from __future__ import annotations

import base64
import datetime as _datetime
from pathlib import Path

from ..client import Post
from ._shared import (
    ImageFolderWriter,
    ThreadData,
    attachment_image_urls,
    fetch_image,
    guess_mime,
    iter_post_image_urls,
    plain_text,
    render_inline_content,
    render_reference_markdown,
)
from .base import Exporter


class MarkdownBuilder(Exporter):
    """把串导出成 Markdown 文档。"""

    key = "markdown"
    #: 合并导出：多个串共用一个 .md，串与串之间用 ``---`` 隔开。
    supports_merge = True
    display = "Markdown（.md）"
    suffix = ".md"
    save_message = "正在生成 Markdown 文件…"
    # 固定用 \n，避免在 Windows 上写出 CRLF。
    lf_newlines = True

    #: 既有 X 岛输出的图片模式；embed 继续按 url 处理，以保持旧输出一致。
    IMAGE_MODES = ("url", "drop", "folder")

    def __init__(
        self,
        client,
        progress=None,
        filename_template: str | None = None,
        **options,
    ) -> None:
        super().__init__(client, progress, filename_template, **options)
        mode = str(options.get("image_mode") or "").strip().lower()
        self._embed_public = mode in ("", "embed")
        if mode == "dir":
            mode = "folder"
        self.image_mode = mode if mode in self.IMAGE_MODES else "url"
        self._embedded: dict[str, str] = {}
        self._images = ImageFolderWriter(self._notify)
        self.missing_images = self._images.missing

    def prepare_output(self, path: Path) -> None:
        """「存到同名目录」需要先知道成品落在哪（基类 ``save()`` 建 build 之前调）。"""
        if self.image_mode == "folder":
            self._images.prepare(path)

    def _embedded_target(self, url: str) -> str:
        """Embed normalized public-site images; legacy Markdown remains URL-only."""
        if url not in self._embedded:
            self.check_cancel()
            data = fetch_image(self._client, url)
            value = ""
            if data:
                payload = base64.b64encode(data).decode("ascii")
                value = f"data:{guess_mime(url, data)};base64,{payload}"
            elif url not in self.missing_images:
                self.missing_images.append(url)
                self._notify(f"图片下载失败，已保留链接：{url}")
            self._embedded[url] = value
        return self._embedded[url]

    def _image_target(self, url: str) -> str:
        """目录模式：下载并写盘，返回正文里该用的相对路径（失败返回空串）。"""
        return self._images.write(self._client, url)

    def _preload_images(self, posts: list[Post]) -> None:
        """「存到同名目录」时先把图片并发下完，再逐楼渲染。

        不预取也能出成品，但每楼渲染时一张张串行下，图多的串会像卡住，
        而且中途点取消要等当前那张下完才响应。
        """
        if self.image_mode != "folder" or not self._images.enabled:
            return
        urls: list[str] = []
        for post in posts:
            urls.extend(iter_post_image_urls(self._client, post))
        self._images.prefetch(self._client, urls, check_cancel=self.check_cancel)

    def _render_post(
        self,
        index: int,
        post: Post,
        thread: ThreadData,
        reference_targets: dict[int, str] | None = None,
        anchor_prefix: str = "",
    ) -> str:
        """单楼渲染。"""
        is_po = thread.is_po_post(post)
        prefix = "[PO] " if is_po else ""
        cookie = post.user_hash or "未知"
        lines = [f"## {index}. {prefix}{cookie} · {post.now or ''} · No.{post.id}"]
        if thread.site != "xdao":
            anchor = f"{anchor_prefix}{post.id}" if anchor_prefix else f"p{post.id}"
            lines.insert(0, f'<a id="{anchor}"></a>')

        notes = [f"饼干 `{cookie}`"]
        if post.admin:
            notes.append("红名")
        if getattr(post, "emoji", ""):
            notes.append(post.emoji)
        name = plain_text(post.name or "")
        if name and name != "无名氏":
            notes.append(name)
        title = plain_text(post.title or "")
        if title and title not in ("无标题", ""):
            notes.append(title)
        lines.append("> " + " · ".join(notes))

        embed_public = self._embed_public and thread.site != "xdao"
        if embed_public:
            body = render_inline_content(
                self._client, post.content, image_mode="embed", embedder=self._embedded_target
            )
        elif self.image_mode == "drop":
            body = render_inline_content(self._client, post.content, image_mode="drop")
        elif self.image_mode == "folder":
            body = render_inline_content(
                self._client,
                post.content,
                image_mode="embed",
                embedder=self._image_target,
            )
        else:
            body = render_inline_content(self._client, post.content, image_mode="url")
        if thread.site != "xdao":
            body = render_reference_markdown(
                body,
                lambda post_id: (
                    (f"#{anchor_prefix}{post_id}" if anchor_prefix else f"#p{post_id}")
                    if post_id in (reference_targets or {})
                    else ""
                ),
            )
        if body:
            lines.append("")
            lines.append(body)

        # 正文之外的附件图单独附在正文后面。
        for attachment in attachment_image_urls(self._client, post) if self.image_mode != "drop" else []:
            target = attachment
            if embed_public:
                target = self._embedded_target(attachment) or attachment
            elif self.image_mode == "folder":
                target = self._image_target(attachment) or attachment
            lines.append("")
            lines.append(f"![图片]({target})")

        lines.append("")
        lines.append("---")
        return "\n".join(lines)

    def _build(
        self,
        thread: ThreadData,
        scope: str = "all",
        include_hashes: list[str] | None = None,
        *,
        anchor_prefix: str = "",
    ) -> str:
        """Render a thread, optionally namespacing B-island anchors in a merge."""
        export_time = _datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        posts = thread.filter_posts(scope, include_hashes)
        self._preload_images(posts)
        reference_targets = {post.id: post.id for post in posts}

        lines = [
            f"# {thread.title or '无标题'}",
            "",
            f"- 串号:No.{thread.thread_id}",
            f"- PO 饼干:{thread.po_hash or '未知'}",
            f"- 导出楼层:{len(posts)}",
            f"- 导出时间:{export_time}",
            "",
            "---",
            "",
        ]
        for index, post in enumerate(posts, start=1):
            self.check_cancel()
            lines.append(
                self._render_post(index, post, thread, reference_targets, anchor_prefix)
            )
            lines.append("")
        self._images.report()
        return "\n".join(lines)

    def build(
        self,
        thread: ThreadData,
        scope: str = "all",
        include_hashes: list[str] | None = None,
    ) -> str:
        """scope: 'all' 导出全部楼层，'po' 只导出 PO（主帖始终保留）。"""
        return self._build(thread, scope, include_hashes)

    def build_many(
        self,
        threads: list[ThreadData],
        scope: str = "all",
        include_hashes: list[str] | None = None,
    ) -> str:
        """多个串拼一份：每串仍用 :meth:`build` 生成，之间用分隔线隔开。"""
        self._notify(f"正在生成合并 Markdown（{len(threads)} 个串）…")
        blocks = [
            self._build(
                thread,
                scope,
                include_hashes,
                anchor_prefix=f"bog-{index}-" if thread.site == "bog" else "",
            )
            for index, thread in enumerate(threads, start=1)
        ]
        return "\n\n---\n\n".join(blocks)


__all__ = ["MarkdownBuilder"]
