"""TXT 导出器：纯文本输出，图片以链接形式保留。"""

from __future__ import annotations

from ._shared import ThreadData, derive_filename, iter_post_image_urls, plain_text
from .base import Exporter

_SEPARATOR = "=" * 48
_POST_SEPARATOR = "-" * 48


class TxtBuilder(Exporter):
    """生成便于阅读和检索的纯文本。"""

    key = "txt"
    #: 合并导出：多个串共用一个 .txt，中间用更粗的分隔线。
    supports_merge = True
    display = "TXT（纯文本）"
    suffix = ".txt"
    # 进度提示放在 build 里（「正在生成 TXT 文件…」），保存阶段不再重复一句。
    save_message = ""

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

    def build_many(
        self,
        threads: list[ThreadData],
        scope: str = "all",
        include_hashes: list[str] | None = None,
    ) -> str:
        """多个串拼一份：每串仍用 :meth:`build` 生成，之间加一条粗分隔线。"""
        self._notify(f"正在生成合并 TXT（{len(threads)} 个串）…")
        blocks = [self.build(thread, scope, include_hashes) for thread in threads]
        return ("\n\n" + "=" * 72 + "\n\n").join(blocks)
