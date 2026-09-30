"""监控列表导入 / 导出的用例。

一份能搬移的列表文件，两条硬需求：**导出的东西能原样读回来**、
**坏文件、坏条目都不许把程序带崩**（这是用户手改得动的文件）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from xdao import watch_list
from xdao.watcher import WatchTarget, target_key


def target(**kw) -> WatchTarget:
    base = {"url_or_id": "https://www.nmbxd1.com/t/7001234"}
    base.update(kw)
    return WatchTarget(**base)


@pytest.fixture()
def list_file(tmp_path: Path) -> Path:
    return tmp_path / "监控列表.json"


def write_payload(path: Path, payload) -> Path:
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


# ---------- 导出 ----------


def test_export_then_read_gives_back_the_same_settings(tmp_path, list_file):
    """导出一份读回来，每条监控的设置必须一字不差。"""
    targets = [
        target(),
        target(url_or_id="7009999", scope="po", format_key="txt", image_mode="url"),
        target(
            url_or_id="https://www.nmbxd1.com/t/7012345",
            format_key="pdf",
            include_hashes=["AAA", "BBB"],
        ),
    ]
    watch_list.export_targets(targets, list_file)

    back = watch_list.read_targets(list_file)

    assert [target_key(t) for t in back] == [target_key(t) for t in targets]
    assert back[1].scope == "po"
    assert back[1].image_mode == "url"
    assert back[2].include_hashes == ["AAA", "BBB"]


def test_export_does_not_carry_runtime_state(list_file):
    """跑出来的状态不跟着走。

    ``last_export_path`` 要是搬过去，导入方第一轮会跳过「该导出但还没导出过」
    的那次导出 —— 而缓存目录在另一台机器上根本不存在。
    """
    src = target()
    src.state = "无更新（12 楼）"
    src.last_check = 1234567890.0
    src.last_error = "上次报的错"
    src.last_export_path = r"D:\别的地方\7001234.html"
    src.exports = 7

    watch_list.export_targets([src], list_file)
    payload = json.loads(list_file.read_text(encoding="utf-8"))
    entry = payload["targets"][0]

    assert "state" not in entry
    assert "last_check" not in entry
    assert "last_error" not in entry
    assert "last_export_path" not in entry
    assert "exports" not in entry

    back = watch_list.read_targets(list_file)[0]
    assert back.last_export_path == ""
    assert back.last_check == 0.0
    assert back.exports == 0


def test_export_writes_a_recognisable_header(list_file):
    watch_list.export_targets([target()], list_file)
    payload = json.loads(list_file.read_text(encoding="utf-8"))
    assert payload["kind"] == "watch-list"
    assert payload["schema"] == watch_list.SCHEMA_VERSION
    assert payload["app"] == "xdao-export"
    assert isinstance(payload["exported_at"], str)


def test_export_creates_missing_directories(tmp_path):
    nested = tmp_path / "a" / "b" / "列表.json"
    watch_list.export_targets([target()], nested)
    assert nested.is_file()


def test_export_empty_list_is_still_a_valid_file(list_file):
    watch_list.export_targets([], list_file)
    assert watch_list.read_targets(list_file) == []


def test_export_keeps_chinese_readable(list_file):
    """中文网址按原样写，不转成 \\uXXXX —— 用户要能直接改这份文件。"""
    watch_list.export_targets([target(url_or_id="https://www.nmbxd1.com/t/7001?名字=测试")], list_file)
    assert "名字=测试" in list_file.read_text(encoding="utf-8")


def test_suggested_filename_has_a_date_and_the_right_suffix():
    name = watch_list.suggested_filename(stamp=1759267200.0)  # 2025-10-01 本地时间
    assert name.endswith(watch_list.FILE_SUFFIX)
    assert "2025" in name


# ---------- 导入：文件层面的坏情况 ----------


def test_missing_file_says_so_in_chinese(tmp_path):
    with pytest.raises(watch_list.WatchListError) as excinfo:
        watch_list.read_targets(tmp_path / "不存在.json")
    assert "找不到这个文件" in str(excinfo.value)


def test_not_json_is_reported_with_the_line_number(list_file):
    list_file.write_text("{ 这不是 JSON", encoding="utf-8")
    with pytest.raises(watch_list.WatchListError) as excinfo:
        watch_list.read_targets(list_file)
    assert "不是有效的 JSON" in str(excinfo.value)


def test_json_but_not_an_object(list_file):
    write_payload(list_file, [1, 2, 3])
    with pytest.raises(watch_list.WatchListError) as excinfo:
        watch_list.read_targets(list_file)
    assert "顶层应该是一个 JSON 对象" in str(excinfo.value)


def test_missing_targets_section(list_file):
    write_payload(list_file, {"app": "xdao-export"})
    with pytest.raises(watch_list.WatchListError) as excinfo:
        watch_list.read_targets(list_file)
    assert "没有 targets" in str(excinfo.value)


def test_newer_schema_is_refused_instead_of_guessed(list_file):
    write_payload(list_file, {"schema": watch_list.SCHEMA_VERSION + 1, "targets": []})
    with pytest.raises(watch_list.WatchListError) as excinfo:
        watch_list.read_targets(list_file)
    assert "格式版本" in str(excinfo.value)


def test_schema_may_be_absent_for_handmade_files(list_file):
    """手写的文件常常没有 schema 这一段，能读的就读，不必非要它。"""
    write_payload(list_file, {"targets": [{"url_or_id": "7001234"}]})
    assert watch_list.read_targets(list_file)[0].thread_id == 7001234


def test_oversized_file_is_refused_before_reading(tmp_path, monkeypatch):
    big = tmp_path / "巨大.json"
    big.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(watch_list, "MAX_FILE_BYTES", 1)
    with pytest.raises(watch_list.WatchListError) as excinfo:
        watch_list.read_targets(big)
    assert "太大了" in str(excinfo.value)


def test_non_utf8_file_is_reported(tmp_path):
    broken = tmp_path / "gbk.json"
    # GBK 里的「列」是 0xC1 0xD0 —— 在 UTF-8 里这是非法的首字节组合。
    broken.write_bytes('{"targets": [], "备注": "列表"}'.encode("gbk"))
    with pytest.raises(watch_list.WatchListError) as excinfo:
        watch_list.read_targets(broken)
    assert "UTF-8" in str(excinfo.value)


# ---------- 导入：条目层面的坏情况 ----------


def test_bad_entries_are_skipped_and_good_ones_kept(list_file):
    write_payload(
        list_file,
        {
            "targets": [
                {"url_or_id": "7001234"},
                "这一条不是配置",
                {"url_or_id": "认不出串号"},
                {"url_or_id": "7002000", "format_key": "docx"},
                {"url_or_id": "7003000", "scope": "只要楼主"},
                {"url_or_id": "7004000", "image_mode": "压缩"},
                {"url_or_id": "7005000", "hashes": 42},
                {"url_or_id": "7006000", "scope": "po", "format_key": "epub"},
            ]
        },
    )
    result = watch_list.import_targets([], list_file)

    assert [t.thread_id for t in result.added] == [7001234, 7006000]
    assert result.total == 8
    assert len(result.skipped) == 6
    reasons = " ".join(reason for reason, _ in result.skipped)
    assert "串号" in reasons
    assert "格式认不出：docx" in reasons
    assert "范围认不出" in reasons
    assert "图片处理认不出" in reasons
    assert "hashes" in reasons


def test_skipped_entries_say_which_one_it_was(list_file):
    write_payload(list_file, {"targets": [{"url_or_id": "7009000", "format_key": "docx"}]})
    result = watch_list.import_targets([], list_file)
    assert result.skipped[0][1] == "7009000"


def test_thread_id_is_recomputed_not_trusted(list_file):
    """文件里的 thread_id 与网址不一致时，以网址为准。

    监控键是按串号拼的，信了被改过的串号，一份状态就会挂到另一个串上，
    表现是「明明有新回复却一直报无更新」—— 只看文件看不出来。
    """
    write_payload(
        list_file,
        {"targets": [{"url_or_id": "https://www.nmbxd1.com/t/7001234", "thread_id": 8000000}]},
    )
    result = watch_list.import_targets([], list_file)
    assert result.added[0].thread_id == 7001234


def test_entry_without_url_uses_the_thread_id_alone(list_file):
    """只写了串号也要能读 —— 手写文件时最常见的就是只写一个号。"""
    write_payload(list_file, {"targets": [{"thread_id": 7001234}]})
    result = watch_list.import_targets([], list_file)
    assert result.added[0].thread_id == 7001234
    assert result.added[0].url_or_id == "7001234"


def test_bare_number_as_url_is_kept_as_is(list_file):
    write_payload(list_file, {"targets": [{"url_or_id": "7001234"}]})
    assert watch_list.import_targets([], list_file).added[0].url_or_id == "7001234"


def test_url_that_cannot_be_parsed_but_has_a_thread_id(list_file):
    """网址写得不成样子但串号还在：补一个站点标准网址，别丢这条。"""
    write_payload(list_file, {"targets": [{"url_or_id": "随便写的", "thread_id": 7001234}]})
    result = watch_list.import_targets([], list_file)
    assert result.added[0].url_or_id == "https://www.nmbxd1.com/t/7001234"


def test_hashes_accept_a_comma_separated_string(list_file):
    """手写时常常写成一个字符串；逗号分隔认，整段当一块饼干不认。"""
    write_payload(list_file, {"targets": [{"url_or_id": "7001234", "hashes": "AAA, BBB，CCC"}]})
    result = watch_list.import_targets([], list_file)
    assert result.added[0].include_hashes == ["AAA", "BBB", "CCC"]


def test_old_field_name_include_hashes_is_still_read(list_file):
    write_payload(list_file, {"targets": [{"url_or_id": "7001234", "include_hashes": ["AAA"]}]})
    assert watch_list.import_targets([], list_file).added[0].include_hashes == ["AAA"]


def test_bad_values_fall_back_to_the_same_defaults_as_the_program(list_file):
    write_payload(
        list_file,
        {"targets": [{"url_or_id": "7001234", "scope": "", "format_key": "", "image_mode": ""}]},
    )
    got = watch_list.import_targets([], list_file).added[0]
    assert (got.scope, got.format_key, got.image_mode) == ("all", "html", "embed")


def test_format_and_scope_are_case_insensitive(list_file):
    write_payload(list_file, {"targets": [{"url_or_id": "7001234", "format_key": "PDF", "scope": "PO"}]})
    got = watch_list.import_targets([], list_file).added[0]
    assert (got.format_key, got.scope) == ("pdf", "po")


# ---------- 导入：合并语义 ----------


def test_import_merges_instead_of_replacing(list_file):
    """手上那份列表是用户自己攒的，导入只做「并进来」。"""
    existing = [target(url_or_id="7001111")]
    write_payload(list_file, {"targets": [{"url_or_id": "7002222"}]})
    result = watch_list.import_targets(existing, list_file)
    assert [t.thread_id for t in result.added] == [7002222]


def test_same_thread_with_another_format_is_a_new_entry(list_file):
    """同一个串按不同格式各监控一份是允许的，去重不能只看串号。"""
    existing = [target(url_or_id="7001234", format_key="html")]
    write_payload(list_file, {"targets": [{"url_or_id": "7001234", "format_key": "txt"}]})
    result = watch_list.import_targets(existing, list_file)
    assert len(result.added) == 1
    assert result.skipped == []


def test_exact_duplicate_is_skipped_with_a_reason(list_file):
    existing = [target(url_or_id="7001234", format_key="html", scope="all")]
    write_payload(list_file, {"targets": [{"url_or_id": "7001234", "format_key": "html"}]})
    result = watch_list.import_targets(existing, list_file)
    assert result.added == []
    assert len(result.skipped) == 1
    assert "已经有了" in result.skipped[0][0]


def test_entries_that_match_the_existing_list_are_reported_as_duplicates(list_file):
    """「已经有了」的条目要单独留一份出来。

    「用文件替换现有列表」时，这些条目是**要留下来的**：调用方只拿 ``added``
    去替换的话，它们会因为不属于 ``added`` 而被丢掉，列表直接被清空
    （真机上踩到过：文件里那条现在就在监控，替换之后一条不剩）。
    """
    existing = [target(url_or_id="7001234", format_key="html", scope="all")]
    write_payload(list_file, {"targets": [{"url_or_id": "7001234", "format_key": "html"}]})
    result = watch_list.import_targets(existing, list_file)
    assert [t.thread_id for t in result.duplicates] == [7001234]
    assert result.added == []


def test_duplicates_are_mentioned_in_the_summary(list_file):
    existing = [target(url_or_id="7001234", format_key="html", scope="all")]
    write_payload(list_file, {"targets": [{"url_or_id": "7001234", "format_key": "html"}]})
    result = watch_list.import_targets(existing, list_file)
    assert result.summary == "新增 0 条，原本就有 1 条，跳过 1 条"


def test_duplicates_inside_one_file_are_collapsed(list_file):
    write_payload(
        list_file,
        {"targets": [{"url_or_id": "7001234"}, {"url_or_id": "https://www.nmbxd1.com/t/7001234"}]},
    )
    result = watch_list.import_targets([], list_file)
    assert len(result.added) == 1
    assert len(result.skipped) == 1


def test_empty_file_imports_nothing_and_says_zero(list_file):
    write_payload(list_file, {"targets": []})
    result = watch_list.import_targets([target()], list_file)
    assert result.added == [] and result.skipped == [] and result.total == 0
    assert result.summary == "新增 0 条"


def test_summary_mentions_skips(list_file):
    write_payload(list_file, {"targets": [{"url_or_id": "7001234"}, {"url_or_id": "坏"}]})
    result = watch_list.import_targets([], list_file)
    assert result.summary == "新增 1 条，跳过 1 条"
