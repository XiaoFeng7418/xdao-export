"""Markdown 导出器：生成便于二次编辑、粘贴到博客的纯文本。

文本处理、文件名推导、图片解析都复用 ``._shared``，与其它导出器保持一致。
图片处理方式（``image_mode``）与 HTML 对齐：``url`` 只留链接、``drop`` 丢掉、
``folder`` 存到成品旁边的同名目录。Markdown 是纯文本，嵌 base64 只会把文件
撑成几兆的乱码，所以 ``embed``（默认设置）按 ``url`` 处理。
"""

from __future__ import annotations

import datetime as _datetime
from pathlib import Path

from ..client import Post
from ._shared import (
    ImageFolderWriter,
    ThreadData,
    attachment_image_url,
    plain_text,
    render_inline_content,
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

    #: Markdown 认得的图片处理方式；``embed`` 在纯文本里没有意义（一长串 base64），
    #: 一律按 ``url`` 处理，输出与旧版逐字节一致。
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
        self.image_mode = mode if mode in self.IMAGE_MODES else "url"
        self._images = ImageFolderWriter(self._notify)

    def prepare_output(self, path: Path) -> None:
        """「存到同名目录」需要先知道成品落在哪（基类 ``save()`` 建 build 之前调）。"""
        if self.image_mode == "folder":
            self._images.prepare(path)

    def _image_target(self, url: str) -> str:
        """目录模式：下载并写盘，返回正文里该用的相对路径（失败返回空串）。"""
        return self._images.write(self._client, url)

    def _render_post(self, index: int, post: Post, thread: ThreadData) -> str:
        """单楼渲染。"""
        is_po = thread.is_po_post(post)
        prefix = "[PO] " if is_po else ""
        cookie = post.user_hash or "未知"
        lines = [f"## {index}. {prefix}{cookie} · {post.now or ''} · No.{post.id}"]

        notes = [f"饼干 `{cookie}`"]
        if post.admin:
            notes.append("红名")
        name = plain_text(post.name or "")
        if name and name != "无名氏":
            notes.append(name)
        title = plain_text(post.title or "")
        if title and title not in ("无标题", ""):
            notes.append(title)
        lines.append("> " + " · ".join(notes))

        if self.image_mode == "drop":
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
        if body:
            lines.append("")
            lines.append(body)

        # 正文之外的附件图单独附在正文后面。
        attachment = attachment_image_url(self._client, post)
        if attachment and self.image_mode != "drop":
            target = attachment
            if self.image_mode == "folder":
                target = self._image_target(attachment) or attachment
            lines.append("")
            lines.append(f"![图片]({target})")

        lines.append("")
        lines.append("---")
        return "\n".join(lines)

    def build(
        self,
        thread: ThreadData,
        scope: str = "all",
        include_hashes: list[str] | None = None,
    ) -> str:
        """scope: 'all' 导出全部楼层，'po' 只导出 PO（主帖始终保留）。"""
        export_time = _datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        posts = thread.filter_posts(scope, include_hashes)

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
            lines.append(self._render_post(index, post, thread))
            lines.append("")
        self._images.report()
        return "\n".join(lines)

    def build_many(
        self,
        threads: list[ThreadData],
        scope: str = "all",
        include_hashes: list[str] | None = None,
    ) -> str:
        """多个串拼一份：每串仍用 :meth:`build` 生成，之间用分隔线隔开。"""
        self._notify(f"正在生成合并 Markdown（{len(threads)} 个串）…")
        blocks = [self.build(thread, scope, include_hashes) for thread in threads]
        return "\n\n---\n\n".join(blocks)


__all__ = ["MarkdownBuilder"]
