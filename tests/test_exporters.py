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
    PdfBuilder,
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

    实测过这个：目录用别的程序（甚至旧版本）都能写，只有本程序的探针文件
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

    实测过的场景：``D:\\X岛\\.cache`` 建得出来，``.cache\\pages`` 拒绝访问。
    只探表层会把这种目录判成"能写"，于是「缓存目录写不进去就换个地方」的兜底
    逻辑永远不触发，缓存文件每次都在同一个地方撞墙。
    """
    from xdao.exporters._shared import can_write_dir

    assert can_write_dir(out_dir) is True

    real_mkdir = Path.mkdir

    def fake_mkdir(self, *args, **kwargs):
        # 只让"探针子目录"这一级失败，模拟权限只放开到上一级的目录。
        # 两个探针名都要挡：只要有一个名字能写，v0.13.27 起就会判成"能写"
        # （名字被安全软件拦掉是另一回事，见下面的重试用例）。
        if ".d" in self.name and self.name.startswith("xdao-write-test."):
            raise PermissionError(13, "Permission denied", str(self))
        return real_mkdir(self, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", fake_mkdir)
    assert can_write_dir(out_dir) is False


def test_can_write_dir_leaves_no_litter(out_dir):
    """探测要自己收拾干净：不留探针文件，也不留探针目录。"""
    from xdao.exporters._shared import PROBE_NAME, can_write_dir

    assert can_write_dir(out_dir) is True
    assert list(out_dir.iterdir()) == []


# ---------- 探针名被单独挡掉时不能判「目录不可写」（v0.13.27）----------


def test_can_write_dir_retries_with_a_second_probe_name(out_dir, monkeypatch):
    """第一个探针名字写不动时，换个名字能写就算能写。

    2026-10-02 真机：用户的导出目录和 %LOCALAPPDATA% 兜底**一起**被判
    「写不进去」，可磁盘、权限、盘符都正常，导出最后也照常写完了 ——
    错的是探针（名字被安全软件挡），不是目录。
    """
    from xdao.exporters._shared import PROBE_ALT_NAME, PROBE_NAME, can_write_dir

    real_write_text = Path.write_text

    def fake_write_text(self, *args, **kwargs):
        if self.name.startswith(PROBE_NAME):
            raise PermissionError(13, "Permission denied")
        return real_write_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", fake_write_text)

    assert can_write_dir(out_dir) is True
    assert not (out_dir / PROBE_NAME).exists()
    assert not (out_dir / PROBE_ALT_NAME).exists()


def test_probe_writable_reports_the_real_reason(out_dir, monkeypatch):
    """探不通必须说得出原因 —— 真机上只有「写不进去」四个字是没法排查的。"""
    from xdao.exporters._shared import PROBE_ALT_NAME, PROBE_NAME, probe_writable

    real_write_text = Path.write_text

    def fake_write_text(self, *args, **kwargs):
        if self.name.startswith("xdao-write-test"):
            raise PermissionError(13, "Permission denied")
        return real_write_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", fake_write_text)

    ok, why = probe_writable(out_dir)
    assert ok is False
    assert "Permission denied" in why
    assert PROBE_NAME in why and PROBE_ALT_NAME in why


def test_probe_writable_answers_yes_without_a_reason(out_dir):
    """能写时不给原因，也不留垃圾。"""
    from xdao.exporters._shared import probe_writable

    assert probe_writable(out_dir) == (True, "")
    assert list(out_dir.iterdir()) == []


# ---------- 导出目录写不进去时换地方（v0.5.3）----------


def test_choose_writable_dir_keeps_the_requested_directory(tmp_path):
    """能写就原样返回，不多此一举换地方，也不产生说明。"""
    from xdao.exporters._shared import choose_writable_dir

    choice = choose_writable_dir(tmp_path)
    assert choice.path == tmp_path
    assert choice.notes == []
    assert choice.fallback is False


def test_choose_writable_dir_moves_to_localappdata_when_blocked(tmp_path, monkeypatch):
    """请求的目录写不进去时，自动改用 LOCALAPPDATA 下的位置。

    这就是实际遇到的场景：导出目录设在桌面下的新文件夹，
    抓取全部成功、写文件时 ``[Errno 13]``（受控文件夹访问只放行白名单程序），
    整趟白跑。
    """
    from xdao.exporters import _shared

    local = tmp_path / "LocalAppData"
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    monkeypatch.setenv("TEMP", str(tmp_path / "Temp"))
    blocked = tmp_path / "桌面" / "新建文件夹"

    choice = _shared.choose_writable_dir(blocked, probe=lambda path: path != blocked)

    assert choice.fallback is True
    assert choice.path.parent == local / "xdao-export"
    assert choice.path.name == "导出"
    text = "".join(choice.notes)
    assert str(blocked) in text and str(choice.path) in text
    assert "受控文件夹访问" in text


def test_choose_writable_dir_falls_back_to_temp_when_localappdata_blocked(tmp_path, monkeypatch):
    """LOCALAPPDATA 也不可用时，退到 %TEMP%。"""
    from xdao.exporters import _shared

    local = tmp_path / "LocalAppData"
    temp = tmp_path / "Temp"
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    monkeypatch.setenv("TEMP", str(temp))

    choice = _shared.choose_writable_dir(
        tmp_path / "blocked", probe=lambda path: str(path).startswith(str(temp))
    )

    assert choice.fallback is True
    assert choice.path == temp / "xdao-export"


def test_choose_writable_dir_never_overwrites_an_earlier_fallback(tmp_path, monkeypatch):
    """兜底目录已存在时换一个带序号的名字：两次导出的成品别互相覆盖。"""
    from xdao.exporters import _shared

    local = tmp_path / "LocalAppData"
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    first = local / "xdao-export" / "导出"
    first.mkdir(parents=True)

    choice = _shared.choose_writable_dir(
        tmp_path / "blocked", probe=lambda path: path != tmp_path / "blocked"
    )

    assert choice.path.name == "导出-2"


def test_choose_writable_dir_pinned_keeps_the_directory_but_explains(tmp_path):
    """显式指定的目录不换地方（allow_fallback=False），但要说清为什么不能用。"""
    from xdao.exporters._shared import choose_writable_dir

    blocked = tmp_path / "被拦的目录"
    choice = choose_writable_dir(blocked, allow_fallback=False, probe=lambda path: False)

    assert choice.path == blocked
    assert choice.fallback is False
    assert "写不进去" in "".join(choice.notes)


def test_choose_writable_dir_reports_when_everything_fails(tmp_path, monkeypatch):
    """连兜底位置都写不进去时，如实报错而不是假装换了地方。"""
    from xdao.exporters import _shared

    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "LocalAppData"))
    monkeypatch.setenv("TEMP", str(tmp_path / "Temp"))
    blocked = tmp_path / "blocked"

    choice = _shared.choose_writable_dir(blocked, probe=lambda path: False)

    assert choice.path == blocked
    assert choice.fallback is False
    assert "也不行" in "".join(choice.notes)
    # 探不通也**不拦下导出**：措辞必须说清"这一趟照样写你选的目录"。
    assert "仍然写在你选的目录里" in "".join(choice.notes)


def test_choose_writable_dir_puts_the_real_reason_in_the_note(tmp_path, monkeypatch):
    """用真探针时，说明里要带上系统给的原话 —— 否则真机上没法查。

    这一条复现的正是 2026-10-02 的现场：两个位置全被判不可写，而原因
    （``Permission denied``）以前被吞掉了，日志里只剩一句「请检查磁盘」。
    """
    from xdao.exporters import _shared

    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "LocalAppData"))
    monkeypatch.setenv("TEMP", str(tmp_path / "Temp"))
    real_write_text = Path.write_text

    def fake_write_text(self, *args, **kwargs):
        if self.name.startswith("xdao-write-test"):
            raise PermissionError(13, "Permission denied")
        return real_write_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", fake_write_text)

    choice = _shared.choose_writable_dir(tmp_path / "导出")

    assert choice.path == tmp_path / "导出"
    assert choice.fallback is False
    text = "".join(choice.notes)
    assert "Permission denied" in text
    assert "连备用位置" in text


def test_fallback_dirs_stay_inside_appdata_and_temp(tmp_path, monkeypatch):
    """兜底候选必须落在 %LOCALAPPDATA% / %TEMP% 里。

    受控文件夹访问保护的是桌面/文档/图片/视频，只有这两个位置是明确留给
    程序写数据的 —— 兜底候选跑到别处就等于没兜底。
    """
    from xdao.exporters._shared import fallback_dirs

    local = tmp_path / "LocalAppData"
    temp = tmp_path / "Temp"
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    monkeypatch.setenv("TEMP", str(temp))

    dirs = fallback_dirs()
    assert dirs[0].parent == local / "xdao-export"
    assert dirs[1] == temp / "xdao-export"


def test_choose_writable_dir_creates_nothing_while_probing(tmp_path):
    """探测（真身 can_write_dir）不会在磁盘上留下文件。

    目录本身会被建出来（不然没法探下一层），但探针文件与探针子目录必须清干净。
    """
    from xdao.exporters._shared import choose_writable_dir

    target = tmp_path / "新目录"
    choice = choose_writable_dir(target)
    assert choice.path == target
    assert list(target.iterdir()) == []


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


def test_create_exporter_keeps_the_pdf_only_arguments():
    """PDF 专属的那几项要能穿过「按能力逐级回退」的链条，落到导出器身上。

    这条链条的毛病很隐蔽：`PdfBuilder` 不收 `image_mode`，所以第一层只要带着
    `image_mode` 就整体 `TypeError`，一定会掉到第二层 —— 而第二层原来没带
    `browser_path` / `pdf_timeout` / `fallback_html`，于是**设置里指定的浏览器、超时、
    「渲染失败就改存 HTML」这三项对 PDF 导出全都不起作用**（2026-10-02 发现；
    `tests/test_watcher.py` 的监控 PDF 用例只钉了「传给 create_exporter 的参数」，
    所以一直没红）。
    """
    from xdao.pdf_opts import PdfOptions

    options = PdfOptions(paper="a3", margin="none")
    exporter = create_exporter(
        "pdf",
        FakeClient(),
        filename_template="[{id}] {title}",
        image_mode="embed",
        browser_path="C:/假的浏览器.exe",
        pdf_timeout=123,
        fallback_html=False,
        pdf_options=options,
    )

    assert isinstance(exporter, PdfBuilder)
    assert exporter.browser_path == "C:/假的浏览器.exe"
    assert exporter.timeout == 123
    assert exporter.fallback_html is False
    assert exporter.pdf_options is options
    assert exporter.filename_template == "[{id}] {title}"


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


# ---------- 导出器基类（2026-10-07 架构评审） ----------


def test_every_exporter_takes_the_same_construction_signature():
    """五个导出器的构造签名统一成 ``(client, progress, filename_template, **options)``。

    签名不一致正是 ``create_exporter`` 当初需要「带全部参数 → TypeError 就删一项再试」
    那条五层阶梯的根因（阶梯丢过 browser_path / pdf_timeout / fallback_html /
    pdf_options）。这条用例把「不认识的参数不炸构造、留在 options 里」钉住 —— 有了它，
    阶梯才可以删。
    """
    from xdao.exporters import EpubBuilder, MarkdownBuilder

    client = FakeClient()
    builders = (HtmlBuilder, TxtBuilder, MarkdownBuilder, EpubBuilder, PdfBuilder)
    for cls in builders:
        exporter = cls(client, progress=None, filename_template=None, 没听说过的参数=1)
        assert exporter.options["没听说过的参数"] == 1


def test_registry_display_and_suffix_come_from_the_class():
    """注册表不再抄一份显示名与扩展名：它与类属性必须逐字一致。

    抄一份的后果是「注册表说 .md、save 里写 .md」这种两份真源可以各自走样
    （2026-10-07 架构评审）。
    """
    for key, (label, suffix, cls) in EXPORTERS.items():
        assert (cls.key, cls.display, cls.suffix) == (key, label, suffix)


def test_save_uses_the_class_suffix(out_dir):
    """扩展名只有一份真源：类属性 ``suffix``（save 不再自己拼 .html/.md/…）。"""
    from xdao.exporters import MarkdownBuilder

    path = MarkdownBuilder(FakeClient()).save(sample_thread(), "all", out_dir)
    assert path.suffix == MarkdownBuilder.suffix
    assert path.exists()


def test_base_class_covers_every_registered_exporter():
    """每个注册在册的导出器都真的继承基类（而不是各自再写一份 save）。"""
    from xdao.exporters import Exporter

    for _, (_, _, cls) in EXPORTERS.items():
        assert issubclass(cls, Exporter)
        assert cls is not Exporter
