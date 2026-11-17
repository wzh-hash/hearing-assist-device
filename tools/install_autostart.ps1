# 听障人士感知器 · 开机自启安装（任务计划程序）
# 用法：双击 tools\install_autostart.bat，或在 PowerShell 里运行本脚本。
$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $PSScriptRoot
$taskName = 'HearingAidSensor'
$entry = Join-Path $root 'pc_web.py'
if (-not (Test-Path $entry)) {
    throw "项目目录不完整：找不到 $entry"
}

# 1) 找 pythonw.exe：目录内 runtime → DSH 运行时 → PATH
$candidates = @(
    (Join-Path $root 'runtime\pythonw.exe'),
    'C:\Users\MR\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\pythonw.exe'
)
$pythonw = $candidates | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $pythonw) {
    $cmd = Get-Command pythonw.exe -ErrorAction SilentlyContinue
    if ($cmd) { $pythonw = $cmd.Source }
}
if (-not $pythonw) {
    throw '找不到 pythonw.exe：请把 runtime\pythonw.exe 放进项目目录，或安装 Python 3.12。'
}

# 2) 任务：登录触发、隐藏窗口（pythonw）、失败重启
$action = New-ScheduledTaskAction -Execute $pythonw `
    -Argument '"pc_web.py" --no-browser' -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -MultipleInstances IgnoreNew `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero)
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType Interactive -RunLevel Limited

try {
    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
        -Settings $settings -Principal $principal -Force `
        -Description '听障人士感知器（网页控制台，登录隐藏启动）' | Out-Null
} catch {
    Write-Host ''
    Write-Host '安装失败：' -ForegroundColor Red
    Write-Host $_.Exception.Message
    Write-Host ''
    Write-Host '如果提示“拒绝访问”，请用管理员 PowerShell 重跑本脚本，'
    Write-Host '或改用“启动”文件夹方式（见 README 的产品运行一节）。'
    exit 1
}

Write-Host ''
Write-Host "已安装：任务计划程序 -> $taskName" -ForegroundColor Green
Write-Host '登录 Windows 后会隐藏启动服务；现在可以手动启动一次：'
Write-Host "    Start-ScheduledTask -TaskName $taskName"
Write-Host '打开页面：http://127.0.0.1:8765/'
Write-Host '日志：sounds\logs\product.log'
