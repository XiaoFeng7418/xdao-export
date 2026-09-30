# 工作交接说明（X岛串导出工具）

> 更新时间：2026-09-29。本文档用于更换模型后快速接手，继续开发和维护。

## 一、项目概述

Windows 桌面小工具：登录 X 岛（nmbxd1.com）后，把任意一个串完整导出为本地文件，突破游客只能看前 100 页的限制。同时提供完整的命令行入口，可无界面批量导出或长期监控。

已实现功能：

- 账号密码 + 5 位验证码登录，登录状态本地保存；支持手动粘贴 userhash 兜底。
- 抓取范围：所有人发言 / 只抓 PO 发言；**只看指定饼干**（界面可从第一页勾选）。
- 四种导出格式：HTML（图片内嵌）、TXT、Markdown、EPUB（图片可选内嵌/链接/丢弃）。
- 文件名模板：`{title} {id} {date} {po} {count}`。
- **断点续传**：页面按串缓存，只补缺页；**增量更新**：回复数未变时只花一次请求。
- **图片缓存**：同一张图只下载一次。
- **串监控**：定时检查新回复并自动导出，可选校验老楼层是否被编辑。
- 失败重试（超时/429/5xx 指数退避）、失败清单与一键重跑、可配置请求间隔与代理。
- PO 高亮、红名/SAGE 标记、饼干完整显示、时间与 No. 保留；标题与作者非默认值时才显示。

## 二、当前状态

功能全部实现并已实测：四种格式在真实长串（No.67024789，33 页 / 626 楼）上导出成功，
EPUB 结构校验通过（`mimetype` 首条且未压缩、manifest 无缺失、626 个楼层 XHTML 全部可解析）。
单元测试 219 个全部通过（离线，无需联网）。

打包版已随 v0.2.2 重新构建，Release 同时提供免安装包（zip）与单文件版（exe）。
单文件版在系统临时目录不可写的环境里会打不开（`Could not create temporary directory!`），
因此默认推荐免安装包 —— 详见 `MAINTENANCE.md` 与各版本的 Release 说明。

## 三、文件结构

```
xdao-export/
├─ main.py                 入口：GUI / 命令行导出 / 监控 / --selftest
├─ pytest.ini              测试配置（禁用 cacheprovider，产物写 .test-artifacts）
├─ README.md               使用说明
├─ HANDOFF.md              本文件
├─ tools/clean_scratch.py  清理测试残留目录（带权限问题的目录）
├─ .test-artifacts/        测试产物目录（已提交占位文件，见"踩坑"一节）
├─ tests/
│  ├─ __init__.py          共享夹具：FakeClient、make_post、sample_thread
│  ├─ conftest.py          artifacts_dir 夹具（替代 tmp_path）
│  ├─ test_cache.py        缓存/断点续传/增量更新（30）
│  ├─ test_exporters.py    HTML/TXT/公共文本处理/文件名模板（55）
│  ├─ test_watcher.py      监控与配置（25）
│  ├─ test_epub.py         EPUB（37）
│  └─ test_markdown.py     Markdown（35）
└─ xdao/
   ├─ __init__.py          版本号
   ├─ client.py            网络层：登录、应用饼干、取串、翻页、下图、重试、代理
   ├─ cache.py             页面缓存、CachedThreadFetcher、断点续传与增量判定
   ├─ fetcher.py           抓取流程与兼容层（ThreadFetcher 不带缓存）
   ├─ watcher.py           WatchTarget / check_once / watch_forever
   ├─ settings.py          AppSettings：本机配置读写
   ├─ gui.py               Tkinter 界面 + 三个对话框 + CookiePicker
   └─ exporters/
      ├─ __init__.py       EXPORTERS 注册表、create_exporter、公共函数再导出
      ├─ _shared.py        plain_text / sanitize_filename / ThreadData / derive_filename
      │                    / render_filename / render_inline_content / iter_post_image_urls
      ├─ html.py           HtmlBuilder
      ├─ txt.py            TxtBuilder
      ├─ markdown.py       MarkdownBuilder
      └─ epub.py           EpubBuilder（纯标准库手写 EPUB 3）
```

## 四、关键技术要点

### 1. X岛接口

- API 基础：`https://www.nmbxd1.com/Api`（备用 `https://api.nmb.best/api`）。
- 取串：`/thread?id=串号&page=页码`，返回 JSON；主帖在顶层，回复在 `Replies`。
- 字段：`user_hash`（饼干）、`name`、`title`、`now`、`content`（HTML）、`img`、`ext`、
  `admin`、`sage`、`id`、`ReplyCount`。
- **主帖在每一页的返回里都会出现**，合并时必须按 id 去重（`_assemble` 已处理）。
- Tips 酱：`id=9999999` 或 `user_hash="Tips"`，解析时过滤。
- 图片 CDN 前缀来自 `getCDNPath`（默认 `https://image.nmb.best/`）；完整图 `image/{img}{ext}`。
- 翻页：每页约 19～20 条回复；总页数从 `PageCount` 等字段取，取不到时用 `ReplyCount // 19 + 1` 估算。
- PO = 主帖的 `user_hash`。

### 2. 登录与饼干

- 登录页 `Member/User/Index/login.html`，POST 字段 `email`、`password`、`verify`、`__hash__`
  （CSRF 从 `<meta name="__hash__">` 提取）。
- 验证码图 `Member/User/Index/verify.html`，5 位，不分大小写。
- 登录成功后需再「应用一块饼干」：`Member/User/Cookie/index.html` → `switchTo/id/{id}.html`
  → `export/id/{id}.html` 取 userhash 值。
- 最终 `userhash` cookie 要设置到多个域：`nmbxd1.com`、`.nmbxd1.com`、`api.nmb.best`、`.api.nmb.best`。

### 3. 缓存与断点续传（cache.py）

- 状态文件 `.cache/threads/<串号>.json`：`reply_count`、`pages`、`last_page_hash`、
  `fingerprints`（楼层 id → 内容指纹）、`last_fetch_at`。
- 页面原始数据 `.cache/pages/<串号>/<页号>.json`。
- 判定流程：先请求第 1 页 → 若 `reply_count` 与缓存一致且缓存页齐全，直接返回缓存
  （`verify_cached=True` 时额外重抓最终页做指纹比对）；否则只下载缺失页。
- **第 1 页必须写进缓存**，否则缓存永远"不完整"导致每次全量重抓（这个 bug 已修）。
- 「新增楼层」与「被编辑楼层」的区分靠 `previous_post_ids`：
  只有本轮之前就见过、且指纹变化的才算被编辑。

### 4. 监控（watcher.py）

- `check_once` 一轮检查一个串；`watch_forever` 在后台线程里循环。
- **每个监控条目有独立的导出状态** `.cache/watch/<键>.json`，键包含
  串号-范围-格式-图片模式-饼干筛选。否则同一个串配两种格式监控时，
  第二种会因为"缓存已是最新"而永远不导出（这个 bug 已修）。
- 没有新回复时每轮只花一次请求（第 1 页）。

### 5. GUI 线程（重要坑）

- `App` 不是 Tk 组件，必须用 `self.root.after(...)`，不能用 `self.after(...)`，
  否则界面日志/进度不刷新。
- 后台线程只往 `queue.Queue` 投消息，主线程用 `after` 轮询消费。
- **监控结果队列只由主窗口消费**（`App._poll_watch` 负责写日志），
  `WatchDialog._poll` 只刷新表格，避免两个消费者互相抢消息。

### 6. 导出器约定

- 对外统一走 `exporters/__init__.py` 的 `create_exporter(format_key, client, progress=...,
  filename_template=..., image_mode=...)`，它按能力逐级回退参数。
- 新增格式：实现 `build()` / `save()`，在 `EXPORTERS` 注册表登记，复用 `_shared.py` 的公共逻辑。
- `save()` 必须支持 `include_hashes`；文件名一律走
  `render_filename(template, thread, derive_filename(thread))`。

### 7. 打包（PyInstaller）

- 使用自装 Python：`C:\Users\14515\Documents\Codex\python3129\python.exe`（Python 3.12.9）
  + PyInstaller 6.22.2。
- 打包前必须设置环境变量：
  - `TCL_LIBRARY=C:\Users\14515\Documents\Codex\python3129\tcl\tcl8.6`
  - `TK_LIBRARY=C:\Users\14515\Documents\Codex\python3129\tcl\tk8.6`
- 命令：`python -m PyInstaller --onefile --windowed --clean --name "X岛串导出工具" main.py`
- 注意：`python3129\Lib\site-packages\PyInstaller\utils\hooks\tcl_tk.py` 已被手动打过补丁
  （利用 TCL_LIBRARY/TK_LIBRARY 绕过本机 Tcl 检测问题）。
- **打包版尚未更新到包含新功能的版本。**

## 五、开发环境踩坑（本机特有，务必先读）

1. **文件沙箱只允许往「命令启动时已存在」的目录里写文件。**
   运行时新建的目录（包括 pytest 的 `tmp_path`、pip 的临时目录）往里写会报
   `PermissionError: [WinError 5]`。因此：
   - 测试用 `tests/conftest.py` 的 `artifacts_dir` 夹具，不要用 `tmp_path`；
   - 测试产物目录 `.test-artifacts/` 已提交进版本库。
2. **pytest 不在项目自带解释器里。** 已装进 DSH 运行时：
   `C:\Users\14515\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe`
   跑测试：
   ```powershell
   Set-Location 'D:\小玩意\xdao-export'
   $env:PYTHONIOENCODING='utf-8'
   & 'C:\Users\14515\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe' -X utf8 -m pytest -q
   ```
   不要给这个解释器设 `TCL_LIBRARY`/`TK_LIBRARY`（那是 python3129 的路径，版本冲突会导致
   tkinter 起不来）。
3. **控制台中文会显示成乱码**（GBK 代码页），加 `$env:PYTHONIOENCODING='utf-8'` 和
   `-X utf8` 即可；纯属显示问题。
4. 早期测试误写进系统临时目录，留下过带「拒绝删除」权限项的残留目录
   （`.pytest_tmp`、`.pytest-scratch`）。已经用 `tools/clean_scratch.py` 清理干净；
   若再遇到同类目录，用这个脚本处理。

## 六、Git 与 GitHub

- 仓库：https://github.com/XiaoFeng7418/xdao-export （公开，默认分支 `master`）
- 已配置：描述、话题、MIT 许可、合并后自动删除分支；Wiki 已关闭。
- Releases：
  - `v0.2.0` → 附件 `xdao-export-v0.2.0.exe`（当前版本，含四种格式与缓存/监控）
  - `v0.1.0` → 附件 `xdao-export-v0.1.0.exe`（旧版）
- 提交作者已设为私密邮箱 `XiaoFeng7418@users.noreply.github.com`。
- gh 便携版：`C:\Users\14515\Documents\Codex\2026-09-05\w-x\work\ghcli\bin\gh.exe`
  （全局 gitconfig 里已把它配成 github.com 的凭据助手，正常终端里 `git push` 不需要再输密码）。
- **打包好的 exe 不进源码树**，只作为 Release 附件发布（`.gitignore` 已排除）。

### 推送通道（2026-09-30 更新）

**本机代理已配好，`git push` 可以直接用，优先用它。** 全局配置里
`http.proxy` 与 `https.proxy` 都是 `http://127.0.0.1:7890`（7890 是 HTTP 混合端口，
写成 `https://` 会直接 TLS 失败）。分支映射已配好：本地 `main` ↔ 远端 `master`。

```powershell
Set-Location 'D:\小玩意\xdao-export'
$py = 'C:\Users\14515\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe'

# 日常推送（两边 sha 完全一致）
git push origin main:refs/heads/master

# 代理不可用时的备用通道：走 GitHub Git Data API 重建对象
& $py -X utf8 tools/push_via_api.py --repo XiaoFeng7418/xdao-export --branch master

# 把远端提交在本地逐字节重建，使两边 sha 对齐
& $py -X utf8 tools/sync_from_api.py --repo XiaoFeng7418/xdao-export --branch master --dry-run

# 发布 Release 并上传 exe 附件
& $py -X utf8 tools/make_release.py --repo XiaoFeng7418/xdao-export --tag v0.2.1 `
    --name "..." --notes-file docs/RELEASE_NOTES_v0.2.0.md --asset "dist/xdao-export-v0.2.1.exe"
```

### 两个必须知道的坑

1. **Release 附件名不要用中文。** 走 `?name=` 上传时 GitHub 会把中文名截断成单个字符
   （`X岛串导出工具.exe` 会变成 `X.exe`），所以附件统一用 `xdao-export-vX.Y.Z.exe`。
2. **走 API 推送时两边 sha 会不同，这不是历史被篡改。** GitHub 的提交对象其实
   **会保留**我们送上去的偏移量（送 `+08:00` 就存 `+0800`），是**接口读回来的日期**
   被改写成 UTC，原偏移量拿不回来。所以远端对象的 sha 无法从接口返回值复算。
   2026-09-30 已用 `git push` 把两边对齐（旧远端历史存为标签
   `backup-before-align-20260930`），细节见 `MAINTENANCE.md` 的「推送通道」一节。

## 七、隐私注意

- 不要在任何文件里写入用户的 X岛账号、密码或 QQ 邮箱。
- 登录状态（userhash）保存在 `%APPDATA%\xdao-export\config.json`，属本机敏感信息，
  不进 git 和工作包。
- `.cache/` 里是抓取到的串内容，同样不进版本库（已在 .gitignore 里）。

## 八、后续可做（暂未实现）

- 无人值守登录 / 验证码识别。
- 导出 PDF（本机有 Edge / Chrome，可用无头模式把内嵌图片的 HTML 转成 PDF）。
- 监控到更新时的桌面通知。
- 断点续传的更细粒度：单页下载失败时的自动补抓与重试队列。
- 把监控列表导出 / 导入为配置文件，方便多台机器迁移。
- 用 GitHub Actions 在推送时自动跑 `pytest`（需要先确认 runner 能装依赖）。

- 无人值守登录 / 验证码识别。
- 导出 PDF 文件（当前可用浏览器打印 HTML；本机有 Edge/Chrome，可考虑无头模式转 PDF）。
- 监控到更新时的桌面通知。
- 断点续传的更细粒度：单页下载失败时的自动补抓与重试队列。
- 把监控列表导出/导入为配置文件，方便在多台机器间迁移。
