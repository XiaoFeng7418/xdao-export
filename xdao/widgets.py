"""自定义控件：ttk 做不出圆角，所以几个关键部件用 Canvas 手绘。

设计原则（改动前先读）：
- 每个控件自带 ``<Configure>`` 重绘，容器缩放时不需要调用方干预；
- 颜色一律从 :mod:`xdao.theme` 的当前配色取，不在本文件里写死色值；
- 不给控件加"必须调用某个方法才会显示"的前提，构造完就是最终样子；
- 全部是 ``tk`` 级别控件（不是 ttk），因为要自定义绘制。
"""

from __future__ import annotations

import tkinter as tk
from collections.abc import Callable

from . import theme


def round_rect_points(x1: float, y1: float, x2: float, y2: float, r: float) -> list[float]:
    """圆角矩形的多边形顶点（配 ``smooth=True`` 用，Tk 会自己把角抹圆）。"""
    r = max(0.0, min(r, (x2 - x1) / 2, (y2 - y1) / 2))
    return [
        x1 + r, y1,
        x2 - r, y1,
        x2, y1,
        x2, y1 + r,
        x2, y2 - r,
        x2, y2,
        x2 - r, y2,
        x1 + r, y2,
        x1, y2,
        x1, y2 - r,
        x1, y1 + r,
        x1, y1,
    ]


def draw_round_rect(canvas: tk.Canvas, x1, y1, x2, y2, r: float, **kwargs) -> int:
    """在画布上画一个圆角矩形。"""
    return canvas.create_polygon(round_rect_points(x1, y1, x2, y2, r), smooth=True, **kwargs)


class Card(tk.Frame):
    """圆角卡片：自身是普通 Frame，内容放进 ``.body``。

    用法::

        card = Card(parent)
        card.pack(fill="x")
        ttk.Label(card.body, text="标题", style="CardHeading.TLabel").pack(anchor="w")

    ``accent`` 为真时，卡片底部会有一条主色短线，用来标记"当前卡片"。
    """

    def __init__(
        self,
        master: tk.Misc,
        *,
        padding: int | None = None,
        radius: int | None = None,
        accent: bool = False,
        stretch: bool = False,
        **kwargs,
    ) -> None:
        pal = theme.PALETTE
        super().__init__(master, bg=pal.bg, highlightthickness=0, bd=0, **kwargs)
        self._radius = theme.RADIUS if radius is None else radius
        self._accent = accent
        self._stretch = stretch
        pad = theme.gap(4) if padding is None else padding

        self._canvas = tk.Canvas(self, bg=pal.bg, highlightthickness=0, bd=0)
        self._canvas.pack(fill="both", expand=True)
        self._host: tk.Frame | None = None
        if stretch:
            # 画布里的窗口默认只有「请求尺寸」，不会跟着画布长高，于是
            # 想撑满的文本区永远只拿到请求高度。中间垫一个铺满画布的
            # 容器，body 再放进容器里 expand，就能真正长到卡片底部。
            self._host = tk.Frame(self._canvas, bg=pal.surface)
            self._window = self._canvas.create_window(0, 0, anchor="nw", window=self._host)
            self.body = tk.Frame(self._host, bg=pal.surface, padx=pad, pady=pad)
            self.body.pack(fill="both", expand=True)
        else:
            self.body = tk.Frame(self._canvas, bg=pal.surface, padx=pad, pady=pad)
            self._window = self._canvas.create_window(0, 0, anchor="nw", window=self.body)
        self._syncing = False
        self._wrap_width = 0
        self._canvas.bind("<Configure>", self._on_canvas_configure)
        self.body.bind("<Configure>", self._on_body_configure)

    def _on_canvas_configure(self, _event=None) -> None:
        """宽度跟着卡片走（内容自适应宽度），高度由内容决定。

        ``stretch=True`` 时高度也交给画布，卡片多高内容就铺多高。
        """
        width = self._canvas.winfo_width()
        height = self._canvas.winfo_height()
        if self._host is not None:
            options = {}
            if width > 1:
                options["width"] = width
            if height > 1:
                options["height"] = height
            if options:
                self._canvas.itemconfigure(self._window, **options)
        elif width > 1:
            self._canvas.itemconfigure(self._window, width=width)
        self._redraw()

    def _on_body_configure(self, _event=None) -> None:
        """内容变高/变矮时同步画布高度，内容变宽时也要让卡片跟着变宽。

        画布不会像 Frame 那样把内嵌窗口的请求尺寸算进自己的请求尺寸，
        所以这里手动同步；``_syncing`` 用来避免"设尺寸 → 触发 Configure → 再设尺寸"的循环。
        """
        if self._syncing:
            return
        if self._host is None:
            needed = self.body.winfo_reqheight()
            if needed > 1 and self._canvas.cget("height") != needed:
                self._syncing = True
                try:
                    self._canvas.configure(height=needed)
                finally:
                    self._syncing = False
        # 宽度：只在内容比画布宽的时候撑开。
        # 历史坑：早期版本从不调宽度，画布就一直是 tk.Canvas 的默认 378px，
        # 于是所有用 Card 装内容的对话框都被钉死在 410px 宽，文字再长也只会被裁掉。
        content_width = self.body.winfo_reqwidth()
        # 带 ``wraplength`` 的文本请求宽度永远只有一行多，容器就不肯变宽 ——
        # 这种情况下按 wraplength 反推容器宽度，文字才有地方排。
        # 结果缓存一次：这个值只在嵌套的对话框里出现，遍历控件树不便宜。
        if self._wrap_width == 0:
            self._wrap_width = self._max_wraplength()
        if self._wrap_width > 0:
            content_width = max(content_width, self._wrap_width + theme.gap(12))
        if content_width > self._canvas.winfo_reqwidth() > 1:
            self._syncing = True
            try:
                self._canvas.configure(width=content_width)
            finally:
                self._syncing = False
        self._redraw()

    def _max_wraplength(self) -> int:
        """子控件里最大的 ``wraplength``（没有就返回 0）。

        注意 ttk 控件 ``cget`` 出来的是 ``Tcl_Obj``（不是 str），
        必须显式转成字符串再解析。
        """
        widest = 0
        for child in self._walk(self.body):
            try:
                value = int(str(child.cget("wraplength")))
            except (tk.TclError, ValueError, TypeError):
                continue
            widest = max(widest, value)
        return widest

    @staticmethod
    def _walk(widget: tk.Misc):
        """递归遍历子控件（只为了找 wraplength）。"""
        for child in widget.winfo_children():
            yield child
            yield from Card._walk(child)

    def _redraw(self) -> None:
        canvas = self._canvas
        width = canvas.winfo_width()
        height = canvas.winfo_height()
        if width <= 2 or height <= 2:
            return
        canvas.delete("bg")
        pal = theme.PALETTE
        draw_round_rect(
            canvas, 0.5, 0.5, width - 0.5, height - 0.5, self._radius,
            fill=pal.surface, outline=pal.border, width=1, tags="bg",
        )
        if self._accent:
            bar_h = 3
            draw_round_rect(
                canvas, theme.gap(1), height - bar_h - 1, theme.gap(7), height - 1,
                bar_h / 2, fill=pal.accent, outline="", tags="bg",
            )
        canvas.tag_lower("bg")


class StatusPill(tk.Canvas):
    """状态胶囊：小圆点 + 文字，颜色跟着状态走。"""

    #: 没传 width 时按文字量算出来的宽度（``set`` 更新文字时会重新调整）
    def __init__(self, master: tk.Misc, text: str = "", *, tone: str = "muted", **kwargs) -> None:
        pal = theme.PALETTE
        self._height = kwargs.pop("height", theme.gap(5.5))
        super().__init__(
            master,
            height=self._height,
            width=kwargs.pop("width", 1),
            bg=kwargs.pop("bg", pal.bg),
            highlightthickness=0,
            bd=0,
            **kwargs,
        )
        self._text = text
        self._tone = tone
        self._font = theme.font(theme.SIZE_SMALL, bold=True)
        self.bind("<Configure>", lambda _e: self._redraw())
        self._fit_width()

    def set(self, text: str, tone: str = "muted") -> None:
        """更新文字与色调（ok / warn / danger / accent / muted）。"""
        self._text = text
        self._tone = tone
        self._fit_width()
        self._redraw()

    def _fit_width(self) -> None:
        """按文字长度自动调宽（放在 pack 之前或之后都成立）。"""
        if not self._text:
            return
        try:
            needed = self.winfo_toplevel().tk.call("font", "measure", self._font, self._text)
        except tk.TclError:  # pragma: no cover - 没有可用字体时退回估算
            needed = len(self._text) * theme.SIZE_SMALL
        self.configure(width=max(int(needed) + theme.gap(6.5), theme.gap(10)))

    def _tone_colors(self) -> tuple[str, str]:
        pal = theme.PALETTE
        mapping = {
            "ok": (pal.accent_soft, pal.ok),
            "warn": (pal.accent_soft, pal.warn),
            "danger": (pal.accent_soft, pal.danger),
            "accent": (pal.accent_soft, pal.accent),
            "muted": (pal.surface_hover, pal.muted),
        }
        return mapping.get(self._tone, mapping["muted"])

    def _redraw(self) -> None:
        self.delete("all")
        width = self.winfo_width()
        height = self.winfo_height()
        if width <= 2 or height <= 2 or not self._text:
            return
        background, foreground = self._tone_colors()
        draw_round_rect(
            self, 0.5, 0.5, width - 0.5, height - 0.5, height / 2 - 1,
            fill=background, outline="",
        )
        pad = theme.gap(2)
        self.create_oval(
            pad, height / 2 - 2.5, pad + 5, height / 2 + 2.5,
            fill=foreground, outline="",
        )
        self.create_text(
            pad + 11, height / 2, text=self._text, anchor="w",
            fill=foreground, font=self._font,
        )


class ModernProgress(tk.Canvas):
    """圆角扁平进度条，支持确定/不确定两种模式。"""

    def __init__(self, master, height: int | None = None, **kwargs) -> None:
        pal = theme.PALETTE
        super().__init__(
            master,
            height=theme.gap(1.5) if height is None else height,
            bg=pal.bg,
            highlightthickness=0,
            borderwidth=0,
            **kwargs,
        )
        self._value = 0
        self._maximum = 100
        self._mode = "determinate"
        self._offset = 0.0
        self._animating = False
        self.bind("<Configure>", lambda _e: self._redraw())

    def _redraw(self) -> None:
        pal = theme.PALETTE
        self.delete("all")
        w = self.winfo_width()
        h = self.winfo_height()
        if w <= 2 or h <= 2:
            return
        r = h / 2
        draw_round_rect(self, 0, 0, w, h, r, fill=pal.track, outline="")
        if self._mode == "indeterminate":
            seg_w = max(30.0, w * 0.25)
            span = w + seg_w
            x = (self._offset * span) % span - seg_w
            draw_round_rect(self, x, 0, x + seg_w, h, r, fill=pal.accent, outline="")
        else:
            ratio = min(1.0, self._value / self._maximum) if self._maximum else 0.0
            fill_w = w * ratio
            if fill_w >= h:
                draw_round_rect(self, 0, 0, fill_w, h, r, fill=pal.accent, outline="")

    def start(self, interval: int = 12) -> None:
        self._mode = "indeterminate"
        self._animating = True
        self._animate()

    def stop(self) -> None:
        self._animating = False
        self._mode = "determinate"
        self._redraw()

    def set_value(self, value: float, maximum: float = 100) -> None:
        was_animating = self._animating
        self._animating = False
        self._mode = "determinate"
        self._value = value
        self._maximum = maximum
        self._redraw()
        if was_animating:
            self._animating = True

    def _animate(self) -> None:
        if not self._animating:
            return
        self._offset += 0.025
        self._redraw()
        self.after(30, self._animate)


class FlatText(tk.Frame):
    """带聚焦描边与圆角外观的文本域。

    真正的输入框还是 ``tk.Text``（``.text``），外面套一层画布画圆角与描边：
    ``tk.Text`` 自己的 ``highlightthickness`` 只能是直角矩形。
    """

    def __init__(
        self,
        master: tk.Misc,
        *,
        height: int = 4,
        font: tuple | None = None,
        padding: int | None = None,
        wrap: str = "word",
        **kwargs,
    ) -> None:
        pal = theme.PALETTE
        super().__init__(master, bg=pal.surface, highlightthickness=0, bd=0, **kwargs)
        self._radius = theme.RADIUS_SMALL
        pad = theme.gap(2) if padding is None else padding
        self._focused = False
        text_font = font or theme.font(theme.SIZE_BODY)
        linespace = self._measure_linespace(text_font)
        canvas_height = height * linespace + pad * 2 + 2

        self._canvas = tk.Canvas(
            self, bg=pal.surface, highlightthickness=0, bd=0, height=canvas_height
        )
        self._canvas.pack(fill="x", expand=False)
        self.text = tk.Text(
            self._canvas,
            height=height,
            wrap=wrap,
            bg=pal.surface_sunken,
            fg=pal.text,
            insertbackground=pal.text,
            selectbackground=pal.accent_soft,
            selectforeground=pal.text,
            relief="flat",
            borderwidth=0,
            highlightthickness=0,
            font=text_font,
            padx=pad,
            pady=pad,
        )
        self._window = self._canvas.create_window(0, 0, anchor="nw", window=self.text)
        self._canvas.bind("<Configure>", self._on_configure)
        self.text.bind("<FocusIn>", lambda _e: self._set_focus(True))
        self.text.bind("<FocusOut>", lambda _e: self._set_focus(False))

    @staticmethod
    def _measure_linespace(text_font: tuple) -> int:
        """一行在像素上有多高（用于把"几行"换算成画布高度）。"""
        try:
            probe = tk.font.Font(font=text_font)
            return int(probe.metrics("linespace")) or theme.gap(4)
        except Exception:  # noqa: BLE001 —— 探测不到就用经验值
            return theme.gap(4)

    def _set_focus(self, focused: bool) -> None:
        self._focused = focused
        self._redraw()

    def _on_configure(self, _event=None) -> None:
        width = self._canvas.winfo_width()
        height = self._canvas.winfo_height()
        inset = 1
        self._canvas.itemconfigure(
            self._window, width=max(1, width - inset * 2), height=max(1, height - inset * 2)
        )
        self._canvas.coords(self._window, inset, inset)
        self._redraw()

    def _redraw(self) -> None:
        canvas = self._canvas
        width = canvas.winfo_width()
        height = canvas.winfo_height()
        if width <= 2 or height <= 2:
            return
        pal = theme.PALETTE
        canvas.delete("border")
        outline = pal.accent if self._focused else pal.border
        draw_round_rect(
            canvas, 0.5, 0.5, width - 0.5, height - 0.5, self._radius,
            fill="", outline=outline, width=1, tags="border",
        )
        canvas.tag_raise(self._window)


class SectionHeading(tk.Frame):
    """「① 标题 + 说明」两行式小标题，卡片里统一用它。"""

    def __init__(self, master: tk.Misc, title: str, subtitle: str = "", **kwargs) -> None:
        super().__init__(master, bg=theme.PALETTE.surface, **kwargs)
        tk.Label(
            self, text=title, bg=theme.PALETTE.surface, fg=theme.PALETTE.text,
            font=theme.font(theme.SIZE_SUBHEAD, bold=True), anchor="w",
        ).pack(anchor="w")
        if subtitle:
            tk.Label(
                self, text=subtitle, bg=theme.PALETTE.surface, fg=theme.PALETTE.faint,
                font=theme.font(theme.SIZE_SMALL), anchor="w", justify="left",
            ).pack(anchor="w", pady=(theme.gap(0.5), 0))


class LinkLabel(tk.Label):
    """看起来像链接的按钮（Tk 没有真正的链接控件）。"""

    def __init__(self, master: tk.Misc, text: str, command: Callable[[], None], **kwargs) -> None:
        pal = theme.PALETTE
        super().__init__(
            master,
            text=text,
            fg=pal.accent,
            bg=kwargs.pop("bg", pal.bg),
            font=theme.font(theme.SIZE_SMALL),
            cursor="hand2",
            **kwargs,
        )
        self.bind("<Button-1>", lambda _e: command())
        self.bind("<Enter>", lambda _e: self.configure(fg=pal.accent_hover))
        self.bind("<Leave>", lambda _e: self.configure(fg=pal.accent))


__all__ = [
    "Card",
    "FlatText",
    "LinkLabel",
    "ModernProgress",
    "SectionHeading",
    "StatusPill",
    "draw_round_rect",
    "round_rect_points",
]
