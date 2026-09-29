"""TXT 导出器：纯文本输出，图片以链接形式保留。"""

from __future__ import annotations

from pathlib import Path

from ..client import XdaoClient
from ._shared import (
    ThreadData,
    derive_filename,
    iter_post_image_urls,
    plain_text,
    render_filename,
    sanitize_filename,
)

_SEPARATOR = "=" * 48
_POST_SEPARATOR = "-" * 48


class TxtBuilder:
    """生成便于阅读和检索的纯文本。"""

    def __init__(self, client: XdaoClient, progress=None, filename_template: str | None = None) -> None:
        self._client = client
        self._progress = progress
        self.filename_template = filename_template

    def _notify(self, message: str) -> None:
        if self._progress:
            self._progress(message)

    def build(
        self,
        thread: ThreadData,
        scope: str = "all",
        include_hashes: list[str] | None = None,
    ) -> str:
        self._notify("正在生成 TXT 文件…")
        posts = thread.filter_posts(scope, include_hashes)

        if include_hashes:
            scope_label = "指定饼干：" + "、".join(include_hashes)
        elif scope == "po":
            scope_label = "仅 PO 发言"
        else:
            scope_label = "全部发言"

        lines: list[str] = [
            f"串号 No.{thread.thread_id}",
            f"标题：{derive_filename(thread)}",
            f"PO 饼干：{thread.po_hash or '未知'}",
            f"导出范围：{scope_label}",
            f"楼层数：{len(posts)}",
            "",
            _SEPARATOR,
            "",
        ]

        for index, post in enumerate(posts, start=1):
            is_po = thread.is_po_post(post)
            parts = [f"#{index}", "[PO]" if is_po else "    ", f"饼干 {post.user_hash}"]
            if post.admin:
                parts.append("红名")
            if post.sage:
                parts.append("SAGE")
            if post.name and post.name not in ("无名氏", ""):
                parts.append(f"作者 {plain_text(post.name)}")
            if post.title and post.title not in ("无标题", ""):
                parts.append(f"标题 {plain_text(post.title)}")
            parts.append(str(post.now))
            parts.append(f"No.{post.id}")

            lines.append("  |  ".join(parts))
            content = plain_text(post.content)
            if content:
                lines.append(content)
            for url in iter_post_image_urls(self._client, post):
                lines.append(f"[图片] {url}")
            lines.append(_POST_SEPARATOR)
            lines.append("")

        return "\n".join(lines)

    def output_name(self, thread: ThreadData) -> str:
        return render_filename(self.filename_template, thread, derive_filename(thread))

    def save(
        self,
        thread: ThreadData,
        scope: str = "all",
        output_dir: Path | str = ".",
        include_hashes: list[str] | None = None,
    ) -> Path:
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        path = output_dir / (sanitize_filename(self.output_name(thread)) + ".txt")
        path.write_text(self.build(thread, scope, include_hashes), encoding="utf-8")
        return path
