"""自绘控件的单元测试。

为什么要单独测这一层：这些控件是 Canvas 手绘的，不靠 ttk 布局，几何计算全在自己手里
（宽度按内容反推、高度由内容同步、圆角半径夹取、进度条填充比例……）。它们的 bug 表现
通常不报错，只是"画歪了/被裁掉/永远停在 0%"——只能靠断言几何量抓住。

需要 Tcl/Tk 的用例统一用真根窗口（``withdraw()`` 不显示）；没有显示环境（无头 CI）
时整组跳过，与 ``test_theme.py`` / ``test_window.py`` 同一套做法。
"""

from __future__ import annotations

import tkinter as tk

import pytest

from xdao import theme
from xdao.widgets import (
    Card,
    FlatText,
    LinkLabel,
    ModernProgress,
    SectionHeading,
    StatusPill,
    draw_round_rect,
    round_rect_points,
)


@pytest.fixture(scope="module")
def window() -> tk.Tk:
    """真根窗口（隐藏）；没有显示环境时跳过整组。"""
    try:
        root = tk.Tk()
    except tk.TclError as exc:  # pragma: no cover - 取决于运行环境
        pytest.skip(f"没有可用的显示环境：{exc}")
    root.withdraw()
    yield root
    root.destroy()


@pytest.fixture(scope="module")
def host(window: tk.Tk) -> tk.Frame:
    """一个固定尺寸的容器：让控件拿到确定的 ``winfo_width/height``。"""
    frame = tk.Frame(window, width=420, height=260)
    frame.pack_propagate(False)
    frame.pack()
    window.update_idletasks()
    return frame


def _place(
    widget: tk.Widget, width: int = 320, height: int = 120
) -> tk.Widget:
    """把控件摆进固定尺寸的坑里，然后等几何生效。"""
    widget.place(x=0, y=0, width=width, height=height)
    widget.winfo_toplevel().update()
    return widget


# ---------------------------------------------------------------- 圆角矩形


def test_round_rect_points_keeps_the_corner_radius_inside_the_rect() -> None:
    """半径不能超过短边的一半，否则勾出来的多边形会自交。"""
    points = round_rect_points(0, 0, 100, 20, 999)

    assert points[:2] == [10, 0]  # 半径被夹到 20/2
    assert (100 - 10, 0) == tuple(points[2:4])


def test_round_rect_points_never_goes_negative() -> None:
    points = round_rect_points(0, 0, 50, 50, -8)

    assert points[:2] == [0, 0]
    assert points[2:4] == [50, 0]


def test_round_rect_points_has_twelve_vertices_and_closes_on_itself() -> None:
    points = round_rect_points(10, 20, 210, 120, 12)

    assert len(points) == 24
    assert points[-2:] == [10, 20]  # 最后一段回到左上角


def test_draw_round_rect_returns_the_polygon_item_id(host: tk.Frame) -> None:
    canvas = tk.Canvas(host)
    _place(canvas)

    item = draw_round_rect(canvas, 1, 1, 100, 40, 8, fill="red", outline="")

    assert item == canvas.find_withtag("current") or canvas.type(item) == "polygon"


# ---------------------------------------------------------------- 卡片


def test_card_body_uses_the_default_padding(host: tk.Frame) -> None:
    card = _place(Card(host))

    assert int(card.body.cget("padx")) == theme.gap(4)
    assert int(card.body.cget("pady")) == theme.gap(4)


def test_card_honours_explicit_padding_and_radius(host: tk.Frame) -> None:
    card = _place(Card(host, padding=3, radius=4))

    assert int(card.body.cget("padx")) == 3
    assert card._radius == 4


def test_card_draws_its_background_and_keeps_it_behind_the_content(host: tk.Frame) -> None:
    card = _place(Card(host), width=200, height=90)
    card._on_canvas_configure()

    assert card._canvas.find_withtag("bg")


def test_card_accent_draws_an_extra_short_bar_at_the_bottom(host: tk.Frame) -> None:
    plain = _place(Card(host), width=200, height=90)
    accent = _place(Card(host, accent=True), width=200, height=90)
    plain._on_canvas_configure()
    accent._on_canvas_configure()

    assert len(accent._canvas.find_withtag("bg")) == len(plain._canvas.find_withtag("bg")) + 1


def test_card_without_accent_has_no_accent_bar(host: tk.Frame) -> None:
    card = _place(Card(host), width=200, height=90)
    card._on_canvas_configure()

    assert not card._accent
    assert len(card._canvas.find_withtag("bg")) == 1


def test_card_redraw_does_nothing_when_the_canvas_is_still_tiny(host: tk.Frame) -> None:
    """构造过程中画布可能还是 1x1，这时不能画（否则会画出负坐标的框）。"""
    card = Card(host)

    card._redraw()

    assert not card._canvas.find_withtag("bg")


def test_card_stretch_puts_the_body_inside_a_host_frame(host: tk.Frame) -> None:
    card = _place(Card(host, stretch=True), width=300, height=160)

    assert card._host is not None
    assert card.body.master is card._host
    assert card._host.master is card._canvas


def test_card_stretch_syncs_both_width_and_height_of_the_window(host: tk.Frame) -> None:
    card = _place(Card(host, stretch=True), width=300, height=160)
    card._canvas.configure(width=300, height=160)
    card._canvas.winfo_toplevel().update()

    card._on_canvas_configure()

    assert int(card._canvas.itemcget(card._window, "width")) == 300
    assert int(card._canvas.itemcget(card._window, "height")) == 160


def test_card_follows_the_width_of_its_canvas(host: tk.Frame) -> None:
    card = _place(Card(host), width=260, height=120)

    card._on_canvas_configure()

    assert int(card._canvas.itemcget(card._window, "width")) == 260


def test_card_body_height_becomes_the_canvas_height(host: tk.Frame) -> None:
    card = _place(Card(host), width=200, height=120)
    tk.Label(card.body, text="四行\n内容\n在\n这里").pack()
    card.body.update_idletasks()

    card._on_body_configure()

    assert int(card._canvas.cget("height")) == card.body.winfo_reqheight()


def test_card_widens_itself_for_wrapped_text(host: tk.Frame) -> None:
    """带 wraplength 的文本请求宽度只有一行宽：容器得按 wraplength 反推宽度。"""
    card = _place(Card(host), width=120, height=120)
    tk.Label(card.body, text="很长的一段说明文字", wraplength=300).pack()
    card.body.update_idletasks()

    card._on_body_configure()

    assert card._canvas.winfo_reqwidth() >= 300


def test_card_caches_the_widest_wraplength_it_found(host: tk.Frame) -> None:
    card = Card(host)
    tk.Label(card.body, text="a", wraplength=120).pack()
    tk.Label(card.body, text="b", wraplength=260).pack()

    assert card._max_wraplength() == 260


def test_card_ignores_children_that_have_no_wraplength(host: tk.Frame) -> None:
    card = Card(host)
    tk.Label(card.body, text="没有换行宽度").pack()

    assert card._max_wraplength() == 0


def test_card_walk_visits_nested_children(host: tk.Frame) -> None:
    card = Card(host)
    outer = tk.Frame(card.body)
    outer.pack()
    inner = tk.Label(outer, text="套了两层")
    inner.pack()

    seen = list(card._walk(card.body))

    assert outer in seen
    assert inner in seen


def test_card_body_configure_is_ignored_while_it_is_already_syncing(host: tk.Frame) -> None:
    """``_syncing`` 防的是"设尺寸 → 触发 Configure → 再设尺寸"的死循环。"""
    card = _place(Card(host), width=200, height=120)
    card._canvas.configure(height=55)
    card._syncing = True

    card._on_body_configure()

    assert int(card._canvas.cget("height")) == 55


# ---------------------------------------------------------------- 状态胶囊


def _pill(window: tk.Tk, **kwargs) -> StatusPill:
    pill = StatusPill(window, **kwargs)
    _place(pill, width=120, height=24)
    return pill


def test_status_pill_widens_itself_for_its_text(window: tk.Tk) -> None:
    short = _pill(window, text="好")
    longer = _pill(window, text="正在导出全部串")

    assert int(longer.cget("width")) > int(short.cget("width"))


def test_status_pill_without_text_keeps_its_requested_width(window: tk.Tk) -> None:
    pill = _pill(window, text="", width=40)

    assert int(pill.cget("width")) == 40


def test_status_pill_set_swaps_the_text_and_the_tone(window: tk.Tk) -> None:
    pill = _pill(window, text="等待")

    pill.set("出错了", "danger")

    assert pill._text == "出错了"
    assert pill._tone == "danger"
    assert pill._tone_colors() == (theme.PALETTE.accent_soft, theme.PALETTE.danger)


@pytest.mark.parametrize("tone", ["ok", "warn", "danger", "accent", "muted"])
def test_every_known_tone_has_its_own_foreground(window: tk.Tk, tone: str) -> None:
    pill = _pill(window, text="状态", tone=tone)

    assert pill._tone_colors()[1] == getattr(theme.PALETTE, tone)


def test_unknown_tone_falls_back_to_muted(window: tk.Tk) -> None:
    pill = _pill(window, text="状态", tone="不认识的色调")

    assert pill._tone_colors() == (theme.PALETTE.surface_hover, theme.PALETTE.muted)


def test_status_pill_draws_the_pill_the_dot_and_the_text(window: tk.Tk) -> None:
    pill = _pill(window, text="就绪")
    pill._redraw()

    kinds = [pill.type(item) for item in pill.find_all()]

    assert kinds.count("polygon") == 1
    assert kinds.count("oval") == 1
    assert kinds.count("text") == 1


def test_status_pill_draws_nothing_when_it_has_no_text(window: tk.Tk) -> None:
    pill = _pill(window, text="")

    pill._redraw()

    assert not pill.find_all()


# ---------------------------------------------------------------- 进度条


def _progress(window: tk.Tk) -> ModernProgress:
    bar = ModernProgress(window, height=12)
    _place(bar, width=200, height=12)
    return bar


def test_progress_starts_out_empty_and_determinate(window: tk.Tk) -> None:
    bar = _progress(window)

    assert bar._mode == "determinate"
    assert bar._value == 0


def test_progress_draws_the_track_and_a_fill_when_there_is_value(window: tk.Tk) -> None:
    bar = _progress(window)

    bar.set_value(50, 100)

    assert len(bar.find_all()) == 2


def test_progress_hides_the_fill_on_zero_and_keeps_the_track(window: tk.Tk) -> None:
    bar = _progress(window)

    bar.set_value(0, 100)

    assert len(bar.find_all()) == 1


def test_progress_clamps_a_value_above_the_maximum(window: tk.Tk) -> None:
    bar = _progress(window)

    bar.set_value(500, 100)

    assert bar._value == 500  # 原始值保留，填充比例在绘制时夹到 100%
    assert len(bar.find_all()) == 2


def test_progress_treats_a_zero_maximum_as_no_fill(window: tk.Tk) -> None:
    bar = _progress(window)

    bar._maximum = 0
    bar._value = 10
    bar._redraw()

    assert len(bar.find_all()) == 1


def test_progress_stop_switches_back_to_determinate(window: tk.Tk) -> None:
    bar = _progress(window)
    bar._mode = "indeterminate"
    bar._animating = True

    bar.stop()

    assert bar._mode == "determinate"
    assert bar._animating is False


def test_progress_indeterminate_draws_a_moving_segment(window: tk.Tk) -> None:
    bar = _progress(window)
    bar._mode = "indeterminate"

    for offset in (0.0, 0.5, 1.0):
        bar._offset = offset
        bar._redraw()
        assert len(bar.find_all()) == 2


def test_progress_animation_is_a_no_op_after_stop(window: tk.Tk) -> None:
    bar = _progress(window)
    bar._animating = False

    bar._animate()

    assert bar._offset == 0.0


def test_progress_animation_advances_the_offset_and_schedules_the_next_frame(
    window: tk.Tk, monkeypatch: pytest.MonkeyPatch
) -> None:
    bar = _progress(window)
    scheduled: list[tuple[int, object]] = []
    monkeypatch.setattr(bar, "after", lambda ms, fn: scheduled.append((ms, fn)))
    bar._animating = True

    bar._animate()

    assert bar._offset == pytest.approx(0.025)
    assert scheduled and scheduled[0][0] == 30


def test_progress_set_value_keeps_animating_when_it_was_animating(window: tk.Tk) -> None:
    bar = _progress(window)
    bar._animating = True

    bar.set_value(3, 10)

    assert bar._animating is True
    assert bar._mode == "determinate"


def test_progress_draws_nothing_while_the_canvas_is_still_tiny(window: tk.Tk) -> None:
    bar = ModernProgress(window, height=12)

    bar._redraw()

    assert not bar.find_all()


# ---------------------------------------------------------------- 文本域


def test_flat_text_provides_a_real_text_widget(window: tk.Tk) -> None:
    field = FlatText(window, height=3)

    field.text.insert("1.0", "第一行")
    assert field.text.get("1.0", "end-1c") == "第一行"


def test_flat_text_canvas_height_follows_the_requested_line_count(window: tk.Tk) -> None:
    three = FlatText(window, height=3, padding=0)
    six = FlatText(window, height=6, padding=0)

    assert int(six._canvas.cget("height")) > int(three._canvas.cget("height"))


def test_flat_text_puts_the_text_widget_inside_the_canvas(window: tk.Tk) -> None:
    field = FlatText(window, height=2)

    assert field.text.master is field._canvas
    assert field.text.cget("wrap") == "word"


def test_flat_text_takes_a_custom_font_and_padding(window: tk.Tk) -> None:
    field = FlatText(window, height=2, font=theme.font(theme.SIZE_SUBHEAD), padding=4)

    assert int(field.text.cget("padx")) == 4


def test_flat_text_focus_switches_the_border_colour(window: tk.Tk) -> None:
    field = FlatText(window, height=2)
    _place(field._canvas, width=240, height=80)

    field._set_focus(True)
    focused = field._canvas.find_withtag("border")

    assert field._focused is True
    assert focused

    field._set_focus(False)

    assert field._focused is False


def test_flat_text_configure_syncs_the_inner_window(window: tk.Tk) -> None:
    field = FlatText(window, height=2)
    _place(field._canvas, width=240, height=80)

    field._on_configure()

    assert int(field._canvas.itemcget(field._window, "width")) == 238
    assert list(field._canvas.coords(field._window)) == [1.0, 1.0]


def test_flat_text_redraw_does_nothing_while_the_canvas_is_still_tiny(window: tk.Tk) -> None:
    field = FlatText(window, height=2)

    field._redraw()

    assert not field._canvas.find_withtag("border")


def test_flat_text_measures_a_real_line_height(window: tk.Tk) -> None:
    linespace = FlatText._measure_linespace(theme.font(theme.SIZE_BODY))

    assert linespace > 1


def test_flat_text_falls_back_when_the_font_cannot_be_measured(
    window: tk.Tk, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(**_kwargs):
        raise tk.TclError("没有可用字体")

    monkeypatch.setattr(tk.font, "Font", boom)

    assert FlatText._measure_linespace(theme.font(theme.SIZE_BODY)) == theme.gap(4)


# ---------------------------------------------------------------- 小标题 / 链接


def test_section_heading_shows_the_title_alone_without_a_subtitle(window: tk.Tk) -> None:
    heading = SectionHeading(window, "① 输入串网址")

    texts = [child.cget("text") for child in heading.winfo_children()]

    assert texts == ["① 输入串网址"]


def test_section_heading_shows_the_subtitle_when_given(window: tk.Tk) -> None:
    heading = SectionHeading(window, "① 输入串网址", "每行一个")

    texts = [child.cget("text") for child in heading.winfo_children()]

    assert texts == ["① 输入串网址", "每行一个"]


def _link_handlers(monkeypatch: pytest.MonkeyPatch, *args, **kwargs):
    """造一个 LinkLabel，并把它注册到各事件上的回调原样交出来。

    为什么不去 `event_generate`：Tk 的鼠标事件只送给**已映射**的部件，
    而无头环境/隐藏根窗口下部件根本没映射；用例若依赖这一点，就会在
    「单跑通过、连跑失败」之间摇摆。这里直接取构造函数注册的那个回调来调。
    """
    handlers: dict[str, object] = {}
    real_bind = tk.Label.bind

    def spy(self, sequence=None, func=None, add=None):
        if sequence is not None and func is not None:
            handlers[sequence] = func
        return real_bind(self, sequence, func, add)

    monkeypatch.setattr(tk.Label, "bind", spy)
    link = LinkLabel(*args, **kwargs)
    monkeypatch.setattr(tk.Label, "bind", real_bind)
    return link, handlers


def test_link_label_runs_its_command_on_click(window: tk.Tk, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []
    _link, handlers = _link_handlers(monkeypatch, window, "打开目录", lambda: calls.append(1))

    handlers["<Button-1>"](None)

    assert calls == [1]


def test_link_label_uses_the_accent_colour_and_a_hand_cursor(window: tk.Tk) -> None:
    link = LinkLabel(window, "点我", lambda: None)

    assert link.cget("fg") == theme.PALETTE.accent
    assert link.cget("cursor") == "hand2"


def test_link_label_brightens_on_hover_and_restores_on_leave(
    window: tk.Tk, monkeypatch: pytest.MonkeyPatch
) -> None:
    link, handlers = _link_handlers(monkeypatch, window, "点我", lambda: None)

    handlers["<Enter>"](None)
    hovered = link.cget("fg")
    handlers["<Leave>"](None)

    assert hovered == theme.PALETTE.accent_hover
    assert link.cget("fg") == theme.PALETTE.accent


def test_link_label_can_override_the_background(window: tk.Tk) -> None:
    link = LinkLabel(window, "点我", lambda: None, bg=theme.PALETTE.surface)

    assert link.cget("bg") == theme.PALETTE.surface
