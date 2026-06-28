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

# Kill whatever is LISTENING on a port (used to reclaim Flask's port from a
# squatter — e.g. a managed demo service that bound 5050 by mistake).
function Free-Port($p) {
    try {
        $pids = Get-NetTCPConnection -LocalPort ([int]$p) -State Listen -ErrorAction SilentlyContinue |
                Select-Object -ExpandProperty OwningProcess -Unique
        foreach ($procId in $pids) {
            if ($procId -and $procId -ne 0 -and $procId -ne $PID) {
                try { Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue
                      Write-Host "$(Get-Date -f 'HH:mm:ss')  freed port $p (killed PID $procId that was squatting it)" -ForegroundColor Yellow } catch {}
            }
        }
    } catch {}
}

# ── Process starters (UseShellExecute=$false -- logs appear in this window) ────
function Start-Flask {
    Free-Port $port   # GUARANTEE Flask owns its port; evict any squatter first
    $psi = [System.Diagnostics.ProcessStartInfo]::new($PythonExe, 'server.py')
    $psi.WorkingDirectory = $ScriptDir
    $psi.UseShellExecute  = $false
    $p = [System.Diagnostics.Process]::Start($psi)
    Write-Host "$(Get-Date -f 'HH:mm:ss')  Server started (PID $($p.Id)) on port $port" -ForegroundColor Green
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

# ── Normalize Cloudflare tunnel config ────────────────────────────────────────
# INVARIANT: api.michaelwegter.com MUST route 100% of traffic to Flask ($port).
# Managed services are exposed via Flask bridge blueprints, never directly via
# the tunnel. Deploy scripts MUST NOT modify ~/.cloudflared/config.yml.
# This function detects and corrects any violation on every monitor tick and at
# startup; if it finds wrong routing it fixes the config and kills the tunnel so
# the monitor loop restarts it with the corrected config (~10s recovery).
function Normalize-TunnelConfig {
    $cfgPath = Join-Path $env:USERPROFILE '.cloudflared\config.yml'
    if (-not (Test-Path $cfgPath)) { return }
    try {
        $lines = Get-Content $cfgPath
        # Wrong if any ingress service line points to a non-Flask port, OR if
        # any path-scoped routing exists (path: means split routing = broken).
        $wrongRoute = $lines | Where-Object {
            ($_ -match 'service:\s*http://localhost:' -and
             $_ -notmatch "localhost:$port" -and
             $_ -notmatch 'http_status') -or
            ($_ -match '^\s+path:')
        }
        if (-not $wrongRoute) { return }
        Write-Host "$(Get-Date -f 'HH:mm:ss')  BAD tunnel config detected (non-Flask ingress) -- auto-fixing" -ForegroundColor Red
        # Preserve only tunnel: and credentials-file: header lines; rewrite ingress.
        $header = ($lines | Where-Object { $_ -match '^tunnel:|^credentials-file:' }) -join "`n"
        $corrected = "$header`n`ningress:`n  - hostname: api.michaelwegter.com`n    service: http://localhost:$port`n  - service: http_status:404"
        Set-Content -Path $cfgPath -Value $corrected -NoNewline
        Write-Host "$(Get-Date -f 'HH:mm:ss')  Tunnel config fixed -> all traffic to Flask :$port" -ForegroundColor Green
        # Kill running tunnel; monitor loop will restart it with corrected config.
        if ($script:tunnelProc -and -not $script:tunnelProc.HasExited) {
            Write-Host "$(Get-Date -f 'HH:mm:ss')  Restarting tunnel with corrected config..." -ForegroundColor Yellow
            try { $script:tunnelProc.Kill() } catch {}
            Start-Sleep -Milliseconds 500
        }
    } catch {
        Write-Host "$(Get-Date -f 'HH:mm:ss')  Normalize-TunnelConfig error: $_" -ForegroundColor Yellow
    }
}

# ── Managed services (reboot-durable) ─────────────────────────────────────────
# Demo backends register themselves in data/services.json; this launcher is their
# SOLE starter, so they come back after a reboot and restart if they crash.
$DataDir      = Join-Path $ScriptDir 'data'
$ServicesFile = Join-Path $DataDir 'services.json'
if (-not (Test-Path $DataDir)) { New-Item -ItemType Directory -Path $DataDir -Force | Out-Null }

# No seed: services.json starts empty and the workflow registers real services
# into it (their files live in the runner workspace, which IS present on the
# Surface). A missing file is treated as "no services".

$script:managed      = @{}   # name -> Process (for cleanup)
$script:svcLastStart = @{}   # name -> last launch time (debounce)

function Read-Services {
    if (-not (Test-Path $ServicesFile)) { return @() }
    try { $j = (Get-Content $ServicesFile -Raw) | ConvertFrom-Json }
    catch { Write-Host "$(Get-Date -f 'HH:mm:ss')  services.json invalid -- ignoring" -ForegroundColor Red; return @() }
    if ($null -eq $j) { return @() }
    if ($j -is [System.Array]) { return $j } else { return @($j) }
}

# Merge repo-tracked services.manifest.json into data/services.json (by name).
function Sync-ServiceManifest {
    $manifest = Join-Path $ScriptDir 'services.manifest.json'
    if (-not (Test-Path $manifest)) { return }
    try {
        $entries = @((Get-Content $manifest -Raw) | ConvertFrom-Json)
        $byName = @{}
        foreach ($e in Read-Services) { $byName[[string]$e.name] = $e }
        foreach ($e in $entries) { $byName[[string]$e.name] = $e }
        ($byName.Values | ConvertTo-Json -Depth 5) | Set-Content -Path $ServicesFile
    } catch {
        Write-Host "$(Get-Date -f 'HH:mm:ss')  services.manifest.json sync failed: $_" -ForegroundColor Red
    }
}

function Stop-ManagedService($name) {
    if ($script:managed[$name] -and -not $script:managed[$name].HasExited) {
        try { $script:managed[$name].Kill() } catch {}
    }
    if ($script:managed.ContainsKey($name)) { $script:managed.Remove($name) }
    foreach ($svc in Read-Services) {
        if ([string]$svc.name -eq $name -and $svc.port) { Free-Port $svc.port }
    }
}

function Build-OrschellService {
    $svcDir = Join-Path $ScriptDir 'services/orschell-ecommerce'
    if (-not (Test-Path $svcDir)) { return }
    $npm = if (Get-Command npm.cmd -ErrorAction SilentlyContinue) { 'npm.cmd' } else { 'npm' }
    Write-Host "  Building orschell-ecommerce-api..." -ForegroundColor Yellow
    Push-Location $svcDir
    try {
        & $npm install 2>&1 | Write-Host
        if ($LASTEXITCODE -ne 0) { throw "npm install failed ($LASTEXITCODE)" }
        & $npm run build 2>&1 | Write-Host
        if ($LASTEXITCODE -ne 0) { throw "npm run build failed ($LASTEXITCODE)" }
    } finally { Pop-Location }
    Stop-ManagedService 'orschell-ecommerce-api'
    Write-Host "  orschell-ecommerce-api built; will restart on next tick" -ForegroundColor Green
}

function Ensure-OrschellBuilt {
    $svcDir = Join-Path $ScriptDir 'services/orschell-ecommerce'
    $distJs = Join-Path $svcDir 'dist/index.js'
    $srcDir = Join-Path $svcDir 'src'
    if (-not (Test-Path (Join-Path $svcDir 'package.json'))) { return }
    $needsBuild = -not (Test-Path $distJs)
    if (-not $needsBuild -and (Test-Path $srcDir)) {
        $srcNewest = (Get-ChildItem $srcDir -Recurse -File | Sort-Object LastWriteTime -Descending | Select-Object -First 1).LastWriteTime
        $needsBuild = $srcNewest -gt (Get-Item $distJs).LastWriteTime
    }
    if ($needsBuild) { Build-OrschellService }
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
    $svcPort = 0; [void][int]::TryParse([string]$svc.port, [ref]$svcPort)
    $script:svcLastStart[$name] = Get-Date   # debounce regardless of outcome
    # HARD INVARIANT: a managed service may NEVER take Flask's port. (A demo service
    # that read process.env.PORT once inherited 5050 and hijacked the whole API.)
    if ($svcPort -eq [int]$port) {
        Write-Host "$(Get-Date -f 'HH:mm:ss')  service '$name' REFUSED: its port ($svcPort) is Flask's port. Re-register it on a different port." -ForegroundColor Red
        return
    }
    $cwd  = [string]$svc.cwd
    if (-not $cwd) { $cwd = $ScriptDir }
    elseif (-not [System.IO.Path]::IsPathRooted($cwd)) { $cwd = Join-Path $ScriptDir $cwd }
    # Normalize away any '..' segments -- Start-Process -WorkingDirectory rejects them.
    try { $cwd = [System.IO.Path]::GetFullPath($cwd) } catch {}
    if (-not (Test-Path -LiteralPath $cwd -PathType Container)) {
        Write-Host "$(Get-Date -f 'HH:mm:ss')  service '$name' skipped: working dir not found -> $cwd" -ForegroundColor Yellow
        return
    }
    $out  = Join-Path $DataDir "$name.log"
    $errl = Join-Path $DataDir "$name.err.log"
    # Pin the child to ITS OWN port so it can never inherit Flask's PORT from .env.
    $savedPort = $env:PORT; $savedNest = $env:NEST_PORT
    if ($svcPort -gt 0) { $env:PORT = "$svcPort"; $env:NEST_PORT = "$svcPort" }
    try {
        $p = Start-Process -FilePath ([string]$svc.cmd) -ArgumentList ([string]$svc.args) `
             -WorkingDirectory $cwd -WindowStyle Hidden -PassThru -ErrorAction Stop `
             -RedirectStandardOutput $out -RedirectStandardError $errl
        $script:managed[$name] = $p
        Write-Host "$(Get-Date -f 'HH:mm:ss')  service '$name' started (PID $($p.Id)) -> port $svcPort" -ForegroundColor Green
    } catch {
        Write-Host "$(Get-Date -f 'HH:mm:ss')  service '$name' failed to start: $($_.Exception.Message)" -ForegroundColor Red
    } finally {
        $env:PORT = $savedPort; $env:NEST_PORT = $savedNest
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
Normalize-TunnelConfig
$script:tunnelProc = Start-Tunnel

Write-Host "-> Managed services from data/services.json..."
Sync-ServiceManifest
Ensure-OrschellBuilt
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
            Sync-ServiceManifest
            if ($changed -match 'services/orschell-ecommerce|services\.manifest\.json') {
                Build-OrschellService
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

    Normalize-TunnelConfig   # self-heal if a deploy script corrupted the config

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
