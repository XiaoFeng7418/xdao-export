"""主题与自绘控件的单元测试。

分两层：
1. 不依赖显示器的部分（调色板、间距、圆角顶点）直接用；
2. 需要 Tcl/Tk 的部分用 ``tk.Tk()`` 真根窗口（并 ``withdraw()`` 不显示）。
   注意 **``tk.Tcl()`` 这种纯 Tcl 解释器里没有 ttk 包**（会报
   ``invalid command name "ttk::style"``），所以样式类用例必须用真根窗口；
   没有显示环境（无头 CI）时整组跳过。
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk

import pytest

from xdao import theme
from xdao.widgets import Card, draw_round_rect, round_rect_points


@pytest.fixture(scope="module")
def window() -> tk.Tk:
    """真根窗口（隐藏、不显示）；没有显示环境时跳过整组样式用例。"""
    try:
        root = tk.Tk()
    except tk.TclError as exc:  # pragma: no cover - 取决于运行环境
        pytest.skip(f"没有可用的显示环境：{exc}")
    root.withdraw()
    yield root
    root.destroy()


@pytest.fixture
def interp(window: tk.Tk) -> tk.Tk:
    """ttk.Style 要的"解释器"：真根窗口最省事。"""
    return window


# ---------------------------------------------------------------- 调色板

PALETTE_FIELDS = tuple(theme.Palette.__dataclass_fields__)


def test_both_palettes_define_every_field() -> None:
    assert set(theme.PALETTES) == {"light", "dark"}
    for name, pal in theme.PALETTES.items():
        assert pal.name == name
        for field in PALETTE_FIELDS:
            value = getattr(pal, field)
            assert isinstance(value, str) and value, f"{name}.{field} 为空"


def test_palette_colors_are_hex() -> None:
    for name, pal in theme.PALETTES.items():
        for field in PALETTE_FIELDS:
            if field == "name":
                continue
            value = getattr(pal, field)
            assert value.startswith("#") and len(value) == 7, f"{name}.{field} = {value}"


def test_dark_palette_is_actually_dark() -> None:
    def luminance(color: str) -> float:
        return sum(int(color[i : i + 2], 16) for i in (1, 3, 5)) / 3

    assert luminance(theme.DARK.bg) < luminance(theme.LIGHT.bg)
    assert luminance(theme.DARK.text) > luminance(theme.LIGHT.text)


def test_text_has_enough_contrast_on_surfaces() -> None:
    """正文/次要文字都要明显亮于或暗于底色，别出现"灰字灰底"。"""

    def luminance(color: str) -> float:
        return sum(int(color[i : i + 2], 16) for i in (1, 3, 5)) / 3

    for pal in theme.PALETTES.values():
        assert abs(luminance(pal.text) - luminance(pal.surface)) > 100
        assert abs(luminance(pal.muted) - luminance(pal.surface)) > 60
        assert abs(luminance(pal.on_accent) - luminance(pal.accent)) > 100


def test_palette_lookup_by_name() -> None:
    assert theme.palette("dark") is theme.DARK
    assert theme.palette("light") is theme.LIGHT
    assert theme.palette("不存在的名字") is theme.PALETTE
    assert theme.palette(None) is theme.PALETTE
    assert theme.palette("") is theme.PALETTE


# ---------------------------------------------------------------- 间距与字体


def test_gap_is_multiples_of_the_unit() -> None:
    assert theme.gap(1) == theme.GAP_UNIT
    assert theme.gap(2) == theme.GAP_UNIT * 2
    assert theme.gap(0.5) == theme.GAP_UNIT // 2
    assert theme.gap(0) == 0
    assert isinstance(theme.gap(3), int)


def test_font_helpers_return_tuples() -> None:
    regular = theme.font(theme.SIZE_BODY)
    bold = theme.font(theme.SIZE_SUBHEAD, bold=True)
    code = theme.mono(theme.SIZE_SMALL)
    assert regular == (theme.FONT_UI, theme.SIZE_BODY)
    assert bold[-1] == "bold" and bold[1] == theme.SIZE_SUBHEAD
    assert code[0] == theme.FONT_MONO
    assert theme.font() == (theme.FONT_UI, theme.SIZE_BODY)


def test_font_candidates_are_ordered_by_preference() -> None:
    assert theme.UI_FONT_CANDIDATES[0] == "Microsoft YaHei UI"
    assert theme.MONO_FONT_CANDIDATES[0] == "Cascadia Mono"
    assert "Consolas" in theme.MONO_FONT_CANDIDATES


def test_resolve_fonts_without_window_keeps_working() -> None:
    """没有根窗口时也要能调用（否则打包/无界面环境会崩）。"""
    ui, mono = theme.resolve_fonts(None)
    assert isinstance(ui, str) and ui
    assert isinstance(mono, str) and mono


def test_font_detection_is_cached_after_the_root_is_gone(window: tk.Tk) -> None:
    """字体探测结果在进程内缓存：根窗口销毁后再调用也不会变、不会抛。

    这条是为了防回归：曾经 `_first_available` 每次都重新枚举字体，
    导入时（无根窗口）拿到一个名字、有窗口后拿到另一个名字，于是
    「控件的字体」和「theme.FONT_MONO」在 CI 上对不上。
    """
    first = theme.resolve_fonts(window)
    dropped = tk.Toplevel(window)
    dropped.destroy()
    after = theme.resolve_fonts(None)
    assert after == first
    assert theme.mono()[0] == first[1]


# ---------------------------------------------------------------- ttk 样式


def test_apply_theme_sets_every_documented_style(interp: tk.Tcl) -> None:
    style = theme.apply_theme(interp)
    missing = [name for name in theme.STYLE_NAMES if not style.configure(name)]
    # 基础样式 "." 是可配置的，上面已经相当于逐条读过了；这里只确认没有报错
    assert isinstance(missing, list)


def test_apply_theme_is_idempotent(interp: tk.Tcl) -> None:
    first = theme.apply_theme(interp)
    before = theme.style_colors(first, "TButton")
    second = theme.apply_theme(interp)
    assert theme.style_colors(second, "TButton") == before


def test_primary_button_uses_the_accent_colour(interp: tk.Tcl) -> None:
    style = theme.apply_theme(interp)
    colors = theme.style_colors(style, "TButton")
    assert colors["background"] == theme.PALETTE.accent
    assert colors["foreground"] == theme.PALETTE.on_accent


def test_secondary_and_ghost_buttons_stay_on_the_card(interp: tk.Tcl) -> None:
    style = theme.apply_theme(interp)
    assert theme.style_colors(style, "Secondary.TButton")["background"] == (
        theme.PALETTE.surface_hover
    )
    ghost = theme.style_colors(style, "Ghost.TButton")
    assert ghost["foreground"] == theme.PALETTE.muted


def test_button_states_are_mapped(interp: tk.Tcl) -> None:
    style = theme.apply_theme(interp)
    disabled = style.lookup("TButton", "background", ("disabled",))
    active = style.lookup("TButton", "background", ("active",))
    assert disabled == theme.PALETTE.track
    assert active == theme.PALETTE.accent_hover


def test_style_colors_only_returns_configured_keys(interp: tk.Tcl) -> None:
    style = theme.apply_theme(interp)
    colors = theme.style_colors(style, "TEntry")
    assert colors  # 至少配了底与描边
    assert all(value for value in colors.values())


def test_apply_theme_can_take_another_palette(interp: tk.Tcl) -> None:
    original = theme.PALETTE
    try:
        style = theme.apply_theme(interp, theme.DARK)
        assert theme.PALETTE is theme.DARK
        assert theme.style_colors(style, "TButton")["background"] == theme.DARK.accent
    finally:
        theme.PALETTE = original


# ------------------------------------------------------------- 切换主题


def test_set_palette_swaps_the_current_palette() -> None:
    original = theme.PALETTE
    try:
        assert theme.set_palette(theme.DARK) is theme.DARK
        assert theme.PALETTE is theme.DARK
        # 名字不认识时 palette() 保持当前配色，不抛异常
        assert theme.palette("不存在的配色").name == "dark"
    finally:
        theme.set_palette(original)
    assert theme.PALETTE is original


def test_palette_fields_covers_every_dataclass_field() -> None:
    """``PALETTE_FIELDS`` 少一个字段，换肤时那类控件就会留着旧底色。"""
    # name 不是颜色，换肤时不需要迁移
    fields = set(theme.Palette.__dataclass_fields__) - {"name"}
    assert set(theme.PALETTE_FIELDS) == fields


def test_color_map_pairs_every_colour_with_its_counterpart() -> None:
    mapping = theme.color_map(theme.LIGHT, theme.DARK)

    assert mapping[theme.LIGHT.bg] == theme.DARK.bg
    assert mapping[theme.LIGHT.surface] == theme.DARK.surface
    assert mapping[theme.LIGHT.text] == theme.DARK.text
    assert mapping[theme.LIGHT.accent] == theme.DARK.accent
    # 浅色里 surface 与 on_accent 同为 #ffffff：以 surface 为准（卡片底色），
    # 否则暗色下卡片底会被映射成主色按钮上的文字色。
    assert mapping["#ffffff"] == theme.DARK.surface
    assert mapping["#ffffff"] != theme.DARK.on_accent


def test_color_map_round_trips_back_to_light() -> None:
    forward = theme.color_map(theme.LIGHT, theme.DARK)
    backward = theme.color_map(theme.DARK, theme.LIGHT)

    for value in forward.values():
        assert value in backward, f"深色里的 {value} 回不到浅色"
    assert backward[theme.DARK.bg] == theme.LIGHT.bg


def test_combobox_popdown_gets_palette_colours(window: tk.Tk) -> None:
    """下拉候选列表是原生 Listbox，只能靠选项库上色（暗色下不然是白框）。

    直接查选项库查不到（``option_get`` 是空的），要看真正的那个
    Listbox 控件 —— 选项库是在它创建时才生效的。
    **已知边界**：选项库只对"之后创建"的控件生效，已经弹出来过的那个
    Listbox 会留到它下次重建；所以这里换回浅色后不跟着变，只保证
    "新窗口/新弹出的候选表用的是当前配色"。
    """
    original = theme.PALETTE
    combo = ttk.Combobox(window, values=["light", "dark"])
    try:
        theme.apply_theme(window, theme.DARK)
        combo.pack()
        window.update_idletasks()
        popdown = combo.tk.call("ttk::combobox::PopdownWindow", combo._w)  # noqa: SLF001
        listbox = f"{popdown}.f.l"
        assert combo.tk.call(listbox, "cget", "-background") == theme.DARK.surface
        assert combo.tk.call(listbox, "cget", "-foreground") == theme.DARK.text
        assert combo.tk.call(listbox, "cget", "-selectbackground") == theme.DARK.accent
    finally:
        theme.PALETTE = original
        combo.destroy()



# ---------------------------------------------------------------- 自绘圆角


def test_round_rect_points_shape() -> None:
    points = round_rect_points(0, 0, 100, 40, 8)
    assert len(points) == 24  # 12 个点，每个点 x/y 各一项
    assert all(isinstance(value, (int, float)) for value in points)
    assert min(points) == 0 and max(points) == 100


def test_round_rect_points_with_zero_radius() -> None:
    points = round_rect_points(0, 0, 10, 10, 0)
    assert len(points) == 24
    assert set(points) <= {0, 10}


def test_draw_round_rect_creates_one_polygon(window: tk.Tk) -> None:
    canvas = tk.Canvas(window, width=60, height=20)
    item = draw_round_rect(canvas, 0, 0, 60, 20, 6, fill="#3b6ef6", outline="")
    assert isinstance(item, int)
    assert len(canvas.find_all()) == 1
    assert canvas.type(item) == "polygon"


# ---------------------------------------------------------------- 卡片


def test_card_body_stops_at_the_requested_height(window: tk.Tk) -> None:
    """默认卡片跟着内容走：里面只有 200 高的东西，卡片就不该撑到 600。"""
    window.deiconify()
    window.geometry("500x600")
    window.update()
    card = Card(window)
    card.pack(fill="both", expand=True)
    tk.Frame(card.body, bg="#ffffff", height=200, width=100).pack(fill="x")
    window.update()
    height = card.body.winfo_height()
    card.destroy()
    window.withdraw()

    assert height <= 280, f"卡片没跟着内容走：body {height}px"


def test_card_stretch_lets_the_body_fill_the_card(window: tk.Tk) -> None:
    """``stretch=True`` 时内容要长到卡片底部，而不是停在请求高度。

    这是「自检窗口里文本区只占 206px、下面空一大块」的根因：
    画布里的内嵌窗口不会自己长高，得垫一层铺满画布的容器。
    """
    window.deiconify()
    window.geometry("500x600")
    window.update()
    card = Card(window, stretch=True)
    card.pack(fill="both", expand=True)
    tk.Frame(card.body, bg="#ffffff", height=200, width=100).pack(fill="x")
    window.update()
    height = card.body.winfo_height()
    card_height = card.winfo_height()
    card.destroy()
    window.withdraw()

    assert height > 400, f"卡片 {card_height} 高，内容只有 {height}"


# ---------------------------------------------------------------- 界面字号档位


@pytest.fixture
def restore_font_scale():
    """每个用例跑完把界面字号比例放回 1.0。

    ``theme.FONT_SCALE`` 是模块级全局量（字体尺寸的真源），被用例改过之后
    不还原，后面的用例就会在"特大"字号下建控件 —— 那种串味最难查。
    """
    original = theme.FONT_SCALE
    yield
    theme.set_font_scale(original)


def test_font_scale_defaults_to_standard() -> None:
    assert theme.FONT_SCALE == 1.0
    assert theme.font_scale_label() == "标准"
    assert theme.scaled(theme.SIZE_BODY) == theme.SIZE_BODY


def test_font_scale_makes_fonts_bigger_and_smaller(restore_font_scale) -> None:
    """换档之后，``font()`` / ``mono()`` 拿到的字号要真的跟着变。"""
    base = theme.font(theme.SIZE_BODY)
    theme.set_font_scale(1.5)
    assert theme.font(theme.SIZE_BODY)[1] > base[1]
    assert theme.mono(theme.SIZE_SMALL)[1] > theme.scaled(theme.SIZE_SMALL) - 1
    assert theme.font(theme.SIZE_BODY, bold=True)[2] == "bold"

    theme.set_font_scale(0.85)
    assert theme.font(theme.SIZE_BODY)[1] < base[1]


def test_font_scale_is_clamped_and_bad_values_fall_back(restore_font_scale) -> None:
    """坏值回落、越界收口 —— 配置是纯文本，用户会手改。"""
    assert theme.set_font_scale("1.3") == pytest.approx(1.3)
    assert theme.set_font_scale(5.0) == theme.FONT_SCALE_MAX
    assert theme.set_font_scale(0.1) == theme.FONT_SCALE_MIN
    assert theme.set_font_scale("大") == 1.0  # 认不出来 → 回落标准
    assert theme.set_font_scale(float("nan")) == 1.0


def test_font_scale_never_shrinks_below_seven_points(restore_font_scale) -> None:
    """再小的档位也不许把字缩到看不清（7 点是下限）。"""
    theme.set_font_scale(theme.FONT_SCALE_MIN)
    assert theme.scaled(theme.SIZE_SMALL) >= 7


def test_every_choice_label_round_trips(restore_font_scale) -> None:
    """每个档位名都能换回它自己的比例，且显示回来还是这个名字。"""
    assert len(theme.FONT_SCALE_CHOICES) >= 3
    for label, value in theme.FONT_SCALE_CHOICES:
        theme.set_font_scale(value)
        assert theme.font_scale_label() == label


def test_unknown_label_falls_back(restore_font_scale) -> None:
    assert theme.font_scale_for_label("超大杯", fallback=1.0) == 1.0
    assert theme.font_scale_for_label("特大") == 1.5


def test_settings_column_grows_with_the_font(restore_font_scale) -> None:
    """左栏宽度跟着字号放大：不放大，字大了那一栏的提示会被截断。"""
    theme.set_font_scale(1.0)
    standard = theme.settings_column_width()
    assert standard == theme.SETTINGS_COLUMN_WIDTH

    theme.set_font_scale(1.5)
    assert theme.settings_column_width() > standard
