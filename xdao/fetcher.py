"""串的抓取流程：解析网址、翻页、汇总。"""

from __future__ import annotations

import re

from .builder import ThreadData
from .client import Post, XdaoClient, XdaoError


def parse_thread_id(value: str) -> int | None:
    """从网址或纯数字中解析串号。"""
    value = value.strip()
    match = re.search(r"(?:^|/t/|/thread/|/id/)(\d{3,})", value)
    if match:
        return int(match.group(1))
    if value.isdigit():
        return int(value)
    return None


class ThreadFetcher:
    def __init__(self, client: XdaoClient, progress=None) -> None:
        self._client = client
        self._progress = progress

    def _notify(self, message: str) -> None:
        if self._progress:
            self._progress(message)

    def fetch(self, url_or_id: str) -> ThreadData:
        thread_id = parse_thread_id(url_or_id)
        if thread_id is None:
            raise XdaoError(f"无法识别串号：{url_or_id}")

        self._notify("正在获取第 1 页…")
        first = self._client.fetch_thread_page(thread_id, 1)
        if isinstance(first, dict) and first.get("success") is False:
            raise XdaoError(str(first.get("error") or "该串不存在或无法访问"))
        if isinstance(first, str):
            raise XdaoError(str(first))

        parsed_first = self._client.parse_thread_page(first)
        posts: list[Post] = list(parsed_first["posts"])
        seen_ids: set[int] = {p.id for p in posts if p.id}

        try:
            reply_count = int(first.get("ReplyCount") or 0)
        except (ValueError, TypeError):
            reply_count = 0
        # 总帖数 = 主帖 1 + 回复数；抓满就停，避免接口在末页后仍返回空页。
        target_posts = reply_count + 1

        page = 2
        while True:
            if target_posts and len(posts) >= target_posts:
                break
            self._notify(f"正在抓取第 {page} 页…")
            payload = self._client.fetch_thread_page(thread_id, page)
            if isinstance(payload, dict) and payload.get("success") is False:
                break
            if isinstance(payload, str):
                break
            parsed = self._client.parse_thread_page(payload)
            replies = parsed["posts"][1:] if len(parsed["posts"]) > 1 else []
            # 部分接口从第 2 页起可能返回空列表或重复主帖。
            if not replies:
                break
            added = False
            for post in replies:
                if post.id in seen_ids:
                    continue
                seen_ids.add(post.id)
                posts.append(post)
                added = True
            if not added:
                break
            page += 1
            # 防御性上限，避免接口异常导致无限翻页。
            if page > 5000:
                break

        # 主帖可能在第 2 页之后再次出现，确保只保留一次。
        unique_posts: list[Post] = []
        for post in posts:
            if post.id not in {p.id for p in unique_posts}:
                unique_posts.append(post)

        return ThreadData(
            thread_id=thread_id,
            title=parsed_first["title"],
            po_hash=parsed_first["po_hash"],
            posts=unique_posts,
        )
