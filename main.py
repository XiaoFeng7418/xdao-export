"""程序入口。

三种用法：

1. 图形界面（不带参数）::

       python main.py

2. 命令行批量导出（适合脚本、计划任务）::

       python main.py 7001 https://www.nmbxd1.com/t/7002 -f markdown -o D:\\备份
       python main.py 7001 --scope po --hashes abc123,def456
       python main.py 7001 --no-cache          # 忽略本地缓存，完整重抓

3. 命令行监控（长时间跑，有新回复就自动导出）::

       python main.py 7001 --watch --interval 600 -f html
       python main.py 7001 7002 --watch --interval 300 --verify

4. 连接自检::

       python main.py --selftest
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def selftest() -> int:
    from xdao.client import XdaoClient, XdaoError

    client = XdaoClient()
    print("1) 测试接口连接…")
    try:
        cdn = client.update_cdn_path()
        print("   OK, CDN =", cdn)
    except XdaoError as exc:
        print("   失败:", exc)
        return 1

    print("2) 测试取串接口（No.50000001 第 1 页）…")
    try:
        data = client.fetch_thread_page(50000001, 1)
        if isinstance(data, dict) and data.get("success") is False:
            print("   接口返回错误:", data.get("error"))
            return 1
        title = data.get("title") if isinstance(data, dict) else None
        print("   OK, 标题 =", title)
    except XdaoError as exc:
        print("   失败:", exc)
        return 1

    print("3) 检查各导出格式可导入…")
    try:
        from xdao.exporters import EXPORTERS

        print("   OK, 格式 =", "、".join(EXPORTERS))
    except Exception as exc:
        print("   失败:", exc)
        return 1

    print("自检完成。")
    return 0


def build_parser() -> argparse.ArgumentParser:
    from xdao import __version__

    parser = argparse.ArgumentParser(
        prog="xdao-export",
        description="X岛串导出工具：把串完整备份为 HTML / TXT / Markdown / EPUB。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  python main.py                    启动图形界面\n"
            "  python main.py 7001 -f markdown   命令行导出单个串\n"
            "  python main.py 7001 --watch       监控串，有新回复就导出\n"
        ),
    )
    parser.add_argument(
        "-V",
        "--version",
        action="version",
        version=f"X岛串导出工具 {__version__}",
    )
    parser.add_argument(
        "threads",
        nargs="*",
        metavar="串号或网址",
        help="要导出的串；留空则启动图形界面",
    )
    parser.add_argument(
        "-f",
        "--format",
        default=None,
        choices=["html", "txt", "markdown", "epub"],
        help="导出格式（默认 html）",
    )
    parser.add_argument("-o", "--output", default=None, metavar="目录", help="导出目录")
    parser.add_argument(
        "--scope",
        default=None,
        choices=["all", "po"],
        help="抓取范围：all 全部发言，po 仅发串人",
    )
    parser.add_argument(
        "--hashes",
        default=None,
        metavar="饼干",
        help="只导出这些饼干，多个用逗号或空格分隔",
    )
    parser.add_argument(
        "--template",
        default=None,
        metavar="模板",
        help="文件名模板，占位符 {title} {id} {date} {po} {count}",
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="忽略本地缓存，完整重新抓取",
    )
    parser.add_argument(
        "--cache-dir",
        default=None,
        metavar="目录",
        help="缓存目录（默认放在导出目录下的 .cache）",
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help="校验老楼层是否被编辑（每轮多一次请求）",
    )
    parser.add_argument(
        "--image-mode",
        default=None,
        choices=["embed", "url", "drop"],
        help="EPUB 的图片处理：embed 内嵌（默认）、url 仅链接、drop 丢弃",
    )
    parser.add_argument(
        "--cookie",
        default=None,
        metavar="userhash",
        help="直接用饼干登录，不读取本机配置",
    )
    parser.add_argument(
        "--watch",
        action="store_true",
        help="进入监控模式：定时检查新回复并自动导出",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=None,
        metavar="秒",
        help="监控检查间隔（默认 300 秒，最小 15 秒）",
    )
    parser.add_argument(
        "--selftest",
        action="store_true",
        help="只做接口连接自检",
    )
    return parser


def _collect_hashes(raw: str | None) -> list[str]:
    if not raw:
        return []
    parts = [p.strip() for p in raw.replace(",", " ").split()]
    seen: set[str] = set()
    result: list[str] = []
    for part in parts:
        if part and part not in seen:
            seen.add(part)
            result.append(part)
    return result


def run_cli(args: argparse.Namespace) -> int:
    """命令行导出 / 监控。返回进程退出码。"""
    from xdao.cache import CachedThreadFetcher
    from xdao.client import XdaoClient, XdaoError
    from xdao.exporters import EXPORTERS, ThreadData, create_exporter
    from xdao.settings import AppSettings
    from xdao.watcher import WatchTarget, check_once, watch_forever

    settings = AppSettings.load()
    client = XdaoClient(
        timeout=settings.timeout,
        retries=settings.retries,
        proxy=settings.proxy or None,
        throttle=settings.throttle,
    )
    userhash = args.cookie or settings.userhash
    if userhash:
        client.set_userhash(userhash)
    else:
        print("提醒：未登录，只能读取每个串的前 100 页。用 --cookie 或先运行图形界面登录。", file=sys.stderr)

    output_dir = Path(args.output or settings.output_dir or (Path.home() / "Documents" / "X岛备份"))
    output_dir.mkdir(parents=True, exist_ok=True)
    format_key = args.format or settings.format_key or "html"
    scope = args.scope or settings.scope or "all"
    hashes = _collect_hashes(args.hashes) or settings.parse_hashes()
    template = args.template if args.template is not None else (settings.filename_template or None)
    use_cache = not args.no_cache and settings.use_cache
    cache_dir = Path(args.cache_dir) if args.cache_dir else settings.resolved_cache_dir(output_dir)
    if use_cache:
        client.image_cache_dir = cache_dir / "images"

    print(f"导出目录：{output_dir}")
    print(f"格式：{format_key}（{EXPORTERS[format_key][1]}）｜范围：{scope}"
          f"｜缓存：{'启用' if use_cache else '关闭'}｜缓存目录：{cache_dir}")
    if hashes:
        print(f"只看饼干：{'、'.join(hashes)}")

    if args.watch:
        interval = args.interval or settings.watch_interval
        image_mode = args.image_mode or settings.image_mode or "embed"
        targets = [
            WatchTarget(
                url,
                scope=scope,
                format_key=format_key,
                include_hashes=hashes,
                image_mode=image_mode,
            )
            for url in args.threads
        ]
        print(f"监控 {len(targets)} 个串，每 {int(max(15.0, interval))} 秒检查一次。按 Ctrl+C 退出。")

        def on_result(result) -> None:
            import time as _time

            stamp = _time.strftime("%H:%M:%S")
            if result.error:
                print(f"[{stamp}] No.{result.target.label} 出错：{result.error}")
            elif result.exported:
                print(f"[{stamp}] No.{result.target.label} → {result.exported.name}（{result.total_posts} 楼）")
            else:
                print(f"[{stamp}] No.{result.target.label} 无更新（{result.total_posts} 楼）")

        def progress(message: str) -> None:
            print(f"    {message}")

        try:
            # 先跑一轮并把抓取过程打出来，然后进入静默循环。
            for target in targets:
                check_once(client, target, output_dir, cache_dir=cache_dir,
                           progress=progress, verify_cached=args.verify)
            watch_forever(
                client, targets, output_dir, max(15.0, interval),
                cache_dir=cache_dir, on_result=on_result, verify_cached=args.verify,
            )
        except KeyboardInterrupt:
            print("\n已停止监控。")
        return 0

    fetcher = CachedThreadFetcher(client, cache_dir=cache_dir, progress=lambda m: print(f"    {m}"),
                                 use_cache=use_cache)
    exporter = create_exporter(
        format_key,
        client,
        filename_template=template,
        image_mode=args.image_mode or settings.image_mode or "embed",
    )

    succeeded = 0
    failed: list[str] = []
    for index, raw in enumerate(args.threads, start=1):
        print(f"[{index}/{len(args.threads)}] {raw}")
        try:
            result = fetcher.fetch(raw, verify_cached=args.verify)
            thread = ThreadData(
                thread_id=result.meta.thread_id,
                title=result.meta.title,
                po_hash=result.meta.po_hash,
                posts=list(result.posts),
            )
            print(f"    {len(thread.posts)} 楼 · {result.reason}")
            if getattr(result, "cache_warning", ""):
                print(
                    f"    警告：缓存写入失败，本次抓取不会被复用 —— {result.cache_warning}",
                    file=sys.stderr,
                )
            path = exporter.save(thread, scope, output_dir, include_hashes=hashes)
            succeeded += 1
            print(f"    完成：{path}")
        except XdaoError as exc:
            failed.append(raw)
            print(f"    失败：{exc}", file=sys.stderr)
        except Exception as exc:
            failed.append(raw)
            print(f"    失败：{type(exc).__name__}: {exc}", file=sys.stderr)

    print(f"全部处理完毕：成功 {succeeded} / {len(args.threads)} 个。")
    if failed:
        print("失败清单：" + "、".join(failed), file=sys.stderr)
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.selftest:
        return selftest()

    if args.threads:
        return run_cli(args)

    # 没有任何串号参数 → 启动图形界面。
    from xdao.gui import run

    run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
