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
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .appinfo import GITHUB_REPO, USER_AGENT, VERSION, asset_name
# 代理探测与 opener 装配的实现在 xdao/github_api.py：xdao/updater.py 下载升级包时也要
# 用同一套，而它原先得反过来 import 本模块。名字仍从本模块出去（下面有转发），
# 免得调用方与测试里打在 update_check 上的桩失效。
from .github_api import _env_proxy, _usable_proxy, _winreg_proxy, build_opener_with_proxy
from .settings import app_config_dir

# 问的就是自家仓库。仓库地址与 User-Agent 的真源在 xdao/appinfo.py（打包脚本、
# CI 工具问的是同一个），这里只是把老名字留在原地。
REPO = GITHUB_REPO
API_URL = f"https://api.github.com/repos/{REPO}/releases/latest"

#: 缓存多久之内不再问接口。
CACHE_TTL_SECONDS = 24 * 60 * 60
#: 单次请求超时（秒）。这是锦上添花的功能，不能让人等。
TIMEOUT_SECONDS = 8.0

_USER_AGENT = USER_AGENT

_CACHE_NAME = "update-check.json"
_MAX_RESPONSE_BYTES = 512 * 1024


class UpdateCheckError(RuntimeError):
    """问不到新版本（网络不通、接口改版……）。"""


@dataclass
class ReleaseInfo:
    """一个已发布的版本。

    ``asset_url`` 是免安装包（zip）的直链，有它才能做「一键升级」；
    老接口、老缓存里没有这个字段时是空串，此时只能去下载页手工下。
    """

    tag: str
    url: str = ""
    asset_url: str = ""
    asset_name: str = ""
    asset_size: int = 0

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
    current: str = VERSION
    latest: ReleaseInfo | None = None
    reason: str = ""
    from_cache: bool = False

    @property
    def latest_version(self) -> str:
        return self.latest.tag if self.latest else ""

    @property
    def url(self) -> str:
        return self.latest.url if self.latest else f"https://github.com/{REPO}/releases/latest"

    @property
    def asset_url(self) -> str:
        """免安装包直链（没有就是空串，只能手工下）。"""
        return self.latest.asset_url if self.latest else ""

    @property
    def asset_name(self) -> str:
        return self.latest.asset_name if self.latest else ""

    @property
    def asset_size(self) -> int:
        return self.latest.asset_size if self.latest else 0

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
                "升级包": self.asset_url,
                "升级包名字": self.asset_name,
                "升级包字节数": self.asset_size,
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
# 一处），都没有就直连。具体实现（``_env_proxy`` / ``_winreg_proxy`` /
# ``_usable_proxy``）在 xdao/github_api.py，本模块按老名字转发过来。


def detect_proxy() -> str:
    """当前该用哪个代理（空串表示直连）。

    实现搬去了 :mod:`xdao.github_api`（``updater`` 下载升级包用的是同一套），但这个
    函数留在原处：调用方与测试都在 ``update_check.detect_proxy`` 这个名字上打桩，
    换地方会让桩打空。它照旧读本模块的 ``_env_proxy`` / ``_winreg_proxy``，
    所以那三个名字也仍然可以从这里替换（见上面的转发 import）。
    """
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


def write_cache(
    tag: str, url: str, path: Path | None = None, *, asset_url: str = ""
) -> None:
    """记一笔。写不进去就算了 —— 不该因为记不住而报错。"""
    target = path or cache_path()
    payload = {
        "tag": str(tag),
        "url": str(url),
        "asset_url": str(asset_url),
        "checked_at": time.time(),
    }
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )
    except OSError:
        pass


# ---------------------------------------------------------------- 问接口


def _pick_asset(payload: Any, tag: str) -> tuple[str, str, int]:
    """从 Release 的 assets 里挑出免安装包，返回 ``(直链, 名字, 字节数)``。

    挑法有两步：先按标签算出「应该叫什么」（``v0.12.0`` → ``xdao-export-v0.12.0-win64.zip``），
    精确命中就用它；算不出来或者名字对不上，就退而求其次拿第一个 zip。
    挑不到就返回三个空值 —— 上层会退化成「打开下载页」。
    """
    if not isinstance(payload, list):
        return ("", "", 0)
    candidates: list[dict[str, Any]] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "")
        link = str(item.get("browser_download_url") or "")
        if not name or not link:
            continue
        try:
            size = int(item.get("size") or 0)
        except (TypeError, ValueError):
            size = 0
        candidates.append({"name": name, "url": link, "size": size})
    if not candidates:
        return ("", "", 0)
    cleaned = (tag or "").strip()
    if cleaned[:1] in ("v", "V"):
        cleaned = cleaned[1:]
    wanted = asset_name(cleaned) if cleaned else ""
    for item in candidates:
        if wanted and item["name"] == wanted:
            return (item["url"], item["name"], item["size"])
    for item in candidates:
        if item["name"].lower().endswith(".zip"):
            return (item["url"], item["name"], item["size"])
    return ("", "", 0)


def fetch_latest(proxy: str = "", timeout: float = TIMEOUT_SECONDS) -> ReleaseInfo:
    """问一次 GitHub：最新 Release 是哪个版本。失败抛 UpdateCheckError。"""
    opener = build_opener_with_proxy(proxy)
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
    asset_url, asset_name, asset_size = _pick_asset(payload.get("assets"), tag)
    return ReleaseInfo(
        tag=tag,
        url=url,
        asset_url=asset_url,
        asset_name=asset_name,
        asset_size=asset_size,
    )


# ---------------------------------------------------------------- 对外入口


def check_for_update(
    *,
    force: bool = False,
    current: str = VERSION,
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
                asset_url=str(cached.get("asset_url") or ""),
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
    write_cache(info.tag, info.url, target, asset_url=info.asset_url)
    return _result_from_tag(
        info.tag,
        info.url,
        current,
        asset_url=info.asset_url,
        asset_name=info.asset_name,
        asset_size=info.asset_size,
    )


def _result_from_tag(
    tag: str,
    url: str,
    current: str,
    *,
    reason: str = "",
    asset_url: str = "",
    asset_name: str = "",
    asset_size: int = 0,
) -> UpdateResult:
    if not tag:
        return UpdateResult(ok=False, current=current, reason=reason or "没有版本号")
    info = ReleaseInfo(
        tag=tag,
        url=url,
        asset_url=asset_url,
        asset_name=asset_name,
        asset_size=asset_size,
    )
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
