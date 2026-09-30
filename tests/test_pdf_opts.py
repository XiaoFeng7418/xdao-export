"""``xdao.pdf_opts`` 的用例：纸张 / 方向 / 边距 / 缩放 / 页码范围。

全是纯函数，不碰浏览器也不碰界面，所以在任何平台的 CI 上都能跑。契约里有几条
特别容易在后续改动中回归，这里都单独钉住了：

* 非法值**静默回落**（配置脏了不该让导出挂掉），只有 CLI 入口
  :func:`require_valid_pdf_options` 才抛 :class:`PdfOptionsError`；
* 自定义毫米数优先于预设名，否则「自定义 10mm」会被当成「跟随网页样式」丢掉；
* 横向只互换宽高，不发 ``orientation`` 之类的 CDP 参数；
* ``preferCSSPageSize`` **只在纸张走默认时**出现，而且它只管纸张：显式纸张 + 这个键会让
  浏览器改用页面 ``@page size``，用户选的纸张白选（真机实测还会时对时错）；反过来纸张走
  默认却不带它，页面里写的 A4 不被采纳、产物退成 Letter（612×792pt，实测）。
  边距与这个键可以并存 —— 真机实测四边 ``margin*`` 照样生效。
* 全默认时不发 ``paperWidth``/``paperHeight``，但要发 ``preferCSSPageSize: True``。

契约里的尺寸与默认值在本文件里另抄一份（``CONTRACT_*``）：实现被改动时这里必须
同时改，改动才会浮出来。
"""

from __future__ import annotations

import ast
import dataclasses
import inspect
import types

import pytest

import xdao.pdf_opts as pdf_opts
from xdao.pdf_opts import (
    MARGIN_PRESETS,
    MARGIN_PRESETS_MM,
    MM_PER_INCH,
    ORIENTATION_LABELS,
    PAGE_RANGES_RE,
    PAPER_LABELS,
    PAPER_SIZES,
    SCALE_DEFAULT,
    SCALE_MAX,
    SCALE_MIN,
    PdfOptions,
    PdfOptionsError,
    apply_page_ranges,
    clamp_margin_mm,
    from_settings,
    from_values,
    margin_inches,
    normalize_margin,
    normalize_orientation,
    normalize_paper,
    normalize_page_ranges,
    normalize_scale,
    require_valid_pdf_options,
)

# 契约里写死的纸张尺寸（英寸）。
CONTRACT_PAPER_SIZES = {
    "a4": (8.27, 11.69),
    "a3": (11.69, 16.54),
    "a5": (5.83, 8.27),
    "letter": (8.5, 11.0),
    "legal": (8.5, 14.0),
}
CONTRACT_MARGIN_MM = {"none": 0.0, "narrow": 10.0, "normal": 18.0, "wide": 25.0}

# CDP ``Page.printToPDF`` 允许出现的键：多一个都算接线错误。
CDP_KEYS = {
    "paperWidth",
    "paperHeight",
    "marginTop",
    "marginBottom",
    "marginLeft",
    "marginRight",
    "preferCSSPageSize",
    "scale",
    "printBackground",
    "pageRanges",
}

# 常见组合（含全默认），用来扫 to_cdp_params() 的键集合与取值类型。
CDP_COMBOS = [
    PdfOptions(),
    PdfOptions(paper="a3", orientation="landscape"),
    PdfOptions(paper="letter", margin="none"),
    PdfOptions(margin="narrow", scale=0.5),
    PdfOptions(margin="wide", margin_mm=12.5),
    PdfOptions(margin="normal", scale=2.0, background=False),
    PdfOptions(paper="a5", page_ranges="1-3,5", scale=1.25),
    PdfOptions(paper="legal", orientation="landscape", margin="wide", margin_mm=7.5,
               scale=0.75, background=False, page_ranges="2-4"),
]


class FakeSettings:
    """``AppSettings`` 替身：只有传进来的字段（用来验证缺字段时的容错）。"""

    def __init__(self, **fields):
        for name, value in fields.items():
            setattr(self, name, value)


class BrokenSettings:
    """读任何字段都抛异常的替身：``from_settings`` 也不许把它抛出来。"""

    def __getattr__(self, name):
        raise RuntimeError(f"读不到配置字段 {name}")


# ---------------- 常量契约 ----------------


@pytest.mark.parametrize("paper", sorted(CONTRACT_PAPER_SIZES))
def test_paper_sizes_match_contract(paper):
    assert PAPER_SIZES[paper] == CONTRACT_PAPER_SIZES[paper]
    width, height = PAPER_SIZES[paper]
    assert 0 < width < height  # 竖版：宽 < 高，横向靠互换


def test_paper_sizes_hold_exactly_the_five_papers():
    assert set(PAPER_SIZES) == set(CONTRACT_PAPER_SIZES)


def test_paper_labels_cover_default_and_every_paper():
    assert set(PAPER_LABELS) == {"default"} | set(PAPER_SIZES)
    assert all(label for label in PAPER_LABELS.values())
    assert PAPER_LABELS["default"] == "跟随网页样式（推荐）"


def test_margin_presets_match_contract():
    assert MARGIN_PRESETS_MM == CONTRACT_MARGIN_MM
    assert set(MARGIN_PRESETS_MM) <= set(MARGIN_PRESETS)
    assert MARGIN_PRESETS["default"] == "跟随网页样式（推荐）"
    assert MARGIN_PRESETS["none"] == "无边距"
    assert all(label for label in MARGIN_PRESETS.values())


def test_orientation_labels_cover_both_directions():
    assert ORIENTATION_LABELS == {"portrait": "纵向", "landscape": "横向"}


def test_scale_bounds_match_contract():
    assert (SCALE_MIN, SCALE_MAX, SCALE_DEFAULT) == (0.5, 2.0, 1.0)


def test_mm_per_inch_is_the_standard_conversion():
    assert MM_PER_INCH == 25.4


def test_page_ranges_regex_is_the_contract_pattern():
    assert PAGE_RANGES_RE.pattern == r"^[0-9]+(-[0-9]+)?(,[0-9]+(-[0-9]+)?)*$"


@pytest.mark.parametrize("text", ["1", "2-5", "2-5,8", "1-3,5,7-9", "0"])
def test_page_ranges_regex_accepts_numeric_forms(text):
    # 正则只负责「长得像」；页码从 1 开始这类语义由 normalize_page_ranges 把关。
    assert PAGE_RANGES_RE.match(text)


@pytest.mark.parametrize("text", ["", "abc", "-1", "1-", "1,", "1.5", "1;2", " 1"])
def test_page_ranges_regex_rejects_other_forms(text):
    assert PAGE_RANGES_RE.match(text) is None


def test_page_ranges_regex_only_checks_the_shape_not_the_semantics():
    # "5-2" 和 "0" 都「长得像」页码范围：正则放行，倒序与 0 起点由 normalize 层拒掉。
    assert PAGE_RANGES_RE.match("5-2")
    assert normalize_page_ranges("5-2") == ""
    assert PAGE_RANGES_RE.match("0")
    assert normalize_page_ranges("0") == ""


def test_module_is_pure_stdlib_without_gui_imports():
    imported = {
        name for name, value in vars(pdf_opts).items() if isinstance(value, types.ModuleType)
    }
    assert imported == {"math", "re"}
    # 直接扫源码的 import 语句：不用 import dataclasses 也能拿到 dataclass，
    # 所以「模块对象」列表不完整，但「根模块名」列表必须钉死。
    roots = set()
    for node in ast.walk(ast.parse(inspect.getsource(pdf_opts))):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
            roots.add(node.module.split(".")[0])
    assert roots == {"__future__", "math", "re", "dataclasses"}
    assert "tkinter" not in inspect.getsource(pdf_opts)


def test_pdf_options_error_is_a_plain_exception():
    assert issubclass(PdfOptionsError, Exception)


# ---------------- normalize_*：合法值 ----------------


@pytest.mark.parametrize(
    "raw,expected",
    [("a4", "a4"), ("A4", "a4"), (" A4 ", "a4"), ("a3", "a3"), ("letter", "letter"),
     ("Legal", "legal"), ("default", "default"), ("DEFAULT", "default"), ("  default  ", "default")],
)
def test_normalize_paper_accepts_known_values(raw, expected):
    assert normalize_paper(raw) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [("portrait", "portrait"), ("Portrait", "portrait"), (" landscape ", "landscape"),
     ("LANDSCAPE", "landscape"), ("default", "portrait")],
)
def test_normalize_orientation_accepts_known_values(raw, expected):
    assert normalize_orientation(raw) == expected


@pytest.mark.parametrize("raw", ["default", "DEFAULT", " none ", "narrow", "NORMAL", "wide"])
def test_normalize_margin_accepts_preset_names(raw):
    assert normalize_margin(raw) == raw.strip().lower()


@pytest.mark.parametrize(
    "raw,expected",
    [(0.5, 0.5), (1.0, 1.0), (1.25, 1.25), (2.0, 2.0), ("1.5", 1.5), (" 0.75 ", 0.75), (1, 1.0)],
)
def test_normalize_scale_keeps_values_inside_range(raw, expected):
    assert normalize_scale(raw) == pytest.approx(expected)


@pytest.mark.parametrize(
    "raw,expected",
    [("1", "1"), ("1-3", "1-3"), ("2-5,8", "2-5,8"), ("1-3,5,7-9", "1-3,5,7-9"),
     (" 1-3, 5 ", "1-3,5"), ("1-3,\u30005", "1-3,5"), ("  7  ", "7"), ("1 - 3", "1-3")],
)
def test_normalize_page_ranges_keeps_valid_forms(raw, expected):
    assert normalize_page_ranges(raw) == expected


# ---------------- normalize_*：非法值静默回落 ----------------


@pytest.mark.parametrize(
    "raw", ["a2", "a0", "b5", "", "   ", "tabloid", None, 4, 4.0, True, ["a4"], {"paper": "a4"}],
)
def test_normalize_paper_falls_back_to_default(raw):
    assert normalize_paper(raw) == "default"


@pytest.mark.parametrize("raw", ["portriat", "横向", "", "  ", None, 90, ["portrait"]])
def test_normalize_orientation_falls_back_to_portrait(raw):
    assert normalize_orientation(raw) == "portrait"


@pytest.mark.parametrize("raw", ["thin", "18", "18mm", "", None, 18, 0.0, ["wide"]])
def test_normalize_margin_falls_back_to_default(raw):
    # 数字串不算预设名：那是 margin_mm 槽位的事（见 from_values 的用例）。
    assert normalize_margin(raw) == "default"


@pytest.mark.parametrize("raw", ["", "  ", None, "abc", "1.5x", [], {}, float("nan"), "nan"])
def test_normalize_scale_falls_back_to_default(raw):
    assert normalize_scale(raw) == SCALE_DEFAULT


@pytest.mark.parametrize(
    "raw,expected", [(0.1, SCALE_MIN), (0.0, SCALE_MIN), (-3, SCALE_MIN), (9, SCALE_MAX), (100.0, SCALE_MAX)]
)
def test_normalize_scale_clamps_out_of_range(raw, expected):
    assert normalize_scale(raw) == pytest.approx(expected)


@pytest.mark.parametrize("raw,expected", [(float("inf"), SCALE_MAX), (float("-inf"), SCALE_MIN)])
def test_normalize_scale_clamps_infinities(raw, expected):
    assert normalize_scale(raw) == pytest.approx(expected)


@pytest.mark.parametrize(
    "raw",
    ["", "   ", "abc", "-1", "0", "1-", "-", "1--2", "1,", ",1", "1,,2", "5-2", "3-1",
     "1-3;5", "1.5", "1e3", None, 3, ["1"], {"pages": "1"}],
)
def test_normalize_page_ranges_falls_back_to_empty(raw):
    # 空串＝整篇；倒序（5-2/3-1）与「0 页」都当非法，免得 CDP 拿到无效 pageRanges。
    assert normalize_page_ranges(raw) == ""


@pytest.mark.parametrize(
    "raw,expected",
    [(0, 0.0), (0.0, 0.0), (10, 10.0), ("18", 18.0), ("12.5", 12.5), (50, 50.0),
     (50.5, 50.0), (999, 50.0), (-3, 0.0), (-0.1, 0.0)],
)
def test_clamp_margin_mm_keeps_and_clamps_numbers(raw, expected):
    assert clamp_margin_mm(raw) == pytest.approx(expected)


@pytest.mark.parametrize("raw", [None, "", "   ", "abc", "18mm", [], {}, float("nan"), "nan"])
def test_clamp_margin_mm_returns_none_for_non_numbers(raw):
    assert clamp_margin_mm(raw) is None


# ---------------- margin_inches ----------------


def test_margin_inches_default_means_follow_the_page():
    assert margin_inches("default", None) is None
    assert margin_inches("", None) is None
    assert margin_inches("unknown", None) is None


@pytest.mark.parametrize("preset", sorted(CONTRACT_MARGIN_MM))
def test_margin_inches_presets_convert_mm_to_inches(preset):
    assert margin_inches(preset, None) == pytest.approx(CONTRACT_MARGIN_MM[preset] / MM_PER_INCH)


def test_margin_inches_normal_is_0_708661():
    assert margin_inches("normal", None) == pytest.approx(0.708661, abs=1e-6)


def test_margin_inches_custom_mm_wins_over_preset():
    assert margin_inches("wide", 10.0) == pytest.approx(10 / MM_PER_INCH)
    assert margin_inches("default", 10.0) == pytest.approx(10 / MM_PER_INCH)
    assert margin_inches("none", 25.0) == pytest.approx(25 / MM_PER_INCH)


def test_margin_inches_custom_mm_is_clamped():
    assert margin_inches("default", 999) == pytest.approx(50 / MM_PER_INCH)
    assert margin_inches("wide", -5) == pytest.approx(0.0)


def test_margin_inches_invalid_custom_mm_falls_back_to_preset():
    assert margin_inches("narrow", "abc") == pytest.approx(10 / MM_PER_INCH)
    assert margin_inches("narrow", None) == pytest.approx(10 / MM_PER_INCH)


# ---------------- apply_page_ranges ----------------


def test_apply_page_ranges_expands_ranges_and_single_pages():
    assert apply_page_ranges("1-3,5", 10) == {1, 2, 3, 5}
    assert apply_page_ranges("2-5,8", 10) == {2, 3, 4, 5, 8}
    assert apply_page_ranges("7", 10) == {7}
    assert apply_page_ranges("4-4", 10) == {4}


def test_apply_page_ranges_drops_pages_beyond_total():
    assert apply_page_ranges("1-3,9", 3) == {1, 2, 3}
    assert apply_page_ranges("9-12", 5) == set()  # 用户要的页整段都不存在


def test_apply_page_ranges_accepts_a_total_given_as_text():
    assert apply_page_ranges("1-2", "3") == {1, 2}


@pytest.mark.parametrize("pages", ["", "   ", "abc", "5-2", "0", "1-", None, 3, "1;2", "1.5"])
def test_apply_page_ranges_illegal_forms_mean_no_limit(pages):
    # None ＝「不限制」，不要拿空集合去当「用户一页都不要」。
    assert apply_page_ranges(pages, 10) is None


@pytest.mark.parametrize("total", [0, -1, -10, None, "many"])
def test_apply_page_ranges_unknown_page_count_means_no_limit(total):
    assert apply_page_ranges("1-3", total) is None


def test_apply_page_ranges_returns_independent_sets():
    first = apply_page_ranges("1-2", 5)
    first.add(99)
    assert apply_page_ranges("1-2", 5) == {1, 2}


# ---------------- PdfOptions：默认值与不可变性 ----------------


def test_defaults_are_all_follow_the_page():
    opts = PdfOptions()
    assert (
        opts.paper,
        opts.orientation,
        opts.margin,
        opts.margin_mm,
        opts.scale,
        opts.background,
        opts.page_ranges,
    ) == ("default", "portrait", "default", None, 1.0, True, "")
    assert opts.is_default is True
    assert opts.needs_cdp is False


def test_positional_field_order_matches_contract():
    opts = PdfOptions("a3", "landscape", "normal", 12.5, 1.5, False, "1-2")
    assert (
        opts.paper,
        opts.orientation,
        opts.margin,
        opts.margin_mm,
        opts.scale,
        opts.background,
        opts.page_ranges,
    ) == ("a3", "landscape", "normal", 12.5, 1.5, False, "1-2")


@pytest.mark.parametrize(
    "field,value",
    [("paper", "a4"), ("orientation", "landscape"), ("margin", "none"),
     ("margin_mm", 10.0), ("scale", 1.5)],
)
def test_is_default_false_when_layout_changes(field, value):
    opts = PdfOptions(**{field: value})
    assert opts.is_default is False
    assert opts.needs_cdp is True


def test_is_default_ignores_background_and_page_ranges():
    opts = PdfOptions(background=False, page_ranges="1-3")
    assert opts.is_default is True  # 契约：is_default 只看纸张/方向/边距/缩放
    assert opts.needs_cdp is True  # 但这两项旧命令行路径表达不了，接线得走 CDP


def test_needs_cdp_follows_page_ranges():
    assert PdfOptions(page_ranges="1-2").needs_cdp is True
    assert PdfOptions(page_ranges="").needs_cdp is False


def test_options_are_frozen_and_hashable():
    opts = PdfOptions(paper="a4")
    with pytest.raises(dataclasses.FrozenInstanceError):
        opts.paper = "a3"
    assert opts == PdfOptions(paper="A4")
    assert isinstance(hash(opts), int)


def test_options_normalize_constructor_input():
    opts = PdfOptions(
        paper="A4", orientation="Landscape", margin="WIDE", margin_mm=999,
        scale=99, background="no", page_ranges=" 2-3, 5 ",
    )
    assert opts.paper == "a4"
    assert opts.orientation == "landscape"
    assert opts.margin == "wide"
    assert opts.margin_mm == pytest.approx(50.0)
    assert opts.scale == pytest.approx(2.0)
    assert opts.background is False
    assert opts.page_ranges == "2-3,5"


@pytest.mark.parametrize(
    "field,value,expected",
    [("paper", "a2", "default"), ("orientation", "sideways", "portrait"),
     ("margin", "thin", "default"), ("margin_mm", "abc", None), ("margin_mm", -1, 0.0),
     ("scale", float("nan"), 1.0), ("scale", 0.1, 0.5), ("scale", 9, 2.0),
     ("background", "false", False), ("background", "1", True),
     ("page_ranges", "5-2", ""), ("page_ranges", None, "")],
)
def test_constructor_normalizes_each_field(field, value, expected):
    assert getattr(PdfOptions(**{field: value}), field) == expected


# ---------------- to_cdp_params ----------------


def test_all_default_params_prefer_the_page_css():
    params = PdfOptions().to_cdp_params()
    assert params == {"preferCSSPageSize": True, "printBackground": True}
    assert "paperWidth" not in params
    assert "paperHeight" not in params
    assert "scale" not in params


@pytest.mark.parametrize("paper", sorted(CONTRACT_PAPER_SIZES))
def test_portrait_paper_uses_contract_size(paper):
    params = PdfOptions(paper=paper).to_cdp_params()
    width, height = CONTRACT_PAPER_SIZES[paper]
    assert params["paperWidth"] == pytest.approx(width)
    assert params["paperHeight"] == pytest.approx(height)
    assert "preferCSSPageSize" not in params


def test_a3_landscape_swaps_width_and_height():
    params = PdfOptions(paper="a3", orientation="landscape").to_cdp_params()
    assert params["paperWidth"] == pytest.approx(16.54)
    assert params["paperHeight"] == pytest.approx(11.69)
    assert "preferCSSPageSize" not in params


def test_explicit_paper_never_asks_for_the_page_css_size():
    # 契约守卫（真机复现过的 bug）：带 preferCSSPageSize 时浏览器改以页面 @page size 为准，
    # 用户选的 A3 会被静默出成 A4，而且时对时错。显式纸张必须只靠 paperWidth/paperHeight。
    assert "preferCSSPageSize" not in PdfOptions(paper="a3").to_cdp_params()
    assert "preferCSSPageSize" not in PdfOptions(paper="a4", margin="normal").to_cdp_params()
    assert "preferCSSPageSize" not in PdfOptions(paper="letter", margin="none").to_cdp_params()
    assert "preferCSSPageSize" not in PdfOptions(paper="a5", scale=1.5).to_cdp_params()
    # 反过来：纸张走默认时它必须在（这是「沿用网页打印样式」的等价开关）。
    assert PdfOptions().to_cdp_params()["preferCSSPageSize"] is True


def test_margins_keep_the_page_css_flag_because_it_only_governs_paper():
    # 真机实测（无 @page margin 的页面，正文块 275mm > 18mm 边距容量 261mm）：只在纸张
    # 显式时才需要撤掉这个键；边距与它并存时四边 margin* 照样生效、照样翻页，页面
    # 自己的 @page size 也还认。撤掉它反而会让「跟随网页样式」的纸张退成 Letter
    # （612×792pt，实测），所以边距显式时**不要**动这个键。
    for opts in (PdfOptions(margin="normal"), PdfOptions(margin="none"), PdfOptions(margin_mm=10)):
        params = opts.to_cdp_params()
        assert params["preferCSSPageSize"] is True
        assert "marginTop" in params


def test_landscape_never_sends_an_orientation_key():
    for orientation in ("portrait", "landscape"):
        params = PdfOptions(
            paper="a4", orientation=orientation, margin="normal", scale=1.3
        ).to_cdp_params()
        assert "orientation" not in params
        assert "landscape" not in params


@pytest.mark.parametrize("preset", sorted(CONTRACT_MARGIN_MM))
def test_preset_margins_go_to_all_four_sides(preset):
    params = PdfOptions(margin=preset).to_cdp_params()
    # 只改边距时纸张仍跟随网页样式，所以 preferCSSPageSize 还要在（它只管纸张规格）。
    assert params["preferCSSPageSize"] is True
    for key in ("marginTop", "marginBottom", "marginLeft", "marginRight"):
        assert params[key] == pytest.approx(CONTRACT_MARGIN_MM[preset] / MM_PER_INCH)


def test_normal_margin_is_0_708661_on_every_side():
    params = PdfOptions(margin="normal").to_cdp_params()
    assert params["marginTop"] == pytest.approx(0.708661, abs=1e-6)
    assert params["marginBottom"] == pytest.approx(0.708661, abs=1e-6)


def test_custom_mm_margin_wins_over_preset_in_params():
    params = PdfOptions(margin="wide", margin_mm=10).to_cdp_params()
    assert params["marginLeft"] == pytest.approx(0.393701, abs=1e-6)
    assert params["marginRight"] == pytest.approx(10 / MM_PER_INCH)
    assert params["preferCSSPageSize"] is True


def test_zero_margin_is_an_explicit_margin():
    params = PdfOptions(margin="none").to_cdp_params()
    assert params["marginTop"] == 0.0
    assert params["marginBottom"] == 0.0
    # 「无边距」是明确要求，不能被算成「跟随网页样式」而丢掉；
    # 同时纸张仍跟随网页样式，所以 preferCSSPageSize 也还在。
    assert params["marginTop"] == 0.0 and params["preferCSSPageSize"] is True


def test_scale_is_sent_only_when_not_default():
    assert "scale" not in PdfOptions(scale=1.0).to_cdp_params()
    assert PdfOptions(scale=1.5).to_cdp_params()["scale"] == pytest.approx(1.5)
    assert PdfOptions(scale=0.5).to_cdp_params()["scale"] == pytest.approx(0.5)
    assert PdfOptions(scale=2.0).to_cdp_params()["scale"] == pytest.approx(2.0)


@pytest.mark.parametrize("background,expected", [(True, True), (False, False)])
def test_print_background_follows_the_setting(background, expected):
    assert PdfOptions(background=background).to_cdp_params()["printBackground"] is expected


@pytest.mark.parametrize(
    "pages,expected", [("1-3", "1-3"), ("2-5,8", "2-5,8"), ("", "")],
)
def test_page_ranges_are_sent_only_when_set(pages, expected):
    params = PdfOptions(page_ranges=pages).to_cdp_params()
    if expected:
        assert params["pageRanges"] == expected
    else:
        assert "pageRanges" not in params


@pytest.mark.parametrize("opts", CDP_COMBOS, ids=lambda opts: opts.describe())
def test_params_only_contain_known_keys_with_json_safe_values(opts):
    params = opts.to_cdp_params()
    assert set(params) <= CDP_KEYS
    assert all(params), "键名不该为空"
    for value in params.values():
        assert isinstance(value, (bool, float, str))
        assert value is not None


def test_prefer_css_page_size_appears_exactly_for_the_default_paper():
    # 一条规则扫全部组合：preferCSSPageSize ⟺ 纸张走 default。两者同时出现的写法
    # （显式纸张 + 这个键）正是真机上「选了 A3 出 A4」的根因，必须堵死；反过来纸张走
    # 默认时不带它，页面里的 @page size 就不被采纳，产物退成 Letter（实测 612×792pt）。
    for opts in CDP_COMBOS:
        params = opts.to_cdp_params()
        assert ("preferCSSPageSize" in params) is (opts.paper == "default"), opts.describe()
        if opts.paper != "default":
            assert "paperWidth" in params and "paperHeight" in params
        else:
            assert "paperWidth" not in params and "paperHeight" not in params


# ---------------- describe ----------------


def test_describe_all_default_is_follow_the_page():
    assert PdfOptions().describe() == "跟随网页样式"


def test_describe_matches_the_contract_example():
    opts = PdfOptions(paper="a3", orientation="landscape", margin_mm=10, scale=1.2)
    assert opts.describe() == "A3 横向 · 边距 10mm · 缩放 120%"


def test_describe_paper_keeps_the_portrait_default_visible():
    assert PdfOptions(paper="a4").describe() == "A4 纵向"


def test_describe_orientation_alone():
    assert PdfOptions(orientation="landscape").describe() == "横向"


def test_describe_margin_presets():
    assert PdfOptions(margin="none").describe() == "无边距"
    assert PdfOptions(margin="normal").describe() == "边距 普通（18mm）"
    assert PdfOptions(margin="wide").describe() == "边距 宽（25mm）"


def test_describe_custom_mm_margin_beats_the_preset_name():
    assert PdfOptions(margin="wide", margin_mm=12.5).describe() == "边距 12.5mm"


def test_describe_scale_as_percentage():
    assert PdfOptions(scale=0.5).describe() == "缩放 50%"
    assert PdfOptions(scale=1.25).describe() == "缩放 125%"
    assert PdfOptions(scale=2.0).describe() == "缩放 200%"


def test_describe_background_and_page_ranges():
    assert PdfOptions(background=False).describe() == "不打印背景"
    assert PdfOptions(page_ranges="1-3,5").describe() == "页码 1-3,5"


def test_describe_joins_parts_with_a_separator():
    text = PdfOptions(paper="a4", margin="normal", scale=1.5, page_ranges="1-2").describe()
    assert text == "A4 纵向 · 边距 普通（18mm） · 缩放 150% · 页码 1-2"


@pytest.mark.parametrize("paper", sorted(PAPER_SIZES))
@pytest.mark.parametrize("margin", ["none", "narrow", "normal", "wide"])
def test_describe_uses_labels_instead_of_raw_keys(paper, margin):
    text = PdfOptions(paper=paper, orientation="landscape", margin=margin).describe()
    assert text != "跟随网页样式"
    assert paper not in text  # 显示的是 A4 而不是 a4
    assert margin not in text
    assert "None" not in text and "{}" not in text


@pytest.mark.parametrize("opts", CDP_COMBOS, ids=lambda opts: opts.describe())
def test_describe_and_params_tell_the_same_story(opts):
    text = opts.describe()
    params = opts.to_cdp_params()
    if "paperWidth" in params:
        short_name = PAPER_LABELS[opts.paper].partition("（")[0]
        assert short_name in text
    if "pageRanges" in params:
        assert opts.page_ranges in text
    if opts.background is False:
        assert "不打印背景" in text


# ---------------- from_values：配置/界面路径，非法值静默回落 ----------------


def test_from_values_full_combination():
    opts = from_values(
        paper="a3", orientation="landscape", margin="normal", margin_mm=10,
        scale=1.2, background=False, page_ranges="1-3",
    )
    assert (
        opts.paper,
        opts.orientation,
        opts.margin,
        opts.margin_mm,
        opts.scale,
        opts.background,
        opts.page_ranges,
    ) == ("a3", "landscape", "normal", 10.0, 1.2, False, "1-3")


@pytest.mark.parametrize(
    "kwargs",
    [{"paper": "a2"}, {"orientation": "diagonal"}, {"margin": "thin"}, {"margin_mm": "abc"},
     {"scale": "big"}, {"page_ranges": "5-2"}, {"paper": None, "orientation": None, "margin": None},
     {"paper": "", "margin": "", "page_ranges": "", "scale": None, "margin_mm": ""}],
)
def test_from_values_falls_back_silently(kwargs):
    assert from_values(**kwargs) == PdfOptions()  # 非法值等于没填，绝不抛异常


def test_from_values_accepts_mm_shorthand_in_the_margin_slot():
    opts = from_values(margin="18")
    assert opts.margin == "default"
    assert opts.margin_mm == pytest.approx(18.0)


def test_from_values_margin_mm_beats_the_preset_name():
    opts = from_values(margin="wide", margin_mm=10)
    assert opts.margin == "wide"
    assert opts.margin_mm == pytest.approx(10.0)
    assert margin_inches(opts.margin, opts.margin_mm) == pytest.approx(10 / MM_PER_INCH)


def test_from_values_clamps_and_cleans_numeric_input():
    assert from_values(margin_mm=999).margin_mm == pytest.approx(50.0)
    assert from_values(margin_mm=-1).margin_mm == pytest.approx(0.0)
    assert from_values(page_ranges=" 2-3, 5 ").page_ranges == "2-3,5"


def test_from_values_blank_margin_mm_is_unset():
    opts = from_values(margin="narrow", margin_mm="  ")
    assert opts.margin_mm is None
    assert margin_inches(opts.margin, opts.margin_mm) == pytest.approx(10 / MM_PER_INCH)


def test_from_values_signature_matches_contract():
    assert list(inspect.signature(from_values).parameters) == [
        "paper", "orientation", "margin", "margin_mm", "scale", "background", "page_ranges",
    ]


# ---------------- from_settings ----------------


def test_from_settings_reads_all_pdf_fields():
    settings = FakeSettings(
        pdf_paper="a3", pdf_orientation="landscape", pdf_margin="narrow",
        pdf_margin_mm=12.5, pdf_scale=1.5, pdf_background=False, pdf_page_ranges="2-4",
    )
    opts = from_settings(settings)
    assert (
        opts.paper,
        opts.orientation,
        opts.margin,
        opts.margin_mm,
        opts.scale,
        opts.background,
        opts.page_ranges,
    ) == ("a3", "landscape", "narrow", 12.5, 1.5, False, "2-4")


def test_from_settings_survives_a_settings_object_without_pdf_fields():
    assert from_settings(object()) == PdfOptions()
    assert from_settings(FakeSettings()) == PdfOptions()


def test_from_settings_treats_none_fields_as_unset():
    settings = FakeSettings(
        pdf_paper=None, pdf_orientation=None, pdf_margin=None, pdf_margin_mm=None,
        pdf_scale=None, pdf_background=None, pdf_page_ranges=None,
    )
    assert from_settings(settings) == PdfOptions()


def test_from_settings_never_raises_on_a_broken_settings_object():
    assert from_settings(BrokenSettings()) == PdfOptions()


def test_from_settings_sanitizes_dirty_values():
    settings = FakeSettings(
        pdf_paper="a2", pdf_orientation="diagonal", pdf_margin="thin", pdf_margin_mm="abc",
        pdf_scale=9, pdf_background="no", pdf_page_ranges="5-2",
    )
    opts = from_settings(settings)
    assert opts.paper == "default"
    assert opts.orientation == "portrait"
    assert opts.margin == "default"
    assert opts.margin_mm is None
    assert opts.scale == pytest.approx(2.0)
    assert opts.background is False
    assert opts.page_ranges == ""


def test_from_settings_module_alias_and_classmethod_are_the_same():
    assert from_settings.__func__ is PdfOptions.from_settings.__func__
    settings = FakeSettings(pdf_paper="a4", pdf_margin="normal")
    assert from_settings(settings) == PdfOptions.from_settings(settings)


# ---------------- require_valid_pdf_options：CLI 路径，非法值报错 ----------------


def test_require_valid_accepts_all_defaults():
    opts = require_valid_pdf_options()
    assert opts == PdfOptions()
    assert opts.is_default is True


def test_require_valid_accepts_the_default_sentinel_values():
    # 老配置/命令行里「不设置」的表现形式就是 default/空串，这条主流路径不能炸。
    for value in ("default", "DEFAULT", " default ", "", None):
        opts = require_valid_pdf_options(margin=value)
        assert opts.margin == "default" and opts.margin_mm is None
    for value in ("default", "", None):
        assert require_valid_pdf_options(paper=value).paper == "default"
        assert require_valid_pdf_options(orientation=value).orientation == "portrait"
    for value in ("", None, "   "):
        assert require_valid_pdf_options(page_ranges=value).page_ranges == ""


def test_require_valid_treats_default_as_unset_for_every_listy_field():
    # 纸张与边距的取值里本来就有 "default"；方向没有这个取值，但同样按「没要求」处理，
    # 免得三个参数里唯独方向多一条报错规则。页码不是这类字段：它的「没设置」就是空串，
    # 写 "default" 属于填错，必须报错而不是悄悄导出全书。
    assert require_valid_pdf_options(orientation="default").orientation == "portrait"
    with pytest.raises(PdfOptionsError) as err:
        require_valid_pdf_options(page_ranges="default")
    assert "页码" in str(err.value)


@pytest.mark.parametrize("preset", sorted(MARGIN_PRESETS_MM))
def test_require_valid_accepts_every_margin_preset(preset):
    assert require_valid_pdf_options(margin=preset).margin == preset


def test_require_valid_accepts_a_full_set_of_values():
    opts = require_valid_pdf_options(
        paper="A4", orientation="landscape", margin="normal", margin_mm=12.5,
        scale=1.25, page_ranges=" 1-3, 5 ",
    )
    assert opts.paper == "a4"
    assert opts.orientation == "landscape"
    assert opts.margin == "normal"
    assert opts.margin_mm == pytest.approx(12.5)
    assert opts.scale == pytest.approx(1.25)
    assert opts.page_ranges == "1-3,5"


def test_require_valid_accepts_mm_shorthand_and_boundaries():
    assert require_valid_pdf_options(margin="18").margin_mm == pytest.approx(18.0)
    assert require_valid_pdf_options(margin_mm=0).margin_mm == pytest.approx(0.0)
    assert require_valid_pdf_options(margin_mm=50).margin_mm == pytest.approx(50.0)
    assert require_valid_pdf_options(scale=SCALE_MIN).scale == pytest.approx(SCALE_MIN)
    assert require_valid_pdf_options(scale=SCALE_MAX).scale == pytest.approx(SCALE_MAX)


@pytest.mark.parametrize(
    "kwargs,message_part",
    [({"paper": "a2"}, "纸张只支持 default/a4/a3/a5/letter/legal"),
     ({"orientation": "diagonal"}, "方向只支持 portrait/landscape"),
     ({"margin": "thin"}, "边距只支持 default/none/narrow/normal/wide"),
     ({"margin": "60"}, "边距只支持"),
     ({"margin_mm": "abc"}, "边距只支持"),
     ({"margin_mm": 60}, "边距只支持"),
     ({"margin_mm": -1}, "边距只支持"),
     ({"scale": "big"}, "缩放只支持"),
     ({"scale": 0.1}, "缩放只支持"),
     ({"scale": 9}, "缩放只支持"),
     ({"scale": float("nan")}, "缩放只支持"),
     ({"page_ranges": "5-2"}, "页码只能是 1-3,5,7-9 这样的写法"),
     ({"page_ranges": "abc"}, "页码只能是"),
     ({"page_ranges": "0"}, "页码只能是")],
)
def test_require_valid_rejects_with_an_actionable_message(kwargs, message_part):
    with pytest.raises(PdfOptionsError) as info:
        require_valid_pdf_options(**kwargs)
    message = str(info.value)
    assert message_part in message
    assert "收到" in message  # 说清「收到什么」，用户照着改


def test_require_valid_quotes_the_offending_value():
    with pytest.raises(PdfOptionsError) as info:
        require_valid_pdf_options(paper="a2")
    assert "'a2'" in str(info.value)


@pytest.mark.parametrize(
    "kwargs",
    [{"paper": "a2"}, {"margin": "thin"}, {"margin_mm": "x"}, {"margin_mm": 60},
     {"scale": []}, {"scale": 0.1}, {"page_ranges": "1;"}, {"page_ranges": "5-2"}],
)
def test_require_valid_raises_only_pdf_options_error(kwargs):
    # 不能漏出 ValueError/TypeError 这类底层异常：CLI 只捕获 PdfOptionsError。
    with pytest.raises(PdfOptionsError):
        require_valid_pdf_options(**kwargs)


@pytest.mark.parametrize(
    "kwargs",
    [{}, {"paper": "a4", "margin": "normal"}, {"margin_mm": 10, "scale": 1.5},
     {"margin": "18"}, {"page_ranges": "1-2,4"}, {"orientation": "landscape"},
     {"paper": "letter", "margin": "none", "scale": 0.5}],
)
def test_cli_and_config_paths_agree_on_legal_values(kwargs):
    assert require_valid_pdf_options(**kwargs) == from_values(**kwargs)


def test_require_valid_signature_matches_contract():
    assert list(inspect.signature(require_valid_pdf_options).parameters) == [
        "paper", "orientation", "margin", "margin_mm", "scale", "page_ranges",
    ]
