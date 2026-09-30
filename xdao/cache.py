"""串的本地缓存：页面级断点续传 + 增量更新。

设计要点：

- 每个串一个状态文件 ``<缓存目录>/threads/<串号>.json``，记录已抓到的页数、
  已知回复数、最后一页的指纹和各页帖子的简短指纹（用于发现被编辑的楼层）。
- 抓取时先读缓存：页码在已知范围内就直接复用，只向服务器请求新增的页，
  长串第二次导出几乎不再翻页。
- 服务器上的回复数没有变化时，一次请求即可判定「无更新」，这是监控功能的基础；
  若需要确认老楼层是否被编辑，可用 ``verify_cached=True`` 做指纹校验
  （不额外增加请求，因为最终页本来就要重新抓一次）。
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from .client import Post, XdaoClient, XdaoError
from .exporters._shared import OutputDirNotWritable, can_write_dir, ensure_writable

# 缓存格式版本，结构不兼容时旧缓存自动失效。
CACHE_VERSION = 1
DEFAULT_CACHE_DIRNAME = ".cache"


def post_fingerprint(post: Post) -> str:
    """一条发言的内容指纹，用来判断楼层是否被编辑过。"""
    raw = "|".join(
        [
            str(post.id),
            post.user_hash or "",
            post.name or "",
            post.content or "",
            post.img or "",
            post.ext or "",
            post.now or "",
            str(post.admin),
        ]
    )
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


@dataclass
class ThreadMeta:
    """从接口主页拿到、不含正文的元信息，用于判断是否有更新。"""

    thread_id: int
    title: str
    po_hash: str
    reply_count: int
    page: int = 1
    pages: int = 1


@dataclass
class CachedThread:
    """缓存命中时的返回结果。"""

    meta: ThreadMeta
    posts: list[Post]
    from_cache: bool = True
    changed: bool = False
    reason: str = ""
    pages_fetched: int = 0
    pages_reused: int = 0
    new_posts: int = 0
    edited_posts: int = 0
    # 缓存不可写时的原因；非空说明这次抓取没有留下断点续传的成果。
    cache_warning: str = ""
    # 补抓之后依然没拿到内容的页号（升序）。非空表示这份产物是缺页的，
    # 调用方必须显式告诉用户，不能让缺页悄悄溜过去。
    failed_pages: list[int] = field(default_factory=list)
    # 补抓过程/缺页的一句话说明，供日志与界面显示；空串表示这次一次失败都没有。
    retry_note: str = ""
    # 产物是否明确不完整（缺页，或撞上页数上限没抓完）。界面据此给不同的提示。
    truncated: bool = False

    def fingerprint_pairs(self) -> list[tuple[int, str, str]]:
        """(页号, 楼层 id, 指纹) 三元组，用于回写缓存。"""
        pairs: list[tuple[int, str, str]] = []
        for post in self.posts:
            page = int(getattr(post, "_page", 1) or 1)
            pairs.append((page, str(post.id), post_fingerprint(post)))
        return pairs


@dataclass
class ThreadCache:
    """某个串的缓存状态（内存中的一份副本，可读写磁盘）。"""

    cache_dir: Path
    thread_id: int
    reply_count: int = 0
    pages: int = 0
    last_page_hash: str = ""
    last_fetch_at: float = 0.0
    last_page_count: int = 0
    fingerprints: dict[str, str] = field(default_factory=dict)
    page_files: list[str] = field(default_factory=list)
    # 写缓存失败时记录原因，供界面提示；空表示一切正常。
    write_error: str = ""
    # 连缓存目录本身都没能建出来（例如目录权限只放开到上一级）。
    fetch_error: str = ""

    @property
    def path(self) -> Path:
        return self.cache_dir / "threads" / f"{self.thread_id}.json"

    @property
    def pages_dir(self) -> Path:
        return self.cache_dir / "pages" / str(self.thread_id)

    def page_path(self, page: int) -> Path:
        return self.pages_dir / f"{page}.json"

    # ---------- 读写 ----------

    def known_post_ids(self) -> set[str]:
        """缓存里已经见过的楼层 id 集合。"""
        return set(self.fingerprints)

    def load_page(self, page: int) -> dict | None:
        """读取某页的原始接口数据；不存在或损坏返回 None。"""
        path = self.page_path(page)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def contiguous_pages(self) -> int:
        """从第 1 页开始**连续**存在的页数。

        必须是"连续"：某一页抓失败时它两侧的页可能都已经落盘了，
        若只数"有没有页面文件"就会以为中间不缺页 —— 那样下次抓取会
        误判成"缓存完整"而少抓一截（实测过：第 4 页失败后紧接着的
        下一次导出会直接复用缺页的缓存）。
        """
        page = 1
        while self.load_page(page) is not None:
            page += 1
        return page - 1

    def store_page(self, page: int, payload: dict) -> None:
        try:
            self.pages_dir.mkdir(parents=True, exist_ok=True)
            self.page_path(page).write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8"
            )
        except (OSError, TypeError) as exc:
            # 缓存写不进去不该中断导出，但必须让上层知道 ——
            # 否则用户会以为抓取成果已留存，下次重跑还得再抓一遍。
            detail = f"{type(exc).__name__}: {exc}"
            if not self.write_error:
                self.write_error = detail
            # 目录这一级就失败（而不是文件写失败）单独记一笔：
            # 「缓存目录本身用不了」和「某个文件写不动」给用户的建议不一样。
            if not self.fetch_error and isinstance(exc, PermissionError):
                self.fetch_error = detail

    @staticmethod
    def hash_payload(payload: dict) -> str:
        """整页指纹，用来判断服务器上的这一页是否变化。"""
        try:
            raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError):
            raw = str(payload)
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]

    @classmethod
    def load(cls, cache_dir: Path, thread_id: int) -> "ThreadCache":
        cache = cls(cache_dir=Path(cache_dir), thread_id=thread_id)
        try:
            data = json.loads(cache.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return cache
        if not isinstance(data, dict) or data.get("version") != CACHE_VERSION:
            return cache
        try:
            cache.reply_count = int(data.get("reply_count") or 0)
            cache.pages = int(data.get("pages") or 0)
            cache.last_page_count = int(data.get("last_page_count") or 0)
        except (TypeError, ValueError):
            pass
        cache.last_page_hash = str(data.get("last_page_hash") or "")
        try:
            cache.last_fetch_at = float(data.get("last_fetch_at") or 0.0)
        except (TypeError, ValueError):
            cache.last_fetch_at = 0.0
        raw_fp = data.get("fingerprints")
        if isinstance(raw_fp, dict):
            cache.fingerprints = {str(k): str(v) for k, v in raw_fp.items()}
        return cache

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "version": CACHE_VERSION,
                "thread_id": self.thread_id,
                "reply_count": self.reply_count,
                "pages": self.pages,
                "last_page_hash": self.last_page_hash,
                "last_page_count": self.last_page_count,
                "last_fetch_at": self.last_fetch_at,
                "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "fingerprints": self.fingerprints,
            }
            self.path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError as exc:
            if not self.write_error:
                self.write_error = f"{type(exc).__name__}: {exc}"

    def clear(self) -> None:
        """清空该串的缓存（页面文件一并删除）。"""
        for path in self.pages_dir.glob("*.json"):
            try:
                path.unlink()
            except OSError:
                pass
        try:
            self.path.unlink()
        except OSError:
            pass
        self.reply_count = 0
        self.pages = 0
        self.fingerprints.clear()
        self.last_page_hash = ""

    def describe(self) -> str:
        """给界面看的一行缓存说明。"""
        if not self.pages:
            return "无缓存"
        when = (
            time.strftime("%Y-%m-%d %H:%M", time.localtime(self.last_fetch_at))
            if self.last_fetch_at
            else "未知时间"
        )
        return f"已缓存 {self.pages} 页 · {self.reply_count} 条回复 · {when}"


def parse_reply_count(payload: dict) -> int:
    """从接口返回里稳妥地取出回复数。"""
    try:
        return max(0, int(payload.get("ReplyCount") or 0))
    except (TypeError, ValueError):
        return 0


def default_cache_dir(base: Path | None = None) -> Path:
    """默认缓存目录：优先放用户配置目录，避免污染导出目录。"""
    import os

    if base is not None:
        return Path(base) / DEFAULT_CACHE_DIRNAME
    appdata = os.environ.get("APPDATA")
    root = Path(appdata) if appdata else Path.home() / ".config"
    return root / "xdao-export" / DEFAULT_CACHE_DIRNAME


def cache_dir_candidates(preferred: Path | str | None = None) -> list[Path]:
    """缓存目录的候选位置，按优先级排列。

    指定的位置永远排第一；后面是「用户配置目录 → 本地配置目录 → 系统临时目录」，
    用来在首选位置写不进去时顶上（只读的导出目录、受限的桌面环境等）。

    缓存只是加速手段，不该因为它写不进去就让整次导出失败 —— 换一个能写的地方继续，
    并把换了地方这件事告诉用户。
    """
    import os
    import tempfile

    candidates: list[Path] = []
    if preferred:
        candidates.append(Path(preferred))
    else:
        candidates.append(default_cache_dir())

    def add(path: Path) -> None:
        if path not in candidates:
            candidates.append(path)

    for env_name in ("LOCALAPPDATA", "APPDATA"):
        value = os.environ.get(env_name)
        if value:
            add(Path(value) / "xdao-export" / DEFAULT_CACHE_DIRNAME)
    # 系统临时目录只是最后的备选；问不到就少一个候选，不耽误导出。
    # 打包版实测过它会在冻结环境里抛 FileNotFoundError（"No usable temporary
    # directory found in [...]"，v0.10.0 真机核验时撞上），而缓存目录的探测本来
    # 就允许「这一处写不进去就换下一处」——所以这里必须自己兜住，否则整次导出
    # 会跟着一起失败。
    try:
        add(Path(tempfile.gettempdir()) / "xdao-export" / DEFAULT_CACHE_DIRNAME)
    except Exception:
        pass
    return candidates


def resolve_cache_dir(preferred: Path | str | None = None) -> tuple[Path, str]:
    """挑一个真能写的缓存目录，返回 (目录, 备注)。

    首选能写就原样用它、备注为空；否则按候选顺序找第一个能写的，备注里说明
    换了地方、原来的为什么不能用。全都写不进去时返回首选位置和空备注，
    由调用方按原逻辑报错。
    """
    options = cache_dir_candidates(preferred)
    first = options[0]
    if can_write_dir(first):
        return first, ""
    for option in options[1:]:
        if can_write_dir(option):
            return option, (
                f"缓存目录 {first} 写不进去，本次改用 {option}。"
                "想固定下来可以在设置里改「缓存目录」。"
            )
    return first, ""


def probe_cache_dir(preferred: Path | str | None = None):
    """resolve_cache_dir 的薄包装，交给调用方自行处理异常。"""
    try:
        return resolve_cache_dir(preferred)
    except OSError:
        return Path(preferred) if preferred else default_cache_dir(), ""


_THREAD_ID_RE = re.compile(r"(?:^|/t/|/thread/|/id/)(\d{3,})")


def parse_thread_id(value: str | int | None) -> int | None:
    """从网址、纯数字字符串或整数中解析串号。"""
    if isinstance(value, bool):  # bool 是 int 的子类，单独挡掉
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    value = (value or "").strip()
    match = _THREAD_ID_RE.search(value)
    if match:
        return int(match.group(1))
    if value.isdigit():
        return int(value)
    return None


class CachedThreadFetcher:
    """带缓存的串抓取器。"""

    #: 某一页彻底抓不到时，隔多久重试（秒）。取一下就能连上的抖动，
    #: 靠这个等待躲过去；太长的等待会让用户以为程序卡死。
    RETRY_DELAY = 2.0
    #: 每一页最多补抓几轮（0 = 失败就停，即 v0.5.3 的行为）。
    RETRY_ATTEMPTS = 2

    def __init__(
        self,
        client: XdaoClient,
        cache_dir: Path | None = None,
        progress=None,
        use_cache: bool = True,
        retry_attempts: int | None = None,
        retry_delay: float | None = None,
    ) -> None:
        self._client = client
        self._progress = progress
        self.use_cache = use_cache
        # 补抓次数/间隔可在构造时覆盖（测试里调成 0 或 0 秒，免得白等）。
        self.retry_attempts = (
            self.RETRY_ATTEMPTS if retry_attempts is None else max(0, int(retry_attempts))
        )
        self.retry_delay = self.RETRY_DELAY if retry_delay is None else max(0.0, float(retry_delay))
        # 最近一次页面抓取失败的原因（str 类接口错误 / 接口报错 / 非预期内容），
        # 供补抓失败后的说明文字使用。
        self._last_page_error = ""
        # 补抓救回来的页数（只在最后汇总时报告一次）。
        self._retried_ok = 0
        requested = Path(cache_dir) if cache_dir else default_cache_dir()
        # 缓存写不进去不该让整次导出失败：挑一个能写的位置继续，并把换了地方这件事
        # 记下来，由调用方在界面/日志里说明（cache_note 最终会并进 CachedThread.cache_warning）。
        self.cache_note = ""
        if use_cache:
            self.cache_dir, self.cache_note = resolve_cache_dir(requested)
        else:
            self.cache_dir = requested

    def _notify(self, message: str) -> None:
        if self._progress:
            self._progress(message)

    # ---------- 元信息 ----------

    def fetch_meta(self, thread_id: int) -> ThreadMeta:
        """只取第 1 页，得到标题、PO 饼干和回复数（监控轮询用，代价最小）。"""
        payload = self._client.fetch_thread_page(thread_id, 1)
        self._raise_if_error(payload, thread_id)
        parsed = self._client.parse_thread_page(payload)
        return ThreadMeta(
            thread_id=thread_id,
            title=parsed["title"],
            po_hash=parsed["po_hash"] or "",
            reply_count=parse_reply_count(payload),
            page=1,
            pages=self._client.last_page_count or 1,
        )

    @staticmethod
    def _raise_if_error(payload, thread_id: int) -> None:
        if isinstance(payload, str):
            raise XdaoError(payload)
        if isinstance(payload, dict) and payload.get("success") is False:
            raise XdaoError(str(payload.get("error") or f"无法访问串 {thread_id}"))

    # ---------- 抓取 ----------

    def fetch(
        self,
        url_or_id: str,
        *,
        max_pages: int = 5000,
        verify_cached: bool = False,
    ) -> CachedThread:
        """抓取（或从缓存取得）一个串的完整内容。

        verify_cached=True 时会重新抓取最终页并比对指纹，能发现老楼层被编辑；
        为 False 时在回复数没变的情况下一次请求都不多用。
        """
        thread_id = parse_thread_id(url_or_id)
        if thread_id is None:
            raise XdaoError(f"无法识别串号：{url_or_id}")

        # 抓一个长串可能要几分钟，靠的就是缓存能续上；所以先确认缓存目录可写，
        # 写不进去就当场停下说清楚，而不是让用户白抓一场。
        if self.use_cache:
            try:
                ensure_writable(self.cache_dir, kind="缓存")
            except OutputDirNotWritable as exc:
                raise XdaoError(
                    f"{exc}\n\n提示：不想用缓存可以关掉「使用本地缓存」，"
                    "或在设置里把缓存目录改到一个可写的位置。"
                ) from exc

        cache = ThreadCache.load(self.cache_dir, thread_id) if self.use_cache else ThreadCache(
            cache_dir=self.cache_dir, thread_id=thread_id
        )
        cached_pages: dict[int, dict] = {}
        if self.use_cache:
            for page in range(1, cache.pages + 1):
                payload = cache.load_page(page)
                if payload is None:
                    break
                cached_pages[page] = payload

        self._notify("正在获取第 1 页…")
        first = self._client.fetch_thread_page(thread_id, 1)
        self._raise_if_error(first, thread_id)
        if not isinstance(first, dict):
            raise XdaoError(f"接口返回了非预期内容：{type(first).__name__}")

        reply_count = parse_reply_count(first)
        page_count = max(1, self._client.last_page_count or 1)
        # 本轮之前的已知楼层：用于区分"新增楼层"与"被编辑的老楼层"。
        previous_ids = cache.known_post_ids()

        # ---- 判定是否需要抓取 ----
        cached_first = cached_pages.get(1)
        # `cache.pages` 的语义是"从第 1 页起、中间不缺页的那一段有多长"
        # （由 contiguous_pages() 维护，见 _assemble）。缓存能不能直接用，
        # 要同时满足两件事：那段长度和缓存里的页数对得上（天然成立，防脏数据），
        # 以及它已经盖到接口报的总页数。后者才是关键：
        #   * 盖满了、回复数也没变 → 一次请求都不用多花，直接用（默认行为）；
        #   * 首页接口没报总页数（page_count == 1）→ 跟以前一样只刷新第 1 页；
        #   * 只盖住前几页、串后面还有页（含"中间某页当时没抓下来、
        #     后面几页却已落盘"那种）→ 必须去补，不能在这儿提前返回，
        #     否则下一次导出会一直复用中间缺页的缓存（实测踩过这个坑）。
        cache_covers = (
            bool(cached_pages)
            and len(cached_pages) >= cache.pages
            and cache.pages >= page_count
        )
        if (
            self.use_cache
            and cached_first is not None
            and cache.reply_count == reply_count
            and (cache_covers or page_count <= 1)
        ):
            if not verify_cached:
                payloads = dict(cached_pages)
                payloads[1] = first
                return self._assemble(
                    thread_id,
                    payloads,
                    cache=cache,
                    reason="回复数未变化，直接使用本地缓存",
                    pages_reused=len(payloads),
                    previous_post_ids=previous_ids,
                )
            # 校验模式：重抓最终页，比对是否有楼层被编辑。
            if page_count in cached_pages and page_count > 1:
                self._notify(f"正在校验第 {page_count} 页是否有改动…")
                try:
                    fresh_last = self._client.fetch_thread_page(thread_id, page_count)
                    self._raise_if_error(fresh_last, thread_id)
                except XdaoError:
                    payloads = dict(cached_pages)
                    payloads[1] = first
                    return self._assemble(
                        thread_id,
                        payloads,
                        cache=cache,
                        reason="校验失败，沿用本地缓存",
                        pages_reused=len(payloads),
                        previous_post_ids=previous_ids,
                    )
                if isinstance(fresh_last, dict) and (
                    ThreadCache.hash_payload(fresh_last)
                    == ThreadCache.hash_payload(cached_pages[page_count])
                ):
                    payloads = dict(cached_pages)
                    payloads[1] = first
                    return self._assemble(
                        thread_id,
                        payloads,
                        cache=cache,
                        reason="校验通过，内容与缓存一致",
                        pages_reused=len(payloads),
                        previous_post_ids=previous_ids,
                    )
                cached_pages[page_count] = fresh_last
                payloads = dict(cached_pages)
                payloads[1] = first
                return self._assemble(
                    thread_id,
                    payloads,
                    cache=cache,
                    reason="检测到楼层变化，已更新",
                    pages_reused=len(payloads),
                    previous_post_ids=previous_ids,
                )
            payloads = dict(cached_pages)
            payloads[1] = first
            return self._assemble(
                thread_id,
                payloads,
                cache=cache,
                reason="仅一页内容，已用最新数据刷新",
                pages_reused=len(payloads),
                previous_post_ids=previous_ids,
            )

        # ---- 需要向服务器补页 ----
        payloads: dict[int, dict] = {}
        reused = 0
        fetched = 0
        pages_to_fetch: list[int] = []
        # 第 1 页由这一轮的首页请求提供，且必须写进缓存，
        # 否则缓存永远缺第 1 页，后续每次都判定为"缓存不完整"而全量重抓。
        for page in range(2, page_count + 1):
            if page in cached_pages:
                payloads[page] = cached_pages[page]
                reused += 1
            else:
                pages_to_fetch.append(page)

        payloads[1] = first
        if self.use_cache:
            cache.store_page(1, first)

        # 抓不下来的页先记进"重试队列"，不再像以前那样当场 break 把后面
        # 所有页一起丢掉。原因：接口偶发超时/限流/返回空壳时，旧行为会静默
        # 少抓一大截，用户拿到的产物缺页却看不出来。
        failed: set[int] = set()
        attempted = 0
        capped = False
        for index, page in enumerate(pages_to_fetch, start=1):
            if len(payloads) >= page_count:
                break
            if index > max_pages:
                # 撞上页数上限，剩下的页一个都没抓 —— 产物必然不完整。
                capped = True
                break
            attempted = page
            status = self._process_page(
                thread_id, page, payloads, cache=cache, failed=failed
            )
            if status == "ok":
                fetched += 1
            elif status == "fail":
                # 这一页这次没成功；先跳过它，让后面还没试过的页先抓
                # （排队的代价最小），等第一遍跑完再统一补抓。
                continue
            else:  # end：接口说到头了，重试没有意义
                break

        if failed and self.retry_attempts:
            retried, retry_reach = self._retry_failed_pages(
                thread_id, page_count, payloads, cache=cache, failed=failed
            )
            fetched += retried
            attempted = max(attempted, retry_reach)

        # 只在"确实试过的页"里统计缺页：报出来的每一页都得是真抓过又失败的，
        # 否则「第 12 页没抓到」这种话会冤枉一堆根本没请求过的页。
        # pages_to_fetch 是自第 2 页起的连续区间，失败页是跳过而不是终止循环，
        # 所以 attempted 一定覆盖 [2, attempted]，统计不会有漏。
        attempts = len(pages_to_fetch)
        missing = sorted(page for page in range(1, attempted + 1) if page not in payloads)
        if attempts >= 3 and len(failed) >= max(2, int(attempts * 0.5)):
            # 试过的页里有一半以上都失败：多半是网络断了/被限流，不是单页偶发。
            # 只报"抓过又失败的那几页"会让用户以为只差一两页，其实后面全没试过；
            # 把收尾点之后的页也算进缺页里（收尾点 ≤ 最大失败页 + 失败页数，
            # 因为失败页是流水账、跳过的页之间最多隔着"失败页个数"页）。
            stop_at = max(missing) + len(failed)
            failed.update(
                page
                for page in range(1, min(page_count, stop_at) + 1)
                if page not in payloads
            )
            missing = sorted(failed)

        reason = (
            "首次抓取"
            if reused == 0
            else f"增量更新：新下载 {fetched} 页，复用缓存 {reused} 页"
        )
        return self._assemble(
            thread_id,
            payloads,
            cache=cache,
            reason=reason,
            pages_reused=reused,
            pages_fetched=fetched,
            previous_reply_count=cache.reply_count,
            previous_post_ids=previous_ids,
            failed_pages=failed,
            page_count=page_count,
            capped=capped,
        )

    def _process_page(
        self,
        thread_id: int,
        page: int,
        payloads: dict[int, dict],
        *,
        cache: ThreadCache,
        failed: set[int],
    ) -> str:
        """抓一页并解析，返回 ``"ok"`` / ``"fail"`` / ``"end"``。

        这里**不再直接 break**：失败（``"fail"``）只是把页号丢进重试队列。
        真正"到末尾了"的信号（第 2 页起只有主帖、解析后没有回复）返回
        ``"end"`` —— 那不是错误，重试也没有意义。
        """
        self._last_page_error = ""
        self._notify(f"正在抓取第 {page} 页…")
        raw = self._client.fetch_thread_page(thread_id, page)
        if isinstance(raw, str):
            failed.add(page)
            self._last_page_error = raw
            return "fail"
        if isinstance(raw, dict) and raw.get("success") is False:
            failed.add(page)
            self._last_page_error = str(raw.get("error") or "接口返回失败")
            return "fail"
        if not isinstance(raw, dict):
            failed.add(page)
            self._last_page_error = f"接口返回了非预期内容：{type(raw).__name__}"
            return "fail"
        parsed = self._client.parse_thread_page(raw, page)
        if page > 1 and not parsed["posts"]:
            # 空页：接口说这页什么都没有，当作到底。
            return "end"
        payloads[page] = raw
        if self.use_cache:
            cache.store_page(page, raw)
        failed.discard(page)
        if page > 1 and len(parsed["posts"]) <= 1:
            # 只有主帖、没有回复，也说明后面没内容了。
            return "end"
        return "ok"

    def _retry_failed_pages(
        self,
        thread_id: int,
        page_count: int,
        payloads: dict[int, dict],
        *,
        cache: ThreadCache,
        failed: set[int],
    ) -> tuple[int, int]:
        """补抓失败页；返回 ``(补上的页数, 补抓时请求到的最大页号)``。

        每一轮只等一次 ``retry_delay``：服务器忙时连着敲反而更糟。
        最后一轮仍失败的页留在 ``failed`` 里，由 :meth:`_assemble` 报到
        界面/日志上 —— 缺页这件事必须让用户看见。
        """
        fetched = 0
        reach = 0
        for round_no in range(1, self.retry_attempts + 1):
            pending = sorted(page for page in failed if page <= page_count)
            if not pending:
                break
            waiting = "、".join(str(page) for page in pending)
            self._notify(
                f"有 {len(pending)} 页没抓下来（第 {waiting} 页），"
                f"第 {round_no}/{self.retry_attempts} 次补抓…"
            )
            if self.retry_delay:
                time.sleep(self.retry_delay)
            for page in pending:
                reach = max(reach, page)
                if self._process_page(
                    thread_id, page, payloads, cache=cache, failed=failed
                ) == "ok":
                    fetched += 1
                    self._retried_ok += 1
        return fetched, reach

    # ---------- 组装 ----------

    def _assemble(
        self,
        thread_id: int,
        payloads: dict[int, dict],
        *,
        cache: ThreadCache,
        reason: str,
        pages_reused: int = 0,
        pages_fetched: int = 0,
        previous_reply_count: int | None = None,
        previous_post_ids: set[str] | None = None,
        failed_pages: set[int] | None = None,
        page_count: int = 0,
        capped: bool = False,
    ) -> CachedThread:
        """把若干页原始数据合并成完整的串内容，并回写缓存状态。

        ``previous_post_ids`` 是本轮抓取之前缓存里已有的楼层 id：
        只有"上次就有、这次指纹变了"才算被编辑，否则一律算新增楼层。
        ``failed_pages`` 是补抓之后仍然缺的页号，会原样带进结果里。
        ``page_count`` 是接口自称的总页数，``capped`` 表示撞上了页数上限
        没抓完 —— 两者一起决定 ``truncated``（产物是否明确不完整）。
        """
        posts: list[Post] = []
        seen: set[int] = set()
        title = ""
        po_hash = ""
        reply_count = 0
        last_page = max(payloads) if payloads else 1

        previous_ids = set(previous_post_ids or ())
        old_fingerprints = dict(cache.fingerprints)

        # 只在解析一次的同时统计新增/被编辑的楼层。
        new_posts = 0
        edited_posts = 0
        seen_ids: set[str] = set()
        parsed_pages: dict[int, list[Post]] = {}

        for page in sorted(payloads):
            payload = payloads[page]
            parsed = self._client.parse_thread_page(payload, page)
            parsed_pages[page] = parsed["posts"]
            if page == 1:
                title = parsed["title"]
                po_hash = parsed["po_hash"] or ""
                reply_count = parse_reply_count(payload)
            for post in parsed["posts"]:
                key = str(post.id)
                if key in seen_ids:
                    continue  # 主帖会在多页出现，只统计一次
                seen_ids.add(key)
                if key in previous_ids:
                    if old_fingerprints.get(key) != post_fingerprint(post):
                        edited_posts += 1
                else:
                    new_posts += 1
                if post.id in seen:
                    continue
                seen.add(post.id)
                # 记录来源页，供缓存指纹使用。
                post.page = page
                posts.append(post)

        meta = ThreadMeta(
            thread_id=thread_id,
            title=title,
            po_hash=po_hash,
            reply_count=reply_count,
            page=1,
            pages=max(1, last_page),
        )
        changed = bool(pages_fetched) or edited_posts > 0

        if previous_reply_count is not None and reply_count > previous_reply_count:
            changed = True

        if self.use_cache:
            cache.reply_count = reply_count
            # 只把"从第 1 页起连续存在"的页数记成已抓范围：某一页失败时它后面
            # 的页可能已经落盘了，若直接记 max(page)，下一页抓取会误判成
            # "缓存完整"而复用中间缺页的缓存（实测踩过：第 4 页失败后，
            # 下一次导出直接复用了缺页的缓存，用户拿到的产物还是缺的）。
            cache.pages = max(cache.pages, cache.contiguous_pages())
            cache.last_page_hash = ThreadCache.hash_payload(payloads[last_page])
            cache.last_page_count = last_page
            cache.last_fetch_at = time.time()
            for page in sorted(parsed_pages):
                for post in parsed_pages[page]:
                    cache.fingerprints[str(post.id)] = post_fingerprint(post)
            cache.save()

        if cache.fetch_error:
            # 缓存目录这一级就用不了：说清楚「这次没留下断点续传的成果」，
            # 并给出下一步（换缓存目录），别只说一句 PermissionError。
            cache.write_error = (
                f"{cache.fetch_error} —— 缓存目录 {self.cache_dir} 写不进去，"
                "这次抓取不会留下断点续传的成果；可以在「设置 → 缓存」里"
                "换一个能写的缓存目录（默认位置一般可用）。"
            )

        # 「换了缓存目录」这类说明要一直带着走；真正的写入失败优先显示。
        warning = cache.write_error or self.cache_note

        missing = sorted(int(page) for page in (failed_pages or ()))
        last_page = max(payloads) if payloads else 0
        # 产物不完整有两种来源：补抓之后仍缺页，或者撞上页数上限没抓完。
        truncated = bool(missing) or capped
        retry_note = ""
        if missing:
            shown = "、".join(str(page) for page in missing[:8])
            more = f" 等 {len(missing)} 页" if len(missing) > 8 else ""
            reason_text = f"（最近一次失败原因：{self._last_page_error}）" if self._last_page_error else ""
            # 说明"整份产物只到第几页"：缺页后面的页会整段消失，只说"少了第 3 页"
            # 会低估损失（用户实测过：以为只差一页，其实后面半篇都没了）。
            stopped = f"这份产物只到第 {last_page} 页" if last_page else "这份产物没有抓到内容"
            retry_note = (
                f"第 {shown}{more} 页补抓 {self.retry_attempts} 次仍未成功{reason_text}："
                f"{stopped}，缺页后面的内容不会出现在成品里。"
                "稍后请再导一次，通常就能补齐"
                "（已经抓下来的页都存进缓存了，重跑只会补缺的那些页）。"
            )
        elif capped:
            retry_note = (
                f"这次只抓到第 {last_page} 页（串比这长），剩下的页没有抓："
                "可以用「抓取范围 → 指定页码」分批导出，或把页数上限调大后重跑。"
            )
        elif getattr(self, "_retried_ok", 0):
            retry_note = f"有 {self._retried_ok} 页第一次没抓下来，补抓成功。"

        return CachedThread(
            meta=meta,
            posts=posts,
            from_cache=pages_fetched == 0,
            changed=changed,
            reason=reason,
            pages_fetched=pages_fetched,
            pages_reused=pages_reused,
            new_posts=new_posts,
            edited_posts=edited_posts,
            cache_warning=warning,
            failed_pages=missing,
            retry_note=retry_note,
            truncated=truncated,
        )
