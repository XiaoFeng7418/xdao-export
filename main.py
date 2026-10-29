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
    "--input",
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


def selftest(json_output: bool = False, offline: bool = False, browser: bool = False) -> int:
    """自检：先看本机环境，再看接口能不能通。

    分两段是有意的 —— 本机那段（配置目录、缓存目录、导出目录、浏览器、导出格式）
    在断网时照样有意义，而它在原来自检里完全没有。

    ``browser=True``（``--selftest --check-browser``）时额外**真的启一次浏览器**：
    本机那段里的「浏览器」只查找不找得到可执行文件，碰上「找得到、一起来就被拦下」
    照样显示「可以」。这一段会真的启动浏览器进程，所以默认不跑。
    """
    from xdao import preflight

    report = preflight.Report()
    if not json_output:
        for line in preflight.environment_lines():
            print(line)
        print("")
    report.extend(preflight.run_local_checks().checks)
    if browser:
        if not json_output:
            print("正在真的启动一次浏览器…（起不来的话会写明退出码）")
        report.extend(preflight.browser_start_checks().checks)
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
    report = browser_check.check_all(explicit, progress=progress)
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
        "--max-pages",
        type=_page_limit,
        default=None,
        metavar="N",
        help="每个串最多抓 N 页（含第 1 页）；填 0 表示不限页数",
    )
    from xdao.cache import PAGE_WORKERS_MAX

    parser.add_argument(
        "--page-workers",
        type=_page_workers,
        default=None,
        metavar="N",
        help=f"同时抓 N 页（1~{PAGE_WORKERS_MAX}，默认 1 即一页一页抓）；"
        "长串调大能快不少，真撞上限流会自动退回一页一页抓",
    )
    parser.add_argument(
        "--input",
        default=None,
        metavar="文件",
        help="从文本文件读要导出的串：一行一个串号或网址，空行与 # 开头的行忽略；"
        "可以和命令行里写的串号一起用",
    )
    parser.add_argument(
        "--until",
        default=None,
        metavar="串号",
        help="导出多个串时抓到该串号为止（含它），它后面的串跳过；"
        "没写这个串号时，列表里串号不超过它的串都导",
    )
    parser.add_argument(
        "--font-size",
        type=_font_size,
        default=None,
        metavar="磅值",
        help="导出成品的正文字号（10~28，默认 14）；只影响 HTML / PDF / EPUB",
    )
    parser.add_argument(
        "--skip-existing",
        action="store_true",
        default=None,
        help="成品同名文件已存在且非空就跳过（一次贴很多串、断了可以接着跑）",
    )
    parser.add_argument(
        "--merge",
        action="store_true",
        help="把这次导出的所有串合并进同一个文件（HTML / TXT / Markdown / PDF）。"
        "默认关闭，即一个串一份文件。",
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
        choices=["embed", "url", "drop", "folder"],
        help=(
            "图片处理（HTML / PDF / EPUB）：embed 内嵌（默认）、url 仅链接、drop 丢弃、"
            "folder 存到成品旁边的同名目录（只对 HTML 与 Markdown 生效，PDF / EPUB 按内嵌处理）"
        ),
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
        help="试一试本机浏览器能不能真的起来（会短暂启动它；排查「用浏览器登录」失败时用）"
        "；配 --selftest 用：把它当成自检里的一条（默认只查找不找得到）",
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


def _page_limit(raw: str) -> int:
    """``--max-pages`` 的取值校验：非负整数；0 表示不限页数。

    为什么挡在这里而不是悄悄回落到默认：写错的参数（``--max-pages abc``）若不报错，
    用户会以为上限生效了，实际按"不限"抓完整个串 —— 那正是这个开关要防的事。
    argparse 的报错会给出开关名与写法，比导出跑到一半再失败清楚得多。
    """
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError(
            f"页数上限得填一个非负整数（现在填的是 {raw!r}）；0 表示不限页数"
        ) from None
    if value < 0:
        raise argparse.ArgumentTypeError("页数上限不能是负数；0 表示不限页数")
    return value


def _font_size(raw: str) -> int:
    """``--font-size`` 的取值校验：10~28 之间的整数。

    和 ``--max-pages`` 同一个理由：写错的值若不报错，用户会以为字号生效了，
    实际还是默认的 14 —— 而他多半是因为"看不清"才来调这个开关的。
    """
    from xdao.settings import EXPORT_FONT_SIZE_MAX, EXPORT_FONT_SIZE_MIN

    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError(
            f"字号得填一个整数（{EXPORT_FONT_SIZE_MIN}~{EXPORT_FONT_SIZE_MAX}），"
            f"现在填的是 {raw!r}"
        ) from None
    if not EXPORT_FONT_SIZE_MIN <= value <= EXPORT_FONT_SIZE_MAX:
        raise argparse.ArgumentTypeError(
            f"字号要在 {EXPORT_FONT_SIZE_MIN}~{EXPORT_FONT_SIZE_MAX} 之间，现在填的是 {value}"
        )
    return value


def _page_workers(raw: str) -> int:
    """``--page-workers`` 的取值校验：1~6 之间的整数。

    默认 1（＝一页一页抓，与旧版本一致）。上限压在 6：再往上对站点不礼貌、
    也更容易撞限流，而抓页的瓶颈本来就多在服务端延迟而不是连接数。
    写错的值必须报错而不是悄悄回落 —— 用户以为开了并发、其实还是单路，
    那这个开关就白给了。
    """
    from xdao.cache import PAGE_WORKERS_MAX

    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError(
            f"同时抓几页得填一个整数（1~{PAGE_WORKERS_MAX}），现在填的是 {raw!r}"
        ) from None
    if not 1 <= value <= PAGE_WORKERS_MAX:
        raise argparse.ArgumentTypeError(
            f"同时抓几页要在 1~{PAGE_WORKERS_MAX} 之间，现在填的是 {value}"
        )
    return value


def _effective_page_workers(args: argparse.Namespace, settings) -> int:
    """这次抓取实际开几路并发：命令行优先，其次配置，都没给就是 1（串行）。

    默认必须是 1：老配置里没有这个键，行为要与旧版本完全一致 —— 并行会把
    同时在飞的请求数涨上去，站点限流阈值谁也说不准，不能让升级顺手改掉它。
    """
    from xdao.cache import DEFAULT_PAGE_WORKERS, normalize_page_workers

    if getattr(args, "page_workers", None) is not None:
        return normalize_page_workers(args.page_workers)
    return normalize_page_workers(
        getattr(settings, "page_workers", DEFAULT_PAGE_WORKERS)
    )


def _effective_font_size(args: argparse.Namespace, settings) -> int:
    """这次导出实际用的正文字号：命令行优先，其次配置，都没给就是 14。

    默认值必须是 14（＝v0.15.0 之前写死在 HTML 里的值）：老配置里没有这个键，
    默认一变，老用户升级后成品字号就跟着变。
    """
    from xdao.settings import (
        DEFAULT_EXPORT_FONT_SIZE,
        EXPORT_FONT_SIZE_MAX,
        EXPORT_FONT_SIZE_MIN,
    )

    if args.font_size is not None:
        return int(args.font_size)
    try:
        value = int(getattr(settings, "export_font_size", DEFAULT_EXPORT_FONT_SIZE) or 0)
    except (TypeError, ValueError):
        return DEFAULT_EXPORT_FONT_SIZE
    if not EXPORT_FONT_SIZE_MIN <= value <= EXPORT_FONT_SIZE_MAX:
        return DEFAULT_EXPORT_FONT_SIZE
    return value


def _effective_max_pages(args: argparse.Namespace, settings) -> int:
    """这次导出实际用的页数上限：命令行给了就用它，否则用配置，都没给就是 0（不限）。

    0 有意与 ``xdao/cache.py`` 里那道 5000 页的安全阀分开：传 0 进去表示"用户没填"，
    抓取层遇到它仍会留一道阀；用户填了更小的值时就以用户填的为准。
    """
    if args.max_pages is not None:
        return int(args.max_pages)
    try:
        value = int(getattr(settings, "max_pages", 0) or 0)
    except (TypeError, ValueError):
        return 0
    return value if value > 0 else 0


def _effective_until(args: argparse.Namespace, settings) -> int | None:
    """这次导出实际用的串号截止：命令行优先，其次配置；都为空就是不截止。

    解析只有 ``xdao.cache.parse_thread_id`` 一套规则（网址、纯数字都认），
    界面上填的东西走的是同一个函数。
    """
    from xdao.cache import parse_thread_id

    raw = args.until if args.until is not None else getattr(settings, "until_thread_id", "")
    return parse_thread_id(raw)


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


def _merge_thread_sources(args: argparse.Namespace) -> list[str] | str:
    """把命令行上写的串号与 --input 指的清单合成一份（去重、保序）。

    返回字符串表示清单读不了，那个字符串是给人看的原因。
    """
    from xdao.cache import parse_thread_id
    from xdao.threadlist import ThreadListError, read_thread_list

    items = list(args.threads)
    source = getattr(args, "input", None)
    if source:
        try:
            items.extend(read_thread_list(source))
        except ThreadListError as exc:
            return str(exc)
    merged: list[str] = []
    seen: set[int] = set()
    for item in items:
        thread_id = parse_thread_id(item)
        if thread_id is None or thread_id in seen:
            continue
        seen.add(thread_id)
        merged.append(item)
    return merged


def run_cli(args: argparse.Namespace) -> int:
    """命令行导出 / 监控。返回进程退出码。"""
    from xdao.cache import CachedThreadFetcher, filter_until_thread_id
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
    # 导出范围（页数上限、串号截止）也先定下来：两者都可能来自配置文件，
    # 且"一个串都不用导"这件事要在建目录、连网络之前就报出来。
    max_pages = _effective_max_pages(args, settings)
    until_thread_id = _effective_until(args, settings)
    # 正文字号同理：命令行优先、其次配置，默认 14（＝老版本写死的值）。
    font_size = _effective_font_size(args, settings)
    # 并行抓页：默认 1 路（串行），命令行优先、其次配置。
    page_workers = _effective_page_workers(args, settings)
    # 断点续跑：--skip-existing 一旦给了就开；没给就看配置里的开关。
    merge_export = bool(getattr(args, "merge", False))
    skip_existing = (
        True
        if getattr(args, "skip_existing", None)
        else bool(getattr(settings, "skip_existing", False))
    )
    requested = _merge_thread_sources(args)
    if isinstance(requested, str):
        # 清单文件读不了：在连网络之前就把原因摆出来。
        print(f"清单文件有误：{requested}", file=sys.stderr)
        return 2
    export_urls, skipped_urls = filter_until_thread_id(requested, until_thread_id)
    if not export_urls:
        # 不是错误：列表被截止挡光了。说清楚规则，用户改一个数就能重来。
        print(
            "串号截止把列表里的串全都挡住了，没有要导出的串。\n"
            f"截止是「抓到串号 {until_thread_id} 为止（含它）」："
            "列表里串号比它大的串都会跳过。\n"
            "把 --until 改成更大的串号、或去掉这个开关后重试。",
            file=sys.stderr,
        )
        return 0
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
    # 生效的导出范围每次都报：只导了一段却看起来"抓完了"，是这类功能最容易误判的地方。
    range_text = "全部页" if max_pages <= 0 else f"最多 {max_pages} 页"
    if until_thread_id is None:
        until_text = "不按串号截止"
    else:
        until_text = f"导到串号 {until_thread_id} 为止（含它）"
        if skipped_urls:
            until_text += f"，跳过它之后的 {len(skipped_urls)} 个串"
    print(f"导出范围：{range_text}；{until_text}。")

    image_mode = args.image_mode or settings.image_mode or "embed"
    if image_mode == "folder" and format_key not in ("html", "markdown"):
        # 「存到同名目录」要往成品旁边写文件：HTML 与 Markdown 都有同名成品；
        # PDF 在内存里渲染、EPUB 是 zip，都按内嵌处理并说一声。
        print("注意：「存到同名目录」只对 HTML 与 Markdown 生效，这次按内嵌处理。")
        image_mode = "embed"

    if args.watch:
        interval = args.interval or settings.watch_interval
        targets = [
            WatchTarget(
                url,
                scope=scope,
                format_key=format_key,
                include_hashes=hashes,
                image_mode=image_mode,
                # 每轮都按命令行的「最多页数」抓（0＝不限），和单次导出同一套语义。
                max_pages=max_pages,
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
                                    pdf_options=pdf_options,
                                    font_size=font_size,
                                    page_workers=page_workers)
                notify_result(notifier, result)
            watch_forever(
                client, targets, output_dir, max(15.0, interval),
                cache_dir=cache_dir, on_result=on_result, verify_cached=args.verify,
                notifier=notifier,
                browser_path=args.pdf_browser or settings.pdf_browser or None,
                pdf_options=pdf_options,
                font_size=font_size,
                page_workers=page_workers,
            )
        except KeyboardInterrupt:
            print("\n已停止监控。")
        return 0

    fetcher = CachedThreadFetcher(client, cache_dir=cache_dir, progress=lambda m: print(f"    {m}"),
                                 use_cache=use_cache, page_workers=page_workers)
    exporter = create_exporter(
        format_key,
        client,
        # 导出器自己也报进度（整理正文、下载图片、渲染 PDF）。不传的话命令行
        # 在「抓完最后一页 → 成品落盘」这段最长的等待里一片安静，看着像卡住。
        progress=lambda message: print(f"    {message}"),
        filename_template=template,
        image_mode=image_mode,
        browser_path=args.pdf_browser or settings.pdf_browser or None,
        pdf_timeout=args.pdf_timeout,
        pdf_options=pdf_options,
        font_size=font_size,
        skip_existing=skip_existing,
    )
    if merge_export and not getattr(exporter, "supports_merge", False):
        # 说清楚为什么没合并：用户勾了合并却发现还是一串一份会很困惑。
        print(
            f"    提示：{format_key} 不支持合并导出，仍按一个串一份文件保存。",
            file=sys.stderr,
        )
        merge_export = False

    succeeded = 0
    failed: list[str] = []
    # 失败原因同时写进文件：控制台输出可能被管道截断，日志文件能留全。
    log_lines: list[str] = [
        f"开始时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"导出目录：{output_dir}",
        f"格式：{format_key}｜范围：{scope}｜缓存：{'启用' if use_cache else '关闭'}",
        f"导出范围：{range_text}；{until_text}",
        f"正文字号：{font_size}px（只影响 HTML / PDF / EPUB）",
        "已存在就跳过：开启（同名成品非空就不重做）" if skip_existing else "已存在就跳过：关闭",
        ("合并导出：开启（所有串写进同一份文件）" if merge_export else "合并导出：关闭"),
        "",
    ]
    merged_threads: list[ThreadData] = []
    for index, raw in enumerate(export_urls, start=1):
        print(f"[{index}/{len(export_urls)}] {raw}")
        log_lines.append(f"[{index}/{len(export_urls)}] {raw}")
        try:
            # 页数上限（0＝不限）交给抓取层：它是唯一知道"到没到上限"的地方，
            # 撞上时结果里的 retry_note 会写清楚停在那一页。
            result = fetcher.fetch(
                raw, max_pages=max_pages, verify_cached=args.verify
            )
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
            if merge_export:
                merged_threads.append(thread)
                succeeded += 1
                print("    已收进合并文件（全部抓完后统一生成）")
                log_lines.append("    已收进合并文件")
                continue
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

    if merge_export and merged_threads:
        try:
            merged_path = exporter.save_many(
                merged_threads, scope, output_dir, include_hashes=hashes
            )
            size = (
                f"（{merged_path.stat().st_size / 1024 / 1024:.1f} MB）"
                if merged_path.exists()
                else ""
            )
            print(f"    合并完成：{merged_path}{size}")
            log_lines.append(f"    合并完成：{merged_path}{size}")
            note = getattr(exporter, "fallback_note", "")
            if note:
                print(f"    说明：{note}")
                log_lines.append(f"    说明：{note}")
        except XdaoError as exc:
            failed.extend(merged_threads and [getattr(t, "thread_id", "") for t in merged_threads])
            print(f"    合并失败：{exc}", file=sys.stderr)
            log_lines.append(f"    合并失败：{exc}")

    summary = f"全部处理完毕：成功 {succeeded} / {len(export_urls)} 个。"
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
        # --check-browser 在这儿是**加一条**（真的启一次浏览器），不是那条单独的
        # 命令：自检里的「浏览器」只查找不找得到可执行文件。
        return selftest(
            json_output=args.selftest_json,
            offline=args.offline,
            browser=bool(args.check_browser or args.check_browser_json),
        )

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

    if args.threads or args.input:
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
