# Life Dashboard — Google Calendar + AI smart reminders

Connects a user's Google Calendar read-only, shows upcoming events in the
dashboard, and uses the GitHub Models API to turn upcoming calendar events into
smart dated reminders such as birthday gifts, travel prep, appointment paperwork,
holiday planning, and real deadlines. Generation runs on connect and then daily
via the in-process scheduler.

## Files
- `gh_models.py` — AI client (name kept for continuity). GitHub Models was retired
  2026-07-30; this now talks to the GitHub Copilot SDK by default, with an
  OpenAI-compatible HTTP path as an escape hatch. See "AI provider" below.
- `life_gcal.py` — Google OAuth and event listing; delegates task generation to
  `life_smart`.
- `life_smart.py` — smart-tasking engine: composes compact prompt, calls model,
  validates output, resolves dates/points from `LEAD`, dedups, caps, upserts, and
  logs drops.
- `life_skills/` — human-readable skill library: core contract, router, per-category
  skill files, and examples. See `life_skills/README.md`.
- `server.py` — routes, encrypted token storage, reminder upsert/prune, scheduler.

## Smart-tasking engine contract
The model only classifies each event and phrases up to 2 short task titles from a
closed set of `kind`s. The code owns mechanical decisions: reminder date, points,
enum validation, deduplication, stable idempotency keys, caps, and pruning. The
model must never emit dates, points, durations, markdown, or commentary.

## Recommended event normalization
Before calling the model, normalize each Google event into a compact record:
- `id`
- `title`
- `start` and `end`
- `allDay`
- `durationDays`
- `calendar` or calendar summary when available
- `location` when available
- short `description` snippet when useful
- recurrence/series key when available

This improves trip/holiday/social classification without shipping huge event
payloads.

## Multi-day policy
Do **not** classify every multi-day event as a trip. Multi-day is a strong signal
only when paired with destination/away/travel cues such as flights, hotels,
Airbnb, airport codes, vacation, camping, road trip, conference in another city,
or "in <place>". Generic multi-day OOO/PTO/busy blocks should usually be ignored.
Holiday, wedding, anniversary, and deadline cues should beat trip when that is
the event's primary meaning.

## Runtime prompt fit
At runtime, compile a compact prompt from:
- allowed categories and kinds from `LEAD`,
- router cues,
- the core hard rules,
- and 1–2 worked examples.

Do not concatenate every markdown file into every model call if that risks token
overflow. Keep the markdown skills and compact prompt builder in sync.

## Routes (all under the existing Life auth: JWT or device token)
- `GET  /api/life/ai/health` — verify the GitHub Models token works.
- `GET  /api/life/gcal/status` — `{configured, connected, email, last_generated_at, …}`.
- `GET  /api/life/gcal/connect` — returns `{auth_url}` to open top-level.
- `GET  /api/life/gcal/callback` — Google redirect target; stores the refresh token.
- `POST /api/life/gcal/disconnect`
- `GET  /api/life/gcal/events?days=90` — normalized upcoming events.
- `POST /api/life/smart-tasks/generate` — run generation now.

## AI provider

**GitHub Models was fully retired on July 30, 2026** — playground, catalog and
inference API all gone. Calls return `410 github_models_retirement_brownout`;
the word "brownout" is misleading, nothing comes back. Anything still setting
`GITHUB_MODELS_TOKEN` / `GITHUB_MODELS_API` / a publisher-prefixed
`LIFE_AI_MODEL` like `openai/gpt-4o-mini` is configured for a dead service.

`gh_models.py` is now provider-agnostic, selected with `LIFE_AI_PROVIDER`:

### `copilot` (default)

The official **GitHub Copilot SDK**, GA since June 2026 and covered by any
Copilot plan including Copilot Free. It drives the Copilot CLI runtime, which is
what makes it a supported path — unlike the reverse-engineered
`api.githubcopilot.com` proxies floating around, which risk your Copilot access.

Server setup:

```
pip install github-copilot-sdk        # Python 3.11+
python -m copilot download-runtime    # or have an authenticated `copilot` on PATH
copilot --version                     # sanity check
```

The CLI must be signed in to a GitHub account with Copilot. Leave
`LIFE_AI_GITHUB_TOKEN` unset and the SDK uses that logged-in identity — the
normal arrangement. Set it only if the server needs a different account.

Caveats worth knowing:

- **No JSON mode.** The Models API had `response_format: {"type":"json_object"}`;
  the SDK has no equivalent, so `gh_models` appends an explicit
  "raw JSON object only" instruction and `life_smart._parse_json` still strips
  code fences and does a repair round-trip. Expect slightly more repair retries.
- **No temperature / max_tokens.** Those arguments are accepted and ignored for
  this provider.
- **Startup cost.** Creating the client spawns the CLI runtime, so
  `gh_models` starts it once, lazily, on a dedicated event-loop thread and
  reuses it for the process lifetime. The first generation after a restart is
  slower than the rest.

### `openai`

Any OpenAI-compatible `/chat/completions` endpoint — OpenAI, Groq, OpenRouter,
Azure Foundry, a local Ollama. Set `LIFE_AI_PROVIDER=openai`,
`LIFE_AI_API_BASE`, `LIFE_AI_API_KEY` and `LIFE_AI_MODEL`. This path keeps JSON
mode, the 429/Retry-After handling and the reduced-parameter retry.

Either way, check it with `GET /api/life/ai/health`, which reports the provider,
the resolved model, and a one-word sample response.

## Environment variables
`start.sh` auto-loads `mw-backend/.env`, so add these there:

```
GOOGLE_CLIENT_ID=xxxxxxxx.apps.googleusercontent.com
GOOGLE_CLIENT_SECRET=xxxxxxxx
GOOGLE_REDIRECT_URI=https://api.michaelwegter.com/api/life/gcal/callback

# AI provider — copilot (default) or openai
LIFE_AI_PROVIDER=copilot
LIFE_AI_MODEL=auto
# LIFE_AI_GITHUB_TOKEN=      # optional; blank = use the Copilot CLI's login

# Only for LIFE_AI_PROVIDER=openai:
# LIFE_AI_API_BASE=https://api.groq.com/openai/v1
# LIFE_AI_API_KEY=xxxxxxxx

LIFE_DASHBOARD_URL=https://mwegter95.github.io/life-dashboard/
LIFE_SCHEDULER=1
```

**Delete any `GITHUB_MODELS_TOKEN` / `GITHUB_MODELS_API` lines** — they point at
a retired service. A leftover publisher-prefixed `LIFE_AI_MODEL` is handled
defensively (it logs a warning and falls back to `auto`) but should be fixed.

`auto` lets Copilot route to whatever it considers current, which is the right
default for a small classification job and survives catalog churn. Pin a
specific model (`gpt-5`, `claude-sonnet-4.5`, …) if you want predictable
latency.

## Google Cloud setup
1. Google Cloud Console → create/select project.
2. APIs & Services → Library → enable Google Calendar API.
3. OAuth consent screen → External → add yourself as a test user if still in Testing.
4. Credentials → Create OAuth client ID → Web application.
5. Authorized redirect URI: `https://api.michaelwegter.com/api/life/gcal/callback`.
6. Copy Client ID and Secret into `.env`.

## Deploy
```
git pull
./venv/Scripts/python -m pip install -r requirements.txt
# set .env
./start.sh --tunnel
```

Verify the AI health route and then run a manual smart-task generation from the
dashboard.

## When Google says `invalid_grant`

Symptom — the events route 502s and the log repeats:

```
[gcal] events fetch failed: Couldn't refresh Google Calendar credentials:
RefreshError: ('invalid_grant: Bad Request', {'error': 'invalid_grant', ...})
```

`invalid_grant` on a *refresh* means the stored refresh token is dead. Retrying
never helps; only a fresh OAuth consent does. The usual causes, most likely first
for a personal project:

1. **The OAuth consent screen is still in `Testing`.** Google expires every
   refresh token issued by a Testing-status app after **7 days**. This is the one
   that comes back every week or so. Fix it once: Google Cloud Console → APIs &
   Services → OAuth consent screen → **Publish app** (moves it to `In production`).
   Verification is *not* required for a personal app under 100 users — you get a
   one-time "Google hasn't verified this app" screen at consent that you click
   through via *Advanced → Go to …*, and the 7-day expiry goes away.
2. The Google account revoked the app at
   [myaccount.google.com/permissions](https://myaccount.google.com/permissions).
3. `GOOGLE_CLIENT_SECRET` was rotated in the Cloud Console but not in `.env`.
4. The account's password was changed (Google invalidates offline tokens).

After publishing, reconnect once from the dashboard to mint a non-expiring token.

**How the server handles it.** `life_gcal.CalendarAuthError` (a `CalendarFetchError`
with `needs_reauth = True`) is raised only for `invalid_grant`; everything else
stays a plain `CalendarFetchError`. The server then:

- sets `life_gcal_accounts.needs_reauth = 1` with the reason,
- answers `GET /api/life/gcal/events` with **409** and `{"needs_reauth": true}`
  instead of a 502 (a 502 reads as "the server broke"; this is user-actionable),
- reports `needs_reauth` from `GET /api/life/gcal/status`, which makes the
  dashboard's Calendar panel swap to a **Reconnect Google Calendar** card,
- **skips the account in the daily scheduler** so it stops hammering the token
  endpoint and filling the log every hour,
- clears the flag on the next successful fetch, and on a successful OAuth
  callback.

## Operational notes
- Store the OAuth refresh token encrypted.
- Smart reminders should be tagged `source: "gcal-ai"`.
- Re-runs should upsert by stable key and avoid duplicates.
- Preserve completed reminders; only prune future incomplete suggestions that are
  no longer relevant.
- Log model output drops and pre-filtered omissions separately so misses can be
  debugged.
- Keep volume modest: this workflow should remain free or extremely low-cost for
  personal use.
