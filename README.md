# X岛串导出工具

[![tests](https://github.com/XiaoFeng7418/xdao-export/actions/workflows/tests.yml/badge.svg)](https://github.com/XiaoFeng7418/xdao-export/actions/workflows/tests.yml)

一个 Windows 桌面小工具：登录 X 岛（nmbxd1.com）后，把任意一个串**完整**导出为本地文件，突破游客只能看前 100 页的限制。也可以完全用命令行运行，适合脚本和计划任务。

## 功能

**导出**

- 五种格式：
  - **HTML**：图片以 base64 内嵌，单文件可离线打开、打印、另存为 PDF；
  - **PDF**：调用本机 Chrome / Edge 的无头模式直接渲染成 PDF，图片与中文字体都内嵌，可直接打印或归档。
    ⚠️ **打包版（Releases 里的 exe）目前无法导出 PDF** —— 打包运行时启动浏览器会被系统中断，
    此时程序会自动改存 HTML 并提示你如何打印成 PDF；需要真正的 PDF 请从源码运行
    `python main.py <串号> -f pdf`。
  - **TXT**：纯文本，图片保留链接；
  - **Markdown**：便于二次编辑、贴到博客；
  - **EPUB**：可直接导入阅读器的电子书，图片可选内嵌 / 仅链接 / 丢弃。
- 一次导出多个串，每个串单独生成一个文件。
- 抓取范围：所有人发言 / 只抓 PO 发言；也可以**只看指定饼干**（界面里可从当前串第一页里勾选）。
- 文件名可用模板控制：`{title} {id} {date} {po} {count}`，例如 `[{id}] {title}`。
- 每个发言人饼干（user_hash）完整显示，PO 高亮，红名与 SAGE 有标记。
- 标题与作者在非默认值时才显示；无标题时文件名取第一楼正文的第一句话。

**速度与稳定**

- **断点续传**：页面按串缓存，中断后再跑只补缺的页；长串第二次导出通常只花一次请求。
- **增量更新**：接口报出的回复数没变就直接用本地缓存，不重复翻页。
- **图片缓存**：同一张图只下载一次，跨次导出复用。
- **失败重试**：超时、连接错误、限流（429）、5xx 自动指数退避重试；失败串可一键重跑。
- 可配置请求间隔，避免请求过密被限流；支持 HTTP 代理。

**监控**

- 盯住若干个串，定时检查新回复并**自动导出**；没有新回复时每轮只花一次请求。
- 发现有新回复时**弹桌面通知**（带提示音），挂着监控去干别的事也不会漏掉更新；
  同一个串 15 分钟内只提醒一次。日志里同步留一行记录。
- 可选「校验老楼层改动」，发现被编辑的楼层时也会重新导出（每轮多一次请求）。

**界面**（v0.5.0 重做）

- 两栏布局：左边设置（输入串、导出目录、格式与开关），右边大块运行日志。
- 统一的配色 / 字体 / 间距 / 圆角，全部集中在 `xdao/theme.py`，亮色暗色两套调色板。
- 窗口默认 `1060x760`，最小 `940x680`（1366x768 笔记本可用）；左栏装不下时可以滚动。
- 状态胶囊、「打开目录」「清空缓存」「重试失败项」等常用动作都在手边。

## 下载（打包版）

无需安装 Python，直接双击即可运行的 Windows 程序。请到本仓库的 Releases 页面下载：

https://github.com/XiaoFeng7418/xdao-export/releases

最新版 **v0.5.0** 提供两个附件，**功能完全一样**，按环境挑一个：

| 附件 | 什么时候用 |
|---|---|
| `xdao-export-v0.5.0-win64.zip` | **推荐**。免安装包，解压后双击 `xdao-export.exe`。不做任何解压动作，受限环境也能启动；**功能最全**。 |
| `xdao-export-v0.5.0.exe` | 单文件版，只有一个文件更好携带。每次启动会把内容解压到系统临时目录。 |

> **如果单文件版弹出「Could not create temporary directory!」**：这是 PyInstaller 单文件模式的
> 启动器在 Python 代码运行前就失败了 —— 当前环境的系统临时目录不可写。程序本身没问题，
> 换用免安装包即可。
>
> 免安装包解压后，`xdao-export.exe` 与 `_internal` 文件夹**必须放在一起**，不要只把 exe 单独拷走。

> **打包版目前导不出 PDF**：PyInstaller 冻结进程启动 Chrome / Edge 时会被系统中断
> （退出码 `0x80000003`）。此时程序**不会报错卡住**，而是自动改存 HTML 并告诉你怎么打印成 PDF。
> 想要真正的 PDF，请从源码运行 `python main.py <串号> -f pdf`。

> **导出目录/缓存目录不可写也不会再失败**：缓存只是加速手段。填的缓存目录写不进去时，
> 程序会自动换到 `%LOCALAPPDATA%\xdao-export\.cache`（再不行换 `%APPDATA%`、临时目录），
> 并在日志里说明换了地方，断点续传照常工作。如果所有位置都写不进去，加 `--no-cache` 关掉缓存即可：
> `xdao-export.exe <串号> -o D:\某目录 --no-cache`。

> **出错时只会给一句人话，不再弹 traceback**（v0.3.3 起）：导出目录建不出来、路径里有一段
> 不是文件夹、配置坏了……都会得到一段说明 + 退出码 1，而不是 PyInstaller 的
> 「Unhandled exception in script」对话框。

> **监控会弹桌面通知**（v0.4.0 起）：发现有新回复时弹一条 Windows 通知 + 提示音，同一个串
> 15 分钟内只提醒一次。界面上的「监控时弹桌面通知」可以关掉，命令行用 `--no-notify`。

更早的版本：`v0.3.3`（任何错误都只给一句人话）、`v0.3.2`（缓存目录自动换地方）、`v0.3.1` / `v0.3.0`（新增 PDF 导出）、`v0.2.2` / `v0.2.0`（只有 HTML / TXT / Markdown / EPUB）、`v0.1.0`（只有 HTML / TXT 导出）。
想运行最新代码也可以直接按下面的方式从源码启动。

## 从源码运行

需要 Python 3.10+（内置 tkinter），无第三方依赖：

```powershell
python main.py
```

### 图形界面

```powershell
python main.py
```

填入串网址 →（可选）设置范围、饼干筛选、格式 → 选择导出目录 → 开始导出。

### 命令行

```powershell
# 导出单个/多个串
python main.py 67024789 -f markdown -o D:\备份
python main.py https://www.nmbxd1.com/t/67024789 https://www.nmbxd1.com/t/68811943 -f epub

# 导出 PDF（需要本机装有 Chrome 或 Edge）
python main.py 67024789 -f pdf -o D:\备份
python main.py 67024789 -f pdf --pdf-browser "C:\Program Files\Google\Chrome\Application\chrome.exe"

# 只导出发串人的发言
python main.py 67024789 --scope po

# 只看指定饼干（多个用逗号或空格分隔）
python main.py 67024789 --hashes abc123,def456

# 文件名模板
python main.py 67024789 --template "[{id}] {title}"

# 忽略缓存，完整重抓
python main.py 67024789 --no-cache

# 监控：每 10 分钟检查一次，有新回复就导出（默认弹桌面通知）
python main.py 67024789 68811943 --watch --interval 600 -f html

# 监控但不要桌面通知
python main.py 67024789 --watch --no-notify

# 只做接口自检
python main.py --selftest

# 查看版本
python main.py --version
```

未登录时也能用，但只能读到每个串的前 100 页。用 `--cookie <userhash>` 或先在图形界面里登录。

## 配置与缓存

- 配置文件：`%APPDATA%\xdao-export\config.json`（登录状态、导出偏好、网络设置、监控列表）。
  **不保存明文账号密码**，只保存 userhash 这类本机状态。
- 缓存目录：默认在导出目录下的 `.cache`（可在「设置」里改成固定位置）：
  - `.cache/threads/<串号>.json`：该串的抓取进度与楼层指纹；
  - `.cache/pages/<串号>/<页号>.json`：页面原始数据；
  - `.cache/images/`：已下载的图片；
  - `.cache/watch/`：各监控条目上次导出的状态。
- 缓存可以随时在界面上「清空缓存」，已导出的文件不受影响。

## 目录结构

```
xdao-export/
├─ main.py                 入口：图形界面 / 命令行导出 / 监控 / 自检
├─ pytest.ini              测试配置
├─ xdao/
│  ├─ client.py            网络：登录、取串、翻页、下载图片、重试与代理
│  ├─ cache.py             页面缓存、断点续传、增量更新（CachedThreadFetcher）
│  ├─ fetcher.py           抓取流程与兼容层
│  ├─ watcher.py           串监控：定时检查、按需导出
│  ├─ notifications.py     桌面通知（Windows Toast / macOS / Linux）
│  ├─ settings.py          本机配置持久化
│  ├─ theme.py             配色、字体、间距、ttk 样式（界面视觉的唯一来源）
│  ├─ widgets.py           自绘控件：圆角卡片、状态胶囊、圆角进度条
│  ├─ gui.py               Tkinter 界面
│  └─ exporters/           导出器
│     ├─ _shared.py        文本处理、文件名推导、图片解析（公共基础）
│     ├─ html.py           HTML（图片内嵌）
│     ├─ txt.py            TXT
│     ├─ markdown.py       Markdown
│     └─ epub.py           EPUB 3（纯标准库实现）
├─ tools/clean_scratch.py  清理测试残留目录
├─ tools/gui_shot.py       开发期界面截图（纯标准库，改界面后自查）
└─ tests/                  290 个离线单元测试（另 1 个真机用例默认跳过）
```

## 开发

测试全部离线运行（用假接口顶替网络），不需要联网也不需要账号：

```powershell
python -m pytest -q
```

约定：

- 新增导出格式请实现 `build()` / `save()`，并复用 `exporters/_shared.py` 里的
  `derive_filename`、`render_filename`、`iter_post_image_urls` 等公共逻辑，不要另写一份；
- 楼层筛选统一用 `ThreadData.filter_posts(scope, include_hashes)`；
- 新增格式后记得在 `exporters/__init__.py` 的 `EXPORTERS` 注册表里登记，界面与命令行会自动带上；
- 测试产物写进 `.test-artifacts/`（本机受限运行环境只允许往已存在的目录里写）。

## 说明

- 抓取会逐页请求，串很长时请耐心等待；缓存让第二次导出快得多。
- 图片全部内嵌会让 HTML / EPUB 体积很大（一个 600 多楼的图串可达数百 MB），
  体积敏感时用 TXT / Markdown，或把 EPUB 的图片模式设为「仅链接」。
- 请遵守 X 岛的使用规则，控制抓取频率，导出的内容仅供个人备份。
