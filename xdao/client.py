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
import zlib
from dataclasses import dataclass
from http.cookiejar import CookieJar
from pathlib import Path
from typing import Iterable


USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36"
)

# 单张图片体积上限，防止异常响应把内存吃满。
MAX_IMAGE_BYTES = 24 * 1024 * 1024

# gzip 包固定以这两个字节开头。
GZIP_MAGIC = b"\x1f\x8b"


def _decode_response_body(data: bytes, content_type: str = "") -> bytes:
    """把响应体还原成它真正的内容。

    实测：登录页的验证码接口把 PNG 包在 gzip 里发回来，HTTP 头却写着
    ``image/png`` —— 只看 ``Content-Encoding`` 会漏掉。所以这里既认 gzip 魔数，
    也认 ``Content-Type`` 里的 gzip 字样；解压失败（响应本身就是坏的）原样返回，
    让调用方按自己的方式报错，而不是在这里抛一个看不出所以然的异常。
    """
    if not data:
        return data
    if data[:2] != GZIP_MAGIC and "gzip" not in (content_type or "").lower():
        return data
    try:
        return gzip.decompress(data)
    except (OSError, EOFError, zlib.error):
        return data


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
        # 最近一次成功响应的头（键统一小写）。用来判断响应体是否需要额外解码，
        # 又不必改动 _request 的返回类型、也就不会打断任何替换 _request 的调用方。
        self._last_response_headers: dict[str, str] = {}

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
                    self._last_response_headers = {
                        str(key).lower(): str(value)
                        for key, value in resp.headers.items()
                    }
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
    def jump_page_url(html: str) -> str:
        """取「跳转提示」页要去的地方，没有就返回空串。

        X 岛用 ThinkPHP 的跳转模板：**HTTP 状态是 200**，靠页面里的
        ``<a id="href" href="…">`` + 一段 JavaScript 定时跳转。浏览器会自己走，
        urllib 不会，所以我们得自己认出来 —— 否则「并没有权限访问」这种页面
        会被当成正常的列表页，解析出零条记录（线上踩过：
        邮箱登录成功后立刻去取饼干列表，拿到的是这张跳转页，程序报
        「未找到可用的饼干」，把真正的「登录没成/没有权限」盖掉了）。
        """
        if not html or "<html" not in html.lower():
            return ""
        if "跳转提示" not in html and 'id="href"' not in html:
            return ""
        match = re.search(
            r'<a[^>]*id="href"[^>]*href="([^"]*)"', html
        ) or re.search(r'<a[^>]*href="([^"]*)"[^>]*id="href"', html)
        return match.group(1).strip() if match else ""

    @staticmethod
    def jump_page_message(html: str) -> str:
        """取「跳转提示」页上写给用户看的那句话（成功或失败），没有就返回空串。"""
        match = re.search(
            r'class="(?:error|success)"[^>]*>(.*?)</p>', html, flags=re.S
        )
        if not match:
            return ""
        import html as _html

        return " ".join(_html.unescape(re.sub(r"<[^>]+>", " ", match.group(1))).split())

    def _request_following_jumps(
        self, url: str, max_jumps: int = 2, **kwargs
    ) -> tuple[bytes, str]:
        """请求一个页面，遇到「跳转提示」页就跟着走（最多 max_jumps 次）。

        返回 ``(响应体, 最终地址)``。每一跳都带上 cookie，会话才能接上。
        """
        raw = self._request(url, **kwargs)
        final = url
        for _ in range(max_jumps):
            target = self.jump_page_url(raw.decode("utf-8", "replace"))
            if not target:
                break
            if target.startswith("/"):
                target = self.SITE + target
            if target == final:
                break
            final = target
            raw = self._request(target, **kwargs)
        return raw, final

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
        self._drop_cookies_named("userhash")
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

    def import_userhash(self, userhash: str) -> str:
        """把「浏览器登录 / 粘贴饼干」拿到的 userhash 设进 cookie jar，并返回它。

        只做「设进 jar」这一件事：写不写配置文件由调用方决定（界面里的
        「记住登录状态」是用户的选项，客户端不该替他做主），所以这里不落盘。
        真正的设值走 :meth:`set_userhash` —— 它会先清掉 jar 里旧的同名 cookie，
        否则同一请求里会出现两条 userhash，服务端取哪条并不确定。
        """
        value = (userhash or "").strip()
        self.set_userhash(value)
        return value

    def _drop_cookies_named(self, name: str) -> None:
        """把 jar 里所有叫这个名字的饼干清掉，不分域、不分路径（v0.13.22）。

        为什么要按名字扫全罐：同一个 jar 里留下两条同名 cookie 时，请求头会变成
        ``Cookie: userhash=新; userhash=旧``，服务端取哪一条并不确定 —— 实测过
        这种情况（登录成功却仍被接口回「必须登入领取饼干后才可以访问」）。
        :meth:`set_userhash` 与 :meth:`import_cookies` 都靠它保持「一个名字只剩一份」。
        """
        for cookie in list(self._jar):
            if cookie.name == name:
                try:
                    self._jar.clear(cookie.domain, cookie.path, cookie.name)
                except KeyError:  # pragma: no cover - 已经被清掉了
                    pass

    def import_cookies(self, cookies: Iterable[dict]) -> int:
        """把浏览器里的饼干整罐装进自己的 jar，返回装进去几条（v0.13.22）。

        为什么要它：浏览器那条路只需要负责「让用户把验证码认过去」，他在窗口里登录成功
        之后，那份资料里的会话饼干**就是这个会话本身**。把这几条饼干交给已经跑了很久的
        HTTP 流程（:meth:`apply_cookie`），比让浏览器自己去跳站点的跳转页可靠得多 ——
        userhash 是在**导出页的响应体**里给出的（``{"cookie": "…"}``，见
        :meth:`_extract_userhash_from_export`），浏览器把那一页渲染成什么、标签页有没有
        被用户切走、跳转倒计时跑到第几秒，都不该决定这件事成不成（用户 m29953/m29954）。

        ``cookies`` 收 CDP ``Network.getCookies`` 那种字典（``name``/``value``/``domain``/
        ``path``/``secure``/``expires``）。名字或值为空的跳过；开始装某个名字之前，先把
        jar 里同名的旧值**扫干净**（不分域、不分路径）：浏览器那份可能同时在几个域上带
        同一个名字，叠起来就是两条 ``userhash`` 同时出现在请求头里（见
        :meth:`_drop_cookies_named`）。
        """
        from http.cookiejar import Cookie

        count = 0
        swept: set[str] = set()
        for item in cookies or ():
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            value = str(item.get("value") or "")
            if not name or not value:
                continue
            if name not in swept:
                swept.add(name)
                self._drop_cookies_named(name)
            domain = str(item.get("domain") or "").strip() or ".nmbxd1.com"
            path = str(item.get("path") or "").strip() or "/"
            expires: int | None = None
            try:
                raw_expires = float(item.get("expires"))  # CDP 给的是秒（会话饼干是 -1）
            except (TypeError, ValueError):
                raw_expires = 0.0
            if raw_expires > 0:
                expires = int(raw_expires)
            self._jar.set_cookie(
                Cookie(
                    version=0,
                    name=name,
                    value=value,
                    port=None,
                    port_specified=False,
                    domain=domain,
                    domain_specified=True,
                    domain_initial_dot=domain.startswith("."),
                    path=path,
                    path_specified=True,
                    secure=bool(item.get("secure")),
                    expires=expires,
                    discard=expires is None,
                    comment=None,
                    comment_url=None,
                    rest={},
                    rfc2109=False,
                )
            )
            count += 1
        return count

    def fetch_login_form(self) -> LoginForm:
        login_url = f"{self.SITE}/Member/User/Index/login.html"
        # 有可能被弹到登录页（例如会话过期），跟着跳转走一遍再解析。
        raw, login_url = self._request_following_jumps(login_url)
        html = raw.decode("utf-8", "replace")

        import re

        hash_match = re.search(r'name="__hash__"[^>]*value="([^"]*)"', html)
        hash_value = hash_match.group(1) if hash_match else ""
        if not hash_value:
            # 部分模板把 CSRF 值放在 <meta name="__hash__"> 里。
            meta_match = re.search(r'name="__hash__"[^>]*content="([^"]+)"', html)
            if meta_match:
                hash_value = meta_match.group(1)
        captcha_raw = self._request(f"{self.SITE}/Member/User/Index/verify.html")
        # 实测：验证码接口把 PNG 包在 gzip 里发回来，头却写着 image/png，
        # 只认 Content-Encoding 会漏，界面那边就会拿到一堆压缩字节。
        captcha_bytes = _decode_response_body(
            captcha_raw, self._last_response_headers.get("content-type", "")
        )
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

        # 服务端也可能用「跳转提示」页回话（HTTP 200 + 页面跳转）。
        # 这种页面里没有「账号/密码/验证码」这些字段名，上面的规则全都匹配不上，
        # 会被当成"登录成功"，最后错误地报成「未找到可用的饼干」。
        jump_message = self.jump_page_message(text)
        jump_target = self.jump_page_url(text)
        if jump_message and "login" in jump_target:
            raise LoginError(
                f"登录没有通过，X 岛返回：{jump_message}（已回到登录页）。"
                "常见原因是账号或密码不对，也可能是验证码过期——请刷新验证码后重试。"
            )

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
        html, final_url = self._request_following_jumps(index_url)
        html = html.decode("utf-8", "replace")

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
            # 被弹回登录页 = 会话没建立起来，不是"账号里没有饼干"。
            if "login" in final_url:
                message = self.jump_page_message(html)
                detail = f"X 岛返回：{message}。" if message else "X 岛把请求弹回了登录页。"
                raise LoginError(
                    f"登录后没能进入用户系统（{detail}）"
                    "请重新登录：确认密码正确、验证码是刚刷新出来的那一张。"
                )
            message = self.jump_page_message(html)
            if message:
                raise LoginError(
                    f"登录后没能读取饼干列表（X 岛返回：{message}）。"
                    "请确认该账号能打开用户系统的「饼干」页。"
                )
            raise XdaoError(
                "已登录，但这个账号的饼干列表是空的。请先在浏览器里打开 X 岛用户系统 "
                "→「饼干」→ 领取并应用一块饼干，再回到程序重新登录；"
                "也可以直接用登录窗口里的「用浏览器登录」。"
            )

        cookie_id = ids[0]
        # 同样跟着「跳转提示」页走：应用饼干这一跳也可能被弹回去。
        self._request_following_jumps(
            f"{self.SITE}/Member/User/Cookie/switchTo/id/{cookie_id}.html"
        )

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
            raise XdaoError(
                "应用饼干成功，但没能从这个账号里读到 userhash。"
                "可以点登录窗口的「用浏览器登录」让它自己取一次，"
                "或把浏览器里的 cookie 整段粘贴到「直接粘贴饼干登录」里。"
            )
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
