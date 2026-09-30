"""客户端层的用例：只测不联网就能验的部分（Cookie 管理、解析、登录页兜底）。"""

from __future__ import annotations

import pytest

from xdao.client import LoginError, XdaoClient, XdaoError

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

