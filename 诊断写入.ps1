# 写入诊断：帮你找出「为什么程序写不进去这个目录」
#
# 用法：右键这个文件 → 「使用 PowerShell 运行」；或者在 PowerShell 里执行：
#     powershell -ExecutionPolicy Bypass -File "D:\小玩意\诊断写入.ps1"
#
# 它只做三件事：在几个目录里试建一个临时文件、读一下目录权限、把结果打印出来。
# 不会改任何设置、不会删任何东西（试建的文件立刻删掉）。

$ErrorActionPreference = 'Continue'

function Write-Head($text) {
    Write-Host ''
    Write-Host "== $text ==" -ForegroundColor Cyan
}

function Test-Dir([string]$dir) {
    if (-not (Test-Path -LiteralPath $dir)) {
        Write-Host ("  ✗ {0}  —— 目录不存在" -f $dir) -ForegroundColor DarkYellow
        return
    }
    $probe = Join-Path $dir ("xdao-write-test-{0}.tmp" -f (Get-Random))
    try {
        Set-Content -LiteralPath $probe -Value 'ok' -Encoding UTF8 -ErrorAction Stop
        Remove-Item -LiteralPath $probe -Force -ErrorAction SilentlyContinue
        Write-Host ("  ✓ {0}  —— 能写" -f $dir) -ForegroundColor Green
    } catch {
        Write-Host ("  ✗ {0}  —— 写不进去：{1}" -f $dir, $_.Exception.Message) -ForegroundColor Red
    }
}

Write-Head '当前账户'
Write-Host ("  用户     : {0}" -f $env:USERNAME)
Write-Host ("  是否管理员: {0}" -f ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator))
Write-Host ("  系统     : {0}" -f (Get-CimInstance Win32_OperatingSystem).Caption)

Write-Head '磁盘'
Get-CimInstance Win32_LogicalDisk | ForEach-Object {
    $fs = $_.FileSystem
    $note = ''
    if ($_.DriveType -eq 2) { $note = '（可移动磁盘：可能有写保护开关）' }
    if ($_.DriveType -eq 5) { $note = '（光驱，只读）' }
    Write-Host ("  {0}  {1,-6} 剩余 {2:N1} GB  {3}" -f $_.DeviceID, $fs, ($_.FreeSpace / 1GB), $note)
}

Write-Head '写入测试（每个目录试建一个临时文件再删掉）'
$targets = @(
    'D:\',
    'D:\X岛',
    'D:\X岛\.cache',
    (Join-Path ([Environment]::GetFolderPath('MyDocuments')) 'xdao-写入测试'),
    (Join-Path $env:LOCALAPPDATA 'xdao-export')
)
foreach ($dir in $targets) { Test-Dir $dir }

Write-Head 'D:\X岛 的权限（谁被允许写）'
if (Test-Path -LiteralPath 'D:\X岛') {
    $acl = Get-Acl -LiteralPath 'D:\X岛'
    Write-Host ("  所有者: {0}" -f $acl.Owner)
    $acl.Access | ForEach-Object {
        Write-Host ("  {0,-42} {1,-18} {2}" -f $_.IdentityReference, $_.AccessControlType, $_.FileSystemRights)
    }
    Write-Host ''
    Write-Host '  提示：上面「允许」行里应该有你（或者 Users / Everyone）带 Write 或 FullControl。'
    Write-Host '  如果只有 SYSTEM / Administrators，那就是这个目录不允许普通账户写。'
} else {
    Write-Host '  目录不存在'
}

Write-Head '勒索软件防护 / 受控文件夹访问'
try {
    $mp = Get-MpPreference -ErrorAction Stop
    Write-Host ("  受控文件夹访问: {0}（1 = 已开启，0 = 关闭）" -f $mp.EnableControlledFolderAccess)
    if ($mp.ControlledFolderAccessProtectedFolders) {
        Write-Host '  受保护的文件夹:'
        $mp.ControlledFolderAccessProtectedFolders | ForEach-Object { Write-Host ("    {0}" -f $_) }
    } else {
        Write-Host '  受保护的文件夹: （没有）'
    }
} catch {
    Write-Host ("  读不到 Defender 设置：{0}" -f $_.Exception.Message)
}

Write-Head '结论怎么看'
Write-Host '  · 只有 D:\X岛 那几行是 ✗、文档目录是 ✓  → 换个导出目录就能用，或按上面的权限表给当前账户加权限'
Write-Host '  · D:\ 和 D:\X岛 都是 ✗、文档目录是 ✓    → 整个 D 盘都写不进去（写保护 / 只读挂载 / 安全软件），改用 C 盘目录'
Write-Host '  · 所有行都是 ✗                        → 系统级限制，多半是安全软件或受控文件夹访问'
Write-Host ''
Write-Host '把这个窗口的内容截图发给维护者即可。'
Write-Host ''
Read-Host '按回车关闭'
