# 工作交接说明（X岛串导出工具）

> 更新时间：2026-09-29。本文档用于更换模型后快速接手，继续开发和维护。

## 一、项目概述

Windows 桌面小工具：登录 X 岛（nmbxd1.com）后，把任意一个串完整导出为本地文件，突破游客只能看前 100 页的限制。同时提供完整的命令行入口，可无界面批量导出或长期监控。

已实现功能：

- 登录三选一：**用浏览器登录**（推荐：程序开一个独立的浏览器窗口，登录后经 CDP 把饼干取回来）、
  账号密码 + 5 位验证码、**直接粘贴饼干**（整段 cookie 贴进来即可，程序自己摘 userhash）；登录状态本地保存。
- 抓取范围：所有人发言 / 只抓 PO 发言；**只看指定饼干**（界面可从第一页勾选）。
- 四种导出格式：HTML（图片内嵌）、TXT、Markdown、EPUB（图片可选内嵌/链接/丢弃）。
- 文件名模板：`{title} {id} {date} {po} {count}`。
- **断点续传**：页面按串缓存，只补缺页；**增量更新**：回复数未变时只花一次请求。
- **图片缓存**：同一张图只下载一次。
- **串监控**：定时检查新回复并自动导出，可选校验老楼层是否被编辑。
- 失败重试（超时/429/5xx 指数退避）、失败清单与一键重跑、可配置请求间隔与代理。
- PO 高亮、红名/SAGE 标记、饼干完整显示、时间与 No. 保留；标题与作者非默认值时才显示。

## 二、当前状态

功能全部实现并已实测：五种格式在真实长串（33 页 / 626 楼）上导出成功，
EPUB 结构校验通过（`mimetype` 首条且未压缩、manifest 无缺失、626 个楼层 XHTML 全部可解析）；
监控桌面通知已在本机实测弹出。**界面已按 v0.5.0 重做**（两栏布局 + 统一主题 + 自绘控件），
改界面前先读 `MAINTENANCE.md` 的「界面架构」一节。

单元测试 1671 项（2026-10-02 数出来的一共这么多；本机 Windows 上 1664 通过、7 项跳过）、
7 个真机用例默认跳过（都离线，无需联网；跳过的那些要显式开关 `XDAO_BROWSER_TEST=1` /
`XDAO_LIVE_NOTIFY=1` / `XDAO_PDF_TEST=1`，还有一个要管理员权限的符号链接用例）；
其中界面相关的 106 项（`test_theme.py` / `test_window.py` / `test_gui_browser_login.py`）
在没有显示环境的机器上会自动 skip。下面那张测试表的数字由 `tests/test_docs_facts.py`
对着真实收集数把关：加删测试文件、用例数量变了，它就会红，照报红的位置改表即可。

打包版随 v0.13.0 重新构建。v0.13.0 加了**一键升级**（`xdao/updater.py`：下载到 `%TEMP%\xdao-export-update` → 只解顶层 `xdao-export-v*` 目录 → 拿**候选版本**跑`--selftest --offline` → 主线程二次确认 → 用**升级包里的那个 exe** 当帮手（`main.py --apply-update <目录> <payload> <pid> <启动程序>`，不依赖系统 Python/PowerShell）等老进程退出后换目录、启动新版、清暂存；旧目录改名成 `<名>.old-<时间戳>` 留 24 小时，每次启动顺手清理；三条设计取舍＝先自检再替换、旧版本不删只改名、帮手用升级包里的 exe）。v0.12.0 加了**新版本检查**（`xdao/update_check.py`：命令行
`--check-update` / `--check-update-json`，界面「运行日志」右上角「检查更新」按钮，启动后 1.2 秒
静默问一次；结果缓存在配置目录，一天内不重复问；只取发布页的 tag 与链接，不发本机信息）。
v0.11.0 加了**环境自检**（`xdao/preflight.py`：`--selftest` 先查本机再查联网，界面「运行日志」右上角有「自检」按钮，新增 `--selftest-json` / `--offline`），同版把 `Card` 的 `stretch=True` 补上（画布不会把内嵌窗口拉到画布高）；v0.10.1、v0.10.2 都是补丁版：前者把冻结登录的核验结论
写进文档，后者只改了两处「复述来源」的措辞，都没有功能改动），Release **只提供免安装包（zip）**。v0.10.0 修掉了
「打包版导不出 PDF」（`xdao/browser_flags.py`：冻结环境启动浏览器时加 `--no-sandbox`，
根因是 Chromium 沙箱层在冻结父进程下初始化失败；源码运行仍带沙箱），
`--pdfdiag` 诊断同步走这一套参数。**登录窗口那条路 2026-10-01 也在冻结环境里量过**：
不加开关时进程起得来但页面不提交、截图一直 `Internal error`（登录页加载不出来），
加上开关后落在登录页、标题「用户登录 - User System - X岛揭示板」、截图 27,910 字节；
写冻结探针的四个坑（`.spec` 的 `pathex`、`hiddenimports`、对照组的模块常量要还原、
stdout 编码）记在 `MAINTENANCE.md` 的「以后要再写冻结探针」一节。
v0.9.0 给串监控加了
**列表导入 / 导出**（`xdao/watch_list.py` 管文件格式：界面「串监控」里的「导出列表 / 导入列表」，
命令行 `--watch-export` / `--watch-import` / `--watch-import-replace`）：导出只带「怎么监控」，
不带监控进度（否则换机器后第一轮该出的不出），导入是**合并**、坏条目逐条给中文原因；
同版修掉「导入/导出按钮被裁到窗口外」（改成单独占一行）。v0.8.0 把 PDF 从「完全跟随
网页打印样式」变成可配置（纸张 / 方向 / 边距 / 缩放 / 背景 / 页码范围），界面的「PDF 页面」
一块、命令行 `--pdf-*` 参数、串监控的每一轮检查三处都接上了同一套选项；**全默认时输出与
上一版逐字节相同**（仍走原来的命令行渲染），改过任何一项才切到 CDP 的
`Page.printToPDF`。v0.7.0 新增「用浏览器登录」（`xdao/browser_login.py`，CDP 通道
v0.8.0 已抽到 `xdao/cdp.py` 与 PDF 渲染共用），v0.6.1 修掉「邮箱登录失败时误报未找到
可用的饼干」：
X 岛的跳转提示页（HTTP 200 + JS 跳转）以前被当成饼干列表解析，现在认得跳转页、会跟着跳，
并按落点分三种情况说清原因。写不进去的目录不再让整趟白跑：
`choose_writable_dir()` 会自动换到 `%LOCALAPPDATA%\xdao-export\导出` 并说明换了地方。
单文件版从 v0.5.1 起不再提供：它的启动器会把内容解压到系统临时目录，解压路径里带中文/
非 ASCII 字符（例如 `D:\某中文目录\…`）时会在 Python 代码运行前就弹出
`Could not create temporary directory!`，而这取决于用户把文件放在哪儿 —— 详见
`MAINTENANCE.md` 与各版本的 Release 说明。

## 三、文件结构

```
xdao-export/
├─ main.py                 入口：GUI / 命令行导出 / 监控 / --selftest
├─ pytest.ini              测试配置（禁用 cacheprovider，产物写 .test-artifacts）
├─ README.md               使用说明
├─ HANDOFF.md              本文件
├─ tools/clean_scratch.py  清理测试残留目录（带权限问题的目录；危险目标先拦下）
├─ .test-artifacts/        测试产物目录（已提交占位文件，见"踩坑"一节）
├─ tests/
│  ├─ __init__.py          共享夹具：FakeClient、make_post、sample_thread
│  ├─ conftest.py          测试夹具 + 两侧硬守卫：用户配置只读、白名单外跳过即失败
│  ├─ test_cache.py        缓存/断点续传/增量更新/失败页补抓（48）
│  ├─ test_client.py       客户端层：Cookie 管理、登录跳转页、userhash 解析、验证码体解包（23）
│  ├─ test_browser_login.py 浏览器登录：路径发现、启动参数、DevTools 端口、WebSocket 帧层、粘贴解析（222）
│  ├─ test_browser_scope.py 浏览器登录只支持 Chromium 内核：界面与两份公开文档都写着（3）
│  ├─ test_cli.py          命令行参数与入口、--selftest/--check-update（48）
│  ├─ test_config_isolation.py 用户配置守卫本身有效、配置文件字节不变（8）
│  ├─ test_public_material.py 公开材料：真串号 / 本机个人目录 / 凭据（5）
│  ├─ test_version_consistency.py 版本号四处一致（5）
│  ├─ test_cli_docs.py     文档里的命令行开关与解析器互相覆盖（3）
│  ├─ test_docs_facts.py   HANDOFF/README 里写死的用例数与真实收集数一致（3）
│  ├─ test_browser_check.py 「浏览器到底行不行」的探测（8）
│  ├─ test_selftest_browser_flag.py 自检里那条 --check-browser 真跑一遍（4）
│  ├─ test_manual_blocks.py 使用说明的版本段：新的在上、衔接对得上、别夹整份复制（6）
│  ├─ test_text_hygiene.py 所有被跟踪的文本文件：BOM 只一个、行尾不混用、不整份翻行尾（7）
│  ├─ test_repo_check.py   tools/repo_check.py：读不到东西时不许说没问题（7）
│  ├─ test_make_release.py 发版：附件不在就别建 Release，发完再核一遍（17）
│  ├─ test_push_via_api.py 备用推送：提交对象逐字节重建、--exclude 写错先拦下（32）
│  ├─ test_sync_from_api.py API 重建远端历史：合并的每条支线、工作区核对（23）
│  ├─ test_ci_logs.py     取 CI 日志：红的记号、--job 拼错、--grep 没搜到都要说话（37）
│  ├─ test_posix_check.py  tools/posix_check.py：六项逐条判定，参数透传/执行位/shebang 都要真查（29）
│  ├─ test_clean_scratch.py 删东西之前先拦危险路径，链接只摘链接本身（37）
│  ├─ test_pdf_diag.py     PDF 诊断：错误页不算成功、没生成 PDF 时退出码是 1（27）
│  ├─ test_repo_info.py    仓库门面：写之前先验、写完再读回来核对（38）
│  ├─ test_gui_probe.py    GUI 探针：看见主窗口才算起来，收尾要杀进程树（41）
│  ├─ test_gui_shot.py     GUI 截图：一片同色不算截好，收尾要还 GDI 句柄（48）
│  ├─ test_gui_entry.py    界面入口、错误文案、监控列表导入导出、自检、更新与一键升级（61）
│  ├─ test_gui_browser_login.py 「用浏览器登录」对话框（43，需真 Tk）
│  ├─ test_exporters.py    HTML/TXT/公共文本处理/文件名模板（73）
│  ├─ test_watcher.py      监控与配置（30）
│  ├─ test_watch_list.py   监控列表文件格式：导出往返、容错、合并去重（33）
│  ├─ test_epub.py         EPUB（31）
│  ├─ test_markdown.py     Markdown（41）
│  ├─ test_notifications.py 桌面通知（28）
│  ├─ test_pdf.py          PDF 渲染与路由（37）
│  ├─ test_pdf_opts.py     PDF 纸张/边距/缩放/页码选项（325）
│  ├─ test_browser_flags.py 打包运行时给浏览器补的开关（7）
│  ├─ test_preflight.py    环境自检：各项检查的 ok/warn/fail 分支（39）
│  ├─ test_update_check.py 新版本检查：版本号比较、接口、缓存、代理（62）
│  ├─ test_pdf_render.py   PDF 真机渲染 4 条（默认跳过，`XDAO_PDF_TEST=1`）
│  ├─ test_updater.py      一键升级：下载、解压、候选版自检、计划、换目录、清理（38）
│  ├─ test_settings.py     配置读写（25）
│  ├─ test_live_notify.py  真机通知（1，默认跳过）
│  ├─ test_theme.py        配色/字体/间距/ttk 样式、Card 拉伸（27，需真 Tk）
│  └─ test_window.py       主窗口布局回归（37，需真 Tk）
└─ xdao/
   ├─ __init__.py          版本号
   ├─ client.py            网络层：登录、应用饼干、取串、翻页、下图、重试、代理、响应体解包
   ├─ browser_login.py     浏览器登录：找 Edge/Chrome、DevTools 端口、进程管理、取饼干
   ├─ browser_flags.py     打包运行时给浏览器补的开关（冻结环境加 --no-sandbox）
   ├─ cdp.py               CDP 传输层（帧协议 + CDPSession），登录与 PDF 渲染共用
   ├─ cache.py             页面缓存、CachedThreadFetcher、断点续传与增量判定
   ├─ fetcher.py           抓取流程与兼容层（ThreadFetcher 不带缓存）
   ├─ watcher.py           WatchTarget / check_once / watch_forever
   ├─ watch_list.py        监控列表的导入 / 导出（文件格式与容错）
   ├─ update_check.py      新版本检查（版本比较、接口、缓存、代理、升级包信息）
   ├─ updater.py           一键升级（下载/解压/自检/换目录/帮手，仅标准库）
   ├─ settings.py          AppSettings：本机配置读写
   ├─ pdf_opts.py          PDF 选项（纸张/方向/边距/缩放/背景/页码）唯一来源
   ├─ notifications.py     桌面通知（Windows Toast 用哨兵 AUMID / macOS / Linux）
   ├─ theme.py             配色 / 字体 / 间距 / ttk 样式（界面视觉唯一来源）
   ├─ widgets.py           自绘控件：Card、StatusPill、ModernProgress、FlatText
   ├─ gui.py               Tkinter 界面 + 五个对话框（登录 / 浏览器登录 / 设置 / 饼干 / 监控）
   └─ exporters/
      ├─ __init__.py       EXPORTERS 注册表、create_exporter、公共函数再导出
      ├─ _shared.py        plain_text / sanitize_filename / ThreadData / derive_filename
      │                    / render_filename / render_inline_content / iter_post_image_urls
      ├─ html.py           HtmlBuilder
      ├─ txt.py            TxtBuilder
      ├─ markdown.py       MarkdownBuilder
      ├─ pdf.py            PdfBuilder：全默认走浏览器命令行，改过选项走 CDP
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
- **用浏览器登录（v0.7.0，`xdao/browser_login.py`）**：用系统里的 Edge / Chrome，以
  `--remote-debugging-port=0 --user-data-dir=<配置目录>\browser-profile` 打开一个**独立用户目录**
  的窗口（刻意**不加** `--headless` / `--guest` / `--incognito`：要的就是用户能看见、且登录态留得住）。
  端口读 `<profile>\DevToolsActivePort` 第一行（第二行是浏览器级 ws 路径）；WebSocket 帧层是
  **纯标准库**手写的（socket + base64 + hashlib + struct），连上 CDP 后用 `Network.getCookies`
  读 `userhash`（HttpOnly 的也读得到），一次问**好几个地址**（站点根 / 饼干页 / 当前页：
  CDP 的 `Network.getCookies` 只回「会发给这个地址」的饼干，只问一个会漏）。罐里没有时再调
  `fetch_leaf_cookie()`。**v0.13.23 起界面层固定传 `navigate=False`：一个标签页都不动**，
  只读页面状态 + 饼干罐；领饼干的正事交给 `apply_leaf_cookie_over_http()`（读整罐饼干 →
  `XdaoClient.import_cookies()` + `XdaoClient.apply_cookie()`：认「跳转提示」页 → 跟着跳 →
  `switchTo/id/{id}.html`（**v0.13.24 起先剥掉链接末尾的 `.html`／`.htm` 再拼**：列表链接本身就带后缀，不剥会拼成 `{id}.html.html` 直接 404）→ 从 `export/id/{id}.html` 的响应体里抠 userhash → 读 cookie jar
  兜底；这套 HTTP 协议从 v0.6.1 起就在线上跑）。`fetch_leaf_cookie(navigate=True)` 那条老路
  还留着，但没有界面在调它。
  **为什么不导航**：站点那张「饼干切换成功!」倒计时页的落点写在页面里的 `<a id="href">` 上，
  成功时它是**空的**，页面脚本 `location.href = href` 于是把当前地址再载一次 —— 程序一路导航，
  就会把用户的标签页留在那张永远重载的页上（用户看到的「一直无限跳转」），而 userhash 始终
  没种上（v0.13.17~v0.13.22 的真机现场都是它）。所以三条导航护栏（`BROWSER_LEAF_NAV_LIMIT` /
  `BROWSER_LEAF_RETRY_SECONDS` / `BROWSER_LEAF_CAP_HINT`）连同「导航次数」那套机制一起删了。
  **也不要改回页面里的 `fetch`**：跳转提示页的第二跳是页面脚本做的，`fetch` 永远走不到，
  饼干也就永远种不上（v0.13.15 真机上就是这样一路等到超时）。结果用
  `LeafCookie(value, detail, navigated)` 带回来，`detail` 显示在窗口那行常驻诊断上。
  拿不到时，界面还会把「浏览器里有哪些饼干（只写名字）」和 HTTP 那条路的原话拼进失败原因
  （`_jar_summary()` / `_diagnosis()`），它会跟着运行日志里那句「浏览器登录没成：…」一起落地
  —— 用户贴日志就能定位（v0.13.23 加这一段，就是为了让下一次真机报告有据可查）。
  界面侧是 `gui.py` 的 `BrowserLoginDialog`；线程只往 `queue.Queue` 投消息，主线程用 `after` 轮询，
  退出前必须停线程 + terminate 浏览器进程，不留孤儿进程（见下面「GUI 线程」一节的约定）。
- 粘贴登录的宽容解析也在 `browser_login.py`：`parse_userhash_input(text)` 支持整段 cookie
  （`a=1; userhash=ABC; b=2`）、`userhash=ABC`、裸值、带引号/换行的粘贴；`looks_like_userhash()`
  判非空、无空白、无 `;`、长度 >= 6、可打印 ASCII。界面只负责问一次、解析失败弹提示、成功就 `set_userhash`。
- 验证码接口 `verify.html` 实测把 PNG **包在 gzip 里**发回来，HTTP 头却写 `image/png`：
  `client.py` 的 `_decode_response_body()` 按 gzip 魔数兜一层（解压失败原样返回），
  `fetch_login_form()` 用最近一次响应头判断后交给界面 —— 只认 `Content-Encoding` 会漏。
  （Tk 的 `PhotoImage` 恰好能直接吃压缩字节，所以这个问题一直没暴露。）

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

- 使用自装 Python：`<本机 Python>\python.exe`（Python 3.12.9）
  + PyInstaller 6.22.2。
- 打包前必须设置环境变量：
  - `TCL_LIBRARY=<本机 Python>\tcl\tcl8.6`
  - `TK_LIBRARY=<本机 Python>\tcl\tk8.6`
- 命令：`python -m PyInstaller --onefile --windowed --clean --name "X岛串导出工具" main.py`
- 注意：`<本机 Python>\Lib\site-packages\PyInstaller\utils\hooks\tcl_tk.py` 已被手动打过补丁
  （利用 TCL_LIBRARY/TK_LIBRARY 绕过本机 Tcl 检测问题）。
- **打包版尚未更新到包含新功能的版本。**

## 五、开发环境踩坑（本机特有，务必先读）

1. **文件沙箱只允许往「命令启动时已存在」的目录里写文件。**
   运行时新建的目录（包括 pytest 的 `tmp_path`、pip 的临时目录）往里写会报
   `PermissionError: [WinError 5]`。因此：
   - 测试用 `tests/conftest.py` 的 `artifacts_dir` 夹具，不要用 `tmp_path`；
   - 测试产物目录 `.test-artifacts/` 已提交进版本库。
2. **pytest 不在项目自带解释器里。** 已装进 DSH 运行时：
   `<本机 Python>\python.exe`
   跑测试：
   ```powershell
   Set-Location '<仓库目录>'
   $env:PYTHONIOENCODING='utf-8'
   & '<本机 Python>\python.exe' -X utf8 -m pytest -q
   ```
   不要给这个解释器设 `TCL_LIBRARY`/`TK_LIBRARY`（那是本机那个 Python 3.12 的路径，版本冲突会导致
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
- gh 便携版：`<本机>\ghcli\bin\gh.exe`
  （全局 gitconfig 里已把它配成 github.com 的凭据助手，正常终端里 `git push` 不需要再输密码）。
- **打包好的 exe 不进源码树**，只作为 Release 附件发布（`.gitignore` 已排除）。

### 推送通道（2026-09-30 更新）

**本机代理已配好，`git push` 可以直接用，优先用它。** 全局配置里
`http.proxy` 与 `https.proxy` 都是 `http://127.0.0.1:7890`（7890 是 HTTP 混合端口，
写成 `https://` 会直接 TLS 失败）。分支映射已配好：本地 `main` ↔ 远端 `master`。

```powershell
Set-Location '<仓库目录>'
$py = '<本机 Python>\python.exe'

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
- 同理也别写真实串号与本机个人目录：例子一律用官方测试串 `50000001`，或 7 位那批明显编的编号（`7001234`、`7012345`）。
- 登录状态（userhash）保存在 `%APPDATA%\xdao-export\config.json`，属本机敏感信息，
  不进 git 和工作包。
- `.cache/` 里是抓取到的串内容，同样不进版本库（已在 .gitignore 里）。

## 八、后续可做（暂未实现）

与 `MAINTENANCE.md` 的「路线图（尚未实现）」保持一致（PDF 纸张/边距、监控列表导入导出、
桌面通知、单页补抓与重试、CI 都已经实现，见那份文件的「已完成」）：

- 无人值守登录：浏览器登录（v0.7.0）已经把这一步缩到只剩验证码。
  实测视觉模型对 X 岛的验证码识别率太低（三张只对一张半），短期不要做。
