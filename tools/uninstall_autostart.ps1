# 听障人士感知器 · 开机自启卸载
$ErrorActionPreference = 'Continue'
$taskName = 'HearingAidSensor'
$root = Split-Path -Parent $PSScriptRoot

try {
    Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
} catch {
    # 任务不存在或已停止，继续
}

try {
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction Stop
    Write-Host "已卸载任务：$taskName"
} catch {
    Write-Host "没有找到任务 $taskName（可能未安装或本机无权限）。"
}

$startup = [Environment]::GetFolderPath('Startup')
$link = Join-Path $startup 'HearingAidSensor.lnk'
if (Test-Path $link) {
    Remove-Item $link -Force
    Write-Host "已删除启动快捷方式：$link"
}

# 先停看门狗，再停它拉起的网页进程，避免“杀掉又被拉起”
Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
    Where-Object {
        $_.Name -eq 'pythonw.exe' -and
        $_.CommandLine -like '*watchdog.py*' -and
        $_.CommandLine -like "*$root*"
    } |
    ForEach-Object {
        try {
            Stop-Process -Id $_.ProcessId -Force -ErrorAction Stop
            Write-Host "已停止看门狗进程 $($_.ProcessId)"
        } catch {
            Write-Host "停止看门狗失败：$($_.Exception.Message)"
        }
    }

Start-Sleep -Seconds 1

Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
    Where-Object {
        $_.Name -eq 'pythonw.exe' -and
        $_.CommandLine -like '*pc_web.py*' -and
        $_.CommandLine -like '*--no-browser*'
    } |
    ForEach-Object {
        try {
            Stop-Process -Id $_.ProcessId -Force -ErrorAction Stop
            Write-Host "已停止网页进程 $($_.ProcessId)"
        } catch {
            Write-Host "停止网页进程失败：$($_.Exception.Message)"
        }
    }

Write-Host '提示：删除自启不会删除录音和配置（sounds\）。'
