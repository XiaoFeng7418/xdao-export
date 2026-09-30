"""本机自检（``xdao/preflight.py``）的离线用例。

这份检查是给「导出失败、用户手上只有一句报错」准备的，所以重点在两件事：

1. 每一项都要**自己兜住异常**（坏配置、写不进去、没浏览器都不能把它带走）；
2. 结论要**说清楚怎么办**，而不是只说「失败了」。

真机跑出来的结论由 ``main.py --selftest`` 那条路核（``tests/test_cli.py`` 里另有用例）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from xdao import preflight
from xdao.browser_login import BrowserInfo


def _ok_browser() -> BrowserInfo:
    return BrowserInfo(name="Edge", path=Path("C:/假浏览器/msedge.exe"))


# ---------------------------------------------------------------- 结果对象


def test_check_line_shows_the_label_and_the_name() -> None:
    check = preflight.Check(name="配置目录", status="fail", detail="写不进去")
    assert check.line() == "[不行] 配置目录：写不进去"


def test_check_line_puts_the_advice_on_its_own_line() -> None:
    check = preflight.Check(name="浏览器", status="fail", detail="没找到", advice="装一个 Edge")
    assert check.line() == "[不行] 浏览器：没找到\n        怎么办：装一个 Edge"


def test_unknown_status_falls_back_to_the_raw_value() -> None:
    assert preflight.Check(name="x", status="weird", detail="").label == "weird"


def test_report_add_accepts_a_check_object_or_keywords() -> None:
    report = preflight.Report()
    report.add(preflight.Check(name="一", status="ok", detail="d"))
    report.add(name="二", status="warn", detail="d2")
    assert [c.name for c in report.checks] == ["一", "二"]


def test_report_add_without_anything_is_a_programming_error() -> None:
    with pytest.raises(ValueError):
        preflight.Report().add()


def test_report_summary_counts_failures_and_warnings() -> None:
    report = preflight.Report()
    report.add(name="a", status="fail", detail="")
    report.add(name="b", status="warn", detail="")
    report.add(name="c", status="ok", detail="")
    text = report.summary()
    assert "1 处走不通" in text
    assert "1 处要注意" in text


def test_report_summary_says_all_clear_when_everything_is_ok() -> None:
    report = preflight.Report()
    report.add(name="a", status="ok", detail="")
    assert report.summary() == "本机自检全部通过。"


def test_report_render_has_a_blank_line_before_the_summary() -> None:
    report = preflight.Report()
    report.add(name="a", status="ok", detail="d")
    assert report.render() == "[可以] a：d\n\n本机自检全部通过。"


def test_report_json_is_readable_chinese() -> None:
    report = preflight.Report()
    report.add(name="配置目录", status="ok", detail="能写")
    payload = json.loads(report.to_json())
    assert payload["程序版本"]
    assert payload["检查结果"][0]["项目"] == "配置目录"
    assert payload["检查结果"][0]["结论"] == "可以"
    assert "\\u" not in report.to_json()


def test_environment_lines_include_version_python_platform_and_mode() -> None:
    text = "\n".join(preflight.environment_lines())
    assert "程序版本：" in text
    assert "Python：" in text
    assert "系统：" in text
    assert "打包运行：" in text


# ---------------------------------------------------------------- 单项检查


def test_running_mode_mentions_the_extra_flags_when_frozen(monkeypatch) -> None:
    monkeypatch.setattr(preflight.browser_flags, "is_frozen", lambda: True)
    monkeypatch.setattr(preflight.browser_flags, "launch_flags", lambda: ["--no-sandbox"])
    check = preflight._check_running_mode()
    assert check.status == "ok"
    assert "--no-sandbox" in check.detail


def test_running_mode_says_source_when_not_frozen(monkeypatch) -> None:
    monkeypatch.setattr(preflight.browser_flags, "is_frozen", lambda: False)
    check = preflight._check_running_mode()
    assert "源码运行" in check.detail


def test_config_dir_that_cannot_be_written_is_a_failure(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(preflight, "can_write_dir", lambda directory: False)
    check = preflight._check_config(tmp_path)
    assert check.status == "fail"
    assert check.advice, "写不进去时得告诉用户怎么办"


def test_config_dir_that_can_be_written_is_fine(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(preflight, "can_write_dir", lambda directory: True)
    check = preflight._check_config(tmp_path)
    assert check.status == "ok"
    assert str(tmp_path) in check.detail


def test_config_file_missing_is_first_run(tmp_path) -> None:
    check = preflight._check_config_file(tmp_path / "config.json")
    assert check.status == "ok"
    assert "首次运行" in check.detail


def test_config_file_counts_the_settings(tmp_path) -> None:
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"proxy": "", "use_cache": True}), encoding="utf-8")
    check = preflight._check_config_file(path)
    assert check.status == "ok"
    assert "2 项设置" in check.detail


def test_config_file_with_broken_json_fails_with_advice(tmp_path) -> None:
    path = tmp_path / "config.json"
    path.write_text("{不是 json", encoding="utf-8")
    check = preflight._check_config_file(path)
    assert check.status == "fail"
    assert "不是有效的 JSON" in check.detail
    assert "删掉它" in check.advice


def test_config_file_that_is_not_an_object_fails(tmp_path) -> None:
    path = tmp_path / "config.json"
    path.write_text("[1, 2, 3]", encoding="utf-8")
    check = preflight._check_config_file(path)
    assert check.status == "fail"
    assert "不是一个对象" in check.detail


def test_config_file_is_checked_before_it_is_parsed_as_json(tmp_path) -> None:
    """目录当成配置文件传进来时也要给结论，不能抛出来。"""
    check = preflight._check_config_file(tmp_path)
    assert check.status == "fail"


def test_export_dir_not_chosen_yet_is_ok() -> None:
    check = preflight._check_export_dir("")
    assert check.status == "ok"
    assert "还没选" in check.detail


def test_export_dir_that_cannot_be_written_is_a_failure(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(preflight, "can_write_dir", lambda directory: False)
    check = preflight._check_export_dir(str(tmp_path))
    assert check.status == "fail"
    assert check.advice


def test_cache_check_lists_the_candidates(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(preflight, "can_write_dir", lambda directory: True)
    check = preflight._check_cache_dir(str(tmp_path))
    assert check.status == "ok"
    assert "候选" in check.detail


def test_cache_check_is_a_failure_when_nothing_is_writable(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(preflight, "can_write_dir", lambda directory: False)
    check = preflight._check_cache_dir(str(tmp_path))
    assert check.status == "fail"
    assert "没有一个能写" in check.detail
    assert check.advice


def test_cache_check_survives_a_broken_candidate_list(tmp_path, monkeypatch) -> None:
    def boom(preferred=None):
        raise FileNotFoundError("No usable temporary directory found in [...]")

    monkeypatch.setattr(preflight.cache, "cache_dir_candidates", boom)
    check = preflight._check_cache_dir(str(tmp_path))
    assert check.status == "fail"
    assert "FileNotFoundError" in check.detail


def test_browser_check_reports_what_it_found(monkeypatch) -> None:
    monkeypatch.setattr(preflight, "find_browser", lambda explicit, env: _ok_browser())
    check = preflight._check_browser("")
    assert check.status == "ok"
    assert "自动找到" in check.detail


def test_browser_check_marks_a_manual_path(monkeypatch) -> None:
    monkeypatch.setattr(preflight, "find_browser", lambda explicit, env: _ok_browser())
    check = preflight._check_browser("C:/某处/msedge.exe")
    assert "手动指定" in check.detail


def test_browser_check_explains_what_still_works_without_one(monkeypatch) -> None:
    monkeypatch.setattr(preflight, "find_browser", lambda explicit, env: None)
    check = preflight._check_browser("")
    assert check.status == "fail"
    assert "PDF" in check.advice
    assert "EPUB" in check.advice, "要说明哪些格式不受影响"


def test_browser_check_survives_a_lookup_that_raises(monkeypatch) -> None:
    def boom(explicit, env):
        raise OSError("盘符不存在")

    monkeypatch.setattr(preflight, "find_browser", boom)
    check = preflight._check_browser("")
    assert check.status == "fail"
    assert "OSError" in check.detail


def test_exporter_check_lists_every_format() -> None:
    check = preflight._check_exporters()
    assert check.status == "ok"
    for name in ("html", "pdf", "txt", "markdown", "epub"):
        assert name in check.detail


# ---------------------------------------------------------------- 整轮


def test_run_local_checks_uses_the_given_paths_without_loading_settings(
    tmp_path, monkeypatch
) -> None:
    """显式给了路径就不该去读用户配置（``AppSettings.load`` 会碰真实配置）。"""

    def boom():
        raise AssertionError("不该读配置")

    monkeypatch.setattr(preflight.settings_mod.AppSettings, "load", staticmethod(boom))
    monkeypatch.setattr(preflight, "find_browser", lambda explicit, env: _ok_browser())
    monkeypatch.setattr(preflight, "can_write_dir", lambda directory: True)

    report = preflight.run_local_checks(
        config_path=tmp_path / "config.json",
        output_dir=str(tmp_path / "out"),
        cache_dir=str(tmp_path / "cache"),
        browser_path="",
    )
    names = [c.name for c in report.checks]
    assert names == [
        "运行方式",
        "配置目录",
        "配置文件",
        "导出目录",
        "缓存目录",
        "浏览器",
        "导出格式",
    ]
    assert not report.failures


def test_run_local_checks_reports_a_broken_config_file_without_raising(
    tmp_path, monkeypatch
) -> None:
    # 浏览器那一项要盯住：CI 的 Linux 机器上没装 Edge / Chrome，不替换的话
    # 失败清单里会多一条「浏览器」，断言在本机绿、在 CI 红。
    monkeypatch.setattr(preflight, "find_browser", lambda explicit, env: _ok_browser())
    path = tmp_path / "config.json"
    path.write_text("{坏的", encoding="utf-8")
    report = preflight.run_local_checks(
        config_path=path,
        output_dir=str(tmp_path),
        cache_dir=str(tmp_path / "cache"),
        browser_path="",
    )
    assert [c.name for c in report.failures] == ["配置文件"]


def test_run_network_checks_reports_a_reachable_interface(monkeypatch) -> None:
    class FakeClient:
        def update_cdn_path(self):
            return "https://image.example/"

        def fetch_thread_page(self, thread_id, page):
            return {"success": True, "title": "示例串"}

    monkeypatch.setattr("xdao.client.XdaoClient", FakeClient)
    checks = preflight.run_network_checks()
    assert [c.name for c in checks] == ["联网·接口连接", "联网·取串接口"]
    assert all(c.status == "ok" for c in checks)
    assert "image.example" in checks[0].detail


def test_run_network_checks_stops_at_the_interface_when_it_is_unreachable(monkeypatch) -> None:
    from xdao.client import XdaoError

    class FakeClient:
        def update_cdn_path(self):
            raise XdaoError("连接超时")

    monkeypatch.setattr("xdao.client.XdaoClient", FakeClient)
    checks = preflight.run_network_checks()
    assert len(checks) == 1, "连不上接口就别再去取串了"
    assert checks[0].status == "fail"
    assert "连接超时" in checks[0].detail
    assert checks[0].advice


def test_run_network_checks_reports_an_api_level_error(monkeypatch) -> None:
    class FakeClient:
        def update_cdn_path(self):
            return "https://image.example/"

        def fetch_thread_page(self, thread_id, page):
            return {"success": False, "error": "该串不存在"}

    monkeypatch.setattr("xdao.client.XdaoClient", FakeClient)
    checks = preflight.run_network_checks()
    assert checks[1].status == "fail"
    assert "该串不存在" in checks[1].detail


def test_run_network_checks_reports_a_fetch_that_raises(monkeypatch) -> None:
    from xdao.client import XdaoError

    class FakeClient:
        def update_cdn_path(self):
            return "https://image.example/"

        def fetch_thread_page(self, thread_id, page):
            raise XdaoError("网络断了")

    monkeypatch.setattr("xdao.client.XdaoClient", FakeClient)
    checks = preflight.run_network_checks()
    assert checks[1].status == "fail"
    assert "网络断了" in checks[1].detail
