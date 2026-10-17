"""
PC Gaming Activity — Flask Blueprint
Mounts under /gaming on the mw-backend server.

Backs the admin-only dashboard at michaelwegter.com/apps/gaming-dashboard and
receives play sessions from the tracker agent running on the gaming PC.

WHY THIS LOOKS LIKE feelgood_blueprint.py:
Same reasoning — sign-in is required for every read endpoint and we reuse the
SAME accounts as the rest of the site, without a circular import of server.py:
  - the shared JWT secret is read straight from data/.secret_key (the exact file
    server.py's _get_secret() writes), and
  - the caller is resolved against the shared `users` table in data/mw.db.
No new auth endpoints are added here; the dashboard uses /auth/login.

TWO KINDS OF CALLER
  1. The OWNER (a signed-in user whose email matches GAMING_OWNER_EMAIL) — reads
     every stat and manages the tracked-game list. Bearer JWT.
  2. The AGENT (the tracker on the gaming PC) — writes sessions and heartbeats.
     It never has a user account; it authenticates with an HMAC-SHA256 signature
     over "<timestamp>.<body>" using GAMING_AGENT_SECRET, with a 300-second
     timestamp window. Same scheme as runner_blueprint.py, so the secret never
     goes over the wire and a captured request cannot be replayed later.

Endpoints
  GET    /gaming/health                  liveness (public)
  POST   /gaming/ingest                  agent → upsert a batch of sessions
  POST   /gaming/heartbeat               agent → "still alive, currently playing X"
  GET    /gaming/config                  agent → the tracked-game registry
  GET    /gaming/status                  owner → live agent + current session
  GET    /gaming/sessions                owner → paged session list
  DELETE /gaming/sessions/<id>           owner → delete a bogus session
  GET    /gaming/stats                   owner → aggregates for the dashboard
  GET    /gaming/games                   owner → tracked games
  POST   /gaming/games                   owner → add a game
  PATCH  /gaming/games/<slug>            owner → edit a game
  DELETE /gaming/games/<slug>            owner → remove a game
  GET    /gaming/candidates              owner → unrecognised processes seen
  POST   /gaming/candidates/<name>/hide  owner → dismiss a candidate

Storage: tables this blueprint owns (gaming_games, gaming_sessions,
gaming_agents, gaming_candidates) inside the existing SQLite DB at data/mw.db,
created lazily with CREATE TABLE IF NOT EXISTS. It does NOT modify the shared
SCHEMA, auth code, or any other table/blueprint.

CORS: handled globally by server.py's CORS(app, ...), which already allow-lists
https://michaelwegter.com.

Environment (.env on the Surface):
  GAMING_AGENT_SECRET   required for ingest/heartbeat/config; unset = agent API
                        is disabled (503) and only the dashboard works.
  GAMING_OWNER_EMAIL    defaults to zweetztuph@gmail.com.
"""

import os
import re
import json
import hmac
import time
import sqlite3
import hashlib
from functools import wraps
from pathlib import Path
from datetime import datetime, timezone, timedelta

import jwt as _jwt
from flask import Blueprint, request, jsonify, g

gaming_bp = Blueprint("gaming", __name__, url_prefix="/gaming")

# Same data dir convention as the rest of the server.
DATA_DIR = Path(__file__).parent / "data"
DB_PATH = DATA_DIR / "mw.db"
SECRET_FILE = DATA_DIR / ".secret_key"

OWNER_EMAIL = (os.environ.get("GAMING_OWNER_EMAIL") or "zweetztuph@gmail.com").strip().lower()
AGENT_SECRET = os.environ.get("GAMING_AGENT_SECRET", "").strip()
SIGNATURE_WINDOW_SECONDS = 300

MAX_BATCH_SESSIONS = 500
MAX_SESSION_SECONDS = 24 * 3600        # a single session longer than a day is bogus
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")

# Chart colours, in assignment order. These are michaelwegter.com's own palette
# stepped into the lightness band that reads correctly on the site's dark
# surface, ordered so that neighbouring slots stay distinguishable under
# red/green colour blindness. A new game takes the next unused slot, so a game
# keeps its colour for life and no two tracked games collide.
CHART_PALETTE = [
    "#b08a05",  # mustard
    "#f0186e",  # hot pink
    "#05a1b4",  # cyan
    "#e83828",  # parrot red
    "#3a8fcc",  # sky blue
    "#41a83f",  # green
]

# Games the agent tracks out of the box. Seeded once, then owned by the DB so
# the dashboard can edit them without a redeploy.
DEFAULT_GAMES = [
    {
        "slug": "rocket-league",
        "name": "Rocket League",
        "process_names": ["RocketLeague.exe"],
        "color": CHART_PALETTE[0],
        "icon": "🚀",
    },
    {
        "slug": "fortnite",
        "name": "Fortnite",
        "process_names": [
            "FortniteClient-Win64-Shipping.exe",
            "FortniteClient-Win64-Shipping_BE.exe",
            "FortniteClient-Win64-Shipping_EAC.exe",
            "FortniteClient-Win64-Shipping_EAC_EOS.exe",
        ],
        "color": CHART_PALETTE[1],
        "icon": "🛡",
    },
]

_secret_cache = None


# ─── Infrastructure ───────────────────────────────────────────────────────────

def _shared_secret():
    """Read the JWT secret server.py created (data/.secret_key). Cached."""
    global _secret_cache
    if _secret_cache is None:
        try:
            _secret_cache = SECRET_FILE.read_text().strip()
        except Exception:
            _secret_cache = None
    return _secret_cache


def _db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


_TABLES_READY = False

SCHEMA = """
CREATE TABLE IF NOT EXISTS gaming_games (
    slug                TEXT PRIMARY KEY,
    name                TEXT NOT NULL,
    process_names       TEXT NOT NULL DEFAULT '[]',   -- JSON array, matched case-insensitively
    color               TEXT NOT NULL DEFAULT '#6b7280',
    icon                TEXT NOT NULL DEFAULT '🎮',
    enabled             INTEGER NOT NULL DEFAULT 1,
    min_session_seconds INTEGER NOT NULL DEFAULT 60,
    created_at          TEXT,
    updated_at          TEXT
);

CREATE TABLE IF NOT EXISTS gaming_sessions (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id         TEXT NOT NULL UNIQUE,   -- agent-generated UUID; the idempotency key
    agent_id          TEXT NOT NULL DEFAULT '',
    game_slug         TEXT NOT NULL,
    game_name         TEXT NOT NULL DEFAULT '',
    process_name      TEXT NOT NULL DEFAULT '',
    started_at        TEXT NOT NULL,          -- ISO8601 UTC
    ended_at          TEXT,                   -- NULL while the session is live
    duration_seconds  INTEGER NOT NULL DEFAULT 0,
    idle_seconds      INTEGER NOT NULL DEFAULT 0,
    active_seconds    INTEGER NOT NULL DEFAULT 0,
    local_date        TEXT NOT NULL DEFAULT '',   -- YYYY-MM-DD in the player's local time
    local_start_hour  INTEGER NOT NULL DEFAULT 0, -- 0-23 local, for the time-of-day heatmap
    local_weekday     INTEGER NOT NULL DEFAULT 0, -- 0=Monday
    tz_offset_minutes INTEGER NOT NULL DEFAULT 0,
    in_progress       INTEGER NOT NULL DEFAULT 0,
    created_at        TEXT,
    updated_at        TEXT
);

CREATE INDEX IF NOT EXISTS idx_gaming_sessions_started ON gaming_sessions(started_at);
CREATE INDEX IF NOT EXISTS idx_gaming_sessions_game    ON gaming_sessions(game_slug);
CREATE INDEX IF NOT EXISTS idx_gaming_sessions_date    ON gaming_sessions(local_date);

CREATE TABLE IF NOT EXISTS gaming_agents (
    agent_id           TEXT PRIMARY KEY,
    hostname           TEXT NOT NULL DEFAULT '',
    agent_version      TEXT NOT NULL DEFAULT '',
    last_seen_at       TEXT,
    current_game_slug  TEXT NOT NULL DEFAULT '',
    current_game_name  TEXT NOT NULL DEFAULT '',
    current_started_at TEXT,
    current_seconds    INTEGER NOT NULL DEFAULT 0,
    current_idle       INTEGER NOT NULL DEFAULT 0,
    created_at         TEXT
);

CREATE TABLE IF NOT EXISTS gaming_candidates (
    process_name TEXT PRIMARY KEY,
    first_seen   TEXT,
    last_seen    TEXT,
    seen_count   INTEGER NOT NULL DEFAULT 1,
    hidden       INTEGER NOT NULL DEFAULT 0
);
"""


def _ensure_tables():
    """Create our tables (once per process) and seed the default games."""
    global _TABLES_READY
    if _TABLES_READY:
        return
    conn = _db()
    try:
        conn.executescript(SCHEMA)
        existing = conn.execute("SELECT COUNT(*) AS n FROM gaming_games").fetchone()["n"]
        if not existing:
            now = _now_iso()
            for game in DEFAULT_GAMES:
                conn.execute(
                    """INSERT OR IGNORE INTO gaming_games
                       (slug, name, process_names, color, icon, enabled,
                        min_session_seconds, created_at, updated_at)
                       VALUES (?,?,?,?,?,1,60,?,?)""",
                    (game["slug"], game["name"], json.dumps(game["process_names"]),
                     game["color"], game["icon"], now, now),
                )
        conn.commit()
    finally:
        conn.close()
    _TABLES_READY = True


# ─── Auth ─────────────────────────────────────────────────────────────────────

def require_owner(f):
    """Authenticate against the SHARED users table using the SHARED JWT secret,
    then require that the account is the site owner. Populates g.user_id."""
    @wraps(f)
    def wrapper(*args, **kwargs):
        secret = _shared_secret()
        if not secret:
            return jsonify({"error": "Auth temporarily unavailable"}), 503
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return jsonify({"error": "Authentication required"}), 401
        try:
            payload = _jwt.decode(auth[7:], secret, algorithms=["HS256"])
        except _jwt.ExpiredSignatureError:
            return jsonify({"error": "Session expired — please sign in again"}), 401
        except _jwt.PyJWTError:
            return jsonify({"error": "Invalid token"}), 401
        user_id = str(payload.get("sub") or "")
        if not user_id:
            return jsonify({"error": "Invalid token"}), 401
        conn = _db()
        try:
            row = conn.execute("SELECT id, email FROM users WHERE id=?", (user_id,)).fetchone()
        finally:
            conn.close()
        if not row:
            return jsonify({"error": "Account not found"}), 401
        # The dashboard is private: only the owner account may read it.
        if (row["email"] or "").strip().lower() != OWNER_EMAIL:
            return jsonify({"error": "Not authorised"}), 403
        g.user_id = user_id
        _ensure_tables()
        return f(*args, **kwargs)
    return wrapper


def _verify_agent_signature(raw_body: bytes) -> str:
    """Returns '' when the request is authentic, else an error message.

    Signature = HMAC-SHA256(GAMING_AGENT_SECRET, "<timestamp>.<raw body>").
    The timestamp is part of the signed payload, so an attacker who captures a
    request cannot replay it outside the window or alter the body.
    """
    if not AGENT_SECRET:
        return "Agent API disabled — set GAMING_AGENT_SECRET on the server"
    ts = request.headers.get("X-Gaming-Timestamp", "").strip()
    sig = request.headers.get("X-Gaming-Signature", "").strip()
    if not ts or not sig:
        return "Missing X-Gaming-Timestamp / X-Gaming-Signature"
    try:
        ts_val = int(float(ts))
    except ValueError:
        return "Bad timestamp"
    if abs(time.time() - ts_val) > SIGNATURE_WINDOW_SECONDS:
        return "Timestamp outside the allowed window"
    expected = hmac.new(
        AGENT_SECRET.encode("utf-8"),
        f"{ts}.".encode("utf-8") + raw_body,
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(expected, sig.lower()):
        return "Bad signature"
    return ""


def require_agent(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        err = _verify_agent_signature(request.get_data() or b"")
        if err:
            status = 503 if err.startswith("Agent API disabled") else 401
            return jsonify({"error": err}), status
        g.agent_id = (request.headers.get("X-Gaming-Agent", "").strip() or "default")[:64]
        _ensure_tables()
        return f(*args, **kwargs)
    return wrapper


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _game_row_to_dict(row):
    try:
        names = json.loads(row["process_names"])
        if not isinstance(names, list):
            names = []
    except Exception:
        names = []
    return {
        "slug": row["slug"],
        "name": row["name"],
        "process_names": [str(n) for n in names],
        "color": row["color"],
        "icon": row["icon"],
        "enabled": bool(row["enabled"]),
        "min_session_seconds": row["min_session_seconds"],
    }


def _session_row_to_dict(row):
    return {
        "id": row["id"],
        "client_id": row["client_id"],
        "game_slug": row["game_slug"],
        "game_name": row["game_name"],
        "process_name": row["process_name"],
        "started_at": row["started_at"],
        "ended_at": row["ended_at"],
        "duration_seconds": row["duration_seconds"],
        "idle_seconds": row["idle_seconds"],
        "active_seconds": row["active_seconds"],
        "local_date": row["local_date"],
        "local_start_hour": row["local_start_hour"],
        "local_weekday": row["local_weekday"],
        "in_progress": bool(row["in_progress"]),
    }


def _clean_int(value, default=0, lo=None, hi=None):
    try:
        out = int(value)
    except (TypeError, ValueError):
        return default
    if lo is not None and out < lo:
        return lo
    if hi is not None and out > hi:
        return hi
    return out


def _parse_iso(value):
    """Lenient ISO8601 → aware UTC datetime, or None."""
    if not value or not isinstance(value, str):
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _derive_local_fields(started_at_dt, tz_offset_minutes, given_date):
    """The player's local calendar day/hour/weekday for a session start.

    The agent reports its UTC offset, so the server can bucket by the player's
    local day without guessing a timezone — a 1am session belongs to the night
    it started, which is what a human expects to see on a calendar."""
    local_dt = started_at_dt + timedelta(minutes=tz_offset_minutes)
    local_date = given_date if (given_date and len(given_date) == 10) else local_dt.strftime("%Y-%m-%d")
    return local_date, local_dt.hour, local_dt.weekday()


def _range_bounds(args):
    """?days=30 (default) or ?from=YYYY-MM-DD&to=YYYY-MM-DD. Returns local dates."""
    frm = (args.get("from") or "").strip()
    to = (args.get("to") or "").strip()
    if len(frm) == 10 and len(to) == 10:
        return frm, to
    days = _clean_int(args.get("days"), default=30, lo=1, hi=3650)
    today = datetime.now(timezone.utc)
    tz_min = _clean_int(args.get("tz"), default=0, lo=-840, hi=840)
    local_today = (today + timedelta(minutes=tz_min)).date()
    return (local_today - timedelta(days=days - 1)).isoformat(), local_today.isoformat()


# ─── Work-hours cleanup ───────────────────────────────────────────────────────
#
# The agent stops recording during work hours, but sessions captured before
# that rule existed are still in this table, and they still reach every total.
# What follows is the retrospective version of the same blackout: it subtracts
# the window from sessions that were already written down.
#
# It is deliberately the agent's rule rather than a blunt "delete anything that
# started between 9 and 5":
#
#   - a Wednesday lunchtime session goes;
#   - an 8:30 session that ran to 10:00 keeps the half hour it earned and is
#     cut at 9:00, which is exactly where the live tracker would cut it;
#   - a 16:45 session that ran to 18:30 keeps the part after 17:00;
#   - evenings, weekends and anything clear of the window are untouched.
#
# A trimmed session's idle and active split cannot be re-derived — the agent
# recorded the totals, not the minute-by-minute shape — so it is prorated over
# the surviving stretch, keeping active + idle == duration, which every
# statistic depends on. A session that straddles the whole window (started
# before 9, ended after 5) keeps its longer side; the live tracker would have
# recorded two sessions there, and inventing the second one would be inventing
# data.

WORKDAY_DEFAULT_DAYS = [0, 1, 2, 3, 4]     # 0 = Monday
WORKDAY_DEFAULT_START = 9 * 60
WORKDAY_DEFAULT_END = 17 * 60
DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def _parse_hhmm(value, default_minutes):
    """'09:00', '9:00' or 540 → minutes from midnight."""
    if isinstance(value, bool) or value is None:
        return default_minutes
    if isinstance(value, (int, float)):
        return int(value) % (24 * 60)
    try:
        hours, _, minutes = str(value).strip().partition(":")
        return (int(hours) * 60 + int(minutes or 0)) % (24 * 60)
    except (TypeError, ValueError):
        return default_minutes


def _clean_weekdays(value):
    if value is None:
        return frozenset(WORKDAY_DEFAULT_DAYS)
    try:
        days = frozenset(int(d) % 7 for d in value)
    except (TypeError, ValueError):
        return frozenset(WORKDAY_DEFAULT_DAYS)
    return days


def _describe_window(days, start_minutes, end_minutes):
    if not days or start_minutes == end_minutes:
        return "nothing (empty window)"
    ordered = sorted(days)
    runs, run = [], [ordered[0]]
    for day in ordered[1:]:
        if day == run[-1] + 1:
            run.append(day)
        else:
            runs.append(run)
            run = [day]
    runs.append(run)
    label = ", ".join(DAY_NAMES[r[0]] if len(r) == 1
                      else f"{DAY_NAMES[r[0]]}-{DAY_NAMES[r[-1]]}" for r in runs)
    return f"{label} {start_minutes // 60:02d}:{start_minutes % 60:02d}" \
           f"-{end_minutes // 60:02d}:{end_minutes % 60:02d}"


def _blackout_spans(local_start, local_end, days, start_minutes, end_minutes):
    """Every blackout interval that could overlap [local_start, local_end].

    Naive local datetimes, so daylight saving is already baked into the offset
    the agent reported for that session. A window whose end is not after its
    start runs past midnight and belongs to the day it opened on, which is why
    the walk begins a day early.
    """
    if not days or start_minutes == end_minutes:
        return []
    length = timedelta(minutes=(end_minutes - start_minutes) % (24 * 60))
    day = local_start.date() - timedelta(days=1)
    last = local_end.date()
    spans = []
    while day <= last:
        if day.weekday() in days:
            opened = (datetime.combine(day, datetime.min.time())
                      + timedelta(minutes=start_minutes))
            spans.append((opened, opened + length))
        day += timedelta(days=1)
    return spans


def _surviving_spans(local_start, local_end, spans):
    """[local_start, local_end] with every blackout interval cut out of it."""
    pieces = [(local_start, local_end)]
    for opened, closed in spans:
        remaining = []
        for seg_start, seg_end in pieces:
            if closed <= seg_start or opened >= seg_end:
                remaining.append((seg_start, seg_end))
                continue
            if seg_start < opened:
                remaining.append((seg_start, opened))
            if closed < seg_end:
                remaining.append((closed, seg_end))
        pieces = remaining
    return pieces


def _cull_plan_for(row, days, start_minutes, end_minutes, min_seconds):
    """What should happen to one session: None, or a plan describing the change.

    `None` means the session never touched the window and is left exactly as it
    is — the common case, and the reason a preview is cheap to run over years
    of history.
    """
    started = _parse_iso(row["started_at"])
    ended = _parse_iso(row["ended_at"])
    if started is None or ended is None or ended <= started:
        return None

    tz_minutes = row["tz_offset_minutes"] or 0
    offset = timedelta(minutes=tz_minutes)
    # Naive local wall clock, which is the only frame the window is defined in.
    local_start = (started + offset).replace(tzinfo=None)
    local_end = (ended + offset).replace(tzinfo=None)

    wall = (local_end - local_start).total_seconds()
    kept = _surviving_spans(local_start, local_end,
                            _blackout_spans(local_start, local_end, days,
                                            start_minutes, end_minutes))
    survives = sum((end - start).total_seconds() for start, end in kept)
    if survives >= wall:
        return None                                   # clear of the window

    old_active = max(0, row["active_seconds"] or 0)
    plan = {
        "id": row["id"],
        "game_slug": row["game_slug"],
        "game_name": row["game_name"] or row["game_slug"],
        "local_date": row["local_date"],
        "started_at": row["started_at"],
        "duration_seconds": row["duration_seconds"] or 0,
        "active_seconds": old_active,
    }

    longest = max(kept, key=lambda s: s[1] - s[0], default=None)
    keep_seconds = int((longest[1] - longest[0]).total_seconds()) if longest else 0
    if keep_seconds < max(1, min_seconds):
        # Nothing worth keeping: what is left is shorter than a session is
        # allowed to be, which is the same bar the agent applies live.
        plan.update(action="delete", active_removed=old_active,
                    seconds_removed=int(wall))
        return plan

    ratio = keep_seconds / wall
    new_active = max(0, min(keep_seconds, int(round(old_active * ratio))))
    new_started = (longest[0] - offset).replace(tzinfo=timezone.utc)
    new_ended = (longest[1] - offset).replace(tzinfo=timezone.utc)
    local_date, local_hour, local_weekday = _derive_local_fields(new_started, tz_minutes, "")
    plan.update(
        action="trim",
        seconds_removed=int(wall - keep_seconds),
        active_removed=old_active - new_active,
        new_started_at=new_started.isoformat(),
        new_ended_at=new_ended.isoformat(),
        new_duration_seconds=keep_seconds,
        new_active_seconds=new_active,
        new_idle_seconds=keep_seconds - new_active,
        new_local_date=local_date,
        new_local_start_hour=local_hour,
        new_local_weekday=local_weekday,
    )
    return plan


# ─── Public ───────────────────────────────────────────────────────────────────

@gaming_bp.get("/health")
def health():
    return jsonify({
        "ok": True,
        "service": "gaming",
        "agent_api": bool(AGENT_SECRET),
    })


# ─── Agent API ────────────────────────────────────────────────────────────────

@gaming_bp.get("/config")
@require_agent
def agent_config():
    """The tracked-game registry the agent polls, so adding a game from the
    dashboard reaches the gaming PC without editing a file there."""
    conn = _db()
    try:
        rows = conn.execute(
            "SELECT * FROM gaming_games WHERE enabled=1 ORDER BY name"
        ).fetchall()
    finally:
        conn.close()
    return jsonify({"games": [_game_row_to_dict(r) for r in rows], "server_time": _now_iso()})


@gaming_bp.post("/ingest")
@require_agent
def ingest():
    """Upsert a batch of sessions.

    Idempotent on client_id: the agent may resend anything it is unsure about
    (after a crash, a lost response, or a week offline) and rows are updated
    rather than duplicated. Finished sessions are never silently overwritten by
    a stale in-progress copy of themselves.
    """
    body = request.get_json(silent=True) or {}
    sessions = body.get("sessions")
    if not isinstance(sessions, list):
        return jsonify({"error": "sessions must be an array"}), 400
    if len(sessions) > MAX_BATCH_SESSIONS:
        return jsonify({"error": f"too many sessions in one batch (max {MAX_BATCH_SESSIONS})"}), 413

    now = _now_iso()
    accepted, rejected = [], []
    conn = _db()
    try:
        known = {r["slug"]: r["name"] for r in conn.execute("SELECT slug, name FROM gaming_games")}
        for raw in sessions:
            if not isinstance(raw, dict):
                rejected.append({"client_id": None, "reason": "not an object"})
                continue
            client_id = str(raw.get("client_id") or "").strip()[:64]
            slug = str(raw.get("game_slug") or "").strip().lower()[:64]
            started = _parse_iso(raw.get("started_at"))
            if not client_id or not slug or not started:
                rejected.append({"client_id": client_id or None,
                                 "reason": "client_id, game_slug and started_at are required"})
                continue

            ended = _parse_iso(raw.get("ended_at"))
            in_progress = 1 if (raw.get("in_progress") or ended is None) else 0
            duration = _clean_int(raw.get("duration_seconds"), 0, 0, MAX_SESSION_SECONDS)
            idle = _clean_int(raw.get("idle_seconds"), 0, 0, duration)
            active = _clean_int(raw.get("active_seconds"), duration - idle, 0, duration)
            tz_off = _clean_int(raw.get("tz_offset_minutes"), 0, -840, 840)
            local_date, local_hour, local_weekday = _derive_local_fields(
                started, tz_off, str(raw.get("local_date") or "")
            )
            game_name = str(raw.get("game_name") or known.get(slug) or slug)[:120]

            conn.execute(
                """INSERT INTO gaming_sessions
                     (client_id, agent_id, game_slug, game_name, process_name,
                      started_at, ended_at, duration_seconds, idle_seconds, active_seconds,
                      local_date, local_start_hour, local_weekday, tz_offset_minutes,
                      in_progress, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(client_id) DO UPDATE SET
                     ended_at         = excluded.ended_at,
                     duration_seconds = excluded.duration_seconds,
                     idle_seconds     = excluded.idle_seconds,
                     active_seconds   = excluded.active_seconds,
                     game_name        = excluded.game_name,
                     in_progress      = excluded.in_progress,
                     updated_at       = excluded.updated_at
                   WHERE gaming_sessions.in_progress = 1""",
                (client_id, g.agent_id, slug, game_name,
                 str(raw.get("process_name") or "")[:120],
                 started.isoformat(), ended.isoformat() if ended else None,
                 duration, idle, active,
                 local_date, local_hour, local_weekday, tz_off,
                 in_progress, now, now),
            )
            accepted.append(client_id)

        # Any unrecognised process the agent noticed, so the dashboard can offer
        # it as a one-click addition instead of making him hunt for the exe name.
        for cand in (body.get("candidates") or [])[:50]:
            name = str(cand or "").strip()[:120]
            if not name:
                continue
            conn.execute(
                """INSERT INTO gaming_candidates (process_name, first_seen, last_seen, seen_count)
                   VALUES (?,?,?,1)
                   ON CONFLICT(process_name) DO UPDATE SET
                     last_seen  = excluded.last_seen,
                     seen_count = gaming_candidates.seen_count + 1""",
                (name, now, now),
            )
        conn.commit()
    finally:
        conn.close()

    return jsonify({"ok": True, "accepted": accepted, "rejected": rejected, "server_time": now})


@gaming_bp.post("/heartbeat")
@require_agent
def heartbeat():
    """Liveness + what is being played right now, for the dashboard's live tile."""
    body = request.get_json(silent=True) or {}
    now = _now_iso()
    conn = _db()
    try:
        conn.execute(
            """INSERT INTO gaming_agents
                 (agent_id, hostname, agent_version, last_seen_at, current_game_slug,
                  current_game_name, current_started_at, current_seconds, current_idle, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(agent_id) DO UPDATE SET
                 hostname           = excluded.hostname,
                 agent_version      = excluded.agent_version,
                 last_seen_at       = excluded.last_seen_at,
                 current_game_slug  = excluded.current_game_slug,
                 current_game_name  = excluded.current_game_name,
                 current_started_at = excluded.current_started_at,
                 current_seconds    = excluded.current_seconds,
                 current_idle       = excluded.current_idle""",
            (g.agent_id,
             str(body.get("hostname") or "")[:120],
             str(body.get("agent_version") or "")[:32],
             now,
             str(body.get("current_game_slug") or "")[:64],
             str(body.get("current_game_name") or "")[:120],
             (lambda d: d.isoformat() if d else None)(_parse_iso(body.get("current_started_at"))),
             _clean_int(body.get("current_seconds"), 0, 0, MAX_SESSION_SECONDS),
             1 if body.get("current_idle") else 0,
             now),
        )
        conn.commit()
    finally:
        conn.close()
    return jsonify({"ok": True, "server_time": now})


# ─── Dashboard API (owner only) ───────────────────────────────────────────────

@gaming_bp.get("/status")
@require_owner
def status():
    conn = _db()
    try:
        agents = conn.execute(
            "SELECT * FROM gaming_agents ORDER BY last_seen_at DESC"
        ).fetchall()
        live = conn.execute(
            "SELECT * FROM gaming_sessions WHERE in_progress=1 ORDER BY started_at DESC"
        ).fetchall()
    finally:
        conn.close()

    out = []
    for a in agents:
        last_seen = _parse_iso(a["last_seen_at"])
        stale = (datetime.now(timezone.utc) - last_seen).total_seconds() if last_seen else None
        out.append({
            "agent_id": a["agent_id"],
            "hostname": a["hostname"],
            "agent_version": a["agent_version"],
            "last_seen_at": a["last_seen_at"],
            "seconds_since_seen": int(stale) if stale is not None else None,
            # The agent heartbeats every minute; 3 minutes of silence means the
            # PC is off, asleep, or the agent died.
            "online": bool(stale is not None and stale < 180),
            "current_game_slug": a["current_game_slug"],
            "current_game_name": a["current_game_name"],
            "current_started_at": a["current_started_at"],
            "current_seconds": a["current_seconds"],
            "current_idle": bool(a["current_idle"]),
        })
    return jsonify({
        "agents": out,
        "live_sessions": [_session_row_to_dict(r) for r in live],
        "server_time": _now_iso(),
    })


@gaming_bp.get("/sessions")
@require_owner
def list_sessions():
    limit = _clean_int(request.args.get("limit"), 100, 1, 1000)
    offset = _clean_int(request.args.get("offset"), 0, 0, 10_000_000)
    slug = (request.args.get("game") or "").strip().lower()
    frm, to = _range_bounds(request.args)

    where = ["local_date >= ?", "local_date <= ?"]
    params = [frm, to]
    if slug:
        where.append("game_slug = ?")
        params.append(slug)
    clause = " WHERE " + " AND ".join(where)

    conn = _db()
    try:
        total = conn.execute(
            f"SELECT COUNT(*) AS n FROM gaming_sessions{clause}", params
        ).fetchone()["n"]
        rows = conn.execute(
            f"SELECT * FROM gaming_sessions{clause} ORDER BY started_at DESC LIMIT ? OFFSET ?",
            params + [limit, offset],
        ).fetchall()
    finally:
        conn.close()
    return jsonify({
        "sessions": [_session_row_to_dict(r) for r in rows],
        "total": total, "limit": limit, "offset": offset,
        "from": frm, "to": to,
    })


@gaming_bp.delete("/sessions/<int:session_id>")
@require_owner
def delete_session(session_id: int):
    conn = _db()
    try:
        cur = conn.execute("DELETE FROM gaming_sessions WHERE id=?", (session_id,))
        conn.commit()
        deleted = cur.rowcount
    finally:
        conn.close()
    if not deleted:
        return jsonify({"error": "session not found"}), 404
    return jsonify({"ok": True, "deleted": session_id})


CULL_PREVIEW_LIMIT = 300


@gaming_bp.post("/sessions/cull")
@require_owner
def cull_sessions():
    """Apply the work-hours blackout to sessions that predate it.

    POST with no body is a **preview**: nothing is written, and the response
    lists every session that would go and every one that would be shortened.
    Send `{"apply": true}` to carry the same plan out.

        curl -X POST https://api.michaelwegter.com/gaming/sessions/cull \
             -H "Authorization: Bearer $TOKEN"                    # preview
        curl -X POST https://api.michaelwegter.com/gaming/sessions/cull \
             -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
             -d '{"apply": true}'                                 # do it

    Body, all optional: `days` (0 = Monday), `start`, `end`, `before`
    (YYYY-MM-DD local — only sessions from before that day, which is how you
    limit this to history the agent recorded before it knew better),
    `min_seconds`, `apply`.

    Live sessions are skipped: the agent still owns them and would write them
    back on its next checkpoint.
    """
    body = request.get_json(silent=True) or {}
    days = _clean_weekdays(body.get("days"))
    start_minutes = _parse_hhmm(body.get("start"), WORKDAY_DEFAULT_START)
    end_minutes = _parse_hhmm(body.get("end"), WORKDAY_DEFAULT_END)
    min_seconds = _clean_int(body.get("min_seconds"), 60, 0, 24 * 3600)
    before = (body.get("before") or "").strip()
    apply_changes = body.get("apply") is True

    where = ["in_progress = 0", "ended_at IS NOT NULL"]
    params = []
    if len(before) == 10:
        where.append("local_date < ?")
        params.append(before)
    clause = " WHERE " + " AND ".join(where)

    conn = _db()
    try:
        rows = conn.execute(
            f"SELECT * FROM gaming_sessions{clause} ORDER BY started_at", params
        ).fetchall()

        plans = []
        for row in rows:
            plan = _cull_plan_for(row, days, start_minutes, end_minutes, min_seconds)
            if plan is not None:
                plans.append(plan)

        if apply_changes and plans:
            try:
                for plan in plans:
                    if plan["action"] == "delete":
                        conn.execute("DELETE FROM gaming_sessions WHERE id=?", (plan["id"],))
                    else:
                        conn.execute(
                            """UPDATE gaming_sessions
                                  SET started_at=?, ended_at=?, duration_seconds=?,
                                      active_seconds=?, idle_seconds=?, local_date=?,
                                      local_start_hour=?, local_weekday=?, updated_at=?
                                WHERE id=?""",
                            (plan["new_started_at"], plan["new_ended_at"],
                             plan["new_duration_seconds"], plan["new_active_seconds"],
                             plan["new_idle_seconds"], plan["new_local_date"],
                             plan["new_local_start_hour"], plan["new_local_weekday"],
                             _now_iso(), plan["id"]),
                        )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
    finally:
        conn.close()

    deleted = [p for p in plans if p["action"] == "delete"]
    trimmed = [p for p in plans if p["action"] == "trim"]
    return jsonify({
        "ok": True,
        "applied": apply_changes,
        "window": _describe_window(days, start_minutes, end_minutes),
        "before": before or None,
        "scanned": len(rows),
        "affected": len(plans),
        "deleted": len(deleted),
        "trimmed": len(trimmed),
        "seconds_removed": sum(p["seconds_removed"] for p in plans),
        "active_seconds_removed": sum(p["active_removed"] for p in plans),
        "days_touched": sorted({p["local_date"] for p in plans}),
        "sessions": plans[:CULL_PREVIEW_LIMIT],
        "truncated": len(plans) > CULL_PREVIEW_LIMIT,
    })


@gaming_bp.get("/stats")
@require_owner
def stats():
    """Everything the dashboard draws, in one round trip.

    Aggregation happens in SQL over the requested local-date range; the payload
    stays small no matter how many sessions exist.
    """
    frm, to = _range_bounds(request.args)
    conn = _db()
    try:
        games = {r["slug"]: _game_row_to_dict(r)
                 for r in conn.execute("SELECT * FROM gaming_games")}

        # Every number below is ACTIVE time: wall clock minus the stretches with
        # no keyboard or mouse input. A game left sitting on the menu must not
        # out-weigh an evening of actually playing, so idle reaches no total, no
        # average, no chart and no streak. Wall clock is reported once, as
        # `wall_seconds`, and per session in the sessions list.
        totals = conn.execute(
            """SELECT COUNT(*) AS sessions,
                      COALESCE(SUM(active_seconds),0)   AS seconds,
                      COALESCE(SUM(duration_seconds),0) AS wall_seconds,
                      COALESCE(SUM(idle_seconds),0)     AS idle_seconds,
                      COUNT(DISTINCT CASE WHEN active_seconds > 0
                                          THEN local_date END) AS days_played
               FROM gaming_sessions
               WHERE local_date >= ? AND local_date <= ?""",
            (frm, to),
        ).fetchone()

        by_game = conn.execute(
            """SELECT game_slug, game_name,
                      COUNT(*) AS sessions,
                      COALESCE(SUM(active_seconds),0) AS seconds,
                      COALESCE(SUM(active_seconds),0) AS active_seconds,
                      COALESCE(MAX(active_seconds),0) AS longest_session,
                      MAX(started_at)                 AS last_played
               FROM gaming_sessions
               WHERE local_date >= ? AND local_date <= ?
               GROUP BY game_slug ORDER BY seconds DESC""",
            (frm, to),
        ).fetchall()

        by_day_rows = conn.execute(
            """SELECT local_date, game_slug,
                      COALESCE(SUM(active_seconds),0) AS seconds,
                      COUNT(*) AS sessions
               FROM gaming_sessions
               WHERE local_date >= ? AND local_date <= ?
               GROUP BY local_date, game_slug ORDER BY local_date""",
            (frm, to),
        ).fetchall()

        by_hour = conn.execute(
            """SELECT local_start_hour AS hour,
                      COALESCE(SUM(active_seconds),0) AS seconds,
                      COUNT(*) AS sessions
               FROM gaming_sessions
               WHERE local_date >= ? AND local_date <= ?
               GROUP BY local_start_hour""",
            (frm, to),
        ).fetchall()

        by_weekday = conn.execute(
            """SELECT local_weekday AS weekday,
                      COALESCE(SUM(active_seconds),0) AS seconds,
                      COUNT(*) AS sessions
               FROM gaming_sessions
               WHERE local_date >= ? AND local_date <= ?
               GROUP BY local_weekday""",
            (frm, to),
        ).fetchall()

        recent = conn.execute(
            """SELECT * FROM gaming_sessions
               WHERE local_date >= ? AND local_date <= ?
               ORDER BY started_at DESC LIMIT 10""",
            (frm, to),
        ).fetchall()

        # Streaks are computed over ALL history, not the selected window — a
        # 40-day streak shouldn't read as 30 just because you're looking at a month.
        # A day you merely left a game running is not a day you played, so it
        # neither counts nor keeps a streak alive.
        all_days = [r["local_date"] for r in conn.execute(
            "SELECT DISTINCT local_date FROM gaming_sessions "
            "WHERE local_date != '' AND active_seconds > 0 ORDER BY local_date"
        )]
        lifetime = conn.execute(
            """SELECT COUNT(*) AS sessions,
                      COALESCE(SUM(active_seconds),0) AS seconds,
                      MIN(CASE WHEN active_seconds > 0 THEN local_date END) AS first_day
               FROM gaming_sessions"""
        ).fetchone()
    finally:
        conn.close()

    # Dense day series: every date in the range, including zero days, so the
    # chart shows the gaps instead of silently compressing them.
    start_d = datetime.strptime(frm, "%Y-%m-%d").date()
    end_d = datetime.strptime(to, "%Y-%m-%d").date()
    per_day = {}
    for r in by_day_rows:
        entry = per_day.setdefault(r["local_date"], {"seconds": 0, "sessions": 0, "games": {}})
        entry["seconds"] += r["seconds"]
        entry["sessions"] += r["sessions"]
        entry["games"][r["game_slug"]] = r["seconds"]
    days = []
    cursor = start_d
    while cursor <= end_d:
        key = cursor.isoformat()
        d = per_day.get(key, {"seconds": 0, "sessions": 0, "games": {}})
        days.append({"date": key, "seconds": d["seconds"],
                     "sessions": d["sessions"], "games": d["games"]})
        cursor += timedelta(days=1)

    hours = [{"hour": h, "seconds": 0, "sessions": 0} for h in range(24)]
    for r in by_hour:
        hours[r["hour"]] = {"hour": r["hour"], "seconds": r["seconds"], "sessions": r["sessions"]}
    weekdays = [{"weekday": w, "seconds": 0, "sessions": 0} for w in range(7)]
    for r in by_weekday:
        weekdays[r["weekday"]] = {"weekday": r["weekday"], "seconds": r["seconds"],
                                  "sessions": r["sessions"]}

    current_streak, longest_streak = _streaks(all_days)
    span_days = (end_d - start_d).days + 1

    return jsonify({
        "range": {"from": frm, "to": to, "days": span_days},
        "totals": {
            "sessions": totals["sessions"],
            "seconds": totals["seconds"],             # played
            "active_seconds": totals["seconds"],      # the same figure, named plainly
            "wall_seconds": totals["wall_seconds"],   # games open, idle included
            "idle_seconds": totals["idle_seconds"],
            "days_played": totals["days_played"],
            "avg_seconds_per_day": round(totals["seconds"] / span_days) if span_days else 0,
            "avg_seconds_per_played_day": (
                round(totals["seconds"] / totals["days_played"]) if totals["days_played"] else 0
            ),
            "avg_session_seconds": (
                round(totals["seconds"] / totals["sessions"]) if totals["sessions"] else 0
            ),
        },
        "lifetime": {
            "sessions": lifetime["sessions"],
            "seconds": lifetime["seconds"],
            "first_day": lifetime["first_day"],
            "current_streak": current_streak,
            "longest_streak": longest_streak,
        },
        "by_game": [{
            "slug": r["game_slug"],
            "name": r["game_name"] or games.get(r["game_slug"], {}).get("name", r["game_slug"]),
            "color": games.get(r["game_slug"], {}).get("color", "#6b7280"),
            "icon": games.get(r["game_slug"], {}).get("icon", "🎮"),
            "sessions": r["sessions"],
            "seconds": r["seconds"],
            "active_seconds": r["active_seconds"],
            "longest_session": r["longest_session"],
            "last_played": r["last_played"],
        } for r in by_game],
        "by_day": days,
        "by_hour": hours,
        "by_weekday": weekdays,
        "recent_sessions": [_session_row_to_dict(r) for r in recent],
    })


def _streaks(sorted_days):
    """(current, longest) consecutive-day streaks. `sorted_days` is ascending
    YYYY-MM-DD strings. The current streak survives 'haven't played yet today'
    — it only breaks once a full day has passed with nothing played."""
    if not sorted_days:
        return 0, 0
    dates = []
    for s in sorted_days:
        try:
            dates.append(datetime.strptime(s, "%Y-%m-%d").date())
        except ValueError:
            continue
    if not dates:
        return 0, 0
    longest = run = 1
    for prev, cur in zip(dates, dates[1:]):
        run = run + 1 if (cur - prev).days == 1 else 1
        longest = max(longest, run)
    today = datetime.now(timezone.utc).date()
    gap = (today - dates[-1]).days
    current = run if gap <= 1 else 0
    return current, longest


# ─── Game registry management (owner only) ────────────────────────────────────

@gaming_bp.get("/games")
@require_owner
def list_games():
    conn = _db()
    try:
        rows = conn.execute("SELECT * FROM gaming_games ORDER BY name").fetchall()
    finally:
        conn.close()
    return jsonify({"games": [_game_row_to_dict(r) for r in rows]})


def _clean_process_names(value):
    if isinstance(value, str):
        value = [p for p in re.split(r"[,\n]", value)]
    if not isinstance(value, list):
        return None
    out = []
    for item in value:
        name = str(item or "").strip()
        if not name:
            continue
        # Accept "RocketLeague" or "RocketLeague.exe"; store with the extension
        # so matching is a simple case-insensitive equality on the agent side.
        if not name.lower().endswith(".exe"):
            name += ".exe"
        if name not in out:
            out.append(name[:120])
    return out[:20]


def _next_color(conn):
    """The first palette slot no game is using; falls back to cycling once every
    slot is taken (at seven-plus games, a repeat beats an unvalidated hue)."""
    used = {r["color"] for r in conn.execute("SELECT color FROM gaming_games")}
    for color in CHART_PALETTE:
        if color not in used:
            return color
    count = conn.execute("SELECT COUNT(*) AS n FROM gaming_games").fetchone()["n"]
    return CHART_PALETTE[count % len(CHART_PALETTE)]


@gaming_bp.post("/games")
@require_owner
def add_game():
    body = request.get_json(silent=True) or {}
    name = str(body.get("name") or "").strip()[:120]
    if not name:
        return jsonify({"error": "name is required"}), 400
    slug = str(body.get("slug") or "").strip().lower()
    if not slug:
        slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:64]
    if not SLUG_RE.match(slug):
        return jsonify({"error": "slug must be lowercase letters, numbers and hyphens"}), 400
    procs = _clean_process_names(body.get("process_names"))
    if not procs:
        return jsonify({"error": "process_names must contain at least one executable name"}), 400

    now = _now_iso()
    conn = _db()
    try:
        color = str(body.get("color") or "").strip()[:32] or _next_color(conn)
        try:
            conn.execute(
                """INSERT INTO gaming_games
                     (slug, name, process_names, color, icon, enabled,
                      min_session_seconds, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (slug, name, json.dumps(procs), color,
                 str(body.get("icon") or "🎮")[:8],
                 0 if body.get("enabled") is False else 1,
                 _clean_int(body.get("min_session_seconds"), 60, 0, 3600),
                 now, now),
            )
        except sqlite3.IntegrityError:
            return jsonify({"error": f"a game with slug '{slug}' already exists"}), 409
        conn.commit()
        row = conn.execute("SELECT * FROM gaming_games WHERE slug=?", (slug,)).fetchone()
    finally:
        conn.close()
    return jsonify({"game": _game_row_to_dict(row)}), 201


@gaming_bp.patch("/games/<slug>")
@require_owner
def edit_game(slug: str):
    body = request.get_json(silent=True) or {}
    sets, params = [], []
    if "name" in body:
        name = str(body.get("name") or "").strip()[:120]
        if not name:
            return jsonify({"error": "name cannot be empty"}), 400
        sets.append("name=?"); params.append(name)
    if "process_names" in body:
        procs = _clean_process_names(body.get("process_names"))
        if not procs:
            return jsonify({"error": "process_names must contain at least one executable name"}), 400
        sets.append("process_names=?"); params.append(json.dumps(procs))
    if "color" in body:
        sets.append("color=?"); params.append(str(body.get("color") or "#6b7280")[:32])
    if "icon" in body:
        sets.append("icon=?"); params.append(str(body.get("icon") or "🎮")[:8])
    if "enabled" in body:
        sets.append("enabled=?"); params.append(1 if body.get("enabled") else 0)
    if "min_session_seconds" in body:
        sets.append("min_session_seconds=?")
        params.append(_clean_int(body.get("min_session_seconds"), 60, 0, 3600))
    if not sets:
        return jsonify({"error": "nothing to update"}), 400

    sets.append("updated_at=?"); params.append(_now_iso())
    params.append(slug)
    conn = _db()
    try:
        cur = conn.execute(f"UPDATE gaming_games SET {', '.join(sets)} WHERE slug=?", params)
        conn.commit()
        if not cur.rowcount:
            return jsonify({"error": "game not found"}), 404
        row = conn.execute("SELECT * FROM gaming_games WHERE slug=?", (slug,)).fetchone()
    finally:
        conn.close()
    return jsonify({"game": _game_row_to_dict(row)})


@gaming_bp.delete("/games/<slug>")
@require_owner
def delete_game(slug: str):
    """Removes the game from tracking. Recorded sessions are kept — deleting a
    game should not rewrite history."""
    conn = _db()
    try:
        cur = conn.execute("DELETE FROM gaming_games WHERE slug=?", (slug,))
        conn.commit()
        if not cur.rowcount:
            return jsonify({"error": "game not found"}), 404
    finally:
        conn.close()
    return jsonify({"ok": True, "deleted": slug})


@gaming_bp.get("/candidates")
@require_owner
def list_candidates():
    """Unrecognised processes the agent saw, most recent first — the raw
    material for 'add this game' without hunting through Task Manager."""
    conn = _db()
    try:
        rows = conn.execute(
            """SELECT c.* FROM gaming_candidates c
               WHERE c.hidden=0
                 AND NOT EXISTS (
                   SELECT 1 FROM gaming_games gg
                   WHERE lower(gg.process_names) LIKE '%' || lower(c.process_name) || '%')
               ORDER BY c.last_seen DESC LIMIT 100"""
        ).fetchall()
    finally:
        conn.close()
    return jsonify({"candidates": [{
        "process_name": r["process_name"],
        "first_seen": r["first_seen"],
        "last_seen": r["last_seen"],
        "seen_count": r["seen_count"],
    } for r in rows]})


@gaming_bp.post("/candidates/<path:process_name>/hide")
@require_owner
def hide_candidate(process_name: str):
    conn = _db()
    try:
        conn.execute("UPDATE gaming_candidates SET hidden=1 WHERE process_name=?",
                     (process_name,))
        conn.commit()
    finally:
        conn.close()
    return jsonify({"ok": True, "hidden": process_name})
