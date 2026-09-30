"""X 岛网络访问：登录、取串、翻页、下载图片。"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from http.cookiejar import CookieJar
from pathlib import Path


USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36"
)

# 单张图片体积上限，防止异常响应把内存吃满。
MAX_IMAGE_BYTES = 24 * 1024 * 1024


class XdaoError(Exception):
    """X 岛访问错误。"""


class LoginError(XdaoError):
    """登录相关错误。"""


@dataclass
class LoginForm:
    """登录页需要的信息。"""

    captcha_bytes: bytes
    hash_value: str
    login_url: str


@dataclass
class Post:
    """一条发言。"""

    id: int
    user_hash: str
    name: str
    title: str
    content: str
    now: str
    img: str
    ext: str
    admin: int
    sage: int = 0
    is_po: bool = False
    # 该发言来自第几页（缓存与去重使用）。
    page: int = 1


class XdaoClient:
    """负责与 X 岛交互。"""

    SITE = "https://www.nmbxd1.com"
    # 两种常见的 API 前缀，连接失败时自动尝试下一个。
    API_BASES = [
        "https://www.nmbxd1.com/Api",
        "https://api.nmb.best/api",
    ]

    def __init__(
        self,
        timeout: float = 20.0,
        retries: int = 2,
        proxy: str | None = None,
        throttle: float = 0.08,
    ) -> None:
        """
        timeout  : 单次请求超时秒数
        retries  : 失败重试次数（指数退避）
        proxy    : 代理地址，如 http://127.0.0.1:7890；留空则读取环境变量
        throttle : 两次请求之间的最小间隔秒数，避免请求过密
        """
        self.timeout = float(timeout)
        self.retries = max(0, int(retries))
        self._throttle_interval = max(0.0, float(throttle))

        handlers: list[urllib.request.BaseHandler] = []
        self.proxy = (proxy or "").strip() or self._proxy_from_env()
        if self.proxy:
            handlers.append(
                urllib.request.ProxyHandler({"http": self.proxy, "https": self.proxy})
            )
        if not any(isinstance(h, urllib.request.ProxyHandler) for h in handlers):
            # 显式装配空代理，避免继承系统级代理设置导致行为不可预期。
            handlers.append(urllib.request.ProxyHandler({}))

        self._jar = CookieJar()
        handlers.append(urllib.request.HTTPCookieProcessor(self._jar))
        self._opener = urllib.request.build_opener(*handlers)

        self._api_base: str | None = None
        self.cdn_path = "https://image.nmb.best/"
        self._last_request_at = 0.0
        # 最近一次取串返回的总页数与页号。
        self.last_page_count = 1
        self.last_page_index = 1
        # 图片本地缓存目录（由外部按需设置，用于跨次导出去重下载）。
        self.image_cache_dir: Path | None = None

    @staticmethod
    def _proxy_from_env() -> str:
        """读取环境变量里的代理设置（大小写两种写法都认）。"""
        for key in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
            value = os.environ.get(key)
            if value:
                return value.strip()
        return ""

    def apply_config(self, settings) -> None:
        """从 AppSettings 应用网络相关配置（超时、重试、代理、限速）。"""
        self.timeout = float(getattr(settings, "timeout", self.timeout) or self.timeout)
        self.retries = max(0, int(getattr(settings, "retries", self.retries) or 0))
        self._throttle_interval = max(
            0.0, float(getattr(settings, "throttle", self._throttle_interval) or 0.0)
        )
        proxy = (getattr(settings, "proxy", "") or "").strip()
        if proxy != (self.proxy or ""):
            self.proxy = proxy or self._proxy_from_env()
            handlers: list[urllib.request.BaseHandler] = []
            if self.proxy:
                handlers.append(
                    urllib.request.ProxyHandler({"http": self.proxy, "https": self.proxy})
                )
            else:
                handlers.append(urllib.request.ProxyHandler({}))
            handlers.append(urllib.request.HTTPCookieProcessor(self._jar))
            self._opener = urllib.request.build_opener(*handlers)

    # ---------- 基础请求 ----------

    def _request(
        self,
        url: str,
        data: bytes | None = None,
        headers: dict | None = None,
        timeout: float | None = None,
        retries: int | None = None,
        max_bytes: int | None = None,
    ) -> bytes:
        """发起请求并返回响应体。

        对超时、连接错误、429 与 5xx 做指数退避重试；
        4xx（除 429）属于确定性错误，直接失败，不做无谓重试。
        """
        self._throttle()
        merged_headers = {
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Accept-Encoding": "gzip, deflate",
            "Connection": "keep-alive",
            "Cache-Control": "no-cache",
        }
        if headers:
            merged_headers.update(headers)

        effective_timeout = float(timeout if timeout is not None else self.timeout)
        attempts = self.retries if retries is None else max(0, int(retries))
        last_error: Exception | None = None

        for attempt in range(attempts + 1):
            req = urllib.request.Request(url, data=data)
            for key, value in merged_headers.items():
                req.add_header(key, value)
            try:
                with self._opener.open(req, timeout=effective_timeout) as resp:
                    if max_bytes:
                        raw = resp.read(max_bytes + 1)
                        if len(raw) > max_bytes:
                            raise XdaoError(
                                f"响应内容超过 {max_bytes // 1024} KB 上限：{url}"
                            )
                    else:
                        raw = resp.read()
                    if resp.headers.get("Content-Encoding") == "gzip":
                        raw = gzip.decompress(raw)
                    return raw
            except urllib.error.HTTPError as exc:
                retryable = exc.code == 429 or 500 <= exc.code < 600
                if not retryable or attempt >= attempts:
                    detail = ""
                    try:
                        detail = exc.read().decode("utf-8", "replace")[:300]
                    except Exception:
                        pass
                    if exc.code == 429:
                        raise XdaoError(
                            "请求过于频繁，已被服务器限流（429）。请稍后重试，"
                            "或在设置里把「请求间隔」调大。"
                        ) from exc
                    raise XdaoError(f"请求失败 {exc.code}: {url}\n{detail}") from exc
                last_error = exc
                self._backoff(attempt, retry_after=exc.headers.get("Retry-After"))
            except TimeoutError as exc:
                last_error = exc
                if attempt >= attempts:
                    break
                self._backoff(attempt)
            except urllib.error.URLError as exc:
                last_error = exc
                if attempt >= attempts:
                    break
                self._backoff(attempt)
            except OSError as exc:
                last_error = exc
                if attempt >= attempts:
                    break
                self._backoff(attempt)

        if isinstance(last_error, TimeoutError):
            raise XdaoError(f"请求超时（{effective_timeout:.0f} 秒）：{url}") from last_error
        raise XdaoError(f"网络错误：{last_error or '未知原因'}（{url}）") from last_error

    @staticmethod
    def _backoff(attempt: int, retry_after: str | None = None) -> None:
        """指数退避等待；服务器给了 Retry-After 就优先听它的。"""
        if retry_after:
            try:
                time.sleep(min(30.0, max(0.0, float(retry_after))))
                return
            except (TypeError, ValueError):
                pass
        time.sleep(min(8.0, 0.8 * (2**attempt)))

    def _throttle(self) -> None:
        if self._throttle_interval <= 0:
            return
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < self._throttle_interval:
            time.sleep(self._throttle_interval - elapsed)
        self._last_request_at = time.monotonic()

    def _resolve_api_base(self) -> str:
        """确定可用的接口前缀；已选中的前缀失效时会重新探测其它备用地址。"""
        candidates = list(self.API_BASES)
        if self._api_base in candidates:
            candidates.remove(self._api_base)
            candidates.insert(0, self._api_base)

        last_error: Exception | None = None
        for base in candidates:
            try:
                self._request(f"{base}/getCDNPath", timeout=10)
                self._api_base = base
                return base
            except XdaoError as exc:
                last_error = exc
        self._api_base = None
        raise XdaoError(f"无法连接 X 岛接口：{last_error}")

    def _api_get_json(self, path: str, params: dict) -> dict | list:
        """取接口 JSON；当前前缀失败时自动换备用前缀重试一次。"""
        last_error: Exception | None = None
        for attempt in range(len(self.API_BASES)):
            base = self._resolve_api_base()
            query = urllib.parse.urlencode(params)
            try:
                raw = self._request(f"{base}/{path}?{query}")
                return json.loads(raw.decode("utf-8"))
            except XdaoError as exc:
                last_error = exc
            except (UnicodeDecodeError, json.JSONDecodeError):
                # 接口偶尔会返回 HTML 错误页，换备用前缀再试。
                last_error = XdaoError(f"接口返回了非 JSON 内容：{base}/{path}")
            # 当前前缀不可用，下一轮会重新探测。
            if self._api_base == base:
                self._api_base = None
            if attempt == len(self.API_BASES) - 1:
                break
        raise last_error if isinstance(last_error, XdaoError) else XdaoError(str(last_error))

    # ---------- 图片 CDN ----------

    def update_cdn_path(self) -> str:
        data = self._api_get_json("getCDNPath", {})
        if isinstance(data, list) and data:
            self.cdn_path = data[0].get("url", self.cdn_path)
        return self.cdn_path

    def image_url(self, img: str, ext: str, thumb: bool = False) -> str:
        prefix = self.cdn_path
        if prefix and not prefix.endswith("/"):
            prefix += "/"
        kind = "thumb" if thumb else "image"
        return f"{prefix}{kind}/{img}{ext}"

    # ---------- 登录 ----------

    def set_userhash(self, userhash: str) -> None:
        """手动设置饼干（userhash）。

        登录、换饼干、读配置各会调一次，旧值必须清掉：同一个 jar 里留下
        两条同名 cookie 时，请求头会变成
        ``Cookie: userhash=新; userhash=旧``，服务端取哪一条并不确定 ——
        实测过这种情况，登录成功却仍被接口回「必须登入领取饼干后才可以访问」。
        """
        from http.cookiejar import Cookie

        value = userhash.strip()
        # 先清掉 jar 里所有旧的 userhash，避免同名 cookie 叠加。
        for cookie in list(self._jar):
            if cookie.name == "userhash":
                try:
                    self._jar.clear(cookie.domain, cookie.path, cookie.name)
                except KeyError:  # pragma: no cover - 已经被清掉了
                    pass
        for domain in ("nmbxd1.com", ".nmbxd1.com", "api.nmb.best", ".api.nmb.best"):
            cookie = Cookie(
                version=0,
                name="userhash",
                value=value,
                port=None,
                port_specified=False,
                domain=domain,
                domain_specified=True,
                domain_initial_dot=domain.startswith("."),
                path="/",
                path_specified=True,
                secure=False,
                expires=None,
                discard=True,
                comment=None,
                comment_url=None,
                rest={},
                rfc2109=False,
            )
            self._jar.set_cookie(cookie)

    def fetch_login_form(self) -> LoginForm:
        login_url = f"{self.SITE}/Member/User/Index/login.html"
        html = self._request(login_url).decode("utf-8", "replace")

        import re

        hash_match = re.search(r'name="__hash__"[^>]*value="([^"]*)"', html)
        hash_value = hash_match.group(1) if hash_match else ""
        if not hash_value:
            # 部分模板把 CSRF 值放在 <meta name="__hash__"> 里。
            meta_match = re.search(r'name="__hash__"[^>]*content="([^"]+)"', html)
            if meta_match:
                hash_value = meta_match.group(1)
        captcha_bytes = self._request(f"{self.SITE}/Member/User/Index/verify.html")
        return LoginForm(captcha_bytes=captcha_bytes, hash_value=hash_value, login_url=login_url)

    def login(self, email: str, password: str, verify: str, hash_value: str = "") -> str:
        """提交登录，成功后返回 userhash。"""
        login_url = f"{self.SITE}/Member/User/Index/login.html"
        payload = urllib.parse.urlencode(
            {
                "email": email,
                "password": password,
                "verify": verify,
                "__hash__": hash_value,
            }
        ).encode("utf-8")
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Referer": login_url,
        }
        raw = self._request(login_url, data=payload, headers=headers, timeout=15)
        text = raw.decode("utf-8", "replace")

        import re
        import html as _html

        plain = _html.unescape(re.sub(r"<[^>]+>", " ", text))
        plain = " ".join(plain.split())

        if "验证码" in plain and ("错" in plain or "不正确" in plain):
            raise LoginError("验证码错误，请点击验证码图片刷新后重试。")
        if "密码" in plain and ("错" in plain or "不正确" in plain):
            raise LoginError("密码错误，请重新输入。")
        if ("账号" in plain or "用户" in plain) and ("错" in plain or "不存在" in plain or "失败" in plain):
            raise LoginError("账号不存在或登录失败。")

        # 登录成功会返回“登陆成功”并设置 memberUserspapapa；
        # 真正的 userhash 需要再去应用一块饼干。
        try:
            return self.apply_cookie()
        except XdaoError as exc:
            raise LoginError(str(exc)) from exc

    def apply_cookie(self) -> str:
        """登录后应用一块饼干，返回对应的 userhash。

        登录成功后只得到 memberUserspapapa（用户系统会话），
        还需要访问饼干列表并应用一块饼干，主站才会设置 userhash。
        """
        index_url = f"{self.SITE}/Member/User/Cookie/index.html"
        html = self._request(index_url).decode("utf-8", "replace")

        import re

        # 饼干列表行里通常有 switchTo/export 链接，链接中的 id 最可靠。
        ids = re.findall(r"Cookie/(?:switchTo|export)/id/([^/\s\"'<]+)", html)
        if not ids:
            # 兼容 <td> 文本形式的 id（第二个单元格是 id）。
            rows = re.findall(r"<tr[^>]*>(.*?)</tr>", html, flags=re.S)
            for row in rows:
                cells = re.findall(r"<td[^>]*>(.*?)</td>", row, flags=re.S)
                if len(cells) >= 2:
                    candidate = re.sub(r"<[^>]+>", "", cells[1]).strip()
                    if candidate:
                        ids.append(candidate)
        if not ids:
            raise XdaoError("未找到可用的饼干，请先在浏览器里登录用户系统并应用一块饼干。")

        cookie_id = ids[0]
        self._request(f"{self.SITE}/Member/User/Cookie/switchTo/id/{cookie_id}.html")

        export_html = self._request(
            f"{self.SITE}/Member/User/Cookie/export/id/{cookie_id}.html"
        ).decode("utf-8", "replace")
        userhash = self._extract_userhash_from_export(export_html)
        if not userhash:
            # 切换饼干后，主站可能已经设置了 userhash Cookie。
            for cookie in self._jar:
                if cookie.name == "userhash" and cookie.value:
                    userhash = cookie.value
                    break
        if not userhash:
            raise XdaoError("应用饼干成功，但未能读取到 userhash，请手动粘贴饼干。")
        self.set_userhash(userhash)
        return userhash

    @staticmethod
    def _extract_userhash_from_export(text: str) -> str | None:
        import json
        import re

        try:
            data = json.loads(text)
            cookie = data.get("cookie") if isinstance(data, dict) else None
            if cookie:
                return cookie
        except Exception:
            pass
        match = re.search(r"userhash=([^;\"'\s]+)", text)
        if match:
            return match.group(1)
        match = re.search(r"\"cookie\"\s*:\s*\"([^\"]+)\"", text)
        if match:
            return match.group(1)
        return None

    # ---------- 取串 ----------

    def fetch_thread_page(self, thread_id: int, page: int = 1) -> dict:
        payload = self._api_get_json("thread", {"id": thread_id, "page": page})
        # 记录最近一次请求的页数，供抓取层判断总量（接口不总是给出）。
        self.last_page_count = self._extract_page_count(payload)
        self.last_page_index = page
        return payload

    @staticmethod
    def _extract_page_count(payload) -> int:
        """接口用不同字段名表达总页数，这里统一取一个可靠的候选值。"""
        if not isinstance(payload, dict):
            return 1
        for key in ("PageCount", "page_count", "TotalPage", "total_page", "pages"):
            try:
                value = int(payload.get(key) or 0)
            except (TypeError, ValueError):
                continue
            if value > 0:
                return value
        try:
            return max(1, int(payload.get("ReplyCount") or 0) // 19 + 1)
        except (TypeError, ValueError):
            return 1

    def parse_thread_page(self, payload: dict, page: int | None = None) -> dict:
        """把一页 /thread 的原始数据解析成规范结构。

        page 省略时沿用最近一次请求的页号；从缓存里读出来的旧页要显式传入页号。
        """
        current_page = int(page or self.last_page_index or 1)
        replies = payload.get("Replies") or []
        po_hash = payload.get("user_hash")
        posts: list[Post] = []

        main = Post(
            id=int(payload.get("id") or 0),
            user_hash=payload.get("user_hash") or "",
            name=payload.get("name") or "无名氏",
            title=payload.get("title") or "无标题",
            content=payload.get("content") or "",
            now=payload.get("now") or "",
            img=payload.get("img") or "",
            ext=payload.get("ext") or "",
            admin=int(payload.get("admin") or 0),
            sage=int(payload.get("sage") or 0),
            is_po=True,
            page=current_page,
        )
        posts.append(main)

        for item in replies:
            # Tips 酱（id=9999999 / user_hash=Tips）是系统帖，不加入正文。
            if str(item.get("id")) == "9999999" or item.get("user_hash") == "Tips":
                continue
            posts.append(
                Post(
                    id=int(item.get("id") or 0),
                    user_hash=item.get("user_hash") or "",
                    name=item.get("name") or "无名氏",
                    title=item.get("title") or "",
                    content=item.get("content") or "",
                    now=item.get("now") or "",
                    img=item.get("img") or "",
                    ext=item.get("ext") or "",
                    admin=int(item.get("admin") or 0),
                    sage=int(item.get("sage") or 0),
                    is_po=False,
                    page=current_page,
                )
            )
        return {
            "id": int(payload.get("id") or 0),
            "fid": payload.get("fid"),
            "ReplyCount": payload.get("ReplyCount"),
            "title": main.title,
            "po_hash": po_hash,
            "posts": posts,
            "has_more": len(replies) > 0,
        }

    def download_image(self, url: str, use_cache: bool = True) -> bytes:
        """下载图片；启用缓存时命中本地文件直接返回，避免重复下载。

        缓存文件按 URL 的 SHA-1 命名，存放在 ``image_cache_dir``。
        """
        path = self._cache_path(url) if use_cache else None
        if path is not None and path.exists():
            try:
                return path.read_bytes()
            except OSError:
                pass  # 缓存损坏就重新下载

        data = self._request(url, timeout=max(30.0, self.timeout), max_bytes=MAX_IMAGE_BYTES)

        if path is not None:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                # 先写临时文件再替换，避免并发下载留下半截文件。
                tmp = path.with_suffix(path.suffix + ".part")
                tmp.write_bytes(data)
                tmp.replace(path)
            except OSError:
                pass  # 缓存写入失败不影响本次导出
        return data

    def _cache_path(self, url: str) -> Path | None:
        if self.image_cache_dir is None or not url or url.startswith("data:"):
            return None
        digest = hashlib.sha1(url.encode("utf-8")).hexdigest()
        # 扩展名取自 URL，兜底 .img；真正类型由调用方按魔数判断。
        suffix = ".img"
        match = re.search(r"\.(jpg|jpeg|png|gif|webp|bmp)(?:\?|$)", url.lower())
        if match:
            suffix = "." + ("jpg" if match.group(1) == "jpeg" else match.group(1))
        return Path(self.image_cache_dir) / f"{digest[:2]}" / f"{digest}{suffix}"
