#Requires -Version 5.1
<#
.SYNOPSIS
    Persistent mw-backend launcher for Windows.
    - Starts Flask server + Cloudflare named tunnel
    - Prevents sleep and hibernate while running
    - Blocks Windows shutdown/restart until stopped (Windows will prompt first)
    - Auto-restarts either process if it crashes
    - Safe to run from VS Code -- detects and relaunches in a standalone window
.USAGE
    Right-click -> "Run with PowerShell"
    -- or --
    powershell -ExecutionPolicy Bypass -File run-server.ps1
#>
param([switch]$Relaunched)

$ErrorActionPreference = 'Continue'
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$PythonExe = Join-Path $ScriptDir 'venv\Scripts\python.exe'

# ── If running inside VS Code terminal, relaunch in a standalone window ───────
if (-not $Relaunched -and ($env:TERM_PROGRAM -eq 'vscode' -or $env:VSCODE_INJECTION)) {
    Write-Host "Detected VS Code terminal -- relaunching in standalone window..." -ForegroundColor Yellow
    Start-Process powershell -ArgumentList "-NoExit -ExecutionPolicy Bypass -File `"$($MyInvocation.MyCommand.Path)`" -Relaunched"
    exit 0
}

# ── Windows API: sleep prevention + shutdown blocking ─────────────────────────
Add-Type -Name "WinPower" -Namespace "MwBackend" -MemberDefinition @"
    [DllImport("kernel32.dll")]
    public static extern uint SetThreadExecutionState(uint esFlags);

    [DllImport("kernel32.dll")]
    public static extern IntPtr GetConsoleWindow();

    [DllImport("user32.dll")]
    public static extern bool ShutdownBlockReasonCreate(
        IntPtr hWnd,
        [MarshalAs(UnmanagedType.LPWStr)] string reason);

    [DllImport("user32.dll")]
    public static extern bool ShutdownBlockReasonDestroy(IntPtr hWnd);
"@

$ES_CONTINUOUS      = [uint32]2147483648
$ES_SYSTEM_REQUIRED = [uint32]1
$hwnd = [MwBackend.WinPower]::GetConsoleWindow()

function Enable-Prevention {
    [MwBackend.WinPower]::SetThreadExecutionState($ES_CONTINUOUS -bor $ES_SYSTEM_REQUIRED) | Out-Null
    [MwBackend.WinPower]::ShutdownBlockReasonCreate(
        $hwnd,
        "mw-backend is running -- stop the server before shutting down.") | Out-Null
    Write-Host "  Sleep / hibernate:        blocked" -ForegroundColor Green
    Write-Host "  Shutdown / restart:       blocked (Windows will prompt first)" -ForegroundColor Green
}

function Disable-Prevention {
    [MwBackend.WinPower]::SetThreadExecutionState($ES_CONTINUOUS) | Out-Null
    [MwBackend.WinPower]::ShutdownBlockReasonDestroy($hwnd) | Out-Null
}

# ── Load .env ─────────────────────────────────────────────────────────────────
$envFile = Join-Path $ScriptDir '.env'
if (Test-Path $envFile) {
    Get-Content $envFile | Where-Object { $_ -notmatch '^\s*#' -and $_ -match '=' } | ForEach-Object {
        $k, $v = $_ -split '=', 2
        [System.Environment]::SetEnvironmentVariable($k.Trim(), $v.Trim(), 'Process')
    }
}
$port = if ($env:PORT) { $env:PORT } else { '5050' }

# ── Process starters (UseShellExecute=$false -- logs appear in this window) ────
function Start-Flask {
    $psi = [System.Diagnostics.ProcessStartInfo]::new($PythonExe, 'server.py')
    $psi.WorkingDirectory = $ScriptDir
    $psi.UseShellExecute  = $false
    $p = [System.Diagnostics.Process]::Start($psi)
    Write-Host "$(Get-Date -f 'HH:mm:ss')  Server started (PID $($p.Id))" -ForegroundColor Green
    return $p
}

function Start-Tunnel {
    $psi = [System.Diagnostics.ProcessStartInfo]::new('cloudflared', 'tunnel run mw-backend')
    $psi.WorkingDirectory = $ScriptDir
    $psi.UseShellExecute  = $false
    $p = [System.Diagnostics.Process]::Start($psi)
    Write-Host "$(Get-Date -f 'HH:mm:ss')  Tunnel started (PID $($p.Id))" -ForegroundColor Green
    return $p
}

# ── Managed services (reboot-durable) ─────────────────────────────────────────
# Demo backends register themselves in data/services.json; this launcher is their
# SOLE starter, so they come back after a reboot and restart if they crash.
$DataDir      = Join-Path $ScriptDir 'data'
$ServicesFile = Join-Path $DataDir 'services.json'
if (-not (Test-Path $DataDir)) { New-Item -ItemType Directory -Path $DataDir -Force | Out-Null }

# Seed once so the existing Freight Factoring API keeps running after this change.
if (-not (Test-Path $ServicesFile)) {
    @(@{ name = 'freight-api'; cmd = 'node'; args = 'apps/api/dist/apps/api/src/main.js';
         cwd  = '..\upwork-agentic-workflow\upwork-runs\trucking-freight-factoring-and-banking\demo-src';
         port = 3001 }) | ConvertTo-Json -Depth 5 | Set-Content -Path $ServicesFile -Encoding UTF8
}

$script:managed      = @{}   # name -> Process (for cleanup)
$script:svcLastStart = @{}   # name -> last launch time (debounce)

function Read-Services {
    if (-not (Test-Path $ServicesFile)) { return @() }
    try { $j = (Get-Content $ServicesFile -Raw) | ConvertFrom-Json }
    catch { Write-Host "$(Get-Date -f 'HH:mm:ss')  services.json invalid -- ignoring" -ForegroundColor Red; return @() }
    if ($null -eq $j) { return @() }
    if ($j -is [System.Array]) { return $j } else { return @($j) }
}

function Test-Port($p) {
    try {
        $c = New-Object System.Net.Sockets.TcpClient
        $iar = $c.BeginConnect('127.0.0.1', [int]$p, $null, $null)
        $up = $iar.AsyncWaitHandle.WaitOne(500) -and $c.Connected
        $c.Close(); return $up
    } catch { return $false }
}

function Start-ManagedService($svc) {
    $name = [string]$svc.name
    $cwd  = [string]$svc.cwd
    if (-not $cwd) { $cwd = $ScriptDir }
    elseif (-not [System.IO.Path]::IsPathRooted($cwd)) { $cwd = Join-Path $ScriptDir $cwd }
    # Normalize away any '..' segments -- Start-Process -WorkingDirectory rejects them.
    try { $cwd = [System.IO.Path]::GetFullPath($cwd) } catch {}
    $script:svcLastStart[$name] = Get-Date   # debounce regardless of outcome
    if (-not (Test-Path -LiteralPath $cwd -PathType Container)) {
        Write-Host "$(Get-Date -f 'HH:mm:ss')  service '$name' skipped: working dir not found -> $cwd" -ForegroundColor Yellow
        return
    }
    $out  = Join-Path $DataDir "$name.log"
    $errl = Join-Path $DataDir "$name.err.log"
    try {
        $p = Start-Process -FilePath ([string]$svc.cmd) -ArgumentList ([string]$svc.args) `
             -WorkingDirectory $cwd -WindowStyle Hidden -PassThru -ErrorAction Stop `
             -RedirectStandardOutput $out -RedirectStandardError $errl
        $script:managed[$name] = $p
        Write-Host "$(Get-Date -f 'HH:mm:ss')  service '$name' started (PID $($p.Id)) -> port $($svc.port)" -ForegroundColor Green
    } catch {
        Write-Host "$(Get-Date -f 'HH:mm:ss')  service '$name' failed to start: $($_.Exception.Message)" -ForegroundColor Red
    }
}

# Start anything in the manifest that is not already listening (debounced 20s).
function Ensure-Services {
    foreach ($svc in Read-Services) {
        if (-not $svc.name -or -not $svc.port) { continue }
        if (Test-Port $svc.port) { continue }
        $last = $script:svcLastStart[[string]$svc.name]
        if ($last -and ((Get-Date) - $last).TotalSeconds -lt 20) { continue }
        Start-ManagedService $svc
    }
}

# ── Cleanup ────────────────────────────────────────────────────────────────────
$script:flaskProc  = $null
$script:tunnelProc = $null

function Stop-All {
    Write-Host "`n$(Get-Date -f 'HH:mm:ss')  Stopping services..." -ForegroundColor Yellow
    foreach ($p in (@($script:flaskProc, $script:tunnelProc) + @($script:managed.Values))) {
        if ($p -and -not $p.HasExited) {
            try { $p.Kill() } catch {}
        }
    }
    Disable-Prevention
    Write-Host "  Stopped. Sleep / shutdown prevention released." -ForegroundColor Yellow
}

[System.Console]::add_CancelKeyPress({
    param($s, $e)
    $e.Cancel = $true
    Stop-All
    [System.Environment]::Exit(0)
})

# ── Banner ─────────────────────────────────────────────────────────────────────
Write-Host ""
Write-Host "==========================================" -ForegroundColor Cyan
Write-Host "  mw-backend  --  persistent launcher" -ForegroundColor Cyan
Write-Host "==========================================" -ForegroundColor Cyan
Enable-Prevention
Write-Host "  Auto-restart on crash:    ON" -ForegroundColor Green
Write-Host ""

# ── Start services ─────────────────────────────────────────────────────────────
Write-Host "-> Waitress server on port $port..."
$script:flaskProc = Start-Flask

Write-Host "-> Cloudflare Tunnel (mw-backend -> api.michaelwegter.com)..."
$script:tunnelProc = Start-Tunnel

Write-Host "-> Managed services from data/services.json..."
Ensure-Services

Write-Host ""
Write-Host "✓ Running. Press Ctrl+C to stop cleanly." -ForegroundColor Green
Write-Host "  Health: https://api.michaelwegter.com/health" -ForegroundColor DarkGray

# ── Auto-deploy: poll git; on a new commit, pull + restart the server IN THIS ──
# ── window (the Python child is replaced; this window never closes).          ──
$script:branch = (git -C $ScriptDir rev-parse --abbrev-ref HEAD 2>$null)
if (-not $script:branch) { $script:branch = 'main' }
$pollEvery = if ($env:AUTO_DEPLOY_SECONDS) { [int]$env:AUTO_DEPLOY_SECONDS } else { 30 }
$lastPoll  = Get-Date
Write-Host "  Auto-deploy:              ON (every ${pollEvery}s on $script:branch)" -ForegroundColor Green
Write-Host ""

function Invoke-AutoDeploy {
    try {
        git -C $ScriptDir fetch origin $script:branch --quiet 2>$null
        $localRev  = (git -C $ScriptDir rev-parse HEAD 2>$null)
        $remoteRev = (git -C $ScriptDir rev-parse "origin/$script:branch" 2>$null)
        if ($localRev -and $remoteRev -and $localRev -ne $remoteRev) {
            Write-Host ""
            Write-Host "$(Get-Date -f 'HH:mm:ss')  New commit on origin/$script:branch -- pulling..." -ForegroundColor Cyan
            $changed = (git -C $ScriptDir diff --name-only HEAD "origin/$script:branch" 2>$null)
            git -C $ScriptDir pull --ff-only 2>&1 | Write-Host
            if ($changed -match 'requirements\.txt') {
                Write-Host "  requirements.txt changed -- installing deps..." -ForegroundColor Yellow
                & $PythonExe -m pip install -r (Join-Path $ScriptDir 'requirements.txt') 2>&1 | Write-Host
            }
            Write-Host "  Restarting server in this window (window stays open)..." -ForegroundColor Yellow
            if ($script:flaskProc -and -not $script:flaskProc.HasExited) { try { $script:flaskProc.Kill() } catch {} }
            Start-Sleep -Milliseconds 500
            $script:flaskProc = Start-Flask
            Write-Host "$(Get-Date -f 'HH:mm:ss')  Deploy complete." -ForegroundColor Green
            Write-Host ""
        }
    } catch {
        Write-Host "$(Get-Date -f 'HH:mm:ss')  auto-deploy check failed: $_" -ForegroundColor Red
    }
}

# ── Monitor loop — restart crashed processes + auto-deploy ────────────────────
while ($true) {
    Start-Sleep -Seconds 10

    if ($script:flaskProc.HasExited) {
        Write-Host "$(Get-Date -f 'HH:mm:ss')  Server exited (code $($script:flaskProc.ExitCode)) -- restarting..." -ForegroundColor Yellow
        $script:flaskProc = Start-Flask
    }

    if ($script:tunnelProc.HasExited) {
        Write-Host "$(Get-Date -f 'HH:mm:ss')  Tunnel exited (code $($script:tunnelProc.ExitCode)) -- restarting..." -ForegroundColor Yellow
        $script:tunnelProc = Start-Tunnel
    }

    Ensure-Services

    if (((Get-Date) - $lastPoll).TotalSeconds -ge $pollEvery) {
        Invoke-AutoDeploy
        $lastPoll = Get-Date
    }
}
