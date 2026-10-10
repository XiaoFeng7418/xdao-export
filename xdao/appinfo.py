"""程序身份与打包/升级契约的事实来源。

为什么要有这个模块：同一件事原先散在好几处 —— 产品名写在通知与打包脚本里各一份、
免安装包的名字在「打包」与「查询新版本」里各拼一遍、仓库 slug 在更新检查与 CI 工具里
各写一遍、窗口标题还被 ``tools/gui_probe.py`` 手抄了一份。改一处漏一处就会出现
「通知里一个名字、标题里另一个」「打出来的包名与查询按的名字对不上」这类只能靠人
盯着的错。这里收着三类事实：

* **产品身份** —— 产品名、窗口标题、GitHub 仓库 slug、版本号与 User-Agent；
* **打包/升级的字符串契约** —— 升级包目录前缀、平台标记、启动器名、附件名、校验后缀；
* **版本号读取** —— 本程序自己的 :data:`VERSION` / :func:`version`，以及读**任意一份
  仓库副本**的 :func:`read_version`（逐行读文本，不 import 那个包）。

这个模块只许依赖标准库：``xdao/updater.py``、``xdao/update_check.py``、``xdao/notifications.py``
与几个 ``tools/`` 脚本都要 import 它，别在这里引入任何重的东西（更不能 import tkinter）。
"""

from __future__ import annotations

from pathlib import Path

from . import __version__

# ---------------------------------------------------------------- 产品身份

#: 产品名：通知标题、PE 版本信息里的 ProductName、发给用户看的说明都用它。
PRODUCT_NAME = "X岛串导出工具"

#: GUI 窗口标题。必须与 ``xdao/gui.py`` 里 ``root.title(...)`` 的那个字符串一致，
#: ``tools/gui_probe.py`` 就是拿它核对「主窗口到底起没起来」的。
WINDOW_TITLE = "X岛串导出"

#: 自家仓库 slug（``owner/name``）：更新检查、发布脚本、CI 日志工具问的都是它。
GITHUB_REPO = "XiaoFeng7418/xdao-export"

#: 问 GitHub 时带的 User-Agent（带上版本号与仓库地址，方便对面认出来是谁）。
#: 仓库 slug 里已经含 ``owner/`` 那一段，所以这里只能等号后面那一根斜杠 ——
#: 写成 ``.../{GITHUB_REPO}`` 会拼出 ``github.com//XiaoFeng7418/...``，
#: 那是把升级下载与「查新版本」两处的 UA 悄悄改了（v0.13.48 前的字面量是单斜杠）。
USER_AGENT = f"xdao-export/{__version__} (+https://github.com/{GITHUB_REPO})"

# ---------------------------------------------------------------- 打包契约

#: 升级包目录名与免安装包名的前缀，后面接版本号。
PAYLOAD_PREFIX = "xdao-export-v"

#: 免安装包目录名里那段平台标记（打包、查询附件名都按它拼）。
PLATFORM_TAG = "-win64"

#: 启动器文件名：包内那个 exe，升级换完文件后拉起来的也是它。
LAUNCHER_NAME = "xdao-export.exe"

#: 校验附件（``*.sha256``）的后缀，附件名就是「它描述的那个文件名 + 这个后缀」。
SIDECAR_SUFFIX = ".sha256"

# ---------------------------------------------------------------- 版本号

#: 本程序当前的版本号。真源仍是 ``xdao/__init__.py`` 里的 ``__version__``
#: （``tests/test_version_consistency.py`` 按它核对文档里四处落点），这里只是转发。
VERSION = __version__


def version() -> str:
    """本程序当前的版本号（等价于 ``xdao.__version__``）。"""
    return VERSION


def payload_root_name(version: str) -> str:
    """升级包的外层目录名，例如 ``xdao-export-v0.13.48``。"""
    return f"{PAYLOAD_PREFIX}{version}"


def payload_dir_name(version: str) -> str:
    """升级包解出来的那层目录，也是 zip 里的顶层目录：``xdao-export-v0.13.48-win64``。"""
    return f"{PAYLOAD_PREFIX}{version}{PLATFORM_TAG}"


def asset_name(version: str) -> str:
    """免安装包（Release 附件）的文件名：``xdao-export-v0.13.48-win64.zip``。"""
    return payload_dir_name(version) + ".zip"


def read_version(repo: Path) -> str:
    """逐行读 ``repo/xdao/__init__.py`` 里的 ``__version__``（**不 import** 那个包）。

    打包脚本要按**手里这份仓库副本**的版本号命名产物，所以这里收一个仓库根目录；
    ``tools/build_zip.py`` 原本就是这么做的（不 import 是为了不把整个包加载起来）。
    读不到就抛 :class:`SystemExit` —— 打包脚本靠它停下来。
    """
    init = Path(repo) / "xdao" / "__init__.py"
    for line in init.read_text(encoding="utf-8").splitlines():
        if line.startswith("__version__"):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise SystemExit("xdao/__init__.py 里找不到 __version__")
