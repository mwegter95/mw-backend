"""
Google Calendar integration + AI smart-task generation for the Life Dashboard.

Kept separate from server.py so the Google client libraries stay isolated (and
so server.py still imports if they're absent). This module is pure logic:
OAuth URL building / code exchange, event listing, and turning events into
smart-reminder dicts via gh_models (GitHub Models). All DB access, refresh-token
encryption, and Flask wiring live in server.py.

Env:
  GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET   — OAuth web client
  GOOGLE_REDIRECT_URI                      — must match the client's authorized URI
"""
from __future__ import annotations

import datetime
import json
import os

from google_auth_oauthlib.flow import Flow
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

import gh_models

# Read-only across the user's calendars (captures the Birthdays calendar too).
SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]

GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
GOOGLE_REDIRECT_URI = os.environ.get(
    "GOOGLE_REDIRECT_URI", "https://api.michaelwegter.com/api/life/gcal/callback"
)

AUTH_URI = "https://accounts.google.com/o/oauth2/auth"
TOKEN_URI = "https://oauth2.googleapis.com/token"


def is_configured():
    return bool(GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET)


def _client_config():
    return {
        "web": {
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "auth_uri": AUTH_URI,
            "token_uri": TOKEN_URI,
            "redirect_uris": [GOOGLE_REDIRECT_URI],
        }
    }


# ── OAuth ──────────────────────────────────────────────────────────────────────

def build_auth_url(state):
    flow = Flow.from_client_config(_client_config(), scopes=SCOPES, redirect_uri=GOOGLE_REDIRECT_URI)
    auth_url, _ = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",          # force a refresh_token to come back every time
        state=state,
    )
    return auth_url


def exchange_code(code):
    """Exchange an auth code for credentials. Returns {refresh_token, scope, email}."""
    flow = Flow.from_client_config(_client_config(), scopes=SCOPES, redirect_uri=GOOGLE_REDIRECT_URI)
    flow.fetch_token(code=code)
    creds = flow.credentials
    email = ""
    try:
        svc = build("calendar", "v3", credentials=creds, cache_discovery=False)
        primary = svc.calendarList().get(calendarId="primary").execute()
        email = primary.get("id", "") or ""
    except Exception:
        pass
    return {
        "refresh_token": creds.refresh_token or "",
        "scope": " ".join(creds.scopes or SCOPES),
        "email": email,
    }


def _credentials_from_refresh(refresh_token):
    return Credentials(
        token=None,
        refresh_token=refresh_token,
        token_uri=TOKEN_URI,
        client_id=GOOGLE_CLIENT_ID,
        client_secret=GOOGLE_CLIENT_SECRET,
        scopes=SCOPES,
    )


# ── Event listing ───────────────────────────────────────────────────────────────

def _event_start(ev):
    s = ev.get("start", {})
    if "date" in s:
        return s["date"], True              # all-day → YYYY-MM-DD
    dt = s.get("dateTime")
    if dt:
        return dt, False
    return None, False


def list_upcoming_events(refresh_token, days=90, max_events=400):
    """Upcoming events across all of the user's calendars within `days`."""
    creds = _credentials_from_refresh(refresh_token)
    svc = build("calendar", "v3", credentials=creds, cache_discovery=False)

    now = datetime.datetime.now(datetime.UTC)
    time_min = now.isoformat()
    time_max = (now + datetime.timedelta(days=days)).isoformat()

    try:
        cal_list = svc.calendarList().list().execute().get("items", [])
    except Exception:
        cal_list = [{"id": "primary"}]

    out = []
    for cal in cal_list:
        cal_id = cal.get("id")
        if not cal_id:
            continue
        try:
            resp = svc.events().list(
                calendarId=cal_id,
                timeMin=time_min,
                timeMax=time_max,
                singleEvents=True,
                orderBy="startTime",
                maxResults=250,
            ).execute()
        except Exception:
            continue
        for ev in resp.get("items", []):
            start, all_day = _event_start(ev)
            if not start:
                continue
            out.append({
                "id": ev.get("id"),
                "calendarId": cal_id,
                "title": ev.get("summary", "(no title)"),
                "start": start,
                "date": start[:10],
                "allDay": all_day,
                "recurring": bool(ev.get("recurringEventId")),
            })

    out.sort(key=lambda e: e["start"])
    return out[:max_events]


# ── AI generation ─────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You turn a person's upcoming calendar events into a SHORT list of proactive, actionable prep reminders for their personal habit dashboard.

Rules:
- Only create reminders for events that genuinely benefit from advance prep: birthdays, anniversaries, weddings, trips/flights, personal milestones, appointments needing prep, holidays that need planning.
- IGNORE routine noise: regular work meetings, standups, recurring focus blocks, lunches, generic busy/free blocks.
- Give each reminder a sensible LEAD-TIME date BEFORE the event (e.g. "buy a gift" ~7 days before a birthday; "plan something" ~10 days before; "pack / check in" ~1 day before a trip). The lead-time date must be today or later, and on or before the event date.
- Be specific and warm: "Buy Mom a birthday gift", not "Birthday reminder". For a birthday you may create up to 2 reminders (e.g. get a gift, make plans).
- points: 1 (small), 2 (medium), 3 (meaningful effort).

Return STRICT JSON only, no prose:
{"tasks":[{"title":"...","date":"YYYY-MM-DD","points":1,"category":"Calendar","sourceEventId":"<id of the source event>"}]}
If nothing warrants a reminder, return {"tasks":[]}."""


def build_messages(events, today_iso):
    compact = [
        {"id": e["id"], "title": e["title"], "date": e["date"], "allDay": e["allDay"]}
        for e in events
    ]
    user = (
        f"Today is {today_iso}. Here are the upcoming events as JSON:\n\n"
        f"{json.dumps(compact, ensure_ascii=False)}\n\n"
        "Produce the smart reminders now."
    )
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]


def _parse_tasks_json(content):
    content = (content or "").strip()
    if content.startswith("```"):
        content = content.strip("`")
        nl = content.find("\n")
        if nl != -1:
            content = content[nl + 1:]
    try:
        return json.loads(content)
    except Exception:
        a, b = content.find("{"), content.rfind("}")
        if a == -1 or b == -1:
            return {}
        try:
            return json.loads(content[a:b + 1])
        except Exception:
            return {}


def generate_tasks(events, today_iso, max_events_for_ai=120):
    """Call GitHub Models and return a list of validated task dicts."""
    if not events:
        return []
    events = events[:max_events_for_ai]
    messages = build_messages(events, today_iso)
    try:
        content = gh_models.chat_completion(messages, json_object=True, max_tokens=1500)
    except gh_models.GitHubModelsError:
        # Some models/endpoints reject response_format; retry as plain text.
        content = gh_models.chat_completion(messages, json_object=False, max_tokens=1500)

    data = _parse_tasks_json(content)
    valid_ids = {e["id"] for e in events}
    tasks = []
    for t in (data.get("tasks") or []):
        title = (t.get("title") or "").strip()
        date = (t.get("date") or "").strip()[:10]
        if not title or len(date) != 10:
            continue
        if date < today_iso:
            date = today_iso
        try:
            pts = int(t.get("points", 1))
        except Exception:
            pts = 1
        pts = max(1, min(3, pts))
        src = (t.get("sourceEventId") or "").strip()
        if src and src not in valid_ids:
            src = ""  # model hallucinated an id; fall back to title/date keying
        tasks.append({
            "title": title[:120],
            "date": date,
            "points": pts,
            "category": (t.get("category") or "Calendar").strip()[:30] or "Calendar",
            "sourceEventId": src,
        })
    return tasks
