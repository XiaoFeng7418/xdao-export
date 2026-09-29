"""Markdown 导出器：生成便于二次编辑、粘贴到博客的纯文本。

文本处理、文件名推导、图片解析都复用 ``._shared``，与其它导出器保持一致。
"""

from __future__ import annotations

import datetime as _datetime
import html as html_lib
from pathlib import Path

from ..client import Post
from ._shared import (
    ThreadData,
    derive_filename,
    iter_post_image_urls,
    plain_text,
    render_filename,
    render_inline_content,
    sanitize_filename,
)


class MarkdownBuilder:
    """把串导出成 Markdown 文档。"""

    def __init__(
        self,
        client,
        progress=None,
        filename_template: str | None = None,
    ) -> None:
        self._client = client
        self._progress = progress
        self.filename_template = filename_template

    def _notify(self, message: str) -> None:
        if self._progress:
            self._progress(message)

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
        attachment = self._attachment_url(post)
        if attachment:
            lines.append("")
            lines.append(f"![图片]({attachment})")

        lines.append("")
        lines.append("---")
        return "\n".join(lines)

    def build(
        self,
        thread: ThreadData,
        scope: str,
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

    def output_name(self, thread: ThreadData) -> str:
        """按模板（若设置）推导文件名主体。"""
        return render_filename(self.filename_template, thread, derive_filename(thread))

    def save(
        self,
        thread: ThreadData,
        scope: str,
        output_dir: Path,
        include_hashes: list[str] | None = None,
    ) -> Path:
        """写文件，返回路径（UTF-8、行尾 \\n）。"""
        self._notify("正在生成 Markdown 文件…")
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        path = output_dir / (sanitize_filename(self.output_name(thread)) + ".md")
        text = self.build(thread, scope, include_hashes)
        # 固定用 \n，避免在 Windows 上写出 CRLF。
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        return path


__all__ = ["MarkdownBuilder"]
