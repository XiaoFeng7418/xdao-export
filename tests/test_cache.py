"""缓存与断点续传的测试（全部离线，用假接口顶替网络）。"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from xdao.cache import (
    CachedThreadFetcher,
    SAFETY_MAX_PAGES,
    ThreadCache,
    cache_dir_candidates,
    default_cache_dir,
    filter_until_thread_id,
    parse_reply_count,
    parse_thread_id,
    post_fingerprint,
    resolve_cache_dir,
)
from xdao.cancel import ExportCancelled
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
    """缓存写不进去时必须留下痕迹，否则会以为抓取成果已留存。"""
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

    真实场景：导出目录在只读介质 / 权限受限的位置（实测过 `D:\\X岛\\.cache`
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
    """候选顺序：显式指定的 → 用户配置目录 → 系统临时目录，且不重复。"""
    preferred = artifacts_dir / "wanted"
    candidates = cache_dir_candidates(preferred)
    assert candidates[0] == preferred
    assert len(candidates) == len(set(candidates))
    assert len(candidates) >= 2
    assert all(isinstance(c, Path) for c in candidates)


def test_cache_dir_candidates_survive_a_broken_temp_dir(artifacts_dir, monkeypatch):
    """问不到系统临时目录时少一个候选，而不是把整次导出带走。

    真实场景（v0.10.0 打包版真机核验）：冻结出来的 exe 里
    ``tempfile.gettempdir()`` 会抛
    ``FileNotFoundError: [Errno 2] No usable temporary directory found in [...]``，
    而 ``cache_dir_candidates()`` 每次导出都会走一遍，于是整次导出直接失败。
    缓存只是加速手段，候选少一个不该有这种后果。
    """
    import tempfile

    import xdao.cache as cache_module

    def boom() -> str:
        raise FileNotFoundError(
            2,
            "No usable temporary directory found in ['C:\\\\Temp', 'C:\\\\tmp']",
        )

    monkeypatch.setattr(tempfile, "gettempdir", boom)
    monkeypatch.setenv("LOCALAPPDATA", str(artifacts_dir / "local"))
    monkeypatch.setenv("APPDATA", str(artifacts_dir / "roaming"))

    preferred = artifacts_dir / "wanted"
    candidates = cache_dir_candidates(preferred)

    assert candidates[0] == preferred
    assert str(artifacts_dir / "local") in [str(item.parent.parent) for item in candidates]
    assert str(artifacts_dir / "roaming") in [str(item.parent.parent) for item in candidates]
    assert all("Temp" not in str(item) for item in candidates)
    assert len(candidates) == 3  # 首选 + LOCALAPPDATA + APPDATA，临时目录那一条没了


def test_cache_dir_candidates_keep_the_temp_dir_when_it_works(artifacts_dir, monkeypatch):
    """反过来：临时目录问得到时照旧排进候选，别把兜底写过头。"""
    import tempfile

    from xdao.cache import DEFAULT_CACHE_DIRNAME

    temp_root = artifacts_dir / "Temp"
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(temp_root))
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.delenv("APPDATA", raising=False)

    preferred = artifacts_dir / "wanted"
    candidates = cache_dir_candidates(preferred)

    assert candidates == [preferred, temp_root / "xdao-export" / DEFAULT_CACHE_DIRNAME]


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


def test_cache_warning_explains_an_unwritable_cache_dir(artifacts_dir, monkeypatch):
    """缓存目录写不进去时，提示里要说清后果和下一步，而不是只甩一句 errno。

    实测过的场景：缓存目录跟着导出目录走，而导出目录只有上一级可写，
    于是 ``.cache`` 建得出来、``.cache\\pages`` 拒绝访问。
    """
    import xdao.cache as cache_module

    def refuse(self, page, payload):
        # 模拟 store_page 的真实行为：pages 子目录建不出来
        detail = f"PermissionError: [Errno 13] Permission denied: '{self.pages_dir}'"
        self.write_error = detail
        self.fetch_error = detail

    monkeypatch.setattr(cache_module.ThreadCache, "store_page", refuse)

    fetcher = CachedThreadFetcher(build_three_page_api(), cache_dir=artifacts_dir)
    result = fetcher.fetch(7001)
    assert len(result.posts) == 7  # 导出照常完成，缓存写不动不拦路
    warning = result.cache_warning
    assert "PermissionError" in warning
    assert str(artifacts_dir) in warning
    assert "断点续传" in warning and "设置 → 缓存" in warning


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


# ---------- 单页失败自动补抓（重试队列） ----------


class FlakyApi(FakeApi):
    """前 ``times`` 次请求指定页时返回失败，之后正常返回。

    用来模拟"接口偶发超时/限流"：一次导出里某一页第一次没抓下来。
    """

    def __init__(self, pages: dict[int, dict], flaky: dict[int, int]) -> None:
        super().__init__(pages)
        self._flaky = dict(flaky)
        self.failures: dict[int, int] = {}

    def fetch_thread_page(self, thread_id: int, page: int = 1) -> dict:
        if self._flaky.get(page, 0) > 0:
            self._flaky[page] -= 1
            self.failures[page] = self.failures.get(page, 0) + 1
            self.requests.append((thread_id, page))
            return "网络错误：连接被重置（测试）"
        return super().fetch_thread_page(thread_id, page)


def always_fail_api(pages: dict[int, dict], bad: set[int]) -> FlakyApi:
    """本来会返回的页，硬是失败到永远 —— 用来验证"缺页必须报出来"。"""
    return FlakyApi(pages, {page: 99 for page in bad})


def make_fetcher(api, artifacts_dir, **kwargs) -> CachedThreadFetcher:
    """测试里一律把补抓间隔调成 0，免得白等。"""
    kwargs.setdefault("retry_delay", 0.0)
    kwargs.setdefault("retry_attempts", 2)
    return CachedThreadFetcher(api, cache_dir=artifacts_dir, **kwargs)


def test_flaky_page_is_retried_and_recovered(artifacts_dir):
    """某一页第一次失败、补抓成功：产物完整，并说明补抓过。"""
    api = FlakyApi(
        {
            1: make_page(7001, 1, [make_reply(101)], reply_count=4, page_count=3),
            2: make_page(7001, 2, [make_reply(102)], reply_count=4, page_count=3),
            3: make_page(7001, 3, [make_reply(103)], reply_count=4, page_count=3),
        },
        flaky={2: 1},
    )
    fetcher = make_fetcher(api, artifacts_dir)
    result = fetcher.fetch(7001)

    assert api.requests == [(7001, 1), (7001, 2), (7001, 3), (7001, 2)]
    assert [post.id for post in result.posts] == [7001, 101, 102, 103]
    assert result.failed_pages == []
    assert "补抓成功" in result.retry_note
    # 抓到的页都进了缓存，下次不用再抓
    assert (artifacts_dir / "pages" / "7001" / "2.json").exists()


def test_all_retries_failing_reports_the_missing_page(artifacts_dir):
    """补抓次数用尽仍失败：明确报出缺哪一页，而不是安静地少一截。"""
    api = always_fail_api(
        {
            1: make_page(7001, 1, [make_reply(101)], reply_count=6, page_count=4),
            2: make_page(7001, 2, [make_reply(102)], reply_count=6, page_count=4),
            3: make_page(7001, 3, [make_reply(103)], reply_count=6, page_count=4),
            4: make_page(7001, 4, [make_reply(104)], reply_count=6, page_count=4),
        },
        bad={3},
    )
    fetcher = make_fetcher(api, artifacts_dir)
    result = fetcher.fetch(7001)

    assert api.failures[3] == 3  # 第一次 + 两次补抓
    assert (7001, 4) in api.requests  # 失败的那页不影响后面继续抓
    assert result.failed_pages == [3]
    assert result.truncated is True  # 缺页 = 产物不完整，界面据此弹提示
    assert "第 3 页" in result.retry_note
    assert "2 次" in result.retry_note
    assert "再导一次" in result.retry_note
    assert [post.id for post in result.posts] == [7001, 101, 102, 104]


def test_retry_can_be_switched_off(artifacts_dir):
    """retry_attempts=0 时一页都不补抓（老行为，供排障与测试用）。"""
    api = always_fail_api(
        {
            1: make_page(7001, 1, [make_reply(101)], reply_count=4, page_count=3),
            2: make_page(7001, 2, [make_reply(102)], reply_count=4, page_count=3),
            3: make_page(7001, 3, [make_reply(103)], reply_count=4, page_count=3),
        },
        bad={2},
    )
    fetcher = make_fetcher(api, artifacts_dir, retry_attempts=0)
    result = fetcher.fetch(7001)

    assert api.failures[2] == 1  # 只试了一次
    assert result.failed_pages == [2]
    assert "0 次" in result.retry_note


def test_missing_page_warning_says_which_pages_never_got_tried(artifacts_dir):
    """大面积失败时不能只报"差一页"：后面没试过的页也要如实算进缺页。"""
    pages = {
        page: make_page(7001, page, [make_reply(100 + page)], reply_count=100, page_count=8)
        for page in range(1, 9)
    }
    api = always_fail_api(pages, bad={2, 3, 4, 5, 6})
    fetcher = make_fetcher(api, artifacts_dir)
    result = fetcher.fetch(7001)

    assert result.failed_pages == [2, 3, 4, 5, 6]
    assert "等 5 页" in result.retry_note or "第 2、3、4、5、6 页" in result.retry_note


def test_failed_page_is_refetched_before_the_cached_ones(artifacts_dir):
    """补抓只补缺的那一页：已经抓下来的页走缓存，不重复请求。"""
    pages = {
        1: make_page(7001, 1, [make_reply(101)], reply_count=6, page_count=4),
        2: make_page(7001, 2, [make_reply(102)], reply_count=6, page_count=4),
        3: make_page(7001, 3, [make_reply(103)], reply_count=6, page_count=4),
        4: make_page(7001, 4, [make_reply(104)], reply_count=6, page_count=4),
    }
    first_api = always_fail_api(pages, bad={4})
    first = make_fetcher(first_api, artifacts_dir).fetch(7001)
    assert first.failed_pages == [4]  # 第 4 页怎么都抓不下来

    again = FlakyApi(pages, flaky={})
    result = make_fetcher(again, artifacts_dir).fetch(7001)

    assert again.requests == [(7001, 1), (7001, 4)]  # 第 2、3 页复用缓存，只补缺的第 4 页
    assert result.failed_pages == []
    assert [post.id for post in result.posts] == [7001, 101, 102, 103, 104]


def test_thread_with_more_pages_than_the_cache_fetches_the_tail(artifacts_dir):
    """串又长了一页：缓存只盖住前 3 页时必须去补第 4 页。

    这是"中间某页当时没抓下来"留下的坑：缓存里第 1~3 页都在、回复数也变了，
    以前会直接判定"缓存够用"就把缺页的那份产物交出去。
    """
    three = {
        page: make_page(7001, page, [make_reply(100 + page)], reply_count=6, page_count=3)
        for page in range(1, 4)
    }
    first = make_fetcher(FakeApi(three), artifacts_dir).fetch(7001)
    assert first.failed_pages == []
    assert first.truncated is False

    four = dict(three)
    four[4] = make_page(7001, 4, [make_reply(104)], reply_count=8, page_count=4)
    # 前 3 页的回复数也一起变了，否则会走"回复数未变化"的那条快捷分支。
    for page in range(1, 4):
        four[page] = make_page(
            7001, page, [make_reply(100 + page)], reply_count=8, page_count=4
        )
    api = FakeApi(four)
    result = make_fetcher(api, artifacts_dir).fetch(7001)

    assert (7001, 4) in api.requests  # 新长出来的那一页真的去抓了
    assert (7001, 2) not in api.requests and (7001, 3) not in api.requests  # 已经有的不重抓
    assert [post.id for post in result.posts] == [7001, 101, 102, 103, 104]
    assert result.failed_pages == []
    assert result.truncated is False


def test_page_cap_marks_the_result_incomplete(artifacts_dir):
    """撞上页数上限：产物照样写，但必须说明"只抓到第几页"。"""
    pages = {
        page: make_page(7001, page, [make_reply(100 + page)], reply_count=6, page_count=4)
        for page in range(1, 5)
    }
    api = FakeApi(pages)
    result = make_fetcher(api, artifacts_dir).fetch(7001, max_pages=1)

    assert (7001, 3) not in api.requests  # 到上限就停手，不再往下抓
    assert result.truncated is True
    assert result.failed_pages == []  # 不是失败，是"没抓完"
    assert "只抓到第" in result.retry_note
    assert "页数上限" in result.retry_note


def test_max_pages_is_the_total_page_limit(artifacts_dir):
    """``max_pages`` 是**总页数**上限（含第 1 页）：填 2 只抓第 1、2 页就停。

    这条是功能的正面用例：用户填的页数上限必须真的挡住后面的页，而且结果里
    要能看出"是因为到上限才停的"（``truncated`` ＋ 说明文字），不能悄悄少抓。
    """
    # 另开一个缓存目录：共用 artifacts_dir 的话会命中上一趟留下的缓存，
    # "有没有真的去请求第 2 页"就测不出来了。
    cache_dir = artifacts_dir / "cap-fresh"
    cache_dir.mkdir(parents=True, exist_ok=True)
    api = build_three_page_api()
    result = make_fetcher(api, cache_dir).fetch(7001, max_pages=2)

    assert (7001, 2) in api.requests  # 上限内的页照抓
    assert (7001, 3) not in api.requests  # 到上限就停手，不再往下抓
    assert result.meta.pages == 2
    assert [post.id for post in result.posts] == [7001, 101, 102, 103, 104]
    assert result.truncated is True  # 产物明确不完整
    assert result.failed_pages == []  # 不是失败，是"没抓完"
    assert "只抓到第" in result.retry_note


def test_page_limit_reuses_cache_without_trimming_the_product(artifacts_dir):
    """缓存里存着超过上限的页时：不再多发请求，但产物只到上限那一页。

    长串第一次不限页数抓过、第二次把上限改小，是这套功能最常见的用法 ——
    这时候不能因为"缓存够用"就把整串（超过上限的部分）交出去。
    """
    full = build_three_page_api()
    first = make_fetcher(full, artifacts_dir).fetch(7001)
    assert first.truncated is False

    api = build_three_page_api()
    result = make_fetcher(api, artifacts_dir).fetch(7001, max_pages=2)

    assert api.requests == [(7001, 1)]  # 首页照刷新，别的页一个都不多请求
    assert result.meta.pages == 2
    assert [post.id for post in result.posts] == [7001, 101, 102, 103, 104]  # 第 3 页不入成品
    assert result.truncated is True
    assert "只抓到第" in result.retry_note


def test_user_page_limit_beats_the_safety_valve(artifacts_dir):
    """用户填的上限更小时以用户上限为准，安全阀拦不住它。"""
    api = FakeApi(
        {
            page: make_page(7001, page, [make_reply(100 + page)], reply_count=6, page_count=3)
            for page in range(1, 4)
        }
    )
    result = make_fetcher(api, artifacts_dir).fetch(7001, max_pages=1)

    assert [page for _, page in api.requests] == [1]  # 第 2、3 页一个都没请求
    assert result.truncated is True
    assert SAFETY_MAX_PAGES > 1  # 挡住它的只能是用户上限，不是那道安全阀


def test_safety_valve_applies_only_when_no_user_limit(artifacts_dir):
    """不填页数上限（0＝不限）时仍然保留 5000 页那道安全阀。

    为什么这条要有：把"用户上限"与"安全阀"分开之后，很容易顺手把安全阀
    一起删掉 —— 那样一个畸形的超长串会被一把拉到底。这里用一个只认页数的
    轻量接口把 5001 次请求跑完，确认第 5001 页一次都没请求、并且明确报"没抓完"。
    """
    total_pages = SAFETY_MAX_PAGES + 1

    class HugeApi(FakeApi):
        def fetch_thread_page(self, thread_id: int, page: int = 1) -> dict:
            self.requests.append((thread_id, page))
            # 每页都给一条回复：一条回复都没有会被当成"接口说到头了"（end），
            # 那样跑不到安全阀就停了，这条用例也就测不到它想测的东西。
            payload = make_page(
                thread_id, page, [make_reply(200 + page)], reply_count=0, page_count=total_pages
            )
            self.last_page_count = total_pages
            self.last_page_index = page
            return payload

    api = HugeApi({})
    fetcher = CachedThreadFetcher(
        api, cache_dir=artifacts_dir, use_cache=False, retry_delay=0.0, retry_attempts=0
    )
    result = fetcher.fetch(7001)

    assert (7001, SAFETY_MAX_PAGES + 1) not in api.requests  # 安全阀挡住了最后一页
    assert (7001, SAFETY_MAX_PAGES) in api.requests  # 阀内的最后一页照抓
    assert len(api.requests) == SAFETY_MAX_PAGES
    assert result.truncated is True
    assert "只抓到第" in result.retry_note


def test_zero_max_pages_means_no_limit(artifacts_dir):
    """``max_pages=0`` 表示不限：比总页数短的串必须原样抓完、不算截断。"""
    api = build_three_page_api()
    result = make_fetcher(api, artifacts_dir).fetch(7001, max_pages=0)

    assert (7001, 3) in api.requests
    assert result.truncated is False
    assert result.retry_note == ""
    assert [post.id for post in result.posts] == [7001, 101, 102, 103, 104, 105, 106]


# ---------- 串号截止 ----------


def test_until_thread_id_keeps_the_cutoff_thread_itself():
    """截止是"含它"：等于截止值的那个串必须留下，它后面的才跳过。"""
    threads = [
        "https://www.nmbxd1.com/t/7001",
        "https://www.nmbxd1.com/t/7003",
        "https://www.nmbxd1.com/t/7005",
    ]
    kept, skipped = filter_until_thread_id(threads, 7003)

    assert kept == threads[:2]
    assert skipped == threads[2:]


def test_until_thread_id_without_the_cutoff_thread_keeps_everything_below():
    """截止串没在列表里：串号 ≤ 截止值的都导（含等于），后面的一律跳过。"""
    threads = [
        "https://www.nmbxd1.com/t/7001",
        "https://www.nmbxd1.com/t/7003",
        "https://www.nmbxd1.com/t/7011",
    ]
    kept, skipped = filter_until_thread_id(threads, 7007)

    assert kept == threads[:2]
    assert skipped == threads[2:]


def test_until_thread_id_keeps_the_list_order_and_plain_ids():
    """列表顺序照用户写的来（这个功能不重排）；纯数字与网址写法都认。"""
    threads = ["7001", "https://www.nmbxd1.com/t/7003", "7005"]
    kept, skipped = filter_until_thread_id(threads, 7003)

    assert kept == threads[:2]
    assert skipped == threads[2:]


def test_until_thread_id_keeps_unparseable_entries():
    """解析不出串号的条目（手写的备注之类）不参与比较，也不该被悄悄丢掉。"""
    threads = ["7001", "顺手记一笔", ""]
    kept, skipped = filter_until_thread_id(threads, 7001)

    assert kept == threads
    assert skipped == []


def test_no_cutoff_keeps_every_thread():
    """没填截止（None）：一个串都不动。"""
    threads = ["7001", "7002"]
    kept, skipped = filter_until_thread_id(threads, None)

    assert kept == threads
    assert skipped == []
    assert kept is not threads  # 返回的是新列表，调用方改它不会动到原列表


def test_until_thread_id_can_skip_every_thread():
    """截止比列表里任何一个都小：全都跳过（调用方据此提示"没有要导出的串"）。"""
    threads = ["https://www.nmbxd1.com/t/7003"]
    kept, skipped = filter_until_thread_id(threads, 7001)

    assert kept == []
    assert skipped == threads


def test_fatal_api_error_still_raises(artifacts_dir):
    """第 1 页就报"串不存在"：这是硬错误，不许被补抓机制吞成半份产物。"""
    fetcher = make_fetcher(FakeApi({}), artifacts_dir)

    class MissingApi(FakeApi):
        def fetch_thread_page(self, thread_id: int, page: int = 1) -> dict:
            return {"success": False, "error": "该串不存在"}

    missing = CachedThreadFetcher(MissingApi({}), cache_dir=artifacts_dir, retry_delay=0.0)
    with pytest.raises(XdaoError, match="该串不存在"):
        missing.fetch(7001)
    assert fetcher is not None


# ---------- 取消（v0.17.0）----------


def test_stop_already_set_drops_the_fetch_before_any_request(artifacts_dir):
    """点「取消导出」后不该再发出任何一页请求。

    抓取这条路以前完全看不见停止位：用户点了取消，后台还会把整串抓完
    才开始收手 —— 长串上要等好几分钟。
    """
    api = build_three_page_api()
    stop = threading.Event()
    stop.set()
    fetcher = CachedThreadFetcher(api, cache_dir=artifacts_dir, stop=stop)

    with pytest.raises(ExportCancelled):
        fetcher.fetch(7001)

    assert api.requests == [], "停止位已经举起，一个请求都不该发"


def test_cancel_between_pages_stops_the_page_loop(artifacts_dir):
    """第 1 页回来时用户点了取消：后面的页不要再抓。"""
    api = build_three_page_api()
    stop = threading.Event()
    fetcher = CachedThreadFetcher(api, cache_dir=artifacts_dir, stop=stop)
    real_fetch = api.fetch_thread_page

    def fetch_and_cancel(thread_id: int, page: int = 1) -> dict:
        data = real_fetch(thread_id, page)
        if page == 1:
            stop.set()
        return data

    api.fetch_thread_page = fetch_and_cancel  # type: ignore[method-assign]

    with pytest.raises(ExportCancelled):
        fetcher.fetch(7001)

    assert api.requests == [(7001, 1)], "取消之后不该再请求第 2 页"


def test_fetch_without_stop_still_works(artifacts_dir):
    """不传 stop 时行为与以前一致（命令行不带取消位）。"""
    api = build_three_page_api()
    fetcher = CachedThreadFetcher(api, cache_dir=artifacts_dir)

    result = fetcher.fetch(7001)

    assert len(result.posts) == 7
