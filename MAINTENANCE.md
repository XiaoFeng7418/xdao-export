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

   每一项都要 ✓ 才算健康（工具在结尾自己报「共 N 项检查」）。它检查：仓库设置、提交同步、文件逐一致、
   工作区干净（有没提交的改动时只警告，退出码仍是 0）、版本号一致、Release 附件齐全、待办积压。

2. **跑测试**（必须全绿；跳过的那几项都要显式开关，见 `HANDOFF.md` 的第二节）

   ```powershell
   & $py -X utf8 -m pytest -q
   ```

3. **处理告警**，对照表：

   | 告警 | 处理方式 |
   |---|---|
   | 本地有提交未推送 | `git push origin main:refs/heads/master`（优先；代理不可用时才用 `tools/push_via_api.py`） |
   | 工作区有没提交的改动 | 先提交（或先 stash）；「本地 HEAD = 远端」说的只是提交，不含工作区 |
   | ✗ 读不到 `xdao.__version__` / 本地树是空的 | 这不是「没问题」而是**没查成**：照报错里的原始信息查，别当成体检通过 |
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
- `--apply` 是先验后写、写完再核对的：推导出来的值不合格（描述空的、超长的、话题空的、
  超过 20 个的、名字不合规的）一律拦下并说清为什么，**一个请求都不发**；写完会再读回来
  核对，还是不一样就返回 1 并写明「别当成同步好了」—— 发过请求不等于改成了。

```powershell
& $py -X utf8 tools/repo_info.py --repo XiaoFeng7418/xdao-export           # 只检查
& $py -X utf8 tools/repo_info.py --repo XiaoFeng7418/xdao-export --apply   # 同步
```

## 发布新版本的完整流程

```powershell
Set-Location '<仓库目录>'
$py = '<本机 Python>\python.exe'

# 1) 改版本号：四处一起改 —— xdao/__init__.py 的注释与 __version__、README.md 的
#    「最新版」那句与附件名、packaging/使用说明.txt 第 1 行与「本版版本号」、
#    docs/RELEASE_NOTES_vX.Y.Z.md（新写一份）。然后跑测试：
#    tests/test_version_consistency.py 会把这四处逐一对一遍，漏改一处就红。
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
#    `--exclude` 里的路径写错一个字母，现在会在发任何请求之前就停下（先验再动）；
#    `--dry-run` 不创建提交、也不动分支引用，但为核对树仍会在远端留下未引用的对象。

# 6) 发布
& $py -X utf8 tools/make_release.py --repo XiaoFeng7418/xdao-export --tag vX.Y.Z `
    --name "vX.Y.Z：..." --notes-file docs/RELEASE_NOTES_vX.Y.Z.md `
    --asset '<盘符>\xdao-export-vX.Y.Z-win64.zip'   # 只有这一个附件
#    附件不在就直接不建 Release（先验再动）；发完会再核一遍附件在不在，缺了或者
#    发布说明是空的，都以退出码 1 结束 —— 别把「跳过」当成发好了。

# 7) 收尾：确认体检全绿
& $py -X utf8 tools/repo_check.py --repo XiaoFeng7418/xdao-export

# 8) 再验一次「旧版能不能看见这个新包」：拿上一版的打包 exe 跑更新检查
& '<盘符>\xdao-export-v<上一版的版本号>\xdao-export-v<上一版的版本号>-win64\xdao-export.exe' --check-update-json
#    应当看到刚发的版本、升级包地址指向刚上传的附件、字节数一致；源码版再跑一次
#    `main.py --check-update`，应当回「已是最新版本」。

# 9) 最后把发出去的附件下载回来跑一遍（用户拿到的就是这个文件，不是本机构建的那个）
& $gh release download vX.Y.Z --repo XiaoFeng7418/xdao-export --pattern '*.zip' `
    --dir <临时目录> --clobber
Get-FileHash <临时目录>\xdao-export-vX.Y.Z-win64.zip -Algorithm SHA256   # 要与本机构建的完全相同
Expand-Archive <临时目录>\xdao-export-vX.Y.Z-win64.zip -DestinationPath <临时目录>\解开
& '<临时目录>\解开\xdao-export-vX.Y.Z-win64\xdao-export.exe' --version   # 应打出这个版本号
& $py -X utf8 tools\gui_probe.py --exe '<临时目录>\解开\xdao-export-vX.Y.Z-win64\xdao-export.exe'
#    必须报「界面已正常启动（主窗口：X岛串导出）」并退 0；退 2 是「没看到主窗口，
#    需要人工确认」、退 1 是启动失败 —— 这两种都不算验过了。
```

上面第 8 步值得单独跑：它验的是**用户那台机器上已经装着的旧版**能不能看见新包，
`repo_check` 只验远端仓库与附件、验不到这条路。2026-10-02 实测：v0.13.17 的打包 exe 跑
`--check-update-json` 得到「最新版本 v0.13.18」、升级包地址指向
`xdao-export-v0.13.18-win64.zip`、附件大小 12040398 字节，与刚上传的完全一致；同一时刻源码版
（0.13.18）回「已是最新版本（0.13.18）」。
第 9 步也值得单独跑：前面几步验的都是「本机构建出来的那个 zip」，而用户下载到的是
「GitHub 上那个文件」。2026-10-02 实测 v0.13.18：下载回来的 zip 与本机构建的完全一致
（都是 12040398 字节，SHA256 都是 `3be6233f…`），解开后 956 个文件、`--version` 打出
0.13.18、包内 `使用说明.txt` 与仓库那份逐字节相同（`e4315ead…`）、
`tools\gui_probe.py --exe` 报「界面已正常启动」。（`$gh` / `$py` 指本机的 gh.exe 与
Python，见本文开头的环境变量段。）

只动测试与维护文档（`tests/**`、`MAINTENANCE.md`、`HANDOFF.md`）时**不必发版**：免安装包里
没有这些文件，包内容与上一版逐字节一样，硬发一版只是噪音。**一旦改到会进包的东西**
（`main.py`、`xdao/**`、`packaging/使用说明.txt` 以及其它随包文件），版本号与 Release 都要
跟上；反过来说，也不能只改版本号不发版 —— CI 在 tag 上比 `__version__` 与最新 Release。

## 发布说明结尾要写「附件」段

`repo_check.py` 会看最新 Release 的说明里有没有讲清楚该下哪个附件。发布说明结尾照这样写：

```markdown
## 附件

- `xdao-export-vX.Y.Z-win64.zip`：免安装包，解压后双击 `xdao-export.exe`。
```

v0.13.10 的说明漏了这一段，体检里那条就成了「发布说明没有说明该下哪个附件」；补上后用
`gh release edit vX.Y.Z --notes-file docs/RELEASE_NOTES_vX.Y.Z.md` 更新已发布的正文。

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
  它的 `--exclude` 现在会在发请求之前核对路径写没写错（写错就是大文件被传上去），
  `--dry-run` 也不算「完全不写远端」：为核对树，它仍会在远端留下未引用的 blob/tree 对象。

### 体检里的「提交同步」别只看远端链

`tools/repo_check.py` 原来用 API 走远端历史来判断哪些提交推过了，而
`push_via_api.remote_chain()` 一次只往回取最近 100 条 —— 本地提交数一超过 100，
最早的本地提交就永远落在窗口外，`find_pushed_prefix()` 返回 0，于是整份历史被误报成
「本地有 102 个提交未推送到远端」（2026-10-01 真的发生了，同一次输出里却又写着
「远端 master = c1d8c1f5」，自己打自己）。现在的判法是：先比远端顶端与本地 HEAD，
一致就是同步；不一致再用本地的 `git log <远端 sha>..HEAD` 数未推送的提交；只有本地
没有远端那个对象时才回退到 API 链，并且**窗口取满时改报「这一项没查成」，不再诬告**。
`push_via_api.py` 也加了同样的 HEAD 短路（否则备用通道会把整份历史重推一遍，又造出
一套新 sha）。

### 盘点本仓改动用 `git status --porcelain`

`_scratch` 在 `<工作目录>\_scratch\...` 下（不在仓库里），所以拿改动清单去扫文本、
查行尾、查 BOM 时，要把 `git status --porcelain` 给出的仓库内相对路径拼上仓库前缀；
漏了这一步会得到「找不到路径」，或者干脆漏扫整个 `_scratch`。

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
5. **改动必须带测试**：`tests/` 是一千多项离线用例（要确切数字就跑
   `& $py -X utf8 -m pytest --collect-only -q` 数一下），新增功能请补用例，
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
   **2026-10-01 再收紧一层：连真实串号也不写。** 例子一律用 X岛官方的测试串
   `50000001`（`tests/test_live_notify.py` 里就是这么标的），或者 7 位那批明显编的
   （`7001234`、`7012345`）；不要拿某个真实长串当例子。这次全量自检翻出来的地方：
   `README.md` 的命令行示例（10 处）、`HANDOFF.md`、`docs/RELEASE_NOTES_v0.3.0.md` /
   `v0.3.1.md` / `v0.4.0.md`、`tests/test_gui_entry.py` 与 `tests/test_window.py` 里的串网址；
   顺手把本机个人目录（形如 `D:\<某人的工作目录>`）与本机解释器自己的目录名换成了
   `<工作目录>` / `<本机 Python>`。**已发布的 Release 正文要单独同步**
   （`gh release edit <tag> --notes-file …`）—— 正文是上传那一刻的快照，改仓库文件不会
   带着它一起变；提交信息与标签信息也要单扫一遍
   （`git log --all --format='%H%n%B'`、`git tag -l -n99`）。
   **这条规则现在有机器把关**：`tests/test_public_material.py` 扫 `git ls-files` 那批文件 ——
   8 位串号只认官方测试串 `50000001` 和编出来的 `12345678`；串网址与 `No.` 后面的编号只认这两个
   加 7 位那批（`7001234`、`7012345`）；本机目录名与用户名、token、真饼干值、非示例邮箱一律红。
   字节数后面跟「字节」两个字、日期写成 `2026-10-01` 这种带分隔符的样子就不会被误伤；报红时
   失败信息里带着文件与行号，照上面的改法换掉就行。
   **命令行开关也有机器把关**（2026-10-02 补上）：`tests/test_cli_docs.py` 拿
   `main.build_parser()` 当唯一的事实来源 —— ①`README.md` 与 `packaging/使用说明.txt`
   里的**命令行示例行**（以 `python main.py` / `xdao-export.exe` / `main.py` 开头）写到的开关
   必须真的存在；②解析器里每个不是 `argparse.SUPPRESS` 的选项，至少要在其中一份文档里
   露过面。加开关忘了写文档、文档里留着早就删掉的开关，都红在这两条上。当时一次都没被
   提到过的开关有五个：`--cache-dir`、`--verify`、`--image-mode`、`--pdf-timeout`、
   `--pdf-margin-mm`，已经补进「功能速览」和 PDF 那一节。
   **版本号也有机器把关**（2026-10-02 补上）：`tests/test_version_consistency.py` 拿
   `xdao.__version__` 当唯一事实来源，把四处**纯文本**逐一对一遍 —— `xdao/__init__.py`
   那句注释；`README.md` 的「最新版 **vX**」与附件表里的 zip 名；`packaging/使用说明.txt`
   的第 1 行与最上面那条「- 本版版本号：」；`docs/RELEASE_NOTES_vX.md` 得存在、标题是
   `# vX…`、并且写明附件叫什么。CI 里那个 job 只比 tag 与 `__version__`，文档侧以前没人管。
   **文档里写死的用例数也有机器把关**（2026-10-02 补上）：`tests/test_docs_facts.py` 拿
   `pytest --collect-only -q` 的真实收集数核两处 —— `HANDOFF.md` 那张测试表（每个
   `tests/test_*.py` 都得在表里、表里写的项数得对得上）与表头那句「单元测试 N 项」，
   还有 `README.md` 目录树里那句「N 个离线单元测试（M 通过，另 K 个真机用例默认跳过）」。
   两处以前都在悄悄漂：查的时候表里有 5 个文件根本没进表、7 个文件的项数还是旧的（差 145 项），
   README 那儿还写着 1119（实际已经 1267）。跨平台能核的只有总数与「M + K = N」这类算术关系，
   本机通过多少只有 Windows 上成立，所以不写进断言。
   **说明书的版本段也有机器把关**（2026-10-02 补上）：`tests/test_manual_blocks.py` 拿
   `packaging/使用说明.txt` 逐段核 —— 新的在上；每段最多一行「上一版（vX）的说明见下）」
   且必须指紧接着的那一段；老的一行写法只许连着排在下面；每段不超过 900 字
   （防整段被复制粘贴）。**这条规矩以前只靠本机脚本 `_scratch/check_usage_txt.py`，
   而它那两条正则只认 `上一版 vX 的说明见下`（中间是空格）的写法** —— 最近十段用的是
   `上一版（v0.13.17）的说明见下）`，于是「衔接对不对」和「有没有夹一整份复制」这两条
   在最新的十段上一直空转，而每次发版动的恰好就是这一片。搬进仓库时按格式无关重写，
   那个本机脚本现在转调这份用例，不再自己算一遍。
   **文本卫生也有机器把关**（2026-10-02 补上）：`tests/test_text_hygiene.py` 扫 `git ls-files`
   那一整批文本文件 —— ①开头不许有两个 BOM（真出过一次：`使用说明.txt` 变成
   `EF BB BF EF BB BF`，一路进了 zip）；②同一个文件里 CRLF 与裸 LF 不许混用；
   ③`packaging/使用说明.txt`、`诊断写入.ps1`、`诊断写入-双击运行.cmd` 必须**保留**开头那一个
   BOM（记事本认 BOM 中文才不乱码；PowerShell 5.1 读不带 BOM 的脚本会当 ANSI 解）。
   ④一个**确实和库里不一样**的文件，工作区行尾不许与索引里是两种 —— 那提交上去就是「每一行都
   变了」的假改动（真出过一次：一个改文档的本机脚本把 README/HANDOFF/MAINTENANCE 整份写成了 LF，
   没有任何用例拦得住，只靠肉眼看 `git diff --stat` 才发现）。判据是 `git ls-files --eol` 的
   `i/` 与 `w/`：只有「内容确实变了」时才要求两者同款，所以 git 自己归一化得掉的情形（索引 LF、
   工作区 CRLF 的 `.py`）不报；`attr` 里带 `eol=crlf` 的那两个 Windows 文件反过来要求工作区必须是
   CRLF。四个早年进库的历史文件（`README.md`/`HANDOFF.md`/`MAINTENANCE.md`/
   `docs/RELEASE_NOTES_v0.4.0.md`）索引里就是 CRLF，在 `LEGACY_CRLF_IN_INDEX` 里单列，
   **名单不许变长**（新内容按 `.gitattributes` 一律以 LF 入库）。
   **体检工具自己也有用例兜着**（2026-10-02 补上）：`tests/test_repo_check.py` 给
   `tools/repo_check.py` 喂一份假 API（`gh_token` / `request_json` / `local_commits` / `local_tree` /
   `worktree_changes` 全换成测试自己的），专盯「读不到东西时照样印没问题」这一类 —— 读不到
   `xdao.__version__` 时不许再补一句「与最新 Release 一致」（旧写法同一件事既报错又说没问题）；
   `git ls-tree` 读空时不许说「逐文件一致（0 个文件）」，只能说「这一项没查成」；工作区有没提交的
   改动时要在结论里露出来（只警告，退出码仍是 0）。三处都注入验过：退回旧写法，用例立刻红。
   **发布这一步也有用例兜着**（2026-10-02 补上）：`tests/test_make_release.py` 拿假 API 跑
   `tools/make_release.py` 的 `main()`（`gh_token` / `request_json` / `upload_asset` 全换成测试自己的），
   盯住「看着成功了、附件却没上去」这一类 —— 附件路径写错时以前只印一行「跳过（文件不存在）」，
   Release 照样建出来、退出码还是 0；现在**先验再动**：附件不在就一个请求都不发（连 Release 都不建）。
   发布说明按 `utf-8-sig` 读（记事本另存出来的 BOM 以前会跟着写进正文），空说明直接拦下；
   传完之后拿最终那份 Release 再核一遍附件在不在，缺了就报「没落到 Release 上」并返回 1。
   另外用例单独核了上传请求本身：附件名必须走 `?name=` 查询串 —— 走别的写法接口只取第一个点号
   之前的部分，中文名还会被截断成一个字。
   **备用推送通道也有用例兜着**（2026-10-02 补上）：`tests/test_push_via_api.py` 32 条，
   拿**真 git** 与假 API 一起跑 `tools/push_via_api.py` —— 提交对象的字节要能逐字节重建
   （特意手写一条「结尾没有换行」的提交当反证）、`git log --pretty=%B` 会擅自补一个换行
   所以不能用来拼对象、`--exclude` 写错时要在任何请求之前就停下、`--dry-run` 也不许说自己
   「未改动远端」。五处护栏都注入验过：退回旧写法，用例立刻红。
   **API 重建远端历史也有用例兜着**（2026-10-02 补上）：`tests/test_sync_from_api.py` 23 条，
   用**真 git 仓库**（临时建一个，两条提交 + 一次真 `merge --no-ff`）配假 API 跑
   `tools/sync_from_api.py`。盯住的三件事：① `commit_chain` 以前**只跟第一个父提交**走，
   远端只要有过合并提交，另一条支线的提交对象就不会被重建，而引用照样指过去、`git status`
   还看不出毛病 —— 现在每个父提交都跟，用例断言两条支线都在链里、每个父提交都排在它前面；
   ② 超过 1 MB 的 blob，JSON 接口只给空壳（`encoding` 不是 `base64`），旧写法拿空串去算 sha，
   最后报一句莫名其妙的「blob 不一致」—— 现在直接说「取不到 blob 内容 … 要改用 raw 媒体类型
   单独取」（空文件的 `content` 就是空串但 `encoding` 仍是 base64，用例专门钉了这条不许误判）；
   ③ 引用指过去之后就只印一句「完成」—— 现在会把工作区里与 HEAD 不一样的**已跟踪**文件列出来
   并返回 1（未跟踪文件不算），干净时才说「工作区里 N 个已跟踪文件也逐一对得上」。
   `--dry-run` 的文案也改了：它确实不动分支引用，但为核对 sha 会往本地 `.git` 写未引用的
   松散对象，不许再说「未改动远端」。五处都注入验过：退回旧写法，用例立刻红。
    **取 CI 日志这一步也有用例兜着**（2026-10-02 补上）：`tests/test_ci_logs.py` 37 条，拿假 API 与
    假 `urlopen` / `build_opener` 跑 `tools/ci_logs.py`，专盯「红的看成绿的」这一类 ——
    ① job 前面那个记号以前只认 `failure`，`cancelled` / `timed_out` / `action_required` /
    `startup_failure` / `stale` 全都印 ✓（一次被取消的运行看着跟全绿一样），现在这些都算没成功、
    还在跑的印 …；② `--job` 拼错时以前只印一行「没有匹配的 job」并返回 0，现在会把这次运行里
    真实的 job 名列出来、返回 1，并且**不写** `--save` 指定的文件；③ 以前只有失败或还在跑的 job
    才拉日志，全绿的运行上 `--grep 超时` 一行都没搜还静默返回 0 —— 现在带 `--grep` 一定把日志
    真的拉下来，匹配不到就明说一句「在 N 个 job、M 行日志里找过」并返回 1；④ `api()` 对 5xx 与
    429 会重试（其它 4xx 立刻停，重试没用）。四处都注入验过：退回旧写法，用例立刻红。

   **POSIX 分支的本地预演也有用例兜着**（2026-10-02 补上）：`tests/test_posix_check.py` 29 条，
   真跑 `tools/posix_check.py`（用例顶掉 `_run` 与 `make_wrapper`，假浏览器照 `tests/test_pdf.py`
   的契约写参数与 PDF），而且**一条都不跳过** —— Windows 上 PATH 里没有 `sh`，用例就退到
   `find_git_sh()` 找 Git 自带的 sh.exe（MSYS 的 `test -x` 对带 shebang 的 .sh 说可执行、
   对普通 .txt 说不可执行，结论与 POSIX 一致，所以这一项在 Windows 上也有真结论）。
   以前的三处毛病：包装脚本的 shebang 从不看；执行位只 `print` 一行**不算进结论**；
   参数只看 `flags.txt` 存不存在、内容对不对从不比对（docstring 却写着「参数原样透传」）。
   现在六项逐条判定，任何一项不过就 `不通过 —— N 项没过` 并返回 1。
   五处都注入验过：退回旧写法，用例立刻红。
   **清理残留目录这个「会删东西」的工具也有护栏兜着**（2026-10-02 补上）：`tests/test_clean_scratch.py` 37 条，
   真删也真造链接（Windows 上用 `mklink /J` 造目录联接、POSIX 上用 `os.symlink`，两个平台都真跑、不跳过）。
   补上的东西：盘符根目录、用户主目录、带 `.git` 的仓库目录、当前工作目录本身或它的祖先一律先拦下，
   并说清为什么（拦下就是没删，退出码 1）；新增 `--dry-run`；传进来是文件或链接就不走 `rmtree` ——
   `shutil.rmtree` 对符号链接和 junction 会直接报「Cannot call rmtree on a symbolic link」，旧写法把它
   当成「删除被拒」，接着去调权限、最后误报「被其它程序占用」，而真正管用的 `os.rmdir(junction)`
   从来没试过（本机 2026-10-02 验过：只摘链接，目标目录里的文件一个都没动；`icacls /t` 也不会穿过
   junction 改到目标那边）；目录读不动时不再让工具崩在半路；`free_up` 的两句 icacls 不再用
   `cmd /c … & …` 串起来、只看最后一句的返回码。八处都注入验过：退回旧写法，用例立刻红。
   **PDF 诊断的结论也有用例兜着**（2026-10-02 补上）：`tests/test_pdf_diag.py` 27 条，把 `find_browser`
   与子进程全顶掉、诊断产物顶到临时目录（默认写进仓库的 `.test-artifacts/`）。钉死的三件事：
   文件存在不等于成功（浏览器被拦下后写的错误页要按 `%PDF` 文件头判掉）；找不到浏览器、启动失败、
   一种方式都没成，都必须走进结论那句话里，而且退出码是 1；参数与正式实现是同一套（含
   `launch_flags()`）。九处都注入验过：退回旧写法，用例立刻红。
   同一轮还揪出：原来那项「经 cmd 启动」**从来没成过** —— `cmd /c "<命令>" > out 2> err` 会被
   cmd 自己的引号规则拆坏（用 `where.exe` 与真浏览器各试了四种引号写法，全部返回 1：「The
   filename, directory name, or volume label syntax is incorrect.」/「The network path was not
   found.」；把命令写进 `.cmd` 文件也过不了中文路径与代码页那一关）。现在换成「分离进程」：
   同一条命令行改用 `DETACHED_PROCESS` 启动、输出写文件，真机上六种方式全绿（都能出 %PDF）。
   **仓库门面同步这个「往公开仓库写」的工具也有用例兜着**（2026-10-02 补上）：
   `tests/test_repo_info.py` 38 条，假 API 顶掉 `gh_token` / `request_json`，还能装成「收下了请求
   但没照办」。补上的两道护栏：① 写之前先验推导出来的值 —— 描述空的、超过 350 字符的、话题空的、
   超过 20 个的、单个名字超过 50 个字符或带大写/空格/下划线/中文的，全在这里拦下并说清为什么，
   **一个请求都不发**（旧代码要等 GitHub 回一句含糊的 422，最坏的情况是把仓库门面清空）；
   ② 写完**再读回来核对** —— 只有读回来确实一致才说「已同步到远端（写完之后重新读回来核对过）」，
   还是不一样就返回 1 并写明「别当成同步好了」（旧代码只要请求没抛异常就印「已同步到远端。」
   并返回 0，等于把「远端没照办」报成成功：GitHub 会规范化话题、截断描述，写入也可能没落库）。
   十一处都注入验过：退回旧写法，用例立刻红。
   **GUI 探针这一步也有用例兜着**（2026-10-02 补上）：`tests/test_gui_probe.py` 41 条，
   窗口标题、进程、时间全是假的（`window_titles` 默认被顶成「炸」，漏顶的用例当场失败）。
   **为什么值得补**：它是发布清单的最后一步（拿下载回来的包跑一遍，看界面能不能起来），
   而旧写法只把标题里带「Unhandled exception」「fatal」的当失败，**别的标题一律报「界面已
   正常启动」** —— 出错时 PyInstaller / Windows 弹的对话框、甚至别的程序留下的窗口都能骗过
   这一步。现在认主窗口标题（`EXPECTED_TITLE = "X岛串导出"`，与 `xdao/gui.py` 里
   `root.title("X岛串导出")` 是同一个串）：看见了才退 0；只看见别的窗口退 2（需要人工确认）；
   看见错误对话框退 1。真机对照过：拿 `notepad.exe` 当 `--exe`，新写法给的是「没有看到主
   窗口「X岛串导出」，只看到：无标题 - 记事本 —— 需要人工确认」（退 2），旧写法会说「界面已
   正常启动」。另外两处也补了：`--exe` 路径不是文件、`--wait` 给了 0 或负数，都在**启动之前**
   拦下并给结论（旧写法是 `FileNotFoundError` 的 traceback）；收尾改杀进程树（win 上
   `taskkill /T /F /PID`），杀不干净会提醒去看任务管理器。十二处都注入验过：退回旧写法，
   用例立刻红。
   **GUI 截图这一步也有用例兜着**（2026-10-02 补上）：`tests/test_gui_shot.py` 48 条，
   窗口、DC、位图、像素全是假的（`_FakeLibs` 把每次 GDI 调用记进 `calls`，用例断言该还的
   句柄都还了）。它守的是「截图这步说自己成功时，图上真的得是界面」：**一片同色的图不算截好**
   （`looks_blank()` 采样整幅像素，颜色种类 ≤ 4 就报失败并说明窗口还没画出来 —— 旧写法对着
   一块白底照样说「已保存」）；`CreateCompatibleDC` / `CreateCompatibleBitmap` 建不出来、
   `GetDIBits` 返回的行数不足、`GetWindowDC` 拿不到，都在**报结论之前**拦下并带上原话
   （`WinError` 或退回去的 0）；`finally` 里按「先建的先还」释放 DC / 位图 / 窗口 DC
   （旧写法只在成功路径上还，失败一次就漏一个句柄）；写出来的 PNG 会再读回来核对签名、
   IHDR 里的宽高、位深 8、色型 2 与 IEND（`verify_png()` 连文件大小一起返回），对不上就报
   「文件不完整」而不是「已保存」；`--width/--height` 只收 1..10000 的整数，写错的尺寸和
   多余的位置参数都在**开窗之前**拦下（旧写法把 0 这类值静默忽略，用默认尺寸截一张还说成功）。
   十六处都注入验过：退回旧写法，用例立刻红。真机跑过：
   `python tools\gui_shot.py 图.png --width 1060 --height 760` 抓到的确实是界面
   （1060x760、33809 字节，抽样 201 种颜色）。

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
   浏览器到底能不能起来（会打印 `sys.frozen` 与「浏览器附加参数」，最后收一句结论：退出码 0 = 至少有一种方式真的生成了 PDF，1 = 一种都没成 —— 看到 1 就是「导出 PDF 在这台机器上会失败」）；
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
改用 `python tools/gui_probe.py --exe <exe 路径>` —— 启动、每 0.5 秒看一眼窗口标题
（最多 `--wait` 秒，默认 8；看见主窗口或错误对话框就提前收工）、结束进程树、给结论：
只有看见主窗口标题「X岛串导出」才退 0，只看见别的窗口退 2（需要人工确认），
出现「Unhandled exception in script」这类错误对话框退 1。
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
  它会等窗口真画出来（`grab_until_painted()` 轮询到整幅不再是同色，最长 6 秒），
  写完之后把 PNG 读回来核对宽高与 IEND 才说「已保存」；尺寸只收 1..10000 的整数，
  写错的值或多余的位置参数会在开窗之前报错退 1 —— 用例在 `tests/test_gui_shot.py`。
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

- **v0.13.25**（接 v0.13.24：拿不到饼干时不再「什么都不说」，等登录期间不再空转着连站点）：
  用户复验 0.13.24 时贴的截图里，一次性窗口**已经在站点里登进去了**（他自己打开
  `…/Member/User/Cookie/index.html` 能看到 5 块饼干、每行都有「应用」），程序却只写
  「浏览器里的饼干：memberUserspapapa、PHPSESSID（还没有 userhash）」。查下来两件事：
  ① `xdao/browser_login.py` 的 `looks_like_userhash()` 要求**全是可打印 ASCII**
  （`all(character.isprintable() and ord(character) < 128 …)`），而站点从导出页正文里抠出来的值
  可能带二进制字节解出来的字符 ⇒ 真值被判「不像」，而 `xdao/gui.py` 的 `_try_leaf_cookie_http()`
  在那种情况下 `return None, detail`（detail 多半是空串）⇒ **一个字都不说**。改法：粗筛只挡
  一眼就不是值的东西（空白、`;`、`<>`、成句中文 `_LOOKS_LIKE_PROSE`），真假交给
  `verify_userhash_live()`；被挡下时用 `_describe_value_shape()` 写下「几个字符、几个非 ASCII、
  几个百分号、开头是字母数字还是别的」（**不写值本身**，那是凭据）；`if not cookies` 与
  「站点既没给值也没给原话」也各留一句，前一类进 `_QUIET_HTTP_NOTES`（只进日志，不顶掉
  「这个窗口里还没登录」）。
  ② 等登录期间 worker 每 `BROWSER_LEAF_SECONDS`（5 秒）就调一次 `_try_leaf_cookie_http`，
  罐头根本没变 ⇒ 一趟登录最多两百来个请求白打。改法：`_jar_signature()` 算饼干罐指纹，
  只有变了才试（读不到罐头就当作变了）。
  `tests/test_browser_login.py` 214 → 222（放宽与「仍然挡住人话」两组参数化用例），
  `tests/test_gui_browser_login.py` 38 → 43（形状描述、指纹、空罐与「站点没说」两条留痕，以及用
  真 worker 跑的 `test_the_http_path_is_not_repeated_while_the_jar_stays_the_same`）。
  反向验证：把 `xdao/gui.py` 里那句 `if jar_now != last_http_jar:` 改成 `if True:`，节流那条用例
  立刻变红（实测连了 7 次）；改回来就绿。
  公开材料同时补了「用途与声明」（README 与 `packaging/使用说明.txt`），写明用途、版权归属、
  请勿违法使用，并给出岛规／使用指南／免责声明三条链接（后两条登记进
  `tests/test_public_material.py` 的 `ALLOWED_THREADS`，否则 8 位数字会被判红）。
  本机全量 **1664 passed / 7 skipped**（收集 1671 项）。
- **v0.13.24**（接 v0.13.23：修掉 HTTP 领饼干路上一处会 404 的拼地址）：把「应用饼干」搬回 HTTP
  之后回头核这条路，发现 `XdaoClient.apply_cookie()`（`xdao/client.py:597`）取 id 的正则
  `Cookie/(?:switchTo|export)/id/([^/\s"'<]+)` 会把列表页链接里的 `.html` **一起捕获**，而
  `:631`／`:635` 又固定补一次 `.html` ⇒ 链接带后缀时会请求 `…/switchTo/id/461037.html.html`，
  直接 404，userhash 也就拿不到（站点「饼干」页的链接确实是带 `.html` 的完整地址）。
  它一直没被用例发现，是因为那两个接口的路由键写得太宽松（`"/Cookie/export/"` 子串匹配），
  拼错的地址照样命中。改法只有一行：拼地址前
  `cookie_id = re.sub(r"\.html?$", "", cookie_id, flags=re.IGNORECASE)`（带/不带后缀都稳）；
  `tests/test_client.py` 20 → 23，新增「空 href 的成功页只请求一次」「跳转目标就是自己时只请求
  一次」「switchTo/export 两个地址逐字钉住」三条。反向验证：把归一化那行换成原样返回，第三条
  用例立刻报 `…/id/abc123.html.html` 变红，改回全绿。
  同一版还修了两处**只有真机才看得见**的地方（用户 m31364 报「日志说登录成功、左上角还写未登录」）：
  ① 角标是 `StatusPill`（`xdao/widgets.py:209` 的 `set(text, tone=…)` 才会改），而 `App.open_login()`
  以前只改 `self.status_var` —— 新增 `App.show_login_state()`（`xdao/gui.py`，紧邻 `open_login`）把两处
  一起写，`tests/test_window.py` 新增 `test_login_pill_follows_the_saved_cookie` 钉住（36 → 37）。
  ② 诊断三处补漏：`_jar_summary`（`xdao/gui.py:1389`）改用 `backend.userhash_from_cookies` 判断罐里
  到底有没有 userhash（真机上那句写死的「还没有 userhash」把人带偏过）；`_try_leaf_cookie_http` 跳过时
  返回新常量 `BROWSER_HTTP_SKIP_ANON_NOTE` 而不是空串（worker 里按值过滤，留痕但不顶掉「这个窗口里
  还没登录」）；新增 `BrowserLoginDialog._timeout_message(steps, diagnosis)` —— 等超时那条以前**不带**
  `_diagnosis()`，`_http_note` 只进「窗口已关」「payload」两条失败路径，所以用户日志里一条 HTTP 记录
  都没有。验饼干失败（`verify_userhash_live` 非 None）时那句话也进「试过的几步」。`test_gui_browser_login.py`
  37 → 38（新增 `test_the_timeout_message_carries_the_diagnosis_too`）。本机全量 **1651 passed / 7 skipped**
  （收集 1658 项）。
- **v0.13.23**（接 v0.13.22：把「导航用户的标签页」整个停掉，并把失败原因写进日志）：0.13.22
  发出去之后复验仍不通行，报「0.13.22还是无法登录，一直无限跳转」（截图里浏览器停在
  `…/Member/User/Cookie/switchTo/id/461037.html`，页面是站点自己的「饼干切换成功!」+
  「页面自动 跳转 等待时间： 1」，界面常驻提示写着「这个窗口里还没登录（页面停在登录页）：
  先在里面登录 X 岛。」，已经等了 110 秒）。判明两件事：① 那张跳转页的落点是页面里那个
  `<a id="href" href="…">`，成功时它是**空的**，`location.href = href` 于是把当前地址再载一次
  —— **页面自己原地重载**，这就是用户看到的「一直无限跳转」；程序那边 `_FIND_APPLY_JS` 用
  `new URL(raw, location.href).href` 解析空 href 也会得到当前页自己，`_follow_jumps()` 于是
  跟着自己跳满 `MAX_COOKIE_JUMPS`。⇒ 只要程序还导航用户的标签页去「应用」饼干，就必然把人留在
  那张页上，userhash 也始终种不上。② 失败时不出声：界面层 `_try_leaf_cookie_http()` 有两条
  **静默**出路（`read_site_cookies()` 读失败被吞成 `[]`、`may_skip_for_typing and not
  _jar_holds_a_session(cookies)`），两条都不产生任何提示，日志里只剩 CDP 那句「还没登录」，
  看不出 HTTP 那条路到底跑没跑。改法：**界面层固定 `navigate=False`**（只读页面状态与饼干罐，
  一个标签页都不碰），三条导航护栏（`BROWSER_LEAF_NAV_LIMIT` / `BROWSER_LEAF_RETRY_SECONDS` /
  `BROWSER_LEAF_CAP_HINT`）连同 `leaf_attempts` 那套计数一起删除；每轮先报一句饼干罐
  （`BrowserLoginDialog._jar_summary()` → `browser_login.summarize_cookies()`，只报名字、去重、
  `、` 分隔）；`read_site_cookies()` 读不到就**抛**（不再吞成空列表），调用方把原话写进提示；
  失败原因（窗口关闭 / 超时 / 出错）一律拼上 `_diagnosis()` ——「（<饼干罐诊断>；走 HTTP 领饼干：
  <原话>）」，它会跟着 `app.log("浏览器登录没成：…")` 进运行日志。用例：
  `tests/test_browser_login.py` 212 → 214、`tests/test_gui_browser_login.py` 36 → 37（删掉三条
  导航期用例，补 `test_the_dialog_never_navigates_the_users_tab` 等不变量）；五处注入全部变红；
  本机全量 **1646 passed / 7 skipped**（收集 1653 项）。

- **v0.13.22**（接 v0.13.21：把「领饼干」从浏览器跳转改成走 HTTP）：0.13.21 发出去之后用户复验，
  报「仍然是在切换饼干成功的界面一直跳」（截图里浏览器停在 `…/Cookie/switchTo/id/461037.html`，
  页面是自己那张「饼干切换成功!」+「页面自动 跳转 等待时间： 0」）。拿站点真容核对：那一页的
  跳转是**页面自己的 JS 倒计时**干的（`<a id="href" href="…">` + `setInterval` 到点
  `location.href = href`），成功页的 `a#href` 指回自己时就变成原地重载 —— 用户看到的
  「一直跳转」就是它。更关键的一条：**站点给 userhash 的落点从来不是那张跳转页的落地页，
  而是「导出页」的响应体**（`{"cookie": "…"}`，`_extract_userhash_from_export` 从 v0.6.1 起
  就是这么读的，见本文件里 v0.6.1 那条「接口 200 不等于内容页面」）。让浏览器自己去跳，就等于
  把「能不能拿到 userhash」押在渲染无关的东西上。改法：浏览器那条路只负责让用户把验证码认
  过去 —— `read_site_cookies()` 读一次整罐饼干，`apply_leaf_cookie_over_http()` 把整罐交给
  `XdaoClient.import_cookies()` + `XdaoClient.apply_cookie()`（认「跳转提示」页、跟跳、从导出页
  正文抠值），**一个页面都不动**。界面（`xdao/gui.py`）保留原来那条 CDP 路先跑，它领不到时再走
  HTTP 兜底（`_try_leaf_cookie_http`）；「用户可能还在打验证码」只在**这一轮没动过页面、也从来
  没真应用过**、且罐里只有一个匿名 `PHPSESSID` 时成立（`_jar_holds_a_session`）—— 否则「标签页
  被弹回登录页、罐里其实还登录着」那种场面会永远卡住；这条 HTTP 路不碰页面，所以不受
  `BROWSER_LEAF_NAV_LIMIT` 约束。`import_cookies` 按 `set_userhash` 的老规矩把**同名的旧饼干
  按名字扫干净**（不分域、不分路径，抽成 `_drop_cookies_named()` 给两处共用），免得请求头里
  出现两条 `userhash`。用例：`tests/test_client.py` 14 → 20、`tests/test_browser_login.py`
  207 → 212、`tests/test_gui_browser_login.py` 28 → 36（新增的 autouse fixture
  `no_real_http_leaf_cookie` 让既有 28 条只验 CDP 那条路、绝不真发 HTTP）；九处注入全部变红
  （罐子判据两头改错、整段停用 HTTP 兜底、值不校验、原话不挪到末尾、服务端那句话不原样带出、
  不扫同名旧饼干、会话饼干当过期、空条目照装）。本机全量 **1641 passed / 7 skipped**
  同版顺手修掉一处旧毛病：`create_exporter()` 那条「按能力逐级回退」的链里，`PdfBuilder` 不收
  `image_mode`，所以第一层必 `TypeError`、PDF 一定落到第二层 —— 而第二层原来只带
  `image_mode + pdf_options`，把 `browser_path` / `pdf_timeout` / `fallback_html` 三项悄悄丢掉了
  （真机表现：设置里指定了浏览器，PDF 导出还是用默认的那个）。第二层现在＝第一层去掉
  `image_mode`，`image_mode + pdf_options` 那层挪到第三层供 EPUB 回落；`tests/test_exporters.py`
  新增 `test_create_exporter_keeps_the_pdf_only_arguments`，`tests/test_watcher.py` 的监控 PDF
  用例改成假渲染并钉住渲染层收到的参数（它原来会真起浏览器，CI 上因此红过一条）。
  （收集 1648 项）。
- **v0.13.21**（真机上报「自动切换饼干之后一直卡在那里」；同版把开发期的界面截图工具
  `tools/gui_shot.py` 补上用例）：用户报的是 —— 在 0.13.20 里能正常登录、也自己跳到了
  「饼干」页，程序自动切换饼干之后就再也没有下文。真机截图证据：浏览器停在
  `…/Member/User/Cookie/switchTo/id/461037.html`，页面是站点自己的「跳转提示」页
  （笑脸 +「饼干切换成功!」+「页面自动 跳转 等待时间： 1」），而界面常驻提示写着「这个窗口里
  还没登录（页面停在登录页）：先在里面登录 X 岛。」，最后日志留下「浏览器登录没成：浏览器
  窗口已经关掉了，还没取到饼干。」。根因是**站点那一跳得由站点自己走完**：`userhash` 是在
  倒计时页自动跳到落地页的那一下由主站种下的，而老代码 `_follow_jumps()` 只在读到的**那一
  瞬间**找 `a#href` / meta refresh —— 倒计时页上什么都找不到，于是「一跳都不跳」，紧接着只读
  一眼饼干罐（当然还没有），**马上导航去 `export/id/<id>.html` 兜底，把标签页从倒计时页上
  拽走**，站点自己的第二跳再也不会发生；那次 export 没有 token，被弹回登录页，下一次轮询
  读到的页面状态就是「登录页」，老代码在这句「还没登录」上早退（当成用户还在打字），从此每
  5 秒重复同一句，再也不去应用饼干；`BROWSER_LEAF_NAV_LIMIT = 2` 让这个状态不可逆。
  改法：① `_FIND_APPLY_JS` 增报 `countdown`（正文里有「等待时间 / 自动跳转 / 跳转提示」），
  `kind` 也认成 `jump`；② `_follow_jumps()` 改成**等站点自己跳完**
  （`JUMP_WAIT_SECONDS = 8.0` 内每 `NAVIGATE_POLL` 重读页面状态，出现 `jump` 就跟着跳，
  页面不再是跳转/倒计时页就收手，正在跳时读失败属正常）；③ 新增 `_wait_for_cookie()`
  （`COOKIE_WAIT_SECONDS = 4.0`，每 `COOKIE_WAIT_POLL = 0.5` 看一眼饼干罐），应用之后不再
  只读一眼；④ `fetch_leaf_cookie()` 多了 `waiting_for_login`，界面在**已经真应用过**
  （`leaf_attempts > 0`）之后不再把「页面停在登录页」当成「用户正在打字」，仍然继续去领
  饼干；⑤ `BROWSER_LEAF_NAV_LIMIT` 2 → 6，用满时把 `BROWSER_LEAF_CAP_HINT` 推到常驻提示
  （旧写法静默退化成只读）。用例：`tests/test_browser_login.py` 202 → 207、
  `tests/test_gui_browser_login.py` 24 → 28（`_SiteHoppingSession` / `_LateJarSession`
  钉住「倒计时页跳走之后才种饼干」「饼干罐要等几次才出现」，界面上钉住「被弹回登录页之后
  还会再去饼干页」与上限说明）；八处注入全部变红（倒计时不认、`_follow_jumps` 退回读一眼
  就走、应用后只读一眼、两处 `waiting_for_login` 不生效、界面不转发该参数、上限提示不推、
  上限改回 2）。本机全量 **1621 passed / 7 skipped**（收集 1628 项）。
- **v0.13.20**（登录慢一点就再也领不到饼干 —— 0.13.18 真机上复现）：
  程序开浏览器时把标签页停在登录页，头两次「领饼干」在登录页上**什么都没碰**就返回了
  （结论「还没登录」），可导航次数照样被扣掉；等用户登进去，次数已经用满，之后每轮只重读
  饼干罐 —— 而 `userhash` 只有真去「应用」（导航到 `switchTo`）才会被主站种下，于是
  「明明登录好了，界面一路等到超时」。真机截图证据：状态行「已经等了 60 秒」、常驻提示
  「自动取饼干这条路试过了：浏览器里还是没有 userhash。」，而浏览器里已经登录成功。
  改法：只有**真动过**用户标签页的那次才扣次数（`if navigate and navigated:
  leaf_attempts += 1`），登录页上的尝试不扣 —— 用户登进去后下一次轮询（≤5 秒）就会去领；
  超时那句诊断也改成按顺序列出试过的几步（`leaf_hints[-3:]`，①②③），不再被收尾那句
  「还是没有 userhash」盖掉有用信息。真机验收：`tests/test_gui_browser_login.py` 24 项全绿，
  两处注入各自变红（还在登录页上也扣次数 → 1 红；只留最后一条诊断 → 1 红），
  本机全量 1564 passed / 7 skipped。
- **v0.13.19**（写明白「用浏览器登录」只支持 Chromium 内核，程序逻辑一行没动）：
  「用浏览器登录」整条路走的是 CDP，只有 Edge / Chrome / Chromium / Brave 能用；Firefox 是另一套
  内核（Marionette / WebDriver BiDi），本项目没实现 —— 可界面与两份公开文档从没把这件事说明白，
  装了 Firefox 的用户会一路白等。补的是话、不是功能：登录窗开头两行（`xdao/gui.py`）、
  找不到浏览器时的提示、`README.md` 的功能条、`packaging/使用说明.txt` 的「支持的浏览器」那节，
  再加一条机器把关 `tests/test_browser_scope.py`（界面与两份文档都写着，且候选表里确实没有
  Firefox；将来真加了支持，它会提醒一起改说明）。因为动到会进包的东西，版本号四处跟着升。
  真机验收：本机全量 **1562 passed / 7 skipped**（222.66 秒，收集 1569 项）；文本卫生正常
  （`MAINTENANCE.md` CRLF 1361 / 裸 LF 0、`README.md` CRLF 476 / 裸 LF 0、
  `使用说明.txt` BOM 1 / 裸 LF 516）；打包版 `--version` → `X岛串导出工具 0.13.19`，
  `--selftest` 与 v0.13.18 那份逐行相同（只差版本号、exe 路径、缓存回落目录三行，都是退出码 1）；
  免安装包里的 `使用说明.txt` 与仓库文件逐字节一致（41105 字节 / 516 行）；
  `tools/repo_check.py` 14 项全绿。程序本体、界面布局、导出结果与 0.13.18 一致。

- **v0.13.18**（免安装包里的说明补上五个命令行开关，只动文字与测试）：
  `packaging/使用说明.txt` 会随包发出去，却一次都没提过 `--cache-dir`、`--verify`、
  `--image-mode`、`--pdf-timeout`、`--pdf-margin-mm`。补写的同时把「文档与解析器一致」
  做成机器把关：新增 `tests/test_cli_docs.py`（两条规则写在「硬性约定」第 8 条里）。
  真机验收：本机全量 **1252 passed / 7 skipped**（143.84 秒）；新用例反向验过 ——
  往 `README.md` 的示例行塞一个 `--definitely-not-a-flag`，它红在 `README.md:477`
  并指出是哪个开关（`git checkout` 还原；注意 `git checkout -- <文件>` 会把同一文件里
  未提交的改动一起还原，所以那次是重跑脚本补回来的）；文本卫生正常
  （`MAINTENANCE.md` CRLF 1132 / 裸 LF 0、`README.md` CRLF 476 / 裸 LF 0、
  `使用说明.txt` BOM 1 / 裸 LF 505）；打包版 `--version` → `X岛串导出工具 0.13.18`，
  `--selftest` 的输出与 v0.13.17 那份逐行相同（只差版本号与 exe 路径两行，都是退出码 1：
  「配置目录」「导出目录」写不进去是开发者机器上沙箱限制，缓存已按设计回落到导出目录）；
  免安装包里的 `使用说明.txt` 与仓库文件逐字节一致（40243 字节 / 505 行）；
  `tools/repo_check.py` 13 项全绿。程序本体、界面、导出结果与 0.13.17 一致。

- **v0.13.17**（用浏览器登录：领饼干从「页面里 fetch」改成「导航」）：真机上「登录好了、页面上也进了
  用户系统，程序却一路等到超时」的根因是**站点自己的「应用」是跳转式的**。真机取证（匿名请求
  `Member/User/Cookie/index.html`）：HTTP 200、正文 1569 字节，`<title>跳转提示</title>`，
  正文是一句「并没有权限访问_(:з」∠)_，请等待自动跳转」，落点在 `<a id="href"
  href="/Member/User/Index/login.html">`，页面自己的脚本 `setInterval` 每 1000 毫秒把 `#wait`
  减一、3 秒后 `location.href = href`（**不是** HTTP 重定向、也不是 meta refresh）。v0.13.15 的
  `_APPLY_COOKIE_JS` 在页面里用 `fetch` 复刻 `switchTo` / `export`：`fetch` 拿回来的只是这张提示页
  的 HTML，它既不执行页面里的跳转、也不会发出第二跳，于是主站从来没把 userhash 种进浏览器。
  改法：`xdao/browser_login.py` 删掉 `_APPLY_COOKIE_JS` / `build_apply_cookie_script`，换成
  `_FIND_APPLY_JS` / `build_find_apply_script`（只读 DOM、不发请求）+ `fetch_leaf_cookie()`
  （用 `Page.navigate` 走浏览器自己的路：当前页 → 跟着「跳转提示」页 → 「饼干」页
  `COOKIE_LIST_PATH` → 最后一块的 `switchTo/id/<id>.html` → 重读饼干罐 → 再导航一次
  `export/id/<id>.html` 从页面正文里抠，最多 `MAX_COOKIE_JUMPS` 跳、单次导航等
  `readyState == 'complete'`）+ `LeafCookie(value, detail, navigated)` + `cookie_urls_for()`
  （站点根 / 饼干页 / 当前页，`Network.getCookies` 只回「会发给这个地址」的饼干）+ 导出页三种形态的
  `userhash_from_export_text()`（`apply_leaf_cookie()` 留作只要值的老签名）。界面侧 `xdao/gui.py`：
  `BROWSER_LEAF_NAV_LIMIT = 2` 次上限（之后只重读饼干罐）、导航过就按
  `BROWSER_LEAF_RETRY_SECONDS = 20.0` 缓一缓、用户还停在登录页时**绝不导航**；新增**常驻诊断行**
  `_set_hint()`（状态行每 15 秒被整句替换，失败原因一闪就没了），超时提示拼上最后试到的一步（进运行
  日志）。用例：`tests/test_browser_login.py` 的 `_ScriptedSession` 按「地址 → 页面状态」脚本化
  （20 条新用例 + 签名契约守卫），`tests/test_gui_browser_login.py` 的 `_FakeSession` 能演导航与
  页面状态（含「不许把用户从登录表单上拽走」「超过上限不再导航」两条护栏）。
  真机核验（`_scratch/probe_leaf_real_v1317.py`，真 Edge + 真站点 + 匿名）：起始页被认成登录页、
  `fetch_leaf_cookie` 零导航、回「这个窗口里还没登录（页面停在登录页）…」；开「饼干」页时认出站点
  的「跳转提示」页（`kind='jump'`、落点 `Member/User/Index/login.html`）并**导航跟着它跳**过去，
  最后落在登录页、回「跟着站点的跳转回到了登录页…」、`navigated=True`；读饼干一次问 4 个地址；
  临时用户目录删干净。**运维事实**：站点同一地址有时直接 302 到登录页、有时回「跳转提示」页
  （探针要先清饼干才稳定碰到后者），两条路代码都走通 —— 这也解释了为什么真机上偶尔「一下就好了」、
  偶尔一直等到超时。
- **v0.13.16**（纯文字：公开内容里不再复述来源）：把仓库里剩下的「拿来源当正文」的句子改成直接
  写现象与规则 —— `docs/RELEASE_NOTES_v0.13.14.md`（现象段与改法第一条）、
  `docs/RELEASE_NOTES_v0.10.2.md` 表格里的句式示例、`xdao/gui.py` 的两处注释、
  `tests/test_window.py` 的两处注释、`tests/test_gui_entry.py` 的文档字符串、`README.md` 与
  `packaging/使用说明.txt` 里举的那个例子。同时把 6 个已发布 Release 的正文按仓库文件重推
  （v0.13.14 / v0.13.7 / v0.10.2 / v0.6.1 / v0.6.0 / v0.3.0）—— 其中 v0.13.7 / v0.6.1 /
  v0.6.0 / v0.3.0 是 v0.13.14 那轮改了文件却没同步到 GitHub 的老正文。
  规则本身（本文件「## 注释与文档的写法」）保留「有人报过…」这类句式名，那是在说规矩。
  真机核验：39 个 Release 正文逐条重扫（只剩上面那 6 个改过的，其余干净）；全仓 grep 复查
  （剩余命中只有规则本身、泛用的产品用语和开发测量记录，没有任何来源原话）；全量两轮；
  打包版 `--version` / 自检基线与升级 E2E 照旧。
  **发行验收（v0.13.16）**：打包 `xdao-export-v0.13.16-win64.zip`（12031049 字节 / 956 条目 /
  SHA256 `b3109c06a45f66bf5342f3f994d4e43e4ea49b1e3772754d8e2fb536a81a293f`），组包前自检
  使用说明.txt 474 行 / BOM=True；冻结核验 `_scratch/check_frozen_code_v1316.py` → 「包里的代码是对的」
  （PYZ 211 个模块、26 个 xdao 模块，v0.13.15 那批钉子全在）；打包版真机：exe 2775682 字节、
  `--version` → 0.13.16、`--selftest --offline --check-browser` 基线 exit 1、跑完 `%TEMP%` 里没有
  产品自己的临时资料目录；敏感串扫描 11 个文件 33 处命中全是既有的（MAINTENANCE/README/tests/gui.py
  里的工具脚本引用），新发布说明与提交信息 0 命中；提交 `7c218b8` + 标签 v0.13.16 → Release 已发布
  （附件 11.47 MB）；`tools/repo_check.py` 13/13（114 个提交 / 116 个文件）；升级 E2E
  `_scratch/probe_upgrade_e2e_v1316.py`：现场 0.13.15 → 0.13.16 自己下载/自检/替换/重启，见证进程
  记下现场版本 0.13.16、换上去的 exe 与包里的那份字节一致、现场 993 项、备份
  `live.old-20261001-225208`、记号目录清干净、普通暂存目录年龄 5.3 秒 < 阈值 600 秒；收尾之后没有
  xdao-export 进程、没有用程序资料目录的浏览器进程、`%TEMP%` 只剩升级暂存目录；CI 绿。
  另外这一版把 `packaging/使用说明.txt` 开头丢掉的那个 UTF-8 BOM 恢复了（前几版发包时都带着它）。

- **v0.13.15**（修「浏览器登录成功后程序一直没反应」）：`xdao/browser_login.py` 的
  `_APPLY_COOKIE_JS` 原来只在当前页面的 DOM 里找 `Cookie/switchTo/id/<id>` 链接 —— 登录后页面
  停在论坛/用户首页时找不到，兜底每 5 秒静默返回 None，5 分钟才超时；现在当前页找不到就自己
  `fetch` 一次 `.../Member/User/Cookie/index.html` 再解析（同源、带登录会话；匿名时拿回的是站点
  那张「跳转提示」页，不会误判），`grab()` 统一 8 秒 `AbortController` 超时 + `credentials:
  'include'`。界面侧（`xdao/gui.py`）新增 `BROWSER_PROGRESS_SECONDS = 15.0` 与
  `_waiting_status()`，队列新增**替换**语义的 `("status", …)` 消息，超时文案补「在自己平时用的
  浏览器里登录不行 / 可以用『直接粘贴饼干登录』」。
  真机核验：取饼干那条链（真 Edge + 临时资料目录 + 真 CDP）9/9 —— 改动前的脚本在「已登录但页面
  不是列表」时返回空（复现），改动后取到饼干，匿名页不被当成饼干，当前页就是列表时不多发请求；
  界面这一层（真 Tk 对话框 + 真浏览器）6/6 —— 等待期间状态栏报时长、登录后窗口自己关掉、userhash
  写进客户端；收尾 `%TEMP%` 无残留、无遗留进程。
  用例：`tests/test_browser_login.py` 改 1 条新增 1 条、`tests/test_gui_browser_login.py` 新增 2 条；
  全量两轮。
  发行验收：打包（PyInstaller onedir）→ `xdao-export-v0.13.15-win64.zip`，12032106 字节 /
  956 条目 / SHA256 384c31882a07f63b8b98a9096565d75a138165665ace05c0b1362d08205a5e0b；
  冻结核验 `_scratch/check_frozen_code_v1315.py` → 「包里的代码是对的」（PYZ 211 个模块、26 个
  xdao 模块，新增 7 条钉子 + 原有 40 余条全在）；打包版真机核验：`xdao-export.exe`（2775682 字节）
  `--version` → 「X岛串导出工具 0.13.15」退出码 0，`--selftest --offline --check-browser` 仍是基线
  （2 处走不通、1 处要注意；浏览器那条「起得来，Edg/154.0.4258.48，调试端口 65429 答得上话 0.5 秒」），
  跑完 `%TEMP%` 里没有产品自己的临时资料目录；升级 E2E（`_scratch/probe_upgrade_e2e_v1315.py`）：
  现场 v0.13.14 → 新版自己下载/自检/替换/重启，见证进程记下现场版本 0.13.15、换上去的 exe 与包里
  那份字节一致、备份 `live.old-20261001-222359`、现场 993 项、记号目录清干净、普通暂存目录年龄
  5.0 秒 < 阈值 600 秒；提交 `92e74f4`、标签 `v0.13.15`、Release 已发布（附件 11.47 MB）、
  体检 `tools/repo_check.py` 13/13、全量两轮各 1216 passed / 7 skipped。
- **v0.13.14**（只改文字，不改功能）：全仓去掉「复述来源」的句式。README、`packaging/使用说明.txt`、
  `docs/RELEASE_NOTES_*.md`、MAINTENANCE、源码注释与用例文档字符串里的「有用户问…」「有人反馈…」
  「有人报过…」「这位用户 / 那位用户…」一律改成直接写现象与规则；规则本身（「## 注释与文档的写法」）
  不动。改写脚本 `_scratch/apply_neutral_wording.py`（25 条替换，先全部替换成功再统一写盘）。
  没改动的：`诊断写入.ps1` 的「用户名」等无关命中，以及泛指假设场景的句子。
  用例：`check_text_hygiene.py` / `check_usage_txt.py` 都 exit 0；全量两轮（各 `1212 passed, 7 skipped`）。
  历史 Release 正文也按新口径用 `gh release edit --notes-file` 重发过（v0.13.9 / v0.13.12 / v0.13.13）。
  **真机验收（打包版）**：exe 2774733 字节；`--version` → 「X岛串导出工具 0.13.14」退出码 0；
  `--selftest --offline --check-browser` 仍是基线（2 处走不通、1 处要注意，退出码 1，浏览器那条照样
  「起得来」：Edg/154.0.4258.48、调试端口 61372 答得上话 0.4 秒），跑完 `%TEMP%` 里产品自己的临时
  目录 0 个。冻结核验 `_scratch/check_frozen_code_v1314.py` → 「包里的代码是对的」（PYZ 211 个模块、
  26 个 xdao 模块，新增钉子「典型现场是 Edge 退出码」全在）。升级 E2E
  `_scratch/probe_upgrade_e2e_v1314.py`：现场 v0.13.13 → 新版自己下载/自检/替换/重启，见证进程记下
  「现场版本 0.13.14、换上去的 exe 与包里的那份字节一致 True、现场 993 项、记号目录清干净、
  普通暂存目录年龄 5.2 秒 < 阈值 600 秒」。`tools/repo_check.py` 13/13；CI run 36870960597（master）
  与 36870969935（标签 v0.13.14）都是 success。
- **v0.13.13**：**只改文案**：把「用浏览器登录」为什么是个干净窗口讲清楚，并给「直接粘贴
  饼干登录」写上复制步骤。现象是「用浏览器登录」打开的窗口与平时使用的浏览器不是同一份资料目录：
  没有已装插件，也没有保存的密码（看起来像另一个软件）。同一个 Edge，只是换了一份临时资料目录；
  Chrome/Edge 136 起不允许在默认资料目录
  上开调试口（https://developer.chrome.com/blog/remote-debugging-port ），必须带非默认
  `--user-data-dir` 才读得到登录饼干。
  改动：①新增模块级常量 `PASTE_WHY` / `PASTE_STEPS` 与 `PasteCookieDialog`（入口 `ask_pasted_cookie()`；
  `_confirm()` 用 `parse_userhash_input()` + `looks_like_userhash()` 校验，失败用 `messagebox.showwarning`
  提示且**不关窗**），替掉 `simpledialog.askstring`（导入也一并删掉）；②`LoginDialog._manual_userhash`
  与 `BrowserLoginDialog._manual_userhash` 都改成 `grab_release()` → `ask_pasted_cookie()` → `finally`
  收回 grab；③`BrowserLoginDialog` 的介绍改成 7 行显式换行 —— Tk 的中文折行是按字符切的，长句会把
  「X岛」劈成两行、还让下一行以逗号开头。
  用例：`tests/test_gui_entry.py` 4 条（含 grab 收回、以及「讲清了没」的文案断言）、`tests/test_window.py`
  5 条（真窗口：粘整段 cookie 摘出 userhash、粘垃圾时窗口不关、最小尺寸下不被裁、`ask_pasted_cookie()`
  的两条返回路径）、`tests/test_gui_browser_login.py` 的粘贴用例改走 `gui.ask_pasted_cookie`。
  真机：`_scratch/probe_paste_dialog_v1313.py` 两个窗口在最小尺寸下「被裁 0 个」，用浏览器登录窗口的
  介绍 7 行干净无断词，真窗口里粘整段 cookie 摘出 `userhash`；截图
  `_scratch/_shots/browser_login_intro.png.png`、`_scratch/_shots/paste_cookie_560x430.png.png`。
  **真机验收（打包版）**：exe 2774727 字节；`--version` → 「X岛串导出工具 0.13.13」退出码 0；
  `--selftest --offline --check-browser` 仍是基线（2 处走不通、1 处要注意，退出码 1，浏览器那条照样
  「起得来」：Edg/154.0.4258.48、调试端口 58995 答得上话 0.4 秒），跑完 `%TEMP%` 里产品自己的临时
  目录 0 个。冻结核验 `_scratch/check_frozen_code_v1313.py` → 「包里的代码是对的」（PYZ 211 个模块、
  26 个 xdao 模块，新增钉子全在）。升级 E2E `_scratch/probe_upgrade_e2e_v1313.py`：现场 v0.13.12 →
  新版自己下载/自检/替换/重启，见证进程记下「现场版本 0.13.13、换上去的 exe 与包里的那份字节一致
  True、现场 993 项、记号目录清干净、普通暂存目录年龄 4.9 秒 < 阈值 600 秒」。`tools/repo_check.py`
  13/13；CI run 36868219766（master）与 36868226373（标签 v0.13.13）都是 success。全量测试两轮各
  `1212 passed, 7 skipped`。
- **v0.13.12**：修「窗口拉小的时候说明文字被裁」这一类毛病（起因是自检窗口的介绍，
  普查下来一共四处）。`xdao/gui.py` 只加两个模块级辅助、不碰业务逻辑：
  `wrap_to_width(label, *, minimum=200)`（`<Configure>` 里
  `wraplength=max(minimum, event.width - theme.gap(1))`；调用处必须
  `pack(fill="x")` 或 grid `sticky="ew"`，否则「改 wraplength → 请求宽度变 → 宽度又变」会抖）、
  `fold_buttons_when_narrow(row, primary, secondary, *, gap_steps=1.5)`（建 `top`/`bottom`
  两个子框架，`needed = gap*(n-1) + Σwinfo_reqwidth()`，`event.width < needed` 时把次要按钮
  `pack(in_=bottom, side="right")` —— **主按钮永远留在 `top`**，第一版把两组一起丢进 `bottom`，
  真机上量到第一行高度 0）。
  用处：自检窗口的介绍与状态行、设置窗口 6 处说明（保留 `wraplength=theme.gap(80)` 作初始值，
  免得 Card 被整句宽度撑开）、主窗口缓存行、运行日志卡片头的说明文字（改成自己占一行）；
  自检窗口按钮排改用折行；`SelftestDialog.minsize(520, 400)` → `(520, 460)`；
  主窗口缓存行改成「按钮先 `pack(side="right")`、标签再 `fill="x", expand=True`」
  （原来标签先 pack，940/1060 宽时都把两个按钮挤到卡片外，看不见也点不到）。
  用例：`tests/test_window.py` 新增 6 条（`wrap_to_width` 跟宽度走 / 极窄时保底 /
  折行后主按钮仍在上排 / 主窗口按钮都在父容器里 / 自检介绍在 520x460 换成多行且按钮不出窗 /
  `minsize == (520, 460)`）；另加了 `_widgets()`、`_settle()`、`_holder()` 三个辅助。
  **真机核验**：`<工作目录>\_scratch\probe_dialog_text_v1312.py`（`App()` → 主窗口
  940x682 与 1060x760、自检 600x652/520x460/900x600、设置、监控逐个量：标签「需要宽度 > 实际
  宽度」记被裁、按钮「实际宽度 < 需要宽度」记被挤、按钮矩形超出窗口记出窗、同一行子控件宽度和
  超容器记行溢出）→ **全部 0**；截图 `_shots\main_940x682.png`（按钮回到卡片里）、
  `_shots\selftest_520x460.png`（介绍换成三行全显示）目视确认。
  **教训**：Card 里的说明标签不给初始 `wraplength` 的话，`Card._on_body_configure()` 会按整句
  宽度把卡片撑到 1188 px（`xdao/widgets.py:126-127` 记着这个历史坑），所以「给保守初始值 +
  跟着宽度改」两层都要有。
  **真机验收（打包版）**：`xdao-export.exe --version` → 「X岛串导出工具 0.13.12」（exe 2772320 字节）；
  `--selftest --offline --check-browser` → 仍是历次基线（2 处走不通、1 处要注意，退出码 1），
  浏览器那条照样「起得来」；跑完 `%TEMP%` 里只剩升级暂存目录 `xdao-export`，产品自己的临时目录 0 个。
  冻结核验 `_scratch/check_frozen_code_v1312.py` → 「包里的代码是对的」（两个新 docstring 与
  `wrap_to_width`/`fold_buttons_when_narrow` 两个名字都在 PYZ 里）。
  升级 E2E `_scratch/probe_upgrade_e2e_v1312.py`：现场 v0.13.11 → 新版自己下载/自检/替换/重启，
  见证进程记「现场版本 0.13.12、换上去的 exe 与包里那份字节一致 True、现场 993 项、记号目录清干净、
  普通暂存目录留着（年龄 4.9 秒 < 阈值 600）」。`tools/repo_check.py` 13/13。
  CI：`1b8724a` 两个 run（36861844229 master / 36861848943 标签）都是 success。

- **v0.13.11**：加一条**启动时清扫** —— 程序被强杀时没人收尾，那些临时资料目录会一直
  留在 `%TEMP%` 里（v0.13.9/v0.13.10 只盖住「程序自己收尾」的路径；真机上量到过 147 个）。
  `xdao/browser_login.py` 新增 `TEMP_DIR_PREFIXES`（`xdao-export-browser-profile` 与
  `xdao-browser-check-` 两种前缀，`_is_temp_profile_dir()` 改成按它判断 —— 顺带把
  `--check-browser` 自建的那份也纳进来，那份以前只靠 `browser_check.check_one()` 末尾一次
  `shutil.rmtree(ignore_errors=True)`，浏览器正咽气时静默失败，真机 `%TEMP%` 里攒了 5 个）、
  `SWEEP_MIN_AGE = 3600.0`、`SWEEP_PROBE_TIMEOUT = 0.5`、`_profile_in_use(profile)`
  （读 `DevToolsActivePort` → `parse_devtools_port()` → `cdp._http_json_once()` 问
  `/json/version`，任何一步失败都算「没人用」）、
  `sweep_stale_temp_profiles(*, min_age=SWEEP_MIN_AGE, keep=())`（三条全中才删：位置+名字前缀、
  mtime 够老、端口没人答话；`keep` 里的不碰；删不掉的不计数）。调用点在 `xdao/gui.py` 启动处
  的后台线程 `_sweep_temp_profiles()`（清不干净也不影响启动）。
  用例：`tests/test_browser_login.py` 新增 9 条（删旧的/留新的与别人的、端口答话的不动、
  `keep`、拿不到临时目录返回 0、删不掉不计数、符号链接不动、`_profile_in_use` 四种形态、
  `_is_temp_profile_dir` 两种前缀、自检目录由 `stop()` 收掉），`tests/test_gui_entry.py` 加
  启动线程那条。
  **真机核验**：`<工作目录>\_scratch\probe_sweep_v1311.py`（造旧的/新的/别人的目录各一，
  再真起一个浏览器）→ 删 2 留 3、活浏览器那份 `_profile_in_use() == True`、门槛 0 的清扫也不动它、
  `stop()` 后目录消失、收尾 0 残留；第一跑顺带清掉了真机上积的 5 个自检遗留目录。
  `<工作目录>\_scratch\probe_check_dir_cleanup.py`（把 `browser_check` 自己那次 `rmtree`
  换成空操作）→ 自检目录照样消失，证明是 `LoginBrowser.stop()` 兜住的。
  源码跑 `--selftest --offline --check-browser` 之后 `%TEMP%` 里 0 个 `xdao-browser-check-*`。
  打包版（`<工作目录>\xdao-export-v0.13.11-win64.zip`，12024842 字节 /
  SHA256 `ea2372505f9bcc8b8748cede8ec877e6e56abdb6f54fbaa94c9f89973e1adc0c`）跑
  `--version` → 「X岛串导出工具 0.13.11」、`--selftest --offline --check-browser` 仍是基线
  「自检发现 2 处走不通、1 处要注意」；跑完 `%TEMP%` 里我们自己的目录 0 个。
  真机升级 E2E（`<工作目录>\_scratch\probe_upgrade_e2e_v1311.py`）：现场 v0.13.10 → 新版自己
  下载、自检、替换、重启，见证进程量到「换上去的 exe 与包里的那份字节一致: True」、
  现场版本 0.13.11、`.old-` 备份与暂存目录的处理都照旧。
  `tools/repo_check.py` 13/13、两个 CI run（master 与标签）都 success。

- **v0.13.10**：补上 v0.13.9 漏掉的一处收尾 —— **临时资料目录在建出来过的情况下一定收掉**。
  v0.13.9 只记「成功用上临时目录」这一种情形（`_fresh_used`），
  `LoginBrowser._launch()` 里 `Popen` 抛 `OSError` 时也**不经过** `start()` 的换目录重试分支，
  于是「浏览器一起来就退」（重试全败）和「启动就抛错」两条失败路径都没人删目录：
  真机上 `%TEMP%` 积了 147 个 `xdao-export-browser-profile-*`，123 个是空壳。
  改法：去掉 `_fresh_used`，`fresh_profile_dir()` 建出目录时就记到 `self.temp_profile`，
  `__exit__` 里 `cleanup_temp_profile()` 无条件调用（它自己会判断 `temp_profile` 在不在），
  `_launch()` 的 `except OSError` 里补一次 `cleanup_temp_profile()`。
  备用目录（`fallback_profile_dirs`）不归程序删。
  真机复量又抓到**第二层**：`start()` 的候选表只在浏览器循环**外面**算一次，换下一个浏览器时
  第一个候选还是刚删掉的那个临时路径，`_launch` 的 `mkdir` 把它**重新建出来**，而这时
  `temp_profile` 已经清成 `None` —— 那一份空壳没人管（真机上量到的空目录就是它）。
  另外 `fallback_profile_dirs()` 的第三个候选建在 `%TEMP%` 下
  （`xdao-export-browser-profile-<pid>`），从来没被记账，每次失败都新建一遍、也一样留着。
  最终改法：候选表挪进浏览器循环（每个浏览器各拿一个新临时目录）；`LoginBrowser` 记一张
  **只增不减**的清单 `self._temp_dirs`（`_fresh_candidate()` 给的名字、以及 `_launch()` 里建在
  系统临时目录下的候选都记上，判据是 `_is_temp_profile_dir()`：位置在系统临时目录 + 名字前缀
  `xdao-export-browser-profile`），`cleanup_temp_profile()` 挨个删、删到过东西才返回 True；
  `start()` 最后报错前再兜底收一次。真机探针 `_scratch/probe_failed_cleanup_dead.py` 复跑：
  3 个临时目录全删、`%TEMP%` 里剩 0 个。
  用例：`tests/test_browser_login.py` 的
  `test_failed_attempts_also_delete_the_temp_profile`（原来那版假 `fresh_profile_dir`
  只返回路径、不真建目录，所以 `not temp_profile.exists()` 是**真空断言** —— 改成会真
  mkdir 的替身）、`test_a_launch_error_that_never_retries_still_deletes_the_temp_profile`、
  `test_each_browser_gets_a_fresh_temp_profile_and_none_is_left_behind`（两个浏览器各拿一个
  新目录、都不留）、`test_a_fallback_profile_under_the_temp_dir_is_cleaned_up_too`
  （把 `tempfile.gettempdir` 指到临时根，验「%TEMP% 下的备用候选也要收、配置目录里的不动」）。

- **v0.13.9**：「用浏览器登录」改用**新建的临时资料目录**（`fresh_profile_dir()`，系统临时目录 + 进程内序号），不再复用配置目录里那个留到现在的旧目录 —— 现场是「自检里浏览器起得来、登录却一起来就退（Edge 退出码 21）」，两条路只差资料目录。关窗时 `LoginBrowser.stop()` 会 `cleanup_temp_profile()`（先 `_kill_processes_using_profile()` 收掉命令行里带该目录的浏览器进程，再 0.25 秒一次 `rmtree`、给 `TEMP_PROFILE_WAIT = 3.0` 秒，删掉立刻返回），删不掉也不抛异常。`_launch` 早退分支里的退出码 21 会追一句「未必是崩溃原因」。`gui.py`：`LoginDialog` 收下 `app=`，`SettingsDialog._open_browser_login` 把 `BrowserLoginDialog.failure` 写进运行日志（「浏览器登录没成：…」），用户贴日志就能带上真正的原因。用例：`tests/test_browser_login.py` 里候选顺序改成「临时目录 → 配置目录 → 备用目录」，新增 4 个临时目录用例，另加 `tests/test_gui_entry.py` 三条日志用例。

- **v0.13.8**：补丁版，把「浏览器到底起不起得来」并进默认自检。`--selftest --check-browser`
  时 `main.selftest(browser=True)` 会先打一行提示，再 `xdao/preflight.py` 新增的
  `browser_start_checks(explicit="", *, timeout=None, settings=None)` 真的启一遍候选浏览器
  （`xdao/browser_check.py` 新增 `check_all()`：和 `check_browsers()` 的差别只在「什么时候停」——
  挨个真试直到有一个起来，因为现场就是「默认那个起不来、另一个能用」；`check_browsers` 现在
  只是它的薄壳）。每个浏览器一条 `浏览器启动·<名字>`，最后补一条总结（全部起不来 →
  `fail`，并给「安全软件拦下 / 换设置里的浏览器 / 改用粘贴饼干」三条建议）。
  `preflight._loaded_settings()` 是为测试抽出来的（真读用户配置会被 `tests/conftest.py` 拦下）。
  **真机验收**：打包版在干净配置里跑 `--selftest --check-browser` 退出码 0，多出
  `[可以] 浏览器启动·Edge：Edge：能起来，Edg/… 调试端口 答得上话（0.5 秒）` 与
  `[可以] 浏览器启动：Edge 起得来…` 两行；单独 `--check-browser` 照旧。
  **踩坑**：①测试里钉 `preflight.find_browser` 对探测没用 —— `browser_check` 是
  `from .browser_login import find_browser`，得钉 `browser_check.find_browser`，否则会真的去启本机
  Edge；②`tests/test_preflight.py` 里直接调 `browser_start_checks()` 的用例在**整个套件**里过、
  单独跑会红（全套件里有 autouse fixture 把 `AppSettings.load` 换掉，单独跑没有）→ 该文件加了
  自己的 autouse fixture `_no_real_settings`。
  顺带修掉 `packaging/使用说明.txt` 里被 apply 脚本复制出来的正文（v0.13.6 段多了一份），
  以及 `_scratch/check_usage_txt.py` 的假绿灯（原来只数「本版版本号」，复制的那份不带标签）。
- **v0.13.7**：补丁版，修「登录窗口里登进去了、程序却没登录上」。旧逻辑 `BrowserLoginDialog._worker` 里 `if value: put(("ok", value))`：只要在浏览器里读到 `userhash` 就宣布成功 —— 而浏览器资料目录（`%APPDATA%\xdao-export\browser-profile`）是留着的，上一次登录的旧饼干还在，会话早失效，于是「界面说成功、导出全未登录」。`client.import_userhash()` 本身不做任何服务端校验（`xdao/client.py:426`）。真机探针定的判据：拿这块饼干请一次`https://www.nmbxd1.com/Member/User/Cookie/index.html`，匿名/假饼干（`userhash=deadbeefdeadbeef`）时 `_request_following_jumps` 的 `final_url` 都会落在`Member/User/Index/login.html`（`"login" in final_url`，饼干行数 0、页面 3282 字节）；试过用取串接口验，**验不了** —— `fetch_thread_page` 对匿名与假饼干都返回 200 且带 `Hide/Replies/ReplyCount` 字段（跳转页被当正常页解析的老坑）。落地：`xdao/gui.py` 新增模块级 `verify_userhash_live(client, userhash) -> str | None`（`None` = 能用；否则返回给用户看的一句话）与两个常量 `BROWSER_STALE_COOKIE_MESSAGE`／`BROWSER_VERIFY_FAILED_MESSAGE`；`_worker` 里 `verified` 缓存（验过的饼干不再反复问）＋`said_dead` 只提示一次；**函数自己 `client.import_userhash(userhash)`**（原先由调用方装，漏了就等于拿别人的身份问、结论会反过来）。用例 3 条（`tests/test_gui_browser_login.py`）：旧饼干不当成功（界面出现「X 岛不认」、窗口不关、`dialog.userhash is None`）、`test_verify_userhash_live_asks_the_cookie_page_with_that_cookie`（假客户端真跑：问了哪个地址、弹回登录页 == 不认、抛异常 == 「没法确认」而不是「不认」）、`test_the_dialog_asks_the_server_before_calling_it_a_success`（AST 钉接线：产品代码里有且只有一处调用、参数就是 `self.client, value`）。**踩过的坑**：①autouse fixture `assume_live_cookies` 在用例正文**之前**就把 `gui.verify_userhash_live` 换成替身了，所以「在用例里读模块属性、想拿到真实现」拿到的其实是替身 —— 真函数要 `from xdao.gui import verify_userhash_live as real_verify_userhash_live` 在模块顶层拿；②别指望「换掉类属性再包一层」绕 monkeypatch（`gui.BrowserLoginDialog.__dict__[...]` 里的东西本身可能已经是替身）；③`tests/test_config_isolation.py` 用 `import xdao.gui`，会另造一份 `xdao.gui` 模块对象（`gui is sys.modules['xdao.gui']` 为 False），跨模块打补丁时容易打空。

- **v0.13.6**：补丁版，把 v0.13.5 的探测能力搬到界面。`SelftestDialog` 新增 `browser_button`（文字「试浏览器」）与 `_run_browser_check()` / `_poll_browser_check()` / `_schedule_browser_poll()`：后台线程调 `browser_check.check_browsers(explicit or "", progress=...)`（`explicit` 取 `getattr(self.app, "settings", None)` 的 `pdf_browser`，取不到就留空自动挑），结果经 `self._browser_queue` 回到主线程轮询（`BROWSER_UI_POLL_MS = 150`）；跑的时候按钮禁用 + 右下角显示「正在试谁」，结论 `report.render()` 追加进自检文本（可一键复制），失败与「探测自己出错」都写进运行日志。`_schedule_browser_poll()` 把 `after()` 包在 `except tk.TclError` 里：探测途中关掉窗口不该把异常抛到主循环。对话框说明文字也补了一句「会真的启动一次浏览器」。用例（`tests/test_gui_entry.py`）2 条：`test_selftest_dialog_has_a_way_to_find_out_if_the_browser_even_starts`（真跑整条链路：假浏览器 + 假调试服务；用 `threading.Event` 当闸门卡住到「真结果已在队列里」，这样「跑着时按钮禁用」不是抢时间断言；替身**必须先存下真实现**再 monkeypatch，否则替身调 `browser_check.check_browsers` 会自己调自己 —— 第一次就写成那样，界面上显示 `RecursionError`）、`test_selftest_browser_check_says_so_when_the_probe_itself_blows_up`（探测抛异常也要说人话、按钮要放开）。注意：用例里**不能在 `_close(dialog)` 之后读 `button.instate()`**（`invalid command name`）。

- **CI 假浏览器用例不再靠跳过打发**（v0.13.5 之后，只动测试）：`tests/test_browser_check.py` 第一版用 `pytestmark + skipif` 在非 Windows 上整组跳过 5 条，撞上 `tests/conftest.py` 的跳过守卫（白名单外的跳过直接报错），CI 36836040741 / 36836049492 两个 ubuntu 作业因此红。改法：POSIX 分支写一个**带 shebang 的 Python 脚本**并 `chmod(0o755)`，剧本参数（模式、端口）放进文件名、脚本从 `sys.argv[0]` 里拆 —— 不再引一层 shell；Windows 仍用 `.cmd`（`write_bytes` 手写 `\r\n`）。`chmod` 写死 `0o755` 而不是 `st_mode | stat.S_IXUSR`：Windows 上 `stat.S_IXUSR` 是 0，那种写法在本机探测不出执行位到底给没给，到 ubuntu 上就是 `PermissionError`；也别在用例里断言执行位（Windows 的 stat 恒返回 0o666）。CI 36837069129 三作业全绿（ubuntu 两个矩阵 1084 passed / 88 skipped，windows 1166 passed / 6 skipped）。

- **子进程 Tk 抖动不再算成回归**（只动测试）：`tests/test_window.py` 新增 `_run_in_fresh_process(script)` —— 那段子进程脚本的失败会以「退出码非 0」报出来，与「Tk 起不来」长得一模一样，所以先探一次（`_TK_UNAVAILABLE_MARKERS = ("Can't find a usable", "no display name", "no $DISPLAY")`，最多重试 3 次），探不成按白名单理由「没有可用的显示环境（子进程里 Tk 起不来）」跳过。起因是 CI run 36834153393（windows-latest，sha a754d187，同 sha 的另一次运行全绿）：`test_widget_fonts_match_the_theme_after_startup` 因子进程报 `_tkinter.TclError: Can't find a usable init.tcl in the following directories: C:/hostedtoolcache/windows/Python/3.12.10/x64/tcl/tcl8.6/init.tcl` 而红。

- **CI 的父进程里 `tk.Tcl()` 也起不来**（v0.13.17 之后，只动测试）：CI run 36885850517（windows-latest / py3.12，sha a4ac75a）报 1 failed —— `test_widget_fonts_match_the_theme_after_startup` 的子进程那段全过了，最后一句 `tk.Tcl().splitlist(log_font)[0]` 却在**父进程**（pytest 自己）里炸：`_tkinter.TclError: Can't find a usable init.tcl in the following directories: C:/hostedtoolcache/windows/Python/3.12.10/x64/tcl/tcl8.6/init.tcl`（同一个 runner 上 `tk.Tk()` 是好的：子进程探针过、脚本也过，只有**纯 Tcl** 解释器建不出来）。改法：字体串改在**子进程**里用 `root.tk.splitlist(log)[0]` 解析（子进程的 `root` 是真的 Tk），父进程只比字符串 —— 与 `tests/test_window.py:242 _widget_font()` 同一个读法。同 sha 的标签 run 36885859950 全绿；本机 `tests/test_window.py` 36 passed。**另一个坑**：本机带 `XDAO_HEADLESS=1` 跑全量时，`test_selftest_intro_wraps_at_the_minimum_size` 与 `test_selftest_minimum_size_fits_the_folded_buttons` 会假红（窗口压根没被映射：`dialog.winfo_height()` 返回 1、`winfo_rooty()` 返回 0，于是「被窗口下沿切掉」的断言必红）—— 结论要拿真机（不设 `XDAO_HEADLESS`）那一轮来说，无头那轮只用来快速回归。

- **v0.13.5**：补丁版，补上「浏览器到底能不能起来」这个盲区。新增 `xdao/browser_check.py`：`BrowserCheck`/`BrowserReport` 两个 dataclass、`check_one(info, timeout, *, root=None) -> BrowserCheck`（真的启一次、等调试端口、再问一句 `/json/version`，然后立刻关掉并删临时资料目录）、`check_browsers(explicit="", *, timeout, limit=3, env=None, progress=None) -> BrowserReport`（挑选顺序 = `find_browser(explicit or None)` → `browser_candidates(head)`，有一个能用就停）；`main.py` 新增 `--check-browser` / `--check-browser-json`（登记进 `_CONSOLE_HINT_FLAGS`）。`xdao/browser_login.py` 的 `LoginBrowser` 新增 `fallback_profiles: bool = True`，`start()` 里`browsers = browser_candidates(self.info) if self.fallback_profiles else [self.info]`、`candidates` 同理 —— 登录那条路照旧兜底，探测这条路必须如实回答「这一个行不行」（实测：开着兜底时会退到用户真正的 `browser-profile`，那里的旧端口让另一个浏览器答了话，把一个起不来的假货判成「可以」）。用例 `tests/test_browser_check.py` 5 条：真答话=通过、写完端口就死=不通过、端口在没人答话=不通过、探测不换目录/不换浏览器、登录那条路兜底仍在。

- **v0.13.4**：补丁版。`gui.py` 新增模块级 `_find_login_browser(settings=None)`：`explicit = (settings or AppSettings.load()).pdf_browser`，交给 `backend.find_browser(explicit or None)`；`BrowserLoginDialog._worker()` 改用它（原来是 `backend.find_browser()`）—— 设置里手动指定的浏览器以前只对 PDF 导出生效，浏览器登录还在自己挑。对话框新增一行 `browser_note_var`（`_set_browser_note()`写、`_poll` 新增 `"browser"` 消息分支），启动后显示「这次用 <名字> 打开（<路径>）。」；「PDF 浏览器」那栏的说明改成「导出 PDF 和「用浏览器登录」时调用的浏览器…留空表示跟随系统默认浏览器，认不出来再找 Chrome 或 Edge」。用例：`tests/test_gui_browser_login.py` 新增`test_browser_note_tells_the_user_which_browser_will_open` 与`test_explicit_browser_in_the_settings_is_the_one_that_gets_used`（`open_dialog` 夹具新增 `set_explicit_browser()`）。

- **v0.13.3**：补丁版。`browser_login.py` 新增「认系统默认浏览器」：`_windows_default_exe()` 读 `HKCU\...\UrlAssociations\https\UserChoice` 的 ProgId、再按 `SOFTWARE\Classes\<ProgId>\shell\open\command`（先 HKCU 后 HKLM）取命令行，`ordered_browsers()` 把默认那个排最前、`find_browser()` 改走它（手动指定路径仍优先）；注册表读取走 `_registry()`，测试可替换，Linux CI 也能用假注册表跑这段。`LoginBrowser.start()` 重写成「遍历浏览器 × 遍历备用资料目录」：`_dead_on_startup()` 认下 `_DEAD_ON_STARTUP_MARKER = "刚起来就退出了"` 后换目录、必要时换浏览器（`browser_note` 供界面提示），全试完才抛错并附「直接粘贴饼干登录」；顺手删掉 `find_browser`/`_pick_page`/`_pick_site_page` 三处「同名函数定义两次」的历史残留。

- **v0.13.2**：补丁版。浏览器登录读调试接口改成**带预算的重试**（`cdp._http_json`，8 秒 / 0.1→0.5 秒退避）：真机实测端口文件出现后还要 328~563 ms 第一次连接才成功，这中间读一次就是 `[WinError 10061] 目标计算机积极拒绝`，以前只读一次就报「打开浏览器失败」。同时 `CDPSession` 新增 `failure_hint`，`gui.py` 把 `LoginBrowser.devtools_failure_hint` 传下去（进程已退出就当场报退出码，不再耗满预算）；`browser_login.start()` 把「调试口连不上」也归入「换备用资料目录重试」。

- **v0.13.1**：补丁版。①升级换完文件后不再弹「升级中」模态框（它会一直等用户点确定，而帮手只等 60 秒，点慢一拍升级就白换）；②`browser_login._profile_failure` 除 Windows 的 5/32 之外也认 POSIX 的 `EACCES`/`EBUSY`（CI 从两条 ubuntu 矩阵变绿）。

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
    2026-10-02 又补上三处：以前只看「文件在不在、是不是非空」，浏览器被拦下后写的错误页也算成功
    （现在验 `%PDF` 文件头），而且不管成没成都返回 0（现在没生成 PDF 就返回 1）；六种启动方式
    逐条记结果，结论里写明是哪几种成的；「经 cmd 启动」那项换成「分离进程」（`DETACHED_PROCESS`），
    因为旧写法被 cmd 的引号规则拆坏、从来没成过 —— 实测记录见上面第 8 条。
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

**只支持 Chromium 内核（Firefox 走不了这条路）**：整条路靠 CDP —— Chrome 与 Edge 都是
Chromium，协议一致，所以一套实现同时支持两者；Firefox 用的是另一套（Marionette / WebDriver
BiDi），本项目没有实现、也不打算实现。界面与 `packaging/使用说明.txt` 从 v0.13.19 起把这件事
写在明处（登录窗开头两行 + 支持浏览器那一节），免得装了 Firefox 的用户反复试。
`xdao/browser_login.py:253-261` 的 `choose_browser()` 认不出 Firefox 时会照旧往下找
Edge / Chrome，所以「系统默认浏览器是 Firefox」的机器仍然能用这条路 —— 也就是说这里
不需要任何功能改动，只用把话说明白。

**导航次数只有「真动过标签页」才扣（v0.13.20）**：轮询循环里的 `BROWSER_LEAF_NAV_LIMIT`
管的是「还能把用户眼前那个标签页拖去饼干页几次」。0.13.18 及以前是**先扣再用**，而用户还在
登录页打字时 `fetch_leaf_cookie(navigate=True)` 什么都不碰就返回（「还没登录」）—— 两次机会
在开头十几秒里用光，之后只剩 `navigate=False` 的重读，userhash 永远不会出现。现在的规矩：
`navigated` 为真才算动过、才扣；`tests/test_gui_browser_login.py` 里的
`test_login_finished_late_still_gets_a_chance_to_apply_the_cookie` 钉住「在登录页上耗几轮 →
登进去还能领到」。另外超时那句诊断改成 `leaf_hints[-3:]` 的有序列表：只留最后一条时，
收尾的「还是没有 userhash」会把「还没登录」盖掉，真机截图上看不出卡在哪。

**等站点自己跳完再读饼干罐（v0.13.21）**：站点的「应用饼干」是**跳转式**的 —— 点下去先落到
它自己的「跳转提示」页（`…/Cookie/switchTo/id/<id>.html`，正文写着「饼干切换成功!」与
「页面自动 跳转 等待时间： 1」），**userhash 是在那一跳落到真正页面时才被主站种下的**。
`_follow_jumps()` 原来是「读一眼页面状态，有 `a#href` / meta refresh 就跟」：倒计时页上什么
都没有，于是它一跳都不跳，调用方紧接着只读一眼饼干罐（还没有），再导航去 `export/id/<id>.html`
兜底 —— 这一下把标签页从倒计时页上拽走，站点的第二跳永远没发生；那次 export 没有 token 会被
弹回登录页，下一次轮询读到的页面状态就是登录页，老代码在「还没登录」上早退，界面于是每 5 秒
重复「这个窗口里还没登录（页面停在登录页）」。现在：① `_FIND_APPLY_JS` 多报一个 `countdown`
（正文里有「等待时间 / 自动跳转 / 跳转提示」），`kind` 也算 `jump`，倒计时页不再被当成
「没有跳转」；② `_follow_jumps()` 在 `JUMP_WAIT_SECONDS = 8.0` 预算内每 `NAVIGATE_POLL`
重读页面状态，等到 `jump` 就跟着跳、等到不再是跳转页就收手（正在跳的时候 `evaluate` 失败很
正常，接着等）；③ 应用之后走新的 `_wait_for_cookie()`（`COOKIE_WAIT_SECONDS = 4.0` /
`COOKIE_WAIT_POLL = 0.5`），不再只读一眼；④ `fetch_leaf_cookie(waiting_for_login=…)`：界面在
已经真应用过（`leaf_attempts > 0`）之后不再把「停在登录页」当成「用户正在打字」，仍然继续领
饼干；⑤ `BROWSER_LEAF_NAV_LIMIT` 2 → 6，用满时把 `BROWSER_LEAF_CAP_HINT` 推到常驻提示
（旧写法只是静默退化成只读）。`tests/test_browser_login.py` 里的 `_SiteHoppingSession` /
`_LateJarSession` 分别钉住「倒计时页跳走之后才种饼干」和「饼干罐要等几次才出现」，
`tests/test_gui_browser_login.py` 的
`test_leaf_cookie_keeps_trying_after_the_tab_is_bounced_back_to_login` 钉住「被弹回登录页之后
还会再去饼干页」（老代码停在 1 次）。

**浏览器只管验证码，领饼干走 HTTP（v0.13.22 起，v0.13.23 收口）**：`XdaoClient.apply_cookie()` 这条 HTTP 协议
从 v0.6.1 起就在线上跑通了 —— 它会认「跳转提示」页（HTTP 200 也可能是跳转页）、跟着跳、从
**导出页的响应体**里抠 userhash。浏览器那条路存在的唯一理由就是验证码（真人认一次），其余步骤
在浏览器里做只是把一个已经解决的问题重新做一遍，还多出两个新的不确定性：站点那张跳转页是
**页面自己的 JS 倒计时**（`setInterval` 到点 `location.href = href`），而成功页的 `a#href` 指回
自己时会变成原地重载（用户看到的就是「一直跳转」）。所以现在的分工是：浏览器窗口负责把验证码
认过去，程序读一次它的整罐饼干（`read_site_cookies()`），剩下的交回 HTTP
（`apply_leaf_cookie_over_http()`）—— 也因此**不需要**再驱动用户的标签页。v0.13.23 把这条
尾巴收干净了：界面层固定 `navigate=False`，而「最多导航几次」那套机制
（`BROWSER_LEAF_NAV_LIMIT` / `BROWSER_LEAF_RETRY_SECONDS` / `BROWSER_LEAF_CAP_HINT`）
连同「导航次数」这个概念一起删掉 —— 一次都不动用户的标签页，就不该留着一个会让人
误以为程序还在导航的状态。拿不到饼干时，界面会把「窗口里有哪些饼干（只写名字）」和
HTTP 那条路的原话拼进失败原因（`_jar_summary()` / `_diagnosis()`），它会跟着
「浏览器登录没成：…」进运行日志。

## 逐级回退的构造函数：只钉「传了什么」看不出来（2026-10-02 发现）

`create_exporter()` 用「带全部参数 → `TypeError` 就删一项再试」来兼容各个导出器的构造签名。
这条链有个安静的失效模式：某一层**本该带上的参数**忘了带，程序不会报错 —— 它只是把那一层的
参数漏给导出器，而导出器通常都有默认值，于是行为悄悄回退，谁也不会发现。

这次是 PDF：`PdfBuilder` 不收 `image_mode`，所以第一层必 `TypeError`、PDF 一定在第二层建起来，
而第二层原来没带 `browser_path` / `pdf_timeout` / `fallback_html` —— 真机表现是「设置里指定了
浏览器，PDF 导出还是用默认那个」。原来那条用例只断言「`create_exporter()` 收到的参数」，
所以一直是绿的。

规矩：给这条链加参数时，用例要钉**导出器实例真正收到什么**（属性值），不是「谁传了什么」；
每加一项就补一条断言。另外，测试里遇到「真起浏览器」的用例（例如监控 PDF 那条）一律换成
假渲染 —— 它在 CI 的 windows 机器上会因为等不到调试端口而红，一条用例就能把整轮 CI 拖红。
（注入验证时也要注意：这条链里「一层的样子」和「另一层的样子」很容易长得像，片段不唯一就会
锚错层 —— 删掉第一层的参数，用例照样全绿，等于什么都没验。片段必须锚到唯一位置。）

## 敏感串扫描：提交信息扫了，文档容易漏（2026-10-01 发现）

发版前有一道「扫敏感串」的手工步骤（本机用户名、开发机路径、聊天里的说法、
内部编号…），v0.13.7 才发现**它只管提交信息，不管文档**：`MAINTENANCE.md`
那一条里留着内部编号，一路推上去了。提交信息里同一个词被扫出来改了写法，
文档里却没人看。

规矩：跑扫描时把**这一轮要提交的所有文本文件**一起扫（`git diff --name-only` 那份清单里
的 `.md` / `.txt` 都算），别只扫 `_commit_msg_*.txt`。内部编号、聊天里的说法、本机路径
都不该出现在公开仓库里；已经推上去的只能下个提交改回来（历史改不了，除非强推）。

## 一键升级的探针：见证进程活不到写结论（2026-10-01 发现）

`_scratch/probe_upgrade_e2e_v1xx.py` 靠「见证进程」记换完之后的账（换上去的字节、备份、
暂存目录清没清）。它自己 `os._exit(0)` 让位给帮手 —— **Windows 会把父进程的整个作业树
一起收掉**，见证进程常常来不及写 `verify.log`（v0.13.7 那轮只写到「现场目录里 exe 在:True」）。
下次要用见证进程，得让它**脱离作业对象**（`creationflags` 加 `DETACHED_PROCESS` 或
`CREATE_BREAKAWAY_FROM_JOB`），或者干脆别靠它：直接看文件也能核账。

补验脚本 `_scratch/check_upgrade_result_v137.py` 就是「不靠见证进程」的版本：
比现场 exe 与包里那份的 sha256、跑一次现场 exe `--version`、看备份目录、
再把暂存目录的 mtime 拨回一天调 `updater.cleanup_staging_leftovers()`。
注意两条容易误判的：探针会把现场 `xdao-export.exe` 换成「起来就退」的替身（所以备份里
不是老版本 exe 是**对的**）；普通暂存目录 `xdao-export-update` 要放够
`STAGING_STALE_SECONDS = 600` 秒才清（所以刚升完几分钟还在也是**对的**）。

2026-10-01 v0.13.10 那轮又补了见证脚本自身的两处：①模板里只 import 了 `hashlib` / `sys` / `time` / `pathlib`，**漏了 `subprocess`**，于是它跑 `--version` 三项全报 `NameError`，只有「换上去的字节与包一致」那一条还有效；②模板里「盯 TEMP 等暂存目录消失」只等 80 秒，跟上面 `STAGING_STALE_SECONDS = 600` 自相矛盾，末行必打「!! 80 秒都没等到清空」。正确的等法是等 `.leftover-<帮手号>-<时间>` 记号目录（启动时立刻清），普通暂存目录按年龄判断。

## 自检类用例要连报告对象一起钉（2026-10-01 发现）

`--selftest` 会跑「本机环境」那几项（配置目录、导出目录、缓存目录能不能写），
**结论随机器而变**。新加的 `tests/test_selftest_browser_flag.py` 一开始只钉了
`preflight._loaded_settings`、`can_write_dir`、`_check_cache_dir` 这些小函数，
结果开发机上全绿、两个 ubuntu 作业在 49df732 红了三条（`assert code == 0` →
`assert 1 == 0`）。

原因：`run_local_checks()` 在**自己的函数体里**再取一次 `_loaded_settings()` ——
开发机上设置里的导出目录恰好能写，干净的 CI 机器上写不进去 → 多出一条 `fail` →
退出码 1。**钉里面的小函数不够，要把 `run_local_checks` /
`run_network_checks` 整个换成固定报告**（`tests/test_cli.py` 的
`_stub_preflight` 早就是这么做的，新的自检类用例照抄它）。

教训：**凡是「跑整个自检再看退出码」的用例，都得先假定自己会读到这台机器的真实
设置**；本机全绿说明不了 CI 全绿，反过来也一样。写完这类用例，先问一句「换一台
干净的机器，这几项会是什么结论」。

## CI 说明

- 工作流在 `push`、`pull_request` 与手动触发时运行，**不需要任何凭据**
  （用例全部离线，用测试替身替代网络）。
- 三个矩阵：Ubuntu + Python 3.10（声明的最低版本）、Ubuntu + 3.12、Windows + 3.12。
- 检查项：语法编译、单元测试、CLI 可用性、格式注册表完整性；
  Windows 上额外跑一次 `--selftest`（联网失败不阻断）。
- 界面相关的用例（`test_theme.py` / `test_window.py` / `test_gui_browser_login.py`）
  在没有显示环境的机器上会自动 skip，Linux CI 上属于预期行为，不算失败。
- **留意**：`compileall` 即使编译失败也返回 0，工作流里已显式 grep 报错，
  改这一步时别退化成无效检查。
- 打 tag 时会校验 `xdao.__version__` 与 tag 相同，避免发错版本号。

### 五个已经踩过的 CI 坑

1. **测试夹具不能写死 Windows 形态**。`tests/test_pdf.py` 里造「假浏览器」时，
   原先只生成 `fake_browser.cmd`，结果 ubuntu 两个 job 全部挂在单元测试：
   `PermissionError: [Errno 13] Permission denied: .../fake_browser.cmd`。
   现在按平台生成 .cmd 或带执行位的 sh 脚本，并加了回归用例
   `test_fake_browser_is_actually_executable` 保证夹具本身真能被执行。
   **本机预演**：`python tools/posix_check.py`（借 Git 自带的 sh.exe 跑 POSIX 分支，
   没有 Linux 也能提前发现这类问题）。2026-10-02 起它会逐条判定、不再只看「跑没跑完」：
   ① 包装脚本有没有 shebang（Linux 就是靠它认解释器）；② sh 认不认为它可执行
   —— **Windows 上 `os.access(browser, os.X_OK)` 对任何存在的文件都是 True，等于没查**，
   所以改成 `sh -c 'test -x "$1"'` 去问 sh（真正的执行位仍由 CI 的 ubuntu job 覆盖）；
   ③ 返回码；④ `flags.txt` 有没有写出来；⑤ **参数有没有原样透传**（以前只看文件在不在，
   `$@` 被引号吃掉、路径被改写都发现不了）；⑥ 导出的 PDF 是不是 `%PDF` 开头。
   任何一项不过就打印 `不通过 —— N 项没过` 并返回 1。由 `tests/test_posix_check.py` 29 条兜着
   （五处都注入验过：退回旧写法，用例立刻红）。
2. **取 Actions 日志用 `tools/ci_logs.py`**，不要用 `gh run view --log`：
   日志真实地址在 `results-receiver.actions.githubusercontent.com`，
   带签名的临时 URL 在本机网络下经常被中途掐断（`unexpected EOF`）。
   该脚本自己跟随重定向、去掉 `Authorization` 头（否则云存储回 401）并分段重试。
   2026-10-02 起它还会说话：`--job` 拼错（一个 job 都没匹配上）或 `--grep` 一行都没匹配到，
   都会明确报出来并**返回 1** —— 「没搜到」不等于「CI 没问题」；job 记号的 ✗ 也不只代表
   `failure`，`cancelled` / `timed_out` 同样算没成功（细节见第 8 条）。
3. **本机 3.12 跑绿不代表 CI 绿**：三个矩阵里有一个是 `Ubuntu + Python 3.10`（声明的最低
   版本），3.10 缺的东西在本机根本不会露头。真实案例（2026-10-02，提交 1c1147c）：
   `tests/test_sync_from_api.py` 里拿 `datetime.fromisoformat()` 去解析 git `%aI` 给出的
   时间 —— git 对 `+0000` 的提交写的是 `2026-09-05T21:14:23Z`（**是 `Z`，不是 `+00:00`**），
   而结尾这个 `Z` 只有 **Python 3.11 起**的 `fromisoformat` 认，3.10 直接
   `ValueError: Invalid isoformat string: '2026-09-05T21:14:23Z'`。当时本机 3.12 全绿、
   windows 与 ubuntu 的 py3.12 两个作业也绿，只有 py3.10 那条红 —— 一条红就是整轮红。
   改法：先把 `Z` 换成 `+00:00` 再解析（产品代码 `sync_from_api.git_ident` 本来就是这么
   写的，栽的是测试自己写的辅助函数）。**别只看本机**：碰日期时间、`tomllib`、`StrEnum`
   这类东西之前，先想一下 3.10 有没有。
4. **Windows 形状的字符串别在 ubuntu 上按字面断言**（2026-10-02，提交 a72e26c）：
   `tests/test_pdf_diag.py` 断言最小 PATH 的第二段「以 `windows\system32` 结尾」，
   而套件在 ubuntu 上跑时 `os.path.join(r"C:\Windows", "system32")` 拼出来的是
   `C:\Windows/system32`（正斜杠）→ ubuntu 两条 job `1 failed, 1380 passed, 106 skipped`，
   windows 那条全绿。改法：**期望值也用 `os.path.join` 现拼**（这个 PATH 只对 Windows
   有意义，但用例会在 Linux 上收集并执行）。注意 `tools/posix_check.py` 只预演浏览器
   包装脚本那条路，**这类坑本机预演不出来**。
5. **走平台分支的用例要自己把平台钉住**（2026-10-02，提交 ce12f2f）：
   `tools/gui_probe.py` 里 `IS_WINDOWS = sys.platform == "win32"` 决定收尾是
   `taskkill /T /F /PID` 还是 `proc.kill()`，而 `tests/test_gui_probe.py` 有两条用例直接
   断言「调了 taskkill」却没钉住平台 —— 本机 Windows 全绿，ubuntu 两条 job
   `2 failed, 1458 passed, 106 skipped`。改法：加一个
   `use_windows(monkeypatch, windows=True)` 帮手把 `gui_probe.IS_WINDOWS` 顶掉，走
   Windows 分支的用例显式钉 True、POSIX 那条显式钉 False。
   **本机预演**：把 `tools/gui_probe.py` 里的 `IS_WINDOWS` 临时改成 `False` 再跑这个测试文件
   （记得跑完按字节还原并核对 sha256）—— 假装 Linux 也是 41 条全绿。记住判据：**用例跑在哪种
   机器上，不该由运行环境决定它验哪条分支**。

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
