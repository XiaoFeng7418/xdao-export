"""清单文件解析（v0.20.0）：命令行 --input 与界面「从文件…」共用这一套规则。"""

from __future__ import annotations

from pathlib import Path

import pytest

from xdao.threadlist import (
    MAX_REPORTED_LINES,
    ThreadListError,
    parse_thread_list,
    read_thread_list,
)


def test_plain_numbers_one_per_line() -> None:
    assert parse_thread_list("50000001\n50000002\n") == ["50000001", "50000002"]


def test_blank_lines_and_comments_are_skipped() -> None:
    text = """
    # 这是注释
    // 这也是注释
    50000001

    50000002
    """
    assert parse_thread_list(text) == ["50000001", "50000002"]


def test_urls_are_accepted_like_numbers() -> None:
    text = "https://www.nmbxd1.com/t/50000001\n50000002\n"
    assert parse_thread_list(text) == ["50000001", "50000002"]


def test_duplicates_keep_the_first_position() -> None:
    text = "50000002\n50000001\n50000002\n"
    assert parse_thread_list(text) == ["50000002", "50000001"]


def test_number_and_its_url_count_as_the_same_thread() -> None:
    text = "50000001\nhttps://www.nmbxd1.com/t/50000001\n"
    assert parse_thread_list(text) == ["50000001"]


def test_unknown_lines_name_the_line_number() -> None:
    with pytest.raises(ThreadListError) as caught:
        parse_thread_list("50000001\n不是串号\n50000002\n")

    message = str(caught.value)
    assert "第 2 行" in message
    assert "不是串号" in message


def test_too_many_unknown_lines_are_summarised() -> None:
    junk = "\n".join(["坏的"] * (MAX_REPORTED_LINES + 3))
    with pytest.raises(ThreadListError) as caught:
        parse_thread_list(junk)

    message = str(caught.value)
    assert message.count("坏的") == MAX_REPORTED_LINES
    assert "还有 3 行" in message


def test_empty_list_is_an_error_not_a_silent_no_op() -> None:
    with pytest.raises(ThreadListError) as caught:
        parse_thread_list("# 只有注释\n\n")

    assert "没有串号" in str(caught.value)


def test_read_thread_list_tolerates_a_bom(tmp_path: Path) -> None:
    target = tmp_path / "清单.txt"
    target.write_text("50000001\n50000002\n", encoding="utf-8-sig")

    assert read_thread_list(target) == ["50000001", "50000002"]


def test_missing_file_says_so(tmp_path: Path) -> None:
    with pytest.raises(ThreadListError) as caught:
        read_thread_list(tmp_path / "没有这个.txt")

    assert "找不到清单文件" in str(caught.value)


def test_directory_instead_of_file_says_so(tmp_path: Path) -> None:
    with pytest.raises(ThreadListError) as caught:
        read_thread_list(tmp_path)

    assert "文件夹" in str(caught.value)


def test_non_utf8_file_gets_actionable_advice(tmp_path: Path) -> None:
    target = tmp_path / "gbk.txt"
    # 纯数字转成 GBK 字节和 UTF-8 一模一样，要用中文才真能触发解码失败。
    target.write_bytes("# 第一行是注释\n50000001\n".encode("gbk"))

    with pytest.raises(ThreadListError) as caught:
        read_thread_list(target)

    assert "UTF-8" in str(caught.value)
    assert "另存为" in str(caught.value)
