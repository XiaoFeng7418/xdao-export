"""客户端层的用例：只测不联网就能验的部分（Cookie 管理、解析、登录页兜底）。"""

from __future__ import annotations

import gzip

import pytest

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

