"""客户端层的用例：只测不联网就能验的部分（Cookie 管理、解析）。"""

from __future__ import annotations

from xdao.client import XdaoClient


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
