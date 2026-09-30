"""真机 PDF 渲染用例：显式设 ``XDAO_PDF_TEST=1`` 才跑（默认跳过）。

只在这一条路径上验证「纸张 / 页边距真的生效」——单元测试用假浏览器，
不可能证明真浏览器会把 A3 打成 A3。断言全部走**纯标准库**：解 PDF 里的
MediaBox 数字（PDF 的页面尺寸只能这么读，项目不引入任何第三方依赖）。

跑法::

    $env:XDAO_PDF_TEST = "1"
    python -m pytest tests/test_pdf_render.py -q

需要本机装有 Chrome 或 Edge（Windows 上基本都有）。
"""

from __future__ import annotations

import os
import re
import zlib
from pathlib import Path

import pytest

from xdao.exporters.pdf import PdfError, find_browser, render_html_to_pdf
from xdao.pdf_opts import PdfOptions

# 一份最小的、带中文的页面：只要能渲染出来就行，内容本身无关紧要。
SAMPLE_HTML = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>纸张测试</title>
<style>@page { size: A4; margin: 20mm; } body { font-family: sans-serif; }</style>
</head><body><h1>介质尺寸测试</h1><p>这一段用来确认纸张参数真的传给了浏览器。</p>
</body></html>
"""

MEDIABOX_RE = re.compile(rb"/MediaBox\s*\[\s*([0-9.]+)\s+([0-9.]+)\s+([0-9.]+)\s+([0-9.]+)\s*\]")

# A4 与 A3 的宽度（点，1 英寸 = 72 点）。浏览器给的是带小数的近似值，所以留 3 点余量。
A4_WIDTH_PT = 595.0
A3_WIDTH_PT = 842.0
TOLERANCE_PT = 3.0

_INTEGRATION_ENABLED = os.environ.get("XDAO_PDF_TEST") == "1"

try:
    _REAL_BROWSER = find_browser()
except PdfError:  # pragma: no cover - 没装浏览器
    _REAL_BROWSER = None


def media_box(path: Path) -> tuple[float, float, float, float]:
    """读出 PDF 第一页的 MediaBox（纯标准库：正则扫裸字节）。"""
    data = path.read_bytes()
    match = MEDIABOX_RE.search(data)
    if match is None:
        # 对象流可能被 FlateDecode 压着，解一层再找。
        for chunk in re.findall(rb"stream\r?\n(.*?)\r?\nendstream", data, re.S):
            if not chunk:
                continue
            try:
                plain = zlib.decompress(chunk)
            except zlib.error:
                continue
            match = MEDIABOX_RE.search(plain)
            if match is not None:
                break
    assert match is not None, "PDF 里没有 MediaBox，产物可能不完整"
    x0, y0, x1, y1 = (float(item) for item in match.groups())
    return x0, y0, x1, y1


@pytest.mark.skipif(
    not (_REAL_BROWSER and _INTEGRATION_ENABLED),
    reason="需要本机装了浏览器，并且显式设 XDAO_PDF_TEST=1 才跑（默认跳过）",
)
def test_real_browser_honours_the_paper_size(artifacts_dir: Path) -> None:
    """显式给 A3 纸张，产物也必须是 A3 —— 命令行做不到这件事，只有 CDP 能。"""
    assert _REAL_BROWSER is not None
    target = artifacts_dir / "a3.pdf"
    render_html_to_pdf(
        SAMPLE_HTML,
        target,
        browser_path=str(_REAL_BROWSER.path),
        options=PdfOptions(paper="a3"),
        timeout=180,
    )
    assert target.read_bytes()[:4] == b"%PDF"
    _, _, width, height = media_box(target)
    assert abs(width - A3_WIDTH_PT) <= TOLERANCE_PT, (width, height)
    assert width < height, "默认是纵向"


@pytest.mark.skipif(
    not (_REAL_BROWSER and _INTEGRATION_ENABLED),
    reason="需要本机装了浏览器，并且显式设 XDAO_PDF_TEST=1 才跑（默认跳过）",
)
def test_real_browser_default_keeps_the_page_style(artifacts_dir: Path) -> None:
    """全默认＝跟随网页样式：页面里写的是 A4，产物就该是 A4。"""
    assert _REAL_BROWSER is not None
    target = artifacts_dir / "default.pdf"
    render_html_to_pdf(
        SAMPLE_HTML,
        target,
        browser_path=str(_REAL_BROWSER.path),
        timeout=180,
    )
    assert target.read_bytes()[:4] == b"%PDF"
    _, _, width, height = media_box(target)
    assert abs(width - A4_WIDTH_PT) <= TOLERANCE_PT, (width, height)


if __name__ == "__main__":  # pragma: no cover - 手动跑一把
    raise SystemExit(pytest.main([__file__, "-v", "-s"]))
