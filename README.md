# X岛串导出工具

一个 Windows 桌面小工具：登录 X 岛（nmbxd1.com）后，把任意一个串**完整**导出为本地文件，突破游客只能看前 100 页的限制。也可以完全用命令行运行，适合脚本和计划任务。

## 功能

**导出**

- 四种格式：
  - **HTML**：图片以 base64 内嵌，单文件可离线打开、打印、另存为 PDF；
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
- 可选「校验老楼层改动」，发现被编辑的楼层时也会重新导出（每轮多一次请求）。

## 下载（打包版）

无需安装 Python，直接双击即可运行的 Windows 程序。请到本仓库的 Releases 页面下载：

https://github.com/XiaoFeng7418/xdao-export/releases

- 最新版：`xdao-export-v0.2.0.exe`（含四种导出格式、断点续传与串监控）；
- 旧版：`xdao-export-v0.1.0.exe`（只有 HTML / TXT 导出）。

> 打包好的 exe 不作为源码仓库的一部分，只通过 Releases 发布；
> 想运行最新代码也可以直接按下面的方式从源码启动。

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

# 只导出发串人的发言
python main.py 67024789 --scope po

# 只看指定饼干（多个用逗号或空格分隔）
python main.py 67024789 --hashes abc123,def456

# 文件名模板
python main.py 67024789 --template "[{id}] {title}"

# 忽略缓存，完整重抓
python main.py 67024789 --no-cache

# 监控：每 10 分钟检查一次，有新回复就导出
python main.py 67024789 68811943 --watch --interval 600 -f html

# 只做接口自检
python main.py --selftest
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
│  ├─ settings.py          本机配置持久化
│  ├─ gui.py               Tkinter 界面
│  └─ exporters/           导出器
│     ├─ _shared.py        文本处理、文件名推导、图片解析（公共基础）
│     ├─ html.py           HTML（图片内嵌）
│     ├─ txt.py            TXT
│     ├─ markdown.py       Markdown
│     └─ epub.py           EPUB 3（纯标准库实现）
├─ tools/clean_scratch.py  清理测试残留目录
└─ tests/                  182 个离线单元测试
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
