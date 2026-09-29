"""串监控：定时检查指定串是否有新回复，有更新就自动导出。

监控刻意做成「与界面无关」的一层：

- :class:`WatchTarget` 描述一个被监控的串（导出范围、格式、饼干筛选）；
- :func:`check_once` 检查一轮并返回每个串的变化情况，便于单独调用和测试；
- :func:`watch_forever` 在后台线程里循环调用 :func:`check_once`。

检测「有没有更新」依赖 :class:`~xdao.cache.CachedThreadFetcher`：
当接口报出的回复数与缓存一致时无需翻页，因此每轮轮询只花一次请求。
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from .cache import CachedThreadFetcher, default_cache_dir, parse_thread_id
from .client import XdaoClient, XdaoError
from .exporters import EXPORTERS, ThreadData, create_exporter


@dataclass
class WatchTarget:
    """一个被监控的串。"""

    url_or_id: str
    scope: str = "all"
    format_key: str = "html"
    include_hashes: list[str] = field(default_factory=list)
    image_mode: str = "embed"
    state: str = "unknown"
    last_check: float = 0.0
    last_error: str = ""
    last_export_path: str = ""
    exports: int = 0
    thread_id: int | None = None

    def __post_init__(self) -> None:
        if self.thread_id is None:
            self.thread_id = parse_thread_id(self.url_or_id)

    @property
    def label(self) -> str:
        return str(self.thread_id or self.url_or_id)

    def to_dict(self) -> dict:
        return {
            "url_or_id": self.url_or_id,
            "scope": self.scope,
            "format_key": self.format_key,
            "include_hashes": list(self.include_hashes),
            "image_mode": self.image_mode,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "WatchTarget":
        image_mode = str(data.get("image_mode") or "embed")
        return cls(
            url_or_id=str(data.get("url_or_id") or ""),
            scope=str(data.get("scope") or "all"),
            format_key=str(data.get("format_key") or "html"),
            include_hashes=[str(h) for h in (data.get("include_hashes") or [])],
            image_mode=image_mode if image_mode in ("embed", "url", "drop") else "embed",
        )


@dataclass
class WatchResult:
    """一轮检查中单个串的结果。"""

    target: WatchTarget
    changed: bool = False
    new_posts: int = 0
    edited_posts: int = 0
    total_posts: int = 0
    exported: Path | None = None
    first_run: bool = False
    reason: str = ""
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error


def target_key(target: WatchTarget) -> str:
    """监控条目的唯一键。

    同一个串可以按不同格式/范围各监控一份，因此键里必须带上这些设置，
    否则第二种格式会因为"缓存里已是最新"而被永久跳过。
    """
    hashes = "-".join(sorted(target.include_hashes)) or "-"
    return f"{target.thread_id}-{target.scope}-{target.format_key}-{target.image_mode}-{hashes}"


def _export_state_path(cache_dir: Path, target: WatchTarget) -> Path:
    return Path(cache_dir) / "watch" / f"{target_key(target)}.json"


def load_export_state(cache_dir: Path, target: WatchTarget) -> dict:
    """读取这个监控条目上次导出时的状态。"""
    try:
        data = json.loads(_export_state_path(cache_dir, target).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_export_state(cache_dir: Path, target: WatchTarget, posts: int, exports: int) -> None:
    path = _export_state_path(cache_dir, target)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "thread_id": target.thread_id,
                    "scope": target.scope,
                    "format_key": target.format_key,
                    "include_hashes": list(target.include_hashes),
                    "posts": posts,
                    "exports": exports,
                    "exported_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    except OSError:
        pass  # 记录写入失败不影响本次导出


def check_once(
    client: XdaoClient,
    target: WatchTarget,
    output_dir: Path,
    cache_dir: Path | None = None,
    progress=None,
    verify_cached: bool = False,
) -> WatchResult:
    """检查一个串并按需导出（有更新才写文件）。

    verify_cached=True 时会额外校验老楼层是否被编辑过（多花一次请求）。
    """
    result = WatchResult(target=target)
    cache_dir = Path(cache_dir) if cache_dir else default_cache_dir()
    try:
        fetcher = CachedThreadFetcher(
            client,
            cache_dir=cache_dir,
            progress=progress,
        )
        fetched = fetcher.fetch(target.url_or_id, verify_cached=verify_cached)
        thread: ThreadData = ThreadData(
            thread_id=fetched.meta.thread_id,
            title=fetched.meta.title,
            po_hash=fetched.meta.po_hash,
            posts=list(fetched.posts),
        )
        result.total_posts = len(thread.posts)
        result.new_posts = fetched.new_posts
        result.edited_posts = fetched.edited_posts
        result.reason = fetched.reason

        scope_posts = thread.filter_posts(target.scope, target.include_hashes)
        # 没有任何楼层可导出时说明筛选条件不匹配，属于用户配置问题。
        if not scope_posts:
            result.error = "筛选范围内没有楼层，请检查饼干或抓取范围"
            target.last_error = result.error
            target.last_check = time.time()
            target.state = "无内容"
            return result

        # 是否要重新导出，由这个监控条目自己的记录决定：
        # 抓取层报"无变化"不代表这份文件已经生成过（例如同一个串同时按
        # HTML 与 TXT 监控，第二次抓取会命中缓存，但 txt 文件还没写过）。
        state = load_export_state(cache_dir, target)
        last_posts = int(state.get("posts") or 0)
        target.exports = int(state.get("exports") or target.exports or 0)
        needs_export = (
            last_posts != len(thread.posts)
            or fetched.changed
            or not target.last_export_path
        )

        if not needs_export:
            target.last_check = time.time()
            target.last_error = ""
            target.state = f"无更新（{result.total_posts} 楼）"
            return result

        exporter = create_exporter(
            target.format_key,
            client,
            progress=progress,
            image_mode=target.image_mode,
        )
        path = exporter.save(
            thread,
            target.scope,
            Path(output_dir),
            include_hashes=target.include_hashes,
        )
        result.exported = path
        result.changed = True
        result.first_run = last_posts == 0
        target.exports += 1
        target.last_export_path = str(path)
        save_export_state(cache_dir, target, len(thread.posts), target.exports)
        target.last_check = time.time()
        target.last_error = ""
        target.state = (
            f"已导出 {len(scope_posts)} 楼"
            if result.first_run
            else f"新增 {result.new_posts} 楼，已导出 {len(scope_posts)} 楼"
        )
        return result
    except XdaoError as exc:
        result.error = str(exc)
    except Exception as exc:  # 监控线程不能被单个串的异常打断
        result.error = f"{type(exc).__name__}: {exc}"
    target.last_check = time.time()
    target.last_error = result.error
    target.state = "出错"
    return result


def watch_forever(
    client: XdaoClient,
    targets: list[WatchTarget],
    output_dir: Path,
    interval: float,
    stop_event: threading.Event | None = None,
    cache_dir: Path | None = None,
    on_result=None,
    verify_cached: bool = False,
) -> None:
    """后台循环：每 interval 秒检查一轮，直到 stop_event 被设置。

    每轮的顺序是「先检查、后等待」，所以启动后马上就会出一次结果。
    """
    interval = max(15.0, float(interval or 60))
    while not (stop_event and stop_event.is_set()):
        for target in list(targets):
            if stop_event and stop_event.is_set():
                return
            result = check_once(
                client,
                target,
                output_dir,
                cache_dir=cache_dir,
                verify_cached=verify_cached,
            )
            if on_result:
                try:
                    on_result(result)
                except Exception:
                    pass  # 回调异常不该终止监控
        # 用 wait 而不是 sleep，这样停止指令能立刻生效。
        if stop_event:
            if stop_event.wait(interval):
                return
        else:
            time.sleep(interval)


def describe_targets(targets: list[WatchTarget]) -> str:
    """给界面用的一行摘要。"""
    if not targets:
        return "未添加监控串"
    parts = []
    for target in targets:
        _, ext, _ = EXPORTERS.get(target.format_key, EXPORTERS["html"])
        parts.append(f"No.{target.label}{ext}")
    return f"监控 {len(targets)} 个串：" + "、".join(parts)
