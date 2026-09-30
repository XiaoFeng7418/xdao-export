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
import os
import sys
import time
from pathlib import Path


def _open_utf8_console() -> None:
    """让命令行输出用 UTF-8，否则中文在 GBK 控制台里会变成乱码。

    打包版（尤其是 `--windowed` 构建）不会自己设 UTF-8，Windows 控制台拿到的是
    按本地代码页（cp936）解读的 UTF-8 字节，于是 `X岛串导出工具 0.3.3` 显示成
    `X������������ 0.3.3`。既然这一版的重点就是「让错误信息能读懂」，
    输出编码也必须一起修。窗口模式（--windowed）下没有控制台，直接跳过。
    """
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if stream is None:  # --windowed 打包版：没有控制台
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001 —— 编码修不好也不该影响导出
            pass


#: 只要命令行里出现这些开关，就说明用户是在终端里跑，需要把输出接到控制台。
_CONSOLE_HINT_FLAGS = {
    "-h",
    "--help",
    "--version",
    "--selftest",
    "--no-cache",
    "--no-notify",
    "--cache-dir",
    "--proxy",
    "--timeout",
    "--retries",
    "--throttle",
    "--interval",
    "--verify-cached",
    "--pdfdiag",
}


def _needs_console(argv: list[str] | None = None) -> bool:
    """判断这次启动需不需要往控制台说话（命令行用法、而不是双击开界面）。"""
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        return False
    return any(arg.split("=", 1)[0] in _CONSOLE_HINT_FLAGS for arg in args)


def _attach_parent_console(argv: list[str] | None = None) -> None:
    """打包版是 GUI 子系统程序，从 cmd 里跑时不会自动连上父控制台。

    结果是 `xdao-export.exe --version` 什么都不显示，而且往那个「无效的 stdout」
    写还会冒出 `OSError: [Errno 22] Invalid argument`（v0.4.0 打包验收时实测到）。
    这里在确实要输出命令行信息时，临时 AttachConsole 到父进程的控制台并重新打开
    标准流；双击启动（没有父控制台）时它只会失败，然后什么都不做。
    """
    if not getattr(sys, "frozen", False) or os.name != "nt":
        return
    if not _needs_console(argv):
        return
    try:
        import ctypes

        ATTACH_PARENT_PROCESS = -1
        if not ctypes.windll.kernel32.AttachConsole(ATTACH_PARENT_PROCESS):
            return  # 没有父控制台（双击启动），保持原样
        for stream_name in ("stdout", "stderr"):
            try:
                setattr(sys, stream_name, open("CONOUT$", "w", encoding="utf-8", errors="replace"))
            except OSError:
                pass
        try:
            stdin = open("CONIN$", "r", encoding="utf-8", errors="replace")
            sys.stdin = stdin
        except OSError:
            pass
    except Exception:  # noqa: BLE001 —— 接不上控制台也不该影响导出
        pass


_attach_parent_console()
_open_utf8_console()


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
        choices=["html", "pdf", "txt", "markdown", "epub"],
        help="导出格式（默认 html；pdf 需要本机装有 Chrome 或 Edge）",
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
        "--pdf-browser",
        default=None,
        metavar="路径",
        help="导出 PDF 时使用的浏览器可执行文件（默认自动找 Chrome / Edge）",
    )
    parser.add_argument(
        "--pdf-timeout",
        type=int,
        default=None,
        metavar="秒",
        help="PDF 渲染超时（默认 900 秒）",
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
        "--no-notify",
        action="store_true",
        help="监控模式下不弹桌面通知（默认开启）",
    )
    parser.add_argument(
        "--selftest",
        action="store_true",
        help="只做接口连接自检",
    )
    parser.add_argument(
        "--pdfdiag",
        action="store_true",
        help=argparse.SUPPRESS,  # 排障用：诊断 PDF 导出为何启动不了浏览器
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
    from xdao.watcher import WatchTarget, check_once, notify_result, watch_forever

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
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        # 走到这里时还没有任何导出器参与，所以要自己把错误讲清楚；
        # 用户报过「导出到 D:\Windows 之类的目录 → 直接弹未处理异常对话框」。
        reason = exc.strerror or str(exc)
        raise XdaoError(
            f"导出目录没法创建：{output_dir}\n{reason}"
            f"（错误码 {exc.errno}）\n"
            "常见原因：这个位置不允许当前账户写入，或者路径里有一段不是文件夹。\n"
            "请换一个当前账户可写的目录（例如「文档」下的文件夹）后重试。"
        ) from None
    format_key = args.format or settings.format_key or "html"
    scope = args.scope or settings.scope or "all"
    hashes = _collect_hashes(args.hashes) or settings.parse_hashes()
    template = args.template if args.template is not None else (settings.filename_template or None)
    use_cache = not args.no_cache and settings.use_cache
    cache_dir = Path(args.cache_dir) if args.cache_dir else settings.resolved_cache_dir(output_dir)
    cache_note = ""
    if use_cache:
        # 缓存目录写不进去时抓取层会自动换一个能写的地方（缓存不该挡住宿主功能），
        # 这里先解析一次，好让表头显示的是真正会用到的目录。
        from xdao.cache import resolve_cache_dir

        cache_dir, cache_note = resolve_cache_dir(cache_dir)
        client.image_cache_dir = cache_dir / "images"

    print(f"导出目录：{output_dir}")
    print(f"格式：{format_key}（{EXPORTERS[format_key][1]}）｜范围：{scope}"
          f"｜缓存：{'启用' if use_cache else '关闭'}｜缓存目录：{cache_dir}")
    if cache_note:
        print(f"注意：{cache_note}")
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

        notifier = None
        if settings.notify and not args.no_notify:
            from xdao.notifications import Notifier

            notifier = Notifier(min_interval=settings.notify_interval)
            tip = "能发桌面通知" if notifier.available() else "当前环境发不出桌面通知，只会在终端打印"
            print(f"桌面通知：开（{tip}）")

        try:
            # 先跑一轮并把抓取过程打出来，然后进入静默循环。
            for target in targets:
                result = check_once(client, target, output_dir, cache_dir=cache_dir,
                                    progress=progress, verify_cached=args.verify)
                notify_result(notifier, result)
            watch_forever(
                client, targets, output_dir, max(15.0, interval),
                cache_dir=cache_dir, on_result=on_result, verify_cached=args.verify,
                notifier=notifier,
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
        browser_path=args.pdf_browser or settings.pdf_browser or None,
        pdf_timeout=args.pdf_timeout,
    )

    succeeded = 0
    failed: list[str] = []
    # 失败原因同时写进文件：控制台输出可能被管道截断，日志文件能留全。
    log_lines: list[str] = [
        f"开始时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"导出目录：{output_dir}",
        f"格式：{format_key}｜范围：{scope}｜缓存：{'启用' if use_cache else '关闭'}",
        "",
    ]
    for index, raw in enumerate(args.threads, start=1):
        print(f"[{index}/{len(args.threads)}] {raw}")
        log_lines.append(f"[{index}/{len(args.threads)}] {raw}")
        try:
            result = fetcher.fetch(raw, verify_cached=args.verify)
            thread = ThreadData(
                thread_id=result.meta.thread_id,
                title=result.meta.title,
                po_hash=result.meta.po_hash,
                posts=list(result.posts),
            )
            print(f"    {len(thread.posts)} 楼 · {result.reason}")
            log_lines.append(f"    抓取：{len(thread.posts)} 楼 · {result.reason}")
            if getattr(result, "cache_warning", ""):
                # 可能是「换了缓存目录」，也可能是「缓存没写成」——两种情况都要说清楚，
                # 否则用户会以为下次能续上。
                print(f"    提示：{result.cache_warning}", file=sys.stderr)
                log_lines.append(f"    提示：{result.cache_warning}")
            path = exporter.save(thread, scope, output_dir, include_hashes=hashes)
            succeeded += 1
            size = f"（{path.stat().st_size / 1024 / 1024:.1f} MB）" if path.exists() else ""
            print(f"    完成：{path}{size}")
            log_lines.append(f"    完成：{path}{size}")
            # 例如 PDF 渲染失败改存了 HTML —— 必须在日志里留下原因
            note = getattr(exporter, "fallback_note", "")
            if note:
                print(f"    说明：{note}")
                log_lines.append(f"    说明：{note}")
        except XdaoError as exc:
            failed.append(raw)
            print(f"    失败：{exc}", file=sys.stderr)
            log_lines.append(f"    失败：{exc}")
        except Exception as exc:
            failed.append(raw)
            detail = f"{type(exc).__name__}: {exc}"
            print(f"    失败：{detail}", file=sys.stderr)
            log_lines.append(f"    失败：{detail}")

    summary = f"全部处理完毕：成功 {succeeded} / {len(args.threads)} 个。"
    print(summary)
    log_lines.append("")
    log_lines.append(summary)
    if failed:
        print(f"失败 {len(failed)} 个：" + "、".join(failed), file=sys.stderr)
        print("（以上失败原因已写入导出目录下的 xdao-export.log）", file=sys.stderr)
        log_lines.append("失败清单：" + "、".join(failed))

    try:
        (output_dir / "xdao-export.log").write_text(
            "\n".join(log_lines) + "\n", encoding="utf-8"
        )
    except OSError:
        pass  # 写日志失败不影响导出结果

    if failed:
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.selftest:
        return selftest()

    if args.pdfdiag:
        from tools.pdf_diag import main as pdf_diag_main

        return pdf_diag_main()

    if args.threads:
        # 兜底：跑到这一步说明是命令行模式，绝不能让未处理的异常穿透到用户面前
        # —— 打包版会把 traceback 弹成「Unhandled exception in script」对话框。
        from xdao.client import XdaoError

        try:
            return run_cli(args)
        except KeyboardInterrupt:
            print("已中断。", file=sys.stderr)
            return 130
        except XdaoError as exc:
            print(f"失败：{exc}", file=sys.stderr)
            return 1
        except Exception as exc:  # noqa: BLE001 —— 这里就是要兜住一切
            print(f"失败：{type(exc).__name__}: {exc}", file=sys.stderr)
            print(
                "这是没预料到的错误。请把上面这行连同复现步骤发到项目 issue："
                "https://github.com/XiaoFeng7418/xdao-export/issues",
                file=sys.stderr,
            )
            return 1

    # 没有任何串号参数 → 启动图形界面。
    from xdao.gui import run

    run()
    return 0


if __name__ == "__main__":
    # 最后一道网：连参数解析、读配置、启动界面都算在内，任何没被接住的异常
    # 都转换成一句人话 + 非零退出码，而不是让打包版弹出 traceback 对话框。
    try:
        code = main()
    except KeyboardInterrupt:
        print("已中断。", file=sys.stderr)
        code = 130
    except BaseException as exc:  # noqa: BLE001 —— 入口处的兜底
        if isinstance(exc, SystemExit):
            raise
        print(f"启动失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        print(
            "如果这是配置或目录问题：可以删掉 %APPDATA%\\xdao-export\\config.json 后重试；"
            "其他情况请把这个错误发到 "
            "https://github.com/XiaoFeng7418/xdao-export/issues",
            file=sys.stderr,
        )
        code = 1
    raise SystemExit(code)
