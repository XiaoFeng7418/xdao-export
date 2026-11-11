"""导出报告：把「缺页 / 缺图」的清单落成一份文本，放在产物旁边。

为什么要有这个文件：这些信息本来只在日志区里滚过去，程序一关就没了。长串
导出缺了几页、哪几张图没下到，事后想核对只能靠翻日志的截图。落成文件之后
可以直接比对，也方便挑出哪些串需要补抓一次。

只在**真的有问题**时才写（缺页，或者有图片没下到）。一次干净的导出不留空
文件，导出目录不会因此多出一堆没用的东西。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

#: 报告文件名的前缀。时间后缀放在后面，同一个目录里多次导出不会互相覆盖。
REPORT_PREFIX = "导出报告"
#: 每个串最多列这么多条图片地址，剩下的只报总数 —— 报告是给人看的，不是日志。
MAX_IMAGES = 20


@dataclass
class ReportEntry:
    """一个串在这次导出里的情况。"""

    label: str
    title: str = ""
    output: str = ""
    posts: int = 0
    #: 缺页 / 撞上页数上限这类说明；空串表示这一份是完整的
    note: str = ""
    #: 没下到的图片地址（已去重）
    missing_images: tuple[str, ...] = field(default_factory=tuple)

    @property
    def has_problem(self) -> bool:
        """这一份是否需要进报告。"""
        return bool(self.note) or bool(self.missing_images)


def take_missing_images(exporter: object) -> tuple[str, ...]:
    """从导出器里取出「这次没下到的图片」，顺手清空。

    导出器是复用的：一次批量导出里同一个实例接连保存好几个串，所以取完必须
    清掉，否则第二个串的报告会把第一个串的缺图也算进去。PDF 的图片处理藏在
    内部的 ``HtmlBuilder`` 里，所以多找一层。
    """
    holder = getattr(exporter, "missing_images", None)
    if holder is None:
        holder = getattr(getattr(exporter, "_html", None), "missing_images", None)
    if not isinstance(holder, list) or not holder:
        return ()
    urls = tuple(dict.fromkeys(str(url) for url in holder))
    holder.clear()
    return urls


def _human_when(when: datetime) -> str:
    return when.strftime("%Y-%m-%d %H:%M:%S")


def render(
    entries: list[ReportEntry],
    *,
    when: datetime,
    output_dir: Path | None = None,
) -> str:
    """把报告正文拼出来。只渲染 ``has_problem`` 为真的条目。"""
    trouble = [entry for entry in entries if entry.has_problem]
    lines = ["X岛串导出报告", f"时间：{_human_when(when)}"]
    if output_dir is not None:
        lines.append(f"导出目录：{output_dir}")
    lines.append(f"这次有 {len(trouble)} 个串没能完整导出。")
    lines.append("")
    for entry in trouble:
        lines.append(f"── {entry.label}")
        if entry.title:
            lines.append(f"   标题：{entry.title}")
        if entry.output:
            lines.append(f"   成品：{entry.output}")
        if entry.posts:
            lines.append(f"   楼层：{entry.posts}")
        if entry.note:
            lines.append(f"   缺页：{entry.note}")
        if entry.missing_images:
            lines.append(f"   缺图：{len(entry.missing_images)} 张（正文里保留了原链接）")
            for url in entry.missing_images[:MAX_IMAGES]:
                lines.append(f"       {url}")
            if len(entry.missing_images) > MAX_IMAGES:
                lines.append(f"       …等 {len(entry.missing_images)} 张")
        lines.append("")
    lines.append("补抓提示：已经抓下来的页都存在缓存里，同一个串再导一次只会补缺的那几页。")
    return "\n".join(lines).rstrip("\n") + "\n"


def write_report(
    output_dir: Path,
    entries: list[ReportEntry],
    *,
    when: datetime | None = None,
) -> Path | None:
    """有问题就写一份报告，返回路径；一切正常返回 ``None``（不留空文件）。"""
    if not any(entry.has_problem for entry in entries):
        return None
    moment = when or datetime.now()
    directory = Path(output_dir)
    path = directory / f"{REPORT_PREFIX}-{moment:%Y%m%d-%H%M%S}.txt"
    path.write_text(render(entries, when=moment, output_dir=directory), encoding="utf-8")
    return path


__all__ = ["MAX_IMAGES", "REPORT_PREFIX", "ReportEntry", "render", "take_missing_images", "write_report"]
