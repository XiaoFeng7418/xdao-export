"""X 岛适配器：保留既有客户端接口和原始缓存读取能力。"""

from __future__ import annotations

from ..client import XdaoClient, XdaoError, reply_count
from .base import NormalizedAdapter, ThreadPage


class NmbxdSiteAdapter(NormalizedAdapter):
    site = "xdao"
    name = site
    display_name = "X岛"
    cache_prefix = ""
    requires_login = True
    site_url = "https://www.nmbxd1.com"

    def thread_url(self, thread_id: int) -> str:
        return f"{self.site_url}/t/{thread_id}"

    def cache_key(self, thread_id: int) -> str:
        return f"{self.cache_prefix}{int(thread_id)}"

    def fetch_page(self, client, thread_id: int, page: int = 1) -> dict:
        payload = client.fetch_thread_page(thread_id, page)
        if not isinstance(payload, dict) or payload.get("success") is False:
            return payload
        return self.normalize_page(client, payload, page)

    @staticmethod
    def normalize_page(client, payload: dict, page: int = 1) -> dict:
        if isinstance(payload, str):
            raise XdaoError(payload)
        if not isinstance(payload, dict):
            raise XdaoError(f"接口返回了非预期内容：{type(payload).__name__}")
        if payload.get("_normalized_page") == 1:
            return payload
        if payload.get("success") is False:
            raise XdaoError(str(payload.get("error") or "无法访问串"))
        parsed = client.parse_thread_page(payload, page)
        return ThreadPage(
            thread_id=int(parsed.get("id") or payload.get("id") or 0),
            title=parsed["title"],
            po_hash=parsed.get("po_hash") or "",
            reply_count=reply_count(payload),
            pages=XdaoClient._extract_page_count(payload),
            posts=parsed["posts"],
            has_more=bool(parsed.get("has_more")),
        ).to_payload()

    @staticmethod
    def image_url(client, img: str, ext: str, thumb: bool = False) -> str:
        return client.image_url(img, ext, thumb)
