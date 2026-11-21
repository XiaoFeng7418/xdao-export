"""站点无关的串定位与分页数据边界。"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Protocol

from ..client import Post, PostImage, XdaoError


@dataclass(frozen=True)
class ThreadRef:
    """已确认站点的串定位信息。"""

    site: str
    thread_id: int
    page: int = 1

    @property
    def cache_key(self) -> str:
        return str(self.thread_id) if self.site == "xdao" else f"{self.site}_{self.thread_id}"


@dataclass
class ThreadPage:
    """适配器解析完成的一页；不暴露站点响应字段。"""

    thread_id: int
    title: str
    po_hash: str
    reply_count: int
    pages: int
    posts: list[Post]
    has_more: bool = False

    def to_payload(self) -> dict:
        """可写入页缓存的统一结构；帖子与图片都使用通用模型字段。"""
        return {
            "_normalized_page": 1,
            "id": self.thread_id,
            "title": self.title,
            "po_hash": self.po_hash,
            "ReplyCount": self.reply_count,
            "PageCount": self.pages,
            "posts": [asdict(post) for post in self.posts],
            "has_more": self.has_more,
        }


def positive_int(value, default: int = 0) -> int:
    try:
        return max(default, int(value or default))
    except (TypeError, ValueError):
        return default


def normalized_page(payload: dict, page: int = 1) -> ThreadPage:
    """将统一缓存记录还原成模型，不依赖网络客户端。"""
    if not isinstance(payload, dict) or payload.get("_normalized_page") != 1:
        raise XdaoError("缓存页不是归一化分页数据")
    posts = []
    for item in payload.get("posts") or []:
        if isinstance(item, Post):
            posts.append(item)
            continue
        if not isinstance(item, dict):
            raise XdaoError("缓存页的发言数据无效")
        images = [
            image if isinstance(image, PostImage) else PostImage(
                url=str(image.get("url") or ""), ext=str(image.get("ext") or "")
            )
            for image in item.get("images") or []
            if isinstance(image, (dict, PostImage))
        ]
        posts.append(Post(
            id=positive_int(item.get("id")),
            user_hash=str(item.get("user_hash") or ""),
            name=str(item.get("name") or "无名氏"),
            title=str(item.get("title") or ""),
            content=str(item.get("content") or ""),
            now=str(item.get("now") or ""),
            img=str(item.get("img") or ""),
            ext=str(item.get("ext") or ""),
            admin=positive_int(item.get("admin")),
            sage=positive_int(item.get("sage")),
            is_po=bool(item.get("is_po")),
            page=page,
            images=images,
            emoji=str(item.get("emoji") or ""),
        ))
    return ThreadPage(
        thread_id=positive_int(payload.get("id")),
        title=str(payload.get("title") or ""),
        po_hash=str(payload.get("po_hash") or ""),
        reply_count=positive_int(payload.get("ReplyCount")),
        pages=positive_int(payload.get("PageCount"), 1),
        posts=posts,
        has_more=bool(payload.get("has_more")),
    )


class SiteAdapter(Protocol):
    """抓取层需要的最小站点接口；传输仍由现有客户端负责。"""

    site: str
    name: str
    display_name: str
    cache_prefix: str
    requires_login: bool

    def cache_key(self, thread_id: int) -> str: ...

    def thread_url(self, thread_id: int) -> str: ...
    def image_request_headers(self, url: str) -> dict[str, str]: ...
    def fetch_page(self, client, thread_id: int, page: int = 1) -> dict: ...
    def raise_for_error(self, payload, thread_id: int) -> None: ...
    def normalize_page(self, client, payload: dict, page: int = 1) -> dict: ...
    def parse_thread_page(self, payload: dict, page: int = 1) -> dict: ...
    def page_count(self, payload: dict) -> int: ...
    def reply_count(self, payload: dict) -> int: ...


class NormalizedAdapter:
    """两站共享的统一分页读取，避免缓存层再读站点原始字段。"""

    @staticmethod
    def image_request_headers(url: str) -> dict[str, str]:
        return {}

    @staticmethod
    def parse_thread_page(payload: dict, page: int = 1) -> dict:
        parsed = normalized_page(payload, page)
        return {
            "id": parsed.thread_id,
            "title": parsed.title,
            "po_hash": parsed.po_hash,
            "ReplyCount": parsed.reply_count,
            "posts": parsed.posts,
            "has_more": parsed.has_more,
        }

    @staticmethod
    def page_count(payload: dict) -> int:
        return positive_int(payload.get("PageCount"), 1)

    @staticmethod
    def reply_count(payload: dict) -> int:
        return positive_int(payload.get("ReplyCount"))

    @staticmethod
    def raise_for_error(payload, thread_id: int) -> None:
        if isinstance(payload, str):
            raise XdaoError(payload)
        if not isinstance(payload, dict):
            raise XdaoError(f"接口返回了非预期内容：{type(payload).__name__}")
        if payload.get("success") is False:
            raise XdaoError(str(payload.get("error") or f"无法访问串 {thread_id}"))
