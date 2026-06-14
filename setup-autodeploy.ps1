#Requires -Version 5.1
<#
.SYNOPSIS
    Registers (or removes) a Windows Task Scheduler task that runs
    auto-deploy.ps1 at login, so the backend auto-pulls + restarts on every push.
    Run this once, in addition to setup-startup.ps1 (which runs the server itself).

.USAGE
    # Install / update:
    powershell -ExecutionPolicy Bypass -File setup-autodeploy.ps1

    # Custom poll interval (seconds):
    powershell -ExecutionPolicy Bypass -File setup-autodeploy.ps1 -IntervalSeconds 15

    # Remove:
    powershell -ExecutionPolicy Bypass -File setup-autodeploy.ps1 -Remove
#>
param([int]$IntervalSeconds = 30, [switch]$Remove)

$TaskName  = 'mw-backend-autodeploy'
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Target    = Join-Path $ScriptDir 'auto-deploy.ps1'

if ($Remove) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "Task '$TaskName' removed." -ForegroundColor Yellow
    exit 0
}

$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME

$action = New-ScheduledTaskAction `
    -Execute 'powershell.exe' `
    -Argument "-ExecutionPolicy Bypass -File `"$Target`" -Relaunched -IntervalSeconds $IntervalSeconds" `
    -WorkingDirectory $ScriptDir

$settings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit 0 `
    -MultipleInstances IgnoreNew `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries

$principal = New-ScheduledTaskPrincipal `
    -UserId $env:USERNAME `
    -LogonType Interactive `
    -RunLevel Limited

$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing) {
    Set-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal | Out-Null
    Write-Host "Task '$TaskName' updated." -ForegroundColor Green
} else {
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal | Out-Null
    Write-Host "Task '$TaskName' registered." -ForegroundColor Green
}

Write-Host ""
Write-Host "  Trigger:   at login for $env:USERNAME" -ForegroundColor DarkGray
Write-Host "  Script:    $Target" -ForegroundColor DarkGray
Write-Host "  Interval:  ${IntervalSeconds}s" -ForegroundColor DarkGray
Write-Host ""
Write-Host "Remove later:" -ForegroundColor DarkGray
Write-Host "  powershell -ExecutionPolicy Bypass -File setup-autodeploy.ps1 -Remove" -ForegroundColor DarkGray
