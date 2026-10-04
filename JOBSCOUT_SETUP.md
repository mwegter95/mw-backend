# Job Scout — setting up wegter-pc

All of Job Scout's backend runs on **wegter-pc**: the `/jobs` API, its database, employer discovery (Google
Maps / web search / OpenStreetMap through Playwright), reading careers sites, and the AI (LM Studio, Gemma 4
12B). Nothing runs on a timer: it works when someone presses **Find matches** in the app. It runs `jobscout_server.py`, which loads only Job Scout, not the rest of mw-backend,
and it's published at **https://jobs.michaelwegter.com** through a Cloudflare tunnel of its own.

The Surface is not involved except for sign-in: people log in with their michaelwegter.com account
(api.michaelwegter.com), and wegter-pc checks each token by asking the Surface's `/auth/me`
(`JOBS_AUTH_URL`). No signing key, database or tunnel credentials are copied between the machines.

```
browser (michaelwegter.com/apps/job-scout → GitHub Pages app)
   ├─ sign in ──────────► api.michaelwegter.com  (Surface, mw-backend tunnel)  — unchanged
   └─ everything else ──► jobs.michaelwegter.com (wegter-pc, "wegter-pc" tunnel)
                            jobscout_server.py :5057 ──► LM Studio :1234 (Gemma 4 12B)
                                     └── verifies tokens with api.michaelwegter.com/auth/me
```

When wegter-pc is off or asleep, Job Scout is offline (the app says so and offers demo data); the rest of
michaelwegter.com is unaffected. While the launcher runs it keeps the PC from sleeping.

## 1. Install the tools (PowerShell)

```powershell
winget install -e --id Git.Git
winget install -e --id GitHub.cli
winget install -e --id Python.Python.3.12
winget install -e --id Cloudflare.cloudflared
```
Then install **LM Studio** from https://lmstudio.ai (skip if it's already there). Open a new PowerShell
window afterwards so the new commands are on PATH.

## 2. Get mw-backend

```powershell
gh auth login                      # GitHub.com → HTTPS → log in with a browser (lets auto-deploy git pull)
cd $HOME\projects                  # anywhere you like
gh repo clone mwegter95/mw-backend
cd mw-backend
py -3.12 -m venv venv
venv\Scripts\pip install -r requirements.jobscout.txt
venv\Scripts\python -m playwright install chromium
```
`requirements.jobscout.txt` is only what Job Scout needs (Flask, requests, BeautifulSoup, Playwright…), not
the Surface's full `requirements.txt`. Playwright drives Chrome if it's installed and otherwise the Chromium
it just downloaded.

## 3. LM Studio

1. **Download the model**: in LM Studio search for **Gemma 4 12B** and download the **QAT** build
   (`google/gemma-4-12b-qat`). It's the version made to run at 4-bit, which fits the RTX 3060's 12 GB.
2. **Load settings** (the model's settings/gear in *My Models*): GPU offload **max**, context length
   **16384** (8192 also works), and **Reasoning off**. Gemma 4 thinks before it answers when Reasoning is on
   (LM Studio 0.4.17+ shows the toggle among the model's inference settings). Job Scout wants a short JSON
   answer, and the thinking spends a minute and thousands of tokens on each of hundreds of requests — often
   the whole budget, so the answer comes back empty or cut off. Job Scout retries those once with more room,
   but it's much faster with Reasoning off.
3. **Start the server**: Developer tab → start the server on port **1234**. Leave "serve on local network" off;
   only this PC needs it. Turn on **Just-in-time model loading** so a request loads the model by itself after
   a reboot.
4. **Keep it running**: Settings (Ctrl + ,) → enable **run the LLM server on login**, so LM Studio's server
   starts when you sign in to Windows (it minimizes to the tray).
5. Check it and note the exact model id:
   ```powershell
   curl.exe http://localhost:1234/v1/models
   ```
   Use the `id` it prints for your Gemma model as `JOBS_AI_MODEL` below.

## 4. `.env` in the mw-backend folder

Put each comment on its own line, as below. (The launcher now also copes with a comment after a value, but
an earlier version of this guide put comments there and the launcher read them as part of the value: that
turned `MW_ROLE` into an unknown role, so it started the Surface's full server, which asked for bcrypt and
Docker.)

```ini
# run-server.ps1 starts jobscout_server.py instead of server.py
MW_ROLE=jobscout
MW_INSTANCE=wegter-pc
# the tunnel's route points at this port
PORT=5057

# Sign-in: tokens are checked against the Surface
JOBS_AUTH_URL=https://api.michaelwegter.com
JOBS_ALLOWED_EMAILS=zweetztuph@gmail.com,ashley@example.com

# AI: LM Studio on this PC. JOBS_AI_MODEL is the exact id from /v1/models
JOBS_AI_API_BASE=http://localhost:1234/v1
JOBS_AI_MODEL=google/gemma-4-12b-qat

# Optional
# enables the "Google Places" discovery source
# GOOGLE_PLACES_API_KEY=...
# OpenRouteService key → drive-time minutes
# ORS_API_KEY=...
# 1 = also run automatically (nightly job reading at JOBS_SWEEP_HOUR, discovery on the 1st of the month).
# Off unless set: Job Scout runs when you press Find matches.
# JOBS_AUTO_RUNS=1
```
Replace `ashley@example.com` with the email Ashley registers with on michaelwegter.com.

## 5. Cloudflare tunnel (a new one, just for wegter-pc)

This is a **second, separate tunnel**. Don't reuse the Surface's `mw-backend` tunnel or copy its
`.cloudflared` folder: Cloudflare sends traffic to every machine connected to a tunnel, so that would split
api.michaelwegter.com between the two PCs.

1. Cloudflare dashboard → **Networking → Tunnels → Create a tunnel** → **Cloudflared**. Name it `wegter-pc`.
2. Choose **Windows**. The page shows an install command ending in a long token, of the form
   `cloudflared.exe service install <token>`. Run it in **PowerShell as Administrator**. This installs the
   tunnel as the `cloudflared` Windows service, so it starts with Windows on its own.
3. Back in the tunnel → **Routes → Add route → Published application**:
   - Subdomain `jobs`, Domain `michaelwegter.com`, Path empty
   - Service URL `http://localhost:5057`

   Cloudflare creates the `jobs.michaelwegter.com` DNS record itself.
4. The tunnel should show **Healthy** in the dashboard. (`Get-Service cloudflared` → Running.)

## 6. Start it, and start it at every login

```powershell
powershell -ExecutionPolicy Bypass -File run-server.ps1          # try it now
powershell -ExecutionPolicy Bypass -File setup-startup.ps1       # then: launch it whenever you sign in
```
The banner should show `Role: jobscout (jobscout_server.py)` and "Cloudflare Tunnel: cloudflared service
running". The launcher restarts the server if it crashes, pulls new commits from GitHub every 30 s
(auto-deploy, reinstalling `requirements.jobscout.txt` when it changes), and blocks sleep while it runs.

Check, in order:
- `http://127.0.0.1:5057/jobs/health` → `{"role": "jobscout", ...}` (the server)
- `https://jobs.michaelwegter.com/jobs/health` → the same JSON (the tunnel)
- michaelwegter.com → Apps → Job Scout → sign in → **Settings → Instances**: wegter-pc online, LM Studio
  responding

After a reboot everything comes back once you sign in to Windows: the tunnel service at boot, and LM Studio
and the launcher at login. If you'd like it up without anyone signing in, turn on automatic sign-in for that
Windows account.

## 7. First run

1. Ashley: register on michaelwegter.com, then put her email in `JOBS_ALLOWED_EMAILS` in `.env` (the launcher
   picks it up on the next restart; closing and reopening the window is enough).
2. **Settings → Profile** (each person has their own):
   - **What you want**, in your own words: the kind of work, team, industry, anything that matters. The AI
     reads this for every job it scores, along with the "avoid" notes and the resume text.
   - **Job categories** (Marketing, Communications, Sales, Operations…) and **target titles** ("Marketing
     Director", "Communications Manager"). These decide which jobs count as in your field. At least one is
     needed before any job can match.
   - **Levels** (Executive, Director, Manager, Lead, Individual contributor), or none for any level.
   - Home address, radius, salary floor, work styles.
   - **What to hunt for** (optional): kinds of employers to look for, as industries or keywords. Leave it
     empty to search every industry.
3. **Jobs → Find matches**. One run does everything, and the progress panel shows each step as it goes:
   searching for employers near home that aren't in the list yet, categorizing each from its own website,
   finding careers pages, reading open jobs everywhere, then Gemma scoring the jobs in your field ("12 of 40
   scored"). The first run takes a while (an hour or more over a wide radius); later runs only look at what's
   new. Press it again whenever you want fresh results.
4. **Companies → Discover companies** is still there for a search with different settings (other industries,
   keywords or sources) without reading jobs.

## The Surface

Nothing to set up. It keeps serving api.michaelwegter.com exactly as before; Job Scout only asks it who is
signed in. It must be up for anyone to sign in, as it already must for the rest of the site.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| Launcher asks for bcrypt, Docker or other Surface services | it isn't reading `MW_ROLE=jobscout`: check that line in `.env` (the banner must say `Role: jobscout`); pull the latest mw-backend |
| App says it can't reach Job Scout | wegter-pc off/asleep, launcher not running, or tunnel down (`Get-Service cloudflared`; dashboard shows the tunnel Down) |
| `jobs.michaelwegter.com` gives Cloudflare error 1033 / 502 | tunnel is up but the server isn't: check the launcher window, or `http://127.0.0.1:5057/jobs/health` |
| Everyone gets "auth_unavailable" | wegter-pc can't reach api.michaelwegter.com (Surface down or no internet) |
| "This account doesn't have access yet" | the email isn't in `JOBS_ALLOWED_EMAILS` (restart the launcher after editing `.env`) |
| Instances: LM Studio not responding | LM Studio server stopped, or `JOBS_AI_MODEL` doesn't match an id from `/v1/models` |
| AI tasks fail with "model returned no answer", "answer cut off" or "Unterminated string" | Reasoning is on for the model in LM Studio and the thinking used the token budget: turn it off, then restart the launcher (tasks that failed this way are retried at startup) |
| Find matches reads no jobs | no profile has job categories or target titles yet (the run log says so) |
| Find matches says another run is going | only one search runs at a time; the progress panel shows the one in progress |
| Discovery: "maps/search skipped" | Playwright's browser missing: `venv\Scripts\python -m playwright install chromium` |
| Discovery: OSM 406/504 | the public Overpass server is busy; the run continues with the other sources and the next Find matches retries |
| A company shows "blocked" | its site refuses automated browsers; open its careers link from the company drawer |
