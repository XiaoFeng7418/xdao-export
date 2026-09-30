"""命令行入口的测试（离线）。

只覆盖不联网的部分：参数解析、版本输出，以及「失败原因要写进日志文件」
这条约定 —— 之前长串 PDF 失败时一个字都没显示出来，才有了这个要求。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from main import build_parser, main

ARTIFACTS = Path(__file__).resolve().parent.parent / ".test-artifacts"


@pytest.fixture
def out_dir() -> Path:
    import shutil

    path = ARTIFACTS / "cli"
    shutil.rmtree(path, ignore_errors=True)
    path.mkdir(parents=True, exist_ok=True)
    return path


def test_parser_knows_all_formats():
    args = build_parser().parse_args(["7001"])
    assert args.format is None
    for fmt in ("html", "pdf", "txt", "markdown", "epub"):
        assert build_parser().parse_args(["7001", "-f", fmt]).format == fmt


def test_parser_pdf_options():
    args = build_parser().parse_args(
        ["7001", "-f", "pdf", "--pdf-browser", r"D:\chrome.exe", "--pdf-timeout", "120"]
    )
    assert args.pdf_browser == r"D:\chrome.exe"
    assert args.pdf_timeout == 120


def test_parser_watch_and_scope():
    args = build_parser().parse_args(["7001", "7002", "--watch", "--interval", "60", "--scope", "po"])
    assert args.threads == ["7001", "7002"]
    assert args.watch is True
    assert args.interval == 60
    assert args.scope == "po"


def test_version_flag_exits_zero(capsys):
    from xdao import __version__

    with pytest.raises(SystemExit) as exc:
        build_parser().parse_args(["--version"])
    assert exc.value.code == 0
    assert __version__ in capsys.readouterr().out


class OfflineClient:
    """离线客户端替身：run_cli 会用到的接口都在这里实现。"""

    def __init__(self, *args, **kwargs) -> None:
        self.cdn_path = "https://image.nmb.best/"
        self.image_cache_dir = None
        self.userhash = ""

    def set_userhash(self, value: str) -> None:
        self.userhash = value

    def apply_config(self, settings) -> None:  # pragma: no cover - 保留接口
        pass

    def image_url(self, img: str, ext: str, thumb: bool = False) -> str:
        return f"{self.cdn_path}image/{img}{ext}"

    def download_image(self, url: str) -> bytes:  # pragma: no cover
        raise AssertionError("离线用例不应该下载图片")


class OfflineSettings:
    """不触碰用户真实配置的 AppSettings 替身。"""

    def __init__(self) -> None:
        self.userhash = "TESTHASH"
        self.output_dir = None
        self.proxy = ""
        self.timeout = 20.0
        self.retries = 2
        self.throttle = 0.08
        self.format_key = "html"
        self.scope = "all"
        self.filename_template = ""
        self.image_mode = "embed"
        self.pdf_browser = ""
        self.use_cache = False
        self.cache_dir = ""
        self.watch_interval = 300.0
        self.verify_cached = False
        self.watch_targets: list = []

    @classmethod
    def load(cls) -> "OfflineSettings":
        return cls()

    def save(self) -> None:
        pass

    def resolved_cache_dir(self, output_dir=None):
        return Path(output_dir or ".") / ".cache"

    def parse_hashes(self, raw=None):
        return []


def patch_offline(monkeypatch):
    """把网络、缓存抓取与配置都换成本地替身。"""
    import xdao.cache as cache_module
    import xdao.client as client_module
    import xdao.exporters as exporters_module
    import xdao.settings as settings_module

    monkeypatch.setattr(client_module, "XdaoClient", OfflineClient)
    monkeypatch.setattr(settings_module, "AppSettings", OfflineSettings)
    return exporters_module, cache_module


@pytest.fixture(autouse=True)
def isolate_settings(monkeypatch):
    """避免用例读到用户真实的 %APPDATA% 配置（里面有真的 userhash）。"""
    import xdao.settings as settings_module

    monkeypatch.setattr(settings_module, "AppSettings", OfflineSettings)
    yield


def make_fake_fetcher(thread_id: int, title: str):
    class FakeResult:
        cache_warning = ""
        reason = "测试用抓取结果"

        class meta:  # noqa: N801
            pass

        posts: list = []

    FakeResult.meta.thread_id = thread_id
    FakeResult.meta.title = title
    FakeResult.meta.po_hash = "PO"

    class FakeFetcher:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def fetch(self, *args, **kwargs):
            return FakeResult()

    return FakeFetcher


def test_failure_reason_is_written_to_log(out_dir, monkeypatch):
    """导出失败时必须留下日志文件，且里面写清楚失败原因。"""
    exporters_module, cache_module = patch_offline(monkeypatch)

    def boom(*args, **kwargs):
        raise RuntimeError("模拟的导出失败原因")

    monkeypatch.setattr(cache_module, "CachedThreadFetcher", make_fake_fetcher(5001, "测试串"))
    monkeypatch.setattr(
        exporters_module, "create_exporter", lambda *a, **k: type("X", (), {"save": staticmethod(boom)})()
    )

    code = main(["5001", "-f", "html", "-o", str(out_dir)])
    assert code == 1

    log = out_dir / "xdao-export.log"
    assert log.exists(), "失败时必须写日志文件"
    text = log.read_text(encoding="utf-8")
    assert "模拟的导出失败原因" in text
    assert "5001" in text
    assert "成功 0 / 1" in text


def test_success_also_writes_log(out_dir, monkeypatch):
    exporters_module, cache_module = patch_offline(monkeypatch)
    monkeypatch.setattr(cache_module, "CachedThreadFetcher", make_fake_fetcher(5002, "测试串"))

    written: list[Path] = []

    class FakeExporter:
        def save(self, thread, scope, output_dir, include_hashes=None):
            path = Path(output_dir) / "结果.html"
            path.write_text("ok", encoding="utf-8")
            written.append(path)
            return path

    monkeypatch.setattr(exporters_module, "create_exporter", lambda *a, **k: FakeExporter())

    code = main(["5002", "-f", "html", "-o", str(out_dir)])
    assert code == 0
    assert written and written[0].exists()
    text = (out_dir / "xdao-export.log").read_text(encoding="utf-8")
    assert "成功 1 / 1" in text
    assert "结果.html" in text
