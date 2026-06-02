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
# The smart-tasking prompt/engine lives in life_smart.py + the life_skills/
# skill library (a core contract + a router/triage skill + per-category skills,
# orchestrated by the model). The model only classifies + phrases; life_smart
# deterministically computes dates/points and validates everything.

def generate_tasks(events, today_iso, max_events_for_ai=None):
    import life_smart
    return life_smart.generate_tasks(events, today_iso)
