# v0.13.28：下载下来的包，能自己核对

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

## 关于「防替换」的实话

`.sha256` 能证明「你下载到的字节就是 Release 里那份」，但它和 zip 放在同一个 Release 里 ——
账号被盗时两者会一起被换，它挡不住那种情况。真要往系统层面走，只有代码签名能让 Windows
不再显示「未知发布者」，而免费的签名（SignPath Foundation，面向开源）证书主体是
SignPath Foundation 而不是个人网名。签名这一半在申请流程里，等有结果再单独说。
