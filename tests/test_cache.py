"""缓存与断点续传的测试（全部离线，用假接口顶替网络）。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from xdao.cache import (
    CachedThreadFetcher,
    ThreadCache,
    cache_dir_candidates,
    default_cache_dir,
    parse_reply_count,
    parse_thread_id,
    post_fingerprint,
    resolve_cache_dir,
)
from xdao.client import Post, XdaoError

PO_HASH = "POCOOKIE"


def make_page(
    thread_id: int,
    page: int,
    replies: list[dict],
    *,
    reply_count: int,
    page_count: int,
    title: str = "测试串标题",
) -> dict:
    """构造一页接口返回，形状与真实接口一致。

    注意：真实接口在每一页里都会带上同一个主帖，所以这里的主帖内容
    刻意与页号无关 —— 否则每轮都会被误判成"主帖被编辑"。
    """
    return {
        "id": thread_id,
        "fid": 4,
        "ReplyCount": reply_count,
        "PageCount": page_count,
        "title": title,
        "user_hash": PO_HASH,
        "name": "无名氏",
        "now": "2026-09-23 12:00",
        "content": "主帖正文",
        "img": "",
        "ext": "",
        "admin": 0,
        "Replies": replies,
    }


def make_reply(post_id: int, cookie: str = "userA", content: str | None = None) -> dict:
    return {
        "id": post_id,
        "user_hash": cookie,
        "name": "无名氏",
        "title": "",
        "content": content if content is not None else f"回复 {post_id}",
        "now": "2026-09-23 12:01",
        "img": "",
        "ext": "",
        "admin": 0,
    }


class FakeApi:
    """假接口：按 (串号, 页号) 返回预设数据，并记录请求次数。"""

    def __init__(self, pages: dict[int, dict]) -> None:
        self.pages = pages
        self.requests: list[tuple[int, int]] = []
        self.last_page_count = 1
        self.last_page_index = 1
        self.cdn_path = "https://image.nmb.best/"
        # 复用真实客户端的解析逻辑，避免测试自己实现一套解析。
        from xdao.client import XdaoClient

        self._real = XdaoClient()
        self._real.cdn_path = self.cdn_path

    def fetch_thread_page(self, thread_id: int, page: int = 1) -> dict:
        self.requests.append((thread_id, page))
        payload = self.pages.get(page)
        if payload is None:
            # 超出范围的页返回空回复列表，模拟接口行为。
            payload = make_page(thread_id, page, [], reply_count=0, page_count=1)
        self.last_page_count = int(payload.get("PageCount") or 1)
        self.last_page_index = page
        return payload

    def parse_thread_page(self, payload: dict, page: int | None = None) -> dict:
        return self._real.parse_thread_page(payload, page or self.last_page_index)

    def image_url(self, img: str, ext: str, thumb: bool = False) -> str:
        return f"{self.cdn_path}image/{img}{ext}"


def build_three_page_api() -> FakeApi:
    """3 页、每页 2 条回复（主帖 + 2 回复），共 7 楼。"""
    return FakeApi(
        {
            1: make_page(7001, 1, [make_reply(101), make_reply(102)], reply_count=6, page_count=3),
            2: make_page(7001, 2, [make_reply(103), make_reply(104)], reply_count=6, page_count=3),
            3: make_page(7001, 3, [make_reply(105), make_reply(106)], reply_count=6, page_count=3),
        }
    )


# ---------- 串号解析 ----------


@pytest.mark.parametrize(
    "value,expected",
    [
        ("7001", 7001),
        (7001, 7001),
        ("https://www.nmbxd1.com/t/7001", 7001),
        ("https://www.nmbxd1.com/Forum/po/id/7001/page/4.html", 7001),
        ("https://www.nmbxd1.com/m/t/7001?page=64", 7001),
        ("  https://www.nmbxd1.com/thread/7001  ", 7001),
        ("不是网址", None),
        ("", None),
        (None, None),
        (True, None),  # bool 不能当串号
        (0, None),
    ],
)
def test_parse_thread_id(value, expected):
    assert parse_thread_id(value) == expected


def test_parse_reply_count_tolerates_bad_values():
    assert parse_reply_count({"ReplyCount": 12}) == 12
    assert parse_reply_count({"ReplyCount": "12"}) == 12
    assert parse_reply_count({"ReplyCount": None}) == 0
    assert parse_reply_count({"ReplyCount": "abc"}) == 0
    assert parse_reply_count({}) == 0


# ---------- 缓存状态文件 ----------


def test_cache_round_trip(artifacts_dir):
    cache = ThreadCache.load(artifacts_dir, 7001)
    assert cache.pages == 0
    cache.reply_count = 6
    cache.pages = 3
    cache.last_fetch_at = 1700000000.0
    cache.fingerprints["101"] = "abc"
    cache.save()

    again = ThreadCache.load(artifacts_dir, 7001)
    assert again.reply_count == 6
    assert again.pages == 3
    assert again.fingerprints["101"] == "abc"
    assert again.last_fetch_at == 1700000000.0
    assert "3 页" in again.describe()


def test_cache_ignores_corrupt_state(artifacts_dir):
    path = artifacts_dir / "threads" / "7001.json"
    path.parent.mkdir(parents=True)
    path.write_text("{ 这不是合法 JSON", encoding="utf-8")
    cache = ThreadCache.load(artifacts_dir, 7001)
    assert cache.pages == 0  # 损坏时静默回退到空缓存


def test_cache_ignores_old_version(artifacts_dir):
    path = artifacts_dir / "threads" / "7001.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"version": 0, "pages": 9}), encoding="utf-8")
    assert ThreadCache.load(artifacts_dir, 7001).pages == 0


def test_page_store_and_load(artifacts_dir):
    cache = ThreadCache.load(artifacts_dir, 7001)
    payload = make_page(7001, 1, [], reply_count=0, page_count=1)
    cache.store_page(1, payload)
    assert cache.load_page(1) == payload
    assert cache.load_page(2) is None
    cache.clear()
    assert cache.load_page(1) is None


def test_default_cache_dir_uses_appdata(artifacts_dir, monkeypatch):
    monkeypatch.setenv("APPDATA", str(artifacts_dir))
    assert default_cache_dir() == artifacts_dir / "xdao-export" / ".cache"
    assert default_cache_dir(artifacts_dir / "out") == artifacts_dir / "out" / ".cache"


def test_post_fingerprint_changes_with_content():
    base = Post(
        id=1, user_hash="u", name="n", title="t", content="正文", now="now",
        img="", ext="", admin=0,
    )
    same = Post(
        id=1, user_hash="u", name="n", title="t", content="正文", now="now",
        img="", ext="", admin=0,
    )
    edited = Post(
        id=1, user_hash="u", name="n", title="t", content="改过的正文", now="now",
        img="", ext="", admin=0,
    )
    assert post_fingerprint(base) == post_fingerprint(same)
    assert post_fingerprint(base) != post_fingerprint(edited)


# ---------- 抓取：首次、复用、增量 ----------


def test_first_fetch_pages_through_and_deduplicates(artifacts_dir):
    api = build_three_page_api()
    fetcher = CachedThreadFetcher(api, cache_dir=artifacts_dir)

    result = fetcher.fetch(7001)

    # 主帖在第 1 页返回，后面每页只有回复；合并后共 7 楼且不重复。
    assert len(result.posts) == 7
    assert len({p.id for p in result.posts}) == 7
    assert result.meta.reply_count == 6
    assert result.meta.pages == 3
    assert result.meta.po_hash == PO_HASH
    assert result.pages_fetched == 2  # 第 1 页由首请求拿到，另抓 2 页
    assert result.from_cache is False
    assert [p.page for p in result.posts if not p.is_po] == [1, 1, 2, 2, 3, 3]
    assert api.requests == [(7001, 1), (7001, 2), (7001, 3)]


def test_second_fetch_reuses_cache_with_one_request(artifacts_dir):
    api = build_three_page_api()
    fetcher = CachedThreadFetcher(api, cache_dir=artifacts_dir)
    fetcher.fetch(7001)
    request_count = len(api.requests)

    api.requests.clear()
    result = fetcher.fetch(7001)

    assert api.requests == [(7001, 1)]  # 只花一次请求
    assert len(result.posts) == 7
    assert result.pages_fetched == 0
    assert result.from_cache is True
    assert result.changed is False
    assert request_count == 3


def test_cache_disabled_always_pages_through(artifacts_dir):
    api = build_three_page_api()
    fetcher = CachedThreadFetcher(api, cache_dir=artifacts_dir, use_cache=False)
    fetcher.fetch(7001)
    api.requests.clear()
    fetcher.fetch(7001)
    assert api.requests == [(7001, 1), (7001, 2), (7001, 3)]


def test_incremental_fetch_only_downloads_missing_pages(artifacts_dir):
    api = build_three_page_api()
    fetcher = CachedThreadFetcher(api, cache_dir=artifacts_dir)
    fetcher.fetch(7001)

    # 服务器上多了 2 条回复，因此出现第 4 页。
    api.pages[1] = make_page(
        7001, 1, [make_reply(101), make_reply(102)], reply_count=8, page_count=4
    )
    api.pages[4] = make_page(
        7001, 4, [make_reply(107), make_reply(108)], reply_count=8, page_count=4
    )
    api.requests.clear()

    result = fetcher.fetch(7001)

    assert api.requests == [(7001, 1), (7001, 4)]  # 中间两页直接复用
    assert result.pages_fetched == 1
    # 复用的是第 2、3 页；第 1 页每轮都必须请求，用来判断有没有新回复。
    assert result.pages_reused == 2
    assert len(result.posts) == 9
    assert result.new_posts == 2
    assert result.edited_posts == 0
    assert result.changed is True
    assert "增量更新" in result.reason


def test_verify_cached_detects_edited_post(artifacts_dir):
    api = build_three_page_api()
    fetcher = CachedThreadFetcher(api, cache_dir=artifacts_dir)
    fetcher.fetch(7001)

    # 老楼层在服务器上被编辑（回复数不变）。
    api.pages[3] = make_page(
        7001, 3, [make_reply(105), make_reply(106, content="被编辑过的回复")],
        reply_count=6, page_count=3,
    )
    api.requests.clear()

    result = fetcher.fetch(7001, verify_cached=True)

    assert api.requests == [(7001, 1), (7001, 3)]  # 只重抓最终页做校验
    assert result.edited_posts == 1
    assert result.changed is True
    assert "变化" in result.reason


def test_verify_cached_reports_no_change(artifacts_dir):
    api = build_three_page_api()
    fetcher = CachedThreadFetcher(api, cache_dir=artifacts_dir)
    fetcher.fetch(7001)
    api.requests.clear()

    result = fetcher.fetch(7001, verify_cached=True)

    assert result.changed is False
    assert result.edited_posts == 0
    assert "一致" in result.reason


def test_fetch_meta_is_cheap(artifacts_dir):
    api = build_three_page_api()
    fetcher = CachedThreadFetcher(api, cache_dir=artifacts_dir)

    meta = fetcher.fetch_meta(7001)

    assert api.requests == [(7001, 1)]
    assert meta.reply_count == 6
    assert meta.title == "测试串标题"
    assert meta.po_hash == PO_HASH


def test_invalid_url_raises(artifacts_dir):
    fetcher = CachedThreadFetcher(FakeApi({}), cache_dir=artifacts_dir)
    with pytest.raises(XdaoError):
        fetcher.fetch("这不是网址")


def test_api_error_payload_raises(artifacts_dir):
    class ErrorApi(FakeApi):
        def fetch_thread_page(self, thread_id: int, page: int = 1) -> dict:
            return {"success": False, "error": "该串不存在"}

    fetcher = CachedThreadFetcher(ErrorApi({}), cache_dir=artifacts_dir)
    with pytest.raises(XdaoError, match="该串不存在"):
        fetcher.fetch(7001)


def test_tips_post_is_filtered_out(artifacts_dir):
    api = FakeApi(
        {
            1: make_page(
                7001,
                1,
                [
                    make_reply(101),
                    {"id": 9999999, "user_hash": "Tips", "content": "Tips 酱提示", "name": "Tips"},
                    make_reply(102, cookie="Tips"),
                ],
                reply_count=2,
                page_count=1,
            )
        }
    )
    fetcher = CachedThreadFetcher(api, cache_dir=artifacts_dir)
    result = fetcher.fetch(7001)
    assert [p.id for p in result.posts] == [7001, 101]


def test_cache_survives_short_page(artifacts_dir):
    """第 2 页起只有主帖时应视为到底，不再继续翻页。"""
    api = FakeApi(
        {
            1: make_page(7001, 1, [make_reply(101)], reply_count=99, page_count=9),
            2: make_page(7001, 2, [], reply_count=99, page_count=9),
        }
    )
    fetcher = CachedThreadFetcher(api, cache_dir=artifacts_dir)
    result = fetcher.fetch(7001)
    assert api.requests == [(7001, 1), (7001, 2)]
    assert len(result.posts) == 2


def test_corrupt_page_file_falls_back_to_download(artifacts_dir):
    api = build_three_page_api()
    fetcher = CachedThreadFetcher(api, cache_dir=artifacts_dir)
    fetcher.fetch(7001)

    (artifacts_dir / "pages" / "7001" / "2.json").unlink()
    api.requests.clear()
    result = fetcher.fetch(7001)

    assert (7001, 2) in api.requests  # 缺页会重新下载
    assert len(result.posts) == 7


# ---------- 缓存不可写时的行为 ----------


def test_cache_write_failure_is_reported_not_swallowed(artifacts_dir, monkeypatch):
    """缓存写不进去时必须留下痕迹，否则用户以为抓取成果已留存。"""
    api = build_three_page_api()
    fetcher = CachedThreadFetcher(api, cache_dir=artifacts_dir)

    from pathlib import Path as _Path

    real_write_text = _Path.write_text

    def fake_write_text(self, *args, **kwargs):
        if self.suffix == ".json" and "pages" in str(self):
            raise PermissionError(13, "Permission denied")
        return real_write_text(self, *args, **kwargs)

    monkeypatch.setattr(_Path, "write_text", fake_write_text)
    result = fetcher.fetch(7001)

    assert "PermissionError" in result.cache_warning
    assert len(result.posts) == 7  # 抓取本身照常完成


def test_cache_falls_back_to_a_writable_place(artifacts_dir, monkeypatch):
    """首选缓存目录写不进去时换一个能写的地方继续，并说明换了地方。

    真实场景：导出目录在只读介质 / 权限受限的位置（用户报过 `D:\\X岛\\.cache`
    写不进去）。缓存只是加速手段，不该因为它写不进去就整次导出失败。
    """
    import xdao.cache as cache_module

    blocked = artifacts_dir / "blocked-cache"
    good = artifacts_dir / "good-cache"

    def fake_can_write(directory):
        return Path(directory) != blocked

    monkeypatch.setattr(cache_module, "can_write_dir", fake_can_write)
    monkeypatch.setattr(cache_module, "cache_dir_candidates", lambda preferred=None: [blocked, good])

    fetcher = CachedThreadFetcher(build_three_page_api(), cache_dir=blocked)
    assert fetcher.cache_dir == good
    assert str(blocked) in fetcher.cache_note and str(good) in fetcher.cache_note

    result = fetcher.fetch(7001)
    assert len(result.posts) == 7
    assert str(blocked) in result.cache_warning
    # 换了地方也确实缓存下来了：状态文件写在新的目录里
    assert (good / "threads" / "7001.json").exists()


def test_cache_keeps_the_requested_dir_when_it_is_writable(artifacts_dir):
    """首选目录能写时不做任何替换，也不留提示。"""
    import xdao.cache as cache_module

    fetcher = CachedThreadFetcher(build_three_page_api(), cache_dir=artifacts_dir)
    assert fetcher.cache_dir == artifacts_dir
    assert fetcher.cache_note == ""
    assert isinstance(cache_module.can_write_dir(artifacts_dir), bool)


def test_cache_dir_candidates_are_ordered_and_unique(artifacts_dir):
    """候选顺序：用户指定的 → 用户配置目录 → 系统临时目录，且不重复。"""
    preferred = artifacts_dir / "wanted"
    candidates = cache_dir_candidates(preferred)
    assert candidates[0] == preferred
    assert len(candidates) == len(set(candidates))
    assert len(candidates) >= 2
    assert all(isinstance(c, Path) for c in candidates)


def test_resolve_cache_dir_returns_note_only_when_it_moves(artifacts_dir, monkeypatch):
    """能写就原样返回；换了地方就在备注里说明原因和目标。"""
    import xdao.cache as cache_module

    preferred = artifacts_dir / "preferred"
    resolved, note = resolve_cache_dir(preferred)
    assert resolved == preferred and note == ""

    blocked = artifacts_dir / "blocked"
    good = artifacts_dir / "good"
    monkeypatch.setattr(cache_module, "can_write_dir", lambda d: Path(d) != blocked)
    monkeypatch.setattr(cache_module, "cache_dir_candidates", lambda preferred=None: [blocked, good])
    resolved, note = resolve_cache_dir(blocked)
    assert resolved == good
    assert str(blocked) in note and str(good) in note


def test_fetch_stops_early_when_no_cache_dir_is_writable(artifacts_dir, monkeypatch):
    """所有候选位置都写不进去时才报错，而且一个请求都不发。"""
    api = build_three_page_api()
    fetcher = CachedThreadFetcher(api, cache_dir=artifacts_dir)

    import xdao.cache as cache_module
    from xdao.exporters._shared import OutputDirNotWritable

    def boom(*args, **kwargs):
        raise OutputDirNotWritable("缓存目录不可写（测试）")

    monkeypatch.setattr(cache_module, "ensure_writable", boom)
    # 只留一个候选，否则真实的兜底目录（用户配置目录）会顶上，测不到"全都写不进去"
    monkeypatch.setattr(
        cache_module, "cache_dir_candidates", lambda preferred=None: [artifacts_dir]
    )
    with pytest.raises(XdaoError, match="缓存"):
        fetcher.fetch(7001)
    assert api.requests == []  # 一个请求都没发出去


def test_fetch_without_cache_skips_the_check(artifacts_dir, monkeypatch):
    """关掉缓存时不做可写性预检，照常抓取。"""
    api = build_three_page_api()
    fetcher = CachedThreadFetcher(api, cache_dir=artifacts_dir, use_cache=False)

    import xdao.cache as cache_module
    from xdao.exporters._shared import OutputDirNotWritable

    def boom(*args, **kwargs):
        raise OutputDirNotWritable("不该被调用")

    monkeypatch.setattr(cache_module, "ensure_writable", boom)
    result = fetcher.fetch(7001)
    assert len(result.posts) == 7
