"""客户端层的用例：只测不联网就能验的部分（Cookie 管理、解析、登录页兜底）。"""

from __future__ import annotations

import gzip
import io
import urllib.error

import pytest

from xdao import client as client_module
from xdao.client import LoginError, XdaoClient, XdaoError, _decode_response_body

# 线上抓到的真实「跳转提示」页（HTTP 200 + JS 跳转，urllib 不会自己跟）。
JUMP_TO_LOGIN = """<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.0 Transitional//EN">
<html xmlns="http://www.w3.org/1999/xhtml"><head><title>跳转提示</title></head>
<body><div class="system-message">
<h1>:(</h1>
<p class="error">并没有权限访问_(:з」∠)_，请等待自动跳转</p>
<p class="detail"></p>
<p class="jump">页面自动 <a id="href" href="/Member/User/Index/login.html">跳转</a>
等待时间： <b id="wait">3</b></p>
</div></body></html>"""

JUMP_SUCCESS = """<html><head><title>跳转提示</title></head><body>
<div class="system-message"><h1>:)</h1>
<p class="success">登陆成功</p>
<p class="jump">页面自动 <a id="href" href="/Member/User/Cookie/index.html">跳转</a></p>
</div></body></html>"""

# 登录成功后服务端给的「跳转提示」页：目标是用户系统的饼干列表。
JUMP_TO_COOKIE_LIST = JUMP_SUCCESS

COOKIE_LIST = """<html><body><table>
<tr><td>1</td><td>abc123</td>
<td><a href="/Member/User/Cookie/switchTo/id/abc123.html">应用</a></td></tr>
</table></body></html>"""


def _responder(routes: dict[str, str], default: str = ""):
    """伪造 _request：按 URL 返回预设内容，并记录调用顺序。"""
    calls: list[str] = []

    def fake_request(url, data=None, **kwargs):
        calls.append(url)
        for key, body in routes.items():
            if key in url:
                return body.encode("utf-8")
        return default.encode("utf-8")

    return fake_request, calls


def _userhash_cookies(client: XdaoClient) -> list[str]:
    return sorted(
        f"{cookie.domain}{cookie.path}" for cookie in client._jar if cookie.name == "userhash"
    )


def test_set_userhash_replaces_the_previous_value():
    """重复设置饼干时不能留下同名旧 cookie —— 请求头会变成两条 userhash。"""
    client = XdaoClient()
    client.set_userhash("OLDHASH")
    first = _userhash_cookies(client)
    assert len(first) == 4

    client.set_userhash("NEWHASH")
    assert _userhash_cookies(client) == first  # 域名集合不变，没有叠加
    assert {cookie.value for cookie in client._jar if cookie.name == "userhash"} == {"NEWHASH"}


def test_set_userhash_trims_surrounding_whitespace():
    client = XdaoClient()
    client.set_userhash("  HASH123  \n")
    assert {cookie.value for cookie in client._jar if cookie.name == "userhash"} == {"HASH123"}


def test_jump_page_url_reads_the_href_and_ignores_normal_pages():
    assert XdaoClient.jump_page_url(JUMP_TO_LOGIN) == "/Member/User/Index/login.html"
    assert XdaoClient.jump_page_url(COOKIE_LIST) == ""
    assert XdaoClient.jump_page_url("") == ""


def test_jump_page_message_reads_error_and_success_text():
    """跳转页上那句提示是唯一能区分「没权限」和「账号里没饼干」的证据。"""
    assert "并没有权限访问" in XdaoClient.jump_page_message(JUMP_TO_LOGIN)
    assert XdaoClient.jump_page_message(JUMP_SUCCESS) == "登陆成功"
    assert XdaoClient.jump_page_message(COOKIE_LIST) == ""


def test_request_following_jumps_lands_on_the_target_page():
    """跳转页必须自己跟过去，否则会把「没权限」当成正常页解析。"""
    client = XdaoClient()
    # 注意路由键要写得互不包含：跳转页正文里也带着 "/Member/User/Index/login.html"。
    fake, calls = _responder({"/Cookie/index.html": COOKIE_LIST}, default=JUMP_TO_COOKIE_LIST)
    client._request = fake  # type: ignore[method-assign]

    raw, final = client._request_following_jumps("https://www.nmbxd1.com/start.html")

    assert b"switchTo" in raw
    assert final == "https://www.nmbxd1.com/Member/User/Cookie/index.html"
    assert calls == [
        "https://www.nmbxd1.com/start.html",
        "https://www.nmbxd1.com/Member/User/Cookie/index.html",
    ]


def test_request_following_jumps_stops_on_a_success_page_whose_href_is_empty():
    """「饼干切换成功!」那页的 ``<a id="href" href="">`` 是空的：跟跳必须就地停住。

    出问题的那张页面（停在「饼干切换成功!」一直跳）就是这条：空 href 一旦被当成
    「再去一趟当前地址」，浏览器会原地重载、程序会自己跟着自己转。HTTP 这条路
    必须钉住「空 href = 到头了」。
    """
    client = XdaoClient()
    success = (
        '<html><head><title>跳转提示</title></head><body><div class="system-message">'
        '<h1>:)</h1><p class="success">饼干切换成功!</p>'
        '<p class="jump">页面自动 <a id="href" href="">跳转</a> '
        '等待时间： <b id="wait">1</b></p></div></body></html>'
    )
    fake, calls = _responder({}, default=success)
    client._request = fake  # type: ignore[method-assign]

    raw, final = client._request_following_jumps("https://www.nmbxd1.com/switch.html")

    assert final == "https://www.nmbxd1.com/switch.html"
    assert calls == ["https://www.nmbxd1.com/switch.html"], "空 href 不该被当成新地址"
    assert "饼干切换成功" in raw.decode("utf-8")


def test_request_following_jumps_stops_when_the_page_points_at_itself():
    """跳转目标就是当前页时也要停 —— 否则两跳额度在原地用完，判断会跟着乱。"""
    client = XdaoClient()
    url = "https://www.nmbxd1.com/loop.html"
    page = (
        '<html><head><title>跳转提示</title></head><body>'
        f'<p class="jump"><a id="href" href="{url}">跳转</a></p></body></html>'
    )
    fake, calls = _responder({}, default=page)
    client._request = fake  # type: ignore[method-assign]

    _, final = client._request_following_jumps(url)

    assert final == url
    assert calls == [url]


def test_apply_cookie_reports_a_session_that_did_not_stick():
    """被弹回登录页时不能说「账号里没有饼干」—— 真相是这次登录根本没进去。"""
    client = XdaoClient()
    fake, _ = _responder({}, default=JUMP_TO_LOGIN)
    client._request = fake  # type: ignore[method-assign]

    with pytest.raises(LoginError) as excinfo:
        client.apply_cookie()

    text = str(excinfo.value)
    assert "并没有权限访问" in text  # 把服务端原话带上
    assert "登录" in text
    assert "未找到可用的饼干" not in text


def test_apply_cookie_explains_an_empty_cookie_list():
    """真登录进去了、列表却是空的 —— 这时才该让用户去领一块饼干。"""
    client = XdaoClient()
    fake, _ = _responder({}, default="<html><body><p>饼干列表</p></body></html>")
    client._request = fake  # type: ignore[method-assign]

    with pytest.raises(XdaoError) as excinfo:
        client.apply_cookie()

    text = str(excinfo.value)
    assert "饼干" in text
    assert "用户系统" in text


def test_apply_cookie_returns_the_userhash_from_the_export_link():
    client = XdaoClient()
    fake, _ = _responder(
        {
            "/Cookie/index.html": COOKIE_LIST,
            "/Cookie/export/": '{"cookie": "REALHASH123"}',
        },
        default="<html></html>",
    )
    client._request = fake  # type: ignore[method-assign]

    assert client.apply_cookie() == "REALHASH123"


def test_apply_cookie_strips_the_html_suffix_before_building_the_next_url():
    """列表页链接带 ``.html`` 时，自己拼地址不能再叠一次。

    钉住的是**两个请求的确切地址**：用宽松的路由键匹配，「…/switchTo/id/abc123.html.html」
    这种 404 会悄悄溜过去 —— v0.13.23 之前这条路一直没被钉住。
    """
    client = XdaoClient()
    switch_url = "https://www.nmbxd1.com/Member/User/Cookie/switchTo/id/abc123.html"
    export_url = "https://www.nmbxd1.com/Member/User/Cookie/export/id/abc123.html"
    success = (
        '<html><head><title>跳转提示</title></head><body>'
        '<p class="success">饼干切换成功!</p>'
        '<p class="jump"><a id="href" href="">跳转</a></p></body></html>'
    )
    fake, calls = _responder(
        {
            "/Cookie/index.html": COOKIE_LIST,
            switch_url: success,
            export_url: '{"cookie": "HASHFROMEXPORT"}',
        }
    )
    client._request = fake  # type: ignore[method-assign]

    assert client.apply_cookie() == "HASHFROMEXPORT"
    assert calls == [
        "https://www.nmbxd1.com/Member/User/Cookie/index.html",
        switch_url,
        export_url,
    ]
    assert {cookie.value for cookie in client._jar if cookie.name == "userhash"} == {
        "HASHFROMEXPORT"
    }


def test_login_reports_a_jump_back_to_the_login_page():
    """登录 POST 也被「跳转提示」页回话时，必须说清是登录没过，而不是饼干的问题。"""
    client = XdaoClient()
    fake, calls = _responder({}, default=JUMP_TO_LOGIN)
    client._request = fake  # type: ignore[method-assign]

    with pytest.raises(LoginError) as excinfo:
        client.login("someone@example.com", "pw", "abcd", "hash")

    text = str(excinfo.value)
    assert "并没有权限访问" in text
    assert "验证码" in text  # 给出可操作的下一步
    assert all("Cookie" not in url for url in calls), "登录没过就不该再去抓饼干列表"


# ---- 验证码响应体：实测 verify.html 把 PNG 包在 gzip 里，HTTP 头却写 image/png ----

CAPTCHA_PNG = b"\x89PNG\r\n\x1a\n" + bytes(range(64))


def test_decode_response_body_unwraps_gzip_when_the_header_says_png():
    """只看 Content-Encoding 会漏：头写 image/png，体却是 gzip 包。"""
    packed = gzip.compress(CAPTCHA_PNG)
    assert packed[:2] == b"\x1f\x8b"  # 前提：压缩后带 gzip 魔数

    assert _decode_response_body(packed, "image/png") == CAPTCHA_PNG
    # 没有头可用时也必须认（魔数兜底），否则替换 _request 的调用路径会漏。
    assert _decode_response_body(packed) == CAPTCHA_PNG
    assert _decode_response_body(packed, "application/octet-stream") == CAPTCHA_PNG


def test_decode_response_body_leaves_plain_bodies_alone():
    assert _decode_response_body(CAPTCHA_PNG, "image/png") == CAPTCHA_PNG
    assert _decode_response_body(b"<html>login</html>", "text/html") == b"<html>login</html>"
    assert _decode_response_body(b"", "image/png") == b""


def test_decode_response_body_keeps_the_original_when_the_gzip_is_broken():
    """截断/损坏的响应不该在解码这一层抛异常，交给调用方按原样报错。"""
    broken = b"\x1f\x8b" + b"\x00" * 8
    assert _decode_response_body(broken) == broken


def test_fetch_login_form_hands_the_gui_a_real_png():
    """界面拿 PhotoImage 直接吃 captcha_bytes：这里必须是解压后的真 PNG。"""
    client = XdaoClient()
    login_html = '<html><body><input name="__hash__" value="HASH123"></body></html>'
    calls: list[str] = []

    def fake_request(url, data=None, **kwargs):
        calls.append(url)
        if "verify.html" in url:
            return gzip.compress(CAPTCHA_PNG)
        return login_html.encode("utf-8")

    client._request = fake_request  # type: ignore[method-assign]

    form = client.fetch_login_form()

    assert form.captcha_bytes == CAPTCHA_PNG
    assert form.captcha_bytes[:8] == b"\x89PNG\r\n\x1a\n"
    assert form.hash_value == "HASH123"
    assert any("verify.html" in url for url in calls)


class _FakeResponse:
    def __init__(self, body: bytes, headers: dict[str, str]) -> None:
        self._body = body
        self.headers = headers

    def read(self, size: int = -1) -> bytes:
        return self._body if size < 0 else self._body[:size]

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> bool:
        return False


class _FakeOpener:
    def __init__(self, response: _FakeResponse) -> None:
        self.response = response

    def open(self, req, timeout=None):  # 只求签名兼容，不看请求内容
        return self.response


def test_request_records_response_headers_in_lower_case():
    """fetch_login_form 靠这份头判断要不要解压，键必须统一小写。"""
    client = XdaoClient()
    packed = gzip.compress(CAPTCHA_PNG)
    client._opener = _FakeOpener(_FakeResponse(packed, {"Content-Type": "image/png"}))  # type: ignore[assignment]

    raw = client._request("https://www.nmbxd1.com/Member/User/Index/verify.html")

    assert raw == packed, "头里没写 Content-Encoding: gzip，这一层不该自己解压"
    assert client._last_response_headers["content-type"] == "image/png"


def test_import_cookies_pours_a_browser_jar_into_the_client() -> None:
    """v0.13.22：浏览器里那罐饼干整罐倒进来，HTTP 那条路才有会话可用。"""
    client = XdaoClient()
    count = client.import_cookies(
        [
            {"name": "PHPSESSID", "value": "abc123", "domain": ".nmbxd1.com", "path": "/"},
            {"name": "userhash", "value": "OLD12345", "domain": ".nmbxd1.com", "path": "/"},
        ]
    )
    assert count == 2
    poured = {cookie.name: cookie.value for cookie in client._jar}
    assert poured == {"PHPSESSID": "abc123", "userhash": "OLD12345"}


def test_import_cookies_replaces_the_same_cookie_instead_of_stacking_it() -> None:
    """同名同域同路径的饼干只能有一条：不然请求头里会出现两条 userhash。"""
    client = XdaoClient()
    client.import_cookies(
        [{"name": "userhash", "value": "OLDHASH", "domain": ".nmbxd1.com", "path": "/"}]
    )
    client.import_cookies(
        [{"name": "userhash", "value": "NEWHASH", "domain": ".nmbxd1.com", "path": "/"}]
    )
    pairs = [(cookie.domain, cookie.path) for cookie in client._jar if cookie.name == "userhash"]
    assert pairs == [(".nmbxd1.com", "/")], "同名同域同路径的饼干叠成了两条"
    assert next(iter(client._jar)).value == "NEWHASH"


def test_import_cookies_replaces_a_stale_userhash_from_another_domain() -> None:
    """客户端 jar 里已经有一条 userhash（别的域）时，导入后不能留下两条。

    同一个请求头里出现 ``userhash=新; userhash=旧`` 时服务端取哪一条并不确定 ——
    `set_userhash` 的注释里记着这个实测过的坑（登录成功却仍被接口回「必须登入领取
    饼干后才可以访问」）。浏览器那份饼干可能同时在几个域上带同一个名字，所以导入时
    要先把同名的旧值扫干净，而不是只换掉同域同路径的那一条。
    """
    client = XdaoClient()
    client.set_userhash("OLDHASH")  # 4 个域各一条
    assert client.import_cookies(
        [{"name": "userhash", "value": "NEWHASH", "domain": ".nmbxd1.com", "path": "/"}]
    ) == 1
    left = [(cookie.domain, cookie.value) for cookie in client._jar if cookie.name == "userhash"]
    assert left == [(".nmbxd1.com", "NEWHASH")], left


def test_import_cookies_skips_entries_without_a_name_or_a_value() -> None:
    """CDP 那边偶尔会给出空条目；装进去只会污染请求头。"""
    client = XdaoClient()
    assert client.import_cookies([{"name": "", "value": "x"}, {"name": "a", "value": ""}]) == 0
    assert len(client._jar) == 0


def test_import_cookies_falls_back_to_sane_domains_and_paths() -> None:
    """CDP 的会话饼干没有 domain/path 时，按站点缺省补上，别装成一条谁都不发的饼干。"""
    client = XdaoClient()
    assert client.import_cookies([{"name": "PHPSESSID", "value": "abc123"}]) == 1
    cookie = next(iter(client._jar))
    assert cookie.domain == ".nmbxd1.com"
    assert cookie.path == "/"


def test_import_cookies_keeps_a_session_cookie_alive_within_this_client() -> None:
    """浏览器里的会话饼干（CDP 给 expires=-1）不能因为「30 秒前就过期了」被丢掉。"""
    client = XdaoClient()
    client.import_cookies(
        [{"name": "PHPSESSID", "value": "abc123", "domain": ".nmbxd1.com", "expires": -1}]
    )
    cookie = next(iter(client._jar))
    assert cookie.expires is None
    assert cookie.is_expired() is False
    assert cookie.discard is True


# ---------------------------------------------------------------- 错误与兜底路径
#
# 覆盖率体检（2026-10-18）发现 client.py 有一大段没被任何用例走过：429/500/超时/断网
# 的重试与报错文案、响应体大小上限、接口前缀探测的失败分支、userhash 的三种导出形状。
# 这些都是用户真能撞上的路径（限流、服务端 502、连不上），却只靠手测过，所以补在这里。


class _RaisingOpener:
    """按顺序抛预设异常；用完之后一直抛最后一个。"""

    def __init__(self, *errors: BaseException) -> None:
        self.errors = list(errors)
        self.calls = 0

    def open(self, req, timeout=None):  # 只求签名兼容，不看请求内容
        self.calls += 1
        index = min(self.calls, len(self.errors)) - 1
        raise self.errors[index]


def _http_error(code: int, body: bytes = b"", retry_after: str | None = None):
    headers = {} if retry_after is None else {"Retry-After": retry_after}
    return urllib.error.HTTPError(
        "https://www.nmbxd1.com/Api/thread", code, "boom", headers, io.BytesIO(body)
    )


def _quiet_client() -> XdaoClient:
    """不真睡的客户端：退避改成空转，限速关掉。"""
    client = XdaoClient(throttle=0.0)
    client._backoff = lambda *args, **kwargs: None  # type: ignore[method-assign]
    return client


def test_request_reports_the_rate_limit_and_stops_retrying():
    """429 是接口明确说「你太快了」，重试到最后一轮后要给人一句能照做的提示。"""
    client = _quiet_client()
    client._opener = _RaisingOpener(_http_error(429), _http_error(429))  # type: ignore[assignment]

    with pytest.raises(XdaoError, match="限流（429）") as excinfo:
        client._request("https://www.nmbxd1.com/Api/thread", retries=1)

    assert client._opener.calls == 2, "429 属于可重试错误，应该试满两轮再放弃"
    assert "请求间隔" in str(excinfo.value), "提示里要告诉用户去调大请求间隔"


def test_request_keeps_a_snippet_of_a_500_body_for_diagnosis():
    """服务端 5xx 时把响应体开头带上，回帖求助时能直接贴出来。"""
    client = _quiet_client()
    client._opener = _RaisingOpener(  # type: ignore[assignment]
        _http_error(500, "<h1>数据库连接失败</h1>".encode("utf-8"))
    )

    with pytest.raises(XdaoError, match="请求失败 500") as excinfo:
        client._request("https://www.nmbxd1.com/Api/thread", retries=0)

    assert "数据库连接失败" in str(excinfo.value)


def test_request_does_not_retry_a_plain_404():
    """4xx（除 429）是确定性错误：页面就是没有，重试只浪费时间。"""
    client = _quiet_client()
    client._opener = _RaisingOpener(_http_error(404, b"not found"))  # type: ignore[assignment]

    with pytest.raises(XdaoError, match="请求失败 404"):
        client._request("https://www.nmbxd1.com/Api/thread", retries=3)

    assert client._opener.calls == 1


def test_request_reports_a_timeout_after_the_last_retry():
    client = _quiet_client()
    client._opener = _RaisingOpener(TimeoutError(), TimeoutError())  # type: ignore[assignment]

    with pytest.raises(XdaoError, match="请求超时") as excinfo:
        client._request("https://www.nmbxd1.com/Api/thread", retries=1)

    assert client._opener.calls == 2
    assert "秒" in str(excinfo.value), "超时文案里要带上等了多久"


def test_request_reports_a_network_error_after_the_last_retry():
    client = _quiet_client()
    client._opener = _RaisingOpener(  # type: ignore[assignment]
        ConnectionResetError("reset"), ConnectionResetError("reset")
    )

    with pytest.raises(XdaoError, match="网络错误"):
        client._request("https://www.nmbxd1.com/Api/thread", retries=1)

    assert client._opener.calls == 2


def test_request_stops_reading_a_body_over_the_size_cap():
    """图片/HTML 接口都带 max_bytes：超过上限立刻失败，别把内存吃光。"""
    client = _quiet_client()
    client._opener = _FakeOpener(_FakeResponse(b"x" * 5000, {}))  # type: ignore[assignment]

    with pytest.raises(XdaoError, match="KB 上限"):
        client._request("https://www.nmbxd1.com/Api/thread", max_bytes=4096)


def test_backoff_honours_retry_after_and_caps_it(monkeypatch):
    """服务器给了 Retry-After 就听它的（夹在 0~30 秒），给了乱码才走指数退避。"""
    slept: list[float] = []
    monkeypatch.setattr(client_module.time, "sleep", slept.append)

    XdaoClient._backoff(0, retry_after="2")
    XdaoClient._backoff(0, retry_after="300")
    XdaoClient._backoff(2, retry_after="不是数字")

    assert slept == [2.0, 30.0, 3.2], f"退避秒数不对：{slept}"


def test_api_get_json_reports_when_every_prefix_is_dead():
    """两个接口前缀都连不上时，报的是最后那次错误，不能只丢一个空异常。"""
    client = _quiet_client()

    def dead_request(url, data=None, **kwargs):
        raise XdaoError(f"连不上：{url}")

    client._request = dead_request  # type: ignore[method-assign]

    with pytest.raises(XdaoError, match="无法连接 X 岛接口"):
        client._api_get_json("thread", {"id": 1})


def test_api_get_json_moves_to_the_backup_prefix_when_the_first_one_fails():
    """首选前缀整个连不上时，自动换备用前缀把数据取回来。"""
    client = _quiet_client()
    client._api_base = client.API_BASES[0]
    primary = client.API_BASES[0]

    def flaky_request(url, data=None, **kwargs):
        if url.startswith(primary):
            raise XdaoError("首选前缀连不上")
        if "/getCDNPath" in url:
            return b"[]"
        return b'{"isOK": true, "id": 1}'

    client._request = flaky_request  # type: ignore[method-assign]

    assert client._api_get_json("thread", {"id": 1}) == {"isOK": True, "id": 1}
    assert client._api_base == client.API_BASES[1], "换过前缀之后要把新前缀记下来"


def test_extract_userhash_reads_the_three_export_shapes():
    """「导出资料」三种形状都见过：JSON 的 cookie 字段、userhash= 参数、坏 JSON 里的字面量。"""
    assert XdaoClient._extract_userhash_from_export('{"cookie": "abc123"}') == "abc123"
    assert XdaoClient._extract_userhash_from_export("userhash=xyz789; path=/") == "xyz789"
    assert XdaoClient._extract_userhash_from_export('{ "cookie": "qqq111", 这后面是坏的 }') == "qqq111"
    assert XdaoClient._extract_userhash_from_export("这里没有饼干") is None


def test_page_count_prefers_the_explicit_field_then_falls_back_to_the_reply_count():
    """接口有时不给页数：给了 PageCount 就信它，没给则按每页 19 条折算。"""
    client = XdaoClient()
    assert client._extract_page_count({"PageCount": "7"}) == 7
    assert client._extract_page_count({"ReplyCount": 38}) == 3  # 38 // 19 + 1
    assert client._extract_page_count({}) == 1
    assert client._extract_page_count(None) == 1


# ---------------------------------------------------------------- 图片缓存
#
# 覆盖率体检（2026-10-18）：``download_image`` 与 ``_cache_path`` 也没被走过。
# 这两段在真导出里天天跑（每张图片一次），出错的表现是「图片下载两遍」或
# 「缓存坏一张就整趟失败」，所以把命中、坏了、写不进去三种情况都钉住。


class _FakeImageDownloader:
    """只记账、不联网的 ``_request``。"""

    def __init__(self, data: bytes = b"\x89PNG-data") -> None:
        self.data = data
        self.calls: list[dict] = []

    def __call__(self, url: str, **kwargs):
        self.calls.append({"url": url, **kwargs})
        return self.data


def test_download_image_writes_the_cache_and_reuses_it(tmp_path):
    """第一次下载后写进缓存；第二次同 URL 直接读文件，不再发请求。"""
    client = XdaoClient()
    client.image_cache_dir = tmp_path / "images"
    fake = _FakeImageDownloader()
    client._request = fake  # type: ignore[method-assign]

    first = client.download_image("https://img.example/a.png")
    second = client.download_image("https://img.example/a.png")

    assert first == second == b"\x89PNG-data"
    assert len(fake.calls) == 1, "第二次应当命中缓存，不该再请求"
    cached = list((tmp_path / "images").rglob("*"))
    assert [p for p in cached if p.is_file()], "缓存文件应当落在 image_cache_dir 下"
    assert not [p for p in cached if p.suffix == ".part"], "临时文件写完要换名，不该留下"


def test_download_image_redownloads_when_the_cache_file_is_broken(tmp_path):
    """缓存文件读不出来（被占、坏了）不能拖垮导出：重新下载即可。"""
    client = XdaoClient()
    client.image_cache_dir = tmp_path / "images"
    path = client._cache_path("https://img.example/b.png")
    path.parent.mkdir(parents=True)
    path.mkdir()  # 同名目录 → read_bytes 抛 OSError
    fake = _FakeImageDownloader(b"\x89PNG-fresh")
    client._request = fake  # type: ignore[method-assign]

    assert client.download_image("https://img.example/b.png") == b"\x89PNG-fresh"
    assert len(fake.calls) == 1


def test_download_image_survives_a_cache_write_failure(tmp_path, monkeypatch):
    """缓存目录写不进去（只读、满了）也只影响下次要不要重下，不影响这次导出。"""
    client = XdaoClient()
    client.image_cache_dir = tmp_path / "images"
    client._request = _FakeImageDownloader()  # type: ignore[method-assign]

    def refuse(self, *args, **kwargs):  # noqa: ANN001
        raise OSError(28, "磁盘满了")

    monkeypatch.setattr(type(tmp_path), "write_bytes", refuse, raising=False)

    assert client.download_image("https://img.example/c.png") == b"\x89PNG-data"


def test_download_image_skips_the_cache_without_a_directory_or_for_data_urls(tmp_path):
    """没配缓存目录、或者是 ``data:`` 内嵌图时，根本不该算缓存路径。"""
    client = XdaoClient()
    assert client._cache_path("https://img.example/d.png") is None

    client.image_cache_dir = tmp_path / "images"
    assert client._cache_path("data:image/png;base64,AAAA") is None
    assert client._cache_path("") is None


def test_download_image_relaxes_the_timeout_and_caps_the_size(tmp_path):
    """图片是「大、慢、可重试」的：超时按客户端默认值放宽，大小另有一个上限。"""
    client = XdaoClient(timeout=5.0)
    fake = _FakeImageDownloader()
    client._request = fake  # type: ignore[method-assign]

    client.download_image("https://img.example/e.png", use_cache=False)

    call = fake.calls[0]
    assert call["timeout"] >= 30.0, "图片服务器偶尔很慢，5 秒太紧"
    assert call["max_bytes"] == client_module.MAX_IMAGE_BYTES
    assert call["url"] == "https://img.example/e.png"



# ---------- 覆盖率体检补的分支（v0.19.6）----------


class _FakeResponse:
    """够用的假响应：支持 read(n)、headers、上下文管理。"""

    def __init__(self, body: bytes, headers: dict[str, str] | None = None) -> None:
        self._body = body
        self.headers = dict(headers or {})

    def read(self, size: int | None = None) -> bytes:
        if size is None:
            chunk, self._body = self._body, b""
            return chunk
        chunk, self._body = self._body[:size], self._body[size:]
        return chunk

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *_exc) -> bool:
        return False


class _ScriptedOpener:
    """按顺序吐出预先排好的「响应或异常」。"""

    def __init__(self, *script) -> None:
        self.script = list(script)
        self.requests: list[object] = []

    def open(self, request, timeout=None):  # noqa: ANN001, ARG002
        self.requests.append(request)
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def _errors_response(code: int, body: bytes = b"") -> urllib.error.HTTPError:
    return urllib.error.HTTPError("https://example/x", code, "boom", {}, io.BytesIO(body))


class _NullSettings:
    def __init__(self, **kwargs) -> None:
        self.__dict__.update(kwargs)


def test_proxy_from_env_reads_both_spellings(monkeypatch) -> None:
    # 注意：Windows 上 os.environ 的键不分大小写，所以不能先 setenv("HTTPS_PROXY")
    # 再 delenv("https_proxy") —— 那会把刚设的那条一起删掉。先全清再设。
    keys = ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy")
    for key in keys:
        monkeypatch.delenv(key, raising=False)
    assert XdaoClient._proxy_from_env() == ""

    monkeypatch.setenv("HTTPS_PROXY", "  http://127.0.0.1:7890  ")
    assert XdaoClient._proxy_from_env() == "http://127.0.0.1:7890"

    monkeypatch.delenv("HTTPS_PROXY", raising=False)
    monkeypatch.setenv("https_proxy", "http://127.0.0.1:8888")
    assert XdaoClient._proxy_from_env() == "http://127.0.0.1:8888"

    monkeypatch.delenv("https_proxy", raising=False)
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:9999")
    assert XdaoClient._proxy_from_env() == "http://127.0.0.1:9999"


def test_apply_config_takes_timeout_retries_and_proxy(monkeypatch) -> None:
    monkeypatch.delenv("HTTPS_PROXY", raising=False)
    monkeypatch.delenv("https_proxy", raising=False)
    client = XdaoClient()

    client.apply_config(
        _NullSettings(timeout=7, retries=4, throttle=0.5, proxy="http://127.0.0.1:7890")
    )

    assert client.timeout == 7.0
    assert client.retries == 4
    assert client._throttle_interval == 0.5
    assert client.proxy == "http://127.0.0.1:7890"


def test_apply_config_keeps_the_same_opener_when_nothing_changed(monkeypatch) -> None:
    monkeypatch.delenv("HTTPS_PROXY", raising=False)
    client = XdaoClient()
    opener = client._opener

    client.apply_config(_NullSettings(timeout=20, retries=2, throttle=0.08, proxy=""))

    assert client._opener is opener


def test_request_retries_a_429_and_then_succeeds(monkeypatch) -> None:
    client = XdaoClient(retries=1, throttle=0.0)
    backs: list[int] = []
    monkeypatch.setattr(client, "_backoff", lambda attempt, retry_after=None: backs.append(attempt))
    client._opener = _ScriptedOpener(_errors_response(429), _FakeResponse(b"ok"))

    assert client._request("https://example/x") == b"ok"
    assert backs == [0]


def test_request_explains_rate_limiting_after_the_last_try(monkeypatch) -> None:
    client = XdaoClient(retries=0, throttle=0.0)
    client._opener = _ScriptedOpener(_errors_response(429))

    with pytest.raises(XdaoError) as caught:
        client._request("https://example/x")

    assert "请求过于频繁" in str(caught.value)


def test_request_reports_a_server_error_with_the_body(monkeypatch) -> None:
    client = XdaoClient(retries=0, throttle=0.0)
    client._opener = _ScriptedOpener(_errors_response(503, "维护中".encode("utf-8")))

    with pytest.raises(XdaoError) as caught:
        client._request("https://example/x")

    assert "请求失败 503" in str(caught.value)
    assert "维护中" in str(caught.value)


def test_request_refuses_a_body_over_the_cap(monkeypatch) -> None:
    client = XdaoClient(retries=0, throttle=0.0)
    client._opener = _ScriptedOpener(_FakeResponse(b"x" * 4096))

    with pytest.raises(XdaoError) as caught:
        client._request("https://example/x", max_bytes=2048)

    assert "超过 2 KB 上限" in str(caught.value)


def test_request_decompresses_a_gzip_body(monkeypatch) -> None:
    client = XdaoClient(retries=0, throttle=0.0)
    body = gzip.compress("中文内容".encode("utf-8"))
    client._opener = _ScriptedOpener(_FakeResponse(body, {"Content-Encoding": "gzip"}))

    assert client._request("https://example/x").decode("utf-8") == "中文内容"
    assert client._last_response_headers.get("content-encoding") == "gzip"


def test_request_turns_a_timeout_into_a_readable_message(monkeypatch) -> None:
    client = XdaoClient(retries=0, throttle=0.0)
    client._opener = _ScriptedOpener(TimeoutError("timed out"))

    with pytest.raises(XdaoError) as caught:
        client._request("https://example/x")

    assert "请求超时" in str(caught.value)


def test_request_turns_a_network_error_into_a_readable_message(monkeypatch) -> None:
    client = XdaoClient(retries=0, throttle=0.0)
    client._opener = _ScriptedOpener(urllib.error.URLError("名字解析失败"))

    with pytest.raises(XdaoError) as caught:
        client._request("https://example/x")

    assert "网络错误" in str(caught.value)


def test_update_cdn_path_takes_the_first_url(monkeypatch) -> None:
    client = XdaoClient()
    monkeypatch.setattr(client, "_api_get_json", lambda path, params: [{"url": "https://cdn.example/"}, {}])

    assert client.update_cdn_path() == "https://cdn.example/"


def test_update_cdn_path_keeps_the_old_one_when_the_answer_is_empty(monkeypatch) -> None:
    client = XdaoClient()
    monkeypatch.setattr(client, "_api_get_json", lambda path, params: [])

    assert client.update_cdn_path() == client.cdn_path


def test_image_url_builds_image_and_thumb_links() -> None:
    client = XdaoClient()

    assert client.image_url("abcdef", ".jpg").endswith("/image/abcdef.jpg")
    assert client.image_url("abcdef", ".jpg", thumb=True).endswith("/thumb/abcdef.jpg")

    client.cdn_path = "https://cdn.example"
    assert client.image_url("abcdef", ".png") == "https://cdn.example/image/abcdef.png"


def test_api_get_json_reports_a_non_json_page(monkeypatch) -> None:
    client = XdaoClient()
    monkeypatch.setattr(client, "_resolve_api_base", lambda: "https://api.example")
    monkeypatch.setattr(client, "_request", lambda *a, **k: b"<html>error</html>")

    with pytest.raises(XdaoError) as caught:
        client._api_get_json("getThreadList", {})

    assert "非 JSON" in str(caught.value)


def test_jump_page_url_finds_the_target_whichever_attribute_comes_first() -> None:
    first = '<html><a id="href" href="https://www.nmbxd1.com/x">go</a></html>'
    second = '<html><a href="https://www.nmbxd1.com/y" id="href">go</a></html>'

    assert XdaoClient.jump_page_url(first) == "https://www.nmbxd1.com/x"
    assert XdaoClient.jump_page_url(second) == "https://www.nmbxd1.com/y"
    assert XdaoClient.jump_page_url("<html>普通页面</html>") == ""
    assert XdaoClient.jump_page_url("") == ""


def test_jump_page_message_unescapes_and_flattens_html() -> None:
    html = '<html><p class="error">没有&amp;权限<br>访问</p></html>'

    assert XdaoClient.jump_page_message(html) == "没有&权限 访问"
    assert XdaoClient.jump_page_message("<html></html>") == ""
