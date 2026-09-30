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


def test_invalid_output_dir_gives_clean_error(capsys):
    """导出目录没法创建时要给一句人话，而不是把 traceback 抛给用户。

    实测过：把导出目录填成 `D:\\Windows` 这类位置时，打包版直接弹出
    「Unhandled exception in script」和一大段 traceback。
    """
    blocker = ARTIFACTS / "cli-not-a-dir"
    blocker.parent.mkdir(parents=True, exist_ok=True)
    if blocker.exists():
        blocker.unlink()
    blocker.write_text("我是一个文件，不是文件夹", encoding="utf-8")

    try:
        code = main(["50000001", "-f", "txt", "-o", str(blocker)])
    finally:
        blocker.unlink(missing_ok=True)

    assert code == 1
    err = capsys.readouterr().err
    assert "导出目录没法创建" in err
    assert "Traceback" not in err


def test_output_dir_falls_back_when_the_configured_one_is_blocked(
    out_dir, tmp_path, monkeypatch, capsys
):
    """配置里的导出目录写不进去时，命令行要自动换到能写的位置。

    真实场景：导出目录设在桌面下的新文件夹，抓取全部成功、写文件时
    ``[Errno 13]``（受控文件夹访问只放行白名单程序），整趟白跑。
    ``-o`` 是命令行里写死的，所以不换地方；只有配置里的默认值才换。
    """
    import xdao.exporters as exporters_module
    import xdao.exporters._shared as shared_module

    exporters_module, cache_module = patch_offline(monkeypatch)
    local = tmp_path / "LocalAppData"
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    monkeypatch.setenv("TEMP", str(tmp_path / "Temp"))

    blocked = tmp_path / "桌面" / "被拦的新建文件夹"
    blocked.mkdir(parents=True)
    # choose_writable_dir 的默认探测函数取自 _shared，patch 必须落在那一层
    monkeypatch.setattr(shared_module, "can_write_dir", lambda path: Path(path) != blocked)

    written: list[Path] = []

    class FakeExporter:
        def save(self, thread, scope, output_dir, include_hashes=None):
            path = Path(output_dir) / "结果.html"
            path.write_text("ok", encoding="utf-8")
            written.append(path)
            return path

    monkeypatch.setattr(exporters_module, "create_exporter", lambda *a, **k: FakeExporter())
    monkeypatch.setattr(cache_module, "CachedThreadFetcher", make_fake_fetcher(5003, "测试串"))

    class BlockedSettings(OfflineSettings):
        def __init__(self) -> None:
            super().__init__()
            self.output_dir = str(blocked)

    import xdao.settings as settings_module

    monkeypatch.setattr(settings_module, "AppSettings", BlockedSettings)

    code = main(["5003", "-f", "html"])  # 没给 -o
    assert code == 0

    out = capsys.readouterr().out
    assert "受控文件夹访问" in out
    assert str(local / "xdao-export" / "导出") in out
    assert written and written[0].exists()
    assert written[0].parent == local / "xdao-export" / "导出"


def test_explicit_output_dir_is_never_silently_moved(out_dir, tmp_path, monkeypatch, capsys):
    """``-o`` 指定的目录只提示、不换地方：用户的显式选择不能被程序偷偷改掉。"""
    import xdao.exporters as exporters_module
    import xdao.exporters._shared as shared_module

    exporters_module, cache_module = patch_offline(monkeypatch)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "LocalAppData"))

    requested = tmp_path / "用户指定的目录"
    monkeypatch.setattr(shared_module, "can_write_dir", lambda path: False)
    monkeypatch.setattr(cache_module, "CachedThreadFetcher", make_fake_fetcher(5004, "测试串"))

    code = main(["5004", "-f", "txt", "-o", str(requested)])

    out = capsys.readouterr().out
    assert "注意：" in out and str(requested) in out
    assert str(tmp_path / "LocalAppData") not in out
    assert code in (0, 1)  # 目录本身能写（tmp_path），导出照常进行
    assert (requested / "xdao-export.log").exists()


def test_unexpected_error_in_cli_is_caught(monkeypatch, capsys):
    """命令行模式里没预料到的异常也要变成一句人话 + 退出码 1。"""
    import main as main_module

    def boom(args):
        raise PermissionError("模拟的意外错误")

    monkeypatch.setattr(main_module, "run_cli", boom)
    code = main_module.main(["50000001", "-f", "txt", "-o", str(ARTIFACTS)])

    assert code == 1
    err = capsys.readouterr().err
    assert "PermissionError" in err
    assert "模拟的意外错误" in err
    assert "Traceback" not in err


def test_needs_console_only_for_command_line_usage():
    """双击开界面（无参数）不该去连控制台，命令行用法才连。"""
    from main import _needs_console

    assert _needs_console([]) is False
    assert _needs_console(["50000001"]) is False  # 只给串号：可能是在终端里导出
    assert _needs_console(["--version"]) is True
    assert _needs_console(["--help"]) is True
    assert _needs_console(["--selftest"]) is True
    assert _needs_console(["--watch", "--no-notify"]) is True
    assert _needs_console(["--cache-dir=D:\\x"]) is True


def test_attach_parent_console_is_skipped_outside_frozen_build(monkeypatch):
    """源码运行时不动任何标准流（否则测试和终端都会被搅乱）。"""
    import main as main_module

    monkeypatch.delattr(main_module.sys, "frozen", raising=False)
    calls: list[str] = []
    monkeypatch.setattr(main_module, "_needs_console", lambda argv=None: calls.append("asked") or True)

    main_module._attach_parent_console(["--version"])

    assert calls == []

