# 工作交接说明（X岛串导出工具）

> 更新时间：2026-09-23。本文档用于更换模型后快速接手，继续开发和维护。

## 一、项目概述

Windows 桌面小工具：登录 X 岛（nmbxd1.com）后，把某一个串完整导出为本地文件，突破游客只能看前 100 页的限制。

已实现功能：

- 账号密码 + 5 位验证码登录，登录状态本地保存；支持手动粘贴 userhash 兜底。
- 登录后自动应用饼干，拿到 userhash，读取完整内容。
- 一次可导出多个串，每个串单独一个文件。
- 抓取范围：所有人发言 / 只抓 PO 发言。
- 导出格式：HTML（图片嵌入，可打印/转 PDF）/ TXT（纯文本，图片以链接保留）。
- PO 明显高亮，所有发言人饼干（user_hash）完整显示，时间、No.、正文、图片保留。
- 标题和作者（name）非默认值时显示；默认“无标题”“无名氏”不显示、不留空位。
- 文件名优先用标题，无标题取第一楼正文第一句话。
- 现代扁平 UI、渐变标题头、卡片分区、圆角进度条、右键菜单、窗口可缩放。

## 二、当前状态

核心功能已全部实现并实测通过：登录、应用饼干、抓取、HTML/TXT 导出、文件名、UI、进度显示。
打包版 exe 已生成，并上传到 GitHub Release v0.1.0。

## 三、文件结构

```
work\xdao-export\
├─ main.py          # 入口（支持 --selftest 自检）
├─ README.md
├─ .gitignore
├─ HANDOFF.md       # 本文件
└─ xdao\
   ├─ __init__.py
   ├─ client.py     # 网络：登录、应用饼干、取串、翻页、下载图片
   ├─ fetcher.py    # 网址解析、翻页抓取、停止条件
   ├─ builder.py    # HTML/TXT 生成、文件名推导、并发下载图片
   ├─ gui.py        # Tkinter 界面（现代样式、ModernProgress）
   └─ settings.py   # 本地配置持久化（userhash、导出目录）
```

## 四、关键技术要点

### 1. X岛接口

- API 基础：`https://www.nmbxd1.com/Api`（备用 `https://api.nmb.best/api`）。
- 取串：`/thread?id=串号&page=页码`，返回 JSON；主帖在顶层，回复在 `Replies`。
- 字段：`user_hash`（饼干）、`name`、`title`、`now`、`content`（HTML）、`img`、`ext`、`admin`、`sage`、`id`。
- Tips 酱：`id=9999999` 或 `user_hash="Tips"`，解析时需过滤。
- 图片 CDN 前缀来自 `getCDNPath`（默认 `https://image.nmb.best/`）；完整图 `image/{img}{ext}`，缩略图 `thumb/{img}{ext}`。
- 翻页：每页约 19 条真实回复 + 主帖 + Tips，顺序从新到旧；停止条件 = 当前页无新增，或用 `ReplyCount+1` 作目标数。
- PO = 主帖的 `user_hash`。

### 2. 登录与饼干

- 登录页 `Member/User/Index/login.html`，POST 字段 `email`、`password`、`verify`、`__hash__`（CSRF 从 `<meta name="__hash__">` 提取）。
- 验证码图 `Member/User/Index/verify.html`，5 位，不分大小写；登录失败会换验证码。
- 登录成功返回“登陆成功”并设置 `memberUserspapapa` cookie，但不会直接给 `userhash`。
- 应用饼干：`Member/User/Cookie/index.html` 列饼干 → `switchTo/id/{id}.html` 切换 → `export/id/{id}.html` 取 userhash 值。
- 最终 `userhash` cookie 需设置到多个域：`nmbxd1.com`、`.nmbxd1.com`、`api.nmb.best`、`.api.nmb.best`。

### 3. GUI 线程（重要坑）

- `App` 不是 Tk 组件，必须用 `self.root.after(...)`，不能用 `self.after(...)`，否则界面日志/进度不刷新。
- 后台线程通过 `queue.Queue` + 主线程 `after` 轮询更新 UI（登录和导出都是这个模式）。

### 4. 打包（PyInstaller）

- 使用自装 Python：`C:\Users\14515\Documents\Codex\python3129\python.exe`（Python 3.12.9）+ PyInstaller 6.22.2。
- 打包前必须设置环境变量：
  - `TCL_LIBRARY=C:\Users\14515\Documents\Codex\python3129\tcl\tcl8.6`
  - `TK_LIBRARY=C:\Users\14515\Documents\Codex\python3129\tcl\tk8.6`
- 命令：`python -m PyInstaller --onefile --windowed --clean --name "X岛串导出工具" main.py`
- 注意：`python3129\Lib\site-packages\PyInstaller\utils\hooks\tcl_tk.py` 已被手动打过补丁（利用 TCL_LIBRARY/TK_LIBRARY 环境变量绕过本机 Tcl 检测问题）。换环境打包时若 Tcl 正常，可还原该补丁。
- 产物：`work\xdao-export\dist\X岛串导出工具.exe`，并复制到 `outputs\X岛串导出工具.exe`。

## 五、Git / GitHub

- 仓库：https://github.com/XiaoFeng7418/xdao-export
- Release v0.1.0（含 exe）：https://github.com/XiaoFeng7418/xdao-export/releases/tag/v0.1.0
- 提交作者已设为私密邮箱 `XiaoFeng7418@users.noreply.github.com`（避免暴露真实邮箱）。
- gh 便携版：`work\ghcli\bin\gh.exe`
- 推送注意：本机 git 配置了 `http.proxy=127.0.0.1:7890`（可能失效），推送时用：
  `git -c http.proxy= -c https.proxy= push`
- 已添加 safe.directory：
  `git config --global --add safe.directory C:/Users/14515/Documents/Codex/2026-09-05/w-x/work/xdao-export`

## 六、隐私注意

- 不要在任何文件里写入用户的 X岛账号、密码或 QQ 邮箱。
- 登录状态（userhash）保存在 `%APPDATA%\xdao-export\config.json`，属本机敏感信息，不进 git 和工作包。

## 七、后续可做（暂未实现）

- 无人值守登录 / 验证码识别。
- 定时抓取、监控串更新。
- 导出 PDF 文件（当前用浏览器打印即可）。
- 断点续传。
