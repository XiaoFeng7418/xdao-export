# 仓库维护规范

> 本文档说明 `XiaoFeng7418/xdao-export` 的日常维护怎么做。
> 接手维护时先读它，再读 [HANDOFF.md](HANDOFF.md) 的技术交接部分。

## 每周例行（有自动提醒）

1. **体检**

   ```powershell
   Set-Location 'D:\小玩意\xdao-export'
   $py = 'C:\Users\14515\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe'
   & $py -X utf8 tools/repo_check.py --repo XiaoFeng7418/xdao-export
   ```

   14 项检查，全部 ✓ 才算健康。它会检查：仓库设置、提交同步、文件逐一致、
   版本号一致、Release 附件齐全、待办积压。

2. **跑测试**（当前基线 299 项，必须全绿）

   ```powershell
   & $py -X utf8 -m pytest -q
   ```

3. **处理告警**，对照表：

   | 告警 | 处理方式 |
   |---|---|
   | 本地有提交未推送 | `git push origin main:refs/heads/master`（优先；代理不可用时才用 `tools/push_via_api.py`） |
   | 描述与代码里的导出格式不一致 | `& $py -X utf8 tools/repo_info.py --repo XiaoFeng7418/xdao-export --apply` |
   | 话题缺少 xxx | 同上（一条命令同时修描述与话题） |
   | 版本号与 Release 不一致 | 升 `xdao/__init__.py` 的版本号 → 打包 → 发新 Release |
   | 附件名含非 ASCII | 用 `make_release.py` 改名或重传（中文名会被 GitHub 截断成单字符） |
   | 开放 issue / PR | 阅读、回复；是 bug 就修并补单元测试 |

4. **看用户反馈**：Release 下载次数、issue、Star。

## 仓库门面（描述与话题）由代码推导

仓库描述里写着「支持哪些导出格式」，而格式是会变的：v0.3.0 加了 PDF，描述却一直停在
「HTML / TXT / Markdown / EPUB」，一直到 v0.3.1 发完才被发现。**所以别再手改描述**：

- 唯一的真相是 `xdao/exporters/__init__.py` 里的 `EXPORTERS` 注册表；
- `tools/repo_info.py` 按它推导出描述与话题，`repo_check.py` 用同一份推导做体检；
- 加新格式后体检会直接报「描述与代码里的导出格式不一致」，跑一次 `--apply` 就修好；
- 想改描述文案就改 `tools/repo_info.py` 顶部的 `DESCRIPTION_TEMPLATE`（上限 350 字符，
  脚本会自己拦截超长）；想改话题就改同一个文件里的 `BASE_TOPICS` / `FORMAT_TOPICS`。

```powershell
& $py -X utf8 tools/repo_info.py --repo XiaoFeng7418/xdao-export           # 只检查
& $py -X utf8 tools/repo_info.py --repo XiaoFeng7418/xdao-export --apply   # 同步
```

## 发布新版本的完整流程

```powershell
Set-Location 'D:\小玩意\xdao-export'
$py = 'C:\Users\14515\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe'

# 1) 改版本号（xdao/__init__.py），跑测试
& $py -X utf8 -m pytest -q

# 2) 打包（只打 onedir 免安装包；单文件版自 v0.5.1 起不再提供 —— 它的启动器在
#    中文/非 ASCII 路径下会在 Python 代码运行前就失败：Could not create temporary directory!）
#    注意必须设 TCL_LIBRARY / TK_LIBRARY，否则打包出的程序缺 Tcl/Tk
$env:TCL_LIBRARY='C:\Users\14515\Documents\Codex\python3129\tcl\tcl8.6'
$env:TK_LIBRARY='C:\Users\14515\Documents\Codex\python3129\tcl\tk8.6'
$pypi = 'C:\Users\14515\Documents\Codex\python3129\python.exe'
& $pypi -m PyInstaller --onedir --windowed --clean --noconfirm --name "xdao-export" main.py

# 3) 组装免安装包（exe + _internal + 使用说明.txt），压缩成
#    xdao-export-vX.Y.Z-win64.zip

# 4) 验证产物能跑（自检退出码应为 0）
#    xdao-export.exe --version      # 应输出「X岛串导出工具 X.Y.Z」
#    xdao-export.exe --selftest

# 5) 提交并推送（2026-09-30 起本机代理可用，直接 git push 即可，两边 sha 完全一致）
$env:HTTP_PROXY='http://127.0.0.1:7890'; $env:HTTPS_PROXY='http://127.0.0.1:7890'
& git add -A; & git commit -m "..."
& git push origin main:refs/heads/master        # 本地分支叫 main，远端默认分支叫 master

# 备用通道：代理不可用时才用 API 推送（sha 会与本地不同，内容仍一致）
& $py -X utf8 tools/push_via_api.py --repo XiaoFeng7418/xdao-export --branch master

# 6) 发布
& $py -X utf8 tools/make_release.py --repo XiaoFeng7418/xdao-export --tag vX.Y.Z `
    --name "vX.Y.Z：..." --notes-file docs/RELEASE_NOTES_vX.Y.Z.md `
    --asset 'D:\小玩意\xdao-export-vX.Y.Z-win64.zip' `
    --asset 'dist\xdao-export-vX.Y.Z.exe'

# 7) 收尾：确认体检全绿
& $py -X utf8 tools/repo_check.py --repo XiaoFeng7418/xdao-export
```

## 推送通道（2026-09-30 起）

- **日常推送直接用 `git push`**。本机代理 `http://127.0.0.1:7890` 已配进全局
  `http.proxy` / `https.proxy`（两处都必须是 `http://` —— 7890 是 HTTP 混合端口，
  写成 `https://` 连它会直接 TLS 握手失败）。未设 `HTTP_PROXY` 环境变量时 git 也能走全局配置。
- 分支映射：本地 `main` ↔ 远端 `master`。已设 `branch.main.remote=origin`、
  `branch.main.merge=refs/heads/master`、`push.default=upstream`，`git push` 一条命令即可。
- 凭据：仓库级 `credential.https://github.com.helper` 指向 gh 便携版，
  **必须以 `!` 开头**（少了它 git 会去 PATH 里找一个叫 `credential-C:/...` 的程序，
  于是每次网络操作都刷一句 `is not a git command` 的警告，但不影响推送）。
- `tools/push_via_api.py` 保留为**代理不可用时的备用通道**，也可用于本地对象逐字节校验。

### 两边 sha 曾经不同 —— 根因与现状

- 现象：本地与远端各 25 条提交，**顺序、说明、tree 全部对应、文件逐字节一致**，但 sha 全不同，
  看起来像历史分叉，`repo_check.py` 也因此报「本地有 N 个提交未推送」。
- 根因（2026-09-30 用字节级证据查清，共两处，都在 `tools/push_via_api.py`）：
  1. 提交说明末尾换行被吃掉。原代码写的是
     `git("log", "-1", "--pretty=%B", sha).decode("utf-8").rstrip("\n")`，
     **少一个字节就让整条历史的 sha 全部不同**。
  2. 拼提交对象时把 ISO 写法的时间（`2026-09-30T13:20:24+08:00`）直接塞了进去，
     而 git 提交对象里必须是 `{epoch} {±HHMM}` 写法。
- 顺带查清两件事：
  - GitHub **会保留**我们送上去的时区偏移（送 `+08:00` 存下来就是 `+0800`）；
    是**接口读回来的日期**一律被改写成 `...Z`，所以原偏移量从接口侧拿不回来。
    因此「远端对象的 sha 无法用接口返回值复算」是正常现象，**不代表历史被篡改**。
  - `git log --pretty=%B` 会给**没有结尾换行**的说明擅自补一个 `\n`，
    而本地历史里两种形态都有（25 条里有 7 条无尾换行：`6749e5bc` `67847a7b` `a7e00042`
    `bc94f2fa` `93fb0f53` `6d0b59aa` `5e7713e3`）。所以消息必须用
    `git cat-file commit` 原样取。修好后本地 25 条提交可以**逐字节复算校验，25/25 通过**。
- 处置：先用 `git push` 把远端旧历史备份成标签
  `backup-before-align-20260930`（本地对应分支 `backup-remote-master-20260930`，
  指向旧远端尖端 `d17e8bd4`），再 `git push --force-with-lease` 把本地对象原样推上去，
  两边 sha 从此完全一致（对齐后远端 master = `f58bf55`）。
- 若将来又出现「文件一致但提交计数不一致」，先按下面两步自查，别急着重推：
  1. `python -X utf8 tools/ci_logs.py` 之类的接口脚本能否读出远端链，
     再用 `tools/push_via_api.py` 里的 `local_commits()` 复算本地 sha（应 25/25 通过）；
  2. 确认不是真的漏推后，用 `git fetch origin && git log --oneline origin/master` 直接对比。

## 巡检发现的真实情况（2026-09-30）

- 远端历史上因多次重跑推送脚本、以及一次修正提交（`git commit --amend`），
  留下了若干**内容相同的重复提交**。这不影响代码 ——
  **判断同步是否正常的依据是「文件」一项**：本地与远端逐字节一致即正常。
- 由此带来一个已知的**误报**：体检里可能出现「本地有 N 个提交未推送」，
  但同时「文件逐文件一致」。这是重复副本造成的计数偏差，不是真的没推。
  `find_pushed_prefix` 已改为取**最长连续匹配**以减少误报，
  但远端链里同名副本过多时仍可能差一两个。
- 结论判据：`文件` ✓ + `版本` ✓ + `发布` ✓ 即可认为同步正常，
  不必因为「提交」一项的计数偏差去重推（重推只会再造一个副本）。
- **上述历史分叉已于 2026-09-30 消除**（见上一节）：远端 master 与本地完全对齐，
  「提交」一项现在也是 ✓，计数偏差不再出现。

## 硬性约定

1. **Release 附件名一律用 ASCII**（`xdao-export-vX.Y.Z-win64.zip` / `.exe`）。
   走 `?name=` 上传时 GitHub 会把中文名截断成单个字符。
2. **打包好的 exe 不进源码树**（`.gitignore` 已忽略根目录 `*.exe` 与 `dist/`），
   只作为 Release 附件。
3. **优先用 `git push`**。走 API 推送时远端提交对象由 GitHub 生成，接口读回来的日期
   被改写成 UTC，原偏移量拿不回来，所以两边 sha 会不同（**内容仍完全一致**，不是历史被篡改）。
   想让 sha 一致只能用 `git push` 把本地对象原样送上去。
4. **每次发布都要能跑**：`--selftest` 退出码 0，最好再做一次真实串导出。
5. **改动必须带测试**：`tests/` 是 299 项离线用例，新增功能请补用例，
   不要依赖联网测试。
6. 本机 git 的 HTTPS 传输不可用（schannel / openssl 都被拦），
   一切远端操作走 `tools/` 下的 API 脚本。
7. **涉及浏览器的验证要在受限模式外跑**：PDF 导出会启动 Chrome / Edge，
   而浏览器需要命名管道通信，沙箱内必然失败（`mojo ... platform_channel` 报拒绝访问）。
   这是环境限制，不是代码问题。

## 已知限制：打包版无法导出 PDF（2026-09-30 查明）

**现象**：免安装包 / 单文件版执行 `-f pdf` 时，浏览器子进程返回
`2147483651`（`0x80000003`，STATUS_BREAKPOINT），PDF 无法生成；
而源码运行（`python main.py -f pdf`）完全正常。

**已排除的原因**（都实测过，全部不是）：

| 假设 | 结果 |
|---|---|
| Tcl/Tk 环境变量指向打包目录、与系统版本冲突 | 去掉后仍崩 |
| 继承的环境变量有问题 | 传干净环境仍崩 |
| 工作目录在打包目录，DLL 搜索命中打包的运行库 | 换干净工作目录仍崩 |
| 用管道捕获输出导致句柄继承问题 | 改为重定向到文件仍崩 |
| 进程树继承问题 | 经 `cmd.exe` 代启仍无效 |
| 完全脱离句柄继承 | 用 ctypes 直接 `CreateProcessW`（无句柄继承）后 `WaitForSingleObject`，浏览器仍不产出 |
| Chrome 自身有问题 | 从**命令行手工**用完全相同的参数启动，正常生成 PDF（32 KB） |

**结论**：从 PyInstaller 冻结进程创建浏览器子进程时，Chrome 会在启动阶段被系统中断，
属于打包运行时的系统级限制，本项目无法绕过。

**应对**（v0.3.1 起）：

- 打包版遇到这种情况会**自动降级**：把内容存成 HTML，并在日志里写明
  「在浏览器里打开它按 Ctrl+P 另存为 PDF」；
- 需要真正的 PDF 时，用源码运行：`python main.py <串号> -f pdf -o <目录>`；
- 错误信息里会显示 `STATUS_BREAKPOINT` 与降级提示，便于以后排查。

**如果以后要重新尝试**：先验证打包版能否启动浏览器（`--pdfdiag` 隐藏开关，
源码里在 `tools/pdf_diag.py`）。能启动就说明限制解除了，可以把降级改回直接报错。

## 缓存目录写不进去不再挡住宿主功能（v0.3.2，2026-09-30）

**用户报的问题**：导出到 `D:\X岛` 时报
`[Errno 13] Permission denied: 'D:\X岛\.cache\.xdao-write-probe'`，
而 0.1.0 版反而能正常写入。

**原因**：0.1.0 没有缓存层，只往导出目录本身写文件；0.2 之后把「缓存目录可写」
变成了前置条件，于是**缓存写不进去 → 整个导出在抓取前就退出**，看起来像"目录没权限"。
用户拿到的错误文案里说的是缓存，而真正写不进去的可能是导出目录本身。

**改法**（`xdao/cache.py` + `xdao/exporters/_shared.py`）：

- `can_write_dir(dir) -> bool`：轻量探测，失败返回 False，不抛异常；
- `cache_dir_candidates(preferred)`：首选 → `%LOCALAPPDATA%\xdao-export\.cache` →
  `%APPDATA%\xdao-export\.cache` → `tempfile.gettempdir()\xdao-export\.cache`，去重保序；
- `resolve_cache_dir(preferred) -> (Path, note)`：能写就原样返回；否则换第一个可写的
  候选并给出说明文案；全都不可写时返回首选，让上层按原逻辑报错；
- `CachedThreadFetcher.cache_note` 与 `CachedThread.cache_warning` 把「换了地方」
  一路带到命令行表头与界面日志；
- `main.py` / `xdao/gui.py` 的表头与缓存信息行显示的是**真正在用**的目录。

**验证要点**（以后回归时照做）：

1. 首选目录能写 → `resolve_cache_dir` 原样返回、note 为空；
2. 首选写不进去 → 换到能写的地方，且**缓存真的落在那边**
   （`pages/<id>/1.json`、`threads/<id>.json` 存在，第二次导出是「下载 0 页」）；
3. 所有候选都写不进去 → 报错，且**一个请求都不发**（`api.requests == []`）；
4. 打包版在输出目录不可写时要报 `OutputDirNotWritable: 导出目录不可写：<目录>`，
   而不是把锅甩给缓存目录。

**注意**：`D:\X岛` 是用户自己的目录，别把测试文件留在里面。

## 入口处不许漏出 traceback（v0.3.3，2026-09-30）

**用户报的问题**：打包版弹出 PyInstaller 的「Unhandled exception in script」对话框，
内容是 `PermissionError: [WinError 5] 拒绝访问。: 'D:\Windows'`，
traceback 指向 `main.py` 的 `output_dir.mkdir(parents=True, exist_ok=True)`。

**原因**：那行 `mkdir` 没有 try/except。异常穿过 `run_cli`（`main()` 当时只是
`return run_cli(args)`），在 windowed 打包版里被 PyInstaller 启动器接住并展示成对话框。

**改法**：

- `main.py` 的 `mkdir` 包 `try/except OSError` → `XdaoError`，文案含路径、`strerror`、
  错误码和「请换一个当前账户可写的目录」；**`raise ... from None`** 才能不带出 traceback 链；
- `main()` 的 `if args.threads:` 分支整体 try：`KeyboardInterrupt → 130`、
  `XdaoError → 1`、其他 `Exception → 打印类型与信息 + issue 地址 → 1`；
- `if __name__ == "__main__":` 是最后一道网，连参数解析和启动界面都包住，
  `SystemExit` 原样放行，其余转成 `启动失败：…` + 提示删 `config.json`；
- `xdao/gui.py`：`start()` 在抓取前先 `ensure_writable(output_dir)`，不可写就弹对话框返回；
  导出线程体改名 `worker_body()`，外面套 `worker()` 兜住一切并投一条日志 + `done` 事件；
  监控线程同样兜底（`监控出错：…`）。

**回归要点**：

1. `python main.py 50000001 -o <一个文件> -f txt` → 退出码 1、stderr 含「导出目录没法创建」、
   **不含 `Traceback`**；
2. `main_module.run_cli` 抛任意异常时 `main()` 返回 1 且同样不含 `Traceback`
   （见 `tests/test_cli.py` 的两个用例）；
3. 界面里点「开始导出」，输出目录不可写时要**立刻**弹说明对话框，不能等抓取跑完；
4. 打包版验收时故意拿一个不可写的输出目录跑一次，确认没有 traceback 对话框；
5. `tests/test_gui_entry.py`：`tk.Tk()` 失败 / `App` 初始化失败都要走 `_report_fatal`
   并以退出码 1 结束；`run()` 必须给 `root.report_callback_exception` 装处理器
   （**打包版没有 stderr**，Tk 回调异常默认就是往 stderr 打印的，
   于是会一路穿到 PyInstaller 启动器变成错误对话框）；
6. 打包版 `xdao-export.exe --version` 必须是可读中文（由 `main._open_utf8_console()` 保证），
   不能是 `X������������ 0.3.3` 这种乱码。
7. 打包版**还要真的能输出**：GUI 子系统的 exe 从 cmd 里跑 `--version`，光设 UTF-8 不够，
   得先 `AttachConsole(ATTACH_PARENT_PROCESS)` 再重开标准流（`main._attach_parent_console()`，
   v0.4.0 加）。验收方式：

   ```powershell
   cmd /c '"D:\...\xdao-export.exe" --version > out.txt 2>&1'
   Get-Content out.txt   # 期望「X岛串导出工具 0.4.0」+ 空的一行，退出码 0
   ```

   不加 `> out.txt` 直接在 pwsh 里 `& $exe --version` 是**验证不了**的：GUI 子系统进程
   接不上 pwsh 的管道，只会得到空输出和一句 `OSError: [Errno 22] Invalid argument`。

**怎么验收打包版的界面**：本机 `Start-Process -PassThru` 对 GUI 进程会卡住不返回，
改用 `python tools/gui_probe.py --exe <exe 路径>` —— 启动、等 9 秒、枚举窗口标题、
强制结束并给出结论；标题是「Unhandled exception in script」就说明启动失败。
把输出目录设成用户的 `D:\X岛`（config 里的默认值）时最容易暴露界面层的兜底问题。

## 桌面通知（v0.4.0，2026-09-30）

监控发现新楼层时会弹系统通知。这一块有两条**实测得来、不知道就会做错**的结论：

1. **Windows 必须用哨兵 AppUserModelID**。Windows 只给「注册过 AppUserModelID」的程序
   显示通知；直接 `CreateToastNotifier("X岛串导出工具")` 时命令返回 0、看起来一切正常，
   但通知中心**什么都收不到**（本机对照实验：A 用哨兵 ID 能看到，B 用应用名看不到）。
   所以 `xdao/notifications.py` 里 `SENTINEL_AUMID =
   {1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe`，
   不要图省事改回应用名。
2. **Toast 默认静音**，所以弹完还要 `winsound.MessageBeep()` 补一声；提示音失败
   （没音频设备）不影响「通知已发出」这个结论。

其它约定：

- 通知只走 `Notifier.notify()`，**任何异常都必须吞掉返回 False**：通知发不出去绝不能
  影响导出。测试用注入 `runner` / `platform` / `beep` 代替真的弹窗。
- 节流按「监控条目键」算（`xdao.watcher.target_key`），默认 900 秒；
  `settings.notify_interval` 可调。
- **「首次导出」不提醒**（`result.first_run and result.new_posts <= 0`）：用户一次加十个
  监控不该弹十条通知。
- 界面上通知在主线程 `_poll_watch` 里发（那里已经有日志和队列），监控线程不碰 GUI。
- 想验证真的能弹：`XDAO_LIVE_NOTIFY=1 python -m pytest tests/test_live_notify.py -q -s`
  （连真实接口抓 No.50000001，伪造 3 个新楼层，跑完「判定 → 导出 → 通知」全链路）。

## 界面架构（v0.5.0 重做，改界面前必读）

- **视觉只有一份来源**：`xdao/theme.py`。配色（`LIGHT`/`DARK` 两套 `Palette`）、
  字体探测（`resolve_fonts`）、字号常量、间距 `gap(n)`（一档 4px）、圆角半径、
  ttk 样式全部在这里；`xdao/gui.py` 里**不要再写死色值**，也不要直接写像素间距。
- **自绘控件在 `xdao/widgets.py`**：`Card`（圆角卡片，内容放 `.body`）、
  `StatusPill`（状态胶囊）、`ModernProgress`（圆角进度条）、`FlatText`、`SectionHeading`。
  全是 `tk` 级控件（不是 ttk），因为要自定义绘制。
- **主窗口是两栏**：左栏固定宽 `theme.SETTINGS_COLUMN_WIDTH`（452），右栏吃满剩余宽度。
  左栏必须 `pack_propagate(False)` + `grid` 里配 `minsize`：里面的滚动画布会把自己的
  实际宽度当成请求宽度，不锁死的话 grid 会把右栏挤成一条缝（实测只剩 39px）。
- **左栏滚动区**（`App._scroll_area`）的画布初始 `width`/`height` 必须写死
  （`SETTINGS_COLUMN_WIDTH` / `SETTINGS_VIEWPORT_HEIGHT`）：不写死时画布请求高度会
  跟着内容反复变，出现"holder 265 / canvas 528"这种不一致。
- **`Card` 的宽度会跟着内容变**：内层画布默认宽 378，早期版本从不调宽度，
  导致所有用卡片装内容的对话框被钉死在 410px、长文字只能裁掉。现在
  `_on_body_configure` 会按内容宽度（含子控件的 `wraplength`）撑开画布。
- **字体只在有窗口时定案**：`theme.resolve_fonts(root)` 探测系统字体并缓存结果；
  没有窗口时（导入期、纯 Tcl 测试）只返回候选里的第一个、**不落缓存** ——
  否则导入期绑定的族名会一路粘住，出现"控件用 Consolas、主题报 Cascadia Mono"
  （CI 上真挂过）。`gui.setup_style()` 会在 `apply_theme` 之后调 `gui.refresh_fonts()`
  重算模块级别名，所以**新增字体常量时也要放进 `refresh_fonts`**，否则又会分叉。
- **线程模型没变**：`App` 不是 Tk 组件，任何定时回调都必须 `self.root.after(...)`；
  后台线程只往 queue 投结果；监控队列只由 `App._poll_watch` 消费。
- **改完界面怎么自查**：`python tools/gui_shot.py [输出.png] [--width N] [--height N]`
  （纯标准库 GDI 截图）。抓图必须用 `root.winfo_id()`，**不要 `GetParent`** ——
  那会把标题栏算进去，整张图内容上移 31px，看着像元素被截断。
- **回归用例**：`tests/test_theme.py`（配色/间距/样式/字体缓存）与 `tests/test_window.py`
  （两栏比例、底部进度条在默认与最小窗口下都完整可见、控件字体与主题同源）。
  四个坑：① `tk.Tcl()` 纯 Tcl 解释器里**没有 ttk 包**（报 `invalid command name "ttk::style"`），
  测样式必须用真 `tk.Tk()`；② 窗口 `withdraw()` 之后 Tk 不算几何，
  `winfo_ismapped()` 和宽度全是 0，测试里要把窗口挪到屏幕外而不是隐藏；
  ③ 读控件字体要用 `widget.tk.splitlist(...)` —— 族名带空格时 `cget("font")`
  返回 `'{Cascadia Mono} 9'`，直接 `split()` 会把族名截成 `'{Cascadia'`；
  ④ 窗口搬出屏幕后 Windows 偶尔要过一拍才映射，断言宽度前用
  `wait_visible()` 等一下，不要硬断。
  `tests/test_window.py` 整组共用一个根窗口（本机连开十几个 Tk 根窗口偶发创建失败），
  并把 `AppSettings.load()` 换成隔离配置，**不许碰用户真实的 `%APPDATA%` 配置**。
- **CI 的 Windows runner 桌面只有 1024x768**：请求 1060 宽的窗口会被系统夹到 1028，
  右栏与日志框比本地窄。窗口尺寸相关的断言一律按最小尺寸（940x680）来写，
  别假设"我要多少就有多少"。

## 路线图（尚未实现）

- 无人值守登录 / 验证码识别
- 单页失败时的自动补抓与重试队列
- 监控列表的导入 / 导出
- PDF 的页边距 / 纸张大小可配置（目前沿用网页的打印样式）
- 暗色配色已经写好（`theme.DARK`）但还没做切换入口

## 已完成

- 四种格式（HTML / TXT / Markdown / EPUB）＋ **PDF**（v0.3.0，走本机浏览器无头渲染）
- 断点续传、增量更新、图片缓存
- 串更新监控
- **监控桌面通知**（v0.4.0）：有新楼时弹 Windows 通知 + 提示音，同串 15 分钟一次
- **界面重做**（v0.5.0）：两栏布局、统一主题（`theme.py`）、自绘控件（`widgets.py`）、
  四个对话框统一风格；`CardFaint.TLabel` 缺失与 `Card` 死宽度两个老问题一并修掉
- **不再用探针文件拦路**（v0.5.1）：`ensure_writable` 只在目录连创建都做不到时失败，
  探针写不动只当预警 —— 修掉「0.1.0 能导出、0.5.0 说目录不可写」那类误报；
  同时停发在中文路径下打不开的单文件版
- **写权限往下探一层**（v0.5.2）：`can_write_dir()` 既试建探针文件也试建临时子目录，
  两级都成功才算可写。教训来自 `D:\X岛\.cache` 建得出来、`.cache\pages` 拒绝访问 ——
  只探表层会把这种目录判成"能写"，于是"写不进去就换地方"的兜底永远不触发。
  推论：**任何"换个能写的地方"的探测，都必须探到实际要创建的那一层**，否则兜底是装饰。
  同版把写盘失败的提示改成可执行的（指出目录、建议换到文档目录、提示查安全软件白名单）
- 命令行入口与 `--selftest`
- **CI**：每次推送/PR 自动跑离线测试（`.github/workflows/tests.yml`），
  Linux 3.10/3.12 + Windows 3.12 三个环境；打 tag 时额外校验版本号与 tag 一致
- 仓库维护脚本（体检 / 推送 / 发布 / 同步 / 清理）
- **错误处理收敛**（v0.3.3）：入口全程兜底，任何失败都只输出一句人话 + 非零退出码，
  打包版不会再弹 traceback 对话框

## CI 说明

- 工作流在 `push`、`pull_request` 与手动触发时运行，**不需要任何凭据**
  （用例全部离线，用测试替身替代网络）。
- 三个矩阵：Ubuntu + Python 3.10（声明的最低版本）、Ubuntu + 3.12、Windows + 3.12。
- 检查项：语法编译、299 项单元测试、CLI 可用性、格式注册表完整性；
  Windows 上额外跑一次 `--selftest`（联网失败不阻断）。
- 界面相关的用例（`test_theme.py` / `test_window.py`）在没有显示环境的机器上会
  自动 skip，Linux CI 上属于预期行为，不算失败。
- **留意**：`compileall` 即使编译失败也返回 0，工作流里已显式 grep 报错，
  改这一步时别退化成无效检查。
- 打 tag 时会校验 `xdao.__version__` 与 tag 相同，避免发错版本号。

### 两个已经踩过的 CI 坑

1. **测试夹具不能写死 Windows 形态**。`tests/test_pdf.py` 里造「假浏览器」时，
   原先只生成 `fake_browser.cmd`，结果 ubuntu 两个 job 全部挂在单元测试：
   `PermissionError: [Errno 13] Permission denied: .../fake_browser.cmd`。
   现在按平台生成 .cmd 或带执行位的 sh 脚本，并加了回归用例
   `test_fake_browser_is_actually_executable` 保证夹具本身真能被执行。
   **本机预演**：`python tools/posix_check.py`（借 Git 自带的 sh.exe 跑 POSIX 分支，
   没有 Linux 也能提前发现这类问题）。
2. **取 Actions 日志用 `tools/ci_logs.py`**，不要用 `gh run view --log`：
   日志真实地址在 `results-receiver.actions.githubusercontent.com`，
   带签名的临时 URL 在本机网络下经常被中途掐断（`unexpected EOF`）。
   该脚本自己跟随重定向、去掉 `Authorization` 头（否则云存储回 401）并分段重试。

本地跑测试用装好 pytest 的那个解释器（项目源码本身只需标准库）：

```powershell
& 'C:\Users\14515\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe' -m pytest -q
```
