# Inbox dashboard

A private, calmer way to read Gmail at **michaelwegter.com/apps/inbox**, wired into the Gmail triage Apps
Script (`~/Desktop/Projects/email-automate-google-script`). The script stays the only thing that touches Gmail.
This blueprint (`inbox_blueprint.py`, logic in `inbox_core.py`) keeps the dashboard's state and runs triage batches.

```
Browser (michaelwegter.com) ──JWT──▶ api.michaelwegter.com/inbox/* ──signed JSON──▶ Apps Script web app ──▶ Gmail
                                              ▲                                         │
                                              └────────── /inbox/ingest ◀── signed ─────┘  (batches applied from the Sheet)
```

## Setup (once)

1. **Apps Script.** Open the triage project at script.google.com.
   - Replace `email-automator.gs` with the repo's version, and add `dashboard-api.gs` as a new file.
   - Under Project Settings, tick "Show appsscript.json", then paste the repo's `appsscript.json`. It adds one scope, for calling the dashboard.
   - Run `setupDashboard()` once and approve the new permission. The execution log prints `GMAIL_SCRIPT_SECRET=…` and the web app URL.
   - Deploy → Manage deployments → ✏️ on the existing web app → Version: **New version** → Deploy. The `/exec` URL doesn't change.
2. **Server `.env`** (on the Surface, next to `server.py`):
   ```
   GMAIL_SCRIPT_URL=https://script.google.com/macros/s/…/exec
   GMAIL_SCRIPT_SECRET=<from setupDashboard()>
   ```
   Optional: `INBOX_OWNER_EMAIL` (default zweetztuph@gmail.com) and `INBOX_TZ` (default America/Chicago).
   `tzdata` was added to requirements.txt so local days work on Windows.
3. Push. `run-server.ps1` picks up the change, installs `tzdata` and restarts.
4. Open the dashboard → Settings → **Connection**. Both rows should be green. Then:
   - add family and close contacts;
   - paste the triage prompt you use with Claude if you'd rather keep it (`{{labels}}` and `{{people}}` are filled in for you);
   - **Import from the Rollup sheet** to bring in history.

## How sorting works

This server never calls an AI itself. Every path ends in the script's same apply core.

| Way | What happens |
|---|---|
| **Copy for Claude** (default) | `/triage/export` returns the prompt plus the same `---EMAIL-n---` blocks the Sheet export uses. Copy them into Claude, then paste Claude's JSON back; it goes to `/triage/apply` (fences, prose and curly quotes are tolerated). An optional review comes first. |
| **Sort on wegter-pc** | The browser takes the same export to LM Studio on wegter-pc, 8 emails per call (`inbox_local_ai.py` at `jobs.michaelwegter.com/jobs/local-ai`), asks it for the batch note, then posts everything to `/triage/apply` with `source: wegter-pc` (reviewed by default). The Surface never talks to wegter-pc, and LM Studio stays on localhost. |
| **The Sheet / old web page** | Unchanged. After each apply the script signs and POSTs the batch to `/inbox/ingest`. |

Each export remembers who sent what. That way the review shows sender and subject, and family and close contacts (Settings) are marked personal and kept in the inbox, whatever Claude or Gemma said.

### wegter-pc (optional)

`jobscout_server.py` now also serves `inbox_local_ai.py`, so wegter-pc picks it up on its next pull. It uses Job Scout's LM Studio settings (`JOBS_AI_API_BASE`, `JOBS_AI_MODEL`) and sign-in check, and only `INBOX_OWNER_EMAIL` may use it.

- Each call carries at most 12 emails and finishes in under 90 seconds, inside Cloudflare's 100-second limit.
- A strict JSON schema limits the model to the exact email ids it was sent.
- While a call is running, others get 409 and the page waits.
- If wegter-pc is asleep or LM Studio is closed, the dashboard says so and Copy for Claude still works.

## Read, unread and the daily counts

Triage marks most mail read in Gmail, so Gmail's read state can't tell whether you've actually looked at an email.
**Read** here means opened in the dashboard (`read_at`).

- **Highlights stay unread.** An important or personal email that triage keeps in the inbox is labeled but left unread in Gmail, so it stays bold until you open it here or mark it read (both mark it read in Gmail too). Archived highlights and everything else are marked read as before. The server decides this at the moment a batch is applied (after any review edits) and sends an explicit `keep_unread` for every email; Settings → "Keep highlights unread until you open them" turns it off. Sheet batches follow the same rule inside the script.
- The script remembers those threads and leaves them out of every export and the "waiting" count, so they aren't sorted again. New mail in the thread brings it back to be sorted. Sorted mail you mark unread here is remembered the same way.
- If you read a kept highlight on your phone, the next live-inbox refresh counts it as read here too.
- Mark unread clears `read_at` and marks the thread unread in Gmail.
- A row looks unread only while it's waiting on you: unopened and still in the inbox, a highlight, or unread in Gmail. Mail triage archived and marked read never looks unread, and label counts follow the same rule.

Daily counts come from `inbox_events` by local day: sorted, read (distinct emails), and archived/trashed.

## API (owner JWT unless noted)

| Method | Path | |
|---|---|---|
| GET | `/inbox/health` | public |
| GET | `/inbox/summary` | today, 14 days, highlight counts, labels, pins, latest/running batch, cached Gmail counts (no Apps Script call) |
| GET | `/inbox/messages?view=highlights\|recent\|sorted\|pinned\|inbox\|all&label=&q=&limit=&offset=` | `recent` = sorted in the last 7 days, newest sort first (`unread`, the old name, still answers) |
| GET | `/inbox/live?refresh=1` | Gmail's inbox right now, merged with dashboard state (cached 45 s) |
| GET | `/inbox/status?refresh=1` | inbox unread + waiting to sort (cached 60 s) |
| GET | `/inbox/thread/<id>?refresh=1` | full thread, cached 30 min, stale copy if Gmail is unreachable |
| POST | `/inbox/actions` `{ids, action, labels?}` | read, unread, pin, unpin, archive, inbox, star, unstar, trash, add_labels, remove_labels |
| GET/PUT | `/inbox/settings` | prompt, people, labels, batch_size, query, review_first, keep_people_in_inbox, keep_highlights_unread, block_images |
| GET | `/inbox/labels` | Gmail labels + labels used by triage |
| POST | `/inbox/triage/export` `{limit?}` | `text` to paste into Claude; `prompt` + `blocks` for wegter-pc |
| POST | `/inbox/triage/apply` `{json}` or `{actions, summary}`, `source`, `review?` | 202 + applying batch, or 201 + review batch |
| GET | `/inbox/triage/batches`, `/inbox/triage/batches/<id>` | |
| POST | `/inbox/triage/batches/<id>/apply` `{actions?, skip?}` / `…/discard` | finish a review |
| POST | `/inbox/import` | Rollup sheet → history (incremental) |
| GET | `/inbox/connection` | ping the script |
| POST | `/inbox/ingest` | **HMAC** from Apps Script: `X-Inbox-Timestamp`, `X-Inbox-Signature` = hex HMAC-SHA256(secret, ts + "." + body) |

Calls to the script are signed the same way (`{"ts","payload","sig"}`, five-minute window, replays refused). Both
sides send ASCII-only JSON, so signatures never depend on text encoding.

## Troubleshooting

When a Gmail call fails, the dashboard shows the reason in a red banner, and Settings → Connection shows it too.
Apps Script failures come back as **424**, never 502/504. Cloudflare replaces any 502/504 from the server with its
own page, which has no CORS headers, so the browser would only report "CORS error".

| Message | Fix |
|---|---|
| Dashboard API is off | Run `setupDashboard()` in the Apps Script editor. |
| Bad signature | `GMAIL_SCRIPT_SECRET` doesn't match the script's secret. Copy the `GMAIL_SCRIPT_SECRET=` line from `setupDashboard()` again and restart the server. |
| Google refused the request (HTTP 401/403) | The web app's "Who has access" isn't "Anyone". Deploy → Manage deployments → ✏️ → Who has access: Anyone → Deploy. To keep your existing link private, add a second deployment set to Anyone for the dashboard only, and put its URL in `GMAIL_SCRIPT_URL`. |
| GMAIL_SCRIPT_URL is the test URL / has an account segment | Use the deployment's Web app URL: `https://script.google.com/macros/s/…/exec`, with no `/dev` and no `/u/0/`. |
| Sent back a web page instead of JSON | The web app wasn't redeployed after adding `dashboard-api.gs` (Manage deployments → edit → New version), or its access isn't "Anyone". |
| Timestamp outside the allowed window | The server's clock is off by more than 5 minutes. |
| Couldn't reach Apps Script | Check `GMAIL_SCRIPT_URL` ends in `/exec`. |

## Tests

- `python -m pytest tests/test_inbox.py tests/test_inbox_local_ai.py`: the blueprint against a fake script, and the wegter-pc endpoint against a fake LM Studio.
- In the script repo, `node --test tests/run-tests.js` runs the real `.gs` files under Node with fake Gmail and Sheets.
- `node tests/gas-sim.js serve 8765` stands in for the deployed web app, for end-to-end runs.
