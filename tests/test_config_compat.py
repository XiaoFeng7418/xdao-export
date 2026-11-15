# -*- coding: utf-8 -*-
"""旧配置与旧文件名模板在新版本里的行为（向后兼容的守卫）。

为什么单独一份：``tests/test_settings.py`` 管的是「每个键怎么读、怎么存」，
这一份只管**跨版本**的那几件事——老配置文件里多出来的键、写错的枚举值、
老用户手写的文件名模板、以及监控条目存盘时的字段改名。它们是升级时最容易
悄悄坏掉、又最难被用户说清的地方。

这份测试只锁定**当前行为**，不改产品行为：写"应该怎样"之前，先确认现在
到底怎样，免得把既有行为当成 bug 改掉。已知的几处「宽松」也照实钉住，
将来真要收紧时，改这里的同时就能看到影响面。

所有读写都落在 ``tmp_path``，绝不碰 ``%APPDATA%`` 里那份真配置。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from xdao import settings as settings_module
from xdao.client import XdaoClient
from xdao.exporters import EXPORTERS, HtmlBuilder, create_exporter
from xdao.exporters._shared import Post, ThreadData, render_filename
from xdao.settings import AppSettings
from xdao.watcher import WatchTarget


def settings_type(path: Path) -> type[AppSettings]:
    """造一个把 ``_path`` 指向临时文件的配置类。"""

    @dataclass
    class Scoped(AppSettings):
        _path: Path = field(default_factory=lambda: path)

    return Scoped


def load_config(path: Path) -> AppSettings:
    assert "tmp" in str(path).lower() or str(path).startswith(str(Path.home())), (
        f"配置路径不在临时目录：{path}"
    )
    return settings_type(path).load()


def write_config(path: Path, data: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


def make_thread() -> ThreadData:
    return ThreadData(
        thread_id=50577215,
        title="标题/带<斜>杠",
        po_hash="POHASH",
        posts=[
            Post(
                id=1,
                user_hash="POHASH",
                name="无名氏",
                title="",
                content="第一楼正文",
                now="2026-11-15(Sun)00:00:00",
                img="",
                ext="",
                admin=0,
                page=1,
            )
        ],
    )


# ---------------------------------------------------------------- 配置：多出来的键


def test_unknown_keys_are_dropped_on_save_but_extra_survives(tmp_path: Path) -> None:
    """不认识的键在存盘时被丢掉；只有 ``extra`` 是自由字典。"""
    path = write_config(
        tmp_path / "config.json",
        {"format_key": "html", "user_added_key": 42, "nested": {"a": 1}, "extra": {"keep": 1}},
    )
    config = load_config(path)
    assert config.save() is True

    saved = json.loads(path.read_text(encoding="utf-8"))
    assert "user_added_key" not in saved, "不认识的顶层键会被丢掉（这是现状，不是 bug）"
    assert "nested" not in saved
    assert saved["extra"] == {"keep": 1}, "extra 是唯一被原样保留的自由字典"


def test_unknown_extra_is_not_read_by_anything(tmp_path: Path) -> None:
    """``extra`` 只是存着，程序里没有一处读它（钉住现状，免得误以为它在生效）。"""
    source = (Path(settings_module.__file__)).read_text(encoding="utf-8")
    uses = [line for line in source.splitlines() if ".extra" in line or '"extra"' in line]
    assert len(uses) == 2, f"extra 只应在读写两处出现，实际 {len(uses)} 处：{uses}"


# ---------------------------------------------------------------- 配置：写错的枚举值


@pytest.mark.parametrize("bad", ["123", "docx", "PDF"])
def test_unknown_format_key_still_falls_back_to_html(tmp_path: Path, bad: str) -> None:
    """不认识的导出格式：配置里原样留着，真正导出时静默用 HTML。"""
    path = write_config(tmp_path / "config.json", {"format_key": bad})
    config = load_config(path)
    assert config.format_key == bad, "配置层不做校验，原样保留"

    exporter = create_exporter(config.format_key, XdaoClient())
    assert isinstance(exporter, HtmlBuilder), "不认识的格式键回退到 HTML"


def test_empty_format_key_reads_as_html(tmp_path: Path) -> None:
    """空字符串不是「不认识的格式」，而是「没写」，直接读成 html。"""
    path = write_config(tmp_path / "config.json", {"format_key": ""})
    assert load_config(path).format_key == "html"


def test_format_key_given_as_a_number_becomes_a_string(tmp_path: Path) -> None:
    path = write_config(tmp_path / "config.json", {"format_key": 123})
    config = load_config(path)
    assert config.format_key == "123"
    assert isinstance(config.format_key, str)


def test_a_misspelled_scope_exports_everything(tmp_path: Path) -> None:
    """``scope`` 拼错（比如 ``po_only``）不会报错，效果等于 all。"""
    path = write_config(tmp_path / "config.json", {"scope": "po_only"})
    config = load_config(path)
    assert config.scope == "po_only"

    thread = make_thread()
    assert len(thread.filter_posts(config.scope)) == len(thread.posts), "拼错的范围等于不筛"
    assert len(thread.filter_posts("po")) == 1, "只有写对 po 才真的筛"


@pytest.mark.parametrize(
    "field_name, bad, expected",
    [
        ("image_mode", "magnify", "embed"),
        ("theme_name", "neon", "light"),
        ("pdf_paper", "a99", "default"),
    ],
)
def test_unknown_enum_values_fall_back(
    tmp_path: Path, field_name: str, bad: str, expected: str
) -> None:
    path = write_config(tmp_path / "config.json", {field_name: bad})
    config = load_config(path)
    assert getattr(config, field_name) == expected


def test_numbers_written_as_strings_are_still_numbers(tmp_path: Path) -> None:
    """手改配置文件时把数字写成字符串，也要能读出来。"""
    path = write_config(
        tmp_path / "config.json",
        {"timeout": "30", "retries": "3", "throttle": "0.5", "page_workers": "4"},
    )
    config = load_config(path)
    assert config.timeout == 30.0
    assert config.retries == 3
    assert config.throttle == 0.5
    assert config.page_workers == 4


def test_a_fully_broken_config_still_opens(tmp_path: Path) -> None:
    """整份配置都是错的类型，也要能起来（全部落默认，不抛异常）。"""
    path = write_config(
        tmp_path / "config.json",
        {
            "format_key": 123,
            "scope": None,
            "image_mode": ["embed"],
            "theme": {"dark": True},
            "timeout": "abc",
            "page_workers": "9",
        },
    )
    config = load_config(path)
    assert config.scope == "all"
    assert config.image_mode == "embed"
    assert config.theme_name == "light"
    assert config.timeout == 20.0
    assert config.page_workers == 6, "超范围的值会被夹到上限，而不是回默认"


# ---------------------------------------------------------------- 文件名模板


def test_legacy_filename_template_placeholders_still_work() -> None:
    """v0.7.0 就有的五个占位符，语义一字未变。"""
    thread = make_thread()
    assert render_filename("{id}", thread, "回退") == "50577215"
    assert render_filename("{count}", thread, "回退") == "1"
    assert render_filename("{po}_{count}", thread, "回退") == "POHASH_1"
    assert render_filename("{title}", thread, "回退") == "回退"
    assert render_filename("{id}-{date}", thread, "回退").startswith("50577215-")


@pytest.mark.parametrize("template", ["{}", "{0}", "{unknown}", "{title", "}{title", ""])
def test_broken_templates_fall_back_instead_of_raising(template: str) -> None:
    """模板写错只会回退到默认名，绝不让导出失败。"""
    assert render_filename(template, make_thread(), "回退") == "回退"


def test_template_braces_can_be_escaped_and_modifiers_work() -> None:
    thread = make_thread()
    assert render_filename("{{title}}", thread, "回退") == "{title}", "双花括号是字面量"
    assert render_filename("{title!r}", thread, "回退") == "'回退'", "格式化修饰符照常生效"


# ---------------------------------------------------------------- 监控条目存盘


def test_watch_target_accepts_the_old_hashes_key_name() -> None:
    """老版本写过 ``include_hashes``，新版本两个名字都认。"""
    for key in ("hashes", "include_hashes"):
        target = WatchTarget.from_dict({"url_or_id": "50000001", key: ["AAA", "BBB"]})
        assert target.include_hashes == ["AAA", "BBB"], f"{key} 应当能被读出来"


def test_watch_target_always_writes_the_new_hashes_key_name() -> None:
    target = WatchTarget.from_dict({"url_or_id": "50000001", "include_hashes": ["AAA"]})
    saved = target.to_dict()
    assert saved["hashes"] == ["AAA"]
    assert "include_hashes" not in saved


def test_watch_target_rejects_the_folder_image_mode() -> None:
    """监控不支持「存到同名目录」，老配置里存了也会被打回内嵌。"""
    target = WatchTarget.from_dict({"url_or_id": "50000001", "image_mode": "folder"})
    assert target.image_mode == "embed"


def test_watch_target_survives_a_round_trip_through_json() -> None:
    original = WatchTarget.from_dict(
        {
            "url_or_id": "https://www.nmbxd1.com/t/50000001",
            "scope": "po",
            "format_key": "epub",
            "hashes": ["AAA"],
            "image_mode": "url",
            "max_pages": 12,
        }
    )
    restored = WatchTarget.from_dict(json.loads(json.dumps(original.to_dict())))
    assert restored.url_or_id == original.url_or_id
    assert restored.thread_id == 50000001
    assert restored.scope == "po"
    assert restored.format_key == "epub"
    assert restored.include_hashes == ["AAA"]
    assert restored.image_mode == "url"
    assert restored.max_pages == 12


# ---------------------------------------------------------------- 格式键本身


def test_the_five_format_keys_never_changed() -> None:
    """导出格式键是配置与命令行的公共契约，五个键一个都不能变。"""
    assert set(EXPORTERS) == {"html", "pdf", "txt", "markdown", "epub"}
