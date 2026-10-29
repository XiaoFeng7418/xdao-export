"""公共基础模块与 Markdown 导出器的测试。"""

from __future__ import annotations

import base64
import threading
import urllib.parse
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from tests import (
    FakeClient,
    SAMPLE_JPEG,
    data_uri_bytes,
    make_post,
    sample_thread,
    sample_thread_with_po_reply,
)
from xdao.exporters._shared import (
    ThreadData,
    derive_filename,
    fetch_image,
    guess_mime,
    iter_post_image_urls,
    markdown_images_to_xhtml,
    plain_text,
    render_inline_content,
    resolve_image_url,
    sanitize_filename,
)
from xdao.cancel import ExportCancelled
from xdao.exporters.markdown import MarkdownBuilder


def _embed(client: FakeClient, url: str) -> str:
    """测试用的 embedder：能下到图就给 data URI，否则给空串。"""
    data = fetch_image(client, url)
    if not data:
        return ""
    return "data:image/jpeg;base64," + base64.b64encode(data).decode("ascii")


# ---------- 1. 文件名推导 ----------


def test_derive_filename_uses_title():
    assert derive_filename(sample_thread("某个很长的标题")) == "某个很长的标题"


def test_derive_filename_falls_back_to_first_sentence_of_main_post():
    post = make_post(1001, "第一句话。第二句话！<br>第三行?", is_po=True)
    thread = ThreadData(thread_id=1001, title="无标题", po_hash="POCOOKIE", posts=[post])
    assert derive_filename(thread) == "第一句话"


def test_derive_filename_handles_half_width_punctuation():
    post = make_post(1001, "half width!second?third", is_po=True)
    thread = ThreadData(thread_id=1001, title="  ", po_hash="POCOOKIE", posts=[post])
    assert derive_filename(thread) == "half width"


def test_derive_filename_falls_back_to_thread_id():
    post = make_post(1001, "", is_po=True)
    thread = ThreadData(thread_id=1001, title="无标题", po_hash="", posts=[post])
    assert derive_filename(thread) == "串1001"


def test_derive_filename_without_posts():
    thread = ThreadData(thread_id=42, title="无标题", po_hash="", posts=[])
    assert derive_filename(thread) == "串42"


# ---------- 2. 文件名安全化 ----------


def test_sanitize_filename_replaces_illegal_chars():
    assert sanitize_filename('a\\b/c:d*e?f"g<h>i|j') == "a_b_c_d_e_f_g_h_i_j"


def test_sanitize_filename_strips_spaces_and_dots():
    assert sanitize_filename("  ..标题..  ") == "标题"


def test_sanitize_filename_truncates_long_names():
    result = sanitize_filename("标" * 200)
    assert len(result) == 80
    assert result == "标" * 80


def test_sanitize_filename_falls_back_when_empty():
    assert sanitize_filename("") == "xdao-thread"
    assert sanitize_filename("   ", fallback="兜底") == "兜底"
    assert sanitize_filename("...") == "xdao-thread"


# ---------- 3. 纯文本 ----------


def test_plain_text_converts_br_and_strips_tags():
    assert plain_text("第一行<br>第二行<br/>第三行") == "第一行\n第二行\n第三行"


def test_plain_text_strips_tags_and_unescapes_entities():
    assert plain_text('<span class="x">5 &lt; 6 &amp; 7</span>') == "5 < 6 & 7"


def test_plain_text_handles_empty_input():
    assert plain_text("") == ""


# ---------- 4. 图片 URL 与正文渲染 ----------


def test_resolve_image_url_fills_cdn_prefix():
    client = FakeClient(cdn_path="https://image.nmb.best")
    assert resolve_image_url(client, "/image/a.jpg") == "https://image.nmb.best/image/a.jpg"
    assert resolve_image_url(client, "image/a.jpg") == "https://image.nmb.best/image/a.jpg"
    assert resolve_image_url(client, "https://other.example/b.png") == "https://other.example/b.png"
    assert resolve_image_url(client, "") == ""


def test_iter_post_image_urls_collects_attachment_and_content_images():
    client = FakeClient()
    post = make_post(
        1001,
        '<img src="/image/one.jpg"><img src="https://cdn.example/two.png"><img src="/image/one.jpg">',
        img="attach",
        ext=".jpg",
    )
    assert iter_post_image_urls(client, post) == [
        "https://image.nmb.best/image/attach.jpg",
        "https://image.nmb.best/image/one.jpg",
        "https://cdn.example/two.png",
    ]


def test_render_inline_content_url_mode_completes_relative_image():
    client = FakeClient()
    text = render_inline_content(client, '看这个<img src="/image/a.jpg">好不好')
    assert "![图片](https://image.nmb.best/image/a.jpg)" in text
    assert "<img" not in text


def test_render_inline_content_embed_mode_produces_data_uri():
    client = FakeClient(images={"https://image.nmb.best/image/a.jpg": SAMPLE_JPEG})
    text = render_inline_content(
        client, '<img src="/image/a.jpg">', image_mode="embed", embedder=lambda url: _embed(client, url)
    )
    assert "![图片](data:image/jpeg;base64," in text


def test_render_inline_content_embed_falls_back_to_url_when_embedder_fails():
    client = FakeClient()
    text = render_inline_content(
        client, '<img src="/image/a.jpg">', image_mode="embed", embedder=lambda url: ""
    )
    assert "![图片](https://image.nmb.best/image/a.jpg)" in text


def test_render_inline_content_drop_mode_removes_images():
    client = FakeClient()
    text = render_inline_content(client, '前<img src="/image/a.jpg">后', image_mode="drop")
    assert "![" not in text
    assert "前后" in text


def test_render_inline_content_never_raises_on_download_failure():
    failed = "https://image.nmb.best/image/a.jpg"
    client = FakeClient(fail_urls={failed})
    text = render_inline_content(
        client, '<img src="/image/a.jpg">', image_mode="embed", embedder=lambda url: _embed(client, url)
    )
    # 下载失败时退回 URL 形式，不抛异常。
    assert failed in text
    assert "data:" not in text


def test_fetch_image_returns_none_on_failure():
    client = FakeClient(fail_urls={"https://image.nmb.best/image/a.jpg"})
    assert fetch_image(client, "https://image.nmb.best/image/a.jpg") is None
    assert fetch_image(client, "") is None


def test_guess_mime_prefers_magic_numbers():
    assert guess_mime("https://x/a.jpg", SAMPLE_JPEG) == "image/jpeg"
    assert guess_mime("https://x/a.png", b"\x89PNG\r\n\x1a\n") == "image/png"
    assert guess_mime("https://x/a.gif", b"GIF89a") == "image/gif"
    assert guess_mime("https://x/a.webp", b"RIFF\x00\x00\x00\x00WEBP") == "image/webp"
    # 没有数据时按扩展名，都没有则默认 jpeg。
    assert guess_mime("https://x/a.PNG?a=1") == "image/png"
    assert guess_mime("https://x/a") == "image/jpeg"


def test_render_inline_content_collapses_blank_lines():
    client = FakeClient()
    assert "\n\n\n" not in render_inline_content(client, "一<br><br><br><br>二")


# ---------- 5. Markdown 导出 ----------


def test_markdown_build_all_scope_counts_every_post():
    text = MarkdownBuilder(FakeClient()).build(sample_thread_with_po_reply(), "all")
    assert "## 1." in text
    assert "## 2." in text
    assert "## 3." in text
    assert "- 导出楼层:3" in text


def test_markdown_build_po_scope_keeps_main_post_and_po_reply():
    text = MarkdownBuilder(FakeClient()).build(sample_thread_with_po_reply(), "po")
    assert "- 导出楼层:2" in text
    assert "## 1. [PO] POCOOKIE" in text
    assert "## 2. [PO] POCOOKIE" in text
    assert "## 3." not in text
    assert "other999" not in text


def test_markdown_build_marks_po_admin_and_images():
    text = MarkdownBuilder(FakeClient()).build(sample_thread(), "all")
    # PO 标记只给 PO 楼层。
    assert "## 1. [PO] POCOOKIE" in text
    assert "## 2. other999" in text
    assert "[PO] other999" not in text
    # 红名标注。
    assert "> 饼干 `other999` · 红名 · 路人甲" in text
    # 附件图与正文内图片都变成了 Markdown 图片。
    assert "![图片](https://image.nmb.best/image/abcdef.jpg)" in text
    assert "![图片](https://image.nmb.best/image/inline.jpg)" in text
    # 正文里的 HTML 标签已经被清掉。
    assert "<br>" not in text


def test_markdown_build_header():
    text = MarkdownBuilder(FakeClient()).build(sample_thread("我的标题"), "all")
    assert text.split("\n")[0] == "# 我的标题"
    assert "- 串号:No.1001" in text
    assert "- PO 饼干:POCOOKIE" in text
    assert "- 导出时间:" in text


def test_markdown_keeps_angle_brackets_as_plain_text():
    client = FakeClient()
    post = make_post(1001, "5 &lt; 6 &amp; 7 &gt; 4", is_po=True)
    thread = ThreadData(thread_id=1001, title="转义", po_hash="POCOOKIE", posts=[post])
    text = MarkdownBuilder(client).build(thread, "all")
    assert "5 < 6 & 7 > 4" in text
    # 不能留下会被当成 HTML 的标签。
    assert "<b>" not in text


def test_markdown_save_writes_utf8_lf(artifacts_dir: Path):
    path = MarkdownBuilder(FakeClient()).save(sample_thread("保存测试"), "all", artifacts_dir)
    assert path == artifacts_dir / "保存测试.md"
    raw = path.read_bytes()
    assert b"\r\n" not in raw
    assert "主帖正文" in raw.decode("utf-8")


def test_markdown_save_sanitizes_filename(artifacts_dir: Path):
    path = MarkdownBuilder(FakeClient()).save(sample_thread("标题/带:非法*字符?"), "all", artifacts_dir)
    assert path.name == "标题_带_非法_字符_.md"
    assert path.exists()


def test_markdown_save_creates_output_dir(artifacts_dir: Path):
    target = artifacts_dir / "还没创建" / "子目录"
    path = MarkdownBuilder(FakeClient()).save(sample_thread(), "all", target)
    assert path.parent == target
    assert path.exists()


def test_markdown_progress_receives_messages(artifacts_dir: Path):
    messages: list[str] = []
    MarkdownBuilder(FakeClient(), progress=messages.append).save(sample_thread(), "all", artifacts_dir)
    assert any("Markdown" in message for message in messages)


@pytest.mark.parametrize("scope", ["all", "po"])
def test_markdown_scope_always_contains_main_post(scope: str):
    assert "## 1." in MarkdownBuilder(FakeClient()).build(sample_thread(), scope)


# ---------- 文件名模板与指定饼干 ----------


def test_markdown_save_honours_filename_template(artifacts_dir: Path):
    thread = sample_thread("模板标题")
    builder = MarkdownBuilder(FakeClient(), filename_template="[{id}] {title}")
    assert builder.output_name(thread) == "[1001] 模板标题"
    path = builder.save(thread, "all", artifacts_dir)
    assert path == artifacts_dir / "[1001] 模板标题.md"
    assert path.exists()


def test_markdown_without_template_falls_back_to_derived_name(artifacts_dir: Path):
    path = MarkdownBuilder(FakeClient()).save(sample_thread("没有模板"), "all", artifacts_dir)
    assert path.name == "没有模板.md"


def test_markdown_bad_template_falls_back():
    """模板写错时不应该让导出失败。"""
    builder = MarkdownBuilder(FakeClient(), filename_template="{unknown}")
    assert builder.output_name(sample_thread("兜底标题")) == "兜底标题"


def test_markdown_include_hashes_filters_posts():
    thread = sample_thread_with_po_reply()
    text = MarkdownBuilder(FakeClient()).build(thread, "all", ["other999"])
    # 只看指定饼干时，主帖不满足条件也会被排除（表头的 PO 饼干不算楼层）。
    assert "- 导出楼层:1" in text
    assert "## 1. other999" in text
    assert "## 2." not in text


def test_markdown_include_hashes_accepts_multiple():
    thread = sample_thread_with_po_reply()
    text = MarkdownBuilder(FakeClient()).build(thread, "all", ["POCOOKIE", "other999"])
    assert "- 导出楼层:3" in text


def test_markdown_include_hashes_save_passes_through(artifacts_dir: Path):
    path = MarkdownBuilder(FakeClient()).save(
        sample_thread_with_po_reply(), "all", artifacts_dir, ["other999"]
    )
    assert "- 导出楼层:1" in path.read_text(encoding="utf-8")


def test_markdown_images_to_xhtml_helper_is_reusable():
    """共享的 Markdown→XHTML 转换（EPUB 复用）顺带回归。"""
    fragment = markdown_images_to_xhtml(
        "第一段\n\n5 < 6 & 7\n\n![图片](data:image/jpeg;base64,QUJD)",
        lambda url: url,
    )
    assert fragment.count("<p>") == 2
    assert "5 &lt; 6 &amp; 7" in fragment
    assert '<img src="data:image/jpeg;base64,QUJD"' in fragment
    # 必须能被 XML 解析器接受。
    ET.fromstring(f"<div>{fragment}</div>")


def test_data_uri_bytes_roundtrip():
    uri = "data:image/jpeg;base64," + base64.b64encode(SAMPLE_JPEG).decode("ascii")
    assert data_uri_bytes(uri) == SAMPLE_JPEG

# ---------- 6. 图片存到同名目录（v0.20.0）----------


def test_markdown_default_mode_keeps_image_links(tmp_path: Path) -> None:
    """不传 image_mode（旧行为）：正文里是绝对链接，不下载任何图片。"""
    client = FakeClient()
    exporter = MarkdownBuilder(client)
    path = exporter.save(sample_thread(), "all", tmp_path)

    text = path.read_text(encoding="utf-8")

    assert exporter.image_mode == "url"
    assert "![图片](https://image.nmb.best/image/inline.jpg)" in text
    assert client.downloaded == []
    assert not path.with_name(path.stem + ".files").exists()


def test_markdown_embed_setting_falls_back_to_links(tmp_path: Path) -> None:
    """全局默认是「内嵌」：Markdown 是纯文本，不能塞 base64，按链接处理。"""
    client = FakeClient(images={"https://image.nmb.best/image/inline.jpg": SAMPLE_JPEG})
    exporter = MarkdownBuilder(client, image_mode="embed")
    text = exporter.build(sample_thread(), "all")

    assert exporter.image_mode == "url"
    assert "![图片](https://image.nmb.best/image/inline.jpg)" in text
    assert "data:image" not in text
    assert client.downloaded == []


def test_markdown_drop_mode_removes_every_image(tmp_path: Path) -> None:
    client = FakeClient()
    exporter = MarkdownBuilder(client, image_mode="drop")
    text = exporter.build(sample_thread(), "all")

    assert exporter.image_mode == "drop"
    assert "![图片]" not in text
    assert client.downloaded == []


def test_markdown_folder_mode_writes_images_beside_the_file(tmp_path: Path) -> None:
    client = FakeClient(
        images={
            "https://image.nmb.best/image/abcdef.jpg": SAMPLE_JPEG,
            "https://image.nmb.best/image/inline.jpg": SAMPLE_JPEG,
        }
    )
    exporter = MarkdownBuilder(client, image_mode="folder")
    path = exporter.save(sample_thread(), "all", tmp_path)

    folder = path.with_name(path.stem + ".files")
    assert folder.is_dir()
    names = sorted(item.name for item in folder.iterdir())
    assert names == ["abcdef.jpg", "inline.jpg"]
    # 写进去的就是原图字节（没有转码、没有加 base64 头）。
    assert (folder / "inline.jpg").read_bytes() == SAMPLE_JPEG

    text = path.read_text(encoding="utf-8")
    # 目录名可能是中文（跟着成品名走），正文里按 URL 规则转义，浏览器与阅读器都认。
    rel = urllib.parse.quote(folder.name, safe="")
    assert f"![图片]({rel}/inline.jpg)" in text
    assert f"![图片]({rel}/abcdef.jpg)" in text
    assert "https://image.nmb.best/image/inline.jpg" not in text


def test_markdown_folder_mode_keeps_the_link_when_download_fails(tmp_path: Path) -> None:
    failed = "https://image.nmb.best/image/inline.jpg"
    client = FakeClient(fail_urls={failed})
    messages: list[str] = []
    exporter = MarkdownBuilder(client, progress=messages.append, image_mode="folder")
    path = exporter.save(sample_thread(), "all", tmp_path)

    text = path.read_text(encoding="utf-8")

    # 下不到就退回外链，正文别因为一张图丢内容。
    assert f"![图片]({failed})" in text
    assert any("图片下载失败，已保留链接" in message for message in messages)


def test_markdown_folder_mode_summarises_many_failures(tmp_path: Path) -> None:
    from tests import make_post
    from xdao.exporters._shared import ThreadData

    posts = []
    for index in range(1, 13):
        posts.append(
            make_post(
                8000 + index,
                f'<img src="https://cdn.example/missing-{index}.jpg">',
                is_po=index == 1,
            )
        )
    thread = ThreadData(thread_id=8000, title="多图", po_hash="POCOOKIE", posts=posts)
    client = FakeClient(fail_urls={f"https://cdn.example/missing-{index}.jpg" for index in range(1, 13)})
    messages: list[str] = []
    exporter = MarkdownBuilder(client, progress=messages.append, image_mode="folder")
    exporter.save(thread, "all", tmp_path)

    assert any("一共 12 张图片没存进目录" in message for message in messages)
    assert any("逐条日志只列了前 10 张" in message for message in messages)


def test_create_exporter_hands_the_folder_mode_to_markdown() -> None:
    from xdao.exporters import create_exporter

    exporter = create_exporter("markdown", FakeClient(), image_mode="folder")

    assert isinstance(exporter, MarkdownBuilder)
    assert exporter.image_mode == "folder"


# ---------- 目录模式的图片并发预取（v0.21.20） ----------


def _thread_with_images(count: int) -> ThreadData:
    """一个主帖 + ``count`` 楼回复，每楼正文里各一张图。"""
    posts = [make_post(9000, "主帖只有文字", is_po=True)]
    for index in range(1, count + 1):
        posts.append(make_post(9000 + index, f'<img src="https://cdn.example/pic-{index}.jpg">'))
    return ThreadData(thread_id=9000, title="多图", po_hash="POCOOKIE", posts=posts)


def test_markdown_folder_mode_prefetches_images_with_progress(tmp_path: Path) -> None:
    """图多的串先把图并发下完再渲染，日志里有总数与百分比进度。"""
    client = FakeClient()
    messages: list[str] = []
    exporter = MarkdownBuilder(client, progress=messages.append, image_mode="folder")

    path = exporter.save(_thread_with_images(3), "all", tmp_path)

    assert "正在下载 3 张图片…" in messages
    assert "正在下载图片：3/3" in messages
    assert sorted(client.downloaded) == [
        "https://cdn.example/pic-1.jpg",
        "https://cdn.example/pic-2.jpg",
        "https://cdn.example/pic-3.jpg",
    ]
    folder = path.with_name(path.stem + ".files")
    assert len(list(folder.iterdir())) == 3


def test_markdown_folder_mode_downloads_a_repeated_image_once(tmp_path: Path) -> None:
    """同一张图在多个楼层出现：只下一次、只落一个文件（以前会写「同名-1」副本）。"""
    url = "https://cdn.example/same.jpg"
    posts = [
        make_post(9101, f'<img src="{url}">', is_po=True),
        make_post(9102, f'<img src="{url}">'),
    ]
    thread = ThreadData(thread_id=9101, title="重复图", po_hash="POCOOKIE", posts=posts)
    client = FakeClient()
    exporter = MarkdownBuilder(client, image_mode="folder")

    path = exporter.save(thread, "all", tmp_path)

    assert client.downloaded == [url]
    folder = path.with_name(path.stem + ".files")
    assert [item.name for item in folder.iterdir()] == ["same.jpg"]
    text = path.read_text(encoding="utf-8")
    rel = urllib.parse.quote(folder.name, safe="")
    assert text.count(f"![图片]({rel}/same.jpg)") == 2


def test_markdown_folder_mode_says_nothing_when_there_are_no_images(tmp_path: Path) -> None:
    """没有图片就不该冒出「正在下载 0 张」这种空话。"""
    thread = ThreadData(
        thread_id=9200,
        title="无图",
        po_hash="POCOOKIE",
        posts=[make_post(9200, "只有文字", is_po=True)],
    )
    client = FakeClient()
    messages: list[str] = []
    exporter = MarkdownBuilder(client, progress=messages.append, image_mode="folder")

    exporter.save(thread, "all", tmp_path)

    assert [message for message in messages if message.startswith("正在下载")] == []
    assert client.downloaded == []


def test_markdown_folder_mode_can_be_cancelled_before_rendering(tmp_path: Path) -> None:
    """停止位举着的时候：图片阶段就抛出去，不落盘、不留半份成品。"""
    stop = threading.Event()
    stop.set()
    exporter = MarkdownBuilder(FakeClient(), image_mode="folder", stop=stop)

    with pytest.raises(ExportCancelled):
        exporter.save(_thread_with_images(2), "all", tmp_path)

    assert list(tmp_path.glob("*.md")) == []


def test_markdown_folder_mode_checks_cancel_between_posts(tmp_path: Path) -> None:
    """图片下完才点取消：逐楼渲染那段也要认，不能当成没取消。"""
    stop = threading.Event()

    def progress(message: str) -> None:
        # 最后一批图片下完时立刻举停止位，真正的取消点落在渲染循环里。
        if message.startswith("正在下载图片：10/10"):
            stop.set()

    exporter = MarkdownBuilder(
        FakeClient(), progress=progress, image_mode="folder", stop=stop
    )

    with pytest.raises(ExportCancelled):
        exporter.save(_thread_with_images(10), "all", tmp_path)

    assert list(tmp_path.glob("*.md")) == []
