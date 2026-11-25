"""界面主题：调色板、字体、间距与 ttk 样式集中在这里。

为什么要单独一个模块：界面控件散落在 ``gui.py`` 和四个对话框里，
颜色和字号以前是各写各的（同一个灰有 ``#eef2f7``/``#eef1f6`` 两种写法），
改一次配色要翻十几处。现在所有色值只在 ``PALETTE`` 里出现一次，
ttk 样式只由 :func:`apply_theme` 配置，自定义控件从 ``theme.gap`` /
``theme.font`` 取名。

约定（改动前先读）：
- 不引入任何第三方依赖，只用 tkinter/ttk 自带能力；
- 字体在 Tk 启动后探测一次，优先使用操作系统已安装的字体；
- ``apply_theme`` 必须幂等，且窗口无需显示。
"""

from __future__ import annotations

import struct
import sys
import zlib
from dataclasses import dataclass, fields

from .theme_rules import (
    FONT_SCALE_CHOICES,
    FONT_SCALE_MAX,
    FONT_SCALE_MIN,
    PALETTE_NAMES,
    normalize_font_scale,
)

try:  # 没有 tkinter 的 Python（少数自编译版本）也要能 import 这个模块
    import tkinter as tk
    from tkinter import font as tkfont
    from tkinter import ttk
except ImportError:  # pragma: no cover —— 只在缺 tkinter 的解释器上走到
    # 注解都在 ``from __future__ import annotations`` 之后是字符串，不会在这里求值；
    # 只有真去画界面时才会用到 tk/ttk，那时程序本来就起不来。
    # 保留无 Tk 时导入主题模块的兼容性；配置只读 theme_rules，不加载本模块。
    tk = None  # type: ignore[assignment]
    tkfont = None  # type: ignore[assignment]
    ttk = None  # type: ignore[assignment]

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
    border_strong: str  # 悬停或需要强调边界时的描边
    text: str  # 主文字
    muted: str  # 次要文字
    faint: str  # 更弱的文字（脚注）
    accent: str  # 主色
    accent_hover: str
    accent_active: str  # 按下时
    accent_soft: str  # 主色的浅底（选中态背景）
    on_accent: str  # 主色底上的文字
    ok: str
    ok_soft: str
    warn: str
    warn_soft: str
    danger: str
    danger_soft: str
    disabled_surface: str
    disabled_text: str
    track: str  # 进度条/滑槽底色
    shadow: str  # 卡片投影


LIGHT = Palette(
    name="light",
    bg="#eff1f4",
    surface="#ffffff",
    surface_sunken="#f5f7fa",
    surface_hover="#e9edf3",
    border="#dce2ea",
    border_strong="#a2afc1",
    text="#192333",
    muted="#475569",
    faint="#58687d",
    accent="#245bc4",
    accent_hover="#1d4dac",
    accent_active="#153e95",
    accent_soft="#e6efff",
    on_accent="#ffffff",
    ok="#12653f",
    ok_soft="#e3f3eb",
    warn="#895300",
    warn_soft="#fff0d5",
    danger="#ac2940",
    danger_soft="#fae8ec",
    disabled_surface="#e6e9ee",
    disabled_text="#737e8e",
    track="#e2e7ee",
    shadow="#d7dce4",
)

DARK = Palette(
    name="dark",
    bg="#1c1e23",
    surface="#252830",
    surface_sunken="#1e2229",
    surface_hover="#313641",
    border="#373c47",
    border_strong="#616d81",
    text="#f2f4f8",
    muted="#bac3cf",
    faint="#9aa6b8",
    accent="#9bbcff",
    accent_hover="#b5cdff",
    accent_active="#80a9fc",
    accent_soft="#293d62",
    on_accent="#121a2a",
    ok="#73d6a6",
    ok_soft="#203c32",
    warn="#f3c06c",
    warn_soft="#483920",
    danger="#ff9bac",
    danger_soft="#482c36",
    disabled_surface="#30343d",
    disabled_text="#8691a1",
    track="#393f4a",
    shadow="#181a1e",
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

#: Windows 优先保证中文行高与字形，其次使用 Segoe UI 的现代 / 常规字族。
UI_FONT_CANDIDATES = (
    "Microsoft YaHei UI",
    "微软雅黑",
    "Microsoft YaHei",
    "Segoe UI Variable Text",
    "Segoe UI Variable",
    "Segoe UI",
    "PingFang SC",
    "Noto Sans CJK SC",
    "Source Han Sans SC",
    "Helvetica Neue",
    "DejaVu Sans",
)
MONO_FONT_CANDIDATES = ("Cascadia Mono", "Consolas", "JetBrains Mono", "Menlo", "DejaVu Sans Mono")

#: 两套字体分别缓存，避免先解析的界面字体挡住等宽字体探测。
_FONT_CACHE: dict[tuple[str, ...], str] = {}


def _available_families(root: tk.Misc | None) -> set[str]:
    """系统里可用的字体族（小写）。拿不到就返回空集合，绝不抛异常。"""
    if tk is None:
        return set()
    default = tk._default_root  # noqa: SLF001 —— 没有根窗口时枚举不了字体
    for source in (root, default):
        if source is None:
            continue
        try:
            if source.winfo_exists():
                return {name.casefold() for name in tkfont.families(source)}
        except tk.TclError:  # 根已销毁，或纯 Tcl 解释器没有加载 Tk
            continue
    return set()


def _first_available(candidates: tuple[str, ...], fallback: str, available: set[str]) -> str:
    """挑系统里第一个装了的字体，结果在进程内缓存。

    两条硬约束：
    1. **有真窗口时才落缓存**。导入期（还没有根窗口）探测不到字体，
       那时给个结果会一路缓存下去，于是"控件用的字体"和 ``theme.FONT_MONO``
       在 CI 上对不上（曾经报 ``assert 'Consolas' == 'Cascadia Mono'``）。
    2. **根窗口可能已被销毁**：销毁后 ``tkfont.families()`` 会抛 TclError，
       所以每个来源都要能安全跳过，绝不能因为字体不存在而影响启动。
    """
    if candidates in _FONT_CACHE:
        return _FONT_CACHE[candidates]
    if not available:
        # 还没有窗口（导入期 / 纯 Tcl 测试）：先给候选首个，不落缓存，等有窗口再定
        return candidates[0]

    chosen = next((name for name in candidates if name.casefold() in available), fallback)
    _FONT_CACHE[candidates] = chosen
    return chosen


FALLBACK_UI_FONT = "TkDefaultFont"
FALLBACK_MONO_FONT = "TkFixedFont"


#: 界面字体名（导入时给个默认值；``resolve_fonts`` 在有了根窗口后探测真实值）
FONT_UI = UI_FONT_CANDIDATES[0]
FONT_MONO = MONO_FONT_CANDIDATES[0]


def resolve_fonts(root: tk.Misc | None = None) -> tuple[str, str]:
    """探测可用字体并更新模块级 ``FONT_UI`` / ``FONT_MONO``，返回两者。

    ``gui.setup_style`` 在根窗口建好后调用它；探测结果在进程内缓存，
    之后再调用（含根窗口被销毁后）都返回同一份结果 —— 界面里同一个控件
    不能一会儿用一个字体名、一会儿用另一个。

    还没有窗口时（导入期 / 纯 Tcl 测试）**不落缓存**，只给候选里的第一个，
    等有窗口后再定，否则导入期绑定的族名会一路粘住。
    """
    global FONT_UI, FONT_MONO
    available = set() if len(_FONT_CACHE) == 2 else _available_families(root)
    FONT_UI = _first_available(UI_FONT_CANDIDATES, FALLBACK_UI_FONT, available)
    FONT_MONO = _first_available(MONO_FONT_CANDIDATES, FALLBACK_MONO_FONT, available)
    return FONT_UI, FONT_MONO


# 字号（点）。正文 10 是 Windows 上最舒服的默认值。
SIZE_TITLE = 21
SIZE_METRIC = 17
SIZE_HEADING = 12
SIZE_SUBHEAD = 11
SIZE_BODY = 10
SIZE_SMALL = 9

# ---------------------------------------------------------------- 界面字号档位
#
# 为什么比例放在这里、而不是在每个 ``font(...)`` 调用点写不同尺寸：
# 「字太小」是个体差异（有人 10 点正合适，有人得 15 点），而字号散落在
# 几十处调用点上时，加一档就得改几十行、还必然漏几处（角落里的提示、
# 日志框、对话框标题……）。现在所有尺寸都过 :func:`scaled`，只有一个真源：
# ``gui.App.switch_font_scale`` 改一次比例、重建一遍界面，全窗口一起换档。
# 档位、范围与归一化规则来自 theme_rules，旧的 theme 导入路径继续可用。
#: 当前界面字号比例（``1.0`` ＝ 下面各 ``SIZE_*`` 的原值）
FONT_SCALE = 1.0

#: 系统缩放比例（125% ＝ ``1.25``）。字号不乘它 —— 声明 DPI 感知之后 Windows
#: 会按真实 DPI 把「点」换算成像素；但间距、左栏宽度、窗口尺寸是写死的像素，
#: 不乘它就会在 150% 的屏上挤成一小块。
DPI_SCALE = 1.0


def normalize_dpi_scale(value: object, fallback: float = 1.0) -> float:
    """把读来的系统缩放收进 ``[1.0, 3.0]``；认不出来就回落。"""
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return fallback
    if number != number:  # NaN
        return fallback
    return min(max(number, 1.0), 3.0)


def set_dpi_scale(scale: object) -> float:
    """设定系统缩放比例，返回真正生效的值。"""
    global DPI_SCALE
    DPI_SCALE = normalize_dpi_scale(scale)
    return DPI_SCALE


def dpi_scale() -> float:
    """当前系统缩放比例（``1.0`` ＝ 100%）。"""
    return DPI_SCALE


def set_font_scale(scale: object) -> float:
    """设定界面字号比例，返回真正生效的值。"""
    global FONT_SCALE
    FONT_SCALE = normalize_font_scale(scale)
    return FONT_SCALE


def font_scale() -> float:
    """当前界面字号比例（``1.0`` ＝ 各 ``SIZE_*`` 的原值）。

    界面那边要拿它算窗口尺寸（字号大了窗口也得跟着放大），所以给一个读口，
    而不是让它直接摸模块全局量。
    """
    return FONT_SCALE


def scaled(size: int) -> int:
    """把基础字号乘上当前比例（下限 7 点，免得小得看不清）。"""
    return max(7, int(round(size * FONT_SCALE)))


def font_scale_label(scale: object = None) -> str:
    """按（给定的或当前的）比例找最接近的档位名。"""
    value = FONT_SCALE if scale is None else normalize_font_scale(scale)
    return min(FONT_SCALE_CHOICES, key=lambda item: abs(item[1] - value))[0]


def font_scale_for_label(label: str, fallback: float = 1.0) -> float:
    """档位名 → 比例；不认识的档位名给 ``fallback``。"""
    for name, value in FONT_SCALE_CHOICES:
        if name == label:
            return value
    return fallback


def font(size: int = SIZE_BODY, *, bold: bool = False) -> tuple:
    """常规字体（按当前 FONT_UI 与当前界面字号比例）。"""
    point = scaled(size)
    return (FONT_UI, point, "bold") if bold else (FONT_UI, point)


def mono(size: int = SIZE_SMALL, *, bold: bool = False) -> tuple:
    """等宽字体（日志用）。"""
    point = scaled(size)
    return (FONT_MONO, point, "bold") if bold else (FONT_MONO, point)


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


def settings_column_width() -> int:
    """左栏实际用的固定宽度：跟着界面字号与系统缩放一起放大。

    字号调大后 452px 装不下「导出目录」那一行的小字提示与输入框，会被截断；
    跟着比例放大才能保持版面比例不变。高分屏上同理 —— 150% 缩放时 452 像素
    在物理尺寸上只有原来的三分之二。
    """
    return int(round(SETTINGS_COLUMN_WIDTH * FONT_SCALE * DPI_SCALE))

#: 左栏滚动区的初始高度（窗口刚打开、还没拿到真实高度时用）。
#: 只是个起点：真实可视高度由布局决定，内容更高时会出现滚动条。
SETTINGS_VIEWPORT_HEIGHT = 430


def gap(steps: float = 1) -> int:
    """间距档位：``gap(1)=4``、``gap(2)=8``、``gap(3)=12``、``gap(4)=16``。

    高分屏上跟着系统缩放一起放大，否则 150% 时控件会挤在一起。
    """
    return int(round(GAP_UNIT * steps * DPI_SCALE))


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
    "CardMetric.TLabel",
    "Accent.TLabel",
    "Ok.TLabel",
    "Warn.TLabel",
    "Danger.TLabel",
    "CardAccent.TLabel",
    "CardOk.TLabel",
    "CardWarn.TLabel",
    "CardDanger.TLabel",
    "Badge.TLabel",
    "TButton",
    "Secondary.TButton",
    "Ghost.TButton",
    "Danger.TButton",
    "TEntry",
    "TCombobox",
    "ComboboxPopdownFrame",
    "TCheckbutton",
    "Card.TCheckbutton",
    "TRadiobutton",
    "Card.TRadiobutton",
    "Treeview",
    "Treeview.Heading",
    "TNotebook",
    "TNotebook.Tab",
    "TSeparator",
    "TLabelframe",
    "TLabelframe.Label",
    "Horizontal.TProgressbar",
    "Vertical.TScrollbar",
    "Horizontal.TScrollbar",
)


#: ``Palette`` 的全部颜色字段，按声明顺序。切换主题时用它把"旧配色 → 新配色"
#: 做成一张映射表（见 ``gui.py`` 的 ``_migrate_colors``），再改写已经画好的
#: 原生控件的颜色；少了任何一个字段，那一类控件换肤后就会留着旧底色。
PALETTE_FIELDS = tuple(field.name for field in fields(Palette) if field.name != "name")


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


def apply_titlebar(window: tk.Misc, pal: Palette | None = None) -> None:
    """在支持的 Windows 上给 Tk 的原生窗口框架同步主题。"""
    if sys.platform != "win32" or tk is None:
        return
    try:
        import ctypes
        from ctypes import wintypes

        # Tk 提供的是客户区 HWND。GA_ROOT 找到系统窗口框架，且不跟随 owner。
        get_ancestor = ctypes.windll.user32.GetAncestor
        get_ancestor.argtypes = (wintypes.HWND, wintypes.UINT)
        get_ancestor.restype = wintypes.HWND
        hwnd = get_ancestor(window.winfo_id(), 2)
        if hwnd:
            set_attribute = ctypes.windll.dwmapi.DwmSetWindowAttribute
            set_attribute.argtypes = (
                wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD
            )
            set_attribute.restype = ctypes.c_long
            # DWMWA_USE_IMMERSIVE_DARK_MODE。旧系统不支持时保留原生外观。
            dark = wintypes.BOOL((pal or PALETTE).name == "dark")
            set_attribute(hwnd, 20, ctypes.byref(dark), ctypes.sizeof(dark))
    except Exception:  # noqa: BLE001 —— 装饰性能力失败不能挡住开窗或测试替身
        pass


def _style_combobox_lists(root: tk.Misc, pal: Palette) -> None:
    """新候选列表读选项库，已经打开过的列表换肤时也同步。"""
    options = {
        "background": ("background", pal.surface),
        "foreground": ("foreground", pal.text),
        "selectBackground": ("selectbackground", pal.accent),
        "selectForeground": ("selectforeground", pal.on_accent),
        "borderWidth": ("borderwidth", 0),
        "highlightThickness": ("highlightthickness", 0),
        "selectBorderWidth": ("selectborderwidth", 0),
    }
    try:
        for pattern, (_, value) in options.items():
            root.option_add(f"*TCombobox*Listbox.{pattern}", value)
        # Tk 8.6 的候选列表不在 Tkinter 的子控件表里，直接查 Tcl。
        configure = [item for name, value in options.values() for item in (f"-{name}", value)]
        stack = root.winfo_children()
        while stack:
            widget = stack.pop()
            if isinstance(widget, ttk.Combobox):
                listbox = f"{widget}.popdown.f.l"
                if root.tk.getboolean(root.tk.call("winfo", "exists", listbox)):
                    root.tk.call(listbox, "configure", *configure)
            else:
                stack.extend(widget.winfo_children())
    except tk.TclError:  # pragma: no cover - 精简版 Tk 或其他平台私有布局
        pass


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

    _style_combobox_lists(root, pal)
    apply_titlebar(root, pal)
    if sys.platform == "win32" and not root.winfo_ismapped():
        # 主窗口初次绘制时，外层系统框架可能还没有创建。
        root.after_idle(apply_titlebar, root)

    # 基础
    style.configure(
        ".", background=pal.bg, foreground=pal.text, font=font(SIZE_BODY),
        bordercolor=pal.border, lightcolor=pal.border, darkcolor=pal.border,
        focuscolor=pal.accent, selectbackground=pal.accent, selectforeground=pal.on_accent,
    )
    style.map(
        ".", background=[], foreground=[("disabled", pal.disabled_text)],
        selectbackground=[("!focus", pal.accent_soft)],
        selectforeground=[("!focus", pal.text)],
    )
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
        foreground=pal.muted,
        font=font(SIZE_SMALL),
    )
    # 多行脚注使用卡片上较次要的文字档位。
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
        "CardMetric.TLabel", background=pal.surface, foreground=pal.text,
        font=font(SIZE_METRIC, bold=True),
    )
    for name, foreground in (
        ("Accent", pal.accent), ("Ok", pal.ok), ("Warn", pal.warn), ("Danger", pal.danger)
    ):
        style.configure(f"{name}.TLabel", background=pal.bg, foreground=foreground)
        style.configure(f"Card{name}.TLabel", background=pal.surface, foreground=foreground)
    style.configure(
        "Badge.TLabel",
        background=pal.accent_soft,
        foreground=pal.accent,
        font=font(SIZE_SMALL, bold=True),
        padding=(gap(1.5), gap(0.5)),
    )

    _style_buttons(style, pal)

    # 输入控件：悬停加强边界，焦点使用强调色，禁用态优先于其他状态。
    field_border = [("disabled", pal.border), ("focus", pal.accent), ("active", pal.border_strong)]
    field_colors = [("disabled", pal.disabled_surface), ("active", pal.surface_hover)]
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
        bordercolor=field_border,
        lightcolor=field_border,
        darkcolor=field_border,
        fieldbackground=field_colors,
        foreground=[("disabled", pal.disabled_text)],
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
        bordercolor=field_border,
        lightcolor=field_border,
        darkcolor=field_border,
        fieldbackground=field_colors,
        background=[("disabled", pal.disabled_surface), ("pressed", pal.accent_soft)],
        foreground=[("disabled", pal.disabled_text)],
        arrowcolor=[("disabled", pal.disabled_text), ("active", pal.accent)],
    )
    style.configure(
        "ComboboxPopdownFrame", background=pal.surface, bordercolor=pal.border_strong,
        lightcolor=pal.border_strong, darkcolor=pal.border_strong, borderwidth=1, relief="solid",
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
            focuscolor=pal.accent,
            focusthickness=1,
            font=font(SIZE_BODY),
            padding=(gap(0.5), gap(0.75)),
            indicatorsize=gap(3),
            indicatormargin=(gap(0.5), gap(0.25), gap(1.5), gap(0.25)),
            indicatorbackground=pal.surface_sunken,
            indicatorforeground=pal.on_accent,
            upperbordercolor=pal.border_strong,
            lowerbordercolor=pal.border_strong,
        )
        indicator_border = [
            ("disabled", pal.border),
            ("focus", pal.accent),
            ("selected", pal.accent),
            ("active", pal.accent),
        ]
        style.map(
            style_name,
            background=[("disabled", background), ("active", pal.surface_hover)],
            foreground=[("disabled", pal.disabled_text)],
            indicatorbackground=[
                ("disabled", pal.disabled_surface),
                ("selected", "pressed", pal.accent_active),
                ("selected", "active", pal.accent_hover),
                ("selected", pal.accent),
                ("pressed", pal.accent_soft),
            ],
            indicatorforeground=[("disabled", pal.disabled_text)],
            upperbordercolor=indicator_border,
            lowerbordercolor=indicator_border,
        )
    _style_indicators(root, style, pal)

    # 表格（监控列表）
    style.configure(
        "Treeview",
        background=pal.surface,
        fieldbackground=pal.surface,
        foreground=pal.text,
        bordercolor=pal.border,
        lightcolor=pal.surface,
        darkcolor=pal.surface,
        rowheight=max(gap(7), round(gap(7) * font_scale())),
        font=font(SIZE_BODY),
        borderwidth=0,
    )
    style.map(
        "Treeview",
        background=[("disabled", pal.disabled_surface), ("selected", pal.accent_soft)],
        foreground=[("disabled", pal.disabled_text), ("selected", pal.text)],
        bordercolor=[("focus", pal.accent)],
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

    # 设置分页和相邻区域之间用轻分隔。标签页保持相同高度以免切换时跳动。
    style.configure(
        "TNotebook", background=pal.bg, bordercolor=pal.border,
        lightcolor=pal.border, darkcolor=pal.border, borderwidth=1,
    )
    style.configure(
        "TNotebook.Tab", background=pal.bg, foreground=pal.muted,
        bordercolor=pal.border, lightcolor=pal.border, darkcolor=pal.border,
        focuscolor=pal.accent, padding=(gap(3), gap(1.5)),
        font=font(SIZE_SMALL, bold=True),
    )
    style.map(
        "TNotebook.Tab",
        padding=[],
        background=[
            ("disabled", pal.disabled_surface),
            ("selected", pal.surface),
            ("active", pal.surface_hover),
        ],
        foreground=[("disabled", pal.disabled_text), ("selected", pal.accent)],
        lightcolor=[("focus", pal.accent)],
    )
    style.configure("TSeparator", background=pal.border)

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

    # 细滚动条的滑块在悬停时加强对比。
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
            background=[("pressed", pal.accent), ("active", pal.muted)],
        )
    return style


def _style_buttons(style: ttk.Style, pal: Palette) -> None:
    # clam 的内层 focus 元素会绘制焦点框；主按钮用与文字相同的高对比色。
    button_common = dict(borderwidth=1, focusthickness=1, relief="flat", anchor="center", width=0)
    for name, base, fg, hover, pressed, border, focus, padding, face in (
        (
            "TButton", pal.accent, pal.on_accent, pal.accent_hover, pal.accent_active,
            pal.accent, pal.on_accent, (gap(4), gap(1.75)), font(SIZE_BODY, bold=True),
        ),
        (
            "Secondary.TButton", pal.surface_hover, pal.text, pal.surface_sunken, pal.border,
            pal.border, pal.accent, (gap(3), gap(1.25)), font(SIZE_SMALL, bold=True),
        ),
        (
            "Ghost.TButton", pal.bg, pal.muted, pal.surface_hover, pal.border,
            pal.bg, pal.accent, (gap(2), gap(1)), font(SIZE_SMALL),
        ),
        (
            "Danger.TButton", pal.danger_soft, pal.danger, pal.surface_hover, pal.border,
            pal.danger, pal.danger, (gap(3), gap(1.25)), font(SIZE_SMALL, bold=True),
        ),
    ):
        style.configure(
            name, background=base, foreground=fg, bordercolor=border, focuscolor=focus,
            lightcolor=base, darkcolor=base, padding=padding, font=face, **button_common,
        )
        disabled = pal.track if name == "TButton" else pal.disabled_surface
        fill = [("disabled", disabled), ("pressed", pressed), ("active", hover)]
        style.map(
            name, background=fill, lightcolor=fill, darkcolor=fill,
            foreground=[
                ("disabled", pal.disabled_text),
                ("active", pal.text if name == "Ghost.TButton" else fg),
            ],
            bordercolor=[("disabled", disabled), ("focus", focus), ("active", hover)],
        )


def _indicator_png(size: int, *, radio: bool, selected: bool, active: bool, disabled: bool,
                   pal: Palette) -> bytes:
    """画小型透明指示器；3 倍采样让圆角和勾号在各种系统 DPI 下保持清晰。"""
    fill = pal.accent if selected else pal.surface_sunken
    border = pal.accent if selected or active else pal.border_strong
    mark = pal.on_accent
    if disabled:
        fill, border, mark = pal.disabled_surface, pal.border, pal.disabled_text
    elif active:
        fill = pal.accent_hover if selected else pal.accent_soft
    colors = {name: tuple(bytes.fromhex(value[1:]))
              for name, value in (("fill", fill), ("border", border), ("mark", mark))}
    center = size / 2
    radius = min(2.5 * size / 12, center)
    image_width = size + gap(2)

    def inside(x: float, y: float, inset: float) -> bool:
        if radio:
            return (x - center) ** 2 + (y - center) ** 2 <= (center - inset) ** 2
        if not (inset <= x <= size - inset and inset <= y <= size - inset):
            return False
        corner = max(0.0, radius - inset)
        dx = max(inset + corner - x, 0.0, x - (size - inset - corner))
        dy = max(inset + corner - y, 0.0, y - (size - inset - corner))
        return dx * dx + dy * dy <= corner * corner

    def segment(x: float, y: float, start: tuple, end: tuple) -> bool:
        ax, ay = (coordinate * size for coordinate in start)
        bx, by = (coordinate * size for coordinate in end)
        dx, dy = bx - ax, by - ay
        position = min(1.0, max(0.0, ((x - ax) * dx + (y - ay) * dy) / (dx * dx + dy * dy)))
        return (x - ax - position * dx) ** 2 + (y - ay - position * dy) ** 2 <= (size * 0.065) ** 2

    raw = bytearray()
    for y in range(size):
        raw.append(0)  # PNG 每行使用无过滤器
        for x in range(image_width):
            samples = []
            for sy in range(3):
                for sx in range(3):
                    px, py = x + (sx + 0.5) / 3, y + (sy + 0.5) / 3
                    if not inside(px, py, 0.5):
                        continue
                    color = "fill" if inside(px, py, max(1.5, size / 9)) else "border"
                    if selected:
                        marked = ((px - center) ** 2 + (py - center) ** 2 <= (size * 0.19) ** 2
                                  if radio else segment(px, py, (0.25, 0.51), (0.44, 0.70))
                                  or segment(px, py, (0.44, 0.70), (0.77, 0.31)))
                        if marked:
                            color = "mark"
                    samples.append(colors[color])
            count = len(samples)
            channels = [round(sum(sample[channel] for sample in samples) / count)
                        for channel in range(3)] if count else [0, 0, 0]
            raw.extend((*channels, round(255 * count / 9)))

    def chunk(kind: bytes, value: bytes) -> bytes:
        return (struct.pack(">I", len(value)) + kind + value
                + struct.pack(">I", zlib.crc32(kind + value) & 0xFFFFFFFF))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", image_width, size, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw))
            + chunk(b"IEND", b""))


def _style_indicators(root: tk.Misc, style: ttk.Style, pal: Palette) -> None:
    """仅替换 ttk 的绘图元素，键盘、变量与禁用行为仍交给原生控件。"""
    owner = root._root()  # noqa: SLF001 —— 图像必须活到所属 Tcl 解释器销毁
    images = getattr(owner, "_xdao_indicator_images", None)
    if images is None:
        images = owner._xdao_indicator_images = {}  # noqa: SLF001
    states = {
        "normal": (False, False, False), "selected": (True, False, False),
        "active": (False, True, False), "selected_active": (True, True, False),
        "disabled": (False, False, True), "selected_disabled": (True, False, True),
    }
    for kind in ("Check", "Radio"):
        for state, (selected, active, disabled) in states.items():
            key = (kind, state)
            data = _indicator_png(
                max(10, gap(3)), radio=kind == "Radio", selected=selected,
                active=active, disabled=disabled, pal=pal,
            )
            if key in images:
                images[key].configure(data=data, format="png")
            else:
                images[key] = tk.PhotoImage(master=root, data=data, format="png")
        element = f"Xdao.{kind}.indicator"
        if element not in style.element_names():
            style.element_create(
                element, "image", images[kind, "normal"],
                ("disabled", "selected", images[kind, "selected_disabled"]),
                ("disabled", images[kind, "disabled"]),
                ("selected", "active", images[kind, "selected_active"]),
                ("active", images[kind, "active"]),
                ("selected", images[kind, "selected"]),
                sticky="",
            )
        style_name = "TCheckbutton" if kind == "Check" else "TRadiobutton"

        def replace(layout, replacement=element):
            result = []
            for name, options in layout:
                options = dict(options)
                if name.endswith(".indicator"):
                    name = replacement
                if "children" in options:
                    options["children"] = replace(options["children"], replacement)
                result.append((name, options))
            return result

        style.layout(style_name, replace(style.layout(style_name)))


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
    "PALETTE_NAMES",
    "PALETTES",
    "Palette",
    "apply_theme",
    "apply_titlebar",
    "dpi_scale",
    "font",
    "font_scale",
    "font_scale_for_label",
    "font_scale_label",
    "gap",
    "mono",
    "normalize_dpi_scale",
    "normalize_font_scale",
    "palette",
    "resolve_fonts",
    "scaled",
    "set_dpi_scale",
    "set_font_scale",
    "settings_column_width",
    "style_colors",
    "DPI_SCALE",
    "FONT_MONO",
    "FONT_SCALE",
    "FONT_SCALE_CHOICES",
    "FONT_SCALE_MAX",
    "FONT_SCALE_MIN",
    "FONT_UI",
    "STYLE_NAMES",
]
