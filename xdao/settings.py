"""本地配置：登录状态与导出目录的持久化。"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path


APP_DIR_NAME = "xdao-export"


def _default_config_path() -> Path:
    # 优先存在用户自己的配置目录，避免污染程序目录。
    base = os.environ.get("APPDATA")
    if base:
        return Path(base) / APP_DIR_NAME / "config.json"
    return Path.home() / f".{APP_DIR_NAME}" / "config.json"


@dataclass
class AppSettings:
    userhash: str | None = None
    output_dir: str | None = None
    extra: dict = field(default_factory=dict)

    _path: Path = field(default_factory=_default_config_path, repr=False, compare=False)

    @classmethod
    def load(cls) -> "AppSettings":
        settings = cls()
        try:
            if settings._path.exists():
                data = json.loads(settings._path.read_text(encoding="utf-8"))
                settings.userhash = data.get("userhash")
                settings.output_dir = data.get("output_dir")
                settings.extra = data.get("extra") or {}
        except Exception:
            # 配置损坏时静默回退到默认值，不影响启动。
            pass
        return settings

    def save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "userhash": self.userhash,
                "output_dir": self.output_dir,
                "extra": self.extra,
            }
            self._path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception:
            # 保存失败不应导致程序崩溃。
            pass
