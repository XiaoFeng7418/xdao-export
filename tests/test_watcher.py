"""监控与配置的测试（全部离线）。

产物统一写到工作区内已提交的 ``.test-artifacts``：
受限环境下运行时新建的目录不可写，所以这里显式建目录并复用，
不依赖 pytest 的 ``tmp_path``。
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from tests.test_cache import FakeApi, build_three_page_api, make_page, make_reply
from xdao.client import XdaoError
from xdao.settings import (
    DEFAULT_RETRIES,
    DEFAULT_THROTTLE,
    DEFAULT_TIMEOUT,
    DEFAULT_WATCH_INTERVAL,
    AppSettings,
)
from xdao.watcher import WatchTarget, check_once, describe_targets, watch_forever

ARTIFACTS = Path(__file__).resolve().parent.parent / ".test-artifacts"


def workspace_dir(name: str) -> Path:
    """给一个用例准备干净的产物目录。

    每次调用都清空该目录：监控会往缓存里写"上次导出状态"，
    若沿用上一次运行的残留，用例之间会互相干扰。
    """
    import shutil

    base = ARTIFACTS / name
    if base.exists():
        shutil.rmtree(base, ignore_errors=True)
    base.mkdir(parents=True, exist_ok=True)
    return base


def prepare_dirs(name: str) -> tuple[Path, Path, Path]:
    """返回 (根目录, 导出目录, 缓存目录)，三者都已存在。"""
    base = workspace_dir(name)
    out = base / "out"
    cache = base / "cache"
    out.mkdir(exist_ok=True)
    cache.mkdir(exist_ok=True)
    return base, out, cache


# ---------- WatchTarget ----------


def test_watch_target_parses_thread_id():
    assert WatchTarget("https://www.nmbxd1.com/t/7001").thread_id == 7001
    assert WatchTarget("7001").thread_id == 7001
    assert WatchTarget("乱七八糟").thread_id is None
    assert WatchTarget("https://www.nmbxd1.com/t/7001").label == "7001"


def test_watch_target_round_trip():
    target = WatchTarget(
        url_or_id="https://www.nmbxd1.com/t/7001",
        scope="po",
        format_key="markdown",
        include_hashes=["abc", "def"],
    )
    again = WatchTarget.from_dict(target.to_dict())
    assert again.url_or_id == target.url_or_id
    assert again.scope == "po"
    assert again.format_key == "markdown"
    assert again.include_hashes == ["abc", "def"]
    assert again.thread_id == 7001


def test_watch_target_from_dict_defaults():
    target = WatchTarget.from_dict({})
    assert target.scope == "all"
    assert target.format_key == "html"
    assert target.include_hashes == []
    assert target.state == "unknown"


def test_describe_targets():
    assert describe_targets([]) == "未添加监控串"
    text = describe_targets([WatchTarget("7001"), WatchTarget("7002", format_key="epub")])
    assert "2 个串" in text
    assert "No.7001.html" in text
    assert "No.7002.epub" in text


# ---------- check_once ----------


def test_first_check_exports_and_writes_file():
    _, out, cache = prepare_dirs("watcher-first")
    api = build_three_page_api()
    target = WatchTarget("7001")

    result = check_once(api, target, out, cache_dir=cache)

    assert result.ok
    assert result.changed is True
    assert result.first_run is True
    assert result.total_posts == 7
    assert result.exported is not None and result.exported.exists()
    assert result.exported.suffix == ".html"
    assert target.exports == 1
    assert "已导出" in target.state


def test_second_check_costs_one_request_and_writes_nothing():
    _, out, cache = prepare_dirs("watcher-second")
    api = build_three_page_api()
    target = WatchTarget("7001")
    check_once(api, target, out, cache_dir=cache)

    api.requests.clear()
    result = check_once(api, target, out, cache_dir=cache)

    assert result.changed is False
    assert result.exported is None
    assert api.requests == [(7001, 1)]  # 没有新回复时只花一次请求
    assert "无更新" in target.state


def test_check_detects_new_replies_and_reexports():
    _, out, cache = prepare_dirs("watcher-new")
    api = build_three_page_api()
    target = WatchTarget("7001")
    check_once(api, target, out, cache_dir=cache)

    # 服务器上多了 2 条回复 → 出现第 4 页
    api.pages[1] = make_page(
        7001, 1, [make_reply(101), make_reply(102)], reply_count=8, page_count=4
    )
    api.pages[4] = make_page(
        7001, 4, [make_reply(107), make_reply(108)], reply_count=8, page_count=4
    )

    result = check_once(api, target, out, cache_dir=cache)

    assert result.changed is True
    assert result.new_posts == 2
    assert result.edited_posts == 0  # 老楼层没被改，不应误报
    assert result.first_run is False
    assert result.total_posts == 9
    assert result.exported is not None
    assert target.exports == 2
    assert "新增" in target.state


def test_check_respects_format_and_scope():
    _, out, cache = prepare_dirs("watcher-format")
    api = build_three_page_api()
    target = WatchTarget("7001", scope="po", format_key="markdown")

    result = check_once(api, target, out, cache_dir=cache)

    assert result.exported is not None
    assert result.exported.suffix == ".md"
    text = result.exported.read_text(encoding="utf-8")
    # 只抓 PO：主帖在内，普通回复不在
    assert "主帖正文" in text
    assert "回复 101" not in text


def test_check_reports_api_error():
    base = workspace_dir("watcher-error")
    api = build_three_page_api()

    def boom(thread_id: int, page: int = 1):
        raise XdaoError("接口挂了")

    api.fetch_thread_page = boom  # type: ignore[method-assign]
    target = WatchTarget("7001")

    result = check_once(api, target, base / "out", cache_dir=base / "cache")

    assert result.ok is False
    assert "接口挂了" in result.error
    assert target.state == "出错"
    assert target.last_error == "接口挂了"
    assert target.exports == 0


def test_check_reports_empty_scope_without_exporting():
    base = workspace_dir("watcher-empty")
    out = base / "out"
    out.mkdir(exist_ok=True)
    api = build_three_page_api()
    target = WatchTarget("7001", include_hashes=["不存在的饼干"])

    result = check_once(api, target, out, cache_dir=base / "cache")

    assert result.ok is False
    assert "没有楼层" in result.error
    assert result.exported is None
    assert target.state == "无内容"


# ---------- watch_forever ----------


def test_watch_forever_returns_immediately_when_stopped():
    base = workspace_dir("watcher-stopped")
    api = build_three_page_api()
    event = threading.Event()
    event.set()
    results: list = []

    watch_forever(
        api,
        [WatchTarget("7001")],
        base / "out",
        interval=15,
        stop_event=event,
        cache_dir=base / "cache",
        on_result=results.append,
    )

    assert results == []
    assert api.requests == []  # 已停止时不发请求


def test_watch_forever_calls_back_per_target():
    _, out, cache = prepare_dirs("watcher-loop")
    api = build_three_page_api()
    event = threading.Event()
    results: list = []

    def on_result(result) -> None:
        results.append(result)
        if len(results) >= 2:
            event.set()  # 两个串各检查一次后收工

    watch_forever(
        api,
        [WatchTarget("7001"), WatchTarget("7001", format_key="txt")],
        out,
        interval=15,
        stop_event=event,
        cache_dir=cache,
        on_result=on_result,
    )

    assert len(results) == 2
    assert {r.exported.suffix for r in results if r.exported} == {".html", ".txt"}


def test_watch_forever_survives_callback_exception():
    _, out, cache = prepare_dirs("watcher-callback")
    api = build_three_page_api()
    event = threading.Event()

    def bad_callback(_result) -> None:
        event.set()
        raise RuntimeError("回调炸了")

    # 回调异常不应冒泡出来中断监控
    watch_forever(
        api,
        [WatchTarget("7001")],
        out,
        interval=15,
        stop_event=event,
        cache_dir=cache,
        on_result=bad_callback,
    )
    assert event.is_set()


# ---------- AppSettings ----------


def make_settings(name: str) -> AppSettings:
    base = workspace_dir("settings")
    path = base / f"{name}.json"
    path.unlink(missing_ok=True)
    return AppSettings(_path=path)


def test_settings_round_trip():
    settings = make_settings("round-trip")
    settings.userhash = "abc123"
    settings.output_dir = "D:/导出"
    settings.proxy = "http://127.0.0.1:7890"
    settings.timeout = 33.0
    settings.retries = 5
    settings.throttle = 0.5
    settings.format_key = "epub"
    settings.scope = "po"
    settings.filename_template = "[{id}] {title}"
    settings.include_hashes = "aaa bbb"
    settings.use_cache = False
    settings.cache_dir = "D:/cache"
    settings.watch_interval = 120.0
    settings.verify_cached = True
    settings.watch_targets = [WatchTarget("7001").to_dict()]
    settings.save()

    # 用同一份文件重新加载
    again = AppSettings(_path=settings.config_path)
    again._apply(json.loads(settings.config_path.read_text(encoding="utf-8")))

    assert again.userhash == "abc123"
    assert again.output_dir == "D:/导出"
    assert again.proxy == "http://127.0.0.1:7890"
    assert again.timeout == 33.0
    assert again.retries == 5
    assert again.throttle == 0.5
    assert again.format_key == "epub"
    assert again.scope == "po"
    assert again.filename_template == "[{id}] {title}"
    assert again.include_hashes == "aaa bbb"
    assert again.use_cache is False
    assert again.cache_dir == "D:/cache"
    assert again.watch_interval == 120.0
    assert again.verify_cached is True
    assert again.watch_targets == [WatchTarget("7001").to_dict()]


def test_settings_load_ignores_corrupt_file():
    settings = make_settings("corrupt")
    settings.config_path.write_text("{ 坏掉的 JSON", encoding="utf-8")

    loaded = AppSettings(_path=settings.config_path)
    loaded._apply({})  # 模拟 load() 在解析失败后的行为

    assert loaded.timeout == DEFAULT_TIMEOUT
    assert loaded.userhash is None


def test_settings_apply_rejects_bad_values():
    settings = make_settings("bad-values")
    settings._apply(
        {
            "timeout": "abc",
            "retries": -3,
            "throttle": 0,
            "watch_interval": None,
            "watch_targets": "不是列表",
        }
    )
    assert settings.timeout == DEFAULT_TIMEOUT
    assert settings.retries == DEFAULT_RETRIES
    assert settings.throttle == DEFAULT_THROTTLE
    assert settings.watch_interval == DEFAULT_WATCH_INTERVAL
    assert settings.watch_targets == []


def test_settings_round_trips_the_theme():
    settings = make_settings("theme")
    settings.theme_name = "dark"
    settings.save()

    again = AppSettings(_path=settings.config_path)
    again._apply(json.loads(settings.config_path.read_text(encoding="utf-8")))

    assert again.theme_name == "dark"


def test_settings_rejects_an_unknown_theme():
    """写了不认识的名字就当没写：界面上不该出现一个空白选项。"""
    settings = make_settings("theme-bad")
    settings._apply({"theme": "彩虹色"})

    assert settings.theme_name == "light"
    # 缺字段时也要给默认值（老配置文件里没有 theme 这一项）
    settings._apply({})
    assert settings.theme_name == "light"



def test_settings_save_is_quiet_on_failure():
    # 指向一个不可能的路径，save() 不应抛异常
    settings = AppSettings(_path=Path("Z:/不存在的盘/xdao/config.json"))
    settings.save()


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("", []),
        ("aaa", ["aaa"]),
        ("aaa bbb", ["aaa", "bbb"]),
        ("aaa,bbb", ["aaa", "bbb"]),
        ("aaa, bbb\nccc", ["aaa", "bbb", "ccc"]),
        ("aaa aaa", ["aaa"]),
        ("  ", []),
    ],
)
def test_parse_hashes(raw, expected):
    assert make_settings("parse").parse_hashes(raw) == expected


def test_resolved_cache_dir_prefers_explicit_setting():
    settings = make_settings("cache-dir")
    assert settings.resolved_cache_dir("D:/out") == Path("D:/out") / ".cache"
    settings.cache_dir = "D:/mycache"
    assert settings.resolved_cache_dir("D:/out") == Path("D:/mycache")


# ---------- PDF 选项要一路传到监控用的导出器 ----------
#
# 起因：v0.8.0 给 PDF 加了纸张/边距设置，但串监控那条路没接上 ——
# 用户在设置里选了 A3、无边距，手动导出对、后台监控导出的却还是默认纸张。
# 这类「只有一条路接上了」的漏洞靠肉眼看不出来，所以用契约断言钉住。


def test_check_once_hands_pdf_options_and_browser_to_the_exporter(monkeypatch):
    """监控导出 PDF 时，设置里的纸张/边距与浏览器路径必须传下去。

    渲染换成假件：真渲染要**启动浏览器**，而这条用例验的是「设置有没有一路传到渲染层」，
    与浏览器起不起得来无关。真机渲染由 `tests/test_pdf_render.py` 在
    `XDAO_PDF_TEST=1` 时单独跑（2026-10-02：这条用例在 CI 上因为浏览器起不来红过一次，
    日志是 `CdpError: 等了 60 秒还没等到 Chrome 的调试端口`）。
    """
    import xdao.watcher as watcher_module
    from xdao.exporters import pdf as pdf_module
    from xdao.pdf_opts import PdfOptions

    _, out, cache = prepare_dirs("watcher-pdf-options")
    api = build_three_page_api()
    target = WatchTarget("7001", format_key="pdf")
    options = PdfOptions(paper="a3", margin="none")
    seen: list[dict] = []
    rendered: list[dict] = []
    real_create = watcher_module.create_exporter

    def spy_create(*args, **kwargs):
        seen.append(kwargs)
        return real_create(*args, **kwargs)

    def fake_render(html, output_pdf, **kwargs):
        rendered.append(kwargs)
        Path(output_pdf).write_bytes(b"%PDF-1.4 fake\n")
        return Path(output_pdf)

    monkeypatch.setattr(watcher_module, "create_exporter", spy_create)
    monkeypatch.setattr(pdf_module, "render_html_to_pdf", fake_render)

    result = check_once(
        api,
        target,
        out,
        cache_dir=cache,
        browser_path="C:/假的浏览器.exe",
        pdf_options=options,
    )

    assert result.ok, result.error
    assert len(seen) == 1, "监控没有走到导出那一步"
    assert seen[0]["browser_path"] == "C:/假的浏览器.exe"
    assert seen[0]["pdf_options"] is options
    assert rendered and rendered[0]["browser_path"] == "C:/假的浏览器.exe"
    assert rendered[0]["options"] is options


def test_check_once_defaults_keep_the_old_behaviour(monkeypatch):
    """不传这两项时不能凭空多出参数 —— 老调用方（含测试）必须照常工作。"""
    import xdao.watcher as watcher_module

    _, out, cache = prepare_dirs("watcher-pdf-default")
    api = build_three_page_api()
    seen: list[dict] = []
    real_create = watcher_module.create_exporter

    def spy_create(*args, **kwargs):
        seen.append(kwargs)
        return real_create(*args, **kwargs)

    monkeypatch.setattr(watcher_module, "create_exporter", spy_create)

    check_once(api, WatchTarget("7001"), out, cache_dir=cache)

    assert seen[0]["browser_path"] is None
    assert seen[0]["pdf_options"] is None


def test_watch_forever_passes_pdf_options_into_every_check(monkeypatch):
    """后台循环的每一轮检查都要带上 PDF 选项与浏览器路径。"""
    import xdao.watcher as watcher_module
    from xdao.pdf_opts import PdfOptions

    base = workspace_dir("watcher-loop-pdf")
    empty = watcher_module.WatchResult(target=WatchTarget("7001"))
    calls: list[dict] = []
    event = threading.Event()

    def fake_check(*args, **kwargs):
        calls.append(kwargs)
        event.set()  # 检查一次就收工
        return empty

    monkeypatch.setattr(watcher_module, "check_once", fake_check)
    options = PdfOptions(paper="a4", scale=1.2)

    watch_forever(
        build_three_page_api(),
        [WatchTarget("7001", format_key="pdf")],
        base / "out",
        interval=15,
        stop_event=event,
        cache_dir=base / "cache",
        browser_path="D:/浏览器.exe",
        pdf_options=options,
    )

    assert calls, "监控一轮都没跑"
    assert all(call["browser_path"] == "D:/浏览器.exe" for call in calls)
    assert all(call["pdf_options"] is options for call in calls)
