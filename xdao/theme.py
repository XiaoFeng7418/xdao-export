"""界面主题：调色板、字体、间距与 ttk 样式集中在这里。

为什么要单独一个模块：界面控件散落在 ``gui.py`` 和四个对话框里，
颜色和字号以前是各写各的（同一个灰有 ``#eef2f7``/``#eef1f6`` 两种写法），
改一次配色要翻十几处。现在所有色值只在 ``PALETTE`` 里出现一次，
ttk 样式只由 :func:`apply_theme` 配置，自定义控件从 ``theme.gap`` /
``theme.font`` 取名。

约定（改动前先读）：
- 不引入任何第三方依赖，只用 tkinter/ttk 自带能力；
- 字体在导入时探测一次（``FONT_UI`` 取系统里装了的第一个候选），
  探不到就退回 Tk 默认字体，绝不能因为字体不存在而报错；
- ``apply_theme`` 必须幂等：同一进程里多次调用得到同样的样式，
  且调用时不需要已有窗口（测试用 ``tk.Tcl()`` 就能跑）。
"""

from __future__ import annotations

import tkinter as tk
from dataclasses import dataclass
from tkinter import font as tkfont
from tkinter import ttk

# ---------------------------------------------------------------- 调色板


@dataclass(frozen=True)
class Palette:
    """一套配色。字段名按用途取，不按颜色名取，换肤时不用改调用处。"""

    name: str
    bg: str  # 窗口背景
    surface: str  # 卡片背景
    surface_sunken: str  # 输入框/日志等凹陷区域
    surface_hover: str  # 鼠标悬停在卡片按钮上
    border: str  # 常规描边
    border_strong: str  # 获得焦点/强调时的描边
    text: str  # 主文字
    muted: str  # 次要文字
    faint: str  # 更弱的文字（脚注）
    accent: str  # 主色
    accent_hover: str
    accent_active: str  # 按下时
    accent_soft: str  # 主色的浅底（选中态背景）
    on_accent: str  # 主色底上的文字
    ok: str
    warn: str
    danger: str
    track: str  # 进度条/滑槽底色
    shadow: str  # 卡片投影


LIGHT = Palette(
    name="light",
    bg="#f2f4f9",
    surface="#ffffff",
    surface_sunken="#f7f9fc",
    surface_hover="#f3f6fb",
    border="#e4e8f0",
    border_strong="#c8d2e2",
    text="#151a23",
    muted="#5b6472",
    faint="#8b95a5",
    accent="#3b6ef6",
    accent_hover="#2f5fe0",
    accent_active="#2450c4",
    accent_soft="#e8eefe",
    on_accent="#ffffff",
    ok="#12996b",
    warn="#c47f16",
    danger="#dc2b3f",
    track="#e7ebf3",
    shadow="#dfe4ee",
)

DARK = Palette(
    name="dark",
    bg="#161a21",
    surface="#1e232c",
    surface_sunken="#171b23",
    surface_hover="#252b36",
    border="#2c333f",
    border_strong="#3b4453",
    text="#eef1f6",
    muted="#a4adbb",
    faint="#7d8797",
    accent="#5c8bff",
    accent_hover="#7099ff",
    accent_active="#4a7af0",
    accent_soft="#25314b",
    on_accent="#0f1319",
    ok="#3ecf8e",
    warn="#e0a63c",
    danger="#ff6b7d",
    track="#2a313c",
    shadow="#11151b",
)

PALETTES = {LIGHT.name: LIGHT, DARK.name: DARK}

# 当前配色。界面在启动时读它，测试可以直接替换。
PALETTE = LIGHT


def palette(name: str | None = None) -> Palette:
    """按名字取配色，名字不认识就返回当前配色。"""
    if not name:
        return PALETTE
    return PALETTES.get(name, PALETTE)


def set_palette(pal: Palette) -> Palette:
    """把 ``PALETTE`` 换成新配色并返回它（不改 ttk 样式，样式归 apply_theme 管）。

    界面切换主题的顺序必须是"先换 PALETTE，再重建控件"：控件颜色在
    创建时就烘进去了（``tk.Frame(bg=...)`` 这类），只改 PALETTE 不会
    让已有控件自己变色。``gui.App`` 会销毁旧控件、按新配色重画一遍。
    """
    global PALETTE
    PALETTE = pal
    return PALETTE


# ---------------------------------------------------------------- 字体

#: 界面字体候选：优先 Windows 自带且中文好看的，其次 macOS/Linux 常见字体。
UI_FONT_CANDIDATES = (
    "Microsoft YaHei UI",
    "微软雅黑",
    "Microsoft YaHei",
    "PingFang SC",
    "Noto Sans CJK SC",
    "Source Han Sans SC",
    "Segoe UI",
    "Helvetica Neue",
    "DejaVu Sans",
)
MONO_FONT_CANDIDATES = ("Cascadia Mono", "Consolas", "JetBrains Mono", "Menlo", "DejaVu Sans Mono")

#: 字体只探测一次；``_FONT_CACHE`` 按候选元组缓存结果
_FONTS_RESOLVED = False
_FONT_CACHE: dict[tuple[str, ...], str] = {}


def _available_families() -> set[str]:
    """系统里可用的字体族（小写）。拿不到就返回空集合，绝不抛异常。"""
    candidates = []
    root = tk._default_root  # noqa: SLF001 —— 没有根窗口时枚举不了字体
    if root is not None:
        candidates.append(root)
    candidates.append(None)
    for source in candidates:
        if source is not None:
            try:
                if not source.winfo_exists():
                    continue
            except Exception:  # noqa: BLE001 —— 根已销毁
                continue
        try:
            return {name.lower() for name in tkfont.families(source)}
        except Exception:  # noqa: BLE001 —— 换下一个来源再试
            continue
    return set()


def _first_available(candidates: tuple[str, ...], fallback: str) -> str:
    """挑系统里第一个装了的字体，结果在进程内缓存。

    两条硬约束：
    1. **有真窗口时才落缓存**。导入期（还没有根窗口）探测不到字体，
       那时给个结果会一路缓存下去，于是"控件用的字体"和 ``theme.FONT_MONO``
       在 CI 上对不上（曾经报 ``assert 'Consolas' == 'Cascadia Mono'``）。
    2. **根窗口可能已被销毁**：销毁后 ``tkfont.families()`` 会抛 TclError，
       所以每个来源都要能安全跳过，绝不能因为字体不存在而影响启动。
    """
    global _FONTS_RESOLVED
    if _FONTS_RESOLVED:
        return _FONT_CACHE.get(candidates, candidates[0])

    available = _available_families()
    if not available:
        # 还没有窗口（导入期 / 纯 Tcl 测试）：先给候选首个，不落缓存，等有窗口再定
        return candidates[0]

    for name in candidates:
        if name.lower() in available:
            chosen = name
            break
    else:
        chosen = fallback
    _FONT_CACHE[candidates] = chosen
    _FONTS_RESOLVED = True
    return chosen


FALLBACK_UI_FONT = "TkDefaultFont"
FALLBACK_MONO_FONT = "TkFixedFont"


#: 界面字体名（导入时给个默认值；``resolve_fonts`` 在有了根窗口后探测真实值）
FONT_UI = "Microsoft YaHei UI"
FONT_MONO = "Consolas"


def resolve_fonts(root: tk.Misc | None = None) -> tuple[str, str]:
    """探测可用字体并更新模块级 ``FONT_UI`` / ``FONT_MONO``，返回两者。

    ``gui.setup_style`` 在根窗口建好后调用它；探测结果在进程内缓存，
    之后再调用（含根窗口被销毁后）都返回同一份结果 —— 界面里同一个控件
    不能一会儿用一个字体名、一会儿用另一个。

    还没有窗口时（导入期 / 纯 Tcl 测试）**不落缓存**，只给候选里的第一个，
    等有窗口后再定，否则导入期绑定的族名会一路粘住。
    """
    global FONT_UI, FONT_MONO
    FONT_UI = _first_available(UI_FONT_CANDIDATES, FALLBACK_UI_FONT)
    FONT_MONO = _first_available(MONO_FONT_CANDIDATES, FALLBACK_MONO_FONT)
    return FONT_UI, FONT_MONO


# 字号（点）。正文 10 是 Windows 上最舒服的默认值。
SIZE_TITLE = 19
SIZE_HEADING = 12
SIZE_SUBHEAD = 11
SIZE_BODY = 10
SIZE_SMALL = 9


def font(size: int = SIZE_BODY, *, bold: bool = False) -> tuple:
    """常规字体（按当前 FONT_UI）。"""
    return (FONT_UI, size, "bold") if bold else (FONT_UI, size)


def mono(size: int = SIZE_SMALL, *, bold: bool = False) -> tuple:
    """等宽字体（日志用）。"""
    return (FONT_MONO, size, "bold") if bold else (FONT_MONO, size)


# ---------------------------------------------------------------- 间距与圆角

#: 一档间距 = 4px，取用时统一用 ``gap(n)``，不要直接写像素。
GAP_UNIT = 4
RADIUS = 10
RADIUS_SMALL = 6

#: 主窗口左栏（输入串/导出目录）的固定宽度。
#:
#: 为什么要写死：左栏里套了一层"装不下就滚动"的画布，画布的请求宽度会
#: 跟着自己的实际宽度走，于是 grid 会把左栏撑到整个窗口宽、把右边日志栏
#: 挤成一条缝（实测右栏只剩 39px）。关掉传播 + 固定宽度最稳，窗口拉宽时
#: 多出来的宽度全部给日志栏。
SETTINGS_COLUMN_WIDTH = 452

#: 左栏滚动区的初始高度（窗口刚打开、还没拿到真实高度时用）。
#: 只是个起点：真实可视高度由布局决定，内容更高时会出现滚动条。
SETTINGS_VIEWPORT_HEIGHT = 430


def gap(steps: float = 1) -> int:
    """间距档位：``gap(1)=4``、``gap(2)=8``、``gap(3)=12``、``gap(4)=16``。"""
    return int(round(GAP_UNIT * steps))


# ---------------------------------------------------------------- ttk 样式

#: 本程序用到的全部 ttk 样式名，测试会核对它们都真的配上颜色了。
STYLE_NAMES = (
    ".",
    "TFrame",
    "TLabel",
    "Card.TFrame",
    "Card.TLabel",
    "CardHeading.TLabel",
    "CardSub.TLabel",
    "CardMuted.TLabel",
    "CardFaint.TLabel",
    "Section.TLabel",
    "Muted.TLabel",
    "Faint.TLabel",
    "Title.TLabel",
    "Subtitle.TLabel",
    "Badge.TLabel",
    "TButton",
    "Secondary.TButton",
    "Ghost.TButton",
    "Danger.TButton",
    "TEntry",
    "TCombobox",
    "TCheckbutton",
    "Card.TCheckbutton",
    "TRadiobutton",
    "Card.TRadiobutton",
    "Treeview",
    "Treeview.Heading",
    "TLabelframe",
    "TLabelframe.Label",
    "Horizontal.TProgressbar",
    "Vertical.TScrollbar",
    "Horizontal.TScrollbar",
)


#: ``Palette`` 的全部字段名，顺序固定。切换主题时用它把"旧配色 → 新配色"
#: 做成一张映射表（见 ``gui.py`` 的 ``_migrate_colors``），再改写已经画好的
#: 原生控件的颜色；少了任何一个字段，那一类控件换肤后就会留着旧底色。
PALETTE_FIELDS = (
    "bg",
    "surface",
    "surface_sunken",
    "surface_hover",
    "border",
    "border_strong",
    "text",
    "muted",
    "faint",
    "accent",
    "accent_hover",
    "accent_active",
    "accent_soft",
    "on_accent",
    "ok",
    "warn",
    "danger",
    "track",
    "shadow",
)


def color_map(old: Palette, new: Palette) -> dict[str, str]:
    """``旧色值 → 新色值`` 的映射（切换主题时迁移原生控件的颜色）。

    同一个色值可能同时属于两个字段（浅色主题里 ``#ffffff`` 既是 ``surface``
    也是 ``on_accent``）。这时以先出现的字段为准：``PALETTE_FIELDS`` 把
    ``surface`` 排在 ``on_accent`` 前面，卡片底色的迁移才不会被主色文字带偏
    （主色按钮上的文字由 ttk 样式负责，不靠这张表）。
    """
    mapping: dict[str, str] = {}
    for field_name in PALETTE_FIELDS:
        mapping.setdefault(getattr(old, field_name), getattr(new, field_name))
    return mapping


def apply_theme(root: tk.Misc, pal: Palette | None = None) -> ttk.Style:
    """把配色装到 ttk 样式上并返回 ``Style`` 对象。

    幂等：同一个 root 反复调用只会重写同样的值。需要窗口已存在
    （ttk.Style 要绑定解释器），但不需要窗口显示出来。

    换主题时直接再调一次即可：ttk 控件会立刻变，原生 tk 控件
    （Frame/Label/Text/Checkbutton）的颜色得靠调用方自己重画或迁移。
    """
    global PALETTE
    pal = pal or PALETTE
    PALETTE = pal
    resolve_fonts(root)

    style = ttk.Style(root)
    try:
        style.theme_use("clam")  # clam 才允许自定义背景色
    except tk.TclError:  # pragma: no cover - 极少数精简版 Tk
        pass

    # ttk.Combobox 弹出的候选列表是原生 tk Listbox，不受样式表管，
    # 只能通过 Tk 选项库上色；不上色的话暗色主题里会突然跳出一个白框。
    try:
        root.option_add("*TCombobox*Listbox.background", pal.surface)
        root.option_add("*TCombobox*Listbox.foreground", pal.text)
        root.option_add("*TCombobox*Listbox.selectBackground", pal.accent)
        root.option_add("*TCombobox*Listbox.selectForeground", pal.on_accent)
    except tk.TclError:  # pragma: no cover - 精简版 Tk 没有 option 库
        pass

    # 基础
    style.configure(".", background=pal.bg, foreground=pal.text, font=font(SIZE_BODY))
    style.configure("TFrame", background=pal.bg)
    style.configure("TLabel", background=pal.bg, foreground=pal.text)

    # 卡片（白底容器）
    style.configure("Card.TFrame", background=pal.surface)
    style.configure("Card.TLabel", background=pal.surface, foreground=pal.text)
    style.configure(
        "CardHeading.TLabel",
        background=pal.surface,
        foreground=pal.text,
        font=font(SIZE_SUBHEAD, bold=True),
    )
    style.configure(
        "CardSub.TLabel",
        background=pal.surface,
        foreground=pal.muted,
        font=font(SIZE_SMALL),
    )
    style.configure(
        "CardMuted.TLabel",
        background=pal.surface,
        foreground=pal.faint,
        font=font(SIZE_SMALL),
    )
    # 卡片上的"更淡的小字"：和 CardMuted 同一档颜色，但行距更松，
    # 用于多行说明（主窗口的饼干提示就靠它别挤成三行）。
    style.configure(
        "CardFaint.TLabel",
        background=pal.surface,
        foreground=pal.faint,
        font=font(SIZE_SMALL),
    )

    # 窗口背景上的标题
    style.configure("Title.TLabel", background=pal.bg, foreground=pal.text, font=font(SIZE_TITLE, bold=True))
    style.configure("Subtitle.TLabel", background=pal.bg, foreground=pal.muted, font=font(SIZE_SMALL))
    style.configure(
        "Section.TLabel", background=pal.bg, foreground=pal.text, font=font(SIZE_SUBHEAD, bold=True)
    )
    style.configure("Muted.TLabel", background=pal.bg, foreground=pal.muted, font=font(SIZE_SMALL))
    style.configure("Faint.TLabel", background=pal.bg, foreground=pal.faint, font=font(SIZE_SMALL))
    style.configure(
        "Badge.TLabel",
        background=pal.accent_soft,
        foreground=pal.accent,
        font=font(SIZE_SMALL, bold=True),
        padding=(gap(1.5), gap(0.5)),
    )

    # 按钮：主按钮实心、次按钮浅底、幽灵按钮只有字、危险按钮红
    button_common = dict(borderwidth=0, focusthickness=0, relief="flat", anchor="center")
    style.configure(
        "TButton",
        background=pal.accent,
        foreground=pal.on_accent,
        padding=(gap(4), gap(2.25)),
        font=font(SIZE_BODY, bold=True),
        **button_common,
    )
    style.map(
        "TButton",
        background=[
            ("disabled", pal.track),
            ("pressed", pal.accent_active),
            ("active", pal.accent_hover),
        ],
        foreground=[("disabled", pal.faint)],
    )
    style.configure(
        "Secondary.TButton",
        background=pal.surface_hover,
        foreground=pal.text,
        padding=(gap(3), gap(1.75)),
        font=font(SIZE_SMALL, bold=True),
        **button_common,
    )
    style.map(
        "Secondary.TButton",
        background=[("disabled", pal.bg), ("pressed", pal.border), ("active", pal.border)],
        foreground=[("disabled", pal.faint)],
    )
    style.configure(
        "Ghost.TButton",
        background=pal.bg,
        foreground=pal.muted,
        padding=(gap(2), gap(1.5)),
        font=font(SIZE_SMALL),
        **button_common,
    )
    style.map(
        "Ghost.TButton",
        background=[("active", pal.surface_hover), ("pressed", pal.border)],
        foreground=[("active", pal.text), ("disabled", pal.faint)],
    )
    style.configure(
        "Danger.TButton",
        background=pal.danger,
        foreground=pal.on_accent,
        padding=(gap(3), gap(1.75)),
        font=font(SIZE_SMALL, bold=True),
        **button_common,
    )
    style.map(
        "Danger.TButton",
        background=[("disabled", pal.track), ("pressed", pal.danger), ("active", pal.danger)],
        foreground=[("disabled", pal.faint)],
    )

    # 输入控件：浅凹陷底 + 细描边，聚焦时主色描边
    style.configure(
        "TEntry",
        fieldbackground=pal.surface_sunken,
        foreground=pal.text,
        bordercolor=pal.border,
        lightcolor=pal.border,
        darkcolor=pal.border,
        insertcolor=pal.text,
        padding=gap(1.5),
    )
    style.map(
        "TEntry",
        bordercolor=[("focus", pal.accent)],
        lightcolor=[("focus", pal.accent)],
        darkcolor=[("focus", pal.accent)],
        fieldbackground=[("disabled", pal.bg)],
    )
    style.configure(
        "TCombobox",
        fieldbackground=pal.surface_sunken,
        background=pal.surface_hover,
        foreground=pal.text,
        arrowcolor=pal.muted,
        bordercolor=pal.border,
        lightcolor=pal.border,
        darkcolor=pal.border,
        padding=gap(1),
    )
    style.map(
        "TCombobox",
        bordercolor=[("focus", pal.accent)],
        lightcolor=[("focus", pal.accent)],
        darkcolor=[("focus", pal.accent)],
        fieldbackground=[("readonly", pal.surface_sunken), ("disabled", pal.bg)],
        foreground=[("disabled", pal.faint)],
        arrowcolor=[("disabled", pal.faint)],
    )

    # 勾选/单选
    for style_name, background in (
        ("TCheckbutton", pal.bg),
        ("Card.TCheckbutton", pal.surface),
        ("TRadiobutton", pal.bg),
        ("Card.TRadiobutton", pal.surface),
    ):
        style.configure(
            style_name,
            background=background,
            foreground=pal.text,
            focuscolor=background,
            font=font(SIZE_BODY),
            padding=(gap(0.5), gap(0.75)),
        )
        style.map(
            style_name,
            background=[("active", background)],
            foreground=[("disabled", pal.faint)],
            indicatorcolor=[
                ("selected", pal.accent),
                ("pressed", pal.accent_hover),
                ("!selected", pal.surface_sunken),
            ],
        )

    # 表格（监控列表）
    style.configure(
        "Treeview",
        background=pal.surface,
        fieldbackground=pal.surface,
        foreground=pal.text,
        bordercolor=pal.border,
        lightcolor=pal.surface,
        darkcolor=pal.surface,
        rowheight=gap(7),
        font=font(SIZE_BODY),
        borderwidth=0,
    )
    style.map(
        "Treeview",
        background=[("selected", pal.accent_soft)],
        foreground=[("selected", pal.text)],
    )
    style.configure(
        "Treeview.Heading",
        background=pal.surface_sunken,
        foreground=pal.muted,
        font=font(SIZE_SMALL, bold=True),
        relief="flat",
        padding=(gap(1.5), gap(1.25)),
        borderwidth=0,
    )
    style.map("Treeview.Heading", background=[("active", pal.surface_hover)])

    # 分组框（设置对话框里用）
    style.configure(
        "TLabelframe",
        background=pal.surface,
        bordercolor=pal.border,
        lightcolor=pal.border,
        darkcolor=pal.border,
        relief="solid",
        borderwidth=1,
        padding=gap(3),
    )
    style.configure(
        "TLabelframe.Label",
        background=pal.surface,
        foreground=pal.muted,
        font=font(SIZE_SMALL, bold=True),
    )

    # 进度条
    style.configure(
        "Horizontal.TProgressbar",
        background=pal.accent,
        troughcolor=pal.track,
        bordercolor=pal.track,
        lightcolor=pal.accent,
        darkcolor=pal.accent,
        thickness=gap(1.5),
    )

    # 滚动条：细长、无箭头
    for orient in ("Vertical", "Horizontal"):
        style.configure(
            f"{orient}.TScrollbar",
            background=pal.border_strong,
            troughcolor=pal.bg,
            bordercolor=pal.bg,
            arrowcolor=pal.muted,
            lightcolor=pal.border_strong,
            darkcolor=pal.border_strong,
            relief="flat",
            borderwidth=0,
            width=gap(2.5),
        )
        style.map(
            f"{orient}.TScrollbar",
            background=[("active", pal.muted), ("pressed", pal.accent)],
        )
    return style


def style_colors(style: ttk.Style, name: str) -> dict:
    """读回某个样式的配色（测试用：确认样式真的配上了颜色）。"""
    return {
        key: style.lookup(name, key)
        for key in ("background", "foreground", "fieldbackground", "bordercolor")
        if style.lookup(name, key)
    }


__all__ = [
    "DARK",
    "LIGHT",
    "PALETTE",
    "PALETTES",
    "Palette",
    "apply_theme",
    "font",
    "gap",
    "mono",
    "palette",
    "resolve_fonts",
    "style_colors",
    "FONT_MONO",
    "FONT_UI",
    "STYLE_NAMES",
]
