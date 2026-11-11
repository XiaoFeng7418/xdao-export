"""导出报告的测试：只记「真正有问题」的那几个串，干净的一次不留文件。

报告是给事后核对用的：程序一关，日志区里滚过去的「缺了第 3 页」就查不到了，
所以这里重点断言「写进文件的内容够不够判断要不要补抓」，以及「别给干净的导出
目录里丢一个空文件」。
"""

from __future__ import annotations

from datetime import datetime

from xdao.export_report import (
    MAX_IMAGES,
    REPORT_PREFIX,
    ReportEntry,
    render,
    take_missing_images,
    write_report,
)


def test_a_clean_export_leaves_no_file(tmp_path):
    entries = [ReportEntry(label="7001", output="a.html", posts=10)]
    assert write_report(tmp_path, entries, when=datetime(2026, 11, 11, 21, 0, 0)) is None
    assert list(tmp_path.iterdir()) == []


def test_report_lists_pages_and_images(tmp_path):
    when = datetime(2026, 11, 11, 21, 0, 0)
    entries = [
        ReportEntry(
            label="7001",
            title="没抓全的串",
            output="a.html",
            posts=12,
            note="第 3 页抓了 2 次都没成功",
            missing_images=("https://cdn.example/a.jpg",),
        ),
        ReportEntry(label="7002", output="b.html", posts=3),
    ]
    path = write_report(tmp_path, entries, when=when)
    assert path is not None
    assert path.name == f"{REPORT_PREFIX}-20261111-210000.txt"
    text = path.read_text(encoding="utf-8")
    assert text.startswith("X岛串导出报告")
    assert "时间：2026-11-11 21:00:00" in text
    assert f"导出目录：{tmp_path}" in text
    assert "这次有 1 个串没能完整导出。" in text
    assert "── 7001" in text
    assert "标题：没抓全的串" in text
    assert "成品：a.html" in text
    assert "楼层：12" in text
    assert "缺页：第 3 页抓了 2 次都没成功" in text
    assert "缺图：1 张（正文里保留了原链接）" in text
    assert "https://cdn.example/a.jpg" in text
    assert "7002" not in text  # 干净的串不进报告
    assert "补抓提示" in text


def test_too_many_images_are_capped():
    urls = tuple(f"https://cdn.example/{index}.jpg" for index in range(MAX_IMAGES + 5))
    text = render(
        [ReportEntry(label="7001", missing_images=urls)],
        when=datetime(2026, 1, 1, 0, 0, 0),
    )
    assert f"缺图：{MAX_IMAGES + 5} 张" in text
    assert f"…等 {MAX_IMAGES + 5} 张" in text
    # 只列前 MAX_IMAGES 条，省得报告本身几十页。
    assert text.count("https://cdn.example/") == MAX_IMAGES


def test_take_missing_images_dedupes_and_clears():
    class FakeExporter:
        def __init__(self) -> None:
            self.missing_images = [
                "https://cdn.example/a.jpg",
                "https://cdn.example/a.jpg",
                "https://cdn.example/b.jpg",
            ]

    exporter = FakeExporter()
    assert take_missing_images(exporter) == (
        "https://cdn.example/a.jpg",
        "https://cdn.example/b.jpg",
    )
    # 取完必须清空：同一个导出器接着导出下一个串，不能把上一个串的缺图算进来。
    assert exporter.missing_images == []
    assert take_missing_images(exporter) == ()


def test_take_missing_images_reaches_into_the_pdf_html_builder():
    class FakeHtml:
        missing_images = ["https://cdn.example/c.jpg"]

    class FakePdf:
        _html = FakeHtml()

    assert take_missing_images(FakePdf()) == ("https://cdn.example/c.jpg",)


def test_take_missing_images_is_safe_on_exporters_without_the_field():
    assert take_missing_images(object()) == ()
