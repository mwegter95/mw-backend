# Life Dashboard — Google Calendar + AI smart reminders

Connects a user's Google Calendar (read-only), shows upcoming events in the
dashboard, and uses the **GitHub Models API** (same approach as `code-genius`)
to turn the next ~3 months of events into **smart dated reminders** (e.g. a
birthday → "buy a gift" a week before). Generation runs on connect and then
daily via an in-process scheduler.

## Files
- `gh_models.py` — GitHub Models client (token resolution + chat, GPT-5-family aware). Port of code-genius `llm.ts`.
- `life_gcal.py` — Google OAuth + event listing (delegates task generation to `life_smart`).
- `life_smart.py` — the smart-tasking engine: composes the skill library, calls the model, and deterministically resolves dates/points/validation.
- `life_skills/` — the skill library (core contract + router/triage + one skill per category + examples). See `life_skills/README.md`.
- `server.py` — routes, encrypted token storage, reminder upsert/prune, scheduler.

## Smart-tasking engine (tuned for GPT-5.4 mini)
The model only **classifies** each event (`category`) and **phrases** ≤2 short
task titles from a closed set of `kind`s. The code owns everything mechanical —
the reminder **date** (lead-time table in `life_smart.LEAD`), **points**, enum
validation, dedup, and caps — so the model literally can't emit a broken date or
runaway output. To tweak behavior, edit the markdown skills in `life_skills/`
and/or the `LEAD` table; see `life_skills/README.md` to add a category.

## Routes (all under the existing Life auth: JWT or device token)
- `GET  /api/life/ai/health` — verify the GitHub Models token works (use on the Surface).
- `GET  /api/life/gcal/status` — `{configured, connected, email, last_generated_at, …}`.
- `GET  /api/life/gcal/connect` — returns `{auth_url}` to open (top-level, not in an iframe).
- `GET  /api/life/gcal/callback` — Google redirect target; stores the refresh token, redirects back to the dashboard.
- `POST /api/life/gcal/disconnect`
- `GET  /api/life/gcal/events?days=90` — normalized upcoming events.
- `POST /api/life/smart-tasks/generate` — run generation now.

## Environment variables (set on the Surface Pro 3)
`start.sh` auto-loads `mw-backend/.env`, so the simplest path is to add these
to a `.env` file there:

```
GOOGLE_CLIENT_ID=xxxxxxxx.apps.googleusercontent.com
GOOGLE_CLIENT_SECRET=xxxxxxxx
GOOGLE_REDIRECT_URI=https://api.michaelwegter.com/api/life/gcal/callback
GITHUB_MODELS_TOKEN=ghp_or_fine_grained_pat_with_models_access
# optional (defaults to gpt-5.4-mini; set to the exact GitHub Models id if different):
LIFE_AI_MODEL=gpt-5.4-mini
LIFE_DASHBOARD_URL=https://mwegter95.github.io/life-dashboard/
LIFE_SCHEDULER=1            # set 0 to disable the daily auto-generation
```

The GitHub token is resolved env-var-first, then `gh auth token`, then Copilot's
`apps.json` — env var is the reliable choice on Windows.

## Google Cloud setup (one-time)
1. **console.cloud.google.com** → new project.
2. **APIs & Services → Library → Google Calendar API → Enable**.
3. **OAuth consent screen** → External → add yourself under **Test users**; leave in **Testing** mode (lets the sensitive `calendar.readonly` scope work for test users without full verification).
4. **Credentials → Create OAuth client ID → Web application** → Authorized redirect URI = `https://api.michaelwegter.com/api/life/gcal/callback`.
5. Copy the Client ID + Secret into `.env`.

## GitHub token (for the AI)
The GitHub Models API needs a token with **Models** access. Put it in
`GITHUB_MODELS_TOKEN`. Two ways:

**A. Fine-grained PAT (scoped):** github.com/settings/personal-access-tokens/new
1. **Resource owner = your personal account** (the Models permission only shows
   when you own the resource — an org won't show it).
2. Scroll to **Permissions → Account permissions** (BELOW Repository permissions).
3. **Models → Access: Read-only** (this is the `models:read` permission, required
   since May 2025 — without it the API returns 401 "The `models` permission is
   required").
4. Generate, copy the `github_pat_…` token.

**B. Classic PAT (simplest):** github.com/settings/tokens/new — classic tokens are
exempt from the models:read requirement and work as-is. No scope needed for Models;
name it, generate, copy the `ghp_…` token.

## Deploy on the Surface
```
git pull
./venv/Scripts/python -m pip install -r requirements.txt   # adds google-* libs
# (set .env as above)
./start.sh --tunnel
```
Then verify: `curl https://api.michaelwegter.com/api/life/ai/health` with an auth
header should return `{"available": true, ...}`.

## Notes
- The OAuth refresh token is stored **encrypted** (AES-GCM via the server secret).
- Smart reminders are regular dated reminders tagged `source: "gcal-ai"`. Re-runs
  upsert by a stable key (no duplicates) and prune future suggestions that are no
  longer relevant **and** that you haven't checked off.
- Cost at this volume (≤100 Calendar calls/month, ~1 AI run/day) is **$0** — both
  the Calendar API and GitHub Models are free at this scale.
