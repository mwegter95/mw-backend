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

`auto-deploy.ps1` watches the git remote. When you push a new commit, it pulls,
installs deps if `requirements.txt` changed, and restarts the server (it kills
`server.py`; `run-server.ps1`'s monitor relaunches it with the new code, the
tunnel stays up). So your flow is just: push from your Mac, and the Surface is
live within ~30 seconds.

Run it once at login alongside the server (one time):

```powershell
powershell -ExecutionPolicy Bypass -File setup-autodeploy.ps1
```

Or run it manually in a window:

```powershell
powershell -ExecutionPolicy Bypass -File auto-deploy.ps1            # polls every 30s
powershell -ExecutionPolicy Bypass -File auto-deploy.ps1 -IntervalSeconds 15
```

After running both setup scripts once, a login starts the server AND the
auto-deploy watcher. To stop auto-deploying:

```powershell
powershell -ExecutionPolicy Bypass -File setup-autodeploy.ps1 -Remove
```

Notes:
- `auto-deploy.ps1` relies on `run-server.ps1` being up to relaunch the killed
  server (it starts one as a fallback if none is found).
- Pulls are `--ff-only`, so local commits on the Surface block the pull; keep the
  Surface checkout clean (it should only ever pull).
- `.env` and `data/` are gitignored and are not touched by deploys.

## Adding a feature (blueprint)

Create `<feature>_blueprint.py` with a Flask Blueprint, register it in
`server.py`, and keep CORS open for the site origin. Mirror
`spotify_blueprint.py` / `apple_music_blueprint.py`. Demos on the site call this
API at `https://api.michaelwegter.com/<prefix>/...`.

## Full deployment guide

See `DEPLOYMENT.md` for first-time setup (venv, `.env`, Cloudflare Tunnel).
