# Windows：把中转注册成「开机自启」的计划任务（无需管理员权限，跑在当前用户下）。
#
# 用法（PowerShell）：
#   powershell -ExecutionPolicy Bypass -File service\install-windows-service.ps1
#
# 可选参数：
#   -TaskName my-codex-router   自定义任务名（默认 codex-model-router）
#   -Uninstall                  卸载
#
# 说明：
#   * 上游 AK 必须已经用 setx 设成用户级环境变量（setx EXAMPLE_GATEWAY_API_KEY "..."），
#     否则计划任务起来后读不到 key。设完记得开新的 PowerShell 窗口再装。
#   * 日志写到 %TEMP%\codex-model-router.log（通过 ROUTER_LOG_FILE 环境变量；计划任务
#     没有 stdout/stderr 重定向，不靠这个变量就完全没日志可查）

param(
    [string]$TaskName = "codex-model-router",
    [switch]$Uninstall
)

$ErrorActionPreference = "Stop"

if ($Uninstall) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "已卸载计划任务 $TaskName"
    return
}

$repo = Split-Path -Parent $PSScriptRoot
$script = Join-Path $repo "codex-model-router.py"
if (-not (Test-Path $script)) { throw "找不到 $script" }

$python = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $python) { $python = (Get-Command py -ErrorAction SilentlyContinue).Source }
if (-not $python) { throw "找不到 python / py，请先装 Python 3.9+ 并加入 PATH" }

$log = Join-Path $env:TEMP "codex-model-router.log"
$codexHome = if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $env:USERPROFILE ".codex" }
$port = if ($env:CODEX_ROUTER_PORT) { $env:CODEX_ROUTER_PORT } else { "8317" }

# 计划任务不会继承你交互式 shell 的临时变量，所以把关键变量写成用户级环境变量，
# 让任务启动的 python 进程能读到。AK 仍由你自己用 setx 维护，这里不碰。
[Environment]::SetEnvironmentVariable("CODEX_HOME", $codexHome, "User")
[Environment]::SetEnvironmentVariable("CODEX_ROUTER_PORT", $port, "User")
[Environment]::SetEnvironmentVariable("ROUTER_LOG_FILE", $log, "User")

$action = New-ScheduledTaskAction -Execute $python `
    -Argument "`"$script`"" `
    -WorkingDirectory $repo

$trigger = New-ScheduledTaskTrigger -AtLogOn

$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -StartWhenAvailable -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Seconds 5) -ExecutionTimeLimit (New-TimeSpan -Seconds 0)

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Settings $settings -Description "Codex multi-model router (loopback Responses proxy)" -Force | Out-Null

# 立刻起一次
Start-ScheduledTask -TaskName $TaskName

Write-Host ""
Write-Host "已注册并启动计划任务：$TaskName"
Write-Host "  中转    -> http://127.0.0.1:$port/v1/responses"
Write-Host "  CODEX_HOME = $codexHome"
Write-Host "  日志    -> $log"
Write-Host "            查看：Get-Content `"$log`" -Wait -Tail 50"
Write-Host ""
Write-Host "管理命令："
Write-Host "  Start-ScheduledTask -TaskName $TaskName"
Write-Host "  Stop-ScheduledTask  -TaskName $TaskName"
Write-Host "  Unregister-ScheduledTask -TaskName $TaskName -Confirm:`$false"
Write-Host ""
Write-Host "注意：计划任务不读交互式 shell 的临时变量。上游 AK 请用 setx 设成用户级环境变量："
Write-Host "  setx EXAMPLE_GATEWAY_API_KEY `"你的AK`""
Write-Host "设完开一个新的 PowerShell 窗口再重跑本脚本。"
