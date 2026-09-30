"""PDF 导出选项：纸张、方向、边距、缩放与页码范围的纯逻辑。

界面、命令行与导出器共用这一份实现：这里只做「校验 → 归一化 → 换算」，
不导入任何界面库，也不碰浏览器，因此可以单独测试（CI 的 Linux 矩阵也能跑）。

贯穿全文件的约定：

1. **静默回落**：``normalize_*`` 与 :func:`from_values` 遇到不认识的值一律回落到默认值。
   配置文件、旧版本界面留下的脏数据不该让整条导出流程挂掉。
2. **只有命令行入口报错**：:func:`require_valid_pdf_options` 才把非法值变成
   :class:`PdfOptionsError`，而且消息要说清「支持什么、收到什么」，能直接照着改。
3. **能交给页面就交给页面**：纸张保持 ``default`` 时输出 ``preferCSSPageSize: True``，
   让排版沿用页面自己的 ``@page size``（与旧版命令行渲染的结果一致）；纸张一旦显式指定，
   就**绝不**再带这个键 —— 带了浏览器会改以页面 CSS 为准，用户选的纸张白选（真机实测还会
   时对时错）。这个键只管纸张，显式边距与它可以并存。
4. **毫米数优先**：``margin_mm`` 是自定义数值，优先级高于预设名，也高于 ``default``；
   否则「自定义 10mm」会被当成「跟随网页样式」而悄悄丢掉。
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

# ---------------- 常量：界面下拉框、命令行校验与导出器共用 ----------------

# 单位英寸。近似值：真实尺寸由浏览器换算。
# A4 取 8.27×11.69 是为了与 CSS @page 的 A4 对齐（真机实测 MediaBox [0 0 594.95996 841.91998]）。
PAPER_SIZES: dict[str, tuple[float, float]] = {
    "a4": (8.27, 11.69),
    "a3": (11.69, 16.54),
    "a5": (5.83, 8.27),
    "letter": (8.5, 11.0),
    "legal": (8.5, 14.0),
}

# 显示名与取值分开：界面显示中文，存进配置的始终是这里的键（a4/letter/…）。
PAPER_LABELS = {
    "default": "跟随网页样式（推荐）",
    "a4": "A4（210×297mm）",
    "a3": "A3（297×420mm）",
    "a5": "A5（148×210mm）",
    "letter": "Letter（8.5×11in）",
    "legal": "Legal（8.5×14in）",
}

MARGIN_PRESETS = {
    "default": "跟随网页样式（推荐）",
    "none": "无边距",
    "narrow": "窄（10mm）",
    "normal": "普通（18mm）",
    "wide": "宽（25mm）",
}

MARGIN_PRESETS_MM = {"none": 0.0, "narrow": 10.0, "normal": 18.0, "wide": 25.0}

ORIENTATION_LABELS = {"portrait": "纵向", "landscape": "横向"}

SCALE_MIN, SCALE_MAX, SCALE_DEFAULT = 0.5, 2.0, 1.0

# 自定义边距的允许范围（毫米）。超出就截断，免得用户把正文挤出纸张。
MARGIN_MM_MIN, MARGIN_MM_MAX = 0.0, 50.0
MM_PER_INCH = 25.4

# 页码写法：1 / 2-5 / 1-3,5,7-9
PAGE_RANGES_RE = re.compile(r"^[0-9]+(-[0-9]+)?(,[0-9]+(-[0-9]+)?)*$")

DEFAULT_PAPER = "default"
DEFAULT_ORIENTATION = "portrait"
DEFAULT_MARGIN = "default"
DEFAULT_PAGE_RANGES = ""

# 「跟随默认 / 不设置」的通用哨兵：纸张与边距的合法取值里本来就有它，
# 方向没有这个取值，但同样接受它当「没特别要求」（老配置里可能就这么存着）。
_UNSET = "default"

# 合法取值的完整集合（含 default），报错信息里的清单也从这里生成，不会再写第二遍。
PAPER_KEYS = (DEFAULT_PAPER, *PAPER_SIZES)
MARGIN_KEYS = tuple(MARGIN_PRESETS)

# 报错信息的前半段：写死一份会被改漏，统一由常量拼出来。
_PAPER_HELP = "纸张只支持 " + "/".join(PAPER_KEYS)
_ORIENTATION_HELP = "方向只支持 " + "/".join(ORIENTATION_LABELS)
_MARGIN_HELP = (
    "边距只支持 "
    + "/".join(MARGIN_KEYS)
    + f" 或 {MARGIN_MM_MIN:g}~{MARGIN_MM_MAX:g} 的毫米数"
)
_SCALE_HELP = f"缩放只支持 {SCALE_MIN}~{SCALE_MAX} 之间的数字（例如 1.2）"
_PAGES_HELP = "页码只能是 1-3,5,7-9 这样的写法"


# ---------------- 小工具 ----------------


def _key(value) -> str:
    """整理成可比对的键：容忍 ``None``/非字符串、首尾空白与大小写。"""
    if not isinstance(value, str):
        return ""
    return value.strip().lower()


def _is_blank(value) -> bool:
    """是否「没填」：``None``、空串、纯空白。

    命令行里「没填这一项」和「填错了」必须分开：前者按默认走，后者要报错。
    """
    return value is None or (isinstance(value, str) and not value.strip())


def _plain_float(value) -> float | None:
    """能当数字看就返回 float，否则 ``None``；``NaN`` 也算 ``None``（不可比较）。"""
    if _is_blank(value):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(number) else number


def _as_bool(value, fallback: bool = True) -> bool:
    """容错布尔：兼容配置里存的 ``"false"``/``"0"``/``"no"`` 这类字符串。"""
    if isinstance(value, bool):
        return value
    if value is None:
        return fallback
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("1", "true", "yes", "on", "y", "t"):
            return True
        if text in ("0", "false", "no", "off", "n", "f", ""):
            return False
    return fallback


def _range_parts(text: str) -> list[tuple[int, int]] | None:
    """把已经归一化的页码串拆成 ``(起, 止)`` 列表；写法不对 → ``None``。

    除了正则，还要挡住两种「能过正则但没意义」的写法：页码从 1 开始（``0`` 不是页），
    以及倒序区间（``5-2``）—— 后者交给浏览器只会得到空文档。
    """
    if not PAGE_RANGES_RE.match(text):
        return None
    parts: list[tuple[int, int]] = []
    for chunk in text.split(","):
        start_text, dash, end_text = chunk.partition("-")
        start = int(start_text)
        if start < 1:
            return None
        if not dash:
            parts.append((start, start))
            continue
        end = int(end_text)
        if end < start:
            return None
        parts.append((start, end))
    return parts


# ---------------- 归一化：非法值静默回落默认值 ----------------


def normalize_paper(value) -> str:
    """纸张名；不认识的（含 ``None``）回落 ``"default"``（跟随网页样式）。"""
    key = _key(value)
    return key if key in PAPER_KEYS else DEFAULT_PAPER


def normalize_orientation(value) -> str:
    """纸张方向；不认识的回落 ``"portrait"``。"""
    key = _key(value)
    return key if key in ORIENTATION_LABELS else DEFAULT_ORIENTATION


def normalize_margin(value) -> str:
    """边距预设名；不认识的回落 ``"default"``（跟随网页样式）。

    直接写毫米数（``"18"``）在这里同样算「不认识」，那属于 :func:`from_values`
    的 ``margin_mm`` 槽位（见 :func:`_resolve_margin`）。
    """
    key = _key(value)
    return key if key in MARGIN_KEYS else DEFAULT_MARGIN


def normalize_scale(value) -> float:
    """缩放倍数：非数字/``NaN`` 回落 1.0，其余数值截断到 ``SCALE_MIN..SCALE_MAX``。"""
    number = _plain_float(value)
    if number is None:
        return SCALE_DEFAULT
    return min(SCALE_MAX, max(SCALE_MIN, number))


def normalize_page_ranges(value) -> str:
    """页码串：去掉空白后整体符合 ``PAGE_RANGES_RE`` 才留下，否则回落空串（整篇）。

    容忍用户从别处粘来的空格（``'1-3, 5'`` → ``'1-3,5'``）和全角空格；
    页码从 1 开始，倒序区间（``5-2``）与格式错误一样算非法。
    """
    if not isinstance(value, str):
        return DEFAULT_PAGE_RANGES
    text = "".join(value.split())
    if not text or _range_parts(text) is None:
        return DEFAULT_PAGE_RANGES
    return text


def clamp_margin_mm(value) -> float | None:
    """自定义边距的毫米数：非数字/``NaN`` → ``None``；数值截断到 ``0~50``。"""
    number = _plain_float(value)
    if number is None:
        return None
    return min(MARGIN_MM_MAX, max(MARGIN_MM_MIN, number))


def _plain_margin_mm(value) -> float | None:
    """``margin`` 槽位里直接写数字时的解析（``"18"`` → 18.0）；不是数字 → ``None``。"""
    if _is_blank(value):
        return None
    return clamp_margin_mm(value)


def _resolve_margin(margin, margin_mm) -> tuple[str, float | None]:
    """把「预设名 / 毫米数」两种写法合并成 ``(预设名, 毫米数)``。

    ``margin`` 里直接写数字只是 ``margin_mm`` 的简写；两者都填时以 ``margin_mm`` 为准。
    """
    key = normalize_margin(margin)
    custom = clamp_margin_mm(margin_mm)
    if custom is None and key == DEFAULT_MARGIN:
        custom = _plain_margin_mm(margin)
    return key, custom


# ---------------- 换算 ----------------


def margin_inches(margin: str, margin_mm: float | None) -> float | None:
    """边距的英寸值；``None`` 表示「跟随网页样式」（交给页面自己的 ``@page`` 规则）。

    ``margin_mm`` 有值就用它（截断 0~50 后换算），否则查 :data:`MARGIN_PRESETS_MM`。
    """
    custom = clamp_margin_mm(margin_mm)
    if custom is not None:
        return custom / MM_PER_INCH
    key = normalize_margin(margin)
    if key == DEFAULT_MARGIN:
        return None
    return MARGIN_PRESETS_MM[key] / MM_PER_INCH


def apply_page_ranges(pages: str, total: int) -> set[int] | None:
    """把页码串落到具体页号集合（1-based）。

    ``None`` 表示「不限制」或「写错了」：空串、非法写法、倒序区间、``total <= 0``
    （页数未知）都算在内 —— 这几种情况调用方都按「整篇导出」处理，
    不要拿一个空集合去当「用户一页都不要」。

    超出 ``total`` 的页号直接丢弃，因此**可能**返回空集合（用户写的页号整段都不存在）。
    """
    try:
        total_pages = int(total)
    except (TypeError, ValueError):
        return None
    if total_pages <= 0:
        return None
    text = normalize_page_ranges(pages)
    if not text:
        return None
    parts = _range_parts(text)
    if parts is None:
        return None
    wanted: set[int] = set()
    for start, end in parts:
        wanted.update(range(start, end + 1))
    return {page for page in wanted if 1 <= page <= total_pages}


# ---------------- 值对象 ----------------


@dataclass(frozen=True)
class PdfOptions:
    """一次 PDF 导出的排版选项。

    构造时就把每个字段归一化（见 :meth:`__post_init__`），
    因此不管是界面、配置文件还是命令行造出来的对象，一定只含合法值。
    """

    paper: str = DEFAULT_PAPER
    orientation: str = DEFAULT_ORIENTATION
    margin: str = DEFAULT_MARGIN
    margin_mm: float | None = None
    scale: float = SCALE_DEFAULT
    background: bool = True
    page_ranges: str = DEFAULT_PAGE_RANGES

    def __post_init__(self) -> None:
        # frozen 数据类里只能用 object.__setattr__ 赋值。
        # 在构造处统一归一化，后面 to_cdp_params()/describe() 就不必再防脏数据。
        object.__setattr__(self, "paper", normalize_paper(self.paper))
        object.__setattr__(self, "orientation", normalize_orientation(self.orientation))
        object.__setattr__(self, "margin", normalize_margin(self.margin))
        object.__setattr__(self, "margin_mm", clamp_margin_mm(self.margin_mm))
        object.__setattr__(self, "scale", normalize_scale(self.scale))
        object.__setattr__(self, "background", _as_bool(self.background))
        object.__setattr__(self, "page_ranges", normalize_page_ranges(self.page_ranges))

    @property
    def is_default(self) -> bool:
        """纸张/方向/边距/缩放都没动过（此时可走旧的命令行渲染路径，输出逐字节一致）。

        ``background`` 与 ``page_ranges`` **不参与**判断：旧路径本来就不接受它们。
        所以调用方在走旧路径前要么自己确认这两项也是默认的，
        要么直接用 :attr:`needs_cdp`。
        """
        return (
            self.paper == DEFAULT_PAPER
            and self.orientation == DEFAULT_ORIENTATION
            and self.margin == DEFAULT_MARGIN
            and self.margin_mm is None
            and self.scale == SCALE_DEFAULT
        )

    @property
    def needs_cdp(self) -> bool:
        """是否必须走 CDP 渲染（``Page.printToPDF`` 参数）而不是旧的命令行路径。

        比 ``not is_default`` 多看了 ``page_ranges``：只填页码时旧路径同样表达不了，
        用它才能避免「用户填了页码却被静默忽略」。
        """
        return (not self.is_default) or bool(self.page_ranges)

    def to_cdp_params(self) -> dict:
        """转成 CDP ``Page.printToPDF`` 的参数，只带与默认不同的项。

        一句话规则：``preferCSSPageSize`` **只在纸张走 ``default`` 时**出现。

        原因（真机实测，不是推理）：这个键让浏览器改以页面 CSS 的 ``@page size`` 为准，
        所以纸张一旦显式指定就**绝不能**带它 —— 带了用户选的 A3 会被静默出成 A4，
        而且时对时错（取决于 ``@page`` 规则有没有解析完）。反过来，纸张走默认时又必须带它，
        否则页面里写的 ``@page { size: A4 }`` 不被采纳，产物会退成浏览器默认纸张（Letter
        612×792pt，实测），「跟随网页样式」就成了假话。

        它管不到边距：显式边距与这个键可以并存，真机实测四边 ``margin*``（18mm，容量
        261mm < 275mm 的正文）照样翻页生效，所以边距该给就给。
        """
        params: dict = {}

        if self.paper != DEFAULT_PAPER:
            width, height = PAPER_SIZES[self.paper]
            if self.orientation == "landscape":
                # 横向＝把纸转 90°，宽高互换。
                width, height = height, width
            params["paperWidth"] = width
            params["paperHeight"] = height
        else:
            params["preferCSSPageSize"] = True

        inches = margin_inches(self.margin, self.margin_mm)
        if inches is not None:
            params["marginTop"] = inches
            params["marginBottom"] = inches
            params["marginLeft"] = inches
            params["marginRight"] = inches

        if self.scale != SCALE_DEFAULT:
            params["scale"] = self.scale
        # 背景默认要打出来，否则深色主题的帖子在 PDF 里会变成一片白。
        params["printBackground"] = self.background
        if self.page_ranges:
            params["pageRanges"] = self.page_ranges
        return params

    def describe(self) -> str:
        """人话摘要，给界面上的「当前设置：…」用；全部默认时是「跟随网页样式」。"""
        parts: list[str] = []
        if self.paper != DEFAULT_PAPER:
            parts.append(f"{_paper_name(self.paper)} {ORIENTATION_LABELS[self.orientation]}")
        elif self.orientation != DEFAULT_ORIENTATION:
            parts.append(ORIENTATION_LABELS[self.orientation])
        if self.margin_mm is not None:
            parts.append(f"边距 {self.margin_mm:g}mm")
        elif self.margin == "none":
            parts.append(MARGIN_PRESETS["none"])
        elif self.margin != DEFAULT_MARGIN:
            parts.append(f"边距 {MARGIN_PRESETS[self.margin]}")
        if self.scale != SCALE_DEFAULT:
            parts.append(f"缩放 {round(self.scale * 100)}%")
        if not self.background:
            parts.append("不打印背景")
        if self.page_ranges:
            parts.append(f"页码 {self.page_ranges}")
        return " · ".join(parts) if parts else "跟随网页样式"

    @classmethod
    def from_settings(cls, settings) -> "PdfOptions":
        """从 :class:`~xdao.settings.AppSettings` 读 PDF 相关字段。

        缺字段（旧配置、测试替身）或取到 ``None`` 都回落默认值，**不抛异常**：
        配置脏了不该让导出整条挂掉。
        """
        return from_values(
            paper=_attr(settings, "pdf_paper", DEFAULT_PAPER),
            orientation=_attr(settings, "pdf_orientation", DEFAULT_ORIENTATION),
            margin=_attr(settings, "pdf_margin", DEFAULT_MARGIN),
            margin_mm=_attr(settings, "pdf_margin_mm", None),
            scale=_attr(settings, "pdf_scale", SCALE_DEFAULT),
            background=_attr(settings, "pdf_background", True),
            page_ranges=_attr(settings, "pdf_page_ranges", DEFAULT_PAGE_RANGES),
        )


def _attr(obj, name: str, fallback):
    """读配置字段：缺失或为 ``None`` 一律给 ``fallback``（``margin_mm`` 的 fallback 也是 None）。"""
    try:
        value = getattr(obj, name, fallback)
    except Exception:  # pragma: no cover - 只防住行为古怪的配置替身
        return fallback
    return fallback if value is None else value


def _paper_name(paper: str) -> str:
    """纸张短名：从显示名里取「（」之前的部分（``A3（297×420mm）`` → ``A3``）。"""
    return PAPER_LABELS[paper].partition("（")[0]


def from_values(
    paper="default",
    orientation="portrait",
    margin="default",
    margin_mm=None,
    scale=1.0,
    background=True,
    page_ranges="",
) -> PdfOptions:
    """按「非法值静默回落默认」的规则造一个 :class:`PdfOptions`。

    这里显式过一遍 ``normalize_*``（构造器里还有一层兜底），
    好让调用方一眼看出每个字段各自是怎么被校验的。
    """
    margin, margin_mm = _resolve_margin(margin, margin_mm)
    return PdfOptions(
        paper=normalize_paper(paper),
        orientation=normalize_orientation(orientation),
        margin=margin,
        margin_mm=margin_mm,
        scale=normalize_scale(scale),
        background=_as_bool(background),
        page_ranges=normalize_page_ranges(page_ranges),
    )


# 模块级别名：``pdf_opts.from_settings(settings)`` 与 ``PdfOptions.from_settings(settings)``
# 等价，接线时不会因为写法不同而踩空。
from_settings = PdfOptions.from_settings


# ---------------- 命令行入口 ----------------


class PdfOptionsError(Exception):
    """PDF 选项非法（命令行入口专用，消息可直接展示给用户）。"""


def _require_margin_number(value) -> float:
    """命令行校验：边距必须是 ``0~50`` 之间的数字，否则报错（越界也算错，不静默截断）。"""
    number = _plain_float(value)
    if number is None or not MARGIN_MM_MIN <= number <= MARGIN_MM_MAX:
        raise PdfOptionsError(f"{_MARGIN_HELP}，收到 {value!r}")
    return number


def require_valid_pdf_options(
    paper="default",
    orientation="portrait",
    margin="default",
    margin_mm=None,
    scale=1.0,
    page_ranges="",
) -> PdfOptions:
    """命令行入口：与 :func:`from_values` 同样归一化，但非法值**报错**而不是静默回落。

    空串 / ``None`` / ``"default"`` 视为「没填这一项」，按默认值处理 —— 命令行与配置文件里
    「没设置」和「设置错了」必须分开，否则用户永远不知道自己的参数被忽略了。

    ``margin`` 槽位里也可以直接写毫米数（``"18"``，等价于 ``margin_mm=18``）。
    """
    paper_key = _key(paper)
    if paper_key and paper_key not in PAPER_KEYS:
        raise PdfOptionsError(f"{_PAPER_HELP}，收到 {paper!r}")

    orientation_key = _key(orientation)
    if orientation_key == _UNSET:
        # 纸张与边距都把 "default" 当合法取值，方向没有这个取值，
        # 但同样按「没要求」处理，别让三个参数里唯独方向多一条报错规则。
        orientation_key = ""
    if orientation_key and orientation_key not in ORIENTATION_LABELS:
        raise PdfOptionsError(f"{_ORIENTATION_HELP}，收到 {orientation!r}")

    margin_text = _key(margin)
    margin_number: float | None = None
    if margin_text in MARGIN_KEYS:
        # 显式写了预设名：default 也算合法取值（它是「跟随网页样式」，不是毫米数）。
        margin_key = margin_text
    elif _is_blank(margin):
        margin_key = DEFAULT_MARGIN
    else:
        # 既不是预设名又填了东西：只可能是「直接写了毫米数」，否则就是填错了。
        margin_key = DEFAULT_MARGIN
        margin_number = _require_margin_number(margin)

    mm_value = margin_number
    if not _is_blank(margin_mm):
        # 两个地方都写了毫米数时以 margin_mm 为准（它是专门的槽位）。
        mm_value = _require_margin_number(margin_mm)

    scale_value = SCALE_DEFAULT
    if not _is_blank(scale):
        parsed = _plain_float(scale)
        if parsed is None or not SCALE_MIN <= parsed <= SCALE_MAX:
            raise PdfOptionsError(f"{_SCALE_HELP}，收到 {scale!r}")
        scale_value = parsed

    if not _is_blank(page_ranges) and not normalize_page_ranges(page_ranges):
        raise PdfOptionsError(f"{_PAGES_HELP}，收到 {page_ranges!r}")

    return PdfOptions(
        paper=paper_key or DEFAULT_PAPER,
        orientation=orientation_key or DEFAULT_ORIENTATION,
        margin=margin_key or DEFAULT_MARGIN,
        margin_mm=mm_value,
        scale=scale_value,
        page_ranges=normalize_page_ranges(page_ranges),
    )
