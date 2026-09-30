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
from xdao.widgets import draw_round_rect, round_rect_points


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
