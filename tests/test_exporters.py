"""HTML / TXT 导出器与公共文本处理的测试（离线）。

EPUB 与 Markdown 的测试见 ``test_epub.py`` / ``test_markdown.py``。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests import FakeClient, SAMPLE_JPEG, make_post, sample_thread, sample_thread_with_po_reply
from xdao.client import Post
from xdao.exporters import (
    EXPORTERS,
    HtmlBuilder,
    OutputDirNotWritable,
    ThreadData,
    TxtBuilder,
    create_exporter,
    derive_filename,
    ensure_writable,
    guess_mime,
    iter_post_image_urls,
    output_path,
    plain_text,
    render_filename,
    render_inline_content,
    resolve_image_url,
    sanitize_filename,
)

ARTIFACTS = Path(__file__).resolve().parent.parent / ".test-artifacts"


@pytest.fixture
def out_dir() -> Path:
    import shutil

    path = ARTIFACTS / "exporters"
    shutil.rmtree(path, ignore_errors=True)
    path.mkdir(parents=True, exist_ok=True)
    return path


# ---------- 文本处理 ----------


def test_plain_text_strips_tags_and_keeps_line_breaks():
    assert plain_text("第一行<br>第二行") == "第一行\n第二行"
    assert plain_text("<b>粗</b>体") == "粗体"
    assert plain_text("a &amp; b &lt;c&gt;") == "a & b <c>"
    assert plain_text("  ") == ""
    assert plain_text("") == ""


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("正常标题", "正常标题"),
        ('标题/带:非法*字符?"<>|', "标题_带_非法_字符_____"),
        ("  前后空格  ", "前后空格"),
        ("结尾有点...", "结尾有点"),
        ("", "xdao-thread"),
        ("///", "___"),
    ],
)
def test_sanitize_filename(raw, expected):
    assert sanitize_filename(raw) == expected


def test_sanitize_filename_truncates_long_names():
    assert len(sanitize_filename("标" * 200)) == 80


def test_derive_filename_prefers_title():
    assert derive_filename(sample_thread("我的标题")) == "我的标题"


def test_derive_filename_falls_back_to_first_sentence():
    thread = sample_thread("无标题")
    thread.posts[0].content = "这是第一句话。第二句话在这里。<br>第三句"
    assert derive_filename(thread) == "这是第一句话"


def test_derive_filename_handles_half_width_punctuation():
    thread = sample_thread("无标题")
    thread.posts[0].content = "First sentence! Second one?"
    assert derive_filename(thread) == "First sentence"


def test_derive_filename_falls_back_to_thread_id():
    thread = ThreadData(thread_id=4242, title="无标题", po_hash="", posts=[])
    assert derive_filename(thread) == "串4242"


# ---------- 文件名模板 ----------


def test_render_filename_without_template_uses_fallback():
    thread = sample_thread("标题")
    assert render_filename(None, thread, "标题") == "标题"
    assert render_filename("", thread, "标题") == "标题"


def test_render_filename_fills_placeholders():
    thread = sample_thread("我的标题")
    thread.thread_id = 7001
    rendered = render_filename("[{id}] {title}（{count}楼）", thread, "我的标题")
    assert rendered == "[7001] 我的标题（2楼）"


def test_render_filename_bad_template_falls_back():
    thread = sample_thread("标题")
    assert render_filename("{不存在的占位符}", thread, "标题") == "标题"
    assert render_filename("{title", thread, "标题") == "标题"


def test_render_filename_sanitizes_result():
    thread = sample_thread("标/题")
    assert render_filename("{title}", thread, "标/题") == "标_题"


def test_output_path_helper():
    assert output_path(Path("D:/out"), "名/称", ".html") == Path("D:/out") / "名_称.html"


# ---------- 图片地址与 MIME ----------


def test_resolve_image_url():
    client = FakeClient()
    assert resolve_image_url(client, "/image/a.jpg") == "https://image.nmb.best/image/a.jpg"
    assert resolve_image_url(client, "image/a.jpg") == "https://image.nmb.best/image/a.jpg"
    assert resolve_image_url(client, "https://x/b.png") == "https://x/b.png"
    assert resolve_image_url(client, "data:image/png;base64,AAA") == "data:image/png;base64,AAA"
    assert resolve_image_url(client, "") == ""


def test_guess_mime_by_magic_number():
    assert guess_mime("whatever", SAMPLE_JPEG) == "image/jpeg"
    assert guess_mime("whatever", b"\x89PNG\r\n\x1a\n" + b"x") == "image/png"
    assert guess_mime("whatever", b"GIF89a" + b"x") == "image/gif"
    assert guess_mime("whatever", b"RIFF\x00\x00\x00\x00WEBP") == "image/webp"


def test_guess_mime_by_extension_fallback():
    assert guess_mime("https://x/a.PNG") == "image/png"
    assert guess_mime("https://x/a.jpeg") == "image/jpeg"
    assert guess_mime("https://x/a") == "image/jpeg"


def test_iter_post_image_urls_collects_both_sources():
    client = FakeClient()
    post = make_post(
        1, '正文<img src="/image/inline.png">再来一张<img src="https://other/x.gif">',
        img="att", ext=".jpg",
    )
    urls = iter_post_image_urls(client, post)
    assert urls == [
        "https://image.nmb.best/image/att.jpg",
        "https://image.nmb.best/image/inline.png",
        "https://other/x.gif",
    ]


# ---------- 内联内容渲染 ----------


def test_render_inline_content_url_mode():
    client = FakeClient()
    text = render_inline_content(client, "a<br>b<img src=\"/image/x.jpg\">")
    assert "![图片](https://image.nmb.best/image/x.jpg)" in text
    assert "a\nb" in text


def test_render_inline_content_drop_mode():
    client = FakeClient()
    assert "图片" not in render_inline_content(client, "<img src=\"/image/x.jpg\">", "drop")


def test_render_inline_content_embed_mode_uses_embedder():
    client = FakeClient()
    text = render_inline_content(
        client, "<img src=\"/image/x.jpg\">", "embed", embedder=lambda url: "data:image/jpeg;base64,AAA"
    )
    assert "![图片](data:image/jpeg;base64,AAA)" in text


def test_render_inline_content_embed_falls_back_to_url():
    client = FakeClient()
    text = render_inline_content(
        client, "<img src=\"/image/x.jpg\">", "embed", embedder=lambda url: None
    )
    assert "https://image.nmb.best/image/x.jpg" in text


# ---------- 楼层筛选 ----------


def test_filter_posts_scope_all_and_po():
    thread = sample_thread_with_po_reply()
    assert len(thread.filter_posts("all")) == 3
    po_ids = [p.id for p in thread.filter_posts("po")]
    assert po_ids == [1001, 1003]  # 主帖 + 同饼干回复
    assert thread.reply_count == 2


def test_filter_posts_by_hashes_ignores_scope():
    thread = sample_thread_with_po_reply()
    # 指定饼干时以筛选为准，"只抓 PO"不再生效
    assert [p.id for p in thread.filter_posts("po", ["other999"])] == [1002]


def test_filter_posts_accepts_multiple_hashes_and_dedupes():
    thread = sample_thread_with_po_reply()
    assert [p.id for p in thread.filter_posts("all", ["other999", "POCOOKIE"])] == [
        1001,
        1002,
        1003,
    ]
    assert [p.id for p in thread.filter_posts("all", ["  ", ""])] == [1001, 1002, 1003]


def test_is_po_post():
    thread = sample_thread_with_po_reply()
    assert thread.is_po_post(thread.posts[0]) is True
    assert thread.is_po_post(thread.posts[1]) is False
    assert thread.is_po_post(thread.posts[2]) is True


# ---------- HTML 导出器 ----------


def test_html_build_contains_meta_and_escapes_body():
    client = FakeClient()
    thread = sample_thread("我的标题")
    thread.posts[0].content = "危险内容 <script>alert(1)</script>"
    html = HtmlBuilder(client).build(thread, "all")

    assert "<title>我的标题</title>" in html
    # 标签不能以可执行形式出现，但作为文本必须保留下来（存档不能丢内容）
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html
    assert "&lt;/script&gt;" in html
    assert "危险内容" in html
    assert "badge-po" in html
    assert "badge-admin" in html
    assert "饼干 POCOOKIE" in html
    assert "No.1002" in html


def test_html_keeps_unknown_tags_as_visible_text():
    client = FakeClient()
    thread = sample_thread("标题")
    thread.posts[0].content = "代码示例：<div class=\"x\">内容</div>"
    html = HtmlBuilder(client).build(thread, "all")
    assert "&lt;div" in html
    assert "代码示例" in html
    assert "内容" in html


def test_html_embeds_images_as_data_uri():
    client = FakeClient()
    html = HtmlBuilder(client).build(sample_thread(), "all")
    assert "data:image/jpeg;base64," in html


def test_html_falls_back_to_link_when_image_fails():
    client = FakeClient(fail_urls={"https://image.nmb.best/image/abcdef.jpg"})
    html = HtmlBuilder(client).build(sample_thread(), "all")
    # 附件图下载失败 → 保留原始链接；正文里那张仍能内嵌
    assert "https://image.nmb.best/image/abcdef.jpg" in html
    assert "data:image/jpeg;base64," in html


def test_html_downloads_each_image_once():
    client = FakeClient()
    thread = sample_thread()
    thread.posts[1].img = "abcdef"  # 与主帖同一张图
    thread.posts[1].ext = ".jpg"
    HtmlBuilder(client).build(thread, "all")
    assert client.downloaded.count("https://image.nmb.best/image/abcdef.jpg") == 1


def test_html_scope_po_excludes_others():
    client = FakeClient()
    html = HtmlBuilder(client).build(sample_thread(), "po")
    assert "回复正文" not in html


def test_html_include_hashes_filters_posts():
    client = FakeClient()
    html = HtmlBuilder(client).build(sample_thread(), "all", include_hashes=["other999"])
    assert "回复正文" in html
    assert "主帖正文" not in html
    assert "指定饼干" in html  # 抬头会写明筛选范围


def test_html_shows_scope_label():
    client = FakeClient()
    assert "全部发言" in HtmlBuilder(client).build(sample_thread(), "all")
    assert "仅 PO 发言" in HtmlBuilder(client).build(sample_thread(), "po")


def test_html_save_uses_template_and_sanitizes(out_dir):
    client = FakeClient()
    path = HtmlBuilder(client, filename_template="[{id}] {title}").save(
        sample_thread("标/题"), "all", out_dir
    )
    assert path.name == "[1001] 标_题.html"
    assert path.read_text(encoding="utf-8").startswith("<!DOCTYPE html>")


def test_html_progress_receives_messages(out_dir):
    messages: list[str] = []
    HtmlBuilder(FakeClient(), progress=messages.append).save(sample_thread(), "all", out_dir)
    assert any("图片" in m for m in messages)
    assert any("保存" in m for m in messages)


# ---------- TXT 导出器 ----------


def test_txt_build_has_header_and_posts():
    client = FakeClient()
    text = TxtBuilder(client).build(sample_thread("我的标题"), "all")

    assert "串号 No.1001" in text
    assert "标题：我的标题" in text
    assert "PO 饼干：POCOOKIE" in text
    assert "导出范围：全部发言" in text
    assert "楼层数：2" in text
    assert "[PO]" in text
    assert "红名" in text
    assert "No.1002" in text
    assert "[图片] https://image.nmb.best/image/abcdef.jpg" in text


def test_txt_build_scope_and_hashes():
    client = FakeClient()
    assert "回复正文" not in TxtBuilder(client).build(sample_thread(), "po")

    text = TxtBuilder(client).build(sample_thread(), "all", include_hashes=["other999"])
    assert "回复正文" in text
    assert "主帖正文" not in text
    assert "指定饼干：other999" in text


def test_txt_strips_html_from_content():
    client = FakeClient()
    text = TxtBuilder(client).build(sample_thread(), "all")
    assert "<br>" not in text
    assert "<img" not in text


def test_txt_save_uses_template(out_dir):
    client = FakeClient()
    path = TxtBuilder(client, filename_template="串{id}").save(sample_thread("我的标题"), "all", out_dir)
    assert path.name == "串1001.txt"


def test_txt_save_creates_output_dir(out_dir):
    target = out_dir / "新的子目录"
    path = TxtBuilder(FakeClient()).save(sample_thread(), "all", target)
    assert path.parent == target and path.exists()


# ---------- 导出目录可写性预检 ----------


def test_ensure_writable_creates_missing_directory(out_dir):
    target = out_dir / "还没创建" / "更深一层"
    assert ensure_writable(target) == target
    assert target.is_dir()


def test_ensure_writable_tolerates_a_failed_probe(out_dir, monkeypatch):
    """探针写不动**不等于**目录不可写，导出不能被它拦下。

    用户真报过这个：目录用别的程序（甚至旧版本）都能写，只有本程序的探针文件
    被拦，于是程序直接弹「导出目录不可写」拒绝开工。
    """
    from pathlib import Path as _Path

    from xdao.exporters._shared import PROBE_NAME

    real_write_text = _Path.write_text

    def fake_write_text(self, *args, **kwargs):
        if self.name == PROBE_NAME:
            raise PermissionError(13, "Permission denied")
        return real_write_text(self, *args, **kwargs)

    monkeypatch.setattr(_Path, "write_text", fake_write_text)
    assert ensure_writable(out_dir) == out_dir
    # 探针没留下垃圾
    assert not (out_dir / PROBE_NAME).exists()


def test_probe_file_looks_like_a_normal_export(out_dir):
    """探针必须是普通文件名，不能是隐藏的点文件。

    2026-09-30 的教训：探针叫 ``.xdao-write-probe`` 时被安全软件单独挡掉，
    探出来的结论跟真实导出物毫无关系，于是误报"目录不可写"。
    """
    from xdao.exporters._shared import PROBE_NAME

    assert not PROBE_NAME.startswith(".")
    assert PROBE_NAME.endswith((".tmp", ".txt", ".log"))


def test_can_write_dir_probes_a_subdirectory(out_dir, monkeypatch):
    """能不能写要**往下一层**探：这一级能写、下一级不能写，算不能写。

    用户报过的真实场景：``D:\\X岛\\.cache`` 建得出来，``.cache\\pages`` 拒绝访问。
    只探表层会把这种目录判成"能写"，于是「缓存目录写不进去就换个地方」的兜底
    逻辑永远不触发，缓存文件每次都在同一个地方撞墙。
    """
    from xdao.exporters._shared import can_write_dir

    assert can_write_dir(out_dir) is True

    real_mkdir = Path.mkdir

    def fake_mkdir(self, *args, **kwargs):
        # 只让"探针子目录"这一级失败，模拟权限只放开到上一级的目录
        if "xdao-write-test.tmp.d" in self.name:
            raise PermissionError(13, "Permission denied", str(self))
        return real_mkdir(self, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", fake_mkdir)
    assert can_write_dir(out_dir) is False


def test_can_write_dir_leaves_no_litter(out_dir):
    """探测要自己收拾干净：不留探针文件，也不留探针目录。"""
    from xdao.exporters._shared import PROBE_NAME, can_write_dir

    assert can_write_dir(out_dir) is True
    assert list(out_dir.iterdir()) == []


@pytest.mark.parametrize("key", ["html", "txt", "markdown", "epub"])
def test_save_creates_missing_directory_and_writes(out_dir, key):
    """目录不存在时要自己建出来（预检失败不再是拦路虎）。"""
    from xdao.exporters._shared import PROBE_NAME

    target = out_dir / "临时子目录"
    exporter = create_exporter(key, FakeClient())
    path = exporter.save(sample_thread(), "all", target)
    assert path.exists() and path.stat().st_size > 0
    assert ensure_writable(target) == target
    assert not (target / PROBE_NAME).exists()


def test_txt_save_without_template_uses_title(out_dir):
    path = TxtBuilder(FakeClient()).save(sample_thread("我的标题"), "all", out_dir)
    assert path.name == "我的标题.txt"


# ---------- 导出器注册表 ----------


def test_registry_lists_all_formats():
    assert set(EXPORTERS) == {"html", "pdf", "txt", "markdown", "epub"}
    for key, (label, suffix, cls) in EXPORTERS.items():
        assert label and suffix.startswith(".") and isinstance(cls, type)
    # 格式键与扩展名要一一对应，界面与命令行都靠它推导文件名
    assert EXPORTERS["pdf"][1] == ".pdf"


def test_create_exporter_returns_right_class_and_falls_back():
    client = FakeClient()
    assert isinstance(create_exporter("txt", client), TxtBuilder)
    assert isinstance(create_exporter("html", client), HtmlBuilder)
    assert isinstance(create_exporter("不存在的格式", client), HtmlBuilder)


@pytest.mark.parametrize("key", ["html", "txt", "markdown", "epub"])
def test_create_exporter_accepts_template_and_saves(out_dir, key):
    """四种格式都必须支持文件名模板，并且真的落盘。

    PDF 单独测：它需要本机浏览器，不适合放进这个参数化列表。
    """
    client = FakeClient()
    exporter = create_exporter(key, client, filename_template="[{id}] {title}")
    thread = sample_thread("模板测试")
    path = exporter.save(thread, "all", out_dir)

    assert re.match(r"\[1001\] 模板测试\.(html|txt|md|epub)$", path.name), path.name
    assert path.stat().st_size > 0


@pytest.mark.parametrize("key", ["html", "txt", "markdown", "epub"])
def test_create_exporter_accepts_include_hashes(out_dir, key):
    client = FakeClient()
    exporter = create_exporter(key, client)
    thread = sample_thread_with_po_reply()
    path = exporter.save(thread, "all", out_dir, include_hashes=["other999"])

    assert path.exists()
    if key in ("txt", "markdown"):
        text = path.read_text(encoding="utf-8")
        assert "回复正文" in text
        assert "主帖正文" not in text
