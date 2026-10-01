# -*- coding: utf-8 -*-
"""查一查浏览器到底能不能起来（不是「找不找得到」，是「起不起得来」）。

为什么要有这个模块：``--selftest`` 里的浏览器那一项只检查**找得到**可执行文件，
于是「装了安全软件、浏览器一起来就被拦下」这种情况在自检里显示为「可以」，
而用户点「用浏览器登录」却永远失败（真机上量到 Edge 退出码 21，排查花了好几轮）。
这个模块真的把浏览器以调试模式启一次、等它把调试端口写出来，然后立刻关掉，
把「起不来」当场变成一条带退出码的结论。

**会真的启动浏览器进程**（写一个临时资料目录，结束就删），所以它是命令行里
一条单独的命令（``--check-browser``），不塞进「不改任何东西」的 ``--selftest``。
"""

from __future__ import annotations

import json
import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .browser_login import (
    BrowserInfo,
    CdpError,
    LoginBrowser,
    browser_candidates,
    find_browser,
)

#: 每个浏览器最多等多久（真机上端口文件通常 0.5～1.2 秒就出现，20 秒是登录那条路的余量）。
DEFAULT_TIMEOUT = 12.0

#: 「起来了但立刻又退出」时记进 :attr:`BrowserCheck.detail` 的说法（调用方靠它判断）。
EXITED_EARLY = "刚起来就退出了"

#: 一条命令最多挨个试几个浏览器：默认那个 + 备用一两个就够了，别把机器上所有 Chromium 都启动一遍。
DEFAULT_LIMIT = 3


@dataclass
class BrowserCheck:
    """一个浏览器「能不能真的起来」的结论。"""

    name: str
    path: str
    ok: bool
    detail: str
    port: int = 0
    seconds: float = 0.0
    exit_code: int | None = None
    found: bool = True

    def line(self) -> str:
        """给命令行看的一行。"""
        if not self.found:
            return f"[找不到] {self.name}：没装，或不在常见安装位置。"
        if self.ok:
            who = f"，{self.detail}" if self.detail else ""
            return (
                f"[可以] {self.name}：能起来{who}，调试端口 {self.port} 答得上话"
                f"（{self.seconds:.1f} 秒）。"
            )
        return f"[不行] {self.name}：{self.detail}"

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "path": self.path,
            "ok": self.ok,
            "detail": self.detail,
            "port": self.port,
            "seconds": round(self.seconds, 2),
            "exit_code": self.exit_code,
            "found": self.found,
        }


@dataclass
class BrowserReport:
    """挨个试过之后的总结论。"""

    checks: list[BrowserCheck]

    @property
    def ok(self) -> bool:
        """只要有**一个**浏览器真能起来，就算通过。"""
        return any(check.ok for check in self.checks)

    @property
    def failures(self) -> list[BrowserCheck]:
        return [check for check in self.checks if not check.ok]

    def first_ok(self) -> BrowserCheck | None:
        for check in self.checks:
            if check.ok:
                return check
        return None

    def render(self) -> str:
        lines = [check.line() for check in self.checks]
        lines.append("")
        good = self.first_ok()
        if good is not None:
            lines.append(f"结论：可以用 {good.name} 打开浏览器登录（{good.path}）。")
            for check in self.failures:
                if check.found:
                    lines.append(
                        f"注意：{check.name} 起不来 —— 安装时若选了某个浏览器，"
                        "请改用它，或在设置面板的「PDF 浏览器」里填上面那个能用的路径。"
                    )
                    break
        else:
            lines.append(
                "结论：本机这些浏览器都起不来，所以「用浏览器登录」暂时用不了。"
                "常见原因：安全软件（火绒、360 之类）拦下浏览器进程或本地调试端口。"
            )
            lines.append("可以先关掉拦截软件再跑一次；也可以改用「直接粘贴饼干登录」。")
        return "\n".join(lines)

    def to_json(self) -> str:
        return json.dumps(
            {
                "ok": self.ok,
                "checks": [check.to_dict() for check in self.checks],
                "usable": (self.first_ok().name if self.first_ok() else ""),
            },
            ensure_ascii=False,
            indent=2,
        )


def _short_reason(exc: BaseException) -> str:
    """把 :class:`CdpError` 的长文案压成一句结论（第一句到句号为止）。"""
    text = str(exc).strip() or type(exc).__name__
    for mark in ("。", "\n"):
        head, sep, _ = text.partition(mark)
        if sep:
            return head + mark if mark == "。" else head
    return text


def _exit_code_from(message: str) -> int | None:
    """从「刚起来就退出了（退出码 21）」里把 21 抠出来。"""
    import re

    match = re.search(r"退出码\s*(-?\d+)", message)
    return int(match.group(1)) if match else None


def _browser_signature(port: int, timeout: float = 3.0) -> str:
    """问一下调试端口的 ``/json/version``，返回「名字/版本」；问不到就返回空串。

    这一步不能省：端口文件出来**不等于**调试服务能连（v0.13.2 修的就是这段时差）。
    实测把 ``cmd.exe`` 当浏览器传进来时，它被 Chromium 那串参数当命令跑，退得比主线程
    第一次 ``poll()`` 还快 —— 只看 ``poll()`` 会把它判成「可以」。
    """
    from .cdp import CdpError, _http_json

    try:
        data = _http_json(f"http://127.0.0.1:{port}/json/version", timeout=timeout)
    except (CdpError, OSError):
        return ""
    if not isinstance(data, dict):
        return ""
    return str(data.get("Browser") or "")


def check_one(
    info: BrowserInfo,
    timeout: float = DEFAULT_TIMEOUT,
    *,
    root: Path | None = None,
) -> BrowserCheck:
    """真的把 ``info`` 这个浏览器启一次，看它能不能把调试端口写出来、并且接口答得上话。

    ``root`` 只在测试里用（指定临时资料目录的父目录），正常情况下交给系统临时目录。

    **这里故意不换备用资料目录**（和「用浏览器登录」那条路不一样）：那条路要的是
    「想尽办法让用户登进去」，而这条路要的是**如实回答这一个浏览器行不行**。
    换目录会连着换浏览器，最后报出来的「可以」可能是另一个浏览器 —— 而且实测踩过：
    假浏览器起不来时会退到用户真正的 ``browser-profile``，那里的旧端口文件让一个
    根本不是它的浏览器答了话，于是判成「可以」。
    """
    started = time.monotonic()
    profile = Path(
        tempfile.mkdtemp(prefix="xdao-browser-check-", dir=str(root) if root else None)
    )
    browser = LoginBrowser(info, profile, "", timeout=timeout)
    browser.fallback_profiles = False
    result: BrowserCheck
    try:
        browser.start()
        elapsed = time.monotonic() - started
        # 端口写出来了也可能当场就死（比如读到的是上一次留下的旧端口文件、或者
        # 来的根本不是浏览器），所以再看一眼进程还在不在、接口答不答话 ——
        # 「端口在、口不通」正是「用浏览器登录」失败的那个形态。
        # 必须在 stop() **之前**看：stop() 会把它 terminate 掉。
        code = browser.process.poll() if browser.process is not None else None
        signature = _browser_signature(browser.port) if code is None and browser.port else ""
        if code is not None:
            result = BrowserCheck(
                name=info.name,
                path=str(info.path),
                ok=False,
                detail=f"{EXITED_EARLY}（退出码 {code}），端口写出来了进程却没留住。",
                port=browser.port,
                seconds=elapsed,
                exit_code=code,
            )
        elif not signature:
            result = BrowserCheck(
                name=info.name,
                path=str(info.path),
                ok=False,
                detail=(
                    f"调试端口 {browser.port} 写出来了，但连不上它"
                    "（进程还在，接口不答话）。"
                ),
                port=browser.port,
                seconds=elapsed,
            )
        else:
            result = BrowserCheck(
                name=info.name,
                path=str(info.path),
                ok=True,
                detail=signature,
                port=browser.port,
                seconds=elapsed,
            )
    except BaseException as exc:  # noqa: BLE001 —— 起不来的原因全都要如实报出来
        elapsed = time.monotonic() - started
        text = f"{type(exc).__name__}: {exc}" if not isinstance(exc, CdpError) else str(exc)
        result = BrowserCheck(
            name=info.name,
            path=str(info.path),
            ok=False,
            detail=_short_reason(exc) if isinstance(exc, CdpError) else text,
            seconds=elapsed,
            exit_code=_exit_code_from(str(exc)),
        )
    browser.stop()
    shutil.rmtree(profile, ignore_errors=True)
    return result


def check_browsers(
    explicit: str = "",
    *,
    timeout: float = DEFAULT_TIMEOUT,
    limit: int = DEFAULT_LIMIT,
    env: dict | None = None,
    progress: Callable[[str], None] | None = None,
) -> BrowserReport:
    """依次试几个候选浏览器，返回第一个「能起来」的结论。

    第一个是**用户在设置里指定的那个 / 系统默认浏览器**（和「用浏览器登录」走的是
    同一个挑选逻辑），所以这份结论能直接回答「我为什么点不动」。
    """
    head = find_browser(explicit or None, env)
    checks: list[BrowserCheck] = []
    if head is None:
        checks.append(
            BrowserCheck(
                name="浏览器",
                path=explicit,
                ok=False,
                found=False,
                detail="没找到 Edge、Chrome、Chromium 或 Brave。",
            )
        )
        return BrowserReport(checks)
    candidates: list[BrowserInfo] = [head]
    for info in browser_candidates(head):
        if info.path == head.path:
            continue
        if all(info.path != seen.path for seen in candidates):
            candidates.append(info)
    for info in candidates[: max(1, limit)]:
        if progress is not None:
            progress(f"正在试 {info.name}（{info.path}）…")
        check = check_one(info, timeout)
        checks.append(check)
        if check.ok:
            break  # 有一个能用就够了，不必把机器上每个浏览器都启动一遍
    return BrowserReport(checks)
