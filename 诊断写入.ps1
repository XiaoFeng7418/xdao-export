# 写入诊断：帮你找出「为什么程序写不进去这个目录」
#
# 用法（二选一）：
#   1. 双击同目录下的「诊断写入-双击运行.cmd」
#   2. 在 PowerShell 里执行：
#        powershell -ExecutionPolicy Bypass -File "诊断写入.ps1"
#
# 它只做三件事：在几个目录里试建一个临时文件、读一下目录权限、把结果打印出来。
# 不会改任何设置、不会删任何东西（试建的文件立刻删掉）。
#
# v0.5.3 起：结果同时写进同目录/桌面上的 诊断结果-日期.txt。
# 因为很多用户的系统把 PowerShell 窗口设成"结束就关"，打印出来的内容根本来不及看。

$ErrorActionPreference = 'Continue'

# ---------- 先把输出抄一份到文件：窗口关了也还有据可查 ----------
$script:logLines = New-Object System.Collections.Generic.List[string]
$script:logPath = $null

function Add-Log([string]$text) {
    $script:logLines.Add($text)
}

function Write-Line([string]$text, [string]$color = 'Gray') {
    Add-Log $text
    if ($color -eq 'Gray') { Write-Host $text } else { Write-Host $text -ForegroundColor $color }
}

function Write-Head($text) {
    Write-Line ''
    Write-Line ("== {0} ==" -f $text) 'Cyan'
}

function Test-Dir([string]$dir) {
    if (-not (Test-Path -LiteralPath $dir)) {
        Write-Line ("  ✗ {0}  —— 目录不存在" -f $dir) 'DarkYellow'
        return
    }
    $probe = Join-Path $dir ("xdao-write-test-{0}.tmp" -f (Get-Random))
    try {
        Set-Content -LiteralPath $probe -Value 'ok' -Encoding UTF8 -ErrorAction Stop
        Remove-Item -LiteralPath $probe -Force -ErrorAction SilentlyContinue
        Write-Line ("  ✓ {0}  —— 能写" -f $dir) 'Green'
    } catch {
        Write-Line ("  ✗ {0}  —— 写不进去：{1}" -f $dir, $_.Exception.Message) 'Red'
    }
}

Add-Log 'X岛串导出工具 · 写入诊断报告'
Add-Log ("生成时间：{0}" -f (Get-Date).ToString('yyyy-MM-dd HH:mm:ss'))

Write-Head '当前账户'
Write-Line ("  用户      : {0}" -f $env:USERNAME)
Write-Line ("  是否管理员: {0}" -f ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator))
Write-Line ("  系统      : {0}" -f (Get-CimInstance Win32_OperatingSystem).Caption)
Write-Line ("  PowerShell: {0}" -f $PSVersionTable.PSVersion)

Write-Head '磁盘'
Get-CimInstance Win32_LogicalDisk | ForEach-Object {
    $fs = $_.FileSystem
    $note = ''
    if ($_.DriveType -eq 2) { $note = '（可移动磁盘：可能有写保护开关）' }
    if ($_.DriveType -eq 5) { $note = '（光驱，只读）' }
    Write-Line ("  {0}  {1,-6} 剩余 {2:N1} GB  {3}" -f $_.DeviceID, $fs, ($_.FreeSpace / 1GB), $note)
}

# 用户实际用过的导出目录：默认值 + 配置里记着的 + 桌面/文档（受控文件夹访问的保护范围）
$desktop = [Environment]::GetFolderPath('Desktop')
$docs = [Environment]::GetFolderPath('MyDocuments')
$configPath = Join-Path $env:APPDATA 'xdao-export\config.json'
$configured = @()
if (Test-Path -LiteralPath $configPath) {
    try {
        $cfg = Get-Content -LiteralPath $configPath -Raw -Encoding UTF8 | ConvertFrom-Json
        if ($cfg.output_dir) { $configured += $cfg.output_dir }
        if ($cfg.cache_dir) { $configured += $cfg.cache_dir }
    } catch {
        Add-Log ("  （读配置失败：{0}）" -f $_.Exception.Message)
    }
}

$targets = @('D:\', 'D:\X岛', 'D:\X岛\.cache', 'C:\X岛导出', (Join-Path $desktop 'xdao-写入测试'))
if ($desktop) { $targets += $desktop }
$targets += $configured
$more = @(
    (Join-Path $docs 'xdao-写入测试'),
    (Join-Path $env:LOCALAPPDATA 'xdao-export\导出'),
    $env:TEMP
) | Where-Object { $_ }
$targets += $more

Write-Head '写入测试（每个目录试建一个临时文件再删掉）'
Write-Line '  结论看这里：✓ 是能写。全部 ✗ 才说明系统级拦截。'
$seen = @{}
foreach ($dir in $targets) {
    if ($seen.ContainsKey($dir)) { continue }
    $seen[$dir] = $true
    Test-Dir $dir
}

Write-Head '关键目录的权限（谁被允许写）'
foreach ($dir in (@('D:\X岛') + $configured + @($desktop)) | Where-Object { $_ } | Select-Object -Unique) {
    Write-Line ("  [{0}]" -f $dir)
    if (-not (Test-Path -LiteralPath $dir)) { Write-Line '    目录不存在' 'DarkYellow'; continue }
    try {
        $acl = Get-Acl -LiteralPath $dir
        Write-Line ("    所有者: {0}" -f $acl.Owner)
        $acl.Access | ForEach-Object {
            Write-Line ("    {0,-42} {1,-18} {2}" -f $_.IdentityReference, $_.AccessControlType, $_.FileSystemRights)
        }
        Write-Line '    提示：上面「允许」行里应该有你（或 Users / Everyone）带 Write 或 FullControl；'
        Write-Line '          只有 SYSTEM / Administrators 就说明普通账户写不进去。'
    } catch {
        Write-Line ("    读权限失败：{0}" -f $_.Exception.Message) 'Red'
    }
}

Write-Head '勒索软件防护 / 受控文件夹访问（最常见的原因）'
try {
    $mp = Get-MpPreference -ErrorAction Stop
    Write-Line ("  受控文件夹访问: {0}（1 = 已开启，0 = 关闭）" -f $mp.EnableControlledFolderAccess)
    if ($mp.ControlledFolderAccessProtectedFolders) {
        Write-Line '  受保护的文件夹:'
        $mp.ControlledFolderAccessProtectedFolders | ForEach-Object { Write-Line ("    {0}" -f $_) }
    } else {
        Write-Line '  受保护的文件夹: （没有）'
    }
    if ($mp.ControlledFolderAccessAllowedApplications) {
        Write-Line '  已放行的程序:'
        $mp.ControlledFolderAccessAllowedApplications | ForEach-Object { Write-Line ("    {0}" -f $_) }
    } else {
        Write-Line '  已放行的程序: （没有）'
    }
} catch {
    Write-Line ("  读不到 Defender 设置：{0}" -f $_.Exception.Message) 'DarkYellow'
    Write-Line '  这不代表没问题：装了第三方安全软件（火绒/360/腾讯管家）时，' 
    Write-Line '  受控文件夹访问或"文件夹保护"可能由它提供，需要到它自己的界面里看。'
}

# ---------- 落盘：优先写用户放脚本的目录，其次桌面，最后临时目录 ----------
Write-Head '结果文件'
$stamp = (Get-Date).ToString('yyyyMMdd-HHmmss')
$fileName = "诊断结果-$stamp.txt"
$candidates = @()
if ($PSScriptRoot) { $candidates += $PSScriptRoot }
if ($desktop) { $candidates += $desktop }
$candidates += $env:TEMP
foreach ($base in $candidates) {
    if (-not $base) { continue }
    try {
        if (-not (Test-Path -LiteralPath $base)) { continue }
        $target = Join-Path $base $fileName
        Set-Content -LiteralPath $target -Value ($script:logLines -join [Environment]::NewLine) -Encoding UTF8 -ErrorAction Stop
        $script:logPath = $target
        break
    } catch {
        Write-Line ("  写不了 {0}（{1}），换下一处" -f $base, $_.Exception.Message) 'DarkYellow'
    }
}
if ($script:logPath) {
    Write-Line ("  已保存：{0}" -f $script:logPath) 'Green'
    Write-Line '  把这个文件发给维护者即可（里面只有用户名和路径，没有饼干等隐私内容）。'
} else {
    Write-Line '  没能保存成文件 —— 请直接把本窗口的内容复制下来发给维护者。' 'Red'
}

Write-Head '结论怎么看'
Write-Line '  · 只有某几个是 ✗、LOCALAPPDATA 那行是 ✓  → 换个导出目录就能用（这一版起程序也会自动换）'
Write-Line '  · D:\ 和 D:\X岛 都是 ✗                  → 整个 D 盘写不进去（写保护 / 只读挂载 / 安全软件）'
Write-Line '  · 桌面/文档是 ✗、LOCALAPPDATA 是 ✓        → 受控文件夹访问在拦截，加白名单或换目录'
Write-Line '  · 所有行都是 ✗                          → 系统级限制，多半是安全软件或磁盘只读'
Write-Line ''
Write-Line '按回车关闭本窗口。'
Read-Host
