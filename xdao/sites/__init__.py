"""轻量站点适配：定位输入、分页抓取和统一模型转换。"""

from __future__ import annotations

import re
import urllib.parse

from .base import SiteAdapter, ThreadPage, ThreadRef
from .bog import BogSiteAdapter
from .nmbxd import NmbxdSiteAdapter

__all__ = ["SiteAdapter", "ThreadPage", "ThreadRef", "BogSiteAdapter", "NmbxdSiteAdapter", "get_site_adapter", "image_request_headers", "resolve_thread_ref"]

_ADAPTERS = {"xdao": NmbxdSiteAdapter(), "bog": BogSiteAdapter()}
_LEGACY_ID = re.compile(r"(?:^|/t/|/thread/|/id/)(\d{3,})(?![0-9A-Za-z])")
_BOG_PATH = re.compile(r"^/t/(\d+)(?:/(\d+))?/?$")
_XDAO_HOSTS = {"nmbxd1.com", "www.nmbxd1.com", "nmbxd.com", "www.nmbxd.com"}


def get_site_adapter(site: str = "xdao") -> SiteAdapter:
    try:
        return _ADAPTERS[site]
    except KeyError as exc:
        raise ValueError(f"不支持的站点：{site}") from exc


def image_request_headers(url: str) -> dict[str, str]:
    """Ask the owning adapter for any required image request headers."""
    for adapter in _ADAPTERS.values():
        headers = adapter.image_request_headers(url)
        if headers:
            return headers
    return {}


def resolve_thread_ref(value: str | int | None, site: str = "auto") -> ThreadRef | None:
    """纯数字继续默认 X 岛，B 岛只由明确站点或 bog.ac 地址识别。"""
    if site not in ("auto", "xdao", "bog") or isinstance(value, bool):
        return None
    text = str(value or "").strip()
    if text.isdigit():
        number = int(text)
        return ThreadRef("xdao" if site == "auto" else site, number) if number > 0 else None
    try:
        url = urllib.parse.urlsplit(text)
        host = (url.hostname or "").lower()
    except ValueError:
        return None
    if host == "bog.ac":
        match = _BOG_PATH.fullmatch(url.path)
        if not match or site == "xdao":
            return None
        number, page = int(match.group(1)), int(match.group(2) or 1)
        return ThreadRef("bog", number, page) if number > 0 and page > 0 else None
    if host and host not in _XDAO_HOSTS:
        return None
    if site == "bog":
        return None
    match = _LEGACY_ID.search(text)
    if match and int(match.group(1)) > 0:
        return ThreadRef("xdao", int(match.group(1)))
    return None
