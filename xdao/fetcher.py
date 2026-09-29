"""串的抓取流程（兼容层）。

真正的实现在 :mod:`xdao.cache`：

- :class:`~xdao.cache.CachedThreadFetcher` 带页面缓存、断点续传与增量更新；
- ``ThreadFetcher`` 保留为不带缓存的轻量实现，供只需要抓一次的场景使用。

``parse_thread_id`` 统一由 :mod:`xdao.cache` 提供，避免同一套正则出现多份。
"""

from __future__ import annotations

from .cache import (
    CachedThread,
    CachedThreadFetcher,
    ThreadMeta,
    parse_thread_id,
)
from .client import Post, XdaoClient, XdaoError
from .exporters import ThreadData

__all__ = [
    "CachedThread",
    "CachedThreadFetcher",
    "ThreadFetcher",
    "ThreadMeta",
    "parse_thread_id",
]


def to_thread_data(result: CachedThread | ThreadData) -> ThreadData:
    """把抓取结果统一成导出器需要的 :class:`ThreadData`。"""
    if isinstance(result, ThreadData):
        return result
    meta = result.meta
    return ThreadData(
        thread_id=meta.thread_id,
        title=meta.title,
        po_hash=meta.po_hash,
        posts=list(result.posts),
    )


class ThreadFetcher:
    """不带缓存的抓取器：每次调用都完整翻页。

    适合一次性导出；需要断点续传或用缓存加速时请改用
    :class:`~xdao.cache.CachedThreadFetcher`。
    """

    def __init__(self, client: XdaoClient, progress=None, cache_dir=None) -> None:
        self._client = client
        self._progress = progress
        self._cache_dir = cache_dir

    def _notify(self, message: str) -> None:
        if self._progress:
            self._progress(message)

    def fetch(self, url_or_id: str) -> ThreadData:
        """完整抓取一个串，返回可交给导出器的 :class:`ThreadData`。"""
        thread_id = parse_thread_id(url_or_id)
        if thread_id is None:
            raise XdaoError(f"无法识别串号：{url_or_id}")

        self._notify("正在获取第 1 页…")
        first = self._client.fetch_thread_page(thread_id, 1)
        if isinstance(first, str):
            raise XdaoError(first)
        if isinstance(first, dict) and first.get("success") is False:
            raise XdaoError(str(first.get("error") or "该串不存在或无法访问"))
        if not isinstance(first, dict):
            raise XdaoError(f"接口返回了非预期内容：{type(first).__name__}")

        parsed_first = self._client.parse_thread_page(first, 1)
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
            if isinstance(payload, str):
                break
            if isinstance(payload, dict) and payload.get("success") is False:
                break
            if not isinstance(payload, dict):
                break
            parsed = self._client.parse_thread_page(payload, page)
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

        return ThreadData(
            thread_id=thread_id,
            title=parsed_first["title"],
            po_hash=parsed_first["po_hash"] or "",
            posts=posts,
        )
