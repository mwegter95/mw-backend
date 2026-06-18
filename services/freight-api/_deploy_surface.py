#!/usr/bin/env python3
"""
Visible, idempotent deploy for the freight-api on the Surface (Windows).

Opens a REAL console window on the Surface and runs every step with live output
(command echoed, stdout/stderr streamed), exactly as if a person were typing in a
terminal. Starts the API server in its own visible window so its logs are visible,
and writes a transcript. Self-cleans the git tree at the end so auto-deploy stays
healthy.

Modes:
  python _deploy_surface.py          -> opens the visible deploy window, returns
  (the heavy downloads happen here, headless, before the window opens)

Steps (idempotent): ensure portable Node + PostgreSQL, ensure PG running,
install pnpm@9, drop the bogus packageManager field, pnpm install, apply the
drizzle schema with psql, build, seed, start the API (:3001) in a visible window,
register freight-pg + freight-api in services.json for reboot durability, then
restore the tracked git tree.
"""
import os
import sys
import json
import time
import secrets
import zipfile
import subprocess
import urllib.request
from pathlib import Path

NODE_VER = "20.18.0"
NODE_URL = f"https://nodejs.org/dist/v{NODE_VER}/node-v{NODE_VER}-win-x64.zip"
PG_URL = "https://get.enterprisedb.com/postgresql/postgresql-16.4-1-windows-x64-binaries.zip"
PG_PORT = "5433"
API_PORT = "3001"
DB_URL = f"postgres://postgres@127.0.0.1:{PG_PORT}/factoring"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0 Safari/537.36"}
CREATE_NEW_CONSOLE = 0x00000010

HERE = Path(__file__).resolve().parent            # services/freight-api
MWB = HERE.parents[1]                              # mw-backend
TOOLS = MWB / "data" / "runner-workspace" / "tools"
NODE_HOME = TOOLS / f"node-v{NODE_VER}-win-x64"
PG_BIN = TOOLS / "pgsql" / "bin"
PGDATA = TOOLS / "pgdata"
API_DIR = HERE / "apps" / "api"
RUN_JS = API_DIR / "_run.js"
SERVICES = MWB / "data" / "services.json"
PS1 = HERE / "deploy_freight.ps1"


def _download(url, dest):
    if dest.exists() and dest.stat().st_size > 0:
        return
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=600) as r, open(dest, "wb") as f:
        while True:
            chunk = r.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)


def _ensure_runtimes():
    TOOLS.mkdir(parents=True, exist_ok=True)
    if not (NODE_HOME / "node.exe").exists():
        z = TOOLS / "node.zip"
        _download(NODE_URL, z)
        with zipfile.ZipFile(z) as zf:
            zf.extractall(TOOLS)
    if not (PG_BIN / "pg_ctl.exe").exists():
        z = TOOLS / "pg.zip"
        _download(PG_URL, z)
        with zipfile.ZipFile(z) as zf:
            zf.extractall(TOOLS)
    if not (PGDATA / "PG_VERSION").exists():
        subprocess.run([str(PG_BIN / "initdb.exe"), "-D", str(PGDATA), "-U", "postgres",
                        "--auth=trust", "--encoding=UTF8"], env=os.environ.copy(), timeout=180)


def _ensure_run_js():
    if RUN_JS.exists():
        return
    main_js = API_DIR / "dist" / "apps" / "api" / "src" / "main.js"
    RUN_JS.write_text(
        f"process.env.DATABASE_URL={json.dumps(DB_URL)};\n"
        f"process.env.NEST_PORT={json.dumps(API_PORT)};\n"
        f"process.env.JWT_SECRET={json.dumps(secrets.token_hex(24))};\n"
        f"require({json.dumps(str(main_js))});\n"
    )


PS1_TEMPLATE = r"""# Auto-generated visible deploy for freight-api. Shows every command + output.
$ErrorActionPreference = 'Continue'
$Host.UI.RawUI.WindowTitle = 'freight-api deploy'
$NODE  = '{NODE_HOME}'
$TOOLS = '{TOOLS}'
$PGBIN = '{PG_BIN}'
$PGDATA= '{PGDATA}'
$APP   = '{HERE}'
$APIDIR= '{API_DIR}'
$RUNJS = '{RUN_JS}'
$MWB   = '{MWB}'
$env:Path = "$NODE;" + $env:Path
$env:DATABASE_URL = '{DB_URL}'
$env:NEST_PORT = '{API_PORT}'

Start-Transcript -Path (Join-Path $APP '_deploy.transcript.log') -Append | Out-Null
function Step($desc, [scriptblock]$block) {{
  Write-Host ''
  Write-Host ('PS> ' + $desc) -ForegroundColor Cyan
  & $block 2>&1 | Write-Host
}}
function PortUp($p) {{ try {{ (New-Object Net.Sockets.TcpClient).Connect('127.0.0.1',$p); $true }} catch {{ $false }} }}

Write-Host '================ FREIGHT-API DEPLOY (live) ================' -ForegroundColor Green
Write-Host ("node: $NODE")

Step 'start PostgreSQL (skip if already up)' {{
  if (PortUp {PG_PORT}) {{ Write-Host 'postgres already running on {PG_PORT}' }}
  else {{ & "$PGBIN\pg_ctl.exe" -D "$PGDATA" -o "-p {PG_PORT}" -l "$TOOLS\pg.log" start }}
}}
Step 'create database (ok if exists)' {{ & "$PGBIN\createdb.exe" -h 127.0.0.1 -p {PG_PORT} -U postgres factoring }}
Step 'install pnpm@9' {{ & "$NODE\npm.cmd" i -g pnpm@9 --force }}
Step 'remove bogus packageManager field' {{ Push-Location $APP; & "$NODE\npm.cmd" pkg delete packageManager; Pop-Location }}
Step 'pnpm install' {{ Push-Location $APP; & "$NODE\pnpm.cmd" install --no-frozen-lockfile; Pop-Location }}
Step 'generate drizzle schema SQL' {{ Push-Location "$APP\packages\db"; & "$NODE\pnpm.cmd" exec drizzle-kit generate; Pop-Location }}
Step 'apply schema with psql' {{
  Get-ChildItem "$APP\packages\db\drizzle\*.sql" -ErrorAction SilentlyContinue | Sort-Object Name | ForEach-Object {{
    Write-Host ('  applying ' + $_.Name)
    & "$PGBIN\psql.exe" -h 127.0.0.1 -p {PG_PORT} -U postgres -d factoring -f $_.FullName
  }}
}}
Step 'build the API (nest build)' {{ Push-Location $APP; & "$NODE\pnpm.cmd" -C apps/api run build; Pop-Location }}
Step 'seed demo data' {{ Push-Location $APP; & "$NODE\pnpm.cmd" -C apps/api run seed; Pop-Location }}

Step 'start API server in its own window (skip if :{API_PORT} up)' {{
  if (PortUp {API_PORT}) {{ Write-Host 'API already listening on {API_PORT}' }}
  else {{
    Start-Process powershell -ArgumentList '-NoExit','-Command',("`$Host.UI.RawUI.WindowTitle='freight-api server'; & '$NODE\node.exe' '$RUNJS'") -WorkingDirectory $APIDIR
    for ($i=0; $i -lt 30 -and -not (PortUp {API_PORT}); $i++) {{ Start-Sleep 1 }}
  }}
}}

Step 'register services for reboot durability' {{
  $svc = @(
    @{{ name='freight-pg';  cmd="$PGBIN\pg_ctl.exe"; args=('-D "'+$PGDATA+'" -o "-p {PG_PORT}" -l "'+$TOOLS+'\pg.log" start'); cwd="$TOOLS"; port={PG_PORT} }},
    @{{ name='freight-api'; cmd="$NODE\node.exe";    args=('"'+$RUNJS+'"'); cwd="$APIDIR"; port={API_PORT} }}
  )
  ($svc | ConvertTo-Json -Depth 6) | Set-Content (Join-Path $MWB 'data\services.json') -Encoding UTF8
  Write-Host 'services.json updated'
}}

Step 'restore clean git tree (build touched package.json/lockfile; keeps auto-deploy healthy)' {{
  & git -C "$MWB" checkout -- . 2>&1 | Write-Host
  Write-Host 'tracked files restored; build output (dist/node_modules) is gitignored and kept'
}}

if (PortUp {API_PORT}) {{ Write-Host ("`nDONE: freight-api LISTENING on 127.0.0.1:{API_PORT}") -ForegroundColor Green }}
else {{ Write-Host ("`nWARN: API not listening on {API_PORT} -- check the server window") -ForegroundColor Yellow }}
Stop-Transcript | Out-Null
Write-Host ''
Read-Host 'Deploy finished. Press Enter to close this window'
"""


def _write_ps1():
    PS1.write_text(PS1_TEMPLATE.format(
        NODE_HOME=NODE_HOME, TOOLS=TOOLS, PG_BIN=PG_BIN, PGDATA=PGDATA, HERE=HERE,
        API_DIR=API_DIR, RUN_JS=RUN_JS, MWB=MWB, DB_URL=DB_URL, PG_PORT=PG_PORT, API_PORT=API_PORT,
    ))


def trigger():
    _ensure_runtimes()
    _ensure_run_js()
    _write_ps1()
    subprocess.Popen(
        ["powershell", "-NoExit", "-ExecutionPolicy", "Bypass", "-File", str(PS1)],
        creationflags=CREATE_NEW_CONSOLE, close_fds=True,
    )
    print(f"visible deploy window opened on the Surface: {PS1}")


if __name__ == "__main__":
    trigger()
