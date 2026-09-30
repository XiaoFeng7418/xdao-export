"""打包版启动浏览器时要补的开关（源码运行不需要）。

**为什么要单独一个模块**：启动浏览器有两条路 —— 无头打印 PDF（``exporters/pdf.py``）
和用户登录窗口（``browser_login.py``）。这个开关必须两条路都补，写在其中一个模块里
另一个就得反向导入，索性独立出来，也让两种启动方式的用例有同一个落点。

**它解决的问题**：PyInstaller 打包出来的 exe 直接启动 Chrome/Edge 时，浏览器在读参数阶段
就被系统中断，退出码 ``2147483651``（``0x80000003``，``STATUS_BREAKPOINT``），
一个字节的 PDF 都不产出。2026-10-01 在冻结环境里逐一排除过：

| 试过的做法 | 结果 |
|---|---|
| 直接 ``Popen`` / ``subprocess.run``（现状） | 崩 |
| 关掉本进程所有可继承句柄再启动 | 崩 |
| ``CREATE_BREAKAWAY_FROM_JOB``（跳出作业对象） | 崩 |
| ctypes ``CreateProcessW``（``bInheritHandles=False``） | 崩 |
| 经 ``cmd /c start`` 两段启动 | 崩 |
| 经 PowerShell ``Start-Process`` 启动 | 崩 |
| ``SetDllDirectory(None)`` 清掉捆绑目录优先级 | 崩 |
| 给子进程一份最小环境块 | 崩 |
| 旧无头模式 / 关 GPU 渲染完整性 / 关各层沙箱 | 崩 |
| **``--no-sandbox``** | **正常出 PDF** |

也就是说：跟句柄继承、作业对象、启动方式、环境变量、DLL 搜索路径都无关 ——
崩的是 Chromium 的沙箱层在「父进程是冻结程序」时的初始化，只有整体关掉沙箱能绕过。

**两条路都补，但症状不一样（2026-10-01 冻结真机各量了一遍）**：

- 无头打印 PDF：浏览器进程直接被打断，退出码 ``2147483651``（``STATUS_BREAKPOINT``），
  一个字节都不产出；
- 登录窗口（有头）：**进程其实起得来**、调试端口也写得出来、CDP 连得上，
  但渲染进程起不来 —— 页面停在既不提交也不报错的空白状态（地址与标题长期为空），
  ``Page.captureScreenshot`` 一直回 ``Internal error``。也就是说窗口可能看得见，
  但登录页永远加载不出来。补上开关后：地址落在登录页、标题
  「用户登录 - User System - X岛揭示板」、截图 27,910 字节。

所以「加了才能用」对两条路都成立，只是「不加」的表现一个是进程崩、一个是页面废。

**安全上的取舍（如实说明）**：``--no-sandbox`` 会关掉渲染进程的沙箱。这里可以接受，因为
本程序让浏览器渲染的只有自己刚生成的本地 HTML（串内容来自 X 岛接口，不执行页面里的脚本），
登录窗口打开的也只有 X 岛登录页；用完就把进程收掉。不关沙箱的结果不是「更安全」，
而是「打包版根本出不了 PDF、登录页也加载不出来」。
源码运行（``python main.py``）走的仍是带沙箱的默认设置，**这个开关只在冻结环境注入**。
"""

from __future__ import annotations

import sys

#: 冻结环境必须补的开关。原因见模块开头那张表。
FROZEN_EXTRA_FLAGS = ("--no-sandbox",)


def is_frozen() -> bool:
    """当前是否跑在 PyInstaller 打出来的可执行文件里。

    不看 ``sys.executable`` 的名字（不牢靠），只看 PyInstaller 引导程序会设的
    ``sys.frozen``。
    """
    return bool(getattr(sys, "frozen", False))


def launch_flags(*, frozen: bool | None = None) -> list[str]:
    """返回「这次启动浏览器要多带的参数」。

    ``frozen`` 显式传值时以它为准（用例就是这么验的：不必真的去打包一遍），
    不传就按当前进程算。
    """
    if frozen is None:
        frozen = is_frozen()
    return list(FROZEN_EXTRA_FLAGS) if frozen else []
