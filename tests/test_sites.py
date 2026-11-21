from __future__ import annotations

import pytest

from xdao.client import XdaoError
from xdao.sites import BogSiteAdapter, ThreadRef, get_site_adapter, resolve_thread_ref


def test_resolve_thread_ref_defaults_numeric_ids_to_xdao() -> None:
    ref = resolve_thread_ref("50000001")

    assert ref == ThreadRef(site="xdao", thread_id=50000001, page=1)


@pytest.mark.parametrize(
    ("url", "expected_page"),
    [
        ("https://bog.ac/t/12345678", 1),
        ("https://bog.ac/t/12345678/7", 7),
    ],
)
def test_resolve_thread_ref_recognizes_bog_thread_urls(url: str, expected_page: int) -> None:
    ref = resolve_thread_ref(url)

    assert ref == ThreadRef(site="bog", thread_id=12345678, page=expected_page)


def test_bog_adapter_builds_the_paged_api_path() -> None:
    adapter = get_site_adapter("bog")

    assert isinstance(adapter, BogSiteAdapter)
    assert adapter.build_thread_url(12345678, 4) == (
        "https://bog.ac/api/threads/12345678/4"
    )


@pytest.mark.parametrize(
    ("site", "key", "display"),
    [("xdao", "12345678", "X岛"), ("bog", "bog_12345678", "B岛")],
)
def test_site_adapter_exposes_cache_namespace_and_display_name(site, key, display) -> None:
    adapter = get_site_adapter(site)

    assert adapter.cache_key(12345678) == key
    assert adapter.display_name == display


def test_bog_adapter_unwraps_and_normalizes_a_page() -> None:
    adapter = BogSiteAdapter()
    payload = {
        "type": "OK",
        "code": 6001,
        "info": {
            "id": 12345678,
            "time": 1704153600000,
            "forum": 4,
            "name": "发串者",
            "emoji": "main-emoji",
            "cookie": "POHASH",
            "admin": 0,
            "title": "测试串",
            "content": "主帖正文",
            "lock": 0,
            "images": [{"url": "main-image", "ext": ".jpg"}],
            "reply_count": 21,
            "reply": [
                {
                    "id": 7001234,
                    "time": 1704153600000,
                    "name": "回复者",
                    "emoji": "reply-emoji",
                    "cookie": "REPLYHASH",
                    "admin": 0,
                    "title": "",
                    "content": (
                        "引用 >>Po.12345678 "
                        '<a href="https://bog.ac/t/12345678/7001235">'
                        "&gt;&gt;Po.7001235</a>"
                    ),
                    "images": [
                        {"url": "reply-image-a", "ext": ".png"},
                        {"url": "reply-image-b", "ext": ".webp"},
                    ],
                }
            ],
        },
    }

    thread = adapter.parse_page(payload, page=2)

    assert thread.thread_id == 12345678
    assert thread.title == "测试串"
    assert thread.po_hash == "POHASH"
    assert thread.reply_count == 21
    assert thread.pages == 2
    assert len(thread.posts) == 2

    main, reply = thread.posts
    assert main.now == "2024-01-02 08:00:00"
    assert main.emoji == "main-emoji"
    assert [(image.url, image.ext) for image in main.images] == [
        ("https://bog.ac/image/large/main-image.jpg", ".jpg")
    ]

    assert reply.id == 7001234
    assert reply.now == "2024-01-02 08:00:00"
    assert reply.emoji == "reply-emoji"
    assert [(image.url, image.ext) for image in reply.images] == [
        ("https://bog.ac/image/large/reply-image-a.png", ".png"),
        ("https://bog.ac/image/large/reply-image-b.webp", ".webp"),
    ]
    assert ">>No.12345678" in reply.content
    assert ">>No.7001235" in reply.content
    assert "<a " not in reply.content


def test_bog_normalizes_both_quote_forms_and_linked_quotes() -> None:
    adapter = BogSiteAdapter()
    page = adapter.parse_page(
        _bog_page(),
        page=1,
    )
    reply = adapter._post(
        {
            "id": 7001234,
            "content": (
                ">>Po.12345678 >>7001235 "
                '<a href="https://bog.ac/t/12345678/2">&gt;&gt;7001236</a>'
            ),
        },
        1,
        False,
    )

    assert page.posts[0].id == 12345678
    assert reply.content == ">>No.12345678 >>No.7001235 >>No.7001236"


def test_bog_adapter_rejects_an_error_envelope() -> None:
    adapter = BogSiteAdapter()

    with pytest.raises(XdaoError, match="error"):
        adapter.parse_page({"type": "error", "code": 4004, "info": "thread missing"})


@pytest.mark.parametrize(
    ("value", "site", "expected"),
    [
        ("12345678", "bog", ThreadRef("bog", 12345678)),
        ("https://www.nmbxd1.com/t/12345678", "auto", ThreadRef("xdao", 12345678)),
        ("https://nmbxd.com/t/12345678", "auto", ThreadRef("xdao", 12345678)),
        ("https://bog.ac/t/12345678/0", "auto", None),
        ("https://bog.ac/t/12345678oops", "auto", None),
        ("https://bog.ac.evil.example/t/12345678", "auto", None),
        ("https://bog.ac/t/12345678", "xdao", None),
        ("https://[invalid/t/12345678", "auto", None),
        (True, "auto", None),
        ("0", "auto", None),
    ],
)
def test_thread_input_keeps_the_site_and_rejects_invalid_targets(value, site, expected) -> None:
    assert resolve_thread_ref(value, site=site) == expected


def _bog_page(reply_count: int = 21, *, images=None) -> dict:
    return {
        "type": "OK", "code": 6001,
        "info": {
            "id": 12345678, "time": 1704153600000, "forum": 4,
            "name": "发串者", "emoji": "smile", "cookie": "POHASH", "admin": 0,
            "title": "测试串", "content": "主帖正文", "lock": 0,
            "images": images, "reply_count": reply_count, "reply": [],
        },
    }


@pytest.mark.parametrize(("count", "pages"), [(0, 1), (1, 1), (20, 1), (21, 2), (40, 2)])
def test_bog_page_estimate_uses_twenty_replies(count: int, pages: int) -> None:
    parsed = BogSiteAdapter().parse_page(_bog_page(count))
    assert parsed.pages == pages
    assert parsed.posts[0].images == []


def test_bog_fetch_page_uses_transport_and_serializes_the_common_model() -> None:
    import json
    from xdao.client import XdaoClient

    client = XdaoClient(throttle=0)
    calls = []
    payload = _bog_page(images=[{"url": "sample-a", "ext": ".png"}])

    def request(url, **kwargs):
        calls.append(url)
        return json.dumps(payload).encode("utf-8")

    client._request = request
    adapter = BogSiteAdapter()
    normalized = adapter.fetch_page(client, 12345678, 2)
    restored = adapter.parse_thread_page(json.loads(json.dumps(normalized)), page=2)

    assert calls == ["https://bog.ac/api/threads/12345678/2"]
    assert normalized["_normalized_page"] == 1
    assert normalized["ReplyCount"] == 21
    assert normalized["PageCount"] == 2
    assert "info" not in normalized
    assert restored["title"] == "测试串"
    assert restored["posts"][0].images[0].url == "https://bog.ac/image/large/sample-a.png"
    assert restored["posts"][0].page == 2


def test_bog_fetch_page_reports_non_json_and_invalid_envelopes() -> None:
    from xdao.client import XdaoClient

    client = XdaoClient(throttle=0)
    client._request = lambda *args, **kwargs: b"<html>not json</html>"
    with pytest.raises(XdaoError, match="JSON"):
        BogSiteAdapter().fetch_page(client, 12345678, 1)
    with pytest.raises(XdaoError, match="info"):
        BogSiteAdapter().parse_page({"type": "OK", "code": 6001, "info": None})


def test_bog_fetch_reuses_http_retry_after_and_throttling(monkeypatch) -> None:
    import io
    import json
    import urllib.error
    from xdao.client import XdaoClient

    class Response:
        headers = {}
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return None
        def read(self, *args):
            return json.dumps(_bog_page()).encode("utf-8")

    client = XdaoClient(throttle=0.2, retries=1)
    waits = []
    requests = []
    throttles = []

    class Opener:
        def open(self, req, timeout):
            requests.append(req.full_url)
            if len(requests) == 1:
                raise urllib.error.HTTPError(req.full_url, 429, "slow down", {"Retry-After": "3"}, io.BytesIO())
            return Response()

    client._opener = Opener()
    monkeypatch.setattr(client, "_throttle", lambda: throttles.append(True))
    monkeypatch.setattr("xdao.client.time.sleep", waits.append)

    normalized = BogSiteAdapter().fetch_page(client, 12345678, 1)

    assert normalized["id"] == 12345678
    assert requests == ["https://bog.ac/api/threads/12345678/1"] * 2
    assert waits == [3.0]
    assert throttles == [True]


def test_nmbxd_adapter_keeps_xdao_parsing_and_cache_roundtrip() -> None:
    import json
    from xdao.client import XdaoClient

    client = XdaoClient(throttle=0)
    raw = {
        "id": 12345678, "title": "X测试串", "user_hash": "XPO", "name": "无名氏",
        "content": "正文", "now": "2026-01-01", "img": "image-a", "ext": ".jpg",
        "admin": 0, "ReplyCount": 19, "Replies": [],
    }
    client.fetch_thread_page = lambda *args, **kwargs: raw
    adapter = get_site_adapter("xdao")
    normalized = adapter.fetch_page(client, 12345678, 1)
    parsed = adapter.parse_thread_page(json.loads(json.dumps(normalized)), page=1)

    assert parsed["po_hash"] == "XPO"
    assert parsed["posts"][0].img == "image-a"
    assert parsed["posts"][0].ext == ".jpg"
    assert parsed["posts"][0].images == []
    assert adapter.reply_count(normalized) == 19
    assert adapter.page_count(normalized) == 2


@pytest.mark.parametrize(
    ("site", "expected"),
    [("bog", "https://bog.ac/t/12345678"), ("xdao", "https://www.nmbxd1.com/t/12345678")],
)
def test_adapter_exposes_a_public_thread_link(site: str, expected: str) -> None:
    assert get_site_adapter(site).thread_url(12345678) == expected
