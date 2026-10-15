"""开源协议、版权声明与免责声明都在，而且打包时真的跟着走。

为什么要有这份检查：LICENSE 是 MIT，正文要求「版权声明与许可声明」随软件的每一份副本
一起分发 —— 也就是说打包脚本必须把 LICENSE 放进 zip；README 与随包发出的
``packaging/使用说明.txt`` 则要把「什么协议、版权归谁、免责是什么」写给读者看。

这三处以前靠人记得，2026-10-15 起钉成用例：协议被删、被改、漏进包，CI 直接红。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import build_zip  # noqa: E402

LICENSE = ROOT / "LICENSE"
README = ROOT / "README.md"
MANUAL = ROOT / "packaging" / "使用说明.txt"

LICENSE_HEAD = "MIT License"
COPYRIGHT = "Copyright (c) 2026"
HOLDER = "XiaoFeng7418"


def test_the_license_file_is_mit_and_names_the_copyright_holder() -> None:
    text = LICENSE.read_text(encoding="utf-8")
    head = text.splitlines()[0].strip()
    assert head == LICENSE_HEAD, f"LICENSE 第一行是「{head}」，MIT 的原文应当是「{LICENSE_HEAD}」"
    assert COPYRIGHT in text, f"LICENSE 里应当有一行版权声明（{COPYRIGHT} …）"
    assert HOLDER in text, "版权声明里要写得出权利人（GitHub 账号 XiaoFeng7418）"
    assert "WITHOUT WARRANTY" in text and "LIABILITY" in text, "MIT 的免责段（WARRANTY / LIABILITY）不许删"


def test_the_readme_has_license_copyright_and_disclaimer_sections() -> None:
    text = README.read_text(encoding="utf-8")
    for heading in ("## 开源协议与版权", "## 免责声明"):
        assert heading in text, f"README 里应当有「{heading}」一节"
    assert "MIT License" in text, "README 的协议那节要写明是 MIT"
    assert "[`LICENSE`](LICENSE)" in text, "README 的协议那节要指到仓库里的 LICENSE 文件"
    assert COPYRIGHT in text, "README 的版权那节要写出版权声明"
    assert "非官方" in text and "按「现状」提供" in text, "README 的免责那节要写明非官方与「按现状提供」"


def test_the_shipped_manual_states_the_license_and_the_disclaimer() -> None:
    text = MANUAL.read_text(encoding="utf-8-sig")
    assert "开源协议与版权" in text, "随包发出的使用说明里要写清楚协议与版权"
    assert "MIT 许可" in text, "使用说明里要写明是 MIT 许可"
    assert "LICENSE.txt" in text, "使用说明要告诉读者协议全文就在同一个文件夹里"
    assert "免责声明" in text, "使用说明里要有免责声明"


def test_the_package_carries_the_license_file() -> None:
    shipped = dict(build_zip.EXTRA_FILES)
    assert shipped.get("LICENSE") == "LICENSE.txt", (
        "打包时要带上 LICENSE（包内叫 LICENSE.txt）—— MIT 要求版权声明与许可声明随副本一起走"
    )
    assert LICENSE.is_file(), "LICENSE 不在仓库里，打包时会 FileNotFoundError"
    assert "LICENSE.txt" not in build_zip.NEEDS_BOM, "LICENSE 是纯 ASCII，不该进 BOM 自检名单"
