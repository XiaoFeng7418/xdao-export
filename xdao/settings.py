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
                "use_cache": self.use_cache,
                "cache_dir": self.cache_dir,
                "watch_interval": self.watch_interval,
                "verify_cached": self.verify_cached,
                "watch_targets": list(self.watch_targets),
                "notify": self.notify,
                "notify_interval": self.notify_interval,
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
