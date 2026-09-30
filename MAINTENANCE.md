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

2. **跑测试**（当前基线 219 项，必须全绿）

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

# 2) 打包两种形式（onedir 免安装包 + onefile 单文件版）
#    注意必须设 TCL_LIBRARY / TK_LIBRARY，否则打包出的程序缺 Tcl/Tk
$env:TCL_LIBRARY='C:\Users\14515\Documents\Codex\python3129\tcl\tcl8.6'
$env:TK_LIBRARY='C:\Users\14515\Documents\Codex\python3129\tcl\tk8.6'
$pypi = 'C:\Users\14515\Documents\Codex\python3129\python.exe'
& $pypi -m PyInstaller --onedir --windowed --clean --noconfirm --name "xdao-export" main.py
& $pypi -m PyInstaller --onefile --windowed --clean --noconfirm --name "xdao-export-single" main.py
# 单文件版改成带版本号的 ASCII 名：dist\xdao-export-vX.Y.Z.exe

# 3) 组装免安装包（exe + _internal + 使用说明.txt），压缩成
#    xdao-export-vX.Y.Z-win64.zip

# 4) 验证两种产物都能跑（自检退出码应为 0）
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
5. **改动必须带测试**：`tests/` 是 219 项离线用例，新增功能请补用例，
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

## 路线图（尚未实现）

- 监控到更新时的桌面通知
- 无人值守登录 / 验证码识别
- 单页失败时的自动补抓与重试队列
- 监控列表的导入 / 导出
- PDF 的页边距 / 纸张大小可配置（目前沿用网页的打印样式）

## 已完成

- 四种格式（HTML / TXT / Markdown / EPUB）＋ **PDF**（v0.3.0，走本机浏览器无头渲染）
- 断点续传、增量更新、图片缓存
- 串更新监控
- 命令行入口与 `--selftest`
- **CI**：每次推送/PR 自动跑离线测试（`.github/workflows/tests.yml`），
  Linux 3.10/3.12 + Windows 3.12 三个环境；打 tag 时额外校验版本号与 tag 一致
- 仓库维护脚本（体检 / 推送 / 发布 / 同步 / 清理）

## CI 说明

- 工作流在 `push`、`pull_request` 与手动触发时运行，**不需要任何凭据**
  （用例全部离线，用测试替身替代网络）。
- 三个矩阵：Ubuntu + Python 3.10（声明的最低版本）、Ubuntu + 3.12、Windows + 3.12。
- 检查项：语法编译、219 项单元测试、CLI 可用性、格式注册表完整性；
  Windows 上额外跑一次 `--selftest`（联网失败不阻断）。
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
