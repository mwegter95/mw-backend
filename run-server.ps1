#Requires -Version 5.1
<#
.SYNOPSIS
    Persistent mw-backend launcher for Windows.
    - Starts Flask server + Cloudflare named tunnel
    - Prevents SYSTEM sleep and hibernate while running (ES_SYSTEM_REQUIRED).
      It does NOT set ES_DISPLAY_REQUIRED, so the display should still turn off
      on its idle timer. If the screen is staying lit, run `powercfg /requests`
      in an elevated prompt — something else is holding a DISPLAY request.
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
$script:svcFails     = @{}   # name -> consecutive failures to come up
$script:svcParked    = @{}   # name -> $true once we stop retrying (logged once)

# A service that can never bind its port used to be relaunched every 20s
# forever, logging a cheerful "started" line each time, because Start-Process
# succeeding was treated as the service working. For a `docker run` the thing
# that "started" is the docker CLI, which exits immediately whether or not the
# container came up. Back off instead, and give up out loud.
$SVC_RETRY_BASE_SECONDS = 20
$SVC_RETRY_MAX_SECONDS  = 600
$SVC_MAX_FAILS          = 6
$SVC_PORT_GRACE_SECONDS = 8

# ── Docker engine (for container-backed services) ─────────────────────────────
# A service entry may be `"type": "container"`, meaning it is a Docker container
# this script starts rather than a process it launches. Those need the engine
# up, which is two separate things on Windows: com.docker.service (the
# privileged helper) and Docker Desktop itself, which hosts the actual engine in
# its WSL2/Hyper-V VM. The service alone is not enough.
$script:dockerLastTry = $null
$script:dockerFails   = 0
$script:dockerParked  = $false
$DOCKER_RETRY_SECONDS = 60
# Docker Desktop on 2014 hardware can take several minutes to bring WSL2 and the
# engine up from cold, so give it ten one-minute attempts before easing off.
$DOCKER_MAX_FAILS     = 10
$DOCKER_DESKTOP_EXE   = 'C:\Program Files\Docker\Docker\Docker Desktop.exe'


function Test-DockerEngine {
    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) { return $false }
    try {
        $null = & docker info --format '{{.ServerVersion}}' 2>$null
        return ($LASTEXITCODE -eq 0)
    } catch { return $false }
}

function Start-DockerEngine {
    $svc = Get-Service com.docker.service -ErrorAction SilentlyContinue
    if ($svc -and $svc.Status -ne 'Running') {
        try {
            Start-Service com.docker.service -ErrorAction Stop
            Write-Host "$(Get-Date -f 'HH:mm:ss')  started com.docker.service" -ForegroundColor Green
        } catch {
            Write-Host "$(Get-Date -f 'HH:mm:ss')  couldn't start com.docker.service — run this script as Administrator, or set it to Automatic once: Set-Service com.docker.service -StartupType Automatic" -ForegroundColor Yellow
        }
    }
    # The helper service does not host the engine. Docker Desktop does.
    if (-not (Get-Process 'Docker Desktop' -ErrorAction SilentlyContinue)) {
        if (Test-Path $DOCKER_DESKTOP_EXE) {
            try {
                Start-Process -FilePath $DOCKER_DESKTOP_EXE -WindowStyle Minimized -ErrorAction Stop
                Write-Host "$(Get-Date -f 'HH:mm:ss')  launching Docker Desktop (engine takes a minute or two on this hardware)" -ForegroundColor Yellow
            } catch {
                Write-Host "$(Get-Date -f 'HH:mm:ss')  couldn't launch Docker Desktop: $($_.Exception.Message)" -ForegroundColor Red
            }
        } else {
            Write-Host "$(Get-Date -f 'HH:mm:ss')  Docker Desktop not found at $DOCKER_DESKTOP_EXE" -ForegroundColor Red
        }
    }
}

# True when the engine is usable. Never blocks the monitor loop: it kicks off a
# start attempt at most once a minute and lets later ticks find the engine up.
function Ensure-DockerEngine {
    # Test BEFORE the park check, always. Parking means "stop trying to launch
    # it", not "stop noticing it" — otherwise an engine that comes up late, or
    # that Michael starts by hand, would be ignored for the rest of the run.
    if (Test-DockerEngine) {
        if ($script:dockerParked -or $script:dockerFails -gt 0) {
            Write-Host "$(Get-Date -f 'HH:mm:ss')  docker engine is up" -ForegroundColor Green
        }
        $script:dockerFails  = 0
        $script:dockerParked = $false
        return $true
    }
    if ($script:dockerParked) { return $false }
    $last = $script:dockerLastTry
    if ($last -and ((Get-Date) - $last).TotalSeconds -lt $DOCKER_RETRY_SECONDS) { return $false }
    $script:dockerLastTry = Get-Date
    $script:dockerFails++
    if ($script:dockerFails -gt $DOCKER_MAX_FAILS) {
        $script:dockerParked = $true
        Write-Host "$(Get-Date -f 'HH:mm:ss')  docker engine never came up after $DOCKER_MAX_FAILS attempts — no longer trying to launch it. Start Docker Desktop by hand and this picks it up on the next tick." -ForegroundColor Red
        return $false
    }
    Write-Host "$(Get-Date -f 'HH:mm:ss')  docker engine not reachable — starting it (attempt $($script:dockerFails)/$DOCKER_MAX_FAILS)" -ForegroundColor Yellow
    Start-DockerEngine
    return $false
}

function Read-Services {
    if (-not (Test-Path $ServicesFile)) { return @() }
    try { $raw = (Get-Content $ServicesFile -Raw | ConvertFrom-Json) }
    catch { Write-Host "$(Get-Date -f 'HH:mm:ss')  services.json invalid -- ignoring" -ForegroundColor Red; return @() }
    if ($null -eq $raw) { return @() }
    $items = if ($raw -is [System.Array]) { $raw } else { @($raw) }
    $out = @()
    foreach ($e in $items) {
        if ($e.name) { $out += $e; continue }
        if ($e.value) {
            foreach ($n in @($e.value)) { if ($n.name) { $out += $n } }
        }
    }
    return $out
}

# Merge repo-tracked services.manifest.json into data/services.json (by name).
function Sync-ServiceManifest {
    $manifest = Join-Path $ScriptDir 'services.manifest.json'
    if (-not (Test-Path $manifest)) { return }
    try {
        $entries = @((Get-Content $manifest -Raw | ConvertFrom-Json))
        $byName = @{}
        # Copy every field through. An earlier version rebuilt each entry from
        # a fixed list of properties, which silently dropped anything new —
        # `type` and `container` would have been erased on the next sync.
        foreach ($e in Read-Services) {
            if (-not $e.name) { continue }
            $byName[[string]$e.name] = $e
        }
        foreach ($e in $entries) {
            if (-not $e.name) { continue }
            $byName[[string]$e.name] = $e
        }
        @($byName.Values) | ConvertTo-Json -Depth 5 | Set-Content -Path $ServicesFile
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

        # Did it actually come up? Launching is not the same as listening, and
        # for a containerised service the launcher exits either way. Wait for
        # the port before claiming success.
        $up = $false
        if ($svcPort -gt 0) {
            for ($i = 0; $i -lt $SVC_PORT_GRACE_SECONDS; $i++) {
                Start-Sleep -Seconds 1
                if (Test-Port $svcPort) { $up = $true; break }
            }
        } else {
            $up = -not $p.HasExited
        }

        if ($up) {
            $script:svcFails[$name] = 0
            Write-Host "$(Get-Date -f 'HH:mm:ss')  service '$name' started (PID $($p.Id)) -> port $svcPort" -ForegroundColor Green
        } else {
            $script:svcFails[$name] = [int]$script:svcFails[$name] + 1
            $n = $script:svcFails[$name]
            $tail = ''
            foreach ($logPath in @($errl, $out)) {
                if (Test-Path $logPath) {
                    $lines = @(Get-Content $logPath -Tail 3 -ErrorAction SilentlyContinue |
                               Where-Object { $_ -and $_.Trim() })
                    if ($lines.Count) { $tail = ($lines -join ' | '); break }
                }
            }
            Write-Host "$(Get-Date -f 'HH:mm:ss')  service '$name' did not reach port $svcPort (attempt $n/$SVC_MAX_FAILS)" -ForegroundColor Yellow
            if ($tail) { Write-Host "      last output: $tail" -ForegroundColor DarkGray }
        }
    } catch {
        $script:svcFails[$name] = [int]$script:svcFails[$name] + 1
        Write-Host "$(Get-Date -f 'HH:mm:ss')  service '$name' failed to start: $($_.Exception.Message)" -ForegroundColor Red
    } finally {
        $env:PORT = $savedPort; $env:NEST_PORT = $savedNest
    }
}

# `docker start` returns once the container is RUNNING, which is not the same as
# ready. MySQL 8 spends a while on crash recovery before it listens, and a
# WordPress container started against a database that isn't accepting
# connections yet either exits or serves "Error establishing a database
# connection" until something restarts it. So wait on each dependency.
function Wait-ContainerReady($c, $timeoutSeconds = 120) {
    for ($i = 0; $i -lt $timeoutSeconds; $i++) {
        $state = (& docker inspect -f '{{.State.Status}}' $c 2>$null)
        if ($LASTEXITCODE -ne 0) { return $false }        # container is gone
        if ("$state".Trim() -ne 'running') { Start-Sleep -Seconds 1; continue }

        # Prefer the container's own healthcheck when it declares one.
        $health = (& docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{end}}' $c 2>$null)
        $health = "$health".Trim()
        if ($health) {
            if ($health -eq 'healthy')   { return $true }
            if ($health -eq 'unhealthy') { return $false }
            Start-Sleep -Seconds 1; continue
        }

        # No healthcheck declared. Probe for a database; anything else that is
        # running is taken as ready.
        $probe = & docker exec $c sh -c 'command -v mysqladmin >/dev/null 2>&1 && mysqladmin ping --silent 2>&1' 2>&1
        $probeText = "$probe"
        if ($LASTEXITCODE -eq 0)                   { return $true }   # answered the ping
        if (-not $probeText.Trim())                { return $true }   # no mysqladmin: not a DB
        # "Access denied" means it handshook with us — it is listening, which is
        # all WordPress needs from us here.
        if ($probeText -match 'Access denied')     { return $true }
        if ($probeText -match 'exec failed|not found|no such|OCI runtime') { return $true }
        Start-Sleep -Seconds 1
    }
    return $false
}

# Container-backed services. `docker start` is idempotent, so this is safe to
# call on an already-running container, and `docker update --restart
# unless-stopped` means Docker brings it back by itself after a reboot without
# this script being involved at all — belt and braces.
function Start-ContainerService($svc) {
    $name = [string]$svc.name
    $svcPort = 0; [void][int]::TryParse([string]$svc.port, [ref]$svcPort)
    $script:svcLastStart[$name] = Get-Date

    # A WordPress demo is usually a pair (the site plus its database), so a
    # service may name several containers. They start in the order listed.
    $containers = @()
    if ($svc.containers) { $containers = @($svc.containers | ForEach-Object { [string]$_ }) }
    elseif ($svc.container) { $containers = @([string]$svc.container) }
    else { $containers = @($name) }

    $last = $containers[-1]
    foreach ($c in $containers) {
        $out = & docker start $c 2>&1
        if ($LASTEXITCODE -ne 0) {
            $script:svcFails[$name] = [int]$script:svcFails[$name] + 1
            Write-Host "$(Get-Date -f 'HH:mm:ss')  container '$c' failed to start: $out" -ForegroundColor Red
            return
        }
        # Make Docker responsible for keeping it alive across reboots.
        & docker update --restart unless-stopped $c 2>&1 | Out-Null

        # Every container but the last is a dependency (the database). Let it
        # finish coming up before starting what needs it; the last one's
        # readiness is the port check below.
        if ($c -ne $last) {
            if (Wait-ContainerReady $c) {
                Write-Host "$(Get-Date -f 'HH:mm:ss')  dependency '$c' ready" -ForegroundColor DarkGray
            } else {
                $script:svcFails[$name] = [int]$script:svcFails[$name] + 1
                Write-Host "$(Get-Date -f 'HH:mm:ss')  dependency '$c' never became ready — not starting '$last' against it" -ForegroundColor Yellow
                $dlogs = & docker logs --tail 3 $c 2>&1
                if ($dlogs) { Write-Host "      docker logs ${c}: $($dlogs -join ' | ')" -ForegroundColor DarkGray }
                return
            }
        }
    }

    $up = $true
    if ($svcPort -gt 0) {
        $up = $false
        # WordPress needs longer than a Node process to answer its first request.
        for ($i = 0; $i -lt 30; $i++) {
            Start-Sleep -Seconds 1
            if (Test-Port $svcPort) { $up = $true; break }
        }
    }
    if ($up) {
        $script:svcFails[$name] = 0
        Write-Host "$(Get-Date -f 'HH:mm:ss')  service '$name' container(s) up -> port $svcPort" -ForegroundColor Green
    } else {
        $script:svcFails[$name] = [int]$script:svcFails[$name] + 1
        $n = $script:svcFails[$name]
        $logs = & docker logs --tail 3 $last 2>&1
        Write-Host "$(Get-Date -f 'HH:mm:ss')  service '$name' started but port $svcPort never opened (attempt $n/$SVC_MAX_FAILS)" -ForegroundColor Yellow
        if ($logs) { Write-Host "      docker logs: $($logs -join ' | ')" -ForegroundColor DarkGray }
    }
}

# Start anything in the manifest that is not already listening, backing off
# after each failure and parking the service once it's clearly not coming up.
function Ensure-Services {
    foreach ($svc in Read-Services) {
        if (-not $svc.name -or -not $svc.port) { continue }
        $name = [string]$svc.name

        if (Test-Port $svc.port) {
            # Recovered on its own (or someone started it by hand) — un-park it
            # so a later crash gets a fresh set of retries.
            if ($script:svcParked[$name]) {
                Write-Host "$(Get-Date -f 'HH:mm:ss')  service '$name' is up again; resuming supervision" -ForegroundColor Green
            }
            $script:svcFails[$name]  = 0
            $script:svcParked[$name] = $false
            continue
        }

        if ($script:svcParked[$name]) { continue }   # already gave up, said so once

        $fails = [int]$script:svcFails[$name]
        if ($fails -ge $SVC_MAX_FAILS) {
            $script:svcParked[$name] = $true
            Write-Host "$(Get-Date -f 'HH:mm:ss')  service '$name' gave up after $fails attempts — not retrying." -ForegroundColor Red
            Write-Host "      Check $DataDir\$name.err.log, then remove it from $ServicesFile or fix it and restart this script." -ForegroundColor DarkGray
            continue
        }

        # 20s, 40s, 80s ... capped. Quiet enough to read the log, quick enough
        # to recover from a genuine blip.
        $wait = [math]::Min($SVC_RETRY_BASE_SECONDS * [math]::Pow(2, $fails), $SVC_RETRY_MAX_SECONDS)
        $last = $script:svcLastStart[$name]
        if ($last -and ((Get-Date) - $last).TotalSeconds -lt $wait) { continue }

        if ([string]$svc.type -eq 'container') {
            # A slow or absent engine is the engine's problem, not this
            # service's — don't burn its retry budget waiting for Docker.
            if (-not (Ensure-DockerEngine)) { continue }
            Start-ContainerService $svc
        } else {
            Start-ManagedService $svc
        }
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
# Kick the engine early so it warms while Flask and the tunnel come up; the
# monitor loop starts the containers once it answers.
if (@(Read-Services | Where-Object { [string]$_.type -eq 'container' }).Count -gt 0) {
    [void](Ensure-DockerEngine)
}
Ensure-Services

# Say what is registered and where each one stands. The supervisor is silent
# when everything is already listening, which is correct but indistinguishable
# from it not running at all — as happened when a pulled launcher sat inert
# because PowerShell had the old script in memory.
$svcAll = @(Read-Services)
if ($svcAll.Count -eq 0) {
    Write-Host "  Managed services:         none registered" -ForegroundColor DarkGray
} else {
    Write-Host "  Managed services:" -ForegroundColor Green
    foreach ($s in $svcAll) {
        $kind = if ([string]$s.type -eq 'container') { 'container' } else { 'process' }
        if (Test-Port $s.port) {
            Write-Host ("    - {0,-24} {1,-10} port {2,-6} listening" -f $s.name, $kind, $s.port) -ForegroundColor Green
        } else {
            Write-Host ("    - {0,-24} {1,-10} port {2,-6} not up yet" -f $s.name, $kind, $s.port) -ForegroundColor Yellow
        }
    }
}

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

            # A change to THIS file needs more than a Flask restart. PowerShell
            # loads the script once; everything below is the version that was on
            # disk when the window opened, so pulling a new launcher and
            # restarting Flask leaves the old supervisor running and the new
            # code inert — silently, which is worse than failing.
            if ($changed -match 'run-server\.ps1') {
                # Don't hand the baton to a script that won't parse; a syntax
                # error here would take the server down with nothing left
                # running to notice.
                $parseErrors = $null
                [void][System.Management.Automation.Language.Parser]::ParseFile(
                    $PSCommandPath, [ref]$null, [ref]$parseErrors)
                if ($parseErrors -and $parseErrors.Count -gt 0) {
                    Write-Host "  run-server.ps1 changed but has $($parseErrors.Count) syntax error(s) — staying on the running version." -ForegroundColor Red
                    foreach ($pe in $parseErrors | Select-Object -First 3) {
                        Write-Host "      line $($pe.Extent.StartLineNumber): $($pe.Message)" -ForegroundColor DarkGray
                    }
                } else {
                    Write-Host "  run-server.ps1 changed -- relaunching the launcher itself..." -ForegroundColor Cyan
                    Stop-All
                    Start-Process powershell -ArgumentList @(
                        '-NoExit', '-ExecutionPolicy', 'Bypass',
                        '-File', "`"$PSCommandPath`"", '-Relaunched'
                    )
                    exit 0
                }
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
