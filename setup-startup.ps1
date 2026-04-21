#Requires -Version 5.1
<#
.SYNOPSIS
    Registers (or removes) a Windows Task Scheduler task that launches
    run-server.ps1 in a visible PowerShell window whenever you log in.

.USAGE
    # Install / update:
    powershell -ExecutionPolicy Bypass -File setup-startup.ps1

    # Remove:
    powershell -ExecutionPolicy Bypass -File setup-startup.ps1 -Remove
#>
param([switch]$Remove)

$TaskName  = 'mw-backend'
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Target    = Join-Path $ScriptDir 'run-server.ps1'

if ($Remove) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "Task '$TaskName' removed." -ForegroundColor Yellow
    exit 0
}

# Trigger: when the current user logs on
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME

# Action: open a new PowerShell window running run-server.ps1
# -Relaunched suppresses the VS Code detection check (not needed at login)
$action = New-ScheduledTaskAction `
    -Execute 'powershell.exe' `
    -Argument "-ExecutionPolicy Bypass -File `"$Target`" -Relaunched" `
    -WorkingDirectory $ScriptDir

# Settings: interactive (visible window), run only when logged on,
#           do not start a new instance if already running
$settings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit 0 `
    -MultipleInstances IgnoreNew `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries

# Principal: run as current user, interactive session (required for visible window)
$principal = New-ScheduledTaskPrincipal `
    -UserId $env:USERNAME `
    -LogonType Interactive `
    -RunLevel Limited

$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing) {
    Set-ScheduledTask -TaskName $TaskName `
        -Action $action -Trigger $trigger -Settings $settings -Principal $principal | Out-Null
    Write-Host "Task '$TaskName' updated." -ForegroundColor Green
} else {
    Register-ScheduledTask -TaskName $TaskName `
        -Action $action -Trigger $trigger -Settings $settings -Principal $principal | Out-Null
    Write-Host "Task '$TaskName' registered." -ForegroundColor Green
}

Write-Host ""
Write-Host "  Trigger:  at login for $env:USERNAME" -ForegroundColor DarkGray
Write-Host "  Script:   $Target" -ForegroundColor DarkGray
Write-Host "  Window:   visible (you can see all logs)" -ForegroundColor DarkGray
Write-Host ""
Write-Host "To remove it later:" -ForegroundColor DarkGray
Write-Host "  powershell -ExecutionPolicy Bypass -File setup-startup.ps1 -Remove" -ForegroundColor DarkGray
