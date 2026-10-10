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
   - paste your ChatGPT prompt if you'd rather keep it (`{{labels}}` and `{{people}}` are filled in for you);
   - **Import from the Rollup sheet** to bring in history.

## How sorting works

| Way | What happens |
|---|---|
| **Sort with AI** | `export` (next N unread, scrubbed like the Sheet export) → `gh_models.chat_completion` with the prompt → optional review (edit labels, importance, keep in inbox, skip) → `apply`. Runs in a background thread; the page polls `/triage/batches/<id>`. |
| **Use ChatGPT** | `/triage/export` returns the prompt plus the same `---EMAIL-n---` blocks to copy. Paste ChatGPT's reply into `/triage/apply` (fences, prose and curly quotes are tolerated). |
| **The Sheet / old web page** | Unchanged. After each apply the script signs and POSTs the batch to `/inbox/ingest`. |
| **Automatic** | Settings → "Sort automatically with AI" every N hours (no review). |

Family and close contacts (Settings) are always marked personal and, by default, never archived, whatever the model says.

## Read, unread and the daily counts

Triage marks mail read in Gmail, so Gmail's read state can't tell whether you've actually looked at an email.
**Read** here means opened in the dashboard (`read_at`).

- Mark unread clears it and also marks the thread unread in Gmail.
- If triage left a thread unread ("Leave important mail unread in Gmail") and you then read it on your phone, the next live-inbox refresh counts it as read.

Daily counts come from `inbox_events` by local day: sorted, read (distinct emails), and archived/trashed.

## API (owner JWT unless noted)

| Method | Path | |
|---|---|---|
| GET | `/inbox/health` | public |
| GET | `/inbox/summary` | today, 14 days, highlight counts, labels, pins, latest/running batch, cached Gmail counts (no Apps Script call) |
| GET | `/inbox/messages?view=highlights\|unread\|sorted\|pinned\|inbox\|all&label=&q=&limit=&offset=` | |
| GET | `/inbox/live?refresh=1` | Gmail's inbox right now, merged with dashboard state (cached 45 s) |
| GET | `/inbox/status?refresh=1` | inbox unread + waiting to sort (cached 60 s) |
| GET | `/inbox/thread/<id>?refresh=1` | full thread, cached 30 min, stale copy if Gmail is unreachable |
| POST | `/inbox/actions` `{ids, action, labels?}` | read, unread, pin, unpin, archive, inbox, star, unstar, trash, add_labels, remove_labels |
| GET/PUT | `/inbox/settings` | prompt, people, labels, batch_size, query, review_first, keep_people_in_inbox, keep_important_unread, block_images, auto_sort_hours |
| GET | `/inbox/labels` | Gmail labels + labels used by triage |
| POST | `/inbox/triage/run` `{limit?, review?}` | AI sort → 202 + batch |
| POST | `/inbox/triage/export` `{limit?}` | ChatGPT text |
| POST | `/inbox/triage/apply` `{json}` | apply pasted ChatGPT JSON → 202 + batch |
| GET | `/inbox/triage/batches`, `/inbox/triage/batches/<id>` | |
| POST | `/inbox/triage/batches/<id>/apply` `{actions?, skip?}` / `…/discard` | finish a review |
| POST | `/inbox/import` | Rollup sheet → history (incremental) |
| GET | `/inbox/connection?ai=1` | ping the script (+ AI provider health) |
| POST | `/inbox/ingest` | **HMAC** from Apps Script: `X-Inbox-Timestamp`, `X-Inbox-Signature` = hex HMAC-SHA256(secret, ts + "." + body) |

Calls to the script are signed the same way (`{"ts","payload","sig"}`, five-minute window, replays refused). Both
sides send ASCII-only JSON, so signatures never depend on text encoding.

## Tests

- `python -m pytest tests/test_inbox.py`: the blueprint against a fake script and model.
- In the script repo, `node --test tests/run-tests.js` runs the real `.gs` files under Node with fake Gmail and Sheets.
- `node tests/gas-sim.js serve 8765` stands in for the deployed web app, for end-to-end runs.
