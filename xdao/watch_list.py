"""监控列表的导入 / 导出（纯文本 JSON，界面与命令行共用）。

监控列表平时存在配置文件的 ``watch_targets`` 里，跟着配置一起走。这一层是
**另一条可搬移的通道**：导出一个 ``.json`` 文件，换台机器、或者重装之后
导进来，不必一条条重新填网址。

设计上刻意做成「与界面无关」的一层，和 :mod:`xdao.watcher` 同样的理由：
导入要能单独测，也要能在命令行里用。

两条硬规矩（都是真踩过的坑倒逼出来的）：

- **导入必须容错**：文件是用户手改得动的文本，坏一处不能带走整份列表，
  也不能报个 ``KeyError`` 上去。逐条校验，坏的跳过并报出原因。
- **导入默认合并、不覆盖**：用户手上那份列表是他自己攒的，一次导入把它
  抹掉是不可接受的。重复的（同一个串、同样的范围/格式/图片模式/饼干筛选）
  直接跳过 —— 判据用 :func:`~xdao.watcher.target_key`，和监控本身认键的方式一致。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path

from .cache import parse_thread_id
from .client import XdaoClient
from .exporters import EXPORTERS
from .watcher import WatchTarget, target_key

#: 导出文件的格式版本。以后字段变了靠它认，不认识就明确拒绝而不是猜着读。
SCHEMA_VERSION = 1

#: 文件后缀（界面文件对话框与命令行提示共用）。
FILE_SUFFIX = ".json"

#: 一份列表最多多大。防的是「选错文件」——用户把几百兆的东西选进来时，
#: 先看大小拒绝，比读进内存再报 MemoryError 友好。
MAX_FILE_BYTES = 10 * 1024 * 1024

_SCOPES = ("all", "po")
_IMAGE_MODES = ("embed", "url", "drop")


class WatchListError(Exception):
    """导入 / 导出失败，消息是给用户看的中文。"""


@dataclass
class ImportResult:
    """一次导入的结果：加进来的、跳过的、以及为什么跳过。

    ``skipped`` 里是 ``(原因, 指认这一条的说法)``。整份文件坏掉的情况直接抛
    :class:`WatchListError`，所以能拿到这个对象就说明「文件是好的，只是有些条目不能用」。
    """

    added: list[WatchTarget] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)
    total: int = 0
    #: 文件里的条目**已经在当前列表里**（同一个串、同样的设置）的那些。
    #: 合并时它们什么都不用做；但「用文件替换现有列表」时它们是**要留下来的** ——
    #: 少了这一份，「替换」就会把它们当垃圾丢掉（列表被清空）。
    duplicates: list[WatchTarget] = field(default_factory=list)

    @property
    def summary(self) -> str:
        """一句话说清这次导入干了什么。"""
        parts = [f"新增 {len(self.added)} 条"]
        if self.duplicates:
            parts.append(f"原本就有 {len(self.duplicates)} 条")
        if self.skipped:
            parts.append(f"跳过 {len(self.skipped)} 条")
        return "，".join(parts)


def suggested_filename(stamp: float | None = None) -> str:
    """导出时的建议文件名，带日期便于区分多次导出。"""
    when = time.localtime(stamp) if stamp is not None else time.localtime()
    return f"xdao-export-监控列表-{time.strftime('%Y%m%d', when)}{FILE_SUFFIX}"


def _describe_target(target: WatchTarget) -> str:
    """给跳过原因配一个人能认出来的说法。"""
    label = target.label or target.url_or_id or "(空的网址)"
    return f"No.{label}"


def _normalize_text(value: object) -> str:
    return str(value).strip() if value is not None else ""


def _clean_url(raw: object, thread_id: int) -> str:
    """把条目里的网址收拾成 :class:`WatchTarget` 认的样子。

    抓取只用到串号（``cache.CachedThreadFetcher.fetch`` 一进门就 ``parse_thread_id``），
    所以网址本身写得不规范也不影响监控。这里只在两种情况下去动手改它：

    - **压根没写网址**（手写文件时最常见的就是只写一个号）：补一个裸串号，
      不去编一个网址 —— 用户看到的就是自己写的东西；
    - **写了但认不出串号**（比如网址被截断了）：补成站点标准网址，
      这样这一条还能用，也会在界面上显示成正常的样子。
    """
    text = _normalize_text(raw)
    if not text:
        return str(thread_id)
    if parse_thread_id(text) is not None:
        return text
    return f"{XdaoClient.SITE}/t/{thread_id}"


def _clean_hashes(raw: object) -> list[str] | None:
    """饼干筛选：认「字符串列表」；不是列表就当条目有问题（返回 None）。"""
    if raw is None:
        return []
    if isinstance(raw, str):
        # 手写文件时容易写成一个字符串。逗号分隔还认，整段当一块饼干不认——
        # 那多半是写错了，而且写错之后会一直「筛选范围内没有楼层」。
        pieces = [p.strip() for p in raw.replace("，", ",").split(",")]
        return [p for p in pieces if p]
    if isinstance(raw, (list, tuple)):
        return [str(h).strip() for h in raw if str(h).strip()]
    return None


def target_from_entry(entry: dict) -> WatchTarget:
    """把文件里的一条记录变成 :class:`WatchTarget`，坏的就抛 :class:`WatchListError`。

    ``thread_id`` 一律**重新算**，不信文件里写的那个：监控键（``target_key``）是
    按串号拼的，串号被人改过之后，一份「指向 7001 的缓存状态」会挂到 8002 上，
    表现是明明有新回复却一直报「无更新」。这类错误只看文件是看不出来的。
    """
    if not isinstance(entry, dict):
        raise WatchListError("这一条不是一段配置（应该是一个 {} 包起来的小节）")

    thread_id = parse_thread_id(entry.get("url_or_id"))
    if thread_id is None:
        thread_id = parse_thread_id(entry.get("thread_id"))
    if thread_id is None:
        raise WatchListError(f"认不出串号：{_normalize_text(entry.get('url_or_id')) or '(空)'}")

    scope = _normalize_text(entry.get("scope")).lower() or "all"
    if scope not in _SCOPES:
        raise WatchListError(f"范围认不出：{scope}（只能是 all 或 po）")

    format_key = _normalize_text(entry.get("format_key")).lower() or "html"
    if format_key not in EXPORTERS:
        allowed = "、".join(sorted(EXPORTERS))
        raise WatchListError(f"格式认不出：{format_key}（只能是 {allowed}）")

    image_mode = _normalize_text(entry.get("image_mode")).lower() or "embed"
    if image_mode not in _IMAGE_MODES:
        raise WatchListError(
            f"图片处理认不出：{image_mode}（只能是 {'、'.join(_IMAGE_MODES)}）"
        )

    hashes = _clean_hashes(entry.get("hashes", entry.get("include_hashes")))
    if hashes is None:
        raise WatchListError("饼干筛选（hashes）应该是一个列表")

    return WatchTarget(
        url_or_id=_clean_url(entry.get("url_or_id"), thread_id),
        scope=scope,
        format_key=format_key,
        include_hashes=hashes,
        image_mode=image_mode,
    )


def _target_record(target: WatchTarget) -> dict:
    """导出时一条记录的形态 —— 只有「怎么监控」，不带跑出来的状态。

    ``state`` / ``last_check`` / ``last_error`` / ``exports`` / ``last_export_path``
    这类字段是**跑出来的临时状态**，跟着缓存目录走。导出它们会让导入方以为
    「这个串已经检查过了、文件也导出过了」，于是第一轮该导出的不导出；
    而缓存目录在另一台机器上根本没有。所以只搬配置，导入后第一轮会正常导出一份。

    配置那几项与配置文件里的形态**同一个来源**（``WatchTarget.to_dict``，v0.13.49
    起统一写 ``hashes``），这里只多补一个 ``thread_id`` 方便人读写。
    """
    record = target.to_dict()
    record["thread_id"] = target.thread_id
    return record


def export_targets(
    targets: list[WatchTarget],
    path: str | Path,
    *,
    stamp: float | None = None,
) -> Path:
    """把监控列表写成 JSON 文件，返回真正写入的路径。

    列表为空时也照写（一份空列表是合法的、能覆盖用），但这是界面该拦的事，
    不在这里替用户做主。
    """
    target_path = Path(path)
    payload = {
        "app": "xdao-export",
        "schema": SCHEMA_VERSION,
        "kind": "watch-list",
        "exported_at": time.strftime(
            "%Y-%m-%d %H:%M:%S", time.localtime(stamp) if stamp is not None else time.localtime()
        ),
        "targets": [_target_record(t) for t in targets],
    }
    try:
        target_path.parent.mkdir(parents=True, exist_ok=True)
        # 手写一点缩进，用户改起来才看得下去；ensure_ascii=False 让中文网址可读。
        text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        target_path.write_text(text, encoding="utf-8")
    except OSError as exc:
        raise WatchListError(f"写不进去：{exc}") from exc
    return target_path


def read_targets(path: str | Path) -> list[WatchTarget]:
    """读一份列表文件并逐条转成 :class:`WatchTarget`（跳过坏条目）。

    文件本身的问题（不存在、太大、不是 JSON、不是这份工具的列表）抛
    :class:`WatchListError`；单条的问题只记录在返回值的 ``skipped`` 里。
    """
    return _read_targets(path)[0]


def _read_targets(path: str | Path) -> tuple[list[WatchTarget], list[tuple[str, str]], int]:
    source = Path(path)
    try:
        if source.stat().st_size > MAX_FILE_BYTES:
            raise WatchListError(
                f"文件太大了（超过 {MAX_FILE_BYTES // (1024 * 1024)} MiB），"
                "多半不是监控列表 —— 请确认选对了文件。"
            )
        text = source.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise WatchListError(f"找不到这个文件：{source}") from exc
    except OSError as exc:
        raise WatchListError(f"读不了这个文件：{exc}") from exc
    except UnicodeDecodeError as exc:
        raise WatchListError("这个文件不是 UTF-8 文本，认不出里面的内容。") from exc

    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise WatchListError(f"这个文件不是有效的 JSON（第 {exc.lineno} 行）：{exc.msg}") from exc

    if not isinstance(payload, dict):
        raise WatchListError("这个文件的顶层应该是一个 JSON 对象，不是列表或其它东西。")
    raw_targets = payload.get("targets")
    if not isinstance(raw_targets, list):
        raise WatchListError("这个文件里没有 targets 这一段，不像是本工具导出的监控列表。")
    schema = payload.get("schema")
    if schema is not None and schema != SCHEMA_VERSION:
        raise WatchListError(
            f"这份列表的格式版本是 {schema}，本版只认 {SCHEMA_VERSION}。"
            "请用导出它的那个版本导入，或升级到更新的版本。"
        )

    good: list[WatchTarget] = []
    bad: list[tuple[str, str]] = []
    for entry in raw_targets:
        try:
            target = target_from_entry(entry)
        except WatchListError as exc:
            bad.append((str(exc), _describe_target_entry(entry)))
            continue
        good.append(target)
    return good, bad, len(raw_targets)


def _describe_target_entry(entry: object) -> str:
    """坏条目也要能指出来是哪一条，所以从原始记录里抠一个说法。"""
    if isinstance(entry, dict):
        for key in ("url_or_id", "thread_id"):
            value = _normalize_text(entry.get(key))
            if value:
                return value
    return "(认不出的一条)"


def import_targets(
    existing: list[WatchTarget],
    path: str | Path,
) -> ImportResult:
    """把文件里的监控条目并进 ``existing``（**合并**，不覆盖已有的）。

    重复的判据是 :func:`~xdao.watcher.target_key` —— 同一个串按不同格式/范围
    各监控一份是允许的，所以「去重」必须按监控键比，不能只看串号。
    """
    parsed, bad, total = _read_targets(path)
    result = ImportResult(total=total)
    seen = {target_key(t) for t in existing}
    for target in parsed:
        key = target_key(target)
        if key in seen:
            # 「已经有了」分两种：既有列表里的（和文件里这条完全一样）和文件内部重复的。
            # 前者在「替换」时必须留下，所以单独记一份；这里不细分，replace 模式下
            # 万一多留一条，也比把用户的列表清空强。
            result.duplicates.append(target)
            result.skipped.append(("这份列表里已经有了（同一个串、同样的设置）", _describe_target(target)))
            continue
        seen.add(key)
        result.added.append(target)
    result.skipped.extend(bad)
    return result
