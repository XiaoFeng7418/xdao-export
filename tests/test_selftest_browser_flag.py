# -*- coding: utf-8 -*-
"""``--selftest --check-browser`` 这条组合开关真跑一遍（用真浏览器进程）。

为什么要单开一个文件：这条路的重点是「主程序把它们接起来了没有」—— 光测
:func:`xdao.preflight.browser_start_checks` 只说明那一段会写字，说明不了
``main.py`` 真的把 ``--check-browser`` 当成了自检里的一条（而不是当成那条单独的
命令又跑一遍）、也说明不了失败会体现在**退出码**上（用户和脚本都看退出码）。

真实浏览器在这里是**故意用假的路径**（一个不存在的 exe）：它和「安全软件把浏览器
拦下来」在程序看来是同一个形态 —— 起不来、报退出码/系统错误。这样用例既不需要
真的启动浏览器，又跑的是真代码路径。
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import main as main_module
from xdao import preflight


def _isolate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """把「本机环境」那几项钉成假的，别去读真配置、更别真往用户目录写。"""
    settings = SimpleNamespace(
        output_dir=str(tmp_path / "导出"),
        cache_dir=str(tmp_path / "缓存"),
        pdf_browser="",
    )
    monkeypatch.setattr(preflight, "_loaded_settings", lambda: settings)
    monkeypatch.setattr(preflight, "can_write_dir", lambda directory: True)
    monkeypatch.setattr(preflight, "_check_config_file", lambda path: preflight.Check(
        name="配置文件", status="ok", detail="能读：1 项设置"
    ))
    monkeypatch.setattr(preflight, "_check_exporters", lambda: preflight.Check(
        name="导出格式", status="ok", detail="可用：html、pdf"
    ))
    monkeypatch.setattr(preflight, "_check_cache_dir", lambda cache_dir: preflight.Check(
        name="缓存目录", status="ok", detail="能写"
    ))
    monkeypatch.setattr(
        main_module,
        "check_update",
        lambda json_output=False: pytest.fail("自检不该去查更新"),
    )


def _fake_find(monkeypatch: pytest.MonkeyPatch, path: Path, name: str = "假浏览器") -> None:
    """让**探测模块自己**去找这个假浏览器。

    钉 ``preflight.find_browser`` 没用：``browser_check`` 是 `from .browser_login import
    find_browser`，拿的是它自己那份引用（第一次写就踩了 —— 真去启动了本机的 Edge）。
    """
    from xdao import browser_check
    from xdao.browser_login import BrowserInfo

    monkeypatch.setattr(
        browser_check,
        "find_browser",
        lambda explicit, env: BrowserInfo(name=name, path=str(path)),
    )


def _run(monkeypatch: pytest.MonkeyPatch, argv: list[str]) -> tuple[int, str]:
    """按给定命令行跑一次 ``main.main``，把标准输出收回来。"""
    monkeypatch.setattr(main_module.sys, "argv", ["main.py", *argv])
    return main_module.main()


def test_deep_selftest_fails_and_says_why_when_the_browser_cannot_start(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _isolate(monkeypatch, tmp_path)
    from xdao import browser_check

    _fake_find(monkeypatch, tmp_path / "不存在.exe")
    monkeypatch.setattr(
        browser_check,
        "check_one",
        lambda info, timeout: browser_check.BrowserCheck(
            name=info.name,
            path=str(info.path),
            ok=False,
            detail="刚起来就退出了（退出码 21），端口写出来了进程却没留住。",
            exit_code=21,
        ),
    )
    code = _run(monkeypatch, ["--selftest", "--offline", "--check-browser"])
    out = capsys.readouterr().out
    assert code == 1, "浏览器起不来时自检必须以「不行」收场（脚本和用户都看退出码）"
    assert "浏览器启动·假浏览器" in out
    assert "退出码 21" in out, "失败原因必须落在输出里，不能只有一句「不行」"
    assert "浏览器启动" in out, "要有一条收尾结论，别让用户自己拼"
    assert "用浏览器登录" in out, "结论要说清这条路被卡住了"
    assert "直接粘贴饼干登录" in out, "还得给一条退路"


def test_deep_selftest_passes_exit_zero_when_a_browser_really_starts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """能起来时退出码必须是 0 —— 否则「加了一条会失败的检查」会把正常的机器也判成坏。"""
    _isolate(monkeypatch, tmp_path)
    from xdao import browser_check

    _fake_find(monkeypatch, tmp_path / "存在.exe")
    monkeypatch.setattr(
        browser_check,
        "check_one",
        lambda info, timeout: browser_check.BrowserCheck(
            name=info.name, path=str(info.path), ok=True, detail="Fake/1.0", port=9222, seconds=0.5
        ),
    )
    code = _run(monkeypatch, ["--selftest", "--offline", "--check-browser"])
    out = capsys.readouterr().out
    assert code == 0
    assert "这条路是通的" in out
    assert "Fake/1.0" in out


def test_plain_selftest_does_not_start_any_browser(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """不加 ``--check-browser`` 时自检仍然是「只读」的：不许启动浏览器进程。"""
    _isolate(monkeypatch, tmp_path)
    from xdao import browser_check

    def boom(*args, **kwargs):
        raise AssertionError("默认自检不许启动浏览器")

    _fake_find(monkeypatch, tmp_path / "存在.exe")
    monkeypatch.setattr(browser_check, "check_one", boom)
    monkeypatch.setattr(browser_check, "check_all", boom)
    code = _run(monkeypatch, ["--selftest", "--offline"])
    out = capsys.readouterr().out
    assert code == 0
    assert "浏览器启动" not in out


def test_the_deep_selftest_json_carries_the_new_check(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--selftest-json --check-browser`` 也要带上这一条（贴给别人看的那种输出）。"""
    _isolate(monkeypatch, tmp_path)
    from xdao import browser_check

    _fake_find(monkeypatch, tmp_path / "存在.exe")
    monkeypatch.setattr(
        browser_check,
        "check_one",
        lambda info, timeout: browser_check.BrowserCheck(
            name=info.name, path=str(info.path), ok=True, detail="Fake/1.0", port=9222, seconds=0.5
        ),
    )
    code = _run(monkeypatch, ["--selftest-json", "--offline", "--check-browser"])
    out = capsys.readouterr().out
    payload = json.loads(out)
    names = [item["项目"] for item in payload["检查结果"]]
    assert any(name.startswith("浏览器启动") for name in names)
    assert code == 0
