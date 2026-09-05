"""X 岛网络访问：登录、取串、翻页、下载图片。"""

from __future__ import annotations

import gzip
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from http.cookiejar import CookieJar


USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36"
)


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

    @property
    def image_url(self) -> str | None:
        if not self.img:
            return None
        return None  # 由 Builder 结合 CDN 前缀解析


class XdaoClient:
    """负责与 X 岛交互。"""

    SITE = "https://www.nmbxd1.com"
    # 两种常见的 API 前缀，连接失败时自动尝试下一个。
    API_BASES = [
        "https://www.nmbxd1.com/Api",
        "https://api.nmb.best/api",
    ]

    def __init__(self) -> None:
        self._jar = CookieJar()
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self._jar)
        )
        self._api_base: str | None = None
        self.cdn_path = "https://image.nmb.best/"
        self._last_request_at = 0.0

    # ---------- 基础请求 ----------

    def _request(
        self,
        url: str,
        data: bytes | None = None,
        headers: dict | None = None,
        timeout: int = 20,
        retries: int = 2,
    ) -> bytes:
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

        last_error: Exception | None = None
        for attempt in range(retries + 1):
            req = urllib.request.Request(url, data=data)
            for key, value in merged_headers.items():
                req.add_header(key, value)
            try:
                with self._opener.open(req, timeout=timeout) as resp:
                    raw = resp.read()
                    encoding = resp.headers.get("Content-Encoding")
                    if encoding == "gzip":
                        raw = gzip.decompress(raw)
                    return raw
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", "replace")[:300]
                raise XdaoError(f"请求失败 {exc.code}: {url}\n{detail}") from exc
            except TimeoutError as exc:
                last_error = exc
            except OSError as exc:
                last_error = exc
                time.sleep(0.6 * (attempt + 1))
            except urllib.error.URLError as exc:
                last_error = exc
                time.sleep(0.6 * (attempt + 1))
        if isinstance(last_error, TimeoutError):
            raise XdaoError(f"请求超时：{url}") from last_error
        raise XdaoError(f"网络错误：{last_error}") from last_error

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < 0.08:
            time.sleep(0.08 - elapsed)
        self._last_request_at = time.monotonic()

    def _resolve_api_base(self) -> str:
        if self._api_base:
            return self._api_base
        last_error: Exception | None = None
        for base in self.API_BASES:
            try:
                self._request(f"{base}/getCDNPath", timeout=10)
                self._api_base = base
                return base
            except XdaoError as exc:
                last_error = exc
        raise XdaoError(f"无法连接 X 岛接口：{last_error}")

    def _api_get_json(self, path: str, params: dict) -> dict | list:
        base = self._resolve_api_base()
        query = urllib.parse.urlencode(params)
        raw = self._request(f"{base}/{path}?{query}")
        return json.loads(raw.decode("utf-8"))

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
        """手动设置饼干（userhash）。"""
        from http.cookiejar import Cookie

        value = userhash.strip()
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
        return self._api_get_json("thread", {"id": thread_id, "page": page})

    def parse_thread_page(self, payload: dict) -> dict:
        """把一页 /thread 的原始数据解析成规范结构。"""
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

    def download_image(self, url: str) -> bytes:
        return self._request(url, timeout=30)
