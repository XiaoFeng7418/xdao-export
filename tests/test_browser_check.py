# -*- coding: utf-8 -*-
"""把「浏览器到底行不行」这个探测钉住（``--check-browser`` 那条路）。

为什么这些用例必须用假浏览器：真 Edge 走的是它自己的单实例逻辑 —— 实测把
``cmd.exe`` 冒充浏览器传进去，它也会把完整的 Chromium 资料目录和活端口交出来，
「失败」这一半根本造不出来。所以假浏览器自己写端口文件，按剧本决定要不要
「答话」（起一个只认 ``/json/version`` 的本地 HTTP 服务）。

假货就是一个「可执行脚本 + ``python -c``」：Windows 上写成 ``.cmd``，其它平台上写成
**带 shebang 的 Python 脚本**（不写 ``.sh`` 是不想再引一层 shell 的引号规则 —— 一个
可执行脚本正是 ``Popen`` 在 POSIX 上会直接执行的东西）。不碰真浏览器、不碰网络
（那个假调试服务只绑 127.0.0.1 的临时端口）。**两个平台都要真跑**：conftest 的跳过守卫
不允许白名单外的跳过，第一版拿 ``skipif`` 把 Linux 作业打发掉，CI 两个 ubuntu 作业
直接红在「有测试被意外跳过」上。

Windows 那个 ``.cmd`` **必须用 ``write_bytes`` 写**：``Path.write_text`` 会把 ``\\n``
翻成 ``\\r\\n``，而 cmd 脚本的行尾要自己写 ``\\r\\n``，否则文件里出现 ``\\r\\r\\n``，
cmd 直接报错退出（第一次写这套用例就踩了：假浏览器「退出码 1」，查了一轮才发现）。
"""
from __future__ import annotations

import json
import os
import stat
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from xdao import browser_check
from xdao.browser_login import BrowserInfo, LoginBrowser

#: 假浏览器：把 DevToolsActivePort 写进 ``--user-data-dir`` 指的那个目录。
#: 多出来的两个参数是给它的剧本：模式（dies / deaf / live）和要写进去的端口。
_FAKE_CODE = (
    "import pathlib,sys,time; "
    "mode=sys.argv[1]; port=sys.argv[2]; "
    "profile=[a.split('=',1)[1] for a in sys.argv if a.startswith('--user-data-dir=')][0]; "
    "p=pathlib.Path(profile); p.mkdir(parents=True, exist_ok=True); "
    "(p/'DevToolsActivePort').write_text(port+chr(10)+'/devtools/browser/fake'+chr(10)); "
    "sys.exit(21) if mode=='dies' else time.sleep(30)"
)


class _VersionHandler(BaseHTTPRequestHandler):
    """只认 ``/json/version`` 的假调试服务。"""

    def do_GET(self) -> None:  # noqa: N802 —— BaseHTTPRequestHandler 的命名
        if self.path != "/json/version":
            self.send_error(404)
            return
        body = json.dumps({"Browser": "FakeBrowser/1.0"}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        return None  # 别往 stderr 里刷访问日志


def _fake_browser(work: Path, mode: str, port: str = "1") -> BrowserInfo:
    """造一个能执行、会写端口文件的假浏览器（两个平台各一种壳）。

    POSIX 上把剧本参数放进**文件名**（``fake_browser-live-9222`` → 从 ``sys.argv[0]``
    里拆出来），Windows 上靠 ``%*`` 追加到命令行末尾 —— 不管哪种，都不用管 shell 引号。
    """
    if os.name == "nt":
        # 行尾自己写 \r\n：write_text 会把 \n 翻成 \r\n，cmd 只认前者。
        wrapper = work / "fake_browser.cmd"
        wrapper.write_bytes(
            f'@echo off\r\n"{sys.executable}" -c "{_FAKE_CODE}" {mode} {port} %*\r\n'.encode(
                "utf-8"
            )
        )
        return BrowserInfo(name="假浏览器", path=str(wrapper))
    # POSIX：带 shebang 的 Python 脚本 + 执行位（Popen 直接执行它）
    # 直接写解释器路径而不是 /usr/bin/env：不挑到别的 Python 版本。
    wrapper = work / f"fake_browser-{mode}-{port}"
    wrapper.write_text(
        f'#!{sys.executable}\n'
        "import os, sys\n"
        "tail = os.path.basename(sys.argv[0]).split('-', 1)[1]\n"
        'sys.argv[1:1] = tail.split("-", 1)\n'
        + _FAKE_CODE.replace("; ", "\n")
        + "\n",
        encoding="utf-8",
    )
    # **写死 0o755**，别用 `st_mode | S_IXUSR`：Windows 上 `stat.S_IXUSR` 是 0，
    # 那种写法在本机探测不出「执行位到底有没有给」（实测在 mock 成 posix 的本地探针里
    # 权限仍是 0o666），到了 ubuntu 作业上就直接 `PermissionError`。
    # 也别在这儿断言执行位：Windows 的 stat 恒返回 0o666，断言只会把本机弄红
    # （执行位只有 ubuntu 的作业才验得了）。
    wrapper.chmod(0o755)
    return BrowserInfo(name="假浏览器", path=str(wrapper))


def test_a_browser_that_writes_the_port_then_answers_is_a_pass(artifacts_dir: Path) -> None:
    """端口文件写出来 + ``/json/version`` 答得上话 = 可以。"""
    server = HTTPServer(("127.0.0.1", 0), _VersionHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        info = _fake_browser(artifacts_dir, "live", str(port))
        result = browser_check.check_one(info, timeout=6.0, root=artifacts_dir)
    finally:
        server.shutdown()
        server.server_close()
    assert result.ok is True
    assert result.port == port
    assert "FakeBrowser/1.0" in result.detail


def test_a_browser_that_dies_after_writing_the_port_is_not_a_pass(
    artifacts_dir: Path,
) -> None:
    """进程退得比主线程第一次 ``poll()`` 还快时，只看 ``poll()`` 会误判成「可以」。

    这是实测踩到的形态（拿 ``cmd.exe`` 冒充浏览器时它比第一次 ``poll()`` 退得还快），
    所以判定必须落在「调试端口答不答话」上，这条用例钉住这一点。
    """
    info = _fake_browser(artifacts_dir, "dies")
    result = browser_check.check_one(info, timeout=6.0, root=artifacts_dir)
    assert result.ok is False
    assert result.exit_code == 21 or "连不上" in result.detail


def test_a_port_nobody_answers_on_is_not_a_pass(artifacts_dir: Path) -> None:
    """进程还在、端口文件也在，但那个口上没人答话 —— 不能算通过。"""
    info = _fake_browser(artifacts_dir, "deaf")
    result = browser_check.check_one(info, timeout=6.0, root=artifacts_dir)
    assert result.ok is False
    assert "连不上" in result.detail
    assert result.port == 1


def test_the_probe_stays_on_the_one_browser_it_was_given(artifacts_dir: Path) -> None:
    """探测要如实回答「这一个浏览器行不行」，不能换目录、更不能换浏览器。

    踩过的坑：早期版本会退到用户真正的 ``browser-profile``，那里上一次留下的端口文件
    让**另一个**浏览器答了话，于是把一个起不来的假货判成「可以」。
    登录那条路该换（用户要登进去），探测这条路不该换（用户要知道真相）。
    """
    profile = artifacts_dir / "only-this-one"
    browser = LoginBrowser(
        BrowserInfo(name="假浏览器", path=str(artifacts_dir / "不存在.exe")), profile, ""
    )
    browser.fallback_profiles = False
    with pytest.raises(Exception):  # noqa: BLE001,PT011 —— 起不来是预期的
        browser.start()
    browser.stop()
    assert browser.info.name == "假浏览器"  # 没被换掉
    assert browser._profile == profile  # 没换备用目录
    assert browser.browser_note == ""  # 没「改用」别的浏览器


def test_the_login_path_still_falls_back_when_the_probe_does_not(
    artifacts_dir: Path,
) -> None:
    """反过来钉住：登录那条路的兜底不能被这次改动关掉。"""
    plain = LoginBrowser(
        BrowserInfo(name="假浏览器", path=str(artifacts_dir / "不存在.exe")),
        artifacts_dir / "profile",
        "",
    )
    assert plain.fallback_profiles is True
