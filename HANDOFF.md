# 项目技术说明

> 更新时间：2026-11-22（v0.24.1）。本文档汇总项目架构、模块职责、测试与构建信息。

## 一、项目概述

Windows 桌面小工具：登录 X 岛（nmbxd1.com）后，把任意一个串完整导出为本地文件，突破游客只能看前 100 页的限制。同时提供完整的命令行入口，可无界面批量导出或长期监控。

v0.24.0 起也支持 B 岛（bog.ac）公开串：网址自动识别且无需登录；纯数字和 X 岛网址仍走 X 岛。`xdao/sites/` 的适配器负责输入识别、分页请求、通用 `Post` 归一化、图片 URL 与页数估算；缓存、监控、GUI 和导出器只使用统一模型。B 岛缓存键使用 `bog_<id>`，展示串号与导出文件名仍为纯数字；旧 X 岛缓存键不变。

已实现功能：

- 登录三选一：**用浏览器登录**（推荐：程序开一个独立的浏览器窗口，登录后经 CDP 把饼干取回来）、
  账号密码 + 5 位验证码、**直接粘贴饼干**（整段 cookie 贴进来即可，程序自己摘 userhash）；登录状态本地保存。
- 抓取范围：所有人发言 / 只抓 PO 发言；**只看指定饼干**（界面可从第一页勾选）。
- 五种导出格式：HTML（图片内嵌）、PDF、TXT、Markdown、EPUB（图片可选内嵌/链接/丢弃）。
- 文件名模板：`{title} {id} {date} {po} {count}`。
- **断点续传**：页面按串缓存，只补缺页；**增量更新**：回复数未变时只花一次请求。
- **图片缓存**：同一张图只下载一次。
- **串监控**：定时检查新回复并自动导出，可选校验老楼层是否被编辑。
- **B 岛公开串（v0.24.0）**：无需登录，支持分页、断点缓存、五种格式导出与监控；B 岛同号串使用独立缓存和监控状态。
- 失败重试（超时/429/5xx 指数退避）、失败清单与一键重跑、可配置请求间隔与代理。
- PO 高亮、红名/SAGE 标记、饼干完整显示、时间与 No. 保留；标题与发帖者字段仅在值非默认时显示。

## 二、当前状态

v0.24.1 收官打磨：复核 v0.22.1 已落实的 EPUB 单份图片存储，并补强两站点与三种图片模式的结构回归；TXT 与自动标题保留字面尖括号。缺 Tk 时完整配置加载也已有兜底，本版进一步将配色名与界面字号校验规则独立于 Tk，验证所有配置字段且不再尝试导入 Tk。中英文首页同步说明 X 岛与无需登录的 B 岛公开串导出，构建脚本核对随包第三方许可。历史审计原文保留；旧隔离目录仅做只读核对，清理步骤与设计文档快照另存本地审计产物。

功能全部实现并已实测：五种格式在真实长串（33 页 / 626 楼）上导出成功，
EPUB 结构校验通过（`mimetype` 首条且未压缩、manifest 无缺失、626 个楼层 XHTML 全部可解析）；
监控桌面通知已通过 Windows 桌面环境验证。**界面已按 v0.5.0 重做**（两栏布局 + 统一主题 + 自绘控件），
改界面前先读 `MAINTENANCE.md` 的「界面架构」一节。

单元测试 3025 项（2026-11-22 Windows 全量运行：3017 通过、8 项跳过，耗时 503.77 秒）、
其中 7 个真机或权限用例按条件跳过（需显式开启 `XDAO_BROWSER_TEST=1` /
`XDAO_LIVE_NOTIFY=1` / `XDAO_PDF_TEST=1`，或当前账户不具备符号链接权限）；另有 1 个版本 PE 资源测试需在默认 dist/ 目录下存在当前版本 exe。
其中界面相关的 316 项（`test_theme.py` / `test_window.py` / `test_gui_browser_login.py`）
在没有显示环境的机器上会自动 skip。下面那张测试表的数字由 `tests/test_docs_facts.py`
对着真实收集数把关：加删测试文件、用例数量变了，它就会红，照报红的位置改表即可。

打包版随 v0.13.0 重新构建。v0.13.0 加了**一键升级**（`xdao/updater.py`：下载到 `%TEMP%\xdao-export-update` → 只解顶层 `xdao-export-v*` 目录 → 拿**候选版本**跑`--selftest --offline` → 主线程二次确认 → 用**升级包里的那个 exe** 当帮手（`main.py --apply-update <目录> <payload> <pid> <启动程序>`，不依赖系统 Python/PowerShell）等老进程退出后换目录、启动新版、清暂存；旧目录改名成 `<名>.old-<时间戳>` 留 24 小时，每次启动同时清理；三条设计取舍＝先自检再替换、旧版本不删只改名、帮手用升级包里的 exe）。v0.12.0 加了**新版本检查**（`xdao/update_check.py`：命令行
`--check-update` / `--check-update-json`，界面「运行日志」右上角「检查更新」按钮，启动后 1.2 秒
静默问一次；结果缓存在配置目录，一天内不重复问；只读取发布页的 tag 与链接，不发送设备信息）。
v0.11.0 加了**环境自检**（`xdao/preflight.py`：`--selftest` 先查本地配置与运行条件，再检查联网状态，界面「运行日志」右上角有「自检」按钮，新增 `--selftest-json` / `--offline`），同版把 `Card` 的 `stretch=True` 补上（画布不会把内嵌窗口拉到画布高）；v0.10.1、v0.10.2 都是补丁版：前者把冻结登录的核验结论
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
├─ tools/build_zip.py      打免安装包 + 写 .sha256（CI 的 build.yml 也用它）
├─ tools/push_one_commit.py 只推本地 HEAD 一个提交（代理坏掉时用）
├─ tools/push_tag_via_api.py 把附注标签推到 GitHub（走 API），再用 fix_tag_object 对齐字节
├─ tools/patch_release_body.py 按 release id 改已发布说明的正文
├─ .test-artifacts/        测试产物目录（已提交占位文件，见"问题记录"一节）
├─ tests/
│  ├─ __init__.py          共享夹具：FakeClient、make_post、sample_thread
│  ├─ conftest.py          测试夹具 + 两侧硬守卫：用户配置只读、白名单外跳过即失败
│  ├─ test_bog_cache.py    B 岛分页归一化、多图缓存与跨站点键隔离（6）
│  ├─ test_cache.py        缓存/断点续传/增量更新/失败页补抓、并行抓页（默认两路）、单页出错不中止整趟（74）
│  ├─ test_cdp.py          CDP 协议层：帧编解码、握手校验、调试接口重试、挑页面标签、命令配对与断线（111）
│  ├─ test_client.py       客户端层：Cookie 管理、登录跳转页、userhash 解析、验证码体解包、deflate 解压、API 前缀缓存、并发节流与图片落盘（65）
│  ├─ test_browser_login.py 浏览器登录：路径发现、启动参数、DevTools 端口（只认本次启动后新写的那份，旧的不删也不认）、WebSocket 帧层、粘贴解析、每轮重挑标签（稳挂活着的非登录页）、复用活着的程序窗口（多扇时先问罐、罐里有 userhash 的优先）、进程侧普查（认自家目录、端口失联的僵尸窗先收）、盯窗横幅（带窗口号、旧横幅会被改号）、开窗前普查、第四读+读罐对账（带罐名/整罐 userhash/页清单/接=端口）、配置目录里的备用目录收尾与开机清扫（v0.13.39）、启动失败时带上浏览器自己写在 stderr 上的最后几行话（v0.13.40）、反复起不来的持久资料目录会自己改名让位（账记在目录里，偶尔一次不算，v0.13.41）、收尾前先问端口（还开着的窗连目录一起留着，v0.13.41）、整罐读并上 Storage.getCookies（F12 那一族读法，分区饼干也读得到；对账行多一段「存储读=」）（v0.13.42）、挨个问每一扇活窗口的罐（自己这只罐里没有就去看别的活窗口，对账行多一段「逐窗=」，从此分得清是同一只罐读漏了还是根本两只罐；读饼干出错也不再一声不响）（v0.13.45）、盯窗横条挪到窗口底部并可收起（登录成的那一刻自己摘掉，不再压着「我的饼干」那一排『应用』；横条与对话框都写明「程序不替你点『应用』」）、对账行补上「哑窗=」（端口哑掉的自家窗口点名）与「同罐进程=N」（正拿着这只资料目录的浏览器进程有几个）（v0.13.46）、挨个问同一只浏览器里**别的罐**（cookie 按浏览器上下文分开存，无痕窗与 Edge 的「工作区」各一份；对账行多一段「别路=」——只有浏览器端点答得出上下文名单，所以这一问特意挂上去）、等超时那一刻把原始读数落盘成「读罐详单」（含域、路径、分区键、哪一份罐，拿来比对用）（v0.13.47）、对账行再补两段「窗口=」（同一只浏览器里每个页面标签各在哪份罐里—— 真机上「程序读的那扇」与「用户眼前那扇」实际是不是同一扇，从此对得出来）与「罐路径=」（这次真正在用的资料目录**完整路径**，同名的常驻罐与一次性罐不再混淆）、「同罐进程=」数出 0 也照写（过去这一段缺席和「查了、就一只」长得一样）、上下文名单改成从 `Target.getBrowserContexts` 与 `Target.getTargets` **两处并起来**（用户自己开的无痕窗不在前者名单里，v0.13.47 的「别路=」就是在这里空的）、读罐详单里多抄「罐路径」与每个目标的上下文（v0.13.48）；v0.13.49 是仓库级重构（目录与写权限下沉 `xdao/paths.py`、导出器立底座、产品名与 GitHub 问答抽成 `xdao/appinfo.py` / `xdao/github_api.py`、删掉确认没人用的旧代码），**这条链路本身没动**；v0.13.51 添了三样：「现场行」（每十几秒把每一份罐此刻有哪些名字、每扇页面挂在哪份罐里、前一批次花了多少秒写进运行日志 —— 登录落在哪一秒、罐是不是跟着长、1.5 秒的读循环有没有被领饼干那几步顶住，事后一眼看得出）、`profile_of_process`（问命令行里那只**真正**在用的资料目录，`罐=`/`罐路径=` 不再误报配置目录那只常驻罐）、每一次候选尝试失败都在运行日志里留一行「第几扇、哪份资料目录、放弃原因」（v0.17.5）；页面自己的 localStorage/sessionStorage 里也找一次 userhash（420；v0.22.5 起补上读罐失败、换目录失败、端口文件内容损坏这些兜底分支与进程表普查的内部细节）
│  ├─ test_browser_scope.py 浏览器登录只支持 Chromium 内核：界面与两份公开文档都写着（3）
│  ├─ test_cli.py          命令行参数与入口、--selftest/--check-update/--page-workers、批量估时与缺页清单、认不出串号时的退出码（94）
│  ├─ test_config_isolation.py 用户配置守卫本身有效、配置文件字节不变、显示环境只探一次（10）
│  ├─ test_config_compat.py 旧配置与旧模板的兼容：多余键被丢弃、写错的枚举回落、模板占位符不变、监控条目字段改名（26）
│  ├─ test_public_material.py 公开材料：真串号 / 本机个人目录 / 凭据（5）
│  ├─ test_license_docs.py 开源协议 / 版权 / 免责都在，且随包发出（6）
│  ├─ test_version_consistency.py 版本号四处一致（5）
│  ├─ test_cli_docs.py     文档里的命令行开关与解析器互相覆盖（3）
│  ├─ test_docs_facts.py   HANDOFF/README 里写死的用例数与真实收集数一致（3）
│  ├─ test_browser_check.py 「浏览器实际行不行」的探测（8）
│  ├─ test_selftest_browser_flag.py 自检里那条 --check-browser 真跑一遍（4）
│  ├─ test_manual_blocks.py 使用说明的版本段：新的在上、衔接对得上、别夹整份复制（6）
│  ├─ test_text_hygiene.py 所有被跟踪的文本文件：BOM 只一个、行尾不混用、不整份翻行尾（7）
│  ├─ test_py310_syntax.py 语法兼容守卫：f-string 表达式里不许带反斜杠或跨行（4）
│  ├─ test_repo_check.py   tools/repo_check.py：读不到东西时不许说没问题（13）
│  ├─ test_make_release.py 发版：附件不在就别建 Release，发完再核一遍；另有三条盯着「取响应体的两个包装函数必须把值交出去」（v0.13.50 补）；另有一条盯着 Release 标题格式（v0.19.5 补）（40）
│  ├─ test_build_zip.py   打包脚本：版本信息、.sha256、BOM、条目顺序、文档自检、版本号判定与 PE 版本资源（36）
│  ├─ test_push_via_api.py 备用推送：提交对象逐字节重建、--exclude 写错先拦下（46）
│  ├─ test_sync_from_api.py API 重建远端历史：合并的每条支线、工作区核对（23）
│  ├─ test_ci_logs.py     取 CI 日志：红的记号、--job 拼错、--grep 没搜到都要说话（37）
│  ├─ test_posix_check.py  tools/posix_check.py：六项逐条判定，参数透传/执行位/shebang 都要真查（29）
│  ├─ test_clean_scratch.py 删东西之前先拦危险路径，链接只摘链接本身（37）
│  ├─ test_pdf_diag.py     PDF 诊断：错误页不算成功、没生成 PDF 时退出码是 1（27）
│  ├─ test_repo_info.py    仓库门面：写之前先验、写完再读回来核对（38）
│  ├─ test_gui_probe.py    GUI 探针：看见主窗口才算起来，收尾要杀进程树（41）
│  ├─ test_gui_shot.py     GUI 截图：一片同色不算截好，收尾要还 GDI 句柄（48）
│  ├─ test_gui_entry.py    界面入口、错误文案、监控列表导入导出、自检、更新与一键升级、启动清扫两种资料目录、取消导出（按钮切换与取消收尾）、监控条目编辑对话框、导出与监控互斥、成品已存在就跳过、从文件读串号清单、合并导出、编辑选中监控、主窗口角落分支、工作线程内层、监控/设置/更新与缓存的角落分支、主窗口骨架与兜底分支、并行抓页设置与透传、导出估时/缺页清单/退回记忆（196）
│  ├─ test_gui_browser_login.py 「用浏览器登录」对话框（86，需真 Tk）
│  ├─ test_bog_exporters.py B 岛多图、引用、时间戳与五种格式图片模式（54）
│  ├─ test_exporters.py    HTML/TXT/公共文本处理/文件名模板/成品已存在就跳过/合并导出/流式写盘（150；v0.13.49 起多 4 条钉住五个导出器共用的底座`Exporter`：构造签名统一、显示名与扩展名来自类属性、保存骨架由模板方法接管）
│  ├─ test_watcher.py      监控与配置（37）
│  ├─ test_watch_list.py   监控列表文件格式：导出往返、容错、合并去重（46）
│  ├─ test_epub.py         EPUB，含没标题时的书名兜底（46）
│  ├─ test_export_report.py 缺页/缺图清单：只写有问题的串、地址封顶 20 条、不留空文件（6）
│  ├─ test_human.py        时长文案：秒 / 分 / 小时，坏值与负数当 0（18）
│  ├─ test_dpi.py          高分屏：探测与夹取、间距与左栏随 DPI 变大、字号不随 DPI（10）
│  ├─ test_markdown.py     Markdown、目录模式的图片预取与去重（53）
│  ├─ test_notifications.py 桌面通知（28）
│  ├─ test_pdf.py          PDF 渲染与路由（46）
│  ├─ test_pdf_opts.py     PDF 纸张/边距/缩放/页码选项（325）
│  ├─ test_browser_flags.py 打包运行时给浏览器补的开关（7）
│  ├─ test_preflight.py    环境自检：各项检查的 ok/warn/fail 分支（39）
│  ├─ test_update_check.py 新版本检查：版本号比较、接口、缓存、代理（72）
│  ├─ test_pdf_render.py   PDF 真机渲染 4 条（默认跳过，`XDAO_PDF_TEST=1`）
│  ├─ test_updater.py      一键升级：下载、解压、候选版自检、计划、换目录、清理（60）
│  ├─ test_settings.py     配置读写、成品已存在就跳过开关、合并导出开关、同时抓页数与老配置迁移、没有 tkinter 也能读配置（117）
│  ├─ test_live_notify.py  真机通知（1，默认跳过）
│  ├─ test_theme.py        配色/字体/间距/ttk 样式、Card 拉伸（34，需真 Tk）
│  ├─ test_widgets.py      自绘控件：圆角矩形、徽章、输入框、链接、进度条（56，需真 Tk）
│  ├─ test_sites.py        站点 URL 识别与协议归一化（30）
│  ├─ test_threadlist.py   清单文件：解析、去重、注释、认不出就报错（13）
│  ├─ test_window.py       主窗口布局、队列轮询、登录对话框、常用动作、监控与保存日志、登录窗失败分支、挑选饼干与从串里读饼干、一键升级链路、登录窗坏分支与握手、读饼干兜底、导出线程内层、登录窗与大串限制的角落分支、底部按钮不再压住滚动区、图片下拉框随格式变（196，需真 Tk）
│  └─ test_dev_tools.py    仓库自带开发脚本：纯函数、用法提示、全仓静态守卫与仓库门面（ruff 配置 / CI 那一步 / 超时与打包前先测试 / issue 与 PR 模板 / 自检那步是真检查）（20）
└─ xdao/
   ├─ sites/               X 岛与 B 岛适配器；站点协议在此归一化
   ├─ __init__.py          版本号
   ├─ client.py            网络层：登录、应用饼干、取串、翻页、下图、重试、代理、响应体解包
   ├─ browser_login.py     浏览器登录：找 Edge/Chrome、DevTools 端口、进程管理、取饼干
   ├─ browser_flags.py     打包运行时给浏览器补的开关（冻结环境加 --no-sandbox）
   ├─ cdp.py               CDP 传输层（帧协议 + CDPSession），登录与 PDF 渲染共用
   ├─ cache.py             页面缓存、CachedThreadFetcher、断点续传与增量判定
   ├─ watcher.py           WatchTarget / check_once / watch_forever
   ├─ watch_list.py        监控列表的导入 / 导出（文件格式与容错）
   ├─ update_check.py      新版本检查（版本比较、接口、缓存、代理、升级包信息）
   ├─ updater.py           一键升级（下载/解压/自检/换目录/帮手，仅标准库）
   ├─ settings.py          AppSettings：本地配置读写
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

### 2. B 岛公开串适配

- `xdao/sites/bog.py` 只走公开 API：`GET https://bog.ac/api/threads/{thread_id}/{page}`。响应必须是 `type=OK`、`code=6001`，帖子字段从 `info` 读取，回复在 `reply`；`type=error` 转成抓取错误。每页 20 条回复，按 `reply_count` 估页数，不过滤 X 岛 Tips 系统帖。
- B 岛主帖/回复时间是 13 位毫秒整数，统一格式为 UTC+8；`images` 可为 `None` 或数组，每个图的原图地址为 `https://bog.ac/image/large/{url}{ext}`。原始字段在适配器内转换成通用 `Post` 与 `PostImage`，导出器/界面不能读取 B 岛 JSON 字段。
- 正文的 `>>Po.N` 与 `>>N`（纯文本或 HTML 链接）由站点适配器归一化；渲染锚点必须只链接当前实际导出的帖子集合，批量合并需避免跨站同号重复锚点。
- `cache.py` 使用通用模型写入归一化页缓存，X 岛缓存路径保持原样，B 岛路径带 `bog_`；`watcher.py` 状态键带站点前缀，旧列表没有 `site` 字段时仍按原逻辑处理。`tools/build_zip.py` 的 PyInstaller 导入图从 `main.py` 递归解析，适配器由 `xdao.cache` 显式导入。

### 3. 登录与饼干

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
  **不导航的设计依据**：站点那张「饼干切换成功!」倒计时页的落点写在页面里的 `<a id="href">` 上，
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

### 4. 缓存与断点续传（cache.py）

- 状态文件 `.cache/threads/<串号>.json`：`reply_count`、`pages`、`last_page_hash`、
  `fingerprints`（楼层 id → 内容指纹）、`last_fetch_at`。
- 页面原始数据 `.cache/pages/<串号>/<页号>.json`。
- 判定流程：先请求第 1 页 → 若 `reply_count` 与缓存一致且缓存页齐全，直接返回缓存
  （`verify_cached=True` 时额外重抓最终页做指纹比对）；否则只下载缺失页。
- **第 1 页必须写进缓存**，否则缓存永远"不完整"导致每次全量重抓（这个 bug 已修）。
- 「新增楼层」与「被编辑楼层」的区分靠 `previous_post_ids`：
  只有当前批次之前就见过、且指纹变化的才算被编辑。

### 5. 监控（watcher.py）

- `check_once` 一轮检查一个串；`watch_forever` 在后台线程里循环。
- **每个监控条目有独立的导出状态** `.cache/watch/<键>.json`，键包含
  串号-范围-格式-图片模式-饼干筛选。否则同一个串配两种格式监控时，
  第二种会因为"缓存已是最新"而永远不导出（这个 bug 已修）。
- 没有新回复时每轮只花一次请求（第 1 页）。

### 6. GUI 线程（重要坑）

- `App` 不是 Tk 组件，必须用 `self.root.after(...)`，不能用 `self.after(...)`，
  否则界面日志/进度不刷新。
- 后台线程只往 `queue.Queue` 投消息，主线程用 `after` 轮询消费。
- **监控结果队列只由主窗口消费**（`App._poll_watch` 负责写日志），
  `WatchDialog._poll` 只刷新表格，避免两个消费者互相抢消息。

### 7. 导出器约定

- 对外统一走 `exporters/__init__.py` 的 `create_exporter(format_key, client, progress=...,
  filename_template=..., image_mode=...)`，它按能力逐级回退参数。
- 新增格式：实现 `build()` / `save()`，在 `EXPORTERS` 注册表登记，复用 `_shared.py` 的公共逻辑。
- `save()` 必须支持 `include_hashes`；文件名一律走
  `render_filename(template, thread, derive_filename(thread))`。

### 8. 打包（PyInstaller）

- 使用仓库内的 `tools/build_zip.py` 构建 Windows 免安装包；脚本负责准备打包依赖、生成可执行文件和 SHA-256 附件。
- 运行时要求 Python 3.10 或更高版本；构建环境应使用项目当前支持的 Python 与 PyInstaller 版本。
- 构建后核对版本、随包许可文件与 `.sha256`，并运行 `--version` 和 `--selftest` 验证产物。

## 五、开发与构建环境说明

- 从仓库根目录运行测试：
  ```powershell
  python -X utf8 -m pytest -q
  ```
- 测试写入使用 `tests/conftest.py` 提供的 `artifacts_dir` 夹具和已登记的 `.test-artifacts/` 目录。该约定可避免测试依赖系统临时目录或受限目录的写入行为。
- Python 3.10 及更高版本受支持。运行测试时不应设置仅供 Tcl/Tk 打包使用的 `TCL_LIBRARY` / `TK_LIBRARY`，以免指向不兼容的 Tcl/Tk 版本。
- Windows PowerShell 5.1 的控制台编码可能影响中文输出；运行命令前可设置 `$env:PYTHONIOENCODING='utf-8'`，并使用 `-X utf8`。
- 若测试遗留临时文件，可先用 `python tools/clean_scratch.py --dry-run` 检查清理目标，再按工具说明处理。

## 六、Git 与 GitHub

- 仓库：<https://github.com/XiaoFeng7418/xdao-export>，默认分支为 `master`。
- 打包的 Windows 免安装包通过 GitHub Releases 发布；可执行文件和 `dist/` 不进入源码树。
- 发布流程：更新版本号、发布说明和随包使用说明；运行测试与 `tools/repo_check.py`；使用 `tools/build_zip.py` 构建包；核对 `.sha256` 后再创建 Release。
- GitHub CLI 或 API 工具均可用于已授权的仓库操作。凭据通过 GitHub CLI 登录态或 `GITHUB_TOKEN` 提供；不要将令牌、账户邮箱或机器专属配置写入仓库文件。
- 推送和发布前检查 `git status --short` 与目标分支；需保持提交对象 SHA 时使用 Git 原生推送。GitHub API 重建提交对象时，时间戳规范化可能导致提交 SHA 与本地不同，内容树仍可一致。

## 七、隐私注意

- 不要在任何文件里写入用户的 X岛账号、密码或 QQ 邮箱。
- 同理也别写真实串号与本机个人目录：例子一律用官方测试串 `50000001`，或 7 位那批明显编的编号（`7001234`、`7012345`）。
- 登录状态（userhash）保存在 `%APPDATA%\xdao-export\config.json`，属本机敏感信息，
  不进 git 和工作包。
- `.cache/` 里是抓取到的串内容，同样不进版本库（已在 .gitignore 里）。

## 八、后续可做

目前**没有**排期中的功能：「无人值守登录」已于 2026-10-04 定为不做、从路线图删掉
（验证码必须真人认一次，视觉模型对 X 岛验证码的识别率太低；定论见 `MAINTENANCE.md`
的「路线图（尚未实现）」一节）。

`MAINTENANCE.md` 的「路线图（尚未实现）」里其余那些（PDF 纸张/边距、监控列表导入导出、
桌面通知、单页补抓与重试、CI）都已经实现，见那份文件的「已完成」。
