# 使用指南

[返回首页](../README.md) · [English overview](../README.en.md) · [文档导航](README.md)

本页介绍当前源码的使用方式；免安装版可把命令中的 `python main.py` 换成 `xdao-export.exe`。历史版本差异见[发布说明](README.md#版本与维护记录)。例子使用官方测试串 `50000001`，`12345678` 为虚构占位编号。

## 登录与首次导出

1. 打开程序，点击「登录 / 设置饼干」。游客也能导出，但只能读前 100 页；长串应先登录。
2. 推荐「用浏览器登录」：在本程序管理的独立浏览器窗口中用 **X 岛账号**登录，完成验证码并领取可用饼干。窗口不复用日常浏览器资料，不需要登录微软账号或开启同步。
3. 等程序确认饼干可用；若浏览器不可用，可用账号密码 + 验证码，或「直接粘贴饼干登录」。粘贴整段 Cookie、`userhash=...` 或其值均可，勿公开这些内容。
4. 输入一行一个串网址或串号，也可点「从文件…」读取清单。选择范围、格式和保存目录，再开始导出。
5. 查看右侧进度与运行日志。完成后可打开导出目录；缺页或缺图时会留下 `导出报告-时间.txt`，请核对后重跑。

浏览器登录与 PDF 支持 Edge / Chrome / Chromium / Brave 等 Chromium 浏览器，**不支持 Firefox**。设置中的浏览器路径同时用于登录和 PDF，留空则自动寻找。

浅色/深色与界面字号可在主窗口右上角切换；导出或监控运行时不能切换。左栏内容装不下时可滚动，底部操作区固定可见。界面字号与导出正文字号是两个独立选项。

## 格式与图片模式

| 格式 | 适合什么 | 图片与限制 |
| --- | --- | --- |
| HTML | 离线阅读、浏览器打印、单文件分享 | 内嵌、链接、丢弃、同名目录四种模式 |
| PDF | 打印与归档 | 需要本机 Chromium 浏览器；目录模式按内嵌处理，浏览器不可用时改存 HTML |
| EPUB | 阅读器与电子书 | 内嵌、链接、丢弃；目录模式按内嵌处理，不支持多串合并 |
| Markdown | 二次编辑、笔记与版本管理 | 默认保留链接，可选同名目录或丢弃；内嵌模式按链接处理 |
| TXT | 简单文本备份 | 图片保留链接，无排版字号选项 |

- `embed`：下载并内嵌图片，可离线看图；图片多时成品较大。
- `url`：只留原图链接，不下载图片；看图需要联网，原链接未来可能失效。
- `drop`：移除图片，不下载图片。
- `folder`：HTML / Markdown 把图保存到 `<成品名>.files/`，正文使用相对路径。移动或分享时须同时带上文件与整个图片目录。

文件名模板支持 `{title}`、`{id}`、`{date}`、`{po}`、`{count}`，例如 `[{id}] {title}`。合并导出（默认关闭）按输入顺序把多串写入同一份 HTML / PDF / Markdown / TXT；PDF 带串级书签。

## 常用命令

```powershell
# 基本导出；--format / --output 是 -f / -o 的长写
python main.py 50000001 -f html -o exports
python main.py https://www.nmbxd1.com/t/50000001 -f epub -o exports

# 一行一个串号或网址；忽略空行和 # 开头的行，重复串号只导一次
python main.py --input threads.txt -f markdown -o exports

# 只看 PO、指定饼干、限页
python main.py 50000001 --scope po --max-pages 20
python main.py 50000001 --hashes abc123,def456

# 串号截止包含该串；输入列表里有它时跳过它后面的串
python main.py 50000001 12345678 --until 50000001

# 已有成品跳过、合并、图片目录
python main.py 50000001 12345678 --skip-existing
python main.py 50000001 12345678 --merge -f html
python main.py 50000001 -f markdown --image-mode folder

# 正文字号与文件名模板
python main.py 50000001 --font-size 20 --template "[{id}] {title}"

# 完整重抓，不复用缓存
python main.py 50000001 --no-cache

# 本机诊断、浏览器启动探测和更新检查
python main.py --selftest --offline
python main.py --check-browser
python main.py --check-update
python main.py --help
python main.py --version
```

无参数启动界面；有串号时进入命令行导出。已在界面保存的登录与偏好可用于命令行；`--cookie` 可直接指定本次使用的 userhash。命令行可能被系统或 shell 记录，勿把真实凭据粘到公开日志。

## PDF 设置

```powershell
python main.py 50000001 -f pdf -o exports
python main.py 50000001 -f pdf --pdf-paper a4 --pdf-orientation landscape
python main.py 50000001 -f pdf --pdf-margin-mm 10 --pdf-scale 1.2 --pdf-pages "1-3,5"
```

浏览器路径可用 `--pdf-browser "<浏览器可执行文件>"` 指定。默认渲染超时 900 秒，`--pdf-timeout` 可调整。页面 CSS 也可能影响最终排版；默认选项尽量沿用网页打印样式。

| 参数 | 取值与作用 |
| --- | --- |
| `--pdf-paper` | `default` / `a4` / `a3` / `a5` / `letter` / `legal` |
| `--pdf-orientation` | `portrait` / `landscape` |
| `--pdf-margin` | `default` / `none` / `narrow` / `normal` / `wide` |
| `--pdf-margin-mm` | 自定义 0–50 毫米，优先于边距预设 |
| `--pdf-scale` | 0.5–2.0，默认 1.0 |
| `--pdf-no-background` | 不打印背景色和背景图，默认打印 |
| `--pdf-pages` | 最终 PDF 页码，如 `1-3,5`；不是站点串页号 |
| `--pdf-browser` / `--pdf-timeout` | 本机浏览器路径 / 渲染超时秒数 |

## 监控与列表备份

```powershell
# 每十分钟检查一轮，有新回复时自动导出
python main.py 50000001 --watch --interval 600 -f html

# 关闭通知；校验旧楼层编辑会额外请求
python main.py 50000001 --watch --no-notify --verify

# 导出、合并导入、替换导入监控列表
python main.py --watch-export watch-list.json
python main.py --watch-import watch-list.json
python main.py --watch-import watch-list.json --watch-import-replace
```

界面中打开「监控串更新」管理列表，双击条目可编辑抓取范围、格式、图片处理和最多页数。导出与监控不能同时运行。监控默认每 300 秒检查一次，同一串 15 分钟内只提醒一次；请保留合理间隔。

列表文件保存监控规则，不包含上次检查进度。默认导入与现有列表合并，只有明确使用 `--watch-import-replace` 才替换。定期备份列表即可在换机后恢复设置。

## 其余 CLI 参数速查

以 `python main.py --help` 的输出为准；PDF 参数见上一节。

| 参数 | 作用 |
| --- | --- |
| `-h` / `--help` | 显示帮助 |
| `-V` / `--version` | 显示版本 |
| `-f` / `--format` | `html` / `pdf` / `txt` / `markdown` / `epub` |
| `-o` / `--output` | 导出目录 |
| `--input` | 从文本清单读串号/网址，可与命令行输入合并 |
| `--scope` / `--hashes` | `all` 或 `po` / 指定发言饼干筛选 |
| `--max-pages` | 每串最多页数，0 = 不限 |
| `--until` | 串号截止（含它）；列表中没有该串时按串号大小筛选 |
| `--page-workers` | 同时抓页数 1–6，默认 2；限流时自动退回顺序抓取 |
| `--font-size` | HTML / PDF / EPUB 正文字号 10–28（CSS 像素），默认 14 |
| `--template` | 文件名模板 |
| `--skip-existing` / `--merge` | 跳过非空同名成品 / 合并多串导出，默认均关闭 |
| `--image-mode` | `embed` / `url` / `drop` / `folder`，适用格式见上表 |
| `--no-cache` / `--cache-dir` | 完整重抓 / 指定缓存目录 |
| `--cookie` | 本次登录 userhash，覆盖本机保存值 |
| `--verify` | 校验已缓存楼层是否编辑，每轮额外请求 |
| `--watch` / `--interval` | 持续监控 / 检查间隔秒数（默认 300，最小 15） |
| `--no-notify` | 监控时不弹桌面通知 |
| `--watch-export` / `--watch-import` | 导出 / 合并导入监控列表 |
| `--watch-import-replace` | 配合导入，替换现有列表 |
| `--selftest` / `--selftest-json` | 本机与联网自检 / 输出 JSON |
| `--offline` | 配合自检只查本机；不是全局断网开关 |
| `--check-browser` / `--check-browser-json` | 真正启动浏览器探测 / 输出 JSON；会使用临时资料目录 |
| `--check-update` / `--check-update-json` | 向 GitHub 查新版本 / 输出 JSON |

## 配置与缓存

- Windows 配置文件：`%APPDATA%\xdao-export\config.json`，包含登录饼干、导出偏好和监控列表；没有 `APPDATA` 时使用 `~/.xdao-export/`。饼干未加密，不应共享这份文件。
- 默认缓存在导出目录下的 `.cache/`，可在设置或 `--cache-dir` 指定。`threads/` 保存抓取状态，`pages/` 保存页面，`images/` 保存图片，`watch/` 保存监控进度。
- 页面和图片缓存用于断点续传与增量更新；清空缓存不会删除已经导出的成品，下次需要重新下载。
- 缓存目录不可写时会尝试本机备用目录并记日志；也可用 `--no-cache` 关闭。
- 手动指定的导出目录不可写时，先修正该目录或换到允许写入的位置。不要把「能读目录」当作「当前程序能写目录」。

## 常见问题

### Windows 拦截或无法启动

发布包目前未签名，可能显示「未知发布者」或 SmartScreen 警告。请先确认来自[官方发布页](https://github.com/XiaoFeng7418/xdao-export/releases/latest)并比对 `.sha256`；摘要匹配不等于程序已签名。保留完整的 exe 与 `_internal` 目录。[签名现状](SIGNING.md)。

源码无法打开界面时，检查 Python 是否包含 tkinter 与可用的 Tcl/Tk；`python main.py --help` 能运行并不能证明 GUI 环境正常。命令行使用不要求启动图形界面。

### 登录后仍无法读取长串

查看日志是否提示饼干失效，重新登录或粘贴有效的 `userhash`。浏览器登录使用本程序管理的独立资料目录，不会继承日常浏览器的登录状态；通常使用临时目录，也可能复用本程序已有窗口或保留站点登录状态的备用目录。账号登录成功与已领取可用饼干是两件事，按站点提示完成领取。

浏览器找得到但启动失败时，运行 `python main.py --check-browser`，或在自检窗口点击「试浏览器」。这会真实启动一次浏览器；检查日志中的退出码及安全软件的拦截记录，不要关闭整机安全防护。

### PDF 结果变成 HTML

浏览器不可用时会保留 HTML，并在日志说明原因。确认 Edge / Chrome 能启动、路径正确；也可打开 HTML 后用浏览器打印为 PDF。PDF 的纸张页码与站点页码不是同一回事。

### 缺页、缺图或被限流

先看运行日志与 `导出报告-时间.txt`，等待后用原范围重跑，保留缓存以复用已抓内容。图片无法下载时通常保留原链接，成功生成文件不代表所有图片都已内嵌。请勿通过减小请求间隔或增加并发持续撞限流。

### 写不进去或打开目录失败

换到允许当前程序写入的目录，检查磁盘空间、安全软件的文件保护和系统受控文件夹设置。随包工具 `诊断写入-双击运行.cmd` 会运行 `诊断写入.ps1` 并保存 `诊断结果-*.txt`；它测的是 PowerShell 的写入权限，与应用进程的权限可能不同。

若「打开目录」失败，可把日志中的导出路径粘到资源管理器地址栏。不要为排查权限问题分享真实配置、Cookie 或未脱敏的目录报告。

### 提交可复核的问题

记录版本、Windows/Python 环境、所用导出格式与脱敏错误信息，附离线自检结果：

```powershell
python main.py --selftest --offline --selftest-json
```

联网自检使用 `--selftest`；浏览器探测使用 `--check-browser`，二者用途不同。提交前删除日志中的本机用户名、个人目录、Cookie、账号和私人串内容；不要把配置文件原件上传至 issue。
