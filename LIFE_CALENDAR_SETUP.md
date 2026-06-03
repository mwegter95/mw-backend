# Life Dashboard — Google Calendar + AI smart reminders

Connects a user's Google Calendar read-only, shows upcoming events in the
dashboard, and uses the GitHub Models API to turn upcoming calendar events into
smart dated reminders such as birthday gifts, travel prep, appointment paperwork,
holiday planning, and real deadlines. Generation runs on connect and then daily
via the in-process scheduler.

## Files
- `gh_models.py` — GitHub Models client: token resolution, chat call, model-specific
  request handling, and JSON/transport fallback behavior.
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

## Environment variables
`start.sh` auto-loads `mw-backend/.env`, so add these there:

```
GOOGLE_CLIENT_ID=xxxxxxxx.apps.googleusercontent.com
GOOGLE_CLIENT_SECRET=xxxxxxxx
GOOGLE_REDIRECT_URI=https://api.michaelwegter.com/api/life/gcal/callback
GITHUB_MODELS_TOKEN=ghp_or_fine_grained_pat_with_models_access
LIFE_AI_MODEL=openai/gpt-4o-mini
GITHUB_MODELS_API=https://models.github.ai/inference
LIFE_DASHBOARD_URL=https://mwegter95.github.io/life-dashboard/
LIFE_SCHEDULER=1
```

Use whichever model your token can call reliably. Prefer a model that returns
strict JSON consistently for this small classification task.

## Google Cloud setup
1. Google Cloud Console → create/select project.
2. APIs & Services → Library → enable Google Calendar API.
3. OAuth consent screen → External → add yourself as a test user if still in Testing.
4. Credentials → Create OAuth client ID → Web application.
5. Authorized redirect URI: `https://api.michaelwegter.com/api/life/gcal/callback`.
6. Copy Client ID and Secret into `.env`.

## GitHub token
The GitHub Models API needs a token with Models access. Put it in
`GITHUB_MODELS_TOKEN`. Use a fine-grained PAT with Models read access or a classic
PAT that works for your account. Verify callable model ids with the GitHub Models
catalog endpoint, then set `LIFE_AI_MODEL` to a supported id.

## Deploy
```
git pull
./venv/Scripts/python -m pip install -r requirements.txt
# set .env
./start.sh --tunnel
```

Verify the AI health route and then run a manual smart-task generation from the
dashboard.

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
