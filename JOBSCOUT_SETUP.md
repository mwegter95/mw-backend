# Job Scout — setting up wegter-pc

All of Job Scout's backend runs on **wegter-pc**: the `/jobs` API, its database, employer discovery (Google
Maps / web search / OpenStreetMap through Playwright), careers-site sweeps, the nightly scheduler and the AI
(LM Studio, Gemma 4 12B). It runs `jobscout_server.py`, which loads only Job Scout, not the rest of mw-backend,
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
   **16384** (8192 also works). If the model has a **thinking** toggle, turn it **off**: Job Scout wants a
   short JSON answer, and thinking spends time and tokens on each of hundreds of requests. (Job Scout copes if
   it's left on, just slower.)
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

```ini
MW_ROLE=jobscout                 # run-server.ps1 starts jobscout_server.py instead of server.py
MW_INSTANCE=wegter-pc
PORT=5057                        # the tunnel's route points here

# Sign-in: tokens are checked against the Surface
JOBS_AUTH_URL=https://api.michaelwegter.com
JOBS_ALLOWED_EMAILS=zweetztuph@gmail.com,<ashley's email>

# AI: LM Studio on this PC
JOBS_AI_API_BASE=http://localhost:1234/v1
JOBS_AI_MODEL=google/gemma-4-12b-qat      # the exact id from /v1/models

# Optional
# JOBS_SWEEP_HOUR=2              # local hour for the nightly sweep
# GOOGLE_PLACES_API_KEY=...      # enables the "Google Places" discovery source
# ORS_API_KEY=...                # OpenRouteService key → drive-time minutes
```

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

1. Ashley: register on michaelwegter.com, then add her email to `JOBS_ALLOWED_EMAILS` in `.env` (the launcher
   picks it up on the next restart; closing and reopening the window is enough).
2. **Settings → Profile**: home address, radius, target titles, salary floor, work styles, resume text, "want"
   and "avoid" notes, and **What to hunt for** (e.g. Plastics & Packaging, Building Materials; keywords like
   "injection molding", "precast concrete", "millwork").
3. **Companies → Discover companies**: preview the plan, start it and watch the log. The app searches town by
   town, categorizes each company from its own website (a quick keyword pass, then Gemma), finds careers pages
   and pulls jobs. Gemma scores jobs as they arrive.
4. Nightly (around 2 AM, if the PC is awake): pending companies go through the pipeline, then every active
   company is swept. Discovery reruns on the 1st of each month with the profile's hunt settings.

## The Surface

Nothing to set up. It keeps serving api.michaelwegter.com exactly as before; Job Scout only asks it who is
signed in. It must be up for anyone to sign in, as it already must for the rest of the site.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| App says it can't reach Job Scout | wegter-pc off/asleep, launcher not running, or tunnel down (`Get-Service cloudflared`; dashboard shows the tunnel Down) |
| `jobs.michaelwegter.com` gives Cloudflare error 1033 / 502 | tunnel is up but the server isn't: check the launcher window, or `http://127.0.0.1:5057/jobs/health` |
| Everyone gets "auth_unavailable" | wegter-pc can't reach api.michaelwegter.com (Surface down or no internet) |
| "This account doesn't have access yet" | the email isn't in `JOBS_ALLOWED_EMAILS` (restart the launcher after editing `.env`) |
| Instances: LM Studio not responding | LM Studio server stopped, or `JOBS_AI_MODEL` doesn't match an id from `/v1/models` |
| AI tasks fail with "model returned no answer" | thinking is on and used the token budget: turn it off for the model |
| Discovery: "maps/search skipped" | Playwright's browser missing: `venv\Scripts\python -m playwright install chromium` |
| Discovery: OSM 406/504 | the public Overpass server is busy; the run continues with the other sources and the next run retries |
| A company shows "blocked" | its site refuses automated browsers; open its careers link from the company drawer |
