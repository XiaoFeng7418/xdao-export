"""「测试绝不许碰用户真实配置」这条铁律，以及守卫本身没坏。

起因是一件真事故：2026-10-01 新增的用例没隔离配置路径（``monkeypatch`` 改
``_default_config_path`` 对 dataclass 字段无效），测试直接读写用户的
``%APPDATA%\\xdao-export\\config.json``，把登录饼干连同偏好写成了测试值 ——
那份配置拿不回来了，用户得重新登录一次。

守卫本体在 ``tests/conftest.py`` 的 ``_forbid_writing_the_real_user_config``，
拦的边界是「**写**到用户那份文件」与「**读**用户那份文件」：

- 用默认落点**构造** ``AppSettings()`` 是允许的 —— 默认路径指向用户配置这件事
  本身是要被测的行为（``tests/test_settings.py`` 里就有一条断言它）；
  把构造拦死只会打死正常用例，却挡不住任何真实写入。
- 真正危险的动作是 ``save()`` 与 ``load()``，它们必须在动文件之前报错。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from xdao.settings import AppSettings, app_config_dir


def temp_settings(path: Path) -> type[AppSettings]:
    """派生一个 ``_path`` 指向临时目录的设置类（与 tests/test_settings.py 同一手法）。

    ``_path`` 必须是 **default_factory**：``load()`` 会用 ``cls()`` 现造一个对象，
    写成普通默认值时 ``cls()`` 落在当前目录上，读的就不是这份文件了。
    """

    @dataclass
    class _TempSettings(AppSettings):
        _path: Path = field(default_factory=lambda: path)

    return _TempSettings


def test_temp_apdata_is_still_recognised_as_the_real_config(
    tmp_path, monkeypatch
) -> None:
    """守卫在「没有 APPDATA 的平台」上也必须装上（照抄库的路径规则）。

    2026-10-01 的 CI 上 Linux 两个矩阵红了三条用例，根因就是守卫的
    ``_real_user_config_path()`` 原先写成「没有 ``APPDATA`` 就返回 None」——
    Linux 上 APPDATA 本来就不存在，守卫于是整个没装，等着报错的用例全成了
    ``DID NOT RAISE``。本地 Windows 永远复现不出来。这里把 ``APPDATA``
    临时改到别处，验证落点会跟着变（而不是「有 APPDATA 就只认本机那一份」）。
    """
    monkeypatch.setenv("APPDATA", str(tmp_path))
    assert app_config_dir() == tmp_path / "xdao-export"
    assert AppSettings().config_path == tmp_path / "xdao-export" / "config.json"


def test_constructing_with_the_real_config_path_is_allowed():
    """构造本身不拦：默认落点指向用户配置是要被测的行为。"""
    settings = AppSettings()
    assert settings.config_path == app_config_dir() / "config.json"


def test_saving_the_real_config_path_is_forbidden():
    """``AppSettings().save()`` —— 事故原句，必须报错而不是默默写下去。"""
    settings = AppSettings()  # 构造允许
    with pytest.raises(AssertionError, match="真实配置"):
        settings.save()


def test_loading_the_real_config_path_is_forbidden_too():
    """``load()`` 会吞异常，守卫要让它冒出来（否则「读到默认值」看着像是正常的）。"""
    with pytest.raises(AssertionError, match="真实配置"):
        AppSettings.load()


def test_temporary_config_still_round_trips(artifacts_dir):
    """落到临时目录的用法照常工作 —— 守卫不能把正常用例一起拦掉。"""
    path = artifacts_dir / "config.json"
    settings = temp_settings(path)()
    assert settings.config_path == path, "临时配置的落点跑偏了"
    settings.pdf_paper = "a3"
    settings.save()

    assert path.exists()
    assert temp_settings(path).load().pdf_paper == "a3"


def test_the_real_config_file_is_not_touched_by_this_module():
    """这条用例本身也不许动用户配置：读一遍，文件必须没变。"""
    path = app_config_dir() / "config.json"
    before = path.read_bytes() if path.exists() else None
    with pytest.raises(AssertionError):
        AppSettings.load()
    after = path.read_bytes() if path.exists() else None
    assert before == after
