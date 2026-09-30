"""看看有没有新版本。

用户常常把某个打包版一直用下去，出了修复也不知道。这里做一件小事：
问一次 GitHub 上最新 Release 的版本号，比当前版本新就告诉用户去哪儿下。

几条自我约束：

* **只读、只问一次**：网络失败、被墙、接口改版，一律当作「查不到」，
  绝不打断导出或启动流程。
* **问过就记 24 小时**：不反复骚扰接口，也不让每次启动都卡在网络上。
* **可以不带代理直连**：本机实测直连 api.github.com 是通的；环境里配了
  代理就优先用它，Windows 上还会读一次「Internet 选项」里的系统代理。
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import __version__
from .settings import app_config_dir

# 问的就是自家仓库，写死即可。
REPO = "XiaoFeng7418/xdao-export"
API_URL = f"https://api.github.com/repos/{REPO}/releases/latest"

#: 缓存多久之内不再问接口。
CACHE_TTL_SECONDS = 24 * 60 * 60
#: 单次请求超时（秒）。这是锦上添花的功能，不能让人等。
TIMEOUT_SECONDS = 8.0
#: 代理探测也很快，给个更短的超时。
PROXY_DETECT_TIMEOUT = 3.0

_USER_AGENT = f"xdao-export/{__version__} (+https://github.com/{REPO})"

_CACHE_NAME = "update-check.json"
_MAX_RESPONSE_BYTES = 512 * 1024


class UpdateCheckError(RuntimeError):
    """问不到新版本（网络不通、接口改版……）。"""


@dataclass
class ReleaseInfo:
    """一个已发布的版本。"""

    tag: str
    url: str = ""

    @property
    def version(self) -> tuple[int, int, int]:
        return parse_version(self.tag)

    @property
    def display_version(self) -> str:
        return ".".join(str(part) for part in self.version)


@dataclass
class UpdateResult:
    """一次检查的结果。

    ``ok`` 为真表示真的问到了接口（``newer`` 才有意义）；为假表示这次没问到，
    ``reason`` 里是给人看的原因。
    """

    ok: bool = False
    newer: bool = False
    current: str = __version__
    latest: ReleaseInfo | None = None
    reason: str = ""
    from_cache: bool = False

    @property
    def latest_version(self) -> str:
        return self.latest.tag if self.latest else ""

    @property
    def url(self) -> str:
        return self.latest.url if self.latest else f"https://github.com/{REPO}/releases/latest"

    def line(self) -> str:
        """一行中文结论（界面日志与命令行共用）。"""
        if not self.ok:
            return f"新版本检查：没查到（{self.reason or '原因不明'}）"
        if self.newer and self.latest is not None:
            return (
                f"有新版本可用：{self.current} → {self.latest.tag}"
                f"（下载页 {self.url}）"
            )
        return f"已是最新版本（{self.current}）。"

    def to_json(self) -> str:
        """给「贴给别人看」用的 JSON。"""
        return json.dumps(
            {
                "当前版本": self.current,
                "查到没有": self.ok,
                "有没有新的": self.newer,
                "最新版本": self.latest_version,
                "下载页": self.url,
                "说明": self.reason,
                "来自缓存": self.from_cache,
            },
            ensure_ascii=False,
            indent=2,
        )


# ---------------------------------------------------------------- 版本号
#
# 比大小这件事只认「数字.数字.数字」，并且只比前三位：发布标签一律是
# ``v0.11.0`` 这种形式，多余的后缀（``-beta1`` 之类）与位数都不参与比较。
# 认不出来就当成 0.0.0，于是永远不会自称「有新版本」。


def parse_version(text: str) -> tuple[int, int, int]:
    """把 ``v0.12.0`` / ``0.12`` / ``V1.2.3.4`` 解析成三元组。"""
    cleaned = (text or "").strip()
    if cleaned[:1] in ("v", "V"):
        cleaned = cleaned[1:]
    if not cleaned:
        return (0, 0, 0)
    parts: list[int] = []
    for chunk in cleaned.split("."):
        digits = ""
        for char in chunk:
            if char.isdigit():
                digits += char
            else:
                break
        if not digits:
            break
        parts.append(int(digits))
        if len(parts) == 3:
            break
    while len(parts) < 3:
        parts.append(0)
    return (parts[0], parts[1], parts[2])


def is_newer(remote: str, local: str) -> bool:
    """``remote`` 比 ``local`` 新就返回 True（认不出来时为 False）。"""
    return parse_version(remote) > parse_version(local)


# ---------------------------------------------------------------- 代理
#
# 这个功能问的是 GitHub，和 X 岛接口不是一回事：用户为 X 岛配的代理可能只
# 代理了别处，而 GitHub 在国内常常需要梯子。所以这里自己决定用哪个代理：
# 环境变量优先，其次 Windows「Internet 选项」里的系统代理（很多梯子只写这
# 一处），都没有就直连。


def _env_proxy() -> str:
    for key in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
        value = (os.environ.get(key) or "").strip()
        if value:
            return value
    return ""


def _winreg_proxy() -> str:
    """读 Windows 的「Internet 选项」代理设置（读不到就空串）。"""
    try:
        import winreg
    except ImportError:
        return ""
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
        ) as key:
            enabled, _ = winreg.QueryValueEx(key, "ProxyEnable")
            server, _ = winreg.QueryValueEx(key, "ProxyServer")
    except OSError:
        return ""
    if not enabled or not server:
        return ""
    text = str(server)
    # 可能是「http=host:port;https=host:port」这种按协议分开的写法。
    if "=" in text:
        for item in text.split(";"):
            name, _, value = item.partition("=")
            if name.strip().lower() == "https" and value.strip():
                return value.strip()
        return ""
    return text


def _usable_proxy(text: str) -> str:
    """把注册表里的 ``127.0.0.1:7890`` 补成 ``http://127.0.0.1:7890``。"""
    candidate = (text or "").strip()
    if not candidate:
        return ""
    if "://" not in candidate:
        candidate = f"http://{candidate}"
    try:
        parsed = urllib.parse.urlsplit(candidate)
    except ValueError:
        return ""
    if parsed.scheme not in ("http", "https"):
        return ""
    if not parsed.hostname or not parsed.port:
        return ""
    return candidate


def detect_proxy() -> str:
    """当前该用哪个代理（空串表示直连）。"""
    return _usable_proxy(_env_proxy()) or _usable_proxy(_winreg_proxy())


# ---------------------------------------------------------------- 缓存


def cache_path() -> Path:
    return app_config_dir() / _CACHE_NAME


def read_cache(path: Path | None = None) -> dict[str, Any] | None:
    """读上次问到的结果；坏了、太旧都当没有。"""
    target = path or cache_path()
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    return payload


def write_cache(tag: str, url: str, path: Path | None = None) -> None:
    """记一笔。写不进去就算了 —— 不该因为记不住而报错。"""
    target = path or cache_path()
    payload = {"tag": str(tag), "url": str(url), "checked_at": time.time()}
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )
    except OSError:
        pass


# ---------------------------------------------------------------- 问接口


def fetch_latest(proxy: str = "", timeout: float = TIMEOUT_SECONDS) -> ReleaseInfo:
    """问一次 GitHub：最新 Release 是哪个版本。失败抛 UpdateCheckError。"""
    handlers: list[urllib.request.BaseHandler] = []
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    else:
        # 显式装配空代理：系统级代理常常只对特定程序生效，继承过来反而
        # 会让「本可以直连」的环境连不上。
        handlers.append(urllib.request.ProxyHandler({}))
    opener = urllib.request.build_opener(*handlers)
    request = urllib.request.Request(
        API_URL,
        headers={
            "User-Agent": _USER_AGENT,
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with opener.open(request, timeout=timeout) as response:
            raw = response.read(_MAX_RESPONSE_BYTES)
    except urllib.error.HTTPError as exc:
        raise UpdateCheckError(f"接口返回 {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise UpdateCheckError(f"连不上 GitHub（{exc.reason}）") from exc
    except OSError as exc:
        raise UpdateCheckError(f"连不上 GitHub（{exc}）") from exc
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise UpdateCheckError("接口返回的不是 JSON") from exc
    if not isinstance(payload, dict):
        raise UpdateCheckError("接口返回的结构看不懂")
    tag = str(payload.get("tag_name") or "").strip()
    if not tag:
        raise UpdateCheckError("接口里没有版本号")
    url = str(payload.get("html_url") or "").strip()
    if not url:
        url = f"https://github.com/{REPO}/releases/tag/{tag}"
    return ReleaseInfo(tag=tag, url=url)


# ---------------------------------------------------------------- 对外入口


def check_for_update(
    *,
    force: bool = False,
    current: str = __version__,
    ttl: float = CACHE_TTL_SECONDS,
    path: Path | None = None,
    fetcher: Any = None,
    proxy: str | None = None,
) -> UpdateResult:
    """查一次新版本。永远返回结果，不抛异常、不阻塞太久。"""
    target = path or cache_path()
    if not force:
        cached = _fresh_cache(target, ttl)
        if cached is not None:
            result = _result_from_tag(
                str(cached.get("tag") or ""),
                str(cached.get("url") or ""),
                current,
                reason="（24 小时内问过）",
            )
            result.from_cache = True
            return result

    query = fetcher or fetch_latest
    try:
        if fetcher is None:
            info = query(proxy=detect_proxy() if proxy is None else proxy)
        else:
            # 传了替身就按替身的签名调用（测试里图省事）。
            info = query()
    except UpdateCheckError as exc:
        return UpdateResult(ok=False, current=current, reason=str(exc))
    except Exception as exc:  # noqa: BLE001 - 第三方接口，什么都可能抛
        return UpdateResult(ok=False, current=current, reason=f"{type(exc).__name__}: {exc}")
    if not isinstance(info, ReleaseInfo) or not info.tag:
        return UpdateResult(ok=False, current=current, reason="接口没给出可用的版本号")
    write_cache(info.tag, info.url, target)
    return _result_from_tag(info.tag, info.url, current)


def _result_from_tag(
    tag: str, url: str, current: str, *, reason: str = ""
) -> UpdateResult:
    if not tag:
        return UpdateResult(ok=False, current=current, reason=reason or "没有版本号")
    info = ReleaseInfo(tag=tag, url=url)
    return UpdateResult(
        ok=True,
        newer=is_newer(tag, current),
        current=current,
        latest=info,
        reason=reason,
    )


def _fresh_cache(path: Path, ttl: float) -> dict[str, Any] | None:
    payload = read_cache(path)
    if not payload or not payload.get("tag"):
        return None
    try:
        checked_at = float(payload.get("checked_at"))
    except (TypeError, ValueError):
        return None
    if ttl < 0 or time.time() - checked_at > ttl:
        return None
    return payload
