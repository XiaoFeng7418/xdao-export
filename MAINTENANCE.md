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

   13 项检查，全部 ✓ 才算健康。它会检查：仓库设置、提交同步、文件逐一致、
   版本号一致、Release 附件齐全、待办积压。

2. **跑测试**（当前基线 191 项，必须全绿）

   ```powershell
   & $py -X utf8 -m pytest -q
   ```

3. **处理告警**，对照表：

   | 告警 | 处理方式 |
   |---|---|
   | 本地有提交未推送 | `& $py -X utf8 tools/push_via_api.py --repo XiaoFeng7418/xdao-export --branch master` |
   | 版本号与 Release 不一致 | 升 `xdao/__init__.py` 的版本号 → 打包 → 发新 Release |
   | 附件名含非 ASCII | 用 `make_release.py` 改名或重传（中文名会被 GitHub 截断成单字符） |
   | 开放 issue / PR | 阅读、回复；是 bug 就修并补单元测试 |

4. **看用户反馈**：Release 下载次数、issue、Star。

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

# 5) 提交并推送
& git add -A; & git commit -m "..."
& $py -X utf8 tools/push_via_api.py --repo XiaoFeng7418/xdao-export --branch master

# 6) 发布
& $py -X utf8 tools/make_release.py --repo XiaoFeng7418/xdao-export --tag vX.Y.Z `
    --name "vX.Y.Z：..." --notes-file docs/RELEASE_NOTES_vX.Y.Z.md `
    --asset 'D:\小玩意\xdao-export-vX.Y.Z-win64.zip' `
    --asset 'dist\xdao-export-vX.Y.Z.exe'

# 7) 收尾：确认体检全绿
& $py -X utf8 tools/repo_check.py --repo XiaoFeng7418/xdao-export
```

## 巡检发现的真实情况（2026-09-30 首次周检）

- 远端曾因早期推送脚本每次重建整套提交，积累了若干**重复提交**。
  已在 2026-09-30 清理：`master` 与 `v0.2.1` 均指向 `87179e27`，
  其父提交为 `6b02daae`，重复的那一条已不在链上。此后推送脚本会从远端
  已有历史**续接**，不会再产生重复。
- 重复提交会干扰「哪些已经推过」的判断（`find_pushed_prefix` 按提交说明 +
  作者时间对齐）。若将来又出现误报，用 `tools/repo_check.py` 看「文件」一项：
  文件逐字节一致就说明内容没问题，只是计数受重复提交影响。

## 硬性约定

1. **Release 附件名一律用 ASCII**（`xdao-export-vX.Y.Z-win64.zip` / `.exe`）。
   走 `?name=` 上传时 GitHub 会把中文名截断成单个字符。
2. **打包好的 exe 不进源码树**（`.gitignore` 已忽略根目录 `*.exe` 与 `dist/`），
   只作为 Release 附件。
3. **API 创建的提交会被规范化时区**（传 `+08:00`、存成 UTC），所以同一提交在本地
   与远端可能算出不同 sha —— 内容一致即视为正常，不要据此判定推送失败。
4. **每次发布都要能跑**：`--selftest` 退出码 0，最好再做一次真实串导出。
5. **改动必须带测试**：`tests/` 是 191 项离线用例，新增功能请补用例，
   不要依赖联网测试。
6. 本机 git 的 HTTPS 传输不可用（schannel / openssl 都被拦），
   一切远端操作走 `tools/` 下的 API 脚本。

## 路线图（尚未实现）

- 导出 PDF（本机有 Edge / Chrome，可用无头模式把内嵌图片的 HTML 转 PDF）
- 监控到更新时的桌面通知
- 无人值守登录 / 验证码识别
- 单页失败时的自动补抓与重试队列
- 监控列表的导入 / 导出
- GitHub Actions 自动跑 pytest（需先确认 runner 能装依赖）
