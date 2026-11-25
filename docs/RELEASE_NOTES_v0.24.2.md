# v0.24.2：修复首页排版与完善发行校验

本版修复说明文档的排版与发行信息，继续支持 X 岛（nmbxd1.com）与 B 岛（bog.ac）公开串。

- 修复中文首页的重复空行，恢复连续的状态徽章与 Markdown 表格，段落间保留一行空白。
- 统一中英文首页、程序版本号、随包使用说明与附件示例中的 v0.24.2 版本信息。
- 增加 Markdown 排版与公开材料回归检查，补充发行包及配套校验文件的用途说明。

本版不增加导出格式、命令行开关或运行时依赖。

## 附件

- `xdao-export-v0.24.2-win64.zip` 是 64 位 Windows 免安装包。解压后双击 `xdao-export.exe`，无需安装 Python；请保持 exe 与 `_internal` 文件夹位于同一目录。
- `xdao-export-v0.24.2-win64.zip.sha256` 是该 zip 的 SHA256 校验文件，用于核对下载后的文件字节是否完整。

## 下载与校验

从官方 Releases 页面下载上述两个文件，放在同一目录，在 PowerShell 中执行：

```powershell
Get-FileHash .\xdao-export-v0.24.2-win64.zip -Algorithm SHA256
Get-Content .\xdao-export-v0.24.2-win64.zip.sha256
```

核对两处 SHA256 值一致后再解压。当前发布包未进行代码签名；校验值用于文件完整性检查，仍需确认下载来源。
