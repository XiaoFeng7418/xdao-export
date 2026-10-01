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
from dataclasses import replace
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
    "--selftest-json",
    "--check-browser",
    "--check-browser-json",
    "--offline",
    "--check-update",
    "--check-update-json",
    "--apply-update",
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
    "--pdf-paper",
    "--pdf-orientation",
    "--pdf-margin",
    "--pdf-margin-mm",
    "--pdf-scale",
    "--pdf-no-background",
    "--pdf-pages",
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


def selftest(json_output: bool = False, offline: bool = False) -> int:
    """自检：先看本机环境，再看接口能不能通。

    分两段是有意的 —— 本机那段（配置目录、缓存目录、导出目录、浏览器、导出格式）
    在断网时照样有意义，而它在原来自检里完全没有。
    """
    from xdao import preflight

    report = preflight.Report()
    if not json_output:
        for line in preflight.environment_lines():
            print(line)
        print("")
    report.extend(preflight.run_local_checks().checks)
    if not offline:
        report.extend(preflight.run_network_checks())
    if json_output:
        print(report.to_json())
    else:
        print(report.render())
    return 1 if report.failures else 0


def check_browser(json_output: bool = False) -> int:
    """试一试本机的浏览器能不能真的起来（会短暂启动浏览器进程）。

    为什么单独一条命令：``--selftest`` 的浏览器那一项只查「找得到可执行文件」，
    可用户碰到的是「找得到、但一起来就被拦下」——自检显示「可以」，点「用浏览器
    登录」却永远失败。这里真的启一次、等调试端口写出来、马上关掉，把结论和
    退出码直接摆出来。至少一个浏览器能起来就算通过（退出码 0）。
    """
    from xdao import browser_check
    from xdao import __version__ as xdao_version
    from xdao.settings import AppSettings

    settings = AppSettings.load()
    explicit = getattr(settings, "pdf_browser", "") or ""

    def progress(message: str) -> None:
        if not json_output:
            print(message)

    if not json_output:
        print(f"程序版本：{xdao_version}")
        if explicit:
            print(f"按设置里指定的浏览器先试：{explicit}")
        else:
            print("设置里没指定浏览器，按「系统默认浏览器 → Edge → Chrome」的顺序试。")
        print("（会真的启动浏览器进程，试完立刻关掉；最多试几个就停。）")
        print("")
    report = browser_check.check_browsers(explicit, progress=progress)
    print(report.to_json() if json_output else report.render())
    return 0 if report.ok else 1


def check_update(json_output: bool = False) -> int:
    """查一下有没有新版本。

    查不到（没网、代理不通、接口改版）不算程序出错，所以退出码是 0 ——
    这条命令的用途是「顺手看一眼」，不是「必须成功」。
    """
    from xdao import update_check

    result = update_check.check_for_update(force=True)
    if json_output:
        print(result.to_json())
    else:
        print(result.line())
    return 0


def apply_update(argv: list[str]) -> int:
    """升级帮手模式：等老进程退出 → 换上新版本 → 启动它。

    这不是给用户用的命令，是界面点「立即升级」后，程序叫醒「另一个自己」
    时带的内部开关（见 :func:`xdao.updater.spawn_helper`）。
    """
    from xdao import updater

    if len(argv) != 4:
        print("--apply-update 需要四个参数：目标目录 新版本目录 老进程号 启动程序路径", file=sys.stderr)
        return 2
    target, payload, pid_text, launcher = argv
    try:
        pid = int(pid_text)
    except ValueError:
        print("--apply-update 的进程号不是数字。", file=sys.stderr)
        return 2
    return updater.apply_update(
        Path(target).resolve(),
        Path(payload).resolve(),
        pid,
        Path(launcher).resolve(),
    )


def _pdf_value_help(table_name: str) -> str:
    """把 :mod:`xdao.pdf_opts` 里的取值表拼成 ``--help`` 里的候选清单。

    直接引用那张表而不是在这里再抄一遍：帮助里列出的取值永远不会和程序
    实际接受的取值走散。
    """
    items: list[str] = []
    try:
        from xdao import pdf_opts

        table = getattr(pdf_opts, table_name, None)
        if isinstance(table, dict):
            items = [f"{key}＝{label}" for key, label in table.items() if str(key) != "default"]
        elif table:
            items = [str(item) for item in table if str(item) != "default"]
    except Exception:  # noqa: BLE001 —— 帮助文本拼不出来也不该让 --help 崩掉
        items = []
    listed = "、".join(items)
    text = f"{listed}，default 跟随网页样式" if listed else "default 跟随网页样式"
    # argparse 会用 % 去格式化 help 字符串，字面量百分号必须写两次；
    # 这里转义一次，以后标签表里加了 "%" 也不会让整个 --help 崩掉。
    return text.replace("%", "%%")


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
        "--pdf-paper",
        default=None,
        metavar="纸张",
        help=f"PDF 纸张（{_pdf_value_help('PAPER_LABELS')}）",
    )
    parser.add_argument(
        "--pdf-orientation",
        default=None,
        metavar="方向",
        help=f"PDF 方向（{_pdf_value_help('ORIENTATION_LABELS')}）",
    )
    parser.add_argument(
        "--pdf-margin",
        default=None,
        metavar="边距",
        help=f"PDF 页边距预设（{_pdf_value_help('MARGIN_PRESETS')}）",
    )
    parser.add_argument(
        "--pdf-margin-mm",
        default=None,
        metavar="毫米",
        help="PDF 页边距自定义值（毫米，会盖过 --pdf-margin；默认跟随网页样式）",
    )
    parser.add_argument(
        "--pdf-scale",
        default=None,
        metavar="倍数",
        # 注意别在 help 里写裸的百分号：argparse 拿 % 格式化这段文本，会直接报错。
        help="PDF 缩放倍数（0.5～2.0，默认 1.0＝原始大小）",
    )
    parser.add_argument(
        "--pdf-no-background",
        action="store_true",
        help="PDF 不打印网页背景色与背景图（默认打印）",
    )
    parser.add_argument(
        "--pdf-pages",
        default=None,
        metavar="页码",
        help="只导出这些页，例如 1-3,5（默认全部页码）",
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
        "--watch-export",
        metavar="文件",
        default=None,
        help="把监控列表导出成文件（默认合并进现有列表）",
    )
    parser.add_argument(
        "--watch-import",
        metavar="文件",
        default=None,
        help="把监控列表文件导入进来（和现在这份合并，不覆盖）",
    )
    parser.add_argument(
        "--watch-import-replace",
        action="store_true",
        help="配 --watch-import 用：用文件里的列表替换现有列表",
    )
    parser.add_argument(
        "--selftest",
        action="store_true",
        help="自检：先查本机环境（配置/缓存/导出目录、浏览器、导出格式），再测接口连接",
    )
    parser.add_argument(
        "--selftest-json",
        action="store_true",
        help="配 --selftest 用：结果按 JSON 输出（便于贴给别人看）",
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="配 --selftest 用：只查本机，不联网",
    )
    parser.add_argument(
        "--check-browser",
        action="store_true",
        help="试一试本机浏览器能不能真的起来（会短暂启动它；排查「用浏览器登录」失败时用）",
    )
    parser.add_argument(
        "--check-browser-json",
        action="store_true",
        help="配 --check-browser 用：结果按 JSON 输出（便于贴给别人看）",
    )
    parser.add_argument(
        "--check-update",
        action="store_true",
        help="查一下有没有新版本（问一次 GitHub，结果记一天）",
    )
    parser.add_argument(
        "--check-update-json",
        action="store_true",
        help="配 --check-update 用：结果按 JSON 输出",
    )
    parser.add_argument(
        "--apply-update",
        nargs=4,
        metavar=("目标目录", "新版本目录", "老进程号", "启动程序"),
        help=argparse.SUPPRESS,  # 内部开关：界面点「立即升级」后由另一个自己执行
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


def _resolve_pdf_options(args: argparse.Namespace, settings):
    """把命令行显式给出的 PDF 选项与配置里的值合成一份 ``PdfOptions``。

    命令行没给的项一律落回**配置里**的值（而不是内置默认值）—— 否则随手加一个
    ``--pdf-paper`` 就会把用户在图形界面里设好的缩放、边距一起抹掉。

    返回 ``(options, error)``：``error`` 非空表示参数不合法，调用方打印它并返回 2。
    """
    from xdao.pdf_opts import PdfOptionsError, require_valid_pdf_options

    def pick(cli_value, attribute: str, fallback: str):
        if cli_value is not None:
            return cli_value
        value = getattr(settings, attribute, None)
        return fallback if value in (None, "") else value

    def margin_value(cli_value, attribute: str) -> str:
        """边距槽位：默认值翻译成 ""（pdf_opts 里 ""＝这一项没填）。

        配置和界面的规范值都是 "default"，而 ``require_valid_pdf_options`` 会把
        显式传进去的 "default" 当成「用户直接写了毫米数」报错，所以这里先翻译。
        """
        value = pick(cli_value, attribute, "default")
        return "" if value in (None, "", "default") else value

    try:
        options = require_valid_pdf_options(
            paper=pick(args.pdf_paper, "pdf_paper", "default"),
            orientation=pick(args.pdf_orientation, "pdf_orientation", "portrait"),
            margin=margin_value(args.pdf_margin, "pdf_margin"),
            margin_mm=pick(args.pdf_margin_mm, "pdf_margin_mm", ""),
            scale=pick(args.pdf_scale, "pdf_scale", "1.0"),
            page_ranges=pick(args.pdf_pages, "pdf_page_ranges", ""),
        )
    except PdfOptionsError as exc:
        return None, str(exc)
    # 「不打印背景」是个开关，没有反方向的参数，所以它不参与上面的校验：
    # 只有命令行显式写了才覆盖配置里的值。
    background = bool(getattr(settings, "pdf_background", True)) and not args.pdf_no_background
    if bool(getattr(options, "background", True)) != background:
        options = replace(options, background=background)
    return options, ""


def run_cli(args: argparse.Namespace) -> int:
    """命令行导出 / 监控。返回进程退出码。"""
    from xdao.cache import CachedThreadFetcher
    from xdao.client import XdaoClient, XdaoError
    from xdao.exporters import EXPORTERS, ThreadData, choose_writable_dir, create_exporter
    from xdao.settings import AppSettings
    from xdao.watcher import WatchTarget, check_once, notify_result, watch_forever

    settings = AppSettings.load()
    # PDF 选项先校验：写在最前面，参数写错了就不必先去建目录、连网络。
    pdf_options, pdf_error = _resolve_pdf_options(args, settings)
    if pdf_error:
        print(f"PDF 参数有误：{pdf_error}", file=sys.stderr)
        print("（可选值见 --help）", file=sys.stderr)
        return 2
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
        # 实测过「导出到 D:\Windows 之类的目录 → 直接弹未处理异常对话框」。
        reason = exc.strerror or str(exc)
        raise XdaoError(
            f"导出目录没法创建：{output_dir}\n{reason}"
            f"（错误码 {exc.errno}）\n"
            "常见原因：这个位置不允许当前账户写入，或者路径里有一段不是文件夹。\n"
            "请换一个当前账户可写的目录（例如「文档」下的文件夹）后重试。"
        ) from None
    # 目录建得出来不代表写得进去：Windows 的「受控文件夹访问」默认保护桌面/文档，
    # 非白名单程序写进去报 [Errno 13]，抓取全成功后才发现就白跑一整趟。
    # 命令行显式给了 -o 就尊重用户指定（只提示），否则自动换到能写的位置。
    choice = choose_writable_dir(
        output_dir, kind="导出", allow_fallback=args.output is None
    )
    output_dir = choice.path
    for note in choice.notes:
        print(f"注意：{note}")
    if choice.fallback:
        # 兜底位置可能是第一次用，先建出来（探测只确认"写得进去"，不负责留下目录）。
        try:
            output_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:  # pragma: no cover - 探测刚说过能写，走到这里就是磁盘满了
            raise XdaoError(f"备用导出目录无法创建：{output_dir}\n{exc}") from None
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
    if format_key == "pdf":
        # 把真正生效的纸张/边距说出来：全默认（跟随网页样式）时不用占一行。
        # needs_cdp 而不是 is_default：只填了页码或只关了背景时同样要说一声。
        try:
            if pdf_options.needs_cdp or not pdf_options.background:
                print(f"PDF 设置：{pdf_options.describe()}")
        except Exception:  # noqa: BLE001 —— 提示语失败不该影响导出
            pass
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
                                    progress=progress, verify_cached=args.verify,
                                    browser_path=args.pdf_browser or settings.pdf_browser or None,
                                    pdf_options=pdf_options)
                notify_result(notifier, result)
            watch_forever(
                client, targets, output_dir, max(15.0, interval),
                cache_dir=cache_dir, on_result=on_result, verify_cached=args.verify,
                notifier=notifier,
                browser_path=args.pdf_browser or settings.pdf_browser or None,
                pdf_options=pdf_options,
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
        pdf_options=pdf_options,
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
            if getattr(result, "retry_note", ""):
                # 缺页/撞上限这类"产物不完整"必须显式说出来：旧版本是静默少抓一大截，
                # 拿到半份成品还以为抓完了。
                print(f"    注意：{result.retry_note}", file=sys.stderr)
                log_lines.append(f"    注意：{result.retry_note}")
            if getattr(result, "cache_warning", ""):
                # 可能是「换了缓存目录」，也可能是「缓存没写成」——两种情况都要说清楚，
                # 否则会以为下次能续上。
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


def _watch_list_command(args) -> int | None:
    """处理 ``--watch-export`` / ``--watch-import``。

    这两件事**不碰网络**：列表本来就存在配置里，搬移一份不需要登录，
    所以必须抢在建客户端、连接口之前返回 —— 没网也该能用。
    """
    from xdao import watch_list
    from xdao.settings import AppSettings
    from xdao.watcher import WatchTarget

    settings = AppSettings.load()
    if args.watch_export:
        targets = [WatchTarget.from_dict(d) for d in settings.watch_targets]
        try:
            written = watch_list.export_targets(targets, args.watch_export)
        except watch_list.WatchListError as exc:
            print(f"导出监控列表失败：{exc}", file=sys.stderr)
            return 2
        print(f"已导出 {len(targets)} 个监控串：{written}")
        if not targets:
            print("（列表是空的，所以文件里没有条目。）")
        return 0

    if not args.watch_import:
        return None

    existing = [WatchTarget.from_dict(d) for d in settings.watch_targets]
    try:
        result = watch_list.import_targets(existing, args.watch_import)
    except watch_list.WatchListError as exc:
        print(f"导入监控列表失败：{exc}", file=sys.stderr)
        return 2

    if args.watch_import_replace:
        # 「替换」= 列表变成文件里那些条目，**包括文件里和现在完全一样的那几条** ——
        # 少了 result.duplicates，「替换」会把它们连着旧列表一起丢掉，列表直接清空。
        merged = list(result.added) + list(result.duplicates)
        kept = len(result.duplicates)
    else:
        merged = existing + list(result.added)
        kept = 0
    settings.watch_targets = [t.to_dict() for t in merged]
    settings.save()
    verb = "替换为" if args.watch_import_replace else "合并"
    tail = f"（其中 {kept} 个本来就一样）" if kept else ""
    print(f"已{verb} {len(result.added)} 个监控串{tail}，现在共 {len(merged)} 个。")
    duplicates = {"这份列表里已经有了（同一个串、同样的设置）"}
    for reason, who in result.skipped:
        # 「已经有了」在合并时是「不用管」，在替换时是「原本就在、留着」——
        # 都印成「跳过」会让人以为这条被丢了。
        mark = "保留" if args.watch_import_replace and reason in duplicates else "跳过"
        print(f"  {mark} {who}：{reason}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.apply_update:
        # 内部开关，排在最前面：这是「另一个自己」被叫醒来换文件，别的都不用管。
        return apply_update(list(args.apply_update))

    if args.offline and (args.check_update or args.check_update_json):
        # 「只查本机」和「问一次 GitHub」是矛盾的，别让用户以为没联网。
        print("--offline 与 --check-update 不能一起用（查版本必须联网）。", file=sys.stderr)
        return 2
    if args.offline and not (args.selftest or args.selftest_json):
        # 单独给 --offline 的话，不接「启动图形界面」那条路 —— 那会让用户以为
        # 参数生效了（界面起来、什么都没查），必须当场说清楚。
        print("--offline 要配 --selftest 用。", file=sys.stderr)
        return 2
    if args.selftest or args.selftest_json:
        return selftest(json_output=args.selftest_json, offline=args.offline)

    if args.check_browser or args.check_browser_json:
        return check_browser(json_output=args.check_browser_json)

    if args.check_update or args.check_update_json:
        return check_update(json_output=args.check_update_json)

    if args.watch_export or args.watch_import:
        if args.watch_export and args.watch_import:
            print("--watch-export 与 --watch-import 只能用一个。", file=sys.stderr)
            return 2
        return _watch_list_command(args) or 0

    if args.watch_import_replace:
        # 单独给这个开关的话，不接「启动图形界面」那条路 —— 那会让用户以为
        # 参数生效了（界面起来、列表没动），必须当场说清楚。
        print("--watch-import-replace 要配 --watch-import 用。", file=sys.stderr)
        return 2

    if args.pdfdiag:
        from tools.pdf_diag import main as pdf_diag_main

        return pdf_diag_main()

    if args.threads:
        # 兜底：跑到这一步说明是命令行模式，绝不能让未处理的异常直接弹给使用者
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
