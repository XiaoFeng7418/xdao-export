"""测试用例集合。

这里只放测试，不放导出实现；测试全部离线运行，用 ``FakeClient`` 顶替网络访问。
"""

from __future__ import annotations

import base64

from xdao.client import Post
from xdao.exporters._shared import ThreadData

# 一张最小的假 JPEG（魔数 + 少量数据），够用来验证 MIME 判断与内嵌。
SAMPLE_JPEG = b"\xff\xd8\xff\xe0" + b"xdao-fake-image-data" * 3
# 一张假 PNG。
SAMPLE_PNG = b"\x89PNG\r\n\x1a\n" + b"xdao-fake-png" * 2


class FakeClient:
    """离线替身：只实现导出器用到的那几个接口。"""

    def __init__(
        self,
        cdn_path: str = "https://image.nmb.best/",
        images: dict[str, bytes] | None = None,
        fail_urls: set[str] | None = None,
    ) -> None:
        self.cdn_path = cdn_path
        self.images = dict(images or {})
        self.fail_urls = set(fail_urls or set())
        self.downloaded: list[str] = []

    def image_url(self, img: str, ext: str, thumb: bool = False) -> str:
        prefix = self.cdn_path
        if prefix and not prefix.endswith("/"):
            prefix += "/"
        kind = "thumb" if thumb else "image"
        return f"{prefix}{kind}/{img}{ext}"

    def download_image(self, url: str) -> bytes:
        self.downloaded.append(url)
        if url in self.fail_urls:
            raise OSError("图片下载失败（测试替身）")
        if url in self.images:
            return self.images[url]
        # 未登记的地址默认给一张 JPEG，便于测试"能下到图"的路径。
        return SAMPLE_JPEG


def make_post(
    post_id: int = 12345678,
    content: str = "",
    *,
    user_hash: str = "abc123",
    name: str = "无名氏",
    title: str = "",
    now: str = "2026-09-23 12:00",
    img: str = "",
    ext: str = "",
    admin: int = 0,
    sage: int = 0,
    is_po: bool = False,
) -> Post:
    """构造一条发言，未指定的字段给合理默认值。"""
    return Post(
        id=post_id,
        user_hash=user_hash,
        name=name,
        title=title,
        content=content,
        now=now,
        img=img,
        ext=ext,
        admin=admin,
        sage=sage,
        is_po=is_po,
    )


def sample_thread(title: str = "测试串标题") -> ThreadData:
    """2 楼的样例串：主帖带一张附件图 + 正文里一张图，1 楼回复是红名非 PO。"""
    main = make_post(
        1001,
        "主帖正文<br>第二行<br><img src=\"/image/inline.jpg\">",
        user_hash="POCOOKIE",
        img="abcdef",
        ext=".jpg",
        is_po=True,
    )
    reply = make_post(
        1002,
        "回复正文",
        user_hash="other999",
        name="路人甲",
        admin=1,
    )
    return ThreadData(thread_id=1001, title=title, po_hash="POCOOKIE", posts=[main, reply])


def sample_thread_with_po_reply(title: str = "测试串标题") -> ThreadData:
    """3 楼样例：主帖 + 饼干同 PO 的回复 + 普通回复。"""
    thread = sample_thread(title)
    thread.posts.append(
        make_post(1003, "PO 的第二条发言", user_hash="POCOOKIE", is_po=False)
    )
    return thread


def data_uri_bytes(uri: str) -> bytes:
    """从 data URI 里取出原始字节。"""
    _, _, payload = uri.partition(",")
    return base64.b64decode(payload)
