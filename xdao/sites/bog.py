"""B 岛公开 API 适配，不涉及登录、验证码或饼干管理。"""

from __future__ import annotations

import datetime
import html
import json
import re
import urllib.parse

from ..client import Post, PostImage, XdaoError
from .base import NormalizedAdapter, ThreadPage, normalized_page, positive_int

_REFERENCE_LINK = re.compile(r"<a\b[^>]*>(.*?)</a\s*>", re.IGNORECASE | re.DOTALL)
_REFERENCE = re.compile(r"(?:>>|&gt;&gt;)(?:Po\.)?(\d+)")
_SHANGHAI = datetime.timezone(datetime.timedelta(hours=8))


def _content(value) -> str:
    content = str(value or "")
    # 引用链接里的站点跳转地址不能盖住本地锚点；只摘引用本身的 a 标签。
    content = _REFERENCE_LINK.sub(
        lambda match: match.group(1) if _REFERENCE.search(html.unescape(match.group(1))) else match.group(0),
        content,
    )
    return _REFERENCE.sub(lambda match: f">>No.{match.group(1)}", content)


def _timestamp(value) -> str:
    try:
        return datetime.datetime.fromtimestamp(float(value) / 1000.0, _SHANGHAI).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError, OSError, OverflowError):
        return ""


class BogSiteAdapter(NormalizedAdapter):
    site = "bog"
    name = site
    display_name = "B岛"
    cache_prefix = "bog_"
    requires_login = False
    site_url = "https://bog.ac"

    @staticmethod
    def build_thread_url(thread_id: int, page: int = 1) -> str:
        return f"https://bog.ac/api/threads/{thread_id}/{page}"

    @staticmethod
    def image_url(img: str, ext: str, thumb: bool = False) -> str:
        return f"https://bog.ac/image/large/{img}{ext}"

    @staticmethod
    def image_request_headers(url: str) -> dict[str, str]:
        parsed = urllib.parse.urlsplit(url)
        if (
            parsed.scheme == "https"
            and parsed.hostname == "bog.ac"
            and parsed.path.startswith("/image/large/")
        ):
            return {"Referer": "https://bog.ac/"}
        return {}

    def thread_url(self, thread_id: int) -> str:
        return f"{self.site_url}/t/{thread_id}"

    def cache_key(self, thread_id: int) -> str:
        return f"{self.cache_prefix}{int(thread_id)}"

    def fetch_page(self, client, thread_id: int, page: int = 1) -> dict:
        raw = client._request(self.build_thread_url(thread_id, page))
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise XdaoError("B 岛接口返回了非 JSON 内容") from exc
        if not isinstance(payload, dict) or str(payload.get("type") or "").lower() != "ok":
            return payload
        return self.normalize_page(client, payload, page)

    def raise_for_error(self, payload, thread_id: int) -> None:
        if isinstance(payload, dict) and payload.get("_normalized_page") == 1:
            return
        if isinstance(payload, dict) and str(payload.get("type") or "").lower() != "ok":
            detail = payload.get("info") or payload.get("type") or "未知错误"
            raise XdaoError(f"B 岛接口 error（{payload.get('code', '')}）：{detail}")
        super().raise_for_error(payload, thread_id)

    def normalize_page(self, client, payload: dict, page: int = 1) -> dict:
        if isinstance(payload, dict) and payload.get("_normalized_page") == 1:
            return payload
        return self.parse_page(payload, page).to_payload()

    def parse_page(self, payload: dict, page: int = 1) -> ThreadPage:
        if isinstance(payload, dict) and payload.get("_normalized_page") == 1:
            return normalized_page(payload, page)
        if not isinstance(payload, dict):
            raise XdaoError("B 岛接口返回了非预期内容")
        kind = str(payload.get("type") or "")
        if kind.lower() != "ok":
            raise XdaoError(f"B 岛接口 error（{payload.get('code', '')}）：{payload.get('info') or kind}")
        info = payload.get("info")
        if not isinstance(info, dict) or not positive_int(info.get("id")):
            raise XdaoError("B 岛接口 info 数据无效")
        replies = info.get("reply") or []
        if not isinstance(replies, list):
            raise XdaoError("B 岛接口回复数据无效")
        po_hash = str(info.get("cookie") or "")
        posts = [self._post(info, page, True)]
        posts.extend(self._post(item, page, False) for item in replies if isinstance(item, dict))
        count = positive_int(info.get("reply_count"))
        return ThreadPage(
            thread_id=positive_int(info.get("id")),
            title=posts[0].title,
            po_hash=po_hash,
            reply_count=count,
            pages=max(1, (count + 19) // 20),
            posts=posts,
            has_more=bool(replies),
        )

    def _post(self, item: dict, page: int, main: bool) -> Post:
        images = [
            PostImage(self.image_url(str(image["url"]), str(image.get("ext") or "")), str(image.get("ext") or ""))
            for image in item.get("images") or []
            if isinstance(image, dict) and image.get("url")
        ]
        return Post(
            id=positive_int(item.get("id")),
            user_hash=str(item.get("cookie") or ""),
            name=str(item.get("name") or "无名氏"),
            title=str(item.get("title") or ("无标题" if main else "")),
            content=_content(item.get("content")),
            now=_timestamp(item.get("time")),
            img=images[0].url if images else "",
            ext=images[0].ext if images else "",
            admin=positive_int(item.get("admin")),
            is_po=main,
            page=page,
            images=images,
            emoji=str(item.get("emoji") or ""),
        )
