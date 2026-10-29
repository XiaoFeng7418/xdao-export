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
    DEFAULT_EXPORT_FONT_SIZE,
    DEFAULT_MAX_PAGES,
    DEFAULT_PDF_BACKGROUND,
    DEFAULT_PDF_MARGIN,
    DEFAULT_PDF_MARGIN_MM,
    DEFAULT_PDF_ORIENTATION,
    DEFAULT_PDF_PAGE_RANGES,
    DEFAULT_PDF_PAPER,
    DEFAULT_PDF_SCALE,
    DEFAULT_UI_FONT_SCALE,
    DEFAULT_UNTIL_THREAD_ID,
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

#: 「导出范围」那两个键：页数上限（0＝不限）与串号截止（空串＝不截止）。
RANGE_FIELDS = ("max_pages", "until_thread_id")

RANGE_DEFAULTS = {
    "max_pages": DEFAULT_MAX_PAGES,
    "until_thread_id": DEFAULT_UNTIL_THREAD_ID,
}

#: 「字号」那两个键：导出成品的正文字号（px）与界面字号比例。
FONT_FIELDS = ("export_font_size", "ui_font_scale")

FONT_DEFAULTS = {
    "export_font_size": DEFAULT_EXPORT_FONT_SIZE,
    "ui_font_scale": DEFAULT_UI_FONT_SCALE,
}

#: 「批量断点续跑」那一个键：成品已存在就跳过。
SKIP_FIELDS = ("skip_existing",)

#: v0.21.0 合并导出（默认关：一个串一份文件）
MERGE_FIELDS = ("merge_export",)

SKIP_DEFAULTS = {"skip_existing": False}
MERGE_DEFAULTS = {"merge_export": False}

#: v0.21.21 并行抓页（默认 1 ＝一页一页抓，与旧版本逐字节一致）
PAGE_FIELDS = ("page_workers",)

PAGE_DEFAULTS = {"page_workers": 1}

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
    """存回去只是多出这些新键：老键与老值一个字都不许变。

    「新键」= PDF 那 7 个 + 导出范围的 2 个（页数上限、串号截止）+ 字号的 2 个。
    """
    load_config(legacy_config).save()

    data = json.loads(legacy_config.read_text(encoding="utf-8"))
    for key, value in LEGACY_V070.items():
        assert data[key] == value, f"老键 {key} 被改动了"
    for name in PDF_FIELDS:
        assert data[name] == DEFAULTS[name]
    for name in RANGE_FIELDS:
        assert data[name] == RANGE_DEFAULTS[name]
    for name in FONT_FIELDS:
        assert data[name] == FONT_DEFAULTS[name]
    for name in SKIP_FIELDS:
        assert data[name] == SKIP_DEFAULTS[name]
    for name in MERGE_FIELDS:
        assert data[name] == MERGE_DEFAULTS[name]
    for name in PAGE_FIELDS:
        assert data[name] == PAGE_DEFAULTS[name]
    assert (
        set(data) - set(LEGACY_V070)
        == set(PDF_FIELDS)
        | set(RANGE_FIELDS)
        | set(FONT_FIELDS)
        | set(SKIP_FIELDS)
        | set(MERGE_FIELDS)
        | set(PAGE_FIELDS)
    )


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
    assert {name: getattr(loaded, name) for name in RANGE_FIELDS} == RANGE_DEFAULTS


# ---------------------------------------------------------------- 导出范围
# 页数上限与串号截止：0 / 空串都是**合法值**（分别表示"不限"与"不截止"），
# 所以坏值的判据与 PDF 那几项不同 —— 这里专门钉住这条边界。


def test_export_range_defaults_are_unlimited(config_path: Path) -> None:
    """全新配置：页数不限（0）、串号不截止（空串）。"""
    settings = fresh_config(config_path)
    assert {name: getattr(settings, name) for name in RANGE_FIELDS} == RANGE_DEFAULTS
    assert settings.max_pages == 0
    assert settings.until_thread_id == ""


def test_export_range_round_trip(config_path: Path) -> None:
    """两个新字段要能原样存下来、再读回来。"""
    settings = fresh_config(config_path)
    settings.max_pages = 12
    # 网址原样存（界面上回显的是用户写的那一串），解析另有 parse_thread_id 一套。
    settings.until_thread_id = "https://www.nmbxd1.com/t/50000001"
    settings.save()

    loaded = load_config(config_path)
    assert loaded.max_pages == 12
    assert loaded.until_thread_id == "https://www.nmbxd1.com/t/50000001"


def test_export_range_zero_and_empty_survive_a_round_trip(config_path: Path) -> None:
    """0 与空串是"不限/不截止"的正规写法，不能被偷偷换成别的值。"""
    settings = fresh_config(config_path)
    settings.max_pages = 0
    settings.until_thread_id = ""
    settings.save()

    loaded = load_config(config_path)
    assert loaded.max_pages == 0
    assert loaded.until_thread_id == ""


@pytest.mark.parametrize(
    "bad",
    (None, "", "abc", -1, "3.5", [], {}),
)
def test_bad_max_pages_falls_back_to_unlimited(config_path: Path, bad: object) -> None:
    """坏值只影响页数上限这一项，且回落到"不限"（0）；读配置绝不抛异常。"""
    write_config(config_path, {"max_pages": bad, "until_thread_id": "50000001"})

    loaded = load_config(config_path)

    assert loaded.max_pages == DEFAULT_MAX_PAGES
    assert loaded.until_thread_id == "50000001"  # 另一项不受牵连


@pytest.mark.parametrize(
    "bad",
    (None, "", "不是网址", "abc123xyz", 0, [], {}),
)
def test_bad_until_thread_id_falls_back_to_no_cutoff(config_path: Path, bad: object) -> None:
    """解析不出串号的截止值一律当"没填"；不影响页数上限。"""
    write_config(config_path, {"max_pages": 7, "until_thread_id": bad})

    loaded = load_config(config_path)

    assert loaded.until_thread_id == DEFAULT_UNTIL_THREAD_ID
    assert loaded.max_pages == 7  # 另一项不受牵连


@pytest.mark.parametrize(
    "good,expected",
    (
        (50000001, "50000001"),  # 纯数字（JSON 里也可能写成数字）
        (" 50000001 ", "50000001"),  # 手改配置时带空白
        ("https://www.nmbxd1.com/t/50000001", "https://www.nmbxd1.com/t/50000001"),
    ),
)
def test_until_thread_id_keeps_anything_parseable(
    config_path: Path, good: object, expected: str
) -> None:
    """能解析出串号就原样保留原文（数字写进来则存成字符串）。"""
    write_config(config_path, {"until_thread_id": good})

    assert load_config(config_path).until_thread_id == expected


# ---------------------------------------------------------------- 字号
# 字号是 v0.15.0 新开出来的：同一份文档，有人看得清 10 点正文，有人得 15 点才顺眼。
# 这里钉住两个键的默认值与坏值回落 —— 尤其是默认导出字号必须
# 还是 14，否则老用户升级后成品的字号会自己变。


def test_font_defaults_keep_the_old_look(config_path: Path) -> None:
    """全新配置：导出正文 14px（＝写死值）、界面比例 1.0（＝原尺寸）。"""
    settings = fresh_config(config_path)
    assert {name: getattr(settings, name) for name in FONT_FIELDS} == FONT_DEFAULTS
    assert settings.export_font_size == 14
    assert settings.ui_font_scale == 1.0


def test_font_round_trip(config_path: Path) -> None:
    settings = fresh_config(config_path)
    settings.export_font_size = 20
    settings.ui_font_scale = 1.3
    settings.save()

    loaded = load_config(config_path)
    assert loaded.export_font_size == 20
    assert loaded.ui_font_scale == pytest.approx(1.3)


@pytest.mark.parametrize(
    "bad",
    (None, "", "abc", 0, -3, 9, 29, 1e9, [], {}),
)
def test_bad_export_font_size_falls_back_to_fourteen(config_path: Path, bad: object) -> None:
    """坏值（包括越界的 9 / 29）回落 14；另一个字号键不受牵连。"""
    write_config(config_path, {"export_font_size": bad, "ui_font_scale": 1.15})

    loaded = load_config(config_path)

    assert loaded.export_font_size == DEFAULT_EXPORT_FONT_SIZE
    assert loaded.ui_font_scale == pytest.approx(1.15)


@pytest.mark.parametrize(
    "raw,expected",
    (
        (1.15, 1.15),
        ("1.3", 1.3),
        (5.0, 2.0),  # 越界往回收，而不是丢掉用户的选择
        (0.1, 0.7),
    ),
)
def test_ui_font_scale_is_clamped(config_path: Path, raw: object, expected: float) -> None:
    """比例合法但越界时收进 [0.7, 2.0]；这比"回落到标准"更贴近用户的意思。"""
    write_config(config_path, {"ui_font_scale": raw})

    assert load_config(config_path).ui_font_scale == pytest.approx(expected)


@pytest.mark.parametrize("bad", (None, "", "特大", [], {}))
def test_bad_ui_font_scale_falls_back_to_standard(config_path: Path, bad: object) -> None:
    write_config(config_path, {"ui_font_scale": bad, "export_font_size": 18})

    loaded = load_config(config_path)

    assert loaded.ui_font_scale == DEFAULT_UI_FONT_SCALE
    assert loaded.export_font_size == 18


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

def test_save_reports_whether_the_file_could_be_written(tmp_path: Path) -> None:
    """``save()`` 要把「写没写成」交出去（v0.13.53）。

    以前它把异常吞掉、什么都不返回，界面便无论成败都报「登录成功，饼干已保存。」——
    2026-10-11 真机上就出现了「日志说保存成功、``config.json`` 的修改时间停在两周前、
    重启程序还要重新登录」这种自相矛盾。写不进去是环境问题，但不许不出声。

    这里用「父路径是一个文件」造一个必然写不进去的落点：跨平台、不依赖权限设置。
    """
    ok_path = tmp_path / "ok" / "config.json"
    assert AppSettings(_path=ok_path).save() is True
    assert ok_path.exists()

    blocker = tmp_path / "blocker"
    blocker.write_text("我不是目录", encoding="utf-8")
    assert AppSettings(_path=blocker / "config.json").save() is False


# ---------------------------------------------------------------- 批量断点续跑


def test_skip_existing_is_off_by_default(config_path: Path) -> None:
    """默认关闭：老用户升级后行为不变，照旧每次都重新导出。"""
    assert load_config(config_path).skip_existing is False


def test_skip_existing_round_trip(config_path: Path) -> None:
    settings = fresh_config(config_path)
    settings.skip_existing = True
    settings.save()

    assert load_config(config_path).skip_existing is True


@pytest.mark.parametrize("bad", (None, "", "maybe", "随便", [], {}))
def test_bad_skip_existing_falls_back_to_off(config_path: Path, bad: object) -> None:
    """认不出的值一律当关闭 —— 宁可多导一遍，也别悄悄少导出文件。"""
    write_config(config_path, {"skip_existing": bad})
    assert load_config(config_path).skip_existing is False


@pytest.mark.parametrize("yes", (True, 1, "1", "true", "yes", "on", "是", "开"))
def test_truthy_skip_existing_reads_as_on(config_path: Path, yes: object) -> None:
    """手改配置写成 "1" / "开" 也算开 —— 和 pdf_background 那套宽容解析一致。"""
    write_config(config_path, {"skip_existing": yes})
    assert load_config(config_path).skip_existing is True

# ------------------------------------------------------ 图片模式「存到同名目录」

def test_image_mode_accepts_the_folder_mode(config_path: Path) -> None:
    write_config(config_path, {"image_mode": "folder"})
    assert load_config(config_path).image_mode == "folder"


def test_image_mode_round_trips_the_folder_mode(config_path: Path) -> None:
    settings = fresh_config(config_path)
    settings.image_mode = "folder"
    settings.save()
    assert load_config(config_path).image_mode == "folder"


# ---------- 合并导出（v0.21.0） ----------


def test_merge_export_is_off_by_default(config_path: Path) -> None:
    """默认关闭：不开就是旧行为（一个串一份文件），老用户升级后不会被改变。"""
    assert load_config(config_path).merge_export is False


def test_merge_export_round_trip(config_path: Path) -> None:
    settings = load_config(config_path)
    settings.merge_export = True
    assert settings.save()
    assert load_config(config_path).merge_export is True


@pytest.mark.parametrize("bad", (None, "", "maybe", 2, [], {}, 0.5))
def test_bad_merge_export_falls_back_to_off(config_path: Path, bad: object) -> None:
    write_config(config_path, {"merge_export": bad})
    assert load_config(config_path).merge_export is False


@pytest.mark.parametrize("yes", (True, "true", 1, "on", "是", "开"))
def test_truthy_merge_export_reads_as_on(config_path: Path, yes: object) -> None:
    from xdao import settings as settings_module

    write_config(config_path, {"merge_export": yes})
    assert load_config(config_path).merge_export == settings_module._as_bool(yes, False)
# ---------- 并行抓页（v0.21.21） ----------


def test_page_workers_defaults_to_one(config_path: Path) -> None:
    """默认 1：等于旧版本「一页一页抓」，升级不会顺手把并发打开。"""
    assert load_config(config_path).page_workers == 1


def test_page_workers_round_trip(config_path: Path) -> None:
    settings = fresh_config(config_path)
    settings.page_workers = 4
    assert settings.save()
    assert load_config(config_path).page_workers == 4


@pytest.mark.parametrize("value,expected", ((1, 1), (3, 3), (6, 6), (0, 1), (-2, 1), (99, 6)))
def test_page_workers_is_clamped(config_path: Path, value: int, expected: int) -> None:
    """越界的数夹回 1~6：上限对站点礼貌，下限是「不能变成 0 路」。"""
    write_config(config_path, {"page_workers": value})
    assert load_config(config_path).page_workers == expected


@pytest.mark.parametrize("bad", (None, "", "abc", 1.5, [], {}))
def test_bad_page_workers_falls_back_to_one(config_path: Path, bad: object) -> None:
    write_config(config_path, {"page_workers": bad})
    assert load_config(config_path).page_workers == 1
