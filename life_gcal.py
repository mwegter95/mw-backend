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
from google.auth.transport.requests import Request
from google.auth.exceptions import RefreshError
from googleapiclient.errors import HttpError
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


def _google_error_detail(exc):
    if isinstance(exc, HttpError):
        status = getattr(getattr(exc, "resp", None), "status", None)
        reason = getattr(getattr(exc, "resp", None), "reason", "")
        content = getattr(exc, "content", b"")
        if isinstance(content, bytes):
            content = content.decode("utf-8", "replace")
        message = content
        try:
            parsed = json.loads(content)
            err = parsed.get("error", parsed)
            message = err.get("message") or err.get("error_description") or content
        except Exception:
            pass
        prefix = f"HTTP {status}" if status else "HTTP error"
        return f"{prefix} {reason}: {message}".strip()
    return f"{exc.__class__.__name__}: {exc}"


class CalendarFetchError(RuntimeError):
    """Raised when Google Calendar could not be reached or authorized."""

    def __init__(self, message, cause=None):
        self.detail = _google_error_detail(cause) if cause else ""
        super().__init__(f"{message}: {self.detail}" if self.detail else message)


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
    # autogenerate_code_verifier=False disables PKCE. We're a confidential web
    # client (client_secret), and connect/callback are separate stateless
    # requests, so there's no verifier to carry between them — without this,
    # Google rejects the exchange with "Missing code verifier".
    flow = Flow.from_client_config(
        _client_config(), scopes=SCOPES, redirect_uri=GOOGLE_REDIRECT_URI,
        autogenerate_code_verifier=False,
    )
    auth_url, _ = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",          # force a refresh_token to come back every time
        state=state,
    )
    return auth_url


def exchange_code(code):
    """Exchange an auth code for credentials. Returns {refresh_token, scope, email}."""
    # autogenerate_code_verifier=False disables PKCE. We're a confidential web
    # client (client_secret), and connect/callback are separate stateless
    # requests, so there's no verifier to carry between them — without this,
    # Google rejects the exchange with "Missing code verifier".
    flow = Flow.from_client_config(
        _client_config(), scopes=SCOPES, redirect_uri=GOOGLE_REDIRECT_URI,
        autogenerate_code_verifier=False,
    )
    flow.fetch_token(code=code)
    creds = flow.credentials
    svc = build("calendar", "v3", credentials=creds, cache_discovery=False)
    try:
        primary = svc.calendarList().get(calendarId="primary").execute()
    except Exception as exc:
        raise CalendarFetchError("Google Calendar connected, but calendar access validation failed", exc) from exc
    email = primary.get("id", "") or ""
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


def _event_end(ev):
    e = ev.get("end", {})
    return e.get("date") or e.get("dateTime")


def _is_multi_day(start, end, all_day):
    """A multi-day span (esp. all-day) is a strong trip signal even when the
    title is just a place name."""
    try:
        sd = datetime.date.fromisoformat(start[:10])
        ed = datetime.date.fromisoformat(end[:10])
        # All-day end.date is exclusive, so a 1-day event has span 1.
        return (ed - sd).days > 1 if all_day else (ed - sd).days >= 1
    except Exception:
        return False


def list_upcoming_events(refresh_token, days=90, max_events=400):
    """Upcoming events across all of the user's calendars within `days`."""
    creds = _credentials_from_refresh(refresh_token)
    try:
        creds.refresh(Request())
    except RefreshError as exc:
        raise CalendarFetchError("Couldn't refresh Google Calendar credentials", exc) from exc
    svc = build("calendar", "v3", credentials=creds, cache_discovery=False)

    now = datetime.datetime.now(datetime.UTC)
    time_min = now.isoformat()
    time_max = (now + datetime.timedelta(days=days)).isoformat()

    try:
        cal_list = svc.calendarList().list().execute().get("items", [])
    except Exception as exc:
        raise CalendarFetchError("Couldn't list Google calendars", exc) from exc

    out = []
    attempted = 0
    succeeded = 0
    failures = []
    for cal in cal_list:
        cal_id = cal.get("id")
        if not cal_id:
            continue
        attempted += 1
        try:
            resp = svc.events().list(
                calendarId=cal_id,
                timeMin=time_min,
                timeMax=time_max,
                singleEvents=True,
                orderBy="startTime",
                maxResults=250,
            ).execute()
            succeeded += 1
        except Exception as exc:
            failures.append(exc)
            continue
        for ev in resp.get("items", []):
            start, all_day = _event_start(ev)
            if not start:
                continue
            end = _event_end(ev)
            out.append({
                "id": ev.get("id"),
                "calendarId": cal_id,
                "title": ev.get("summary", "(no title)"),
                "location": (ev.get("location") or "")[:120],
                "start": start,
                "date": start[:10],
                "end": end,
                "allDay": all_day,
                "multiDay": _is_multi_day(start, end, all_day) if end else False,
                "recurring": bool(ev.get("recurringEventId")),
                "recurringEventId": ev.get("recurringEventId"),
            })

    if attempted and succeeded == 0 and failures:
        raise CalendarFetchError("Couldn't fetch Google Calendar events", failures[0]) from failures[0]

    out.sort(key=lambda e: e["start"])
    return out[:max_events]


# ── AI generation ─────────────────────────────────────────────────────────────
# The smart-tasking prompt/engine lives in life_smart.py + the life_skills/
# skill library (a core contract + a router/triage skill + per-category skills,
# orchestrated by the model). The model only classifies + phrases; life_smart
# deterministically computes dates/points and validates everything.

def generate_tasks(events, today_iso, max_events_for_ai=None):
    import life_smart
    return life_smart.generate_tasks(events, today_iso)
