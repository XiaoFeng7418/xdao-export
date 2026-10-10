"""Markdown 导出器：生成便于二次编辑、粘贴到博客的纯文本。

文本处理、文件名推导、图片解析都复用 ``._shared``，与其它导出器保持一致。
"""

from __future__ import annotations

import datetime as _datetime

from ..client import Post
from ._shared import (
    ThreadData,
    attachment_image_url,
    plain_text,
    render_inline_content,
)
from .base import Exporter


class MarkdownBuilder(Exporter):
    """把串导出成 Markdown 文档。"""

    key = "markdown"
    display = "Markdown（.md）"
    suffix = ".md"
    save_message = "正在生成 Markdown 文件…"
    # 固定用 \n，避免在 Windows 上写出 CRLF。
    lf_newlines = True

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

        body = render_inline_content(self._client, post.content, image_mode="url")
        if body:
            lines.append("")
            lines.append(body)

        # 正文之外的附件图单独附在正文后面。
        attachment = attachment_image_url(self._client, post)
        if attachment:
            lines.append("")
            lines.append(f"![图片]({attachment})")

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
        return "\n".join(lines)


__all__ = ["MarkdownBuilder"]
