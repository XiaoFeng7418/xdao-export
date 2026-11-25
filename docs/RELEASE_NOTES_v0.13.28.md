# v0.13.28：下载下来的包，能自己核对

## 该下哪个附件

下载 **免安装包** `xdao-export-v0.13.28-win64.zip`（解压后双击 `xdao-export.exe`，不需要装
Python）。同一页还挂着一份 `xdao-export-v0.13.28-win64.zip.sha256` —— 那是给上面这个包核对
用的校验文件，不是程序，不用解压。

## 这一版做了什么

每个 Release 除了 zip，再多挂一份 `xdao-export-v0.13.28-win64.zip.sha256`。
里面只有一行：`<SHA256 摘要>  <文件名>`。下载后放同一个文件夹，在 PowerShell 里：

```powershell
Get-FileHash .\xdao-export-v0.13.28-win64.zip -Algorithm SHA256
Get-Content .\xdao-export-v0.13.28-win64.zip.sha256
```

两行的字符串一样就是原件。想省事：

```powershell
$f = 'xdao-export-v0.13.28-win64.zip'
(Get-FileHash $f -Algorithm SHA256).Hash -eq ((Get-Content "$f.sha256") -split '\s+')[0]
```

回 `True` 就是原件。

## 发版流程也跟着改了两处

- `tools/make_release.py` 上传前先自己算一遍摘要：校验文件写的摘要和 zip 对不上、或者 .sha256
  里指的不是这个 zip，就直接拒绝建 Release。理由很简单：**挂一份不配套的校验文件，比不挂更坏**
  —— 用户照着核会以为包被换过。
- `tools/repo_check.py` 的体检从 14 项变成 15 项：不下载 zip，直接拿 GitHub 自己给附件算的
  `digest` 和 .sha256 里那一行比。没挂校验文件只算「需要处理」（退出码仍为 0），摘要不符才算红。

## 界面、导出格式、配置文件、命令行开关

和 0.13.27 一模一样。

## 完整性校验与签名状态

`.sha256` 用于核对下载文件与发布页提供的摘要是否一致；校验文件与压缩包位于同一 Release，不能防止两者同时被替换。当前发布包未进行代码签名。仓库已预留 GitHub Actions + SignPath 接入流程，但尚未启用；未来满足条件后再按流程接入。
