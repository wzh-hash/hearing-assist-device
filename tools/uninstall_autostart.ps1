# 听障人士感知器 · 开机自启卸载
$ErrorActionPreference = 'Stop'
$taskName = 'HearingAidSensor'
$root = Split-Path -Parent $PSScriptRoot

try {
    Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
} catch {
    # 任务不存在或已停止，继续卸载
}

try {
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction Stop
    Write-Host "已卸载任务：$taskName" -ForegroundColor Green
} catch {
    Write-Host "没有找到任务 $taskName（可能未安装）。"
}

# 兜底：停掉本项目由 pythonw 启动的进程（只匹配 pc_web.py）
Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
    Where-Object {
        $_.Name -eq 'pythonw.exe' -and
        $_.CommandLine -like '*pc_web.py*' -and
        $_.CommandLine -like "*$root*"
    } |
    ForEach-Object {
        try {
            Stop-Process -Id $_.ProcessId -Force -ErrorAction Stop
            Write-Host "已停止进程 $($_.ProcessId)"
        } catch {
            Write-Host "停止进程 $($_.ProcessId) 失败：$($_.Exception.Message)"
        }
    }

Write-Host '提示：删除任务不会删除录音和配置（sounds\）。'
