# mw-backend

Flask API for michaelwegter.com's apps and demos. Runs on the Surface Pro and is
exposed to the internet at `https://api.michaelwegter.com` via a Cloudflare
Tunnel. The frontend is GitHub Pages (auto-deploys on push); this backend
auto-deploys on push too (see below).

## Start the server (on the Surface)

```powershell
powershell -ExecutionPolicy Bypass -File run-server.ps1
```

This launches the waitress server (`server.py`, port 5050) and the Cloudflare
tunnel, blocks sleep/shutdown while running, and auto-restarts Flask if it
crashes. Health check: `https://api.michaelwegter.com/health`.

Run it automatically at login (one time):

```powershell
powershell -ExecutionPolicy Bypass -File setup-startup.ps1
```

## Auto-deploy on every push

Auto-deploy is **built into `run-server.ps1`** — there is nothing extra to run.
Its monitor loop polls the git remote every 30 seconds; when it sees a new commit
on the current branch it pulls (`--ff-only`), installs deps if `requirements.txt`
changed, and restarts the server **in the same window** (it replaces the Python
child process; the window and the Cloudflare tunnel stay up). You see the
`pulling... / Restarting... / Deploy complete.` lines right in that window. So
your flow is: push from your Mac, and the Surface is live within ~30 seconds.

Change the interval (optional):

```powershell
$env:AUTO_DEPLOY_SECONDS = 15 ; powershell -ExecutionPolicy Bypass -File run-server.ps1
```

If you set up the old standalone watcher task earlier, remove it once:

```powershell
Unregister-ScheduledTask -TaskName 'mw-backend-autodeploy' -Confirm:$false
```

Notes:
- Pulls are `--ff-only`, so local commits on the Surface block the pull; keep the
  Surface checkout clean (it should only ever pull).
- `.env` and `data/` are gitignored and are not touched by deploys.
- Requires `git` on PATH (it is, since you clone/pull there).

## Adding a feature (blueprint)

Create `<feature>_blueprint.py` with a Flask Blueprint, register it in
`server.py`, and keep CORS open for the site origin. Mirror
`spotify_blueprint.py` / `apple_music_blueprint.py`. Demos on the site call this
API at `https://api.michaelwegter.com/<prefix>/...`.

To expose a non-Flask service (e.g. a Node app the runner started on loopback),
copy `bridge_blueprint_template.py` to `<feature>_blueprint.py`, set its `PREFIX`
and `UPSTREAM`, and register it the same way — it reverse-proxies `/<prefix>/*`
to the local service so it inherits the tunnel and CORS.

Long-running services are listed in `data/services.json` (gitignored), e.g.
`{ "name", "cmd", "args", "cwd", "port" }`. `run-server.ps1` is their sole
launcher: each monitor tick it starts any listed service whose port isn't open,
so they survive crashes and reboots. The workflow registers them via
`surface_register_service.py`; `cwd` is relative to this folder (or absolute) and
service stdout/stderr go to `data/<name>.log` / `.err.log`.

## Remote runner ( /run/exec ) — powerful, handle with care

`runner_blueprint.py` lets an authenticated caller run a python or bash script on
the Surface and get back stdout/stderr/exit code. This is what lets the Upwork
workflow build and deploy REAL backends here (install Node, build a service,
start it, bridge it through Flask) instead of mocking.

**This is remote code execution on this machine.** It is OFF unless both of these
are in `.env`:

```env
RUN_ENDPOINT_ENABLED=1
RUN_SECRET=<32+ char secret>     # python3 -c "import secrets; print(secrets.token_hex(32))"
```

Requests are authenticated with an HMAC-SHA256 signature over
`"<timestamp>.<body>"` using `RUN_SECRET`, with a 60-second timestamp window
(replay protection); the secret never goes over the wire. Every call is recorded
in `data/runner-audit.log` (hash of the script, exit code, duration, IP, never
the secret or script body).

Treat `RUN_SECRET` like an SSH key. Because `/run` is reachable through the public
tunnel, strongly consider adding **Cloudflare Access** in front of the `/run`
path as a second factor. Leave `RUN_ENDPOINT_ENABLED` unset when you do not need
it. The same secret goes in the Upwork workflow's `.env` (`RUN_SECRET`), which
calls this via `scripts/surface_run.py`.

## Full deployment guide

See `DEPLOYMENT.md` for first-time setup (venv, `.env`, Cloudflare Tunnel).
