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

## Full deployment guide

See `DEPLOYMENT.md` for first-time setup (venv, `.env`, Cloudflare Tunnel).
