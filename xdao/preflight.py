"""本机自检：把「这台机器上到底哪里不对劲」一次说清楚。

``--selftest`` 原本只做联网自检（接口、CDN、取串），本机环境的问题
（配置目录写不进去、缓存目录没有一处能写、找不到浏览器、打包版该补的参数没补上）
它一个字都不说。碰上导出失败时，用户手里就只有一句报错，排障要从头问起。

这个模块把检查项独立出来，按「想干什么 → 这一步行不行 → 不行该怎么办」组织，
输出既给人看（中文、带结论），也能给支持者看（``--selftest-json`` 原样吐 JSON）。

约定：

* **只读**：不做任何会改变用户配置的动作 —— 判断配置目录能不能写用的是
  ``can_write_dir`` 的探针（自己建、自己删），绝不真的调用 ``AppSettings.save()``。
* **不抛异常**：每一项都自己兜住异常，某项炸了就记一条失败，剩下的继续跑。
* 状态分三种：``ok`` / ``warn``（能用，但有值得知道的情况）/ ``fail``（这条路走不通）。

联网那两条（接口连接、取串接口）默认不跑：断网时本机这些检查照样有意义，
界面上也就能先给本机结论、用户点了再去联网。
"""

from __future__ import annotations

import json
import platform
import sys
from dataclasses import dataclass, field
from pathlib import Path

from . import __version__, browser_flags, cache, settings as settings_mod
from .browser_login import find_browser
from .exporters._shared import can_write_dir

STATUS_LABELS = {"ok": "可以", "warn": "注意", "fail": "不行"}


@dataclass
class Check:
    """一项检查的结果。"""

    name: str
    status: str
    detail: str
    advice: str = ""

    @property
    def label(self) -> str:
        return STATUS_LABELS.get(self.status, self.status)

    def line(self) -> str:
        text = f"[{self.label}] {self.name}：{self.detail}"
        if self.advice:
            text += f"\n        怎么办：{self.advice}"
        return text


@dataclass
class Report:
    """一整轮自检的结果。"""

    checks: list[Check] = field(default_factory=list)

    def add(
        self,
        check: "Check | None" = None,
        *,
        name: str = "",
        status: str = "ok",
        detail: str = "",
        advice: str = "",
    ) -> Check:
        """收一项结果。

        两种写法都收：直接给一个 :class:`Check`（``report.add(_check_x())``），
        或者给 ``name=`` / ``status=`` / ``detail=`` 现造一个。
        """
        if check is None:
            if not name:
                raise ValueError("要么给一个 Check，要么给 name；两个都没给")
            check = Check(name=name, status=status, detail=detail, advice=advice)
        self.checks.append(check)
        return check

    def extend(self, checks: list[Check]) -> None:
        self.checks.extend(checks)

    @property
    def failures(self) -> list[Check]:
        return [c for c in self.checks if c.status == "fail"]

    @property
    def warnings(self) -> list[Check]:
        return [c for c in self.checks if c.status == "warn"]

    def summary(self) -> str:
        failed, warned = len(self.failures), len(self.warnings)
        if failed:
            return (
                f"自检发现 {failed} 处走不通、{warned} 处要注意。"
                "下面标着「不行」的先解决，程序才能正常干活。"
            )
        if warned:
            return f"本机自检通过，有 {warned} 处值得注意，不影响使用。"
        return "本机自检全部通过。"

    def render(self) -> str:
        lines = [check.line() for check in self.checks]
        lines.append("")
        lines.append(self.summary())
        return "\n".join(lines)

    def to_json(self) -> str:
        payload = {
            "程序版本": __version__,
            "检查结果": [
                {
                    "项目": c.name,
                    "结论": c.label,
                    "状态": c.status,
                    "说明": c.detail,
                    "怎么办": c.advice,
                }
                for c in self.checks
            ],
        }
        return json.dumps(payload, ensure_ascii=False, indent=2)


def environment_lines() -> list[str]:
    """版本与运行环境（给日志面板和支持者看的抬头）。"""
    frozen = "是（免安装版）" if browser_flags.is_frozen() else "否（源码运行）"
    return [
        f"程序版本：{__version__}",
        f"Python：{platform.python_version()}（{sys.executable}）",
        f"系统：{platform.platform()}",
        f"打包运行：{frozen}",
    ]


def _check_running_mode() -> Check:
    frozen = browser_flags.is_frozen()
    flags = browser_flags.launch_flags()
    if frozen:
        return Check(
            name="运行方式",
            status="ok",
            detail=f"免安装版；启动浏览器时会附加 {'、'.join(flags) or '（无）'}",
        )
    return Check(
        name="运行方式",
        status="ok",
        detail="源码运行；启动浏览器时按浏览器默认的安全设置（不附加开关）",
    )


def _check_config(config_dir: Path) -> Check:
    writable = can_write_dir(config_dir)
    if writable:
        return Check(name="配置目录", status="ok", detail=f"能写：{config_dir}")
    return Check(
        name="配置目录",
        status="fail",
        detail=f"写不进去：{config_dir}",
        advice="设置（登录饼干、导出目录、格式偏好）改完存不下来。"
        "把程序放到有写权限的位置，或给这个目录加上写权限；"
        "安全软件的「受控文件夹访问」也会拦这里。"
        "火绒、电脑管家一类的安全软件还可能只信「有签名」的程序：没签名的程序写这些目录会被"
        "悄悄拒绝（别的程序写同一个目录却没事）。遇上这种就把程序目录加进安全软件的信任区，"
        "或等签名版发布后再试。",
    )


def _check_config_file(config_path: Path) -> Check:
    if not config_path.exists():
        return Check(name="配置文件", status="ok", detail=f"还没有（首次运行）：{config_path}")
    try:
        raw = config_path.read_text(encoding="utf-8")
    except OSError as exc:
        return Check(name="配置文件", status="fail", detail=f"读不出来：{exc}")
    except UnicodeDecodeError:
        return Check(
            name="配置文件",
            status="fail",
            detail="不是 UTF-8 文本，像是被别的程序改坏了",
            advice="删掉它再启动，程序会按默认值重建（登录状态要重新登一次）。",
        )
    try:
        data = json.loads(raw)
    except ValueError as exc:
        return Check(
            name="配置文件",
            status="fail",
            detail=f"不是有效的 JSON（{exc}）",
            advice="删掉它再启动，程序会按默认值重建（登录状态要重新登一次）。",
        )
    if not isinstance(data, dict):
        return Check(
            name="配置文件",
            status="fail",
            detail="内容不是一个对象",
            advice="删掉它再启动，程序会按默认值重建。",
        )
    return Check(name="配置文件", status="ok", detail=f"能读：{len(data)} 项设置")


def _check_export_dir(output_dir: str) -> Check:
    if not output_dir:
        return Check(
            name="导出目录",
            status="ok",
            detail="还没选（首次导出时会让你选一个）",
        )
    target = Path(output_dir)
    if can_write_dir(target):
        return Check(name="导出目录", status="ok", detail=f"能写：{target}")
    return Check(
        name="导出目录",
        status="fail",
        detail=f"写不进去：{target}",
        advice="导出时会自动换到能写的目录并告诉你换到了哪里；"
        "想固定下来就在界面里重新选一个能写的目录。"
        "如果别的程序写这个目录没事、只有本程序写不进去，多半是安全软件在拦没签名的程序："
        "把程序目录加进它的信任区即可。",
    )


def _check_cache_dir(preferred: str) -> Check:
    try:
        candidates = cache.cache_dir_candidates(preferred or None)
        chosen, note = cache.resolve_cache_dir(preferred or None)
    except Exception as exc:  # 候选本身算出不来才算失败
        return Check(
            name="缓存目录",
            status="fail",
            detail=f"算不出可用位置：{type(exc).__name__}: {exc}",
            advice="缓存只是加速手段，但这说明本机的环境变量（用户目录、临时目录）不对劲。",
        )
    writable = [path for path in candidates if can_write_dir(path)]
    if not writable:
        return Check(
            name="缓存目录",
            status="fail",
            detail=f"{len(candidates)} 个候选位置没有一个能写",
            advice="导出会退化成不使用缓存（每次重新抓取，慢但结果一样）；"
            "想恢复缓存就在设置里把缓存目录指到一个能写的位置。",
        )
    status = "ok" if not note else "warn"
    detail = f"会用在：{chosen}"
    if note:
        detail += f"（{note}）"
    detail += f"；候选 {len(candidates)} 个，能写 {len(writable)} 个"
    return Check(name="缓存目录", status=status, detail=detail)


def _check_browser(explicit: str) -> Check:
    try:
        found = find_browser(explicit or None, None)
    except Exception as exc:
        return Check(
            name="浏览器",
            status="fail",
            detail=f"查找时出错：{type(exc).__name__}: {exc}",
            advice="在设置里手动指定浏览器可执行文件的完整路径。",
        )
    if found is None:
        return Check(
            name="浏览器",
            status="fail",
            detail="系统 Edge / Chrome 都没找到",
            advice="导出 PDF 和「用浏览器登录」都需要它：装一个 Edge 或 Chrome，"
            "或在设置里手动指定浏览器路径。其余格式（HTML / TXT / Markdown / EPUB）不受影响。",
        )
    detail = f"{found.name}：{found.path}"
    if explicit:
        detail = "（手动指定）" + detail
    else:
        detail = "（自动找到）" + detail
    return Check(name="浏览器", status="ok", detail=detail)


def _check_exporters() -> Check:
    try:
        from .exporters import EXPORTERS
    except Exception as exc:
        return Check(
            name="导出格式",
            status="fail",
            detail=f"导入失败：{type(exc).__name__}: {exc}",
            advice="程序文件不完整，重新下载解压一次。",
        )
    names = "、".join(EXPORTERS)
    return Check(name="导出格式", status="ok", detail=f"可用：{names}")


def _loaded_settings():
    """读一次用户配置；测试里可以把它换掉，免得自检去碰真实配置。"""
    return settings_mod.AppSettings.load()


def browser_start_checks(
    explicit: str = "", *, timeout: float | None = None, settings=None
) -> Report:
    """真的启一次浏览器，看它起不起得来（``--selftest --check-browser`` 加的那一条）。

    为什么要有：默认自检里的「浏览器」那一项只查**找得到可执行文件**，于是「装了安全
    软件、浏览器一起来就被拦下」在自检里显示「可以」，而「用浏览器登录」永远失败
    （真机上量到 Edge 退出码 21，排查花了好几轮）。这条会短暂启动浏览器进程，所以
    **默认不跑**，得显式点名。

    这里挨个试候选（而不是试到一个能用就停）：用户碰到的正是「默认那个起不来、
    另一个能用」，只报一个失败会让人以为整条路都废了。

    ``explicit`` 留空时按设置里指定的浏览器试（和「用浏览器登录」同一条挑选逻辑），
    所以结论回答的就是「我点那个按钮时用的是谁、它行不行」。
    """
    from . import browser_check

    if settings is None:
        settings = _loaded_settings()
    if not explicit:
        explicit = getattr(settings, "pdf_browser", "") or ""
    if timeout is None:
        timeout = browser_check.DEFAULT_TIMEOUT
    report = browser_check.check_all(explicit, timeout=timeout)
    good = report.first_ok()
    checks: list[Check] = []
    for item in report.checks:
        if item.ok:
            checks.append(
                Check(
                    name=f"浏览器启动·{item.name}",
                    status="ok",
                    detail=item.line().removeprefix("[可以] "),
                )
            )
            continue
        if item.found:
            detail = item.line().removeprefix("[不行] ")
            advice = (
                "「用浏览器登录」这条路多半就卡在这儿：安全软件（火绒、360 之类）"
                "会拦下浏览器进程或本地调试端口。可以先关掉拦截再试，"
                "也可以在设置面板的「PDF 浏览器」里换一个能用的，"
                "或改用「直接粘贴饼干登录」。"
            )
        else:
            detail = item.detail
            advice = (
                "装一个 Edge、Chrome、Chromium 或 Brave；"
                "或者在设置面板的「PDF 浏览器」里手动填可执行文件的完整路径。"
            )
        checks.append(Check(name=f"浏览器启动·{item.name}", status="fail", detail=detail, advice=advice))
    if good is not None:
        checks.append(
            Check(
                name="浏览器启动",
                status="ok",
                detail=f"{good.name} 起得来，所以「用浏览器登录」这条路是通的。",
            )
        )
    elif report.checks:
        checks.append(
            Check(
                name="浏览器启动",
                status="fail",
                detail="本机这些浏览器都起不来，「用浏览器登录」暂时用不了。",
                advice="见上面每一条的「怎么办」；实在不行就用「直接粘贴饼干登录」。",
            )
        )
    return Report(checks)


def run_local_checks(
    *,
    config_path: Path | None = None,
    output_dir: str | None = None,
    cache_dir: str | None = None,
    browser_path: str | None = None,
) -> Report:
    """跑本机检查（不联网、不改任何东西）。"""
    if config_path is None:
        config_path = settings_mod.app_config_dir() / "config.json"
    config_path = Path(config_path)

    if output_dir is None or cache_dir is None or browser_path is None:
        loaded = _loaded_settings()
        if output_dir is None:
            output_dir = loaded.output_dir or ""
        if cache_dir is None:
            cache_dir = loaded.cache_dir or ""
        if browser_path is None:
            browser_path = loaded.pdf_browser or ""

    report = Report()
    report.add(_check_running_mode())
    report.add(_check_config(config_path.parent))
    report.add(_check_config_file(config_path))
    report.add(_check_export_dir(output_dir))
    report.add(_check_cache_dir(cache_dir))
    report.add(_check_browser(browser_path))
    report.add(_check_exporters())
    return report


def run_network_checks() -> list[Check]:
    """联网那两条：接口通不通、取串返不返回内容。

    ``XdaoClient()`` 自己读配置（代理、超时、重试都在里面），所以这里不用传参。
    """
    from .client import XdaoClient, XdaoError

    client = XdaoClient()
    reachable = Check(name="联网·接口连接", status="ok", detail="")
    try:
        cdn = client.update_cdn_path()
    except XdaoError as exc:
        return [
            Check(
                name="联网·接口连接",
                status="fail",
                detail=f"连不上：{exc}",
                advice="检查网络、代理设置（设置里的「代理」）和防火墙；"
                "没网时本机上那些检查仍然有效。",
            )
        ]
    reachable.detail = f"CDN = {cdn}"

    try:
        data = client.fetch_thread_page(50000001, 1)
    except XdaoError as exc:
        return [reachable, Check(name="联网·取串接口", status="fail", detail=f"取不到内容：{exc}")]
    if isinstance(data, dict) and data.get("success") is False:
        return [
            reachable,
            Check(name="联网·取串接口", status="fail", detail=f"接口返回错误：{data.get('error')}"),
        ]
    title = data.get("title") if isinstance(data, dict) else None
    return [
        reachable,
        Check(name="联网·取串接口", status="ok", detail=f"能取到内容（示例串标题：{title}）"),
    ]
