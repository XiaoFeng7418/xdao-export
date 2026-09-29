"""EPUB 导出器的测试：离线生成后重新读回校验结构。"""

from __future__ import annotations

import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import pytest

from tests import FakeClient, SAMPLE_JPEG, SAMPLE_PNG, data_uri_bytes, make_post, sample_thread
from xdao.exporters._shared import ThreadData
from xdao.exporters.epub import EpubBuilder

OPF_NS = "http://www.idpf.org/2007/opf"
DC_NS = "http://purl.org/dc/elements/1.1/"
XHTML_NS = "http://www.w3.org/1999/xhtml"
CONTAINER_NS = "urn:oasis:names:tc:opendocument:xmlns:container"


def build_epub(artifacts_dir: Path, thread: ThreadData | None = None, **kwargs) -> Path:
    """生成一个 epub 并返回路径。

    产物写进 ``artifacts_dir``（工作区内已存在的目录，沙箱下可写）。
    """
    thread = thread or sample_thread()
    scope = kwargs.pop("scope", "all")
    return EpubBuilder(kwargs.pop("client", FakeClient()), progress=kwargs.pop("progress", None)).build(
        thread, scope, artifacts_dir / "out.epub", **kwargs
    )


def picture_thread() -> ThreadData:
    """2 楼、1 张正文假图片 + 1 张附件假图片。"""
    main = make_post(
        2001,
        '正文第一行<br>第二行<img src="/image/inline.jpg">',
        user_hash="POCOOKIE",
        img="attach",
        ext=".jpg",
        is_po=True,
    )
    reply = make_post(2002, "回复一", user_hash="other999", name="路人乙", admin=1)
    return ThreadData(thread_id=2001, title="图片串", po_hash="POCOOKIE", posts=[main, reply])


def escaped_thread() -> ThreadData:
    """正文含中文与 < & 等字符，用来验证 XHTML 转义。"""
    main = make_post(
        3001,
        "5 &lt; 6 &amp; 7 &gt; 4 是数学<br>还有「引号」与 <b>标签</b>",
        user_hash="POCOOKIE",
        is_po=True,
    )
    return ThreadData(thread_id=3001, title="转义串", po_hash="POCOOKIE", posts=[main])


# ---------- 文件与第一个条目 ----------


def test_build_writes_epub_file(artifacts_dir: Path):
    path = build_epub(artifacts_dir)
    assert path == artifacts_dir / "out.epub"
    assert path.exists()
    assert zipfile.is_zipfile(path)


def test_mimetype_is_first_entry_and_stored(artifacts_dir: Path):
    path = build_epub(artifacts_dir)
    with zipfile.ZipFile(path) as archive:
        infos = archive.infolist()
        assert infos[0].filename == "mimetype"
        assert infos[0].compress_type == zipfile.ZIP_STORED
        data = archive.read("mimetype")
        assert data == b"application/epub+zip"
        assert b"\n" not in data
        assert not data.startswith(b"\xef\xbb\xbf")


def test_required_entries_exist(artifacts_dir: Path):
    with zipfile.ZipFile(build_epub(artifacts_dir)) as archive:
        names = archive.namelist()
    for required in (
        "mimetype",
        "META-INF/container.xml",
        "OEBPS/content.opf",
        "OEBPS/toc.ncx",
        "OEBPS/nav.xhtml",
        "OEBPS/text/post-0001.xhtml",
        "OEBPS/text/post-0002.xhtml",
    ):
        assert required in names, f"缺少条目 {required}"


# ---------- container / opf ----------


def test_container_xml_points_to_opf(artifacts_dir: Path):
    with zipfile.ZipFile(build_epub(artifacts_dir)) as archive:
        root = ET.fromstring(archive.read("META-INF/container.xml"))
    rootfiles = root.find(f"{{{CONTAINER_NS}}}rootfiles")
    assert rootfiles is not None
    rootfile = list(rootfiles)[0]
    assert rootfile.get("full-path") == "OEBPS/content.opf"
    assert rootfile.get("media-type") == "application/oebps-package+xml"


def test_opf_metadata_and_manifest(artifacts_dir: Path):
    with zipfile.ZipFile(build_epub(artifacts_dir, picture_thread())) as archive:
        names = set(archive.namelist())
        raw = archive.read("OEBPS/content.opf")
        root = ET.fromstring(raw)

    assert root.tag == f"{{{OPF_NS}}}package"
    assert root.get("version") == "3.0"
    assert root.get("unique-identifier") == "bookid"

    metadata = root.find(f"{{{OPF_NS}}}metadata")
    assert metadata is not None
    identifier = metadata.find(f"{{{DC_NS}}}identifier")
    assert identifier is not None and identifier.text.startswith("urn:uuid:")
    assert identifier.get("id") == "bookid"
    assert metadata.find(f"{{{DC_NS}}}title").text == "图片串"
    assert metadata.find(f"{{{DC_NS}}}language").text == "zh-CN"
    assert metadata.find(f"{{{DC_NS}}}creator").text == "POCOOKIE"
    modified = metadata.find(f"{{{OPF_NS}}}meta[@property='dcterms:modified']")
    assert modified is not None
    assert len(modified.text) == 20 and modified.text.endswith("Z") and "T" in modified.text

    # manifest 里每一项都必须真实存在。
    manifest = root.find(f"{{{OPF_NS}}}manifest")
    items = list(manifest)
    assert items
    hrefs = [item.get("href") for item in items]
    assert "nav.xhtml" in hrefs
    assert "toc.ncx" in hrefs
    for item in items:
        href = "OEBPS/" + item.get("href")
        assert item.get("media-type")
        assert href in names, f"manifest 指向了不存在的文件：{item.get('href')}"

    # spine 按顺序引用全部楼层。
    spine = root.find(f"{{{OPF_NS}}}spine")
    assert spine is not None
    idrefs = [item.get("idref") for item in spine]
    assert "post-0001" in idrefs and "post-0002" in idrefs
    assert idrefs.index("post-0001") < idrefs.index("post-0002")
    assert spine.get("toc") == "ncx"


def test_nav_and_ncx_parse_and_have_one_entry_per_post(artifacts_dir: Path):
    with zipfile.ZipFile(build_epub(artifacts_dir, picture_thread())) as archive:
        nav = ET.fromstring(archive.read("OEBPS/nav.xhtml"))
        ncx = ET.fromstring(archive.read("OEBPS/toc.ncx"))

    assert nav.tag == f"{{{XHTML_NS}}}html"
    links = [
        element
        for element in nav.iter(f"{{{XHTML_NS}}}a")
    ]
    assert len(links) == 2
    assert links[0].get("href") == "text/post-0001.xhtml"

    points = ncx.findall(".//{http://www.daisy.org/z3986/2005/ncx/}navPoint")
    assert len(points) == 2
    assert points[0].get("playOrder") == "1"


# ---------- 楼层 XHTML ----------


def test_all_post_pages_are_well_formed_xhtml(artifacts_dir: Path):
    with zipfile.ZipFile(build_epub(artifacts_dir, picture_thread())) as archive:
        pages = [name for name in archive.namelist() if name.startswith("OEBPS/text/")]
        assert len(pages) == 2
        for name in pages:
            raw = archive.read(name)
            assert raw.startswith(b'<?xml version="1.0" encoding="utf-8"?>')
            root = ET.fromstring(raw)  # 不合法会直接抛异常
            assert root.tag == f"{{{XHTML_NS}}}html"
            assert root.get("{http://www.w3.org/XML/1998/namespace}lang") == "zh-CN"
            text = "".join(root.itertext())
            assert "No." in text


def test_post_page_contains_meta_and_body(artifacts_dir: Path):
    with zipfile.ZipFile(build_epub(artifacts_dir, picture_thread())) as archive:
        raw = archive.read("OEBPS/text/post-0001.xhtml").decode("utf-8")
        reply = archive.read("OEBPS/text/post-0002.xhtml").decode("utf-8")
    assert "饼干 POCOOKIE" in raw
    assert "No.2001" in raw
    assert "[PO]" in raw
    assert "正文第一行" in raw and "第二行" in raw
    assert "红名" not in raw
    # 非 PO 楼层没有 PO 标记，但有红名标记。
    assert "[PO]" not in reply
    assert "红名" in reply
    assert "No.2002" in reply


def test_special_characters_stay_valid_and_readable(artifacts_dir: Path):
    with zipfile.ZipFile(build_epub(artifacts_dir, escaped_thread())) as archive:
        root = ET.fromstring(archive.read("OEBPS/text/post-0001.xhtml"))
    text = "".join(root.itertext())
    assert "5 < 6 & 7 > 4 是数学" in text
    assert "「引号」" in text
    # <b> 标签只保留文字内容。
    assert "标签" in text


# ---------- 图片 ----------


def test_embed_mode_inlines_images_and_writes_files(artifacts_dir: Path):
    client = FakeClient(
        images={
            "https://image.nmb.best/image/attach.jpg": SAMPLE_JPEG,
            "https://image.nmb.best/image/inline.jpg": SAMPLE_PNG,
        }
    )
    path = build_epub(artifacts_dir, picture_thread(), client=client, image_mode="embed")
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        page = ET.fromstring(archive.read("OEBPS/text/post-0001.xhtml"))
        srcs = [element.get("src") for element in page.iter(f"{{{XHTML_NS}}}img")]
        # 正文内图片在前，附件图在后。
        assert len(srcs) == 2
        assert all(src.startswith("data:image/") for src in srcs)
        assert srcs[0].startswith("data:image/png;base64,")
        assert srcs[1].startswith("data:image/jpeg;base64,")
        # data URI 里必须是真实图片字节。
        assert data_uri_bytes(srcs[0]) == SAMPLE_PNG
        assert data_uri_bytes(srcs[1]) == SAMPLE_JPEG

        stored = [name for name in names if name.startswith("OEBPS/images/")]
        assert len(stored) == 2
        blobs = {archive.read(name) for name in stored}
        assert blobs == {SAMPLE_JPEG, SAMPLE_PNG}
        # 扩展名必须跟 MIME 对得上：PNG 数据不能存成 .jpg。
        for name in stored:
            data = archive.read(name)
            suffix = Path(name).suffix
            assert suffix == (".png" if data == SAMPLE_PNG else ".jpg"), name


def test_url_mode_keeps_absolute_urls(artifacts_dir: Path):
    path = build_epub(artifacts_dir, picture_thread(), image_mode="url")
    with zipfile.ZipFile(path) as archive:
        page = ET.fromstring(archive.read("OEBPS/text/post-0001.xhtml"))
        names = archive.namelist()
    srcs = [element.get("src") for element in page.iter(f"{{{XHTML_NS}}}img")]
    assert "https://image.nmb.best/image/attach.jpg" in srcs
    assert "https://image.nmb.best/image/inline.jpg" in srcs
    assert not [name for name in names if name.startswith("OEBPS/images/")]


def test_image_download_failure_is_silently_skipped(artifacts_dir: Path):
    failed = "https://image.nmb.best/image/attach.jpg"
    client = FakeClient(fail_urls={failed})
    path = build_epub(artifacts_dir, picture_thread(), client=client, image_mode="embed")
    with zipfile.ZipFile(path) as archive:
        assert archive.read("OEBPS/text/post-0001.xhtml")  # 页面照样生成
    assert path.exists()


def test_progress_messages_are_reported(artifacts_dir: Path):
    messages: list[str] = []
    build_epub(artifacts_dir, picture_thread(), progress=messages.append, image_mode="embed")
    assert any("下载图片" in message for message in messages)
    assert any("EPUB" in message for message in messages)


def test_progress_none_does_not_break(artifacts_dir: Path):
    assert build_epub(artifacts_dir).exists()


# ---------- 范围与覆盖写入 ----------


def test_po_scope_keeps_main_post(artifacts_dir: Path):
    thread = picture_thread()
    thread.posts.append(make_post(2003, "PO 的补充", user_hash="POCOOKIE"))
    path = build_epub(artifacts_dir, thread, scope="po")
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        assert "OEBPS/text/post-0001.xhtml" in names
        assert "OEBPS/text/post-0002.xhtml" in names
        assert "OEBPS/text/post-0003.xhtml" not in names
        first = archive.read("OEBPS/text/post-0001.xhtml").decode("utf-8")
        assert "饼干 POCOOKIE" in first


def test_all_scope_exports_every_post(artifacts_dir: Path):
    with zipfile.ZipFile(build_epub(artifacts_dir, picture_thread(), scope="all")) as archive:
        pages = [name for name in archive.namelist() if name.startswith("OEBPS/text/")]
    assert len(pages) == 2


def test_build_overwrites_existing_file(artifacts_dir: Path):
    target = artifacts_dir / "out.epub"
    target.write_text("占位的旧内容", encoding="utf-8")
    path = EpubBuilder(FakeClient()).build(sample_thread(), "all", target)
    assert path == target
    with zipfile.ZipFile(path) as archive:
        assert archive.infolist()[0].filename == "mimetype"
        assert archive.read("mimetype") == b"application/epub+zip"
    # 旧内容的痕迹必须消失（覆盖而非追加）。
    assert b"\xe5\x8d\xa0\xe4\xbd\x8d" not in path.read_bytes()


def test_build_twice_same_path_is_consistent(artifacts_dir: Path):
    target = artifacts_dir / "same.epub"
    builder = EpubBuilder(FakeClient())
    first = builder.build(sample_thread(), "all", target)
    second = builder.build(sample_thread(), "all", target)
    assert first == second == target
    with zipfile.ZipFile(target) as archive:
        names = archive.namelist()
        assert names[0] == "mimetype"
        assert "OEBPS/text/post-0002.xhtml" in names
        ET.fromstring(archive.read("OEBPS/content.opf"))


# ---------- save ----------


def test_save_uses_derived_filename(artifacts_dir: Path):
    thread = sample_thread("导出的标题")
    path = EpubBuilder(FakeClient()).save(thread, "all", artifacts_dir)
    assert path == artifacts_dir / "导出的标题.epub"
    assert zipfile.is_zipfile(path)


def test_save_sanitizes_filename_and_creates_dir(artifacts_dir: Path):
    thread = sample_thread('标题/带:非法*字符?')
    target = artifacts_dir / "新建目录"
    path = EpubBuilder(FakeClient()).save(thread, "all", target)
    assert path.parent == target
    assert path.name == "标题_带_非法_字符_.epub"
    assert path.exists()


@pytest.mark.parametrize("scope", ["all", "po"])
@pytest.mark.parametrize("image_mode", ["embed", "url", "drop"])
def test_matrix_of_scope_and_image_mode_stays_valid(artifacts_dir: Path, scope: str, image_mode: str):
    path = build_epub(artifacts_dir, picture_thread(), scope=scope, image_mode=image_mode)
    with zipfile.ZipFile(path) as archive:
        assert archive.infolist()[0].filename == "mimetype"
        ET.fromstring(archive.read("OEBPS/content.opf"))
        for name in archive.namelist():
            if name.endswith(".xhtml"):
                ET.fromstring(archive.read(name))


# ---------- 文件名模板与指定饼干 ----------


def test_save_honours_filename_template(artifacts_dir: Path):
    thread = sample_thread("模板标题")
    builder = EpubBuilder(FakeClient(), filename_template="[{id}] {title}")
    assert builder.output_name(thread) == "[1001] 模板标题"
    path = builder.save(thread, "all", artifacts_dir)
    assert path == artifacts_dir / "[1001] 模板标题.epub"
    assert zipfile.is_zipfile(path)


def test_save_without_template_falls_back_to_derived_name(artifacts_dir: Path):
    path = EpubBuilder(FakeClient()).save(sample_thread("没有模板"), "all", artifacts_dir)
    assert path.name == "没有模板.epub"


def test_bad_template_falls_back():
    """模板写错时不应该让导出失败。"""
    builder = EpubBuilder(FakeClient(), filename_template="{unknown}")
    assert builder.output_name(sample_thread("兜底标题")) == "兜底标题"


def test_include_hashes_filters_posts(artifacts_dir: Path):
    thread = picture_thread()
    thread.posts.append(make_post(2003, "第三楼", user_hash="third777"))
    path = EpubBuilder(FakeClient()).build(
        thread, "all", artifacts_dir / "hashes.epub", include_hashes=["other999"]
    )
    with zipfile.ZipFile(path) as archive:
        pages = [name for name in archive.namelist() if name.startswith("OEBPS/text/")]
        assert len(pages) == 1
        page = archive.read(pages[0]).decode("utf-8")
    assert "other999" in page
    assert "POCOOKIE" not in page


def test_include_hashes_save_passes_through(artifacts_dir: Path):
    thread = picture_thread()
    path = EpubBuilder(FakeClient()).save(
        thread, "all", artifacts_dir, include_hashes=["POCOOKIE", "other999"]
    )
    with zipfile.ZipFile(path) as archive:
        pages = [name for name in archive.namelist() if name.startswith("OEBPS/text/")]
    assert len(pages) == 2
