# Job Scout — running it on the Surface and wegter-pc

Job Scout runs as two copies of mw-backend:

| | Surface | wegter-pc |
|---|---|---|
| `MW_ROLE` | `primary` | `ai-worker` |
| Serves api.michaelwegter.com (Cloudflare tunnel) | yes | **never** |
| Job Scout database, discovery, careers sweeps, schedulers | yes | no |
| AI: categorize companies, read careers pages, score jobs | no | yes (LM Studio, Gemma 4 12B) |
| Listens on | as today | 127.0.0.1 only |

The worker **pulls** work from the Surface over HTTPS, so wegter-pc needs no open ports, and it can be off, asleep
or gaming — tasks just queue until it's back. Until the AI has run, jobs show a rule-based fit score and companies a
keyword-based category ("Categorizing…" in the app).

> **Never run the tunnel on wegter-pc.** Cloudflare sends traffic to *every* `cloudflared` connected to a tunnel,
> so a second connector would split api.michaelwegter.com across two databases (random logouts, "missing" data).
> `run-server.ps1` skips the tunnel when `MW_ROLE=ai-worker`, `server.py` refuses to start as a worker while
> `cloudflared` is running, and — the simplest guard — **don't copy `~/.cloudflared` to wegter-pc**.

## Surface (primary)

Add to `mw-backend/.env`:

```ini
MW_ROLE=primary
MW_INSTANCE=surface
# Long random secret shared with wegter-pc:  python -c "import secrets; print(secrets.token_urlsafe(32))"
JOBS_WORKER_TOKEN=<secret>
# Who can use Job Scout (they sign in with their michaelwegter.com account)
JOBS_ALLOWED_EMAILS=zweetztuph@gmail.com,<ashley's email>
# Optional:
# JOBS_SWEEP_HOUR=2              # local hour for the nightly sweep
# GOOGLE_PLACES_API_KEY=...      # enables the "Google Places" discovery source
# ORS_API_KEY=...                # OpenRouteService key → drive-time minutes
```

Then `git pull` (the launcher's auto-deploy does this and reinstalls `requirements.txt`, which now lists `requests`).
Playwright is already installed for the SEO analyzer and Client Finder; Job Scout drives the installed Chrome
(`JOBS_BROWSER_CHANNEL=chrome`, the default) and falls back to Playwright's Chromium.

Check: `https://api.michaelwegter.com/jobs/health` → `{"role": "primary", ...}`.

## wegter-pc (ai-worker)

1. **LM Studio**: load `google/gemma-4-12b` (the QAT / 4-bit build fits the 12 GB RTX 3060), set context length to
   8192–16384, and start the local server (Developer tab → Start Server, port 1234, localhost only).
   Check: `curl http://localhost:1234/v1/models` lists the model — use that exact id for `JOBS_AI_MODEL`.
2. **mw-backend**: `git clone git@github.com:mwegter95/mw-backend.git`, create `venv` and
   `venv\Scripts\pip install -r requirements.txt` (same as the Surface; `server.py` imports every blueprint).
3. `.env`:

   ```ini
   MW_ROLE=ai-worker
   MW_INSTANCE=wegter-pc
   MW_PRIMARY_URL=https://api.michaelwegter.com
   JOBS_WORKER_TOKEN=<same secret as the Surface>
   JOBS_AI_API_BASE=http://localhost:1234/v1
   JOBS_AI_MODEL=google/gemma-4-12b
   # A port nothing else on this PC uses — the launcher kills whatever holds its port.
   PORT=5057
   LIFE_SCHEDULER=0
   ```
4. Start it with `run-server.ps1` like on the Surface. The banner shows `Role: ai-worker`, "Cloudflare Tunnel:
   skipped", and sleep/shutdown stay allowed. Auto-deploy keeps it on the same commit as the Surface.
   Check: `http://127.0.0.1:5057/jobs/health` → `{"role": "ai-worker", ...}`, and in Job Scout → Settings →
   Instances, wegter-pc shows **online** with LM Studio OK.

## First run

1. Sign in to michaelwegter.com → Apps → Job Scout. (Ashley: register on michaelwegter.com, then add her email to
   `JOBS_ALLOWED_EMAILS` on the Surface.)
2. **Settings → Profile**: home address, radius, target titles, salary floor, work styles, resume text, "want" and
   "avoid" notes, and **What to hunt for** (industries such as Plastics & Packaging and Building Materials, plus
   keywords like "injection molding", "precast concrete", "millwork").
3. **Companies → Discover companies**: preview the plan, start it, and watch the log. The app searches Google Maps,
   the web and OpenStreetMap town by town, categorizes every site it finds, then detects careers pages and pulls jobs.
   New companies keep flowing through as wegter-pc works the AI queue.
4. Nightly: companies still pending go through the pipeline, then every active company is swept. Discovery reruns on
   the 1st of each month with the profile's hunt settings.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| Instances shows wegter-pc **offline** | worker not running, wrong `MW_PRIMARY_URL`, or token mismatch (worker log: 401) |
| "LM Studio not OK" | server not started in LM Studio, or `JOBS_AI_MODEL` doesn't match `/v1/models` |
| "wegter-pc needs git pull" | protocol mismatch between the two copies — pull on wegter-pc (auto-deploy usually handles it) |
| Discovery: "maps/search skipped" | Playwright/Chrome missing on the Surface: `venv\Scripts\python -m playwright install chromium` |
| Discovery: OSM 406/504 | public Overpass server busy; the run continues with the other sources and the next run retries |
| A company shows "blocked" | its site refuses automated browsers; open its careers link from the company drawer |
| Worker exits with code 2 | `cloudflared` is running on wegter-pc — stop it (see the warning above) |
