# -*- coding: utf-8 -*-
"""「用浏览器登录」只支持 Chromium 内核这件事，必须写在用户看得见的地方。

为什么要有这份检查：这条路靠 CDP，而 CDP 只有 Chromium 内核（Chrome / Edge / Chromium /
Brave）才有 —— Firefox 是另一套协议。程序本身从来没写错过（``browser_login._CANDIDATES``
里一个 Firefox 项都没有），错的是**话没说明白**：只有 Firefox 的人会点开这个窗口一直等，
等不到还得自己猜为什么。

2026-10-02 起把这句话钉在三处公开文字里（登录窗开头、`README.md`、随包发出去的
`packaging/使用说明.txt`）。将来谁重写文案把这段丢掉，这里会红。

反过来也钉一下：候选表里**真的**没有 Firefox。哪天真加了支持，这条会红，
提醒把三处说明一起改掉 —— 而不是让文档继续写着「不行」。
"""

from __future__ import annotations

from pathlib import Path

from xdao import browser_login

ROOT = Path(__file__).resolve().parents[1]

#: 界面代码：登录窗文案与「找不到浏览器」那句都在这里。
GUI_SOURCE = (ROOT / "xdao" / "gui.py").read_text(encoding="utf-8")
README = (ROOT / "README.md").read_text(encoding="utf-8")
MANUAL = (ROOT / "packaging" / "使用说明.txt").read_text(encoding="utf-8-sig")


def test_the_login_window_says_firefox_cannot_be_used() -> None:
    """登录窗开头与找不到浏览器时的那句提示，都要点明 Firefox 不行。"""
    assert "Firefox 内核的浏览器不行，两者不是同一套内核。" in GUI_SOURCE, (
        "登录窗开头没了「Firefox 内核的浏览器不行」这句 —— 只有 Firefox 的用户会白等一场。"
    )
    assert "Firefox 走不了这条路（内核不同）。" in GUI_SOURCE, (
        "「没找到 Edge 或 Chrome」那句提示没提 Firefox：用户会以为是「没装浏览器」，"
        "而不是「这个浏览器不行」。"
    )


def test_public_documents_say_firefox_cannot_be_used() -> None:
    """README 与随包发出去的那份说明，也要写着这件事。"""
    assert "**Firefox 不行**" in README, "README.md 的「用浏览器登录」那条应当标注 Firefox 不行"
    assert "**Firefox 用不了这条路**" in MANUAL, (
        "packaging/使用说明.txt 的支持浏览器那一节应当写明 Firefox 用不了"
    )


def test_the_candidate_list_really_has_no_firefox() -> None:
    """上面那些话只在「真的不支持」时才对：候选表里不许冒出 Firefox。"""
    names = {name for name, _env, _relative in browser_login._CANDIDATES}

    assert "Firefox" not in names, (
        "候选表里出现了 Firefox —— 那三处「Firefox 不行」的说明要一起改掉。"
    )
    assert names == {"Edge", "Chrome", "Chromium", "Brave"}, (
        f"候选浏览器变成 {sorted(names)} 了：文档里「Edge、Chrome、Chromium、Brave」那句"
        "（README 与使用说明都有）要跟着改。"
    )
