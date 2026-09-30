"""导出器公共基础：文本处理、文件名推导、图片解析。

所有导出器（HTML / TXT / EPUB / Markdown）都复用这里的函数，
避免同一套逻辑在多处实现后渐渐走样。
"""

from __future__ import annotations

import html as html_lib
import os
import re
import uuid
from dataclasses import dataclass
from pathlib import Path

from ..client import Post, XdaoClient

_TAG_RE = re.compile(r"<[^>]+>")
# 文件名里 Windows 不允许出现的字符。
_ILLEGAL_FILENAME_CHARS = re.compile(r'[\\/:*?"<>|]')
# 一句话的结束符，中英文都要覆盖。
_SENTENCE_SPLIT = re.compile(r"[\n。！？!?]")
# 正文里 <img src="..."> 的提取。
_IMG_SRC_RE = re.compile(r'<img[^>]+src="([^"]+)"', re.IGNORECASE)
# Markdown 里图片链接的还原（供 EPUB 的 XHTML 转换使用）。
_MD_IMAGE_RE = re.compile(r"!\[[^\]]*\]\(([^)]+)\)")


def plain_text(value: str) -> str:
    """去掉 HTML 标记，保留可读文本；``<br>`` 视为换行。"""
    value = re.sub(r"<br\s*/?>", "\n", value, flags=re.IGNORECASE)
    value = _TAG_RE.sub("", value)
    value = html_lib.unescape(value)
    return value.strip()


def sanitize_filename(name: str, fallback: str = "xdao-thread") -> str:
    """把任意字符串规整成安全的 Windows 文件名（不含扩展名）。"""
    cleaned = _ILLEGAL_FILENAME_CHARS.sub("_", name).strip()
    cleaned = cleaned.strip(" .")
    if len(cleaned) > 80:
        cleaned = cleaned[:80].strip(" .")
    return cleaned or fallback


def render_filename(template: str | None, thread: "ThreadData", fallback: str) -> str:
    """按模板渲染文件名主体（不含扩展名）。

    可用占位符：``{title}`` 标题、``{id}`` 串号、``{date}`` 导出日期（YYYYMMDD）、
    ``{po}`` PO 饼干、``{count}`` 楼层数。模板为空或渲染失败时回退到默认标题推导。
    """
    if not template:
        return fallback
    import datetime

    try:
        rendered = template.format(
            title=fallback,
            id=thread.thread_id,
            date=datetime.date.today().strftime("%Y%m%d"),
            po=thread.po_hash or "",
            count=len(thread.posts),
        )
    except (KeyError, IndexError, ValueError):
        # 模板写错不该导致导出失败，静默回退。
        return fallback
    return sanitize_filename(rendered) or fallback


@dataclass
class ThreadData:
    """一个串的完整数据。"""

    thread_id: int
    title: str
    po_hash: str
    posts: list[Post]

    @property
    def reply_count(self) -> int:
        """回复数（不含主帖）。"""
        return max(0, len(self.posts) - 1)

    def is_po_post(self, post: Post) -> bool:
        """主帖本身，或与发串人同一饼干，都算 PO。"""
        return bool(post.is_po or (self.po_hash and post.user_hash == self.po_hash))

    def filter_posts(
        self,
        scope: str = "all",
        include_hashes: list[str] | None = None,
    ) -> list[Post]:
        """按范围筛选楼层。

        - ``scope="all"``：全部楼层；``scope="po"``：仅 PO（主帖始终保留）。
        - ``include_hashes``：非空时只保留这些饼干的楼层；此时主帖也遵守该筛选，
          因为它同样属于某个饼干。
        """
        wanted = {h.strip() for h in include_hashes or [] if h and h.strip()}
        selected: list[Post] = []
        for post in self.posts:
            if wanted and post.user_hash not in wanted:
                continue
            if not wanted and scope == "po" and not self.is_po_post(post):
                continue
            selected.append(post)
        return selected


def derive_filename(thread: ThreadData) -> str:
    """默认文件名推导：优先标题，无标题时取第一楼正文的第一句话。"""
    title = plain_text(thread.title)
    if title and title not in ("无标题",):
        return title
    for post in thread.posts:
        text = plain_text(post.content)
        if not text:
            continue
        first = _SENTENCE_SPLIT.split(text)[0].strip()
        if first:
            return first
    return f"串{thread.thread_id}"


def resolve_image_url(client: XdaoClient, src: str) -> str:
    """把正文里的图片地址补全为绝对 URL。"""
    if not src:
        return ""
    src = src.strip()
    if src.startswith("http://") or src.startswith("https://"):
        return src
    if src.startswith("data:"):
        return src
    base = client.cdn_path or ""
    if base and not base.endswith("/"):
        base += "/"
    return base + src.lstrip("/")


def iter_post_image_urls(client: XdaoClient, post: Post) -> list[str]:
    """收集一楼涉及的全部图片绝对 URL（去重、保序）。"""
    urls: list[str] = []
    if post.img and post.ext:
        urls.append(client.image_url(post.img, post.ext))
    for match in _IMG_SRC_RE.findall(post.content or ""):
        urls.append(resolve_image_url(client, html_lib.unescape(match)))

    seen: set[str] = set()
    unique: list[str] = []
    for url in urls:
        if url and url not in seen:
            seen.add(url)
            unique.append(url)
    return unique


def fetch_image(client: XdaoClient, url: str) -> bytes | None:
    """下载图片，失败返回 None（导出流程绝不因单张图片中断）。"""
    if not url or url.startswith("data:"):
        return None
    try:
        return client.download_image(url)
    except Exception:
        return None


def guess_mime(url: str, data: bytes | None = None) -> str:
    """判断图片 MIME：优先看魔数，其次看扩展名。"""
    if data:
        if data[:3] == b"\xff\xd8\xff":
            return "image/jpeg"
        if data[:8] == b"\x89PNG\r\n\x1a\n":
            return "image/png"
        if data[:6] in (b"GIF87a", b"GIF89a"):
            return "image/gif"
        if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            return "image/webp"
    lowered = (url or "").lower().split("?")[0]
    for ext, mime in (
        (".jpg", "image/jpeg"),
        (".jpeg", "image/jpeg"),
        (".png", "image/png"),
        (".gif", "image/gif"),
        (".webp", "image/webp"),
    ):
        if lowered.endswith(ext):
            return mime
    return "image/jpeg"


_MIME_EXT = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
}


def mime_to_ext(mime: str) -> str:
    """MIME 对应的文件扩展名。"""
    return _MIME_EXT.get(mime, ".jpg")


def collapse_blank_lines(text: str) -> str:
    """把 3 个以上连续空行折叠成 2 个，并去掉首尾空白。"""
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def render_inline_content(
    client: XdaoClient,
    content: str,
    image_mode: str = "url",
    embedder=None,
) -> str:
    """把一楼正文渲染成干净的 Markdown 文本。

    - ``<br>`` 变成换行，其它标签只保留文字内容（并转义 Markdown 里有歧义的字符）。
    - ``image_mode="url"``：图片输出为 ``![图片](绝对URL)``。
    - ``image_mode="drop"``：丢弃图片。
    - ``image_mode="embed"``：调用 ``embedder(url)`` 取 data URI，失败则回退成 URL 形式。
    """
    if not content:
        return ""

    from html.parser import HTMLParser

    class _Renderer(HTMLParser):
        def __init__(self) -> None:
            super().__init__(convert_charrefs=True)
            self.parts: list[str] = []

        def handle_starttag(self, tag, attrs) -> None:
            tag = tag.lower()
            if tag == "br":
                self.parts.append("\n")
            elif tag == "img" and image_mode != "drop":
                src = dict(attrs).get("src") or ""
                url = resolve_image_url(client, src)
                target = url
                if image_mode == "embed" and embedder is not None:
                    embedded = embedder(url) if url else None
                    if embedded:
                        target = embedded
                if target:
                    self.parts.append(f"\n![图片]({target})\n")

        def handle_startendtag(self, tag, attrs) -> None:
            self.handle_starttag(tag, attrs)

        def handle_endtag(self, tag) -> None:
            if tag.lower() == "br":
                self.parts.append("\n")

        def handle_data(self, data) -> None:
            self.parts.append(data)

    renderer = _Renderer()
    renderer.feed(content)
    renderer.close()
    text = "".join(renderer.parts)
    # 行尾空白清掉，但保留段落结构。
    text = "\n".join(line.rstrip() for line in text.split("\n"))
    return collapse_blank_lines(text)


def markdown_images_to_xhtml(text: str, image_attr) -> str:
    """把 Markdown 文本转成 XHTML 片段（EPUB 用）。

    ``image_attr(url) -> str`` 由调用方给出 ``<img>`` 的 src（可能是内嵌 data URI），
    返回空串表示该图片无法使用，则丢弃。
    """
    blocks: list[str] = []
    paragraph: list[str] = []

    def flush() -> None:
        if paragraph:
            blocks.append("<p>" + "<br/>".join(paragraph) + "</p>")
            paragraph.clear()

    for raw_line in text.split("\n"):
        line = raw_line.strip()
        if not line:
            flush()
            continue
        match = _MD_IMAGE_RE.fullmatch(line)
        if match:
            flush()
            src = image_attr(match.group(1))
            if src:
                blocks.append(
                    f'<div class="img"><img src="{html_lib.escape(src, quote=True)}" alt="图片"/></div>'
                )
            continue
        paragraph.append(_escape_xhtml_line(line))
    flush()
    return "\n".join(blocks)


def _escape_xhtml_line(line: str) -> str:
    """转义文本，同时保留行内 Markdown 图片为后续处理让路（此处只做纯文本转义）。"""
    return html_lib.escape(line, quote=False)


def output_path(output_dir: Path, name: str, suffix: str) -> Path:
    """拼出导出文件路径，文件名安全化后加上扩展名。"""
    return Path(output_dir) / (sanitize_filename(name) + suffix)


class OutputDirNotWritable(Exception):
    """导出目录不可写。提前抛出，避免白抓一遍再失败。"""


# 写权限探针的文件名：故意用**普通文件名**而不是隐藏文件。
# 2026-09-30 的教训：原来的探针叫 ``.xdao-write-probe``，被安全软件/策略挡掉后
# 程序就拒绝导出，而用户真正的导出文件明明写得进去（0.1.0 能做的事 0.5.0 反而
# 做不了）。探针要和真实导出物同类，结论才有意义。
PROBE_NAME = "xdao-write-test.tmp"


def ensure_writable(output_dir: Path | str, kind: str = "导出") -> Path:
    """确认导出目录可写，返回规范化后的目录。

    抓一个长串可能要几分钟，如果最后才发现目录写不进去，那一趟就白跑了，
    所以开始抓之前先探一次。目录不存在时会尝试创建。

    **探针失败不再等于目录不可写**：0.5.0 之前只要有一步写不进去就直接拦下
    导出，结果用户明明能正常写这个目录（用记事本、用旧版本都行），程序却弹出
    「导出目录不可写」拒绝开工 —— 那是探针文件（``.xdao-write-probe``）自己被
    安全软件/策略/只读介质挡了，跟真正的导出结果文件不是一回事。现在：

    * 探针文件改成**和导出结果同类的普通文件**（``xdao-write-test.tmp``，不留
      隐藏属性、不带前导点），写完立刻删掉 —— 它写不动往往意味着真的写不了；
    * 即使这样探针还是失败，也**不拦下导出**：真实写盘失败会带着真实文件名和
      真实 errno 报出来；
    * 只有"目录连创建都做不到"才提前失败（那才是真的没法用）。
    """
    target = Path(output_dir)
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise OutputDirNotWritable(
            f"{kind}目录无法创建：{target}\n{exc}\n"
            "请换一个可写的目录，或检查该位置的权限。"
        ) from exc

    probe = target / PROBE_NAME
    try:
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError:
        # 探针写不动 —— 只是少了一层提前预警，不代表导出会失败
        # （不同名的文件、已有的文件往往照样能写）。
        probe.unlink(missing_ok=True)
    return target


def can_write_dir(directory: Path | str) -> bool:
    """轻量探测：目录能不能写。只回答是或否，不抛异常。

    和 ``ensure_writable`` 探的是同一件事，区别是这里要拿来**换地方重试**
    （例如缓存目录写不进去时另找一个），所以失败不该炸掉调用方。

    探测要**往下一层**探，不能只看这一层：用户报过 ``D:\\X岛\\.cache`` 这一级
    建得出来、``.cache\\pages`` 却拒绝访问——只探表层会把这种目录判成"能写"，
    然后换目录的兜底逻辑就永远不会触发（v0.5.1 的实测教训）。所以这里既试建
    文件，也试建一个临时子目录，任何一步失败就算不能写。
    """
    target = Path(directory)
    probe = target / PROBE_NAME
    write_ok = True
    try:
        target.mkdir(parents=True, exist_ok=True)
        probe.write_text("ok", encoding="utf-8")
    except OSError:
        write_ok = False
    finally:
        try:
            probe.unlink(missing_ok=True)
        except OSError:
            pass
    if not write_ok:
        return False
    # 下一层：真实导出/缓存都会在自己下面建子目录（cache/pages、images/ab/ 等）。
    child = target / f"{PROBE_NAME}.d{os.getpid()}-{uuid.uuid4().hex[:8]}"
    try:
        child.mkdir()
    except OSError:
        return False
    try:
        (child / PROBE_NAME).write_text("ok", encoding="utf-8")
    except OSError:
        return False
    finally:
        # 先删文件再删目录，别在用户目录里留垃圾。
        try:
            (child / PROBE_NAME).unlink(missing_ok=True)
        except OSError:
            pass
        try:
            child.rmdir()
        except OSError:
            pass
    return True
