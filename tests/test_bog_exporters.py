"""Offline export checks for normalized public-site posts."""

from __future__ import annotations

import base64
import re
import zipfile
from pathlib import Path

import pytest

from tests import FakeClient, SAMPLE_PNG, make_post
from xdao.client import PostImage
from xdao.exporters import EpubBuilder, HtmlBuilder, MarkdownBuilder, PdfBuilder, ThreadData, TxtBuilder
from xdao.exporters._shared import attachment_image_urls, iter_post_image_urls

URLS = ("https://bog.ac/image/large/first.png", "https://bog.ac/image/large/second.png")
STAMP = "2026-11-21 08:00:00"


def public_thread() -> ThreadData:
    post = make_post(12345678, '正文<br><a href="/t/7001">&gt;&gt;No.7001</a>', now=STAMP, is_po=True)
    post.images = [PostImage(URLS[0], ".png"), PostImage(URLS[1], ".png"), PostImage(URLS[0], ".png")]
    post.emoji = "🐱"
    reply = make_post(7001, "回复", now=STAMP)
    return ThreadData(12345678, "公开多图样例", "abc123", [post, reply], site="bog")


def client() -> FakeClient:
    return FakeClient(images={url: SAMPLE_PNG for url in URLS})


def test_normalized_attachments_are_deduplicated_without_legacy_url_building() -> None:
    post = public_thread().posts[0]
    post.img, post.ext = "legacy", ".jpg"
    fake = client()
    assert attachment_image_urls(fake, post) == list(URLS)
    assert iter_post_image_urls(fake, post) == list(URLS)


@pytest.mark.parametrize("mode", ["embed", "url", "drop", "folder", "dir"])
def test_html_renders_all_public_images_and_respects_mode(mode: str, artifacts_dir: Path) -> None:
    fake = client()
    path = HtmlBuilder(fake, image_mode=mode).save(public_thread(), output_dir=artifacts_dir)
    text = path.read_text(encoding="utf-8")
    assert "🐱" in text and STAMP in text
    assert 'href="#p7001"' in text and 'id="p7001"' in text
    assert "&lt;a" not in text
    if mode == "drop":
        assert '<img class="post-img"' not in text and fake.downloaded == []
    else:
        assert text.count('<img class="post-img"') == 2
        if mode == "embed":
            assert text.count("data:image/png;base64,") == 2
        elif mode in ("folder", "dir"):
            assert len(list(path.with_name(path.stem + ".files").glob("*.png"))) == 2
        else:
            assert all(url in text for url in URLS) and fake.downloaded == []
    if mode in ("embed", "folder", "dir"):
        assert sorted(fake.downloaded) == sorted(URLS)


@pytest.mark.parametrize("mode", ["embed", "url", "drop", "folder", "dir"])
def test_markdown_renders_all_public_images_and_respects_mode(mode: str, artifacts_dir: Path) -> None:
    fake = client()
    path = MarkdownBuilder(fake, image_mode=mode).save(public_thread(), output_dir=artifacts_dir)
    text = path.read_text(encoding="utf-8")
    assert "🐱" in text and STAMP in text
    assert '<a id="p7001"></a>' in text and "](\u0023p7001)" in text
    if mode == "drop":
        assert "![图片]" not in text and fake.downloaded == []
    else:
        assert text.count("![图片]") == 2
        if mode == "embed":
            assert text.count("data:image/png;base64,") == 2
        elif mode in ("folder", "dir"):
            assert len(list(path.with_name(path.stem + ".files").glob("*.png"))) == 2
        else:
            assert all(url in text for url in URLS) and fake.downloaded == []


@pytest.mark.parametrize("mode", ["embed", "url", "drop", "folder", "dir"])
def test_epub_renders_all_public_images_without_duplicate_payloads(mode: str, artifacts_dir: Path) -> None:
    fake = client()
    path = EpubBuilder(fake, image_mode=mode).save(public_thread(), output_dir=artifacts_dir)
    with zipfile.ZipFile(path) as archive:
        text = archive.read("OEBPS/text/post-0001.xhtml").decode("utf-8")
        assert "🐱" in text and STAMP in text
        assert 'href="post-0002.xhtml#p7001"' in text
        assert not any(name.startswith("OEBPS/images/") for name in archive.namelist())
        if mode == "drop":
            assert "<img " not in text and fake.downloaded == []
        else:
            assert text.count("<img ") == 2
            if mode == "url":
                assert all(url in text for url in URLS) and fake.downloaded == []
            else:
                assert text.count("data:image/png;base64,") == 2
                assert sorted(fake.downloaded) == sorted(URLS)


def test_txt_preserves_all_image_links_and_normalized_metadata() -> None:
    fake = client()
    text = TxtBuilder(fake).build(public_thread())
    assert "🐱" in text and STAMP in text and ">>No.7001" in text
    assert all(text.count("[图片] " + url) == 1 for url in URLS)
    assert fake.downloaded == []


@pytest.mark.parametrize("mode", ["embed", "url", "drop", "folder", "dir"])
def test_pdf_streams_public_images_and_references_to_renderer(mode: str, artifacts_dir: Path, monkeypatch) -> None:
    fake = client()
    captured: list[str] = []
    builder = PdfBuilder(fake, image_mode=mode)

    def render(html, work_dir, output_pdf, html_writer=None):
        intermediate = artifacts_dir / "printed.html"
        html_writer(intermediate)
        captured.append(intermediate.read_text(encoding="utf-8"))
        output_pdf.write_bytes(b"%PDF-1.4 offline")
        return output_pdf

    monkeypatch.setattr(builder, "render_pdf", render)
    assert builder.save(public_thread(), output_dir=artifacts_dir).read_bytes().startswith(b"%PDF")
    text = captured[0]
    assert "🐱" in text and STAMP in text and 'href="#p7001"' in text
    assert text.count('<img class="post-img"') == (0 if mode == "drop" else 2)
    if mode in ("embed", "folder", "dir"):
        assert text.count("data:image/png;base64,") == 2


@pytest.mark.parametrize("builder_class", [HtmlBuilder, MarkdownBuilder, TxtBuilder])
@pytest.mark.parametrize("mode", ["embed", "url", "drop", "folder", "dir"])
def test_public_merged_export_includes_each_threads_images(builder_class, mode: str, artifacts_dir: Path) -> None:
    first = public_thread()
    second = public_thread()
    second.thread_id = 7002
    second.posts[0].id = 7002
    fake = client()
    path = builder_class(fake, image_mode=mode).save_many([first, second], output_dir=artifacts_dir)
    text = path.read_text(encoding="utf-8")
    assert text.count("🐱") == 2
    if builder_class is TxtBuilder:
        assert all(text.count(url) == 2 for url in URLS)
    elif mode == "drop":
        assert not any(url in text for url in URLS)
        assert fake.downloaded == []
    elif mode == "embed":
        assert text.count("data:image/png;base64,") == 4
    elif mode == "url":
        assert all(text.count(url) == 2 for url in URLS)
        assert fake.downloaded == []
    else:
        assert len(list(path.with_name(path.stem + ".files").glob("*.png"))) == 2


def test_merged_html_namespaces_bog_anchors_away_from_xdao(artifacts_dir: Path) -> None:
    xdao = ThreadData(12345678, "X样例", "x", [make_post(12345678, "X楼")])
    bog = public_thread()
    bog.posts[0].content = ">>No.12345678"

    document = HtmlBuilder(FakeClient(), image_mode="drop").build_many([xdao, bog])

    assert document.count('id="p12345678"') == 1
    assert 'href="#bog-2-12345678"' in document
    assert 'id="bog-2-12345678"' in document


def test_merged_markdown_namespaces_repeated_bog_post_ids() -> None:
    first = public_thread()
    second = public_thread()
    second.posts[0].content = ">>No.12345678"

    text = MarkdownBuilder(FakeClient(), image_mode="drop").build_many([first, second])

    assert text.count('<a id="bog-1-12345678"></a>') == 1
    assert text.count('<a id="bog-2-12345678"></a>') == 1
    assert "](#bog-2-12345678)" in text


@pytest.mark.parametrize("builder_class", [HtmlBuilder, MarkdownBuilder])
def test_filtered_bog_export_does_not_link_to_omitted_posts(builder_class) -> None:
    thread = public_thread()
    thread.posts[0].content = ">>No.7001"
    thread.posts[1].user_hash = "other-cookie"

    text = builder_class(FakeClient(), image_mode="drop").build(thread, scope="po")

    assert "#p7001" not in text


def test_filtered_epub_keeps_quote_text_without_a_dead_link(artifacts_dir: Path) -> None:
    thread = public_thread()
    thread.posts[0].content = ">>No.7001"
    thread.posts[1].user_hash = "other-cookie"

    path = EpubBuilder(FakeClient(), image_mode="drop").save(
        thread, scope="po", output_dir=artifacts_dir
    )

    with zipfile.ZipFile(path) as archive:
        text = archive.read("OEBPS/text/post-0001.xhtml").decode("utf-8")
    assert "&gt;&gt;No.7001" in text
    assert "post-0002.xhtml#p7001" not in text


def test_pdf_merged_cross_site_quote_uses_bog_anchor(
    artifacts_dir: Path, monkeypatch
) -> None:
    xdao = ThreadData(12345678, "X样例", "x", [make_post(12345678, "X楼")])
    bog = public_thread()
    bog.posts[0].content = ">>No.12345678"
    builder = PdfBuilder(FakeClient(), image_mode="drop")
    captured: list[str] = []

    def render(html, work_dir, output_pdf, html_writer=None):
        intermediate = artifacts_dir / "merged-reference.html"
        html_writer(intermediate)
        captured.append(intermediate.read_text(encoding="utf-8"))
        output_pdf.write_bytes(b"%PDF-1.4 offline")
        return output_pdf

    monkeypatch.setattr(builder, "render_pdf", render)
    builder.save_many([xdao, bog], output_dir=artifacts_dir)

    assert captured[0].count('id="p12345678"') == 1
    assert 'href="#bog-2-12345678"' in captured[0]


def test_embedded_public_images_decode_to_original_bytes(artifacts_dir: Path) -> None:
    path = EpubBuilder(client()).save(public_thread(), output_dir=artifacts_dir)
    with zipfile.ZipFile(path) as archive:
        text = archive.read("OEBPS/text/post-0001.xhtml").decode("utf-8")
    payloads = re.findall(r"data:image/png;base64,([^\"]+)", text)
    assert len(payloads) == 2
    assert all(base64.b64decode(data) == SAMPLE_PNG for data in payloads)


def test_markdown_public_default_embeds_without_changing_legacy_default() -> None:
    fake = client()
    public = MarkdownBuilder(fake).build(public_thread())
    assert public.count("data:image/png;base64,") == 2
    legacy = ThreadData(7001, "旧样例", "abc123", [make_post(7001, "旧正文", img="legacy", ext=".jpg")])
    assert "data:image" not in MarkdownBuilder(FakeClient()).build(legacy)


def test_markdown_embed_checks_cancellation_between_images() -> None:
    import threading

    from xdao.cancel import ExportCancelled

    stop = threading.Event()
    fake = client()
    download = fake.download_image

    def first_image(url: str) -> bytes:
        data = download(url)
        stop.set()
        return data

    fake.download_image = first_image
    with pytest.raises(ExportCancelled):
        MarkdownBuilder(fake, image_mode="embed", stop=stop).build(public_thread())
    assert len(fake.downloaded) == 1


def test_markdown_failed_embedding_keeps_urls_and_reports_missing() -> None:
    fake = FakeClient(fail_urls=set(URLS))
    builder = MarkdownBuilder(fake, image_mode="embed")
    text = builder.build(public_thread())
    assert all(url in text for url in URLS)
    assert builder.missing_images == list(URLS)


@pytest.mark.parametrize("mode", ["folder", "dir"])
def test_epub_explicit_build_folder_modes_embed(mode: str, artifacts_dir: Path) -> None:
    fake = client()
    path = EpubBuilder(fake).save(public_thread(), output_dir=artifacts_dir, image_mode=mode)
    with zipfile.ZipFile(path) as archive:
        text = archive.read("OEBPS/text/post-0001.xhtml").decode("utf-8")
    assert text.count("data:image/png;base64,") == 2
    assert sorted(fake.downloaded) == sorted(URLS)


@pytest.mark.parametrize("mode", ["embed", "url", "drop", "folder", "dir"])
def test_pdf_merged_public_export_uses_multigraph_html(mode: str, artifacts_dir: Path, monkeypatch) -> None:
    builder = PdfBuilder(client(), image_mode=mode)
    captured: list[str] = []

    def render(html, work_dir, output_pdf, html_writer=None):
        intermediate = artifacts_dir / "merged.html"
        html_writer(intermediate)
        captured.append(intermediate.read_text(encoding="utf-8"))
        output_pdf.write_bytes(b"%PDF-1.4 offline")
        return output_pdf

    monkeypatch.setattr(builder, "render_pdf", render)
    builder.save_many([public_thread(), public_thread()], output_dir=artifacts_dir)
    text = captured[0]
    if mode in ("embed", "folder", "dir"):
        assert text.count("data:image/png;base64,") == 4
    elif mode == "url":
        assert all(text.count(url) == 2 for url in URLS)
    else:
        assert '<img class="post-img"' not in text
    assert text.count("🐱") == 2
