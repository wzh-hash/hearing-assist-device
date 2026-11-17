# 听障人士感知器 · 开机自启安装
# 优先用任务计划程序；当前账户没有注册权限时，自动退回“启动”文件夹 + 看门狗。
# 两种方式都是隐藏运行，并由 watchdog.py 负责崩溃自动拉起。
$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $PSScriptRoot
$taskName = 'HearingAidSensor'
$watchdog = Join-Path $root 'tools\watchdog.py'
$entry = Join-Path $root 'pc_web.py'
if (-not (Test-Path $entry)) { throw "项目目录不完整：找不到 $entry" }
if (-not (Test-Path $watchdog)) { throw "缺少守护脚本：$watchdog" }

# 1) pythonw.exe：目录内 runtime → DSH 运行时 → PATH
$candidates = @(
    (Join-Path $root 'runtime\pythonw.exe'),
    'C:\Users\MR\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\pythonw.exe'
)
$pythonw = $candidates | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $pythonw) {
    $cmd = Get-Command pythonw.exe -ErrorAction SilentlyContinue
    if ($cmd) { $pythonw = $cmd.Source }
}
if (-not $pythonw) { throw '找不到 pythonw.exe：请把 runtime\pythonw.exe 放进项目目录，或安装 Python 3.12。' }

function Start-WatchdogNow {
    Start-Process -FilePath $pythonw -ArgumentList "`"$watchdog`"" `
        -WorkingDirectory $root -WindowStyle Hidden
}

function Install-StartupShortcut {
    $startup = [Environment]::GetFolderPath('Startup')
    $link = Join-Path $startup 'HearingAidSensor.lnk'
    $shell = New-Object -ComObject WScript.Shell
    $shortcut = $shell.CreateShortcut($link)
    $shortcut.TargetPath = $pythonw
    $shortcut.Arguments = "`"$watchdog`""
    $shortcut.WorkingDirectory = $root
    $shortcut.WindowStyle = 1
    $shortcut.Description = '听障人士感知器（隐藏启动 + 看门狗）'
    $shortcut.Save()
    return $link
}

# 2) 首选：任务计划程序（登录触发、隐藏、任务异常可重试）
$taskInstalled = $false
try {
    $action = New-ScheduledTaskAction -Execute $pythonw `
        -Argument "`"$watchdog`"" -WorkingDirectory $root
    $trigger = New-ScheduledTaskTrigger -AtLogOn
    $settings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -StartWhenAvailable -MultipleInstances IgnoreNew `
        -RestartCount 99 -RestartInterval (New-TimeSpan -Minutes 1) `
        -ExecutionTimeLimit ([TimeSpan]::Zero)
    $principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" `
        -LogonType Interactive -RunLevel Limited
    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
        -Settings $settings -Principal $principal -Force `
        -Description '听障人士感知器（隐藏启动 + 看门狗）' | Out-Null
    $taskInstalled = $true
} catch {
    Write-Host '任务计划程序不可用（当前账户没有注册权限），改用“启动”文件夹 + 看门狗。'
}

if ($taskInstalled) {
    Write-Host "已安装：任务计划程序 -> $taskName" -ForegroundColor Green
} else {
    $link = Install-StartupShortcut
    Write-Host "已安装：启动文件夹快捷方式 -> $link" -ForegroundColor Green
}

Start-WatchdogNow
Write-Host '看门狗已启动；它会确保网页服务在 8765 端口运行，崩溃后约 20 秒自动拉起。'
Write-Host '打开页面：http://127.0.0.1:8765/'
Write-Host '日志：sounds\logs\product.log、sounds\logs\watchdog.log'
Write-Host '卸载：tools\uninstall_autostart.bat'
