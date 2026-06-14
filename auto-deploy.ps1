#Requires -Version 5.1
<#
.SYNOPSIS
    mw-backend auto-deploy watcher.
    Polls the git remote on an interval. When a new commit appears on the tracked
    branch, it pulls, installs deps if requirements.txt changed, and restarts the
    Flask server by killing server.py -- run-server.ps1's monitor loop then
    relaunches it with the new code (the tunnel keeps running).

    Run this ALONGSIDE run-server.ps1 (which must be running for the auto-restart
    to happen). If no running server is found, this script starts one as a fallback.

.USAGE
    powershell -ExecutionPolicy Bypass -File auto-deploy.ps1
    powershell -ExecutionPolicy Bypass -File auto-deploy.ps1 -IntervalSeconds 15
#>
param([int]$IntervalSeconds = 30, [switch]$Relaunched)

$ErrorActionPreference = 'Continue'
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$PythonExe = Join-Path $ScriptDir 'venv\Scripts\python.exe'
function ts { Get-Date -Format 'HH:mm:ss' }

# If launched from a VS Code terminal, relaunch in a standalone window.
if (-not $Relaunched -and ($env:TERM_PROGRAM -eq 'vscode' -or $env:VSCODE_INJECTION)) {
    Start-Process powershell -ArgumentList "-NoExit -ExecutionPolicy Bypass -File `"$($MyInvocation.MyCommand.Path)`" -Relaunched -IntervalSeconds $IntervalSeconds"
    exit 0
}

Write-Host ""
Write-Host "==========================================" -ForegroundColor Cyan
Write-Host "  mw-backend  --  auto-deploy watcher" -ForegroundColor Cyan
Write-Host "==========================================" -ForegroundColor Cyan
Write-Host "  repo:      $ScriptDir"
Write-Host "  interval:  ${IntervalSeconds}s"
Write-Host "  On a new commit: git pull -> restart server.py (run-server.ps1 relaunches it)."
Write-Host "  Press Ctrl+C to stop." -ForegroundColor DarkGray
Write-Host ""

function Restart-Backend {
    $killed = $false
    try {
        Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" -ErrorAction SilentlyContinue |
            Where-Object { $_.CommandLine -and $_.CommandLine -match 'server\.py' } |
            ForEach-Object {
                Write-Host "$(ts)  Stopping server.py (PID $($_.ProcessId))" -ForegroundColor Yellow
                Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
                $killed = $true
            }
    } catch {}

    if ($killed) {
        Write-Host "$(ts)  Server stopped. run-server.ps1 will relaunch it with the new code." -ForegroundColor Green
    } else {
        Write-Host "$(ts)  No running server found -- starting one as a fallback." -ForegroundColor Yellow
        try {
            $psi = [System.Diagnostics.ProcessStartInfo]::new($PythonExe, 'server.py')
            $psi.WorkingDirectory = $ScriptDir
            $psi.UseShellExecute = $true
            [System.Diagnostics.Process]::Start($psi) | Out-Null
        } catch {
            Write-Host "$(ts)  Could not start server: $_" -ForegroundColor Red
        }
    }
}

while ($true) {
    try {
        git -C $ScriptDir fetch --quiet 2>$null
        $local  = (git -C $ScriptDir rev-parse HEAD 2>$null)
        $remote = (git -C $ScriptDir rev-parse '@{u}' 2>$null)
        if ($local -and $remote -and ($local -ne $remote)) {
            Write-Host "$(ts)  New commit on remote -- deploying..." -ForegroundColor Cyan
            $changed = git -C $ScriptDir diff --name-only HEAD '@{u}' 2>$null
            git -C $ScriptDir pull --ff-only 2>&1 | Write-Host
            if ($changed -match 'requirements\.txt') {
                Write-Host "$(ts)  requirements.txt changed -- installing deps..." -ForegroundColor Yellow
                & $PythonExe -m pip install -r (Join-Path $ScriptDir 'requirements.txt') 2>&1 | Write-Host
            }
            Restart-Backend
            Write-Host "$(ts)  Deploy complete." -ForegroundColor Green
        }
    } catch {
        Write-Host "$(ts)  auto-deploy check failed: $_" -ForegroundColor Red
    }
    Start-Sleep -Seconds $IntervalSeconds
}
