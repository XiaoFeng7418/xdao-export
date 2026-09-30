"""本地配置：登录状态、导出偏好、网络选项与监控列表的持久化。

配置文件位置：``%APPDATA%\\xdao-export\\config.json``。
只保存 userhash 这类本机状态，不保存明文账号密码。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path


APP_DIR_NAME = "xdao-export"

# 默认值集中在这里，便于界面展示与重置。
DEFAULT_TIMEOUT = 20.0
DEFAULT_RETRIES = 2
DEFAULT_THROTTLE = 0.08
DEFAULT_WATCH_INTERVAL = 300.0
# 同一个串两次桌面通知的最小间隔：监控每轮都可能报"有更新"，不节流会刷屏。
DEFAULT_NOTIFY_INTERVAL = 900.0

# ---------- PDF 纸张 / 边距的默认值 ----------
# 这组默认值等价于 v0.7.0 的行为：跟随网页样式（纸张与页边距都交给浏览器）、
# 缩放 100%、打印背景、全部页码。老配置文件里没有这些键时必须落在这里，
# 少一项都会改变老用户的成品外观。
DEFAULT_PDF_PAPER = "default"
DEFAULT_PDF_ORIENTATION = "portrait"
DEFAULT_PDF_MARGIN = "default"
DEFAULT_PDF_MARGIN_MM = ""
# 缩放存字符串：浮点写进 JSON 再读回来会出现 0.30000000000000004 这类尾数。
DEFAULT_PDF_SCALE = "1.0"
DEFAULT_PDF_BACKGROUND = True
DEFAULT_PDF_PAGE_RANGES = ""


def app_config_dir() -> Path:
    """本机配置目录。"""
    base = os.environ.get("APPDATA")
    if base:
        return Path(base) / APP_DIR_NAME
    return Path.home() / f".{APP_DIR_NAME}"


def _default_config_path() -> Path:
    # 优先存在用户自己的配置目录，避免污染程序目录。
    return app_config_dir() / "config.json"


@dataclass
class AppSettings:
    """本机配置。"""

    userhash: str | None = None
    output_dir: str | None = None

    # ---------- 网络 ----------
    proxy: str = ""
    timeout: float = DEFAULT_TIMEOUT
    retries: int = DEFAULT_RETRIES
    throttle: float = DEFAULT_THROTTLE

    # ---------- 导出偏好 ----------
    format_key: str = "html"
    scope: str = "all"
    filename_template: str = ""
    include_hashes: str = ""
    # EPUB 的图片处理方式：embed 内嵌 / url 仅链接 / drop 丢弃
    image_mode: str = "embed"
    # 导出 PDF 时使用的浏览器；留空表示自动探测 Chrome / Edge
    pdf_browser: str = ""

    # ---------- PDF 纸张 / 页边距 ----------
    # 归一化全部交给 xdao/pdf_opts.py（界面与命令行同一套规则）；
    # 这里只负责持久化，坏值一律回落到上面那组默认值。
    pdf_paper: str = DEFAULT_PDF_PAPER
    pdf_orientation: str = DEFAULT_PDF_ORIENTATION
    pdf_margin: str = DEFAULT_PDF_MARGIN
    # 自定义页边距（毫米）；空串表示没设、用 pdf_margin 那个预设
    pdf_margin_mm: str = DEFAULT_PDF_MARGIN_MM
    pdf_scale: str = DEFAULT_PDF_SCALE
    pdf_background: bool = DEFAULT_PDF_BACKGROUND
    # 只导出这些页码，写法例如 "1-3,5"；空串表示全部
    pdf_page_ranges: str = DEFAULT_PDF_PAGE_RANGES

    # ---------- 缓存 ----------
    use_cache: bool = True
    cache_dir: str = ""

    # ---------- 监控 ----------
    watch_interval: float = DEFAULT_WATCH_INTERVAL
    verify_cached: bool = False
    watch_targets: list[dict] = field(default_factory=list)
    # 监控到更新时弹桌面通知；同一串的提醒间隔（秒），避免刷屏
    notify: bool = True
    notify_interval: float = DEFAULT_NOTIFY_INTERVAL

    # ---------- 界面 ----------
    # 配色：light / dark（见 xdao/theme.py 的 PALETTES）；不认识的取值回落到 light
    theme_name: str = "light"

    extra: dict = field(default_factory=dict)

    _path: Path = field(default_factory=_default_config_path, repr=False, compare=False)

    # ---------- 读写 ----------

    @classmethod
    def load(cls) -> "AppSettings":
        settings = cls()
        try:
            if settings._path.exists():
                data = json.loads(settings._path.read_text(encoding="utf-8"))
                settings._apply(data)
        except Exception:
            # 配置损坏时静默回退到默认值，不影响启动。
            pass
        return settings

    def _apply(self, data: dict) -> None:
        if not isinstance(data, dict):
            return
        self.userhash = data.get("userhash") or None
        self.output_dir = data.get("output_dir") or None
        self.proxy = str(data.get("proxy") or "")
        self.timeout = _as_float(data.get("timeout"), DEFAULT_TIMEOUT)
        self.retries = _as_int(data.get("retries"), DEFAULT_RETRIES)
        self.throttle = _as_float(data.get("throttle"), DEFAULT_THROTTLE)
        self.format_key = str(data.get("format_key") or "html")
        self.scope = str(data.get("scope") or "all")
        self.filename_template = str(data.get("filename_template") or "")
        self.include_hashes = str(data.get("include_hashes") or "")
        image_mode = str(data.get("image_mode") or "embed")
        self.image_mode = image_mode if image_mode in ("embed", "url", "drop") else "embed"
        self.pdf_browser = str(data.get("pdf_browser") or "")
        # 逐字段解析：老配置没有这些键 → 取默认值（＝ v0.7.0 的成品外观）；
        # 有键但值坏 → 同样回落默认，绝不让读配置抛异常。
        self.pdf_paper = _pdf_choice(data.get("pdf_paper"), DEFAULT_PDF_PAPER, "normalize_paper")
        self.pdf_orientation = _pdf_choice(
            data.get("pdf_orientation"), DEFAULT_PDF_ORIENTATION, "normalize_orientation"
        )
        self.pdf_margin = _pdf_choice(
            data.get("pdf_margin"), DEFAULT_PDF_MARGIN, "normalize_margin"
        )
        self.pdf_margin_mm = _pdf_margin_mm(data.get("pdf_margin_mm"), DEFAULT_PDF_MARGIN_MM)
        self.pdf_scale = _pdf_scale(data.get("pdf_scale"), DEFAULT_PDF_SCALE)
        self.pdf_background = _as_bool(data.get("pdf_background"), DEFAULT_PDF_BACKGROUND)
        self.pdf_page_ranges = _pdf_page_ranges(
            data.get("pdf_page_ranges"), DEFAULT_PDF_PAGE_RANGES
        )
        self.use_cache = bool(data.get("use_cache", True))
        self.cache_dir = str(data.get("cache_dir") or "")
        self.watch_interval = _as_float(data.get("watch_interval"), DEFAULT_WATCH_INTERVAL)
        self.verify_cached = bool(data.get("verify_cached", False))
        raw_targets = data.get("watch_targets")
        if isinstance(raw_targets, list):
            self.watch_targets = [t for t in raw_targets if isinstance(t, dict)]
        self.notify = bool(data.get("notify", True))
        self.notify_interval = _as_float(
            data.get("notify_interval"), DEFAULT_NOTIFY_INTERVAL
        )
        # 配色的合法值由 theme.PALETTES 说了算；写了不认识的名字就当没写，
        # 免得界面上出现一个空白选项。
        from .theme import PALETTES

        theme_name = str(data.get("theme") or "light")
        self.theme_name = theme_name if theme_name in PALETTES else "light"
        self.extra = data.get("extra") or {}

    def save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "userhash": self.userhash,
                "output_dir": self.output_dir,
                "proxy": self.proxy,
                "timeout": self.timeout,
                "retries": self.retries,
                "throttle": self.throttle,
                "format_key": self.format_key,
                "scope": self.scope,
                "filename_template": self.filename_template,
                "include_hashes": self.include_hashes,
                "image_mode": self.image_mode,
                "pdf_browser": self.pdf_browser,
                "pdf_paper": self.pdf_paper,
                "pdf_orientation": self.pdf_orientation,
                "pdf_margin": self.pdf_margin,
                "pdf_margin_mm": self.pdf_margin_mm,
                "pdf_scale": self.pdf_scale,
                "pdf_background": self.pdf_background,
                "pdf_page_ranges": self.pdf_page_ranges,
                "use_cache": self.use_cache,
                "cache_dir": self.cache_dir,
                "watch_interval": self.watch_interval,
                "verify_cached": self.verify_cached,
                "watch_targets": list(self.watch_targets),
                "notify": self.notify,
                "notify_interval": self.notify_interval,
                "theme": self.theme_name,
                "extra": self.extra,
            }
            self._path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception:
            # 保存失败不应导致程序崩溃。
            pass

    # ---------- 便捷方法 ----------

    @property
    def config_path(self) -> Path:
        return self._path

    def resolved_cache_dir(self, output_dir: Path | str | None = None) -> Path:
        """实际使用的缓存目录：显式设置优先，其次跟随导出目录，最后回落到配置目录。"""
        from .cache import default_cache_dir

        if self.cache_dir.strip():
            return Path(self.cache_dir.strip())
        if output_dir:
            return Path(output_dir) / ".cache"
        return default_cache_dir()

    def parse_hashes(self, raw: str | None = None) -> list[str]:
        """把「饼干筛选」文本框解析成列表，支持逗号、空格、换行分隔。"""
        text = self.include_hashes if raw is None else raw
        parts = [p.strip() for p in str(text or "").replace(",", " ").split()]
        seen: set[str] = set()
        result: list[str] = []
        for part in parts:
            if part and part not in seen:
                seen.add(part)
                result.append(part)
        return result


def _as_float(value, fallback: float) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return fallback
    return result if result > 0 else fallback


def _as_int(value, fallback: int) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError):
        return fallback
    return result if result >= 0 else fallback


def _as_bool(value, fallback: bool) -> bool:
    """宽容地读布尔项。

    JSON 里本该是 true/false，但配置是纯文本、用户会手改，所以 "0"/"否"
    这类写法也认。认不出来（含空串）时回落到 fallback。
    """
    if value is None:
        return fallback
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "on", "是", "开"):
        return True
    if text in ("0", "false", "no", "off", "否", "关"):
        return False
    return fallback


def _pdf_normalizer(name: str):
    """取 :mod:`xdao.pdf_opts` 里的归一化函数；取不到就返回 None。

    归一化规则只有一套（pdf_opts），配置层不另写一遍 —— 但也绝不因为
    它取不到/抛异常就让读配置失败，那种情况按"坏值"处理即可。
    """
    try:
        from . import pdf_opts
    except Exception:  # noqa: BLE001 —— 归一化模块缺失不该挡住启动
        return None
    return getattr(pdf_opts, name, None)


def _pdf_choice(value, fallback: str, normalizer_name: str) -> str:
    """纸张 / 方向 / 页边距这类枚举项：认不出来一律回落默认值。"""
    text = str(value if value is not None else "").strip()
    if not text:
        return fallback
    normalize = _pdf_normalizer(normalizer_name)
    if normalize is None:
        return fallback
    try:
        normalized = normalize(text)
    except Exception:  # noqa: BLE001 —— 坏值回落默认，不许把异常抛给启动流程
        return fallback
    normalized_text = "" if normalized is None else str(normalized).strip()
    # normalize_* 遇到不认识的值时可能静默给回默认值：那和"坏值"同解。
    if not normalized_text or normalized_text == fallback:
        return fallback
    return normalized_text


def _pdf_margin_mm(value, fallback: str) -> str:
    """自定义页边距（毫米）：存字符串；非法值回落（空串＝没设）。"""
    text = str(value if value is not None else "").strip()
    if not text:
        return fallback
    try:
        from .pdf_opts import require_valid_pdf_options

        # margin="" 是「这一项没填」的哨兵：pdf_opts 里显式传 "default"
        # 会被当成「用户直接写了毫米数」而报错（pdf_opts.py:481-485）。
        options = require_valid_pdf_options(margin="", margin_mm=text)
    except Exception:  # noqa: BLE001
        return fallback
    if getattr(options, "margin_mm", None) is None:
        return fallback
    return text


def _pdf_scale(value, fallback: str) -> str:
    """缩放：存字符串避免浮点误差；非数字或越界一律回落默认。"""
    text = str(value if value is not None else "").strip()
    if not text:
        return fallback
    try:
        from .pdf_opts import SCALE_MAX, SCALE_MIN

        number = float(text)
    except Exception:  # noqa: BLE001
        return fallback
    # 先按写进来的原值判范围：normalize_scale 可能把越界值悄悄夹进区间，
    # 那样坏值就会伪装成合法值被存下来。
    if not (SCALE_MIN <= number <= SCALE_MAX):
        return fallback
    normalize = _pdf_normalizer("normalize_scale")
    if normalize is None:
        return fallback
    try:
        normalize(text)
    except Exception:  # noqa: BLE001
        return fallback
    return text


def _pdf_page_ranges(value, fallback: str) -> str:
    """页码范围（例如 "1-3,5"）：通过校验就保留原文，坏值回落空串。"""
    text = str(value if value is not None else "").strip()
    if not text:
        return fallback
    try:
        from .pdf_opts import require_valid_pdf_options

        # 同上：margin="" 表示「边距这一项没填」。
        options = require_valid_pdf_options(margin="", page_ranges=text)
    except Exception:  # noqa: BLE001 —— 页码写错不该让程序起不来
        return fallback
    normalized = getattr(options, "page_ranges", text)
    if isinstance(normalized, str) and normalized.strip():
        return normalized.strip()
    # 归一化结果不是字符串（例如已解析成集合）时，校验通过就保留用户原文。
    return text
