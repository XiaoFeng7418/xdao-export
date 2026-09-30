"""命令行入口的测试（离线）。

只覆盖不联网的部分：参数解析、版本输出，以及「失败原因要写进日志文件」
这条约定 —— 之前长串 PDF 失败时一个字都没显示出来，才有了这个要求。
"""

from __future__ import annotations

import json
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
        # 监控分支会读这两个（--watch 才有机会碰到上面那条 AttributeError）。
        self.notify = False
        self.notify_interval = 0.0

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


# ------------------------------------------------------- PDF 纸张 / 边距（命令行）
#
# 命令行的职责只有两件：把用户给的值（或配置里的值）凑成 PdfOptions 传给导出器，
# 以及值写错时说一句中文、别开工。真实渲染由 tests/test_pdf_render.py 覆盖。


def capture_pdf_options(monkeypatch, thread_id: int = 5005) -> list:
    """记下 create_exporter 收到的 pdf_options；不真导出、不联网。"""
    exporters_module, cache_module = patch_offline(monkeypatch)
    seen: list = []

    class FakeExporter:
        def save(self, thread, scope, output_dir, include_hashes=None):
            path = Path(output_dir) / "结果.pdf"
            path.write_text("ok", encoding="utf-8")
            return path

    def fake_create_exporter(*args, **kwargs):
        seen.append(kwargs.get("pdf_options"))
        return FakeExporter()

    monkeypatch.setattr(exporters_module, "create_exporter", fake_create_exporter)
    monkeypatch.setattr(cache_module, "CachedThreadFetcher", make_fake_fetcher(thread_id, "测试串"))
    return seen


def test_parser_knows_pdf_page_options():
    args = build_parser().parse_args(
        [
            "7001",
            "-f",
            "pdf",
            "--pdf-paper",
            "a4",
            "--pdf-orientation",
            "landscape",
            "--pdf-margin",
            "narrow",
            "--pdf-margin-mm",
            "12.5",
            "--pdf-scale",
            "0.9",
            "--pdf-pages",
            "1-3,5",
        ]
    )
    assert args.pdf_paper == "a4"
    assert args.pdf_orientation == "landscape"
    assert args.pdf_margin == "narrow"
    assert args.pdf_margin_mm == "12.5"
    assert args.pdf_scale == "0.9"  # 字符串：非法值好说中文，不走 argparse 的英文 exit 2
    assert args.pdf_pages == "1-3,5"


def test_parser_pdf_options_are_none_when_not_given():
    """没写这些开关时必须是 None，才能区分「用户没给」与「用户给了 default」。"""
    args = build_parser().parse_args(["7001"])
    for name in ("pdf_paper", "pdf_orientation", "pdf_margin", "pdf_margin_mm", "pdf_scale", "pdf_pages"):
        assert getattr(args, name) is None
    assert args.pdf_no_background is False


def test_pdf_help_text_comes_from_pdf_opts():
    """--help 里要列出可选值，且用的是 pdf_opts 的中文标签（而不是另抄一份）。"""
    import main as main_module
    from xdao import pdf_opts

    help_text = main_module._pdf_value_help("PAPER_LABELS")
    assert pdf_opts.PAPER_LABELS["a4"] in help_text
    assert "跟随网页样式" in help_text

    parser_help = build_parser().format_help()
    for flag in ("--pdf-paper", "--pdf-pages", "--pdf-no-background"):
        assert flag in parser_help


def test_pdf_options_are_passed_to_the_exporter(out_dir, monkeypatch, capsys):
    seen = capture_pdf_options(monkeypatch)

    code = main(
        [
            "5005",
            "-f",
            "pdf",
            "-o",
            str(out_dir),
            "--pdf-paper",
            "a4",
            "--pdf-orientation",
            "landscape",
            "--pdf-margin",
            "narrow",
            "--pdf-margin-mm",
            "12.5",
            "--pdf-scale",
            "0.9",
            "--pdf-pages",
            "1-3,5",
        ]
    )

    assert code == 0
    options = seen[0]
    assert (options.paper, options.orientation) == ("a4", "landscape")
    assert options.margin_mm == 12.5  # 自定义毫米数盖过 narrow 预设
    assert options.scale == 0.9
    assert options.page_ranges == "1-3,5"
    assert options.background is True
    assert options.needs_cdp is True

    out = capsys.readouterr().out
    assert "PDF 设置" in out  # 生效的设置要说一声，别让用户猜
    assert "A4" in out and "横向" in out


def test_old_config_gets_v070_behaviour(out_dir, monkeypatch, capsys):
    """老配置（一个 pdf_* 键都没有）→ 全默认，且不打扰用户。"""
    from xdao.pdf_opts import PdfOptions

    seen = capture_pdf_options(monkeypatch, 5006)

    code = main(["5006", "-f", "pdf", "-o", str(out_dir)])

    assert code == 0
    assert seen[0] == PdfOptions()
    assert seen[0].needs_cdp is False  # 不套纸张参数 = 升级前的成品外观
    assert "PDF 设置" not in capsys.readouterr().out


def test_settings_values_are_used_and_cli_wins(out_dir, monkeypatch, capsys):
    """配置里的纸张/边距生效；命令行显式给的那一项盖过配置，其余仍取配置。"""
    import xdao.settings as settings_module

    class PdfSettings(OfflineSettings):
        def __init__(self) -> None:
            super().__init__()
            self.pdf_paper = "a3"
            self.pdf_orientation = "landscape"
            self.pdf_margin = "wide"
            self.pdf_margin_mm = ""
            self.pdf_scale = "0.8"
            self.pdf_background = False
            self.pdf_page_ranges = "2,4"

    seen = capture_pdf_options(monkeypatch, 5007)
    monkeypatch.setattr(settings_module, "AppSettings", PdfSettings)

    code = main(["5007", "-f", "pdf", "-o", str(out_dir), "--pdf-paper", "a5"])

    assert code == 0
    options = seen[0]
    assert options.paper == "a5"  # 命令行赢
    assert options.orientation == "landscape"  # 没给的仍取配置
    assert options.scale == 0.8
    assert options.background is False
    assert options.page_ranges == "2,4"

    out = capsys.readouterr().out
    assert "A5" in out and "不打印背景" in out


def test_margin_also_accepts_a_plain_millimetre_number(out_dir, monkeypatch):
    """``--pdf-margin 18`` 也是合法的（pdf_opts 允许直接写毫米数）。"""
    seen = capture_pdf_options(monkeypatch, 5010)

    code = main(["5010", "-f", "pdf", "-o", str(out_dir), "--pdf-margin", "18"])

    assert code == 0
    assert seen[0].margin_mm == 18.0


def test_no_background_is_reported_even_though_paper_is_default(out_dir, monkeypatch, capsys):
    """只关了背景时也要报一声：纸张没改，成品外观却变了。"""
    seen = capture_pdf_options(monkeypatch, 5009)

    code = main(["5009", "-f", "pdf", "-o", str(out_dir), "--pdf-no-background"])

    assert code == 0
    assert seen[0].background is False
    assert seen[0].is_default is True  # is_default 只看纸张/边距/缩放
    out = capsys.readouterr().out
    assert "PDF 设置" in out and "不打印背景" in out


@pytest.mark.parametrize(
    ("flag", "value", "expected"),
    (
        ("--pdf-pages", "5-2", "页码"),
        ("--pdf-scale", "5", "缩放"),
        ("--pdf-scale", "abc", "缩放"),
        ("--pdf-margin", "thin", "边距"),
        ("--pdf-margin-mm", "999", "边距"),
        ("--pdf-paper", "b5", "纸张"),
        ("--pdf-orientation", "斜", "方向"),
    ),
)
def test_illegal_pdf_values_stop_before_any_work(monkeypatch, capsys, flag, value, expected):
    """非法值：一句中文 + 退出码 2，而且不建目录、不抓取、不导出。"""
    import shutil

    target = ARTIFACTS / "pdf-参数有误时不该建的目录"
    shutil.rmtree(target, ignore_errors=True)

    seen = capture_pdf_options(monkeypatch, 5008)

    code = main(["5008", "-f", "pdf", "-o", str(target), flag, value])

    err = capsys.readouterr().err
    assert code == 2
    assert "PDF 参数有误" in err
    assert expected in err and value in err  # 说清是哪一项、收到的是什么
    assert "Traceback" not in err
    assert seen == []  # 导出器根本没被创建
    assert not target.exists()  # 参数不对就不该先动手


def test_pdf_flags_are_still_checked_for_other_formats(out_dir, monkeypatch, capsys):
    """写错 PDF 参数时哪怕这次导的是网页版也要拦下来。

    宁可让人看到「参数有误」，也别把一个错别字悄悄咽掉、让人以为生效了。
    """
    code = main(["5011", "-f", "html", "-o", str(out_dir), "--pdf-pages", "5-2"])

    assert code == 2
    assert "PDF 参数有误" in capsys.readouterr().err


def test_pdf_settings_are_not_logged_for_other_formats(out_dir, monkeypatch, capsys):
    """导 HTML 时不该出现 PDF 设置那一行。"""
    import xdao.settings as settings_module

    class PdfSettings(OfflineSettings):
        def __init__(self) -> None:
            super().__init__()
            self.pdf_paper = "a3"

    capture_pdf_options(monkeypatch, 5012)
    monkeypatch.setattr(settings_module, "AppSettings", PdfSettings)

    code = main(["5012", "-f", "html", "-o", str(out_dir)])

    assert code == 0
    assert "PDF 设置" not in capsys.readouterr().out


def test_watch_mode_hands_pdf_options_to_every_check(out_dir, monkeypatch):
    """串监控也要用上 PDF 纸张/边距 —— 只接手动导出那条路是看不出来的漏洞。"""
    import xdao.watcher as watcher_module

    patch_offline(monkeypatch)
    calls: list[tuple] = []
    empty = watcher_module.WatchResult(target=watcher_module.WatchTarget("5009"))

    def fake_check(client, target, out, **kwargs):
        calls.append((target, kwargs))
        return empty

    # 停在「检查完一轮」那一刻，不真的进后台循环。
    # 注意：main.run_cli 是在函数里 `from xdao.watcher import check_once` 的，
    # 所以只能打 watcher 模块上的名字，模块对象上打 main.check_once 会 AttributeError。
    monkeypatch.setattr(watcher_module, "check_once", fake_check)
    monkeypatch.setattr(watcher_module, "watch_forever", lambda *a, **k: None)

    code = main(
        [
            "5009",
            "--watch",
            "-f",
            "pdf",
            "-o",
            str(out_dir),
            "--pdf-paper",
            "a3",
            "--pdf-margin",
            "none",
            "--pdf-browser",
            "C:/假浏览器.exe",
        ]
    )

    assert code == 0
    assert calls, "监控一轮都没跑"
    target, kwargs = calls[0]
    assert target.format_key == "pdf"
    options = kwargs["pdf_options"]
    assert (options.paper, options.margin) == ("a3", "none")
    assert kwargs["browser_path"] == "C:/假浏览器.exe"


# ---------- 监控列表的导入 / 导出（--watch-export / --watch-import） ----------


class SettingsWithTargets(OfflineSettings):
    """带一两个监控串的配置替身。"""

    targets: list = []

    @classmethod
    def load(cls) -> "SettingsWithTargets":
        obj = cls()
        obj.watch_targets = [dict(t) for t in cls.targets]
        return obj


@pytest.fixture()
def watch_settings(monkeypatch):
    """把配置换成带监控串的替身；返回记录下来的 save() 调用。"""
    import xdao.settings as settings_module

    saved: list[list] = []
    SettingsWithTargets.targets = [
        {
            "url_or_id": "https://www.nmbxd1.com/t/7001111",
            "scope": "all",
            "format_key": "html",
            "include_hashes": [],
            "image_mode": "embed",
        }
    ]
    monkeypatch.setattr(settings_module, "AppSettings", SettingsWithTargets)
    monkeypatch.setattr(
        SettingsWithTargets, "save", lambda self: saved.append(list(self.watch_targets))
    )
    yield SettingsWithTargets, saved
    SettingsWithTargets.targets = []


def test_watch_export_writes_the_list_without_touching_the_network(watch_settings, tmp_path):
    """导出不连网、不需要串号参数 —— 列表本来就存在配置里。"""
    out = tmp_path / "列表.json"
    code = main(["--watch-export", str(out)])
    assert code == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert [t["thread_id"] for t in payload["targets"]] == [7001111]


def test_watch_import_merges_into_the_existing_list(watch_settings, tmp_path):
    _, saved = watch_settings
    source = tmp_path / "来.json"
    source.write_text(
        json.dumps({"targets": [{"url_or_id": "7002222"}]}, ensure_ascii=False), encoding="utf-8"
    )

    code = main(["--watch-import", str(source)])

    assert code == 0
    assert saved, "导入之后必须把列表存进配置"
    ids = [t["url_or_id"] for t in saved[0]]
    assert ids == ["https://www.nmbxd1.com/t/7001111", "7002222"]


def test_watch_import_replace_replaces_the_list(watch_settings, tmp_path):
    _, saved = watch_settings
    source = tmp_path / "来.json"
    source.write_text(
        json.dumps({"targets": [{"url_or_id": "7002222"}]}, ensure_ascii=False), encoding="utf-8"
    )

    code = main(["--watch-import", str(source), "--watch-import-replace"])

    assert code == 0
    assert [t["url_or_id"] for t in saved[0]] == ["7002222"]


def test_watch_import_replace_keeps_entries_that_match_the_existing_list(
    watch_settings, tmp_path
):
    """真机踩到的坑：文件里那条**现在就已经监控着**时，「替换」不能把它当垃圾丢掉。

    之前的写法是 ``merged = result.added``，而「同一个串、同样的设置」会被算进
    ``result.skipped`` —— 于是替换之后列表里一条不剩（用户以为只是把列表换成
    文件里那些，结果清空了）。现在这些条目单独记在 ``result.duplicates`` 里。
    """
    _, saved = watch_settings
    source = tmp_path / "来.json"
    source.write_text(
        json.dumps(
            {
                "targets": [
                    {"url_or_id": "7001111"},  # 配置里本来就有这一条
                    {"url_or_id": "7002222"},  # 这一条是新的
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    code = main(["--watch-import", str(source), "--watch-import-replace"])

    assert code == 0
    # 配置里的 url_or_id 是怎么写就怎么留（既有的那条是完整网址，新来的那条是裸串号），
    # 所以这里按串号比，别按字面比 —— 字面比会在两条「其实都在」时误报。
    from xdao.watcher import parse_thread_id

    got = [parse_thread_id(t["url_or_id"]) for t in saved[0]]
    assert got == [7002222, 7001111], (
        "替换只能丢掉「不在文件里」的条目，不能丢掉「本来就在、文件里也有」的条目"
    )


def test_watch_import_reports_skipped_entries(watch_settings, tmp_path, capsys):
    source = tmp_path / "来.json"
    source.write_text(
        json.dumps({"targets": [{"url_or_id": "7002222", "format_key": "docx"}]}, ensure_ascii=False),
        encoding="utf-8",
    )

    code = main(["--watch-import", str(source)])

    assert code == 0
    printed = capsys.readouterr().out
    assert "跳过" in printed
    assert "格式认不出：docx" in printed


def test_watch_import_bad_file_exits_with_two(watch_settings, tmp_path, capsys):
    broken = tmp_path / "坏.json"
    broken.write_text("这不是 JSON", encoding="utf-8")

    code = main(["--watch-import", str(broken)])

    assert code == 2
    assert "不是有效的 JSON" in capsys.readouterr().err


def test_watch_export_and_import_together_are_refused(watch_settings, tmp_path, capsys):
    code = main(["--watch-export", str(tmp_path / "a.json"), "--watch-import", str(tmp_path / "b.json")])
    assert code == 2
    assert "只能用一个" in capsys.readouterr().err


def test_replace_without_import_is_refused_instead_of_opening_the_gui(watch_settings, capsys):
    """单独给这个开关绝不能落到「启动图形界面」那条路 —— 用户会以为生效了。"""
    code = main(["--watch-import-replace"])
    assert code == 2
    assert "要配 --watch-import 用" in capsys.readouterr().err
