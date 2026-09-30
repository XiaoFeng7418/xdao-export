"""配置读写的测试：重点是 PDF 纸张 / 边距这 7 个新键。

用户给这条功能的硬要求是「老配置文件必须与 v0.7.0 行为完全一致」，所以这里
的断言分三层：
1. 没有这些键的老配置读进来，得到的值**等于**内置默认值（不是"看起来差不多"）；
2. 由它算出来的 :class:`~xdao.pdf_opts.PdfOptions` 落在「跟随网页样式」那条路上
   （``is_default`` 且 ``needs_cdp`` 为假），也就是 PDF 渲染不套任何纸张参数；
3. 存回去时只是多出这 7 个键，别的键一个字都不许动。

坏值一律回落默认；配置文件整体损坏也不能让程序起不来。

**这一条最要紧**：所有读写都必须落在临时目录。``AppSettings._path`` 的
default_factory 在类创建时就绑进了 ``__init__``，monkeypatch 模块里的
``_default_config_path`` 不起作用 —— 写这份测试时正是踩了这个坑，把用户真实的
``%APPDATA%\\xdao-export\\config.json`` 读写了一遍。现在统一用
:func:`settings_type` 造一个把 ``_path`` 指向临时文件的子类，而
:func:`load_config` / :func:`fresh_config` 里都带一句「路径必须是临时目录」的断言。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from xdao import settings as settings_module
from xdao.pdf_opts import PdfOptions, from_settings
from xdao.settings import (
    DEFAULT_PDF_BACKGROUND,
    DEFAULT_PDF_MARGIN,
    DEFAULT_PDF_MARGIN_MM,
    DEFAULT_PDF_ORIENTATION,
    DEFAULT_PDF_PAGE_RANGES,
    DEFAULT_PDF_PAPER,
    DEFAULT_PDF_SCALE,
    AppSettings,
)

PDF_FIELDS = (
    "pdf_paper",
    "pdf_orientation",
    "pdf_margin",
    "pdf_margin_mm",
    "pdf_scale",
    "pdf_background",
    "pdf_page_ranges",
)

DEFAULTS = {
    "pdf_paper": DEFAULT_PDF_PAPER,
    "pdf_orientation": DEFAULT_PDF_ORIENTATION,
    "pdf_margin": DEFAULT_PDF_MARGIN,
    "pdf_margin_mm": DEFAULT_PDF_MARGIN_MM,
    "pdf_scale": DEFAULT_PDF_SCALE,
    "pdf_background": DEFAULT_PDF_BACKGROUND,
    "pdf_page_ranges": DEFAULT_PDF_PAGE_RANGES,
}

#: v0.7.0 的配置文件长这样：正好是这几个新键出现之前的那一套。
LEGACY_V070 = {
    "userhash": "OLDTESTHASH",
    "output_dir": None,
    "proxy": "",
    "timeout": 20.0,
    "retries": 2,
    "throttle": 0.08,
    "format_key": "pdf",
    "scope": "all",
    "filename_template": "",
    "include_hashes": "",
    "image_mode": "embed",
    "pdf_browser": "",
    "use_cache": True,
    "cache_dir": "",
    "watch_interval": 300.0,
    "verify_cached": False,
    "watch_targets": [],
    "notify": True,
    "notify_interval": 900.0,
    "theme": "light",
    "extra": {},
}


def settings_type(path: Path) -> type[AppSettings]:
    """造一个「配置路径指向 path」的 AppSettings 子类。

    只重新声明 ``_path`` 这一个字段，``load()`` / ``_apply()`` / ``save()`` 跑的还是
    父类那份代码；但落点从用户的 ``%APPDATA%`` 换成了临时目录。
    """

    @dataclass
    class _PathSettings(AppSettings):
        _path: Path = field(default_factory=lambda: path)

    return _PathSettings


def load_config(path: Path) -> AppSettings:
    """读指定配置文件，并确认真的读了它（而不是用户那份）。"""
    settings = settings_type(path).load()
    assert settings.config_path == path, "配置路径跑偏了，可能读到了真实用户配置"
    return settings


def fresh_config(path: Path) -> AppSettings:
    """一个全新的配置对象，落点在 path。"""
    settings = settings_type(path)()
    assert settings.config_path == path, "配置路径跑偏了，可能写到了真实用户配置"
    return settings


def write_config(path: Path, data: dict) -> Path:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


@pytest.fixture
def config_path(artifacts_dir: Path) -> Path:
    return artifacts_dir / "config.json"


@pytest.fixture
def legacy_config(config_path: Path) -> Path:
    """一份老配置：没有那 7 个新键。"""
    return write_config(config_path, LEGACY_V070)


# ---------------------------------------------------------------- 默认值


def test_defaults_are_the_web_page_style(config_path: Path) -> None:
    """全新安装（没有任何配置）时，7 个键都必须是「跟随网页样式」那组值。

    注意用的是 ``fresh_config``（落点在临时目录）而不是裸 ``AppSettings()``：
    后者会被 ``tests/conftest.py`` 的守卫拦下 —— 那条守卫是正确的，见文件开头。
    """
    settings = fresh_config(config_path)
    assert {name: getattr(settings, name) for name in PDF_FIELDS} == DEFAULTS


def test_legacy_config_reads_as_defaults(legacy_config: Path) -> None:
    """老配置里没有这些键 → 每个键都落在默认值上。"""
    loaded = load_config(legacy_config)

    assert loaded.userhash == "OLDTESTHASH"  # 老键照常读出来
    assert {name: getattr(loaded, name) for name in PDF_FIELDS} == DEFAULTS


def test_legacy_config_still_renders_like_v070(legacy_config: Path) -> None:
    """老配置算出来的 PDF 选项必须是「不套纸张参数」那条路。

    这是「行为不变」的实质：``is_default`` 为真、``needs_cdp`` 为假，
    PDF 渲染就不会用 CDP 覆盖纸张/边距，与升级前一模一样。
    """
    options = from_settings(load_config(legacy_config))

    assert options == PdfOptions()
    assert options.is_default is True
    assert options.needs_cdp is False
    # 边距交给浏览器的 @page 规则（跟随网页样式），不是这里另算一套。
    assert options.to_cdp_params()["preferCSSPageSize"] is True


def test_legacy_config_gains_only_the_new_keys(legacy_config: Path) -> None:
    """存回去只是多出这 7 个键：老键与老值一个字都不许变。"""
    load_config(legacy_config).save()

    data = json.loads(legacy_config.read_text(encoding="utf-8"))
    for key, value in LEGACY_V070.items():
        assert data[key] == value, f"老键 {key} 被改动了"
    for name in PDF_FIELDS:
        assert data[name] == DEFAULTS[name]
    assert set(data) - set(LEGACY_V070) == set(PDF_FIELDS)


# ---------------------------------------------------------------- 有效值


def test_values_round_trip(config_path: Path) -> None:
    """界面/命令行写进来的值要能原样存下来、再读回来。"""
    settings = fresh_config(config_path)
    settings.pdf_paper = "a3"
    settings.pdf_orientation = "landscape"
    settings.pdf_margin = "narrow"
    settings.pdf_margin_mm = "22.5"
    settings.pdf_scale = "0.8"
    settings.pdf_background = False
    settings.pdf_page_ranges = "1-3,5"
    settings.save()

    loaded = load_config(config_path)
    assert loaded.pdf_paper == "a3"
    assert loaded.pdf_orientation == "landscape"
    assert loaded.pdf_margin == "narrow"
    assert loaded.pdf_margin_mm == "22.5"
    assert loaded.pdf_scale == "0.8"  # 字符串，不是 0.8000000000000001
    assert loaded.pdf_background is False
    assert loaded.pdf_page_ranges == "1-3,5"

    options = from_settings(loaded)
    assert options.needs_cdp is True
    assert "A3" in options.describe() and "横向" in options.describe()


def test_case_whitespace_and_chinese_booleans(config_path: Path) -> None:
    """大小写、空白与「是/否」都要认（配置是用户手改得动的文本）。"""
    write_config(
        config_path,
        {
            "pdf_paper": " A3 ",
            "pdf_orientation": "Landscape",
            "pdf_margin": "WIDE",
            "pdf_margin_mm": " 18 ",
            "pdf_scale": "1.25",
            "pdf_background": "否",
            "pdf_page_ranges": "1-3, 5",
        },
    )

    loaded = load_config(config_path)
    assert loaded.pdf_paper == "a3"
    assert loaded.pdf_orientation == "landscape"
    assert loaded.pdf_margin == "wide"
    assert loaded.pdf_margin_mm == "18"
    assert loaded.pdf_scale == "1.25"
    assert loaded.pdf_background is False
    assert loaded.pdf_page_ranges == "1-3,5"  # 空白归一化，写法仍是用户那套

    assert settings_module._as_bool("开", False) is True
    assert settings_module._as_bool("off", True) is False


@pytest.mark.parametrize(
    ("name", "bad"),
    (
        ("pdf_paper", "b5"),
        ("pdf_paper", "A4纸"),
        ("pdf_orientation", "斜"),
        ("pdf_margin", "thin"),
        ("pdf_margin_mm", "999"),
        ("pdf_margin_mm", "abc"),
        ("pdf_scale", "5"),  # 越界不能悄悄夹到 2.0，那会让用户以为生效了
        ("pdf_scale", "abc"),
        ("pdf_scale", None),
        ("pdf_background", "maybe"),
        ("pdf_page_ranges", "5-2"),
        ("pdf_page_ranges", "1-"),
        ("pdf_paper", None),
        ("pdf_margin", ""),
    ),
)
def test_bad_values_fall_back_to_defaults(config_path: Path, name: str, bad) -> None:
    """坏值只影响它自己那一项，回落默认；读配置绝不能抛异常。"""
    write_config(config_path, {"pdf_paper": "a3", "pdf_scale": "0.9", name: bad})

    loaded = load_config(config_path)

    assert getattr(loaded, name) == DEFAULTS[name]
    if name != "pdf_paper":
        assert loaded.pdf_paper == "a3"  # 别的项不受牵连
    if name != "pdf_scale":
        assert loaded.pdf_scale == "0.9"


@pytest.mark.parametrize(
    "broken",
    (
        "{ 这不是 JSON",
        "[]",  # 合法 JSON 但不是对象
        "null",
    ),
)
def test_broken_config_file_falls_back_to_defaults(config_path: Path, broken: str) -> None:
    """配置文件损坏时静默回退默认值，程序照常启动。"""
    config_path.write_text(broken, encoding="utf-8")

    loaded = load_config(config_path)
    assert {name: getattr(loaded, name) for name in PDF_FIELDS} == DEFAULTS


# ---------------------------------------------------------------- 路径守卫


def test_default_config_path_is_still_the_user_profile() -> None:
    """新键没有把配置搬到别处去：还是 %APPDATA%\\xdao-export\\config.json。

    这里**不能**造一个裸 ``AppSettings()`` 来问它 ``config_path``：那条路正是
    conftest 守卫要拦的「用例碰了用户真实配置」。所以改问两件等价的事 ——
    模块级的默认路径函数，以及 ``_path`` 的 default_factory 有没有被换掉。
    """
    expected = settings_module.app_config_dir() / "config.json"

    assert settings_module._default_config_path() == expected
    factory = AppSettings.__dataclass_fields__["_path"].default_factory
    assert factory is settings_module._default_config_path


def test_tests_never_touch_the_real_config(config_path: Path) -> None:
    """守卫：本文件所有读写都在临时目录里，别碰用户那份配置。

    这条用例存在的理由：最早的版本用 monkeypatch 改 ``_default_config_path``，
    而 dataclass 早就把 factory 绑进了 ``__init__``，于是测试静默读写了几十公里外
    的真实配置。现在 ``load_config`` / ``fresh_config`` 自带断言，这里再钉一遍。
    """
    real_dir = settings_module.app_config_dir()

    assert real_dir not in config_path.parents
    assert real_dir not in load_config(config_path).config_path.parents
    assert real_dir not in fresh_config(config_path).config_path.parents
