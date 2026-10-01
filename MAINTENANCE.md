# 仓库维护规范

> 本文档说明 `XiaoFeng7418/xdao-export` 的日常维护怎么做。
> 接手维护时先读它，再读 [HANDOFF.md](HANDOFF.md) 的技术交接部分。

## 每周例行（有自动提醒）

1. **体检**

   ```powershell
   Set-Location '<仓库目录>'
   $py = '<本机 Python>\python.exe'
   & $py -X utf8 tools/repo_check.py --repo XiaoFeng7418/xdao-export
   ```

   14 项检查，全部 ✓ 才算健康。它会检查：仓库设置、提交同步、文件逐一致、
   版本号一致、Release 附件齐全、待办积压。

2. **跑测试**（当前基线 962 项，必须全绿）

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

4. **看外部反馈**：Release 下载次数、issue、Star。

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
Set-Location '<仓库目录>'
$py = '<本机 Python>\python.exe'

# 1) 改版本号（xdao/__init__.py），跑测试
& $py -X utf8 -m pytest -q

# 2) 打包（只打 onedir 免安装包；单文件版自 v0.5.1 起不再提供 —— 它的启动器在
#    中文/非 ASCII 路径下会在 Python 代码运行前就失败：Could not create temporary directory!）
#    注意必须设 TCL_LIBRARY / TK_LIBRARY，否则打包出的程序缺 Tcl/Tk
$env:TCL_LIBRARY='<本机 Python>\tcl\tcl8.6'
$env:TK_LIBRARY='<本机 Python>\tcl\tk8.6'
$pypi = '<本机 Python>\python.exe'
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
    --asset '<盘符>\xdao-export-vX.Y.Z-win64.zip' `
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
5. **改动必须带测试**：`tests/` 是 962 项离线用例，新增功能请补用例，
   不要依赖联网测试。
6. 本机 git 的 HTTPS 传输不可用（schannel / openssl 都被拦），
   一切远端操作走 `tools/` 下的 API 脚本。
7. **涉及浏览器的验证要在受限模式外跑**：PDF 导出会启动 Chrome / Edge，
   而浏览器需要命名管道通信，沙箱内必然失败（`mojo ... platform_channel` 报拒绝访问）。
   这是环境限制，不是代码问题。
8. **公开材料里不许出现真实盘符/目录名/串标题**。
   写文档、发布说明、报错示例、提交信息时一律用占位写法：`<盘符>\X岛`、`<盘符>\串`、
   `%LOCALAPPDATA%\xdao-export\导出`、`Desktop\某目录\`。注意公开的地方不只是仓库文件，
   **还包括 GitHub 的 Release 说明正文**（它由 `docs/RELEASE_NOTES_*.md` 上传而来），
   以及打包进 zip 的 `诊断写入.ps1` / `使用说明.txt`。
   工具脚本里的本机路径（`gh.exe`、自装 Python）也要避免写死：用环境变量后取，
   如 `tools/make_release.py` 的 `gh_token()`（`XDAO_GH` / `GITHUB_TOKEN`）。
   **2026-09-30 又收紧了一层：连「听谁说的」也不写。**
   提交信息、文档、发布说明、代码注释、测试 docstring 里不要出现
   「有人报过…」「收到反馈说…」「按某方的要求…」这类**引用来源**的说法；
   把现象和规则写清楚就行 —— 举例：不写「有人报过导出目录不可写」，
   写「导出目录不可写时要给一句人话，而不是把 traceback 丢出去」。
   需要交代背景时用客观时间或场景：「2026-09-30 实测」「真实场景：…」。
   清历史要动整条链，步骤是：备份（`git clone --mirror`）→ 改文件 →
   `git filter-branch --force --tree-filter … --msg-filter … --tag-name-filter cat -- --all`
   → 把标签重新指向清洗后的提交（`filter-branch` 可能丢标签）→ 强推 master 与**每个**标签
   → 核对远端每个标签与 Release 正文（正文要单独 `PATCH`）。工作树必须先提交干净，
   否则报 `Cannot rewrite branches: You have unstaged changes.`
   **标签也算公开材料**：GitHub 会为每个标签自动生成 "Source code" 压缩包，
   所以打完标签之后又清洗过历史文件的话，要把标签挪到清洗后的提交上
   （`git tag -f -a <标签> -m "<标签>" <提交>`，再 `git push origin refs/tags/<标签> --force`）。
   2026-09-30 就这么处理过 `v0.5.2` / `v0.5.3`：附件内容不变，只让标签指向清洗后的树。
9. **界面用例只许调 `App.prepare_export_dir()`，不许调 `App.start()`**。
   `start()` 会走到 `persist_prefs()` → `AppSettings.save()`，而 `AppSettings.load`
   被测试替换过、`save` 没有，结果是把**真实的
   `%APPDATA%\xdao-export\config.json` 覆盖成测试目录**（2026-09-30 真的发生过：
   登录饼干被写成 `TESTHASH`、导出目录变成 pytest 临时目录）。
   `tests/test_window.py` 的 `isolated_settings` 现在把 `AppSettings.save` 也换成了空函数，
   再加用例时不要绕过它。

## 打包版导不出 PDF：已修（v0.3.0 发现，v0.10.0 修掉，2026-10-01）

**原现象**：免安装包 / 单文件版执行 `-f pdf` 时，浏览器子进程返回
`2147483651`（`0x80000003`，STATUS_BREAKPOINT），PDF 无法生成；
而源码运行（`python main.py -f pdf`）完全正常。

**根因**：**Chromium 的沙箱层在「父进程是冻结程序」时初始化失败**。
所以真正管用的开关只有一个：`--no-sandbox`。打包版会在启动浏览器时自动带上它
（`xdao/browser_flags.py`），源码运行仍然带沙箱。

**怎么找出来的**（以后遇到同类「冻结进程里子进程崩」照这个顺序）：

1. 先用诊断开关 `main.py --pdfdiag` / `python tools/pdf_diag.py` 确认打包版里
   浏览器到底能不能起来（会打印 `sys.frozen` 与「浏览器附加参数」）；
2. 冻结探针（PyInstaller `console=True` 打包一个小脚本）里直接调 `render_html_to_pdf`，
   **同一次运行里对照「不给开关 / 给开关」**，排除机器与网络因素；
3. 用 `CreateProcessW` 之类的启动方式矩阵逐个换（见下表），直到变量收敛到一个开关。

**排除表**（都实测过，全部不是）：

| 假设 | 结果 |
|---|---|
| Tcl/Tk 环境变量指向打包目录、与系统版本冲突 | 去掉后仍崩 |
| 继承的环境变量有问题 | 传干净环境仍崩 |
| 工作目录在打包目录，DLL 搜索命中打包的运行库 | 换干净工作目录仍崩 |
| 用管道捕获输出导致句柄继承问题 | 改为重定向到文件仍崩 |
| 进程树继承问题 | 经 `cmd.exe` 代启仍无效 |
| 句柄继承 | 关掉全部可继承句柄仍崩 |
| 进程在作业对象里（job object） | `IsProcessInJob` 显示**不在**作业里 |
| 完全脱离句柄继承 | 用 ctypes 直接 `CreateProcessW`（`bInheritHandles=False`）仍崩 |
| `CREATE_BREAKAWAY_FROM_JOB` | 仍崩 |
| 换解释器/壳启动（PowerShell、`cmd /c start`、`Start-Process`） | 仍崩 |
| 显卡相关 `--disable-gpu` | 仍崩 |
| 渲染器代码完整性 `RendererCodeIntegrity` | 仍崩 |
| 各种细粒度沙箱开关（`--disable-gpu-sandbox`、`--disable-setuid-sandbox`、`--no-zygote`、`--single-process`、NetworkServiceSandbox、JIT 沙箱、seccomp 过滤） | 全部仍崩 |
| 只换 headless 新旧实现（`--headless=old`） | 仍崩（headed 也一样崩，与无头无关） |
| Chrome 自身有问题 | 从**命令行手工**用完全相同的参数启动，正常生成 PDF（32 KB） |
| **`--no-sandbox`** | **成功**（0 退出码，出 PDF） |

**当年为什么会误判成「系统级限制、无法绕过」**：只试了 Tcl/Tk、环境变量、工作目录、
管道句柄、进程树这几类「自家代码的嫌疑」，没试 Chromium 自己的开关；
而手工从命令行启动又恰好因为父进程不是冻结程序而正常，于是把根因归到了系统上。

**应对**（v0.10.0 起）：

- 打包版（`sys.frozen` 为真）启动浏览器时自动带 `--no-sandbox`，**PDF 照常导出**；
- 源码运行不带这个开关，沙箱保持完好；
- 渲染的还是本程序自己写出来的本地 HTML（内容来自抓取到的串），不接受远程页面；
- `PdfBuilder.save()` 的 HTML 降级**保留**：路径写错、安全软件拦下、机器上其实没装浏览器时
  仍然用得上，降级时给的提示也按新的原因重写了（不再提「需要从源码运行」）。

**回归要点**：`tests/test_browser_flags.py` 钉住开关常量与冻结判据的映射、
`tests/test_pdf.py` 与 `tests/test_browser_login.py` 各有一条「参数里必须带上开关」的用例
（都做过变异校验：删掉参数传递就会红）。真机核验见下面「冻结环境 PDF」一段。

**以后要再写冻结探针，照这个来**（2026-10-01 一次性踩了四个坑）：

1. 用 `.spec` 文件而不是一串命令行参数。`Analysis(..., pathex=[仓库根])` 必须写，
   否则 `from xdao import …` 会被记成 missing module、PYZ 里根本没有 `xdao`，
   冻结后 `ModuleNotFoundError: No module named 'xdao'`（运行期再往 `sys.path`
   里塞仓库根也没用 —— 那是源码运行的做法）。
2. 显式列 `hiddenimports`。极简入口脚本的分析图走不到标准库，冻结后会
   `ModuleNotFoundError: No module named 'json'`；改成把 `json`、`json.decoder`、
   `urllib.error`、`urllib.parse` 与 `xdao.*` 全部点名后才齐。想确认打进去了没有，
   用 `PyInstaller.archive.readers` 的 `CArchiveReader`/`ZlibArchiveReader` 把
   `PYZ.pyz` 的模块名列出来，别猜。
3. **对照组要在同一次运行里、而且要还原被改的模块常量**。第一版探针把
   `browser_flags.FROZEN_EXTRA_FLAGS` 改成 `()` 模拟旧版之后就**没还原**，
   后一轮「按真实常量」其实还是空开关 —— 测出来的「带开关」数据全是假的，
   差点得出「`--no-sandbox` 对登录没用」的错误结论。现在先把「命令行里有没有那个开关」
   断言掉，再谈页面能不能用。
4. 小程序的 stdout 也要防一手：Python 在 Windows 上按本机代码页（GBK）写 stdout，
   转述 PowerShell 输出时混进一个替换字符就 `UnicodeEncodeError`，整轮探针当场断掉；
   探针里自己把 `sys.stdout` 换成按 UTF-8 写、装不下就转义的包装器。

另外两条与探针无关但同样会咬人的：`Page.captureScreenshot` 返回的是
`{"data": "<base64>"}` 而不是裸 base64（直接喂给 `b64decode` 会
`TypeError: argument should be a bytes-like object or ASCII string, not 'dict'`）；
`Get-Content` 默认按 ANSI 读文件，UTF-8 日志看上去会是乱码，加 `-Encoding utf8`。

## 缓存目录写不进去不再挡住宿主功能（v0.3.2，2026-09-30）

**报出来的问题**：导出到 `<盘符>\X岛` 时报
`[Errno 13] Permission denied: '<盘符>\X岛\.cache\xdao-write-test.tmp'`，
而 0.1.0 版反而能正常写入。

**原因**：0.1.0 没有缓存层，只往导出目录本身写文件；0.2 之后把「缓存目录可写」
变成了前置条件，于是**缓存写不进去 → 整个导出在抓取前就退出**，看起来像"目录没权限"。
报错文案里说的是缓存，而真正写不进去的可能是导出目录本身。

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

**注意**：`<盘符>\X岛` 是使用者的真实目录，别把测试文件留在里面。

## 入口处不许漏出 traceback（v0.3.3，2026-09-30）

**报出来的问题**：打包版弹出 PyInstaller 的「Unhandled exception in script」对话框，
内容是 `PermissionError: [WinError 5] 拒绝访问。: '<系统目录>'`，
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
把输出目录设成用户的 `<盘符>\X岛`（config 里的默认值）时最容易暴露界面层的兜底问题。

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
- **「首次导出」不提醒**（`result.first_run and result.new_posts <= 0`）：一次加十个
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

- 无人值守登录（浏览器登录已把这一步缩到只剩验证码）—— **已定论不做**：验证码必须
  真人认一次，没有可靠的自动化办法（详见下面的备注）。
  - 备注：实测视觉模型对 X 岛的验证码识别率太低（三张只对一张半），
    **不要**把基于 OCR / 视觉模型的自动登录写进产品 —— 认错一次就得从头再来，
    还不如让用户自己点一下浏览器窗口。

## 已完成

- **一键升级**（v0.13.0）：新增 `xdao/updater.py` 与 `main.py --apply-update`。点「有新版本」
  按钮后：下载到 `%TEMP%\xdao-export-update`（`download_asset`，有大小上限、
  失败清半个文件）→ `extract_payload` 只认顶层 `xdao-export-v*` 目录并挡越界路径 →
  `run_selftest` 拿**候选版本**跑 `--selftest --offline`（不过就什么都不换）→ 主线程二次确认 →
  `spawn_helper` 用**升级包里的那个 exe** 当帮手（不依赖系统 Python/PowerShell），帮手
  `--apply-update <目录> <payload> <pid> <启动程序>` 等老进程退出 → `swap_in`
  （旧目录改名为 `<名>.old-<时间戳>`，新版搬进来，搬不动就改回去）→ 启动新版 → 清暂存。
  三条设计取舍：①**先自检再替换**，宁可这次不升也不换上一个打不开的；②**旧版本不删**，
  只改名，留 24 小时（`cleanup_backups`，每次启动顺手清）；③**帮手用升级包里的 exe**，
  系统里没 Python、没 PowerShell 也能升级。顺带把「答应用户中途取消」的路径也做了：
  取消或失败一律 `discard_staging`，不在 `%TEMP%` 里留几十 MB。

- **新版本检查**（v0.12.0）：新增 `xdao/update_check.py`（`parse_version` / `is_newer` /新增 `xdao/update_check.py`（`parse_version` / `is_newer` /
  `fetch_latest` / `check_for_update`）。问的是 `releases/latest` 接口，**只取 tag_name 与
  html_url**，不发任何本机信息；代理按「环境变量（HTTPS_PROXY 等）→ Windows 系统代理设置
  （注册表 ProxyEnable/ProxyServer）→ 直连」的顺序探。结果缓存在 `配置目录/update-check.json`，
  24 小时内不重复问；命令行 `--check-update` / `--check-update-json`（**手动查一律不吃缓存**），
  界面在「运行日志」右上角加「检查更新」按钮，启动后 1.2 秒自己问一次（静默，只写日志）。
  三条设计取舍：①认不出的版本号当 0.0.0 —— 宁可不说「有新版本」，也不能瞎报；
  ②查不到不算程序出错，`--check-update` 一律退出码 0，`--offline` 与它同时给出会明确报错；
  ③界面那条路必须**用队列把结果交回主线程** —— 工作线程直接 `root.after()` 会撞上
  `RuntimeError: main thread is not in main loop`（本机探针真踩到过，导出/监控原本就是队列）。
  顺带修掉一个老毛病：`--offline` 以前只在「没配 `--selftest`」时报错，和 `--check-update`
  一起给会被忽略，现在是明确的不兼容组合。

- **环境自检**（v0.11.0）：`--selftest` 从「只联网测接口」扩成「本机环境体检 + 联网检查」，
  新增 `xdao/preflight.py`（`Check` / `Report` / 八项本机检查 / `run_network_checks()`），
  命令行加 `--selftest-json`、`--offline`（单独给 `--offline` 会明确报错，不落到启动界面），
  界面加 `SelftestDialog` 与「运行日志」右上角的「自检」按钮。全程只读：配置目录用
  `can_write_dir` 探针、绝不调 `AppSettings.save()`；本机那部分断网也照样有意义。
  顺带修掉 `Card` 的一个布局老问题：画布不会把内嵌窗口的高度拉到画布高，`body` 恒为
  请求尺寸，于是「填满」是假的 —— 新增 `Card(stretch=True)`（先垫一层铺满画布的
  `Frame` 再把 `body` pack 进去），自检对话框的文本区从 206px 变成 442px。
  教训：**withdraw 的根窗口量出来的 Tk 尺寸不可信**，布局用例必须先 `deiconify()+update()`。
- **改掉两处「复述来源」的措辞**（v0.10.2）：`--pdfdiag` 的说明里「用户报……时」改成
  「遇到……时」，缓存目录候选顺序的说明里「用户要求的位置」改成「指定的位置」。
  仓库是公开的，注释与帮助文字不写「用户报/用户要求」这类复述来源的句式；
  两处都不涉及行为，功能与命令行完全不变，测试基线也不变。
- **打包版登录窗口也核过了**（v0.10.1）：v0.10.0 只量了 PDF 那条路，这一版在冻结真机里
  同样把「启动浏览器 → 连 CDP → 等登录页 → 读标题 → 截图」跑了一遍。结论是**症状要分两种**：
  不带 `--no-sandbox` 时进程起得来、调试端口也有，但页面永不提交、截图一直 `Internal error`
  （崩的是渲染器，不是进程）；带上之后落在登录页、标题「用户登录 - User System - X岛揭示板」、
  截图 27,910 字节。功能零改动，改的是文档里的说法与核验范围。
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
  两级都成功才算可写。教训来自 `<盘符>\X岛\.cache` 建得出来、`.cache\pages` 拒绝访问 ——
  只探表层会把这种目录判成"能写"，于是"写不进去就换地方"的兜底永远不触发。
  推论：**任何"换个能写的地方"的探测，都必须探到实际要创建的那一层**，否则兜底是装饰。
  同版把写盘失败的提示改成可执行的（指出目录、建议换到文档目录、提示查安全软件白名单）
- **导出目录写不进去就自动换地方**（v0.5.3）：`choose_writable_dir()` 在写不进去时依次试
  `%LOCALAPPDATA%\xdao-export\导出` → `%TEMP%\xdao-export`，返回 `DirChoice(path, notes, fallback)`；
  界面与命令行都会明说换了地方，且**不动导出设置**。在「更改…」里手选过目录
  （`App._dir_pinned`）或命令行给了 `-o`，则只提示不换。触发场景是受控文件夹访问默认保护
  桌面/文档/图片/视频 —— 抓取全部成功、最后一个文件都写不下去。
  同版把 `诊断写入.ps1` 改成会把报告写成 `诊断结果-*.txt`（双击一闪而过的抱怨），
  并新增 `诊断写入-双击运行.cmd`
- 命令行入口与 `--selftest`
- **CI**：每次推送/PR 自动跑离线测试（`.github/workflows/tests.yml`），
  Linux 3.10/3.12 + Windows 3.12 三个环境；打 tag 时额外校验版本号与 tag 一致
- 仓库维护脚本（体检 / 推送 / 发布 / 同步 / 清理）
- **错误处理收敛**（v0.3.3）：入口全程兜底，任何失败都只输出一句人话 + 非零退出码，
  打包版不会再弹 traceback 对话框
- **单页失败自动补抓 / 重试队列**（v0.6.0）：抓取循环里任何一页失败只是**记下来**，
  后面的页照抓；第一遍跑完对失败页统一补抓（`RETRY_ATTEMPTS = 2`、`RETRY_DELAY = 2.0`，
  每轮只等一次；`retry_attempts=0` 可关）。补不上的页进 `CachedThread.failed_pages`，
  `retry_note` 如实写明缺第几页、补了几次、最近一次失败原因；只在试过的页一半以上失败时
  （多半断网/限流）才把"连试都没试"的尾页一起算进缺失。撞 `max_pages` 时
  `truncated = True` 并说明只抓到第几页。界面额外弹一次「有串没抓全」。
  **两个真 bug 一并修掉**：① `cache.pages` 以前写的是 `max(payloads)`（最大页号），
  中间缺页时缓存仍声称完整，下次导出直接复用缺页缓存 → 改成 `cache.contiguous_pages()`
  （从第 1 页起连续存在的页数）。② 光改成"连续前缀"还不够：`len(cached_pages) >= cache.pages`
  恒真，必须再跟接口报的 `page_count` 比一次
  （`cache_covers = bool(cached_pages) and len(cached_pages) >= cache.pages and cache.pages >= page_count`），
  否则缺页永远补不上。**教训：任何"缓存够不够用"的判断都要同时看连续前缀与接口报的总页数。**
- **暗色模式**（v0.6.0）：右上角「配色」下拉框，`theme.set_palette` + `App.switch_theme`
  重建界面（销毁 `_widget_roots` 里的控件再 `_build_ui(keep_log)`），配色存进
  `AppSettings.theme_name`（`_apply` 用 `PALETTES` 白名单校验，写 `"theme"`）。
  切主题的三个坑：① `from .theme import PALETTE` 是**导入期旧对象**，必须用
  `_PAL = theme.PALETTE` + `refresh_colors()`；② `refresh_open_dialogs` 的 `previous`
  要在 `refresh_colors()` **之前**取，否则 `color_map` 变成"新→新"，对话框纹丝不动；
  ③ 日志框建出来就是 `state="disabled"`，重建时先 `state="normal"` 再 insert，否则旧日志全丢。
- **收尾不再崩**（v0.6.0）：`_finish()` 以前读 `start()` 的局部变量 `output_dir`
  （从队列回调里根本看不到），导出正常却弹 `NameError: name 'output_dir' is not defined`，
  改成 `self._output_dir` + `_resolved_output_dir()` 兜底。
- **被接口拒绝时给出下一步**（v0.6.0）：`describe_export_failure` 认「必须登入 / 领取饼干」，
  说明是服务端不认 userhash 并指向重新登录/手动粘贴；`set_userhash` 设置前先清掉同名旧 cookie
  （否则请求头会出现两条 `userhash`）
- **认得「跳转提示」页**（v0.6.1）：X 岛失败时用 ThinkPHP 的跳转模板回话，**HTTP 状态是 200**，
  靠页面里的 JS 自己跳 —— 浏览器会跳，`urllib` 不会。旧版把这张页面当饼干列表解析，
  一个 id 都找不到就抛「未找到可用的饼干」，把真正的失败原因盖掉（邮箱登录失败的根因）。
  现在 `XdaoClient.jump_page_url()` 认 `跳转提示` / `id="href"`（两种属性顺序都试）、
  `jump_page_message()` 取 `class="error"|"success"` 那段文字，
  `_request_following_jumps(url, max_jumps=2)` 遇到跳转页就跟着走并返回 `(响应体, 最终地址)`
  （相对地址补 `self.SITE`、同一地址不重复跟）；`fetch_login_form()`、`apply_cookie()`、
  `switchTo` 那一跳都走它。`apply_cookie()` 按最终落点分三种情况：
  被弹回登录页 → `LoginError`「登录后没能进入用户系统」；有服务端原话 → 「没能读取饼干列表」；
  都没有 → 才说「这个账号的饼干列表是空的」。
  **教训：接口 200 不等于内容页面，解析前先看是不是跳转页；测试样例必须用线上真实 HTML，
  手写的假数据里没有跳转页，这个 bug 正是因此漏过去的。**
- **登录失败的文案分型**（v0.6.1）：`gui.describe_login_failure(message) -> (标题, 该做的事)`，
  五种分支（验证码不对 / 密码不对 / 账号有问题 / 登录没有生效 / 这个账号还没有饼干）+ 兜底透传原文，
  `LoginDialog._poll_login` 失败时用它当弹窗标题与正文。
- **用浏览器登录**（v0.7.0）：新增 `xdao/browser_login.py`，用系统里的 Edge / Chrome 开一个
  **独立用户目录**的窗口（`--remote-debugging-port=0 --user-data-dir=<配置目录>\browser-profile`，
  刻意不加 `--headless` / `--guest` / `--incognito`），端口读 `<profile>\DevToolsActivePort` 第一行，
  再用**纯标准库**手写的 WebSocket 帧层连 CDP，`Network.getCookies` 读 `userhash`
  （HttpOnly 的也读得到）；没读到就 `apply_leaf_cookie()` 在页面里 `fetch` 一遍 switchTo/export 兜底。
  界面侧 `gui.BrowserLoginDialog` 的后台线程只往 `queue.Queue` 投消息、主线程 `after` 轮询，
  退出前 terminate 浏览器进程，不留孤儿。
  **教训：验证码那一步没法自动化** —— 实测视觉模型对 X 岛的验证码识别率太低（三张只对一张半），
  所以"真人自己登录一次"才是最小代价方案，不要把基于 OCR 的自动登录写进产品。
- **粘贴登录更宽容**（v0.7.0）：`browser_login.parse_userhash_input()` 认整段 cookie
  （`a=1; userhash=ABC; b=2`）、`userhash=ABC`、裸值、带引号换行的粘贴，`looks_like_userhash()`
  判形态；`LoginDialog._manual_userhash` 解析失败只弹提示、不抛异常。旧版要求用户自己打开
  浏览器开发者工具从 Cookie 里挑出 `userhash=` 到 `;` 之间的那一段，是登录流程里最容易劝退人的一步。
- **验证码响应的 gzip 隐患**（v0.7.0）：`verify.html` 实测把 PNG 包在 gzip 里发回来，
  HTTP 头却写 `image/png`；旧代码只认 `Content-Encoding`，所以字节一直是压缩的。
  Tk 的 `PhotoImage` 恰好能直接吃压缩字节，这个问题才一直没暴露。
  `client._decode_response_body(data, content_type)` 按 gzip 魔数兜一层（解压失败原样返回），
  `_request()` 顺手把最近一次响应头记进 `self._last_response_headers`（键统一小写），
  `fetch_login_form()` 用它判断 —— **不改 `_request` 的返回类型**，替换 `_request` 的既有夹具不受影响。
- **PDF 纸张 / 边距 / 缩放 / 页码可配置**（v0.8.0）：选项集中在 `xdao/pdf_opts.py`
  （`PdfOptions` + `from_settings()` + `require_valid_pdf_options()`），界面、命令行、导出器
  共用同一套校验与文案。`exporters/pdf.py` 按 `PdfOptions.needs_cdp` 路由：全默认走原来的
  `--print-to-pdf` 命令行（输出与 v0.7.0 逐字节相同），改动过就走 CDP 的 `Page.printToPDF`。
  界面新增设置对话框的「PDF 页面」一块，命令行新增 7 个 `--pdf-*` 开关（写错 exit 2），
  `watcher.check_once()/watch_forever()` 也接上了同一套选项。真机用例在
  `tests/test_pdf_render.py`（`XDAO_PDF_TEST=1` 才跑，4 条：A3 纸张、默认 A4、
  边距影响分页、页码范围截断）。

- **CDP 通道抽成公共模块**（v0.8.0）：`xdao/cdp.py` 只放传输层（帧协议、`CDPSession`、
  `pick_page`/`pick_site_page`、`http_json`），不含任何业务站点常量；站点前缀由调用方通过
  `CDPSession(ws_url, timeout=…, site_urls=[…])` 传进去。`browser_login.py` 保留同名兼容出口
  （`from .cdp import …` + `BrowserLoginError = CdpError` + `_new_session()`），所以既有测试与
  调用方不用改。**`LoginBrowser.start()/stop()` 与 `_kill_process_tree()` 必须留在
  `browser_login.py`**：测试用 `monkeypatch.setattr(bl.subprocess, "Popen"/"run")` 打桩，
  走的是本模块的 `subprocess` 名字。
- **PDF 纸张 / 边距可配置**（v0.8.0）：`xdao/pdf_opts.py` 是选项的唯一来源（界面下拉框、
  命令行校验、导出器共用）；`exporters/pdf.py` 有两条渲染路径 —— 全默认走原来的
  `--print-to-pdf` 命令行（输出与 v0.7.0 逐字节相同），改过任何一项走 CDP 的
  `Page.printToPDF`（返回的 `result["data"]` 是 base64 的 PDF，解出来写盘即可）。
  `watcher.check_once()/watch_forever()` 必须由调用方显式传 `pdf_options` 与 `browser_path`
  —— 监控是长期后台功能，漏接会让用户以为设置没保存。三个必须记住的点：
  1. **路由判断用 `PdfOptions.needs_cdp`，不要用 `is_default`** —— `is_default` 不看
     `page_ranges`，只填页码会被判成「没改过」而走命令行，页码被静默丢掉；
  2. **显式纸张时绝不能带 `preferCSSPageSize`** —— 两者同时出现时浏览器改以页面 CSS 的
     `@page size` 为准（用户选 A3 出 A4），而且是竞态：同一份 HTML、同一组参数连跑三次，
     只有在 `@page` 还没解析完的那一次才按显式纸张出纸。真机对照脚本
     `_scratch/cmp_pdf_paths.py`；
  3. **页面自己声明 `@page { margin: … }` 时，CDP 的 `marginTop/…` 会被页面样式盖掉** ——
     浏览器的行为，绕不过去，所以「跟随网页样式」才是推荐默认值。
- **测试绝不许碰用户真实配置**（v0.8.0 的教训）：`tests/test_settings.py` 早期版本没有隔离
  配置，跑一次全量就把 `%APPDATA%\xdao-export\config.json` 里的 PDF 键写成了测试值
  （实现者自己跑全量时同样触发）。现在 `tests/conftest.py` 有 autouse 守卫
  `_forbid_writing_the_real_user_config`：**模块级安装、只装不拆、幂等**，拦「读用户配置」与
  「写用户配置」，**不拦构造**（`AppSettings()` 默认落到用户配置本身是要被测的行为）。
  两条踩过的坑：①改 dataclass 的 `__dataclass_fields__["_path"].default_factory` 或改模块里
  `_default_config_path` 名字都拦不住（factory 函数对象在类创建时就进了 `__init__` 的默认值），
  只有包 `AppSettings.__init__`/`load`/`save` 有效；②安装时要先看
  `AppSettings.load.__name__ != "load"` 就**直接退让**，否则会把别人（module 作用域夹具）已经
  换上的替身永久留在类上（实测 24 条界面用例全红）。临时配置的正确写法是**派生子类**并把
  `_path` 声明成 `default_factory=lambda: path`。
- **打包版能导出 PDF 了**（v0.10.0）：原先打包版 `-f pdf` 会被
  `2147483651`（STATUS_BREAKPOINT）挡下、自动降级成 HTML，根因是 **Chromium 的沙箱层在
  「父进程是冻结程序」时初始化失败**，加 `--no-sandbox` 即通（`xdao/browser_flags.py`
  只在 `sys.frozen` 为真时加；源码运行仍带沙箱）；同版把 `--pdfdiag` 诊断对齐到同一套参数、
  把「需要从源码运行」的旧提示改写掉。详见「打包版导不出 PDF：已修」一节。
- **监控列表能备份 / 还原**（v0.9.0）：新模块 `xdao/watch_list.py` 管文件格式，
  界面（`WatchDialog` 的「导出列表 / 导入列表」）与命令行（`--watch-export` /
  `--watch-import` / `--watch-import-replace`）共用同一套读写。三个定下来就不好改的决定：
  1. **导出文件只带「怎么监控」，不带「监控到哪儿了」** —— `state` / `last_check` /
     `last_error` / `exports` / `last_export_path` 都是跟着缓存目录跑的临时状态，
     跟着文件搬到另一台机器会让它以为"已经导出过了"，第一轮该出的不出。
  2. **`thread_id` 一律按文件重算**，不信文件里写的串号 —— 监控键
     `xdao.watcher.target_key` 是按串号拼的，串号被改过之后一份状态会挂到另一个串上，
     表现成「明明有新回复却一直报无更新」。顺带把认不出串号的网址补成规范形式。
  3. **导入是合并、不是替换**（按 `target_key` 去重；想替换用 `--watch-import-replace`）。
     **「替换」的判据是「只留下文件里那些条目」，包括文件里和现在完全一样的那几条** ——
     最早的写法是 `merged = result.added`，而「同一个串、同样的设置」被算进了
     `result.skipped`，于是替换之后**列表被清空**（用户以为只是换成文件里那份）。
     现在这些条目单独记在 `result.duplicates` 里，替换时一起留下，命令行也把
     「已经有了」印成「保留」而不是「跳过」（真机冒烟 `_scratch/smoke_watch_list_cli.py`
     踩出来的；`tests/test_cli.py` 有专门的回归用例）。
  文件层面的坏（不存在 / 超 10 MiB / 不是 JSON / 没有 `targets`）抛 `WatchListError`，
  条目层面的坏逐条跳过并把中文原因回给用户 —— **坏一条不该毁掉整份文件**。
  还有一个只在真机截图上看得见的坑：导入/导出按钮原先挤在「添加 / 移除 / 间隔 / 校验」
  那一行右边，**被整条裁到窗口外**，用例全绿而用户看不见。它们现在单独占一行，
  `WatchDialog` 的高度也跟着加了一行（`760x520`）。教训：**加了控件要重新截图看一眼**，
  用例只能证明对象存在，证明不了它出现在窗口里。
  **另一个坑：改文档别把行尾换掉。** 这几个 Markdown 在 git 里是 CRLF，用 Python
  默认的 `write_text` 读改写之后整份变成 LF，`git diff` 里 1000 多行全是行尾变化，
  真正的改动被埋掉（`HANDOFF.md` 37 行真改动显示成 549 行）。改完用
  `_scratch/check_line_endings.py` 那种方式核一遍 `git diff --stat`。
- **打包版能导出 PDF 了**（v0.10.0）：新增 `xdao/browser_flags.py`
  （`FROZEN_EXTRA_FLAGS = ("--no-sandbox",)`、`is_frozen()`、`launch_flags(*, frozen=None)`），
  `xdao/exporters/pdf.py` 的命令行渲染路径与 `xdao/browser_login.py` 的 `build_args()`
  都在参数里摊上 `launch_flags()`。**根因、排除表与回归要点写在上面
  「打包版导不出 PDF：已修」一节**，改这块之前先读它。另外两件事一起做了：
  1. `tools/pdf_diag.py`（`--pdfdiag`）跟着用同一套参数并打印「浏览器附加参数」——
     诊断必须和正式实现走同一条路，否则它给出的「启动失败」是误导；直接
     `python tools/pdf_diag.py` 时要自己把仓库根塞进 `sys.path`（否则
     `ModuleNotFoundError: No module named 'xdao'`）。
  2. `exporters/pdf.py` 的 `is_frozen()` 委托给 `browser_flags.is_frozen()`，
     **判据只留一处**；`browser_launch_failure_hint()` 的文案重写成「多半是浏览器路径
     不对或安全软件拦下」，并且只在冻结环境才追加第二段。
  3. `xdao/cache.py:295` 的 `tempfile.gettempdir()` 包了兜底：缓存候选列表本来就有
     「这一处写不进去就换下一处」的设计，问不到系统临时目录应当只是**少一个候选**，
     而不是让整次导出失败（真机核验时撞到过 `No usable temporary directory found in [...]`）。
     这条路径的候选少了不会被静默忽略 —— `resolve_cache_dir()` 照样会把换目录的事写进备注。
     用例：`tests/test_cache.py::test_cache_dir_candidates_survive_a_broken_temp_dir` /
     `::test_cache_dir_candidates_keep_the_temp_dir_when_it_works`（去掉兜底会红）。
  **真机核验必须用冻结 exe，不能靠改 `sys.frozen` 的单元用例**：
  `_scratch/pdf_frozen_verify.py`（PyInstaller `console=True`）打印
  `is_frozen() = True | launch_flags() = ['--no-sandbox']`，同一次运行里
  「不给开关 → `PdfError: Chrome 没有生成 PDF（退出码 2147483651）`，给开关 → 成功，
  65,290 字节 / `%PDF` 是 / 1 页 / 612.0x792.0pt」。
  **登录窗口那条路 2026-10-01 也单独量过**（`_scratch/loginverify_frozen.py` + `loginverify.spec`，
  同一台机器、同一次运行里两轮对照）：
  - 不给开关：浏览器**进程起来了**（pid 有、调试端口写出来了、CDP 连得上），
    但页面既不提交也不报错 —— 地址与标题一直是空的，`Page.captureScreenshot`
    连试三次都是 `Internal error`；
  - 给开关：地址 `https://www.nmbxd1.com/Member/User/Index/login.html`、
    标题「用户登录 - User System - X岛揭示板」、截图 27,910 字节。
  所以「有头启动也一样崩」这句话当年说得太粗 —— 崩的不是进程，是渲染器：
  窗口可能开得出来，登录页永远加载不了。补开关的理由不变，症状要按实际写。
- **行尾判据只有一条：工作区字节 vs 索引 blob**（v0.10.0 的返工教训）。
  编辑工具把 `tests/test_browser_login.py`、`xdao/browser_login.py` 两个 LF 文件写成了
  整份 CRLF；`.gitattributes` 是 `* text=auto eol=lf`，所以 `git diff --numstat` 只显示
  真改动（25/6 行）、`git status` 却一直报 `M`，`git diff` 不过滤时会打出整文件。
  两个错误判据都试过，都得出过错误结论：①`git show HEAD:<文件>` 给的是**规范化成 LF**
  的内容（README 显示 0 个 CRLF），而索引里的 blob 其实是 **CRLF** —— 别拿它当期望；
  ②`git ls-files --eol` 的 `attr/` 列说的是「规范化之后该是什么」，据此把该是 CRLF 的
  `诊断写入.ps1` / `诊断写入-双击运行.cmd` 误改成了 LF。**正解是 `git checkout -- <文件>`**
  （直接从索引取原始字节写回）。工具是 `_scratch/check_line_endings.py`：
  `git ls-files -s` 取索引 blob 逐个比字节，`git check-attr eol` 判「这个文件本来就该是
  什么行尾」，输出分【行尾被翻过】/【按 .gitattributes 就该这样】/【有内容改动】/
  【可疑】四类，只有【可疑】（字节不同而 `git diff` 是空的）才返回 1；`--fix` 等价于
  上面那条 checkout。**「有内容改动」这一类是必须的**：不加的话每个正在改的文件都会被
  误报成「行尾被翻」。

## 浏览器登录：两个真机才量得出来的坑（v0.7.0）

跑真机验证的脚本是 `_scratch/e2e_browser_login.py`（在仓库外；打 tag 前跑一次，
它会启一次真 Edge、真 CDP，登录页截图落在 `_scratch/e2e_browser_login/login_page.png`）。

1. **「`/json/list` 上写着登录页地址」不等于「页面已经能用」。**
   真机实测：浏览器起来后目标列表里立刻就有
   `https://www.nmbxd1.com/Member/User/Index/login.html`，但挂上去后 `location.href`
   一直是 `about:blank`、`document.title` 是空串，`Page.captureScreenshot` /
   `Runtime.evaluate` 一律超时 —— **文档还没提交**，页面里的 `fetch` 属于别的源。
   本机走代理时这一步要 **10–12 秒**才提交（有时代理更快就更早）。
   推论（写测试 / 写健康检查时都适用）：
   - 断言要分两层：**目标层面**（站点标签在不在、会话挂的是不是它、`apply_leaf_cookie`
     返回 None）任何情况下都成立，可以当健康判据；**文档层面**（`location.href`、
     `document.title`）依赖网络是否可达，不能当判据。
   - 真机脚本要**等文档提交**再截图/断言，否则会得到一张空白页截图和一个假的失败。
   - `gui.BrowserLoginDialog` 之所以在连上后调一次
     `browser_login.ensure_login_page(session)`（`xdao/gui.py:780-788`，失败只投 note、
     不掐流程），就是为了这个窗口期；**它只在启动时调一次**。
2. **`ensure_login_page()` 不能放进轮询循环。**
   `session.current_url()` 反映的是**已提交的文档**，不是待加载地址 —— 网络不通时它一直
   是 `about:blank`，每轮都会判定"不在站内"再导航一次＝反复重载登录页（用户正输密码也照重载）。
   将来真要周期性校正，必须加"每个标签最多导航一次"的护栏。
3. **收尾要按实例、不要回头读登记处。** `gui.BrowserLoginDialog._release()` 会把
   `self._browser` 清成 None；取消若落在「后台线程已登记、`browser.start()` 还在 Popen 里」
   那一瞬，主线程那次收尾停不掉任何东西（`process` 还是 None）却清空了登记处，后台线程随后
   真把进程拉起来就**没人 terminate，成了孤儿**。修法是 `_release_browser(self, browser)`
   按**局部实例引用**收尾（`xdao/gui.py:671-686`），两个"起来之后才发现已取消"的守卫都用它。
   相应教训：`all(p.returncode is not None for p in processes)` 在 `processes` 为空时**恒真**，
   旧用例因此会空过 —— 断言孤儿必须先把"进程确实被拉起来"钉死。

## CI 说明

- 工作流在 `push`、`pull_request` 与手动触发时运行，**不需要任何凭据**
  （用例全部离线，用测试替身替代网络）。
- 三个矩阵：Ubuntu + Python 3.10（声明的最低版本）、Ubuntu + 3.12、Windows + 3.12。
- 检查项：语法编译、962 项单元测试、CLI 可用性、格式注册表完整性；
  Windows 上额外跑一次 `--selftest`（联网失败不阻断）。
- 界面相关的用例（`test_theme.py` / `test_window.py` / `test_gui_browser_login.py`）
  在没有显示环境的机器上会自动 skip，Linux CI 上属于预期行为，不算失败。
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

### 测试替身与真契约（v0.7.0 踩的坑）

1. **替身要跟着真契约长**。库在会话对象上长出新要求（尤其是上下文管理协议、新方法）时，
   替身会整个漏接，而且表现成一片看着像环境问题的失败。真实案例：`LoginBrowser.start()`
   一度自己 `with CDPSession(...)` 去确保登录页就绪，而 `tests/test_gui_browser_login.py`
   里的假会话当时没有 `__enter__` / `__exit__` → `TypeError: object does not support the
   context manager protocol` 从 `start()` 里逃出去，worker 收到 error，症状却是
   「浏览器没起来 / 读不到 userhash」。事后在 `tests/test_gui_browser_login.py` 末尾补了
   两条契约守卫用例：一条用 AST 扫库源码里 `session.<方法>()` 的调用集、断言替身都覆盖到；
   一条断言 `LoginBrowser.start()` 不许自己开 CDP 会话（这次的根因，防回归）。
   **加替身时先看一眼真实现，别照着上一次的调用习惯抄。**
2. **全量结论只认「冻结树 + 重跑」**。在别人还在写的树上跑全量，数字随时会被下一次写入作废。
   跑之前和跑完之后各对 `xdao/*.py` 与 `tests/*.py` 取一次哈希，两边一致才把结果当数；
   文档里的测试计数也用这个数字。
3. **「0 failed」不等于「全跑过了」**。实测撞到过一次 `473 passed, 12 skipped, 0 failed`：
   `tk.Tk()` 偶发抛 `TclError`，`tests/test_theme.py:28`、`tests/test_window.py:50/96`、
   `tests/test_gui_browser_login.py` 的模块级夹具就 `pytest.skip("没有可用的显示环境")`，
   一挂带走一整组界面用例；用强制 `Tk` 抛错的插件量过最坏情况是 **47 条**无声跳过，报告里
   一个 failed 都没有。所以 `tests/conftest.py` 加了一对 hook：`pytest_report_teststatus`
   收集跳过项，`pytest_sessionfinish` 收尾核对 —— 白名单外**任何**跳过直接报错；白名单内
   （`XDAO_BROWSER_TEST` / `XDAO_LIVE_NOTIFY` / 「没有可用的显示环境」/ 仅有平台的 skipif）
   但数量超过 5 条时打印「本次跳过了 N 条（界面/真机用例），未计入通过数」。
   **验收测试时用 `pytest -q -p no:cacheprovider -rs`，跳过清单必须正好是那两条真机用例。**

本地跑测试用装好 pytest 的那个解释器（项目源码本身只需标准库）。
**本机要把 Tcl/Tk 的库目录指出来**，否则 `tk.Tk()` 会报
`TclError: invalid command name "tcl_findLibrary"`，整组界面用例被当成
「没有可用的显示环境」跳过（2026-10-01 实测：不设环境变量时是
`944 passed, 16 skipped`，退出码 1；设上之后是 `956 passed, 6 skipped`，退出码 0）：

```powershell
$env:TCL_LIBRARY = '<本机 Python>\tcl\tcl8.6'
$env:TK_LIBRARY  = '<本机 Python>\tcl\tk8.6'
& '<本机 Python>\python.exe' -X utf8 -m pytest -q -p no:cacheprovider -rs
```
