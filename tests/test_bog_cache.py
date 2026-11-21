"""Cross-site cache behavior, with offline HTTP fixtures."""
from __future__ import annotations

import json

import pytest

from xdao.cache import CachedThreadFetcher, ThreadCache, post_fingerprint
from xdao.client import XdaoClient
from tests.test_cache import FakeApi, make_page, make_reply


def bog_page(page=1, count=21):
    return {"type": "OK", "code": 6001, "info": {
        "id": 12345678, "time": 1704153600000, "forum": 1,
        "cookie": "PO", "name": "无名氏", "emoji": "🌙", "admin": None,
        "title": "测试串", "content": "主帖", "images": None,
        "reply_count": count,
        "reply": [{"id": 7001234 + page, "time": 1704153600000,
                   "cookie": "USER", "admin": None, "content": "回复",
                   "images": [{"url": "a", "ext": ".png"}, {"url": "b", "ext": ".png"}]}],
    }}


def bog_client(monkeypatch):
    client = XdaoClient(throttle=0, retries=0)
    calls = []
    def request(url, **kwargs):
        calls.append(url)
        page = int(url.rsplit("/", 1)[1])
        return json.dumps(bog_page(page)).encode()
    monkeypatch.setattr(client, "_request", request)
    return client, calls


def test_disk_key_is_separate_from_numeric_id(artifacts_dir):
    x = ThreadCache.load(artifacts_dir, 12345678)
    b = ThreadCache.load(artifacts_dir, 12345678, site="bog")
    assert b.thread_id == x.thread_id == 12345678
    assert x.path.name == "12345678.json"
    assert x.pages_dir.name == "12345678"
    assert b.path.name == "bog_12345678.json"
    assert b.pages_dir.name == "bog_12345678"
    x.reply_count, b.reply_count = 2, 21
    x.save()
    b.save()
    assert ThreadCache.load(artifacts_dir, 12345678).reply_count == 2
    assert ThreadCache.load(artifacts_dir, 12345678, site="bog").reply_count == 21


def test_bog_pages_are_normalized_cached_and_reused(artifacts_dir, monkeypatch):
    client, calls = bog_client(monkeypatch)
    fetcher = CachedThreadFetcher(client, cache_dir=artifacts_dir, retry_delay=0)
    result = fetcher.fetch("https://bog.ac/t/12345678/2")
    assert calls == ["https://bog.ac/api/threads/12345678/1", "https://bog.ac/api/threads/12345678/2"]
    assert result.meta.thread_id == 12345678 and result.meta.site == "bog"
    assert result.meta.pages == 2 and len(result.posts) == 3
    assert result.posts[1].now == "2024-01-02 08:00:00"
    assert len(result.posts[1].images) == 2
    disk = json.loads((artifacts_dir / "pages/bog_12345678/2.json").read_text(encoding="utf-8"))
    assert disk["_normalized_page"] == 1
    assert "info" not in disk and "reply" not in disk
    calls.clear()
    again = fetcher.fetch("12345678", site="bog")
    assert calls == ["https://bog.ac/api/threads/12345678/1"]
    assert not again.changed and again.edited_posts == 0
    assert len(again.posts) == 3


def test_same_id_sites_never_overwrite_pages(artifacts_dir, monkeypatch):
    api = FakeApi({1: make_page(12345678, 1, [make_reply(101)], reply_count=1, page_count=1)})
    x = CachedThreadFetcher(api, cache_dir=artifacts_dir).fetch("12345678")
    client, _ = bog_client(monkeypatch)
    CachedThreadFetcher(client, cache_dir=artifacts_dir).fetch("https://bog.ac/t/12345678")
    again = CachedThreadFetcher(api, cache_dir=artifacts_dir).fetch("12345678")
    assert [p.id for p in again.posts] == [p.id for p in x.posts]
    assert (artifacts_dir / "threads/12345678.json").is_file()
    assert (artifacts_dir / "threads/bog_12345678.json").is_file()


def test_legacy_x_raw_cache_still_reuses_pages(artifacts_dir):
    pages = {n: make_page(12345678, n, [make_reply(100+n)], reply_count=2, page_count=2) for n in (1, 2)}
    cache = ThreadCache.load(artifacts_dir, 12345678)
    cache.pages, cache.reply_count = 2, 2
    for n, payload in pages.items():
        cache.store_page(n, payload)
    cache.save()
    api = FakeApi(pages)
    result = CachedThreadFetcher(api, cache_dir=artifacts_dir).fetch("12345678")
    assert api.requests == [(12345678, 1)]
    assert len(result.posts) == 3


def test_fingerprint_covers_second_image_and_emoji(artifacts_dir, monkeypatch):
    from dataclasses import replace
    from xdao.client import PostImage
    client, _ = bog_client(monkeypatch)
    p = CachedThreadFetcher(client, cache_dir=artifacts_dir).fetch("https://bog.ac/t/12345678").posts[1]
    changed_image = replace(p, images=[p.images[0], PostImage("https://bog.ac/image/large/c.png", ".png")])
    changed_emoji = replace(p, emoji="⭐")
    assert post_fingerprint(p) != post_fingerprint(changed_image)
    assert post_fingerprint(p) != post_fingerprint(changed_emoji)


def test_bog_error_never_creates_a_valid_cache(artifacts_dir, monkeypatch):
    from xdao.client import XdaoError
    client = XdaoClient(throttle=0)
    monkeypatch.setattr(client, "_request", lambda *a, **k: b'{"type":"error","code":4004,"info":"missing"}')
    with pytest.raises(XdaoError, match="missing"):
        CachedThreadFetcher(client, cache_dir=artifacts_dir).fetch("https://bog.ac/t/12345678")
    assert not (artifacts_dir / "threads/bog_12345678.json").exists()
