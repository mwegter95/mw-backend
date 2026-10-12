"""
Inbox — Flask Blueprint
Mounts under /inbox on the mw-backend server.

Backs the private inbox dashboard at michaelwegter.com/apps/inbox: a calmer
way to read Gmail that is wired into the Gmail triage Apps Script
(email-automate-google-script). The script stays the only thing that touches
Gmail; this blueprint keeps the dashboard's own state and runs triage batches.

  Browser ──JWT──▶ /inbox/* ──signed JSON──▶ Apps Script web app ──▶ Gmail
                      ▲                              │
                      └──── /inbox/ingest ◀──signed──┘  (batches applied from the Sheet)

WHAT LIVES HERE (tables in data/mw.db, created lazily)
  inbox_messages  every email the dashboard knows: triage result (summary,
                  labels, importance, personal), Gmail state, and dashboard
                  state (read in the dashboard, pinned)
  inbox_batches   triage runs: AI, ChatGPT paste, Sheet, history import
  inbox_events    read / unread / archive / pin / sorted, by local day, for
                  the daily counts
  inbox_bodies    cached full threads (HTML), so re-opening is instant
  inbox_settings  prompt, people, batch size and other preferences

TRIAGE (every path ends in the script's same apply core; this server never
calls an AI itself)
  1. Copy for Claude   /triage/export gives the prompt + emails to copy into
                       Claude; the JSON Claude answers is pasted back into
                       /triage/apply.
  2. wegter-pc's AI    the browser carries the same export, a few emails at a
                       time, to LM Studio on wegter-pc (inbox_local_ai.py,
                       served at jobs.michaelwegter.com/jobs/local-ai) and
                       posts the results to /triage/apply.
  3. The Sheet / old web page keep working; the script reports to /ingest.
  Either of the first two can stop for a review before anything changes.

READ, UNREAD AND COUNTS
  Triage marks mail read in Gmail, so Gmail's read state can't say whether
  Michael has actually looked at something. "Read" here means opened in the
  dashboard (read_at). Mark unread clears it and marks the thread unread in
  Gmail too. Highlights that triage keeps in the inbox are left unread in
  Gmail until they're opened here or marked read (keep_highlights_unread).
  Daily counts come from inbox_events in local time (INBOX_TZ).

AUTH: same as gaming_blueprint.py — the shared JWT secret (data/.secret_key)
and the shared users table; only INBOX_OWNER_EMAIL may use any endpoint
except /health and the HMAC-signed /ingest.

Environment (.env on the Surface)
  GMAIL_SCRIPT_URL      the triage web app's /exec URL
  GMAIL_SCRIPT_SECRET   the secret setupDashboard() printed in Apps Script
  INBOX_OWNER_EMAIL     defaults to zweetztuph@gmail.com
  INBOX_TZ              defaults to America/Chicago
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from functools import wraps
from pathlib import Path

import jwt as _jwt
from flask import Blueprint, g, jsonify, request

import inbox_core as core
from inbox_core import ScriptClient, ScriptError

log = logging.getLogger(__name__)

inbox_bp = Blueprint("inbox", __name__, url_prefix="/inbox")

DATA_DIR = Path(__file__).parent / "data"
DB_PATH = DATA_DIR / "mw.db"
SECRET_FILE = DATA_DIR / ".secret_key"

OWNER_EMAIL = (os.environ.get("INBOX_OWNER_EMAIL") or "zweetztuph@gmail.com").strip().lower()
SCRIPT_URL = os.environ.get("GMAIL_SCRIPT_URL", "").strip()
SCRIPT_SECRET = os.environ.get("GMAIL_SCRIPT_SECRET", "").strip()

try:
    from zoneinfo import ZoneInfo
    LOCAL_TZ = ZoneInfo(os.environ.get("INBOX_TZ") or "America/Chicago")
except Exception:  # Windows without the tzdata package
    log.warning("[inbox] INBOX_TZ unavailable (pip install tzdata); using UTC-5")
    LOCAL_TZ = timezone(timedelta(hours=-5))

HIGHLIGHT_DAYS = 14             # highlights older than this drop off once read
RECENT_DAYS = 7                 # "Recently sorted" covers this many days of sorts
BODY_CACHE_FRESH_SECONDS = 1800  # re-fetch a cached thread after 30 minutes
BODY_CACHE_KEEP_DAYS = 30
LIVE_CACHE_SECONDS = 45
STATUS_CACHE_SECONDS = 60
STALE_BATCH_MINUTES = 20        # a batch "running" longer than this died with the process
MAX_IDS_PER_ACTION = 100
PREFETCH_BODIES = 12            # bodies fetched ahead after a batch (highlights first)

DEFAULT_SETTINGS = {
    "prompt": "",                   # '' = core.DEFAULT_PROMPT
    "people": [],                   # [{match, name, kind}]
    "labels": [],                   # explicit label list for the prompt; [] = detect
    "batch_size": 50,
    "query": "",                    # '' = the script's QUERY
    "review_first": True,           # wegter-pc's sorts wait for a look before applying
    "keep_people_in_inbox": True,
    "keep_highlights_unread": True,  # highlights kept in the inbox stay unread until opened here
    "block_images": False,
    "rollup_imported": 0,           # rows of the Rollup sheet already imported
    "last_export": {},              # id → {from, subject, date, snippet} of the latest export
}

ACTIONS = {
    # action: (Gmail op for the script or None, event kind)
    "read": ("read", "read"),
    "unread": ("unread", "unread"),
    "pin": (None, "pin"),
    "unpin": (None, "unpin"),
    "archive": ("archive", "archive"),
    "inbox": ("inbox", "inbox"),
    "star": ("star", "star"),
    "unstar": ("unstar", "unstar"),
    "trash": ("trash", "trash"),
    "add_labels": ("add_labels", "label"),
    "remove_labels": ("remove_labels", "label"),
}

_secret_cache = None
_tables_ready = False
_tables_lock = threading.Lock()
_triage_lock = threading.Lock()   # one triage batch at a time
_cache: dict[str, tuple[float, object]] = {}


# ─── Infrastructure ───────────────────────────────────────────────────────────

def get_script_client() -> ScriptClient:
    """Tests replace this with a fake."""
    return ScriptClient(SCRIPT_URL, SCRIPT_SECRET)


def spawn(fn, *args):
    """Background work (tests run it inline)."""
    t = threading.Thread(target=_guarded, args=(fn, *args), daemon=True, name=f"inbox-{fn.__name__}")
    t.start()
    return t


def _guarded(fn, *args):
    try:
        fn(*args)
    except Exception:
        log.exception("[inbox] background job %s failed", fn.__name__)


def _shared_secret():
    global _secret_cache
    if _secret_cache is None:
        try:
            _secret_cache = SECRET_FILE.read_text().strip()
        except Exception:
            _secret_cache = None
    return _secret_cache


def _db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime | None = None) -> str:
    return (dt or _now()).astimezone(timezone.utc).replace(microsecond=0).isoformat()


def _local_day(dt: datetime | None = None) -> str:
    return (dt or _now()).astimezone(LOCAL_TZ).strftime("%Y-%m-%d")


def _parse_iso(value) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _cached(key: str, ttl: float):
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    return None


def _remember(key: str, value):
    _cache[key] = (time.time(), value)
    return value


def _forget(prefix: str):
    for key in [k for k in _cache if k.startswith(prefix)]:
        _cache.pop(key, None)


def _marks(values) -> str:
    """Placeholders for an IN (...) list; NULL matches nothing when empty."""
    return ",".join("?" * len(values)) or "NULL"


SCHEMA = """
CREATE TABLE IF NOT EXISTS inbox_messages (
    thread_id     TEXT PRIMARY KEY,
    message_id    TEXT NOT NULL DEFAULT '',
    from_name     TEXT NOT NULL DEFAULT '',
    from_email    TEXT NOT NULL DEFAULT '',
    subject       TEXT NOT NULL DEFAULT '',
    snippet       TEXT NOT NULL DEFAULT '',
    summary       TEXT NOT NULL DEFAULT '',
    labels        TEXT NOT NULL DEFAULT '[]',
    importance    TEXT NOT NULL DEFAULT '',
    personal      INTEGER NOT NULL DEFAULT 0,
    in_inbox      INTEGER NOT NULL DEFAULT 1,
    gmail_unread  INTEGER NOT NULL DEFAULT 0,
    starred       INTEGER NOT NULL DEFAULT 0,
    message_count INTEGER NOT NULL DEFAULT 1,
    email_date    TEXT NOT NULL DEFAULT '',
    sorted_at     TEXT,
    sorted_day    TEXT NOT NULL DEFAULT '',
    batch_id      INTEGER,
    source        TEXT NOT NULL DEFAULT '',
    read_at       TEXT,
    pinned_at     TEXT,
    trashed       INTEGER NOT NULL DEFAULT 0,
    updated_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_inbox_messages_sorted ON inbox_messages(sorted_at);
CREATE INDEX IF NOT EXISTS idx_inbox_messages_date   ON inbox_messages(email_date);
CREATE INDEX IF NOT EXISTS idx_inbox_messages_pinned ON inbox_messages(pinned_at);

CREATE TABLE IF NOT EXISTS inbox_batches (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at  TEXT NOT NULL,
    finished_at TEXT,
    day         TEXT NOT NULL DEFAULT '',
    source      TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'running',
    step        TEXT NOT NULL DEFAULT '',
    total       INTEGER NOT NULL DEFAULT 0,
    applied     INTEGER NOT NULL DEFAULT 0,
    archived    INTEGER NOT NULL DEFAULT 0,
    important   INTEGER NOT NULL DEFAULT 0,
    personal    INTEGER NOT NULL DEFAULT 0,
    missing     INTEGER NOT NULL DEFAULT 0,
    errored     INTEGER NOT NULL DEFAULT 0,
    summary     TEXT NOT NULL DEFAULT '',
    message     TEXT NOT NULL DEFAULT '',
    error       TEXT NOT NULL DEFAULT '',
    proposal    TEXT
);

CREATE TABLE IF NOT EXISTS inbox_events (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    at        TEXT NOT NULL,
    day       TEXT NOT NULL,
    kind      TEXT NOT NULL,
    thread_id TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_inbox_events_day ON inbox_events(day, kind);

CREATE TABLE IF NOT EXISTS inbox_bodies (
    thread_id  TEXT PRIMARY KEY,
    fetched_at TEXT NOT NULL,
    data       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS inbox_settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def _ensure_tables():
    global _tables_ready
    if _tables_ready:
        return
    with _tables_lock:
        if _tables_ready:
            return
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        conn = _db()
        try:
            conn.executescript(SCHEMA)
            # Batches left "running" by a restart can never finish now.
            cutoff = _iso(_now() - timedelta(minutes=STALE_BATCH_MINUTES))
            conn.execute(
                "UPDATE inbox_batches SET status='failed', error='The server restarted while this was running.', "
                "finished_at=? WHERE status IN ('running','applying') AND created_at < ?", (_iso(), cutoff))
            conn.execute("DELETE FROM inbox_bodies WHERE fetched_at < ?",
                         (_iso(_now() - timedelta(days=BODY_CACHE_KEEP_DAYS)),))
            conn.commit()
        finally:
            conn.close()
        _tables_ready = True


# ─── Auth ─────────────────────────────────────────────────────────────────────

def require_owner(f):
    """The shared users table + shared JWT secret, then: owner only."""
    @wraps(f)
    def wrapper(*args, **kwargs):
        secret = _shared_secret()
        if not secret:
            return jsonify({"error": "Auth temporarily unavailable"}), 503
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return jsonify({"error": "Sign in to open your inbox"}), 401
        try:
            payload = _jwt.decode(auth[7:], secret, algorithms=["HS256"])
        except _jwt.ExpiredSignatureError:
            return jsonify({"error": "Session expired. Sign in again."}), 401
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
        if (row["email"] or "").strip().lower() != OWNER_EMAIL:
            return jsonify({"error": "This inbox is private"}), 403
        g.user_id = user_id
        _ensure_tables()
        return f(*args, **kwargs)
    return wrapper


# Never answer a Gmail/Apps Script failure with 502 or 504: Cloudflare swaps any
# 502/504 from this server for its own branded error page, which carries no CORS
# headers, so the browser reports a CORS error and the real reason is lost.
# 424 Failed Dependency passes through Cloudflare untouched.
UPSTREAM_FAILED = 424


def _script_error_response(e: ScriptError):
    status = 404 if e.code == "not_found" else UPSTREAM_FAILED
    return jsonify({"error": str(e), "code": e.code}), status


# ─── Settings ─────────────────────────────────────────────────────────────────

def load_settings(conn=None) -> dict:
    own = conn is None
    conn = conn or _db()
    try:
        rows = conn.execute("SELECT key, value FROM inbox_settings").fetchall()
    finally:
        if own:
            conn.close()
    out = dict(DEFAULT_SETTINGS)
    for r in rows:
        if r["key"] in DEFAULT_SETTINGS:
            try:
                out[r["key"]] = json.loads(r["value"])
            except ValueError:
                pass
    out["people"] = core.normalize_people(out.get("people"))
    return out


def save_settings(changes: dict, conn) -> None:
    for key, value in changes.items():
        conn.execute("INSERT INTO inbox_settings(key, value) VALUES(?, ?) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, json.dumps(value)))


def _clean_settings(body: dict) -> dict:
    out = {}
    if "prompt" in body:
        prompt = str(body.get("prompt") or "").strip()
        out["prompt"] = "" if prompt == core.DEFAULT_PROMPT.strip() else prompt[:20000]
    if "people" in body:
        out["people"] = core.normalize_people(body.get("people"))
    if "labels" in body:
        labels = body.get("labels") if isinstance(body.get("labels"), list) else []
        out["labels"] = [str(x).strip()[:120] for x in labels if str(x).strip()][:200]
    if "batch_size" in body:
        out["batch_size"] = max(1, min(100, _int(body.get("batch_size"), 50)))
    if "query" in body:
        out["query"] = str(body.get("query") or "").strip()[:500]
    for key in ("review_first", "keep_people_in_inbox", "keep_highlights_unread", "block_images"):
        if key in body:
            out[key] = bool(body.get(key))
    return out


def _int(value, default):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def triage_labels(settings: dict, conn) -> list[str]:
    """Labels offered to the model: the explicit list, else every label triage
    has used before plus Gmail labels in the 'A. Something' style."""
    if settings.get("labels"):
        return settings["labels"]
    seen = {}
    for r in conn.execute("SELECT labels FROM inbox_messages WHERE sorted_at IS NOT NULL "
                          "ORDER BY sorted_at DESC LIMIT 3000"):
        for name in _json_list(r["labels"]):
            seen[name] = seen.get(name, 0) + 1
    gmail = _cached("gmail-labels", 600) or []
    for name in gmail:
        if len(name) > 2 and name[0].isalnum() and name[1] == "." and name not in seen:
            seen[name] = 0
    return sorted(seen)[:150]


def _json_list(text) -> list:
    try:
        value = json.loads(text or "[]")
        return value if isinstance(value, list) else []
    except ValueError:
        return []


# ─── Messages ─────────────────────────────────────────────────────────────────

def _looks_unread(r) -> bool:
    """Unopened mail that's still waiting on Michael: in the inbox, a highlight, or unread in
    Gmail. Mail triage archived and marked read doesn't count, opened or not."""
    if r["read_at"] or (not r["sorted_at"] and not r["gmail_unread"]):
        return False
    return bool(r["in_inbox"] or r["gmail_unread"] or r["personal"] or r["importance"] == "high")


def _msg_dict(r) -> dict:
    sorted_at = r["sorted_at"]
    read = bool(r["read_at"]) or (not sorted_at and not r["gmail_unread"])
    return {
        "id": r["thread_id"],
        "message_id": r["message_id"],
        "from_name": r["from_name"],
        "from_email": r["from_email"],
        "subject": r["subject"],
        "snippet": r["snippet"],
        "summary": r["summary"],
        "labels": _json_list(r["labels"]),
        "importance": r["importance"],
        "personal": bool(r["personal"]),
        "in_inbox": bool(r["in_inbox"]),
        "gmail_unread": bool(r["gmail_unread"]),
        "starred": bool(r["starred"]),
        "message_count": r["message_count"],
        "date": r["email_date"],
        "sorted_at": sorted_at,
        "source": r["source"],
        "batch_id": r["batch_id"],
        "read": read,
        "read_at": r["read_at"],
        "pinned": bool(r["pinned_at"]),
        "pinned_at": r["pinned_at"],
    }


def _event(conn, kind: str, thread_id: str = "", at: datetime | None = None, once_per_day=False):
    at = at or _now()
    day = _local_day(at)
    if once_per_day and conn.execute(
            "SELECT 1 FROM inbox_events WHERE day=? AND kind=? AND thread_id=? LIMIT 1",
            (day, kind, thread_id)).fetchone():
        return
    conn.execute("INSERT INTO inbox_events(at, day, kind, thread_id) VALUES(?,?,?,?)",
                 (_iso(at), day, kind, thread_id))


def store_sorted_items(conn, items: list[dict], batch_id: int | None, source: str,
                       people: list[dict], at: datetime | None = None) -> dict:
    """Upsert the per-thread results of an apply. Returns batch counts."""
    at = at or _now()
    counts = {"applied": 0, "archived": 0, "important": 0, "personal": 0}
    for it in items:
        tid = str(it.get("id") or "").strip()
        if not tid:
            continue
        name, email = core.parse_sender(it.get("from", ""))
        personal = bool(it.get("personal")) or bool(core.match_person(email, people))
        importance = str(it.get("importance") or "").lower()
        importance = importance if importance in core.IMPORTANCE_LEVELS else ""
        archived = bool(it.get("archived"))
        gmail_unread = 0 if it.get("markedRead", True) else 1
        conn.execute("""
            INSERT INTO inbox_messages (thread_id, message_id, from_name, from_email, subject, snippet, summary,
                labels, importance, personal, in_inbox, gmail_unread, starred, message_count, email_date,
                sorted_at, sorted_day, batch_id, source, read_at, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,?)
            ON CONFLICT(thread_id) DO UPDATE SET
                message_id=excluded.message_id, from_name=excluded.from_name, from_email=excluded.from_email,
                subject=excluded.subject, snippet=CASE WHEN excluded.snippet!='' THEN excluded.snippet ELSE snippet END,
                summary=excluded.summary, labels=excluded.labels, importance=excluded.importance,
                personal=excluded.personal, in_inbox=excluded.in_inbox, gmail_unread=excluded.gmail_unread,
                starred=MAX(starred, excluded.starred), message_count=excluded.message_count,
                email_date=excluded.email_date, sorted_at=excluded.sorted_at, sorted_day=excluded.sorted_day,
                batch_id=excluded.batch_id, source=excluded.source, trashed=0,
                -- sorted again means new mail arrived in the thread: show it unread
                read_at=CASE WHEN inbox_messages.message_id != excluded.message_id THEN NULL ELSE read_at END,
                updated_at=excluded.updated_at
        """, (tid, str(it.get("messageId") or ""), name[:200], email[:200], str(it.get("subject") or "")[:500],
              str(it.get("snippet") or "")[:400], str(it.get("summary") or "")[:core.MAX_SUMMARY_CHARS],
              json.dumps([str(x) for x in (it.get("labels") or []) if str(x).strip()][:core.MAX_LABELS_PER_EMAIL]),
              importance, int(personal), 0 if archived else 1, gmail_unread, int(bool(it.get("starred"))),
              _int(it.get("messageCount"), 1), str(it.get("date") or ""), _iso(at), _local_day(at),
              batch_id, source, _iso()))
        _event(conn, "sorted", tid, at)
        counts["applied"] += 1
        counts["archived"] += int(archived)
        counts["important"] += int(importance == "high")
        counts["personal"] += int(personal)
    return counts


VIEWS = ("highlights", "recent", "sorted", "pinned", "inbox", "all")


@inbox_bp.get("/messages")
@require_owner
def list_messages():
    view = request.args.get("view", "highlights")
    if view == "unread":  # the old name, from a page loaded before the rename
        view = "recent"
    if view not in VIEWS:
        view = "highlights"
    limit = max(1, min(300, _int(request.args.get("limit"), 120)))
    offset = max(0, _int(request.args.get("offset"), 0))
    label = (request.args.get("label") or "").strip()
    q = (request.args.get("q") or "").strip()
    importance = (request.args.get("importance") or "").strip().lower()

    where, params = ["trashed=0"], []
    order = "email_date DESC"
    if view == "highlights":
        where.append("(importance='high' OR personal=1)")
        where.append("(read_at IS NULL OR COALESCE(sorted_at, email_date) >= ?)")
        params.append(_iso(_now() - timedelta(days=HIGHLIGHT_DAYS)))
    elif view == "recent":
        # What triage filed lately, newest sort first; the page groups it by sort.
        where.append("sorted_at >= ?")
        params.append(_iso(_now() - timedelta(days=RECENT_DAYS)))
        order = "sorted_at DESC, email_date DESC"
    elif view == "sorted":
        where.append("sorted_at IS NOT NULL")
    elif view == "pinned":
        where.append("pinned_at IS NOT NULL")
        order = "pinned_at DESC"
    elif view == "inbox":
        where.append("in_inbox=1")
    if label:
        where.append("labels LIKE ?")
        params.append('%' + json.dumps(label) + '%')  # the quoted JSON string: an exact element
    if importance in core.IMPORTANCE_LEVELS:
        where.append("importance=?")
        params.append(importance)
    if q:
        like = f"%{q.lower()}%"
        where.append("(lower(subject) LIKE ? OR lower(from_name) LIKE ? OR lower(from_email) LIKE ? "
                     "OR lower(summary) LIKE ? OR lower(snippet) LIKE ?)")
        params += [like] * 5
    conn = _db()
    try:
        total = conn.execute(f"SELECT COUNT(*) FROM inbox_messages WHERE {' AND '.join(where)}", params).fetchone()[0]
        rows = conn.execute(
            f"SELECT * FROM inbox_messages WHERE {' AND '.join(where)} ORDER BY {order} LIMIT ? OFFSET ?",
            params + [limit, offset]).fetchall()
    finally:
        conn.close()
    items = [_msg_dict(r) for r in rows]
    if label:  # LIKE is a prefilter; make the label match exact
        items = [m for m in items if label in m["labels"]]
    return jsonify({"view": view, "items": items, "total": total,
                    "next_offset": offset + limit if offset + limit < total else None})


@inbox_bp.get("/summary")
@require_owner
def summary():
    """Everything the dashboard frame needs in one fast call (no Apps Script)."""
    today = _local_day()
    conn = _db()
    try:
        days = []
        start = (_now().astimezone(LOCAL_TZ) - timedelta(days=13)).strftime("%Y-%m-%d")
        counts = {}
        for r in conn.execute(
                "SELECT day, kind, COUNT(DISTINCT thread_id) AS n FROM inbox_events WHERE day >= ? "
                "AND kind IN ('sorted','read','archive','trash') GROUP BY day, kind", (start,)):
            counts.setdefault(r["day"], {})[r["kind"]] = r["n"]
        base = _now().astimezone(LOCAL_TZ)
        for i in range(13, -1, -1):
            d = (base - timedelta(days=i)).strftime("%Y-%m-%d")
            c = counts.get(d, {})
            days.append({"day": d, "sorted": c.get("sorted", 0), "read": c.get("read", 0),
                         "cleared": c.get("archive", 0) + c.get("trash", 0)})
        cutoff = _iso(_now() - timedelta(days=HIGHLIGHT_DAYS))
        highlights_unread = conn.execute(
            "SELECT COUNT(*) FROM inbox_messages WHERE trashed=0 AND read_at IS NULL "
            "AND (importance='high' OR personal=1)").fetchone()[0]
        highlights_total = conn.execute(
            "SELECT COUNT(*) FROM inbox_messages WHERE trashed=0 AND (importance='high' OR personal=1) "
            "AND (read_at IS NULL OR COALESCE(sorted_at, email_date) >= ?)", (cutoff,)).fetchone()[0]
        in_inbox = conn.execute("SELECT COUNT(*) FROM inbox_messages WHERE trashed=0 AND in_inbox=1").fetchone()[0]
        label_counts = {}
        for r in conn.execute("SELECT * FROM inbox_messages WHERE trashed=0 AND sorted_at >= ?",
                              (_iso(_now() - timedelta(days=60)),)):
            waiting = _looks_unread(r)
            for name in _json_list(r["labels"]):
                c = label_counts.setdefault(name, {"name": name, "count": 0, "unread": 0})
                c["count"] += 1
                c["unread"] += int(waiting)
        pins = [_msg_dict(r) for r in conn.execute(
            "SELECT * FROM inbox_messages WHERE pinned_at IS NOT NULL AND trashed=0 ORDER BY pinned_at DESC LIMIT 40")]
        latest = conn.execute("SELECT * FROM inbox_batches WHERE status IN ('done','review') "
                              "ORDER BY id DESC LIMIT 1").fetchone()
        running = conn.execute("SELECT * FROM inbox_batches WHERE status IN ('running','applying') "
                               "ORDER BY id DESC LIMIT 1").fetchone()
        sorted_total = conn.execute("SELECT COUNT(*) FROM inbox_messages WHERE sorted_at IS NOT NULL").fetchone()[0]
        settings = load_settings(conn)
    finally:
        conn.close()
    status = _cached("status", 10 * 60)
    return jsonify({
        "today": next(d for d in days if d["day"] == today),
        "days": days,
        "highlights": {"unread": highlights_unread, "total": highlights_total},
        "in_inbox": in_inbox,
        "sorted_total": sorted_total,
        "labels": sorted(label_counts.values(), key=lambda c: (-c["count"], c["name"]))[:60],
        "pins": pins,
        "latest_batch": _batch_dict(latest) if latest else None,
        "running_batch": _batch_dict(running) if running else None,
        "gmail": status,
        "configured": {"script": get_script_client().configured},
        "settings": {"block_images": settings["block_images"], "review_first": settings["review_first"],
                     "batch_size": settings["batch_size"]},
        "now": _iso(),
        "local_day": today,
    })


@inbox_bp.get("/status")
@require_owner
def gmail_status():
    """Live counts from Gmail (one Apps Script call, cached a minute)."""
    if request.args.get("refresh") != "1":
        hit = _cached("status", STATUS_CACHE_SECONDS)
        if hit:
            return jsonify(hit)
    settings = load_settings()
    try:
        data = get_script_client().call("stats", query=settings["query"] or None, timeout=60)
    except ScriptError as e:
        return _script_error_response(e)
    out = {"inbox_unread": data.get("inboxUnread"), "waiting": data.get("waiting"),
           "waiting_capped": bool(data.get("waitingCapped")), "query": data.get("query"), "checked_at": _iso()}
    return jsonify(_remember("status", out))


@inbox_bp.get("/live")
@require_owner
def live_inbox():
    """The Gmail inbox right now, merged with what the dashboard knows."""
    limit = max(1, min(100, _int(request.args.get("limit"), 50)))
    key = f"live:{limit}"
    data = None if request.args.get("refresh") == "1" else _cached(key, LIVE_CACHE_SECONDS)
    if data is None:
        try:
            data = _remember(key, get_script_client().call("inbox", limit=limit, timeout=90))
        except ScriptError as e:
            return _script_error_response(e)
    items = data.get("items") or []
    conn = _db()
    try:
        people = load_settings(conn)["people"]
        live_ids = []
        for it in items:
            tid = str(it.get("id") or "")
            if not tid:
                continue
            live_ids.append(tid)
            name, email = core.parse_sender(it.get("from", ""))
            row = conn.execute("SELECT gmail_unread, read_at, sorted_at FROM inbox_messages WHERE thread_id=?",
                               (tid,)).fetchone()
            unread = int(bool(it.get("unread")))
            if row is None:
                conn.execute("""INSERT INTO inbox_messages (thread_id, message_id, from_name, from_email, subject,
                    snippet, labels, personal, in_inbox, gmail_unread, starred, message_count, email_date, source,
                    updated_at) VALUES (?,?,?,?,?,?,?,?,1,?,?,?,?,'inbox',?)""",
                    (tid, str(it.get("messageId") or ""), name[:200], email[:200], str(it.get("subject") or "")[:500],
                     str(it.get("snippet") or "")[:400], json.dumps(it.get("labels") or []),
                     int(bool(core.match_person(email, people))), unread, int(bool(it.get("starred"))),
                     _int(it.get("messageCount"), 1), str(it.get("date") or ""), _iso()))
            else:
                # Read on the phone after triage left it unread → count it as read here too.
                read_elsewhere = row["gmail_unread"] and not unread and not row["read_at"]
                conn.execute("""UPDATE inbox_messages SET in_inbox=1, trashed=0, gmail_unread=?, starred=?,
                    message_count=?, email_date=?, snippet=CASE WHEN ?!='' THEN ? ELSE snippet END,
                    read_at=CASE WHEN ? THEN ? ELSE read_at END, updated_at=? WHERE thread_id=?""",
                    (unread, int(bool(it.get("starred"))), _int(it.get("messageCount"), 1),
                     str(it.get("date") or ""), str(it.get("snippet") or ""), str(it.get("snippet") or "")[:400],
                     int(bool(read_elsewhere)), _iso(), _iso(), tid))
        if len(items) < limit:  # we saw the whole inbox: anything else has left it
            conn.execute(f"UPDATE inbox_messages SET in_inbox=0 WHERE in_inbox=1 AND thread_id NOT IN "
                         f"({_marks(live_ids)})", live_ids)
        conn.commit()
        rows = {r["thread_id"]: r for r in conn.execute(
            f"SELECT * FROM inbox_messages WHERE thread_id IN ({_marks(live_ids)})", live_ids)}
    finally:
        conn.close()
    merged = [_msg_dict(rows[t]) for t in live_ids if t in rows]
    if data.get("inboxUnread") is not None:
        status = _cached("status", 10 * 60) or {}
        _remember("status", {**status, "inbox_unread": data.get("inboxUnread"), "checked_at": _iso()})
    return jsonify({"items": merged, "inbox_unread": data.get("inboxUnread"), "complete": len(items) < limit,
                    "fetched_at": _iso()})


# ─── Reading a thread ─────────────────────────────────────────────────────────

def fetch_thread(thread_id: str, refresh: bool = False) -> tuple[dict, bool]:
    """(thread, from_cache). Serves a fresh cache, otherwise asks the script and
    falls back to a stale copy if the script can't be reached."""
    conn = _db()
    try:
        row = conn.execute("SELECT fetched_at, data FROM inbox_bodies WHERE thread_id=?", (thread_id,)).fetchone()
    finally:
        conn.close()
    if row and not refresh:
        fetched = _parse_iso(row["fetched_at"])
        if fetched and (_now() - fetched).total_seconds() < BODY_CACHE_FRESH_SECONDS:
            return json.loads(row["data"]), True
    try:
        data = get_script_client().call("thread", id=thread_id, timeout=90)
    except ScriptError as e:
        if row and e.code != "not_found":
            stale = json.loads(row["data"])
            stale["stale"] = True
            return stale, True
        raise
    thread = data.get("thread") or {}
    conn = _db()
    try:
        conn.execute("INSERT INTO inbox_bodies(thread_id, fetched_at, data) VALUES(?,?,?) "
                     "ON CONFLICT(thread_id) DO UPDATE SET fetched_at=excluded.fetched_at, data=excluded.data",
                     (thread_id, _iso(), json.dumps(thread)))
        conn.execute("UPDATE inbox_messages SET in_inbox=?, starred=?, message_count=?, updated_at=? "
                     "WHERE thread_id=?", (int(bool(thread.get("inInbox"))), int(bool(thread.get("starred"))),
                                           len(thread.get("messages") or []) or 1, _iso(), thread_id))
        conn.commit()
    finally:
        conn.close()
    return thread, False


@inbox_bp.get("/thread/<thread_id>")
@require_owner
def get_thread(thread_id):
    try:
        thread, cached = fetch_thread(thread_id, refresh=request.args.get("refresh") == "1")
    except ScriptError as e:
        if e.code == "not_found":
            conn = _db()
            try:
                conn.execute("UPDATE inbox_messages SET trashed=1 WHERE thread_id=?", (thread_id,))
                conn.commit()
            finally:
                conn.close()
        return _script_error_response(e)
    conn = _db()
    try:
        row = conn.execute("SELECT * FROM inbox_messages WHERE thread_id=?", (thread_id,)).fetchone()
    finally:
        conn.close()
    return jsonify({"thread": thread, "message": _msg_dict(row) if row else None, "cached": cached})


def _prefetch_bodies(thread_ids: list[str]):
    for tid in thread_ids[:PREFETCH_BODIES]:
        try:
            fetch_thread(tid)
        except Exception as e:  # prefetch is a nicety
            log.info("[inbox] prefetch %s skipped: %s", tid, e)


# ─── Actions ──────────────────────────────────────────────────────────────────

@inbox_bp.post("/actions")
@require_owner
def actions():
    body = request.get_json(silent=True) or {}
    action = str(body.get("action") or "")
    if action not in ACTIONS:
        return jsonify({"error": f"Unknown action: {action}"}), 400
    ids = [str(i) for i in (body.get("ids") or []) if str(i).strip()][:MAX_IDS_PER_ACTION]
    if not ids:
        return jsonify({"error": "No emails given"}), 400
    labels = [str(x).strip() for x in (body.get("labels") or []) if str(x).strip()][:10]
    if action in ("add_labels", "remove_labels") and not labels:
        return jsonify({"error": "Choose a label"}), 400

    gmail_op, event_kind = ACTIONS[action]
    conn = _db()
    try:
        rows = {r["thread_id"]: r for r in conn.execute(
            f"SELECT * FROM inbox_messages WHERE thread_id IN ({_marks(ids)})", ids)}
    finally:
        conn.close()

    # Only call Gmail when it would change something there.
    gmail_ids = ids
    if action == "read":
        gmail_ids = [i for i in ids if i not in rows or rows[i]["gmail_unread"]]
    extra = {}
    if action == "unread":
        # Sorted mail marked unread here is waiting for me, not for triage: the script skips it
        # in its exports until new mail arrives in the thread.
        extra["sorted"] = [i for i in gmail_ids if i in rows and rows[i]["sorted_at"]]
    failed = {}
    if gmail_op and gmail_ids:
        try:
            res = get_script_client().call("modify", ids=gmail_ids, action=gmail_op, labels=labels, timeout=90,
                                           **extra)
            failed = {r["id"]: r.get("error", "failed") for r in res.get("results", []) if not r.get("ok")}
        except ScriptError as e:
            if action != "read":  # reading still counts even if Gmail is unreachable
                return _script_error_response(e)
            failed = {}

    now = _iso()
    done = [i for i in ids if i not in failed]
    conn = _db()
    try:
        for tid in done:
            if tid not in rows:
                continue
            r = rows[tid]
            if action == "read":
                conn.execute("UPDATE inbox_messages SET read_at=COALESCE(read_at, ?), gmail_unread=0, updated_at=? "
                             "WHERE thread_id=?", (now, now, tid))
                _event(conn, "read", tid, once_per_day=True)
                continue
            if action == "unread":
                conn.execute("UPDATE inbox_messages SET read_at=NULL, gmail_unread=1, updated_at=? WHERE thread_id=?",
                             (now, tid))
            elif action == "pin":
                conn.execute("UPDATE inbox_messages SET pinned_at=COALESCE(pinned_at, ?), updated_at=? "
                             "WHERE thread_id=?", (now, now, tid))
            elif action == "unpin":
                conn.execute("UPDATE inbox_messages SET pinned_at=NULL, updated_at=? WHERE thread_id=?", (now, tid))
            elif action == "archive":
                conn.execute("UPDATE inbox_messages SET in_inbox=0, read_at=COALESCE(read_at, ?), updated_at=? "
                             "WHERE thread_id=?", (now, now, tid))
            elif action == "inbox":
                conn.execute("UPDATE inbox_messages SET in_inbox=1, trashed=0, updated_at=? WHERE thread_id=?",
                             (now, tid))
            elif action in ("star", "unstar"):
                conn.execute("UPDATE inbox_messages SET starred=?, updated_at=? WHERE thread_id=?",
                             (int(action == "star"), now, tid))
            elif action == "trash":
                conn.execute("UPDATE inbox_messages SET trashed=1, in_inbox=0, pinned_at=NULL, updated_at=? "
                             "WHERE thread_id=?", (now, tid))
            elif action in ("add_labels", "remove_labels"):
                current = _json_list(r["labels"])
                if action == "add_labels":
                    current += [x for x in labels if x not in current]
                else:
                    current = [x for x in current if x not in labels]
                conn.execute("UPDATE inbox_messages SET labels=?, updated_at=? WHERE thread_id=?",
                             (json.dumps(current), now, tid))
            _event(conn, event_kind, tid, once_per_day=action in ("archive", "trash"))
        conn.commit()
        updated = [_msg_dict(r) for r in conn.execute(
            f"SELECT * FROM inbox_messages WHERE thread_id IN ({_marks(ids)})", ids)]
    finally:
        conn.close()
    _forget("live:")
    return jsonify({"action": action, "updated": updated, "failed": failed})


# ─── Settings & labels ────────────────────────────────────────────────────────

@inbox_bp.get("/settings")
@require_owner
def get_settings():
    conn = _db()
    try:
        settings = load_settings(conn)
        detected = triage_labels({**settings, "labels": []}, conn)
    finally:
        conn.close()
    settings.pop("last_export", None)
    return jsonify({**settings, "default_prompt": core.DEFAULT_PROMPT, "prompt_is_default": not settings["prompt"],
                    "detected_labels": detected})


@inbox_bp.put("/settings")
@require_owner
def put_settings():
    changes = _clean_settings(request.get_json(silent=True) or {})
    conn = _db()
    try:
        save_settings(changes, conn)
        conn.commit()
    finally:
        conn.close()
    return get_settings()


@inbox_bp.get("/labels")
@require_owner
def labels():
    gmail = _cached("gmail-labels", 600)
    error = None
    if gmail is None:
        try:
            gmail = _remember("gmail-labels", get_script_client().call("labels", timeout=60).get("labels") or [])
        except ScriptError as e:
            gmail, error = [], str(e)
    conn = _db()
    try:
        used = triage_labels({"labels": []}, conn)
    finally:
        conn.close()
    return jsonify({"labels": sorted(set(gmail) | set(used)), "gmail": gmail, "used": used, "error": error})


# ─── Triage batches ───────────────────────────────────────────────────────────

def _batch_dict(r, with_proposal=False) -> dict:
    out = {k: r[k] for k in ("id", "created_at", "finished_at", "day", "source", "status", "step", "total",
                             "applied", "archived", "important", "personal", "missing", "errored", "summary",
                             "message", "error")}
    if with_proposal:
        out["proposal"] = json.loads(r["proposal"]) if r["proposal"] else None
    return out


def _new_batch(conn, source: str, status="running", step="", total=0) -> int:
    cur = conn.execute("INSERT INTO inbox_batches(created_at, day, source, status, step, total) VALUES(?,?,?,?,?,?)",
                       (_iso(), _local_day(), source, status, step, total))
    return cur.lastrowid


def _update_batch(batch_id: int, **fields):
    if not fields:
        return
    conn = _db()
    try:
        conn.execute(f"UPDATE inbox_batches SET {', '.join(f'{k}=?' for k in fields)} WHERE id=?",
                     list(fields.values()) + [batch_id])
        conn.commit()
    finally:
        conn.close()


def _busy_batch(conn):
    return conn.execute("SELECT id FROM inbox_batches WHERE status IN ('running','applying') "
                        "ORDER BY id DESC LIMIT 1").fetchone()


def _export(settings: dict, limit: int | None = None) -> list[dict]:
    args = {"limit": limit or settings["batch_size"]}
    if settings.get("query"):
        args["query"] = settings["query"]
    return get_script_client().call("export", timeout=120, **args).get("items") or []


def _prompt_for(settings: dict, conn) -> str:
    return core.fill_prompt(settings.get("prompt") or core.DEFAULT_PROMPT, triage_labels(settings, conn),
                            settings["people"])


def _apply(batch_id: int, actions: list[dict], summary: str, source: str):
    """Send actions to the script's apply core and record the result."""
    _update_batch(batch_id, status="applying", total=len(actions),
                  step=f"Applying labels to {len(actions)} emails in Gmail")
    settings = load_settings()
    # Decided here, after any review edits, so what's archived and what stays unread always agree.
    actions = core.unread_rule(actions, settings["keep_highlights_unread"])
    result = get_script_client().call("apply", actions=actions, summary=summary, timeout=330)
    items = result.get("items") or []
    conn = _db()
    try:
        counts = store_sorted_items(conn, items, batch_id, source, settings["people"])
        conn.execute("""UPDATE inbox_batches SET status='done', step='', applied=?, archived=?, important=?,
            personal=?, missing=?, errored=?, summary=?, message=?, finished_at=? WHERE id=?""",
                     (counts["applied"], counts["archived"], counts["important"], counts["personal"],
                      _int(result.get("missing"), 0), _int(result.get("errored"), 0),
                      result.get("summary") or summary, result.get("message") or "", _iso(), batch_id))
        conn.commit()
    finally:
        conn.close()
    _forget("status")
    _forget("live:")
    keep = [it["id"] for it in items if it.get("importance") == "high" or it.get("personal")]
    if keep:
        spawn(_prefetch_bodies, keep)


def run_apply_batch(batch_id: int, actions: list[dict], summary: str, source: str):
    if not _triage_lock.acquire(blocking=False):
        _update_batch(batch_id, status="failed", error="Another sort is already running.", finished_at=_iso())
        return
    try:
        _apply(batch_id, actions, summary, source)
    except ScriptError as e:
        _update_batch(batch_id, status="failed", step="", error=str(e), finished_at=_iso())
    except Exception as e:
        log.exception("[inbox] apply batch %s failed", batch_id)
        _update_batch(batch_id, status="failed", step="", error=f"{e.__class__.__name__}: {e}", finished_at=_iso())
    finally:
        _triage_lock.release()


def _start(source: str, target, *args, step="Starting", total=0):
    conn = _db()
    try:
        busy = _busy_batch(conn)
        if busy:
            return None, busy["id"]
        batch_id = _new_batch(conn, source, step=step, total=total)
        conn.commit()
    finally:
        conn.close()
    spawn(target, batch_id, *args)
    return batch_id, None


def _batch_response(batch_id: int, status=202):
    conn = _db()
    try:
        row = conn.execute("SELECT * FROM inbox_batches WHERE id=?", (batch_id,)).fetchone()
    finally:
        conn.close()
    return jsonify({"batch": _batch_dict(row, with_proposal=True)}), status


SORT_SOURCES = ("claude", "wegter-pc")
EXPORT_FIELDS = ("from", "subject", "date", "snippet")


@inbox_bp.post("/triage/export")
@require_owner
def triage_export():
    """The next unread emails, ready to sort: `text` to paste into Claude, or `prompt` + `blocks`
    (one per email) for wegter-pc's AI. The senders are kept so the people rules can be applied
    to whatever comes back."""
    body = request.get_json(silent=True) or {}
    conn = _db()
    try:
        settings = load_settings(conn)
        prompt = _prompt_for(settings, conn)
    finally:
        conn.close()
    try:
        items = _export(settings, max(1, min(100, _int(body.get("limit"), settings["batch_size"]))))
    except ScriptError as e:
        return _script_error_response(e)
    conn = _db()
    try:
        save_settings({"last_export": {it["id"]: {"from": it.get("from", ""), "subject": it.get("subject", ""),
                                                  "date": it.get("date", ""), "snippet": (it.get("body") or "")[:240]}
                                       for it in items}}, conn)
        conn.commit()
    finally:
        conn.close()
    return jsonify({
        "count": len(items),
        "text": core.paste_text(prompt, items, LOCAL_TZ) if items else "",
        "prompt": prompt,
        "blocks": core.email_blocks(items, LOCAL_TZ),
        "items": [{k: it.get(k) for k in ("id", "from", "subject", "date")} for it in items],
    })


@inbox_bp.post("/triage/apply")
@require_owner
def triage_apply():
    """Apply sorting results — the JSON pasted back from Claude ({json}), or wegter-pc's
    results ({actions, summary}). `source` is 'claude' or 'wegter-pc'. With `review`, nothing
    changes in Gmail until the review is applied (/triage/batches/<id>/apply)."""
    body = request.get_json(silent=True) or {}
    source = body.get("source") if body.get("source") in SORT_SOURCES else "claude"
    try:
        if isinstance(body.get("actions"), list):
            actions = [a for a in (core.normalize_action(x) for x in body["actions"]) if a]
            summary = str(body.get("summary") or "").strip()[:2000]
            if not actions:
                raise ValueError("No sorted emails came back.")
        else:
            actions, summary = core.parse_actions(str(body.get("json") or ""))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    conn = _db()
    try:
        if _busy_batch(conn):
            return jsonify({"error": "A sort is already running"}), 409
        settings = load_settings(conn)
        known = settings.get("last_export") or {}
        senders = {tid: (v or {}).get("from", "") for tid, v in known.items()}
        actions = core.apply_people_rules(actions, senders, settings["people"], settings["keep_people_in_inbox"])
        if body.get("review"):
            # One review at a time: a new one replaces any left unfinished.
            conn.execute("UPDATE inbox_batches SET status='discarded', finished_at=? WHERE status='review'", (_iso(),))
            proposal = [{**a, **{k: (known.get(a["id"]) or {}).get(k, "") for k in EXPORT_FIELDS}} for a in actions]
            batch_id = _new_batch(conn, source, status="review", total=len(actions))
            conn.execute("UPDATE inbox_batches SET summary=?, proposal=? WHERE id=?",
                         (summary, json.dumps({"actions": proposal, "summary": summary}), batch_id))
            conn.commit()
            return _batch_response(batch_id, 201)
        batch_id = _new_batch(conn, source, step="Applying", total=len(actions))
        conn.commit()
    finally:
        conn.close()
    spawn(run_apply_batch, batch_id, actions, summary, source)
    return _batch_response(batch_id)


@inbox_bp.get("/triage/batches")
@require_owner
def list_batches():
    limit = max(1, min(100, _int(request.args.get("limit"), 15)))
    conn = _db()
    try:
        rows = conn.execute("SELECT * FROM inbox_batches ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    finally:
        conn.close()
    return jsonify({"batches": [_batch_dict(r) for r in rows]})


@inbox_bp.get("/triage/batches/<int:batch_id>")
@require_owner
def get_batch(batch_id):
    conn = _db()
    try:
        row = conn.execute("SELECT * FROM inbox_batches WHERE id=?", (batch_id,)).fetchone()
    finally:
        conn.close()
    if not row:
        return jsonify({"error": "Batch not found"}), 404
    return jsonify({"batch": _batch_dict(row, with_proposal=True)})


@inbox_bp.post("/triage/batches/<int:batch_id>/apply")
@require_owner
def apply_reviewed(batch_id):
    """Apply a reviewed sort, with any edits made in the review."""
    body = request.get_json(silent=True) or {}
    conn = _db()
    try:
        row = conn.execute("SELECT * FROM inbox_batches WHERE id=?", (batch_id,)).fetchone()
        if not row:
            return jsonify({"error": "Batch not found"}), 404
        if row["status"] != "review":
            return jsonify({"error": "This batch isn't waiting for review"}), 409
        if _busy_batch(conn):
            return jsonify({"error": "A sort is already running"}), 409
        proposal = json.loads(row["proposal"] or "{}")
        allowed = {a["id"] for a in proposal.get("actions", [])}
        source = body.get("actions") if isinstance(body.get("actions"), list) else proposal.get("actions", [])
        actions = [a for a in (core.normalize_action(x) for x in source) if a and a["id"] in allowed]
        skip = {str(x) for x in (body.get("skip") or [])}
        actions = [a for a in actions if a["id"] not in skip]
        if not actions:
            return jsonify({"error": "Nothing left to apply"}), 400
        summary = str(body.get("summary") or proposal.get("summary") or "")
        conn.execute("UPDATE inbox_batches SET status='applying', step='Applying', total=? WHERE id=?",
                     (len(actions), batch_id))
        conn.commit()
    finally:
        conn.close()
    spawn(run_apply_batch, batch_id, actions, summary, row["source"] or "claude")
    return _batch_response(batch_id)


@inbox_bp.post("/triage/batches/<int:batch_id>/discard")
@require_owner
def discard_batch(batch_id):
    conn = _db()
    try:
        conn.execute("UPDATE inbox_batches SET status='discarded', finished_at=? WHERE id=? AND status='review'",
                     (_iso(), batch_id))
        conn.commit()
    finally:
        conn.close()
    return _batch_response(batch_id, 200)


# ─── History import from the Rollup sheet ─────────────────────────────────────

def run_import(batch_id: int):
    conn = _db()
    try:
        settings = load_settings(conn)
    finally:
        conn.close()
    offset = int(settings.get("rollup_imported") or 0)
    imported = 0
    client = get_script_client()
    try:
        while True:
            _update_batch(batch_id, step=f"Reading Rollup rows from {offset + 1}")
            page = client.call("rollup", offset=offset, limit=500, timeout=120)
            rows = page.get("rows") or []
            if not rows:
                break
            conn = _db()
            try:
                people = load_settings(conn)["people"]
                for r in rows:
                    tid = r.get("id") or ""
                    if not tid or conn.execute("SELECT 1 FROM inbox_messages WHERE thread_id=?", (tid,)).fetchone():
                        continue
                    at = _parse_iso(r.get("processedAt")) or _now()
                    store_sorted_items(conn, [{
                        "id": tid, "from": r.get("from", ""), "subject": r.get("subject", ""),
                        "summary": r.get("summary", ""), "labels": r.get("labels") or [],
                        "importance": r.get("importance", ""), "archived": r.get("archived"),
                        "date": r.get("date", ""), "markedRead": True,
                    }], batch_id, "import", people, at=at)
                    # History is history: don't flood the highlights as unread.
                    conn.execute("UPDATE inbox_messages SET read_at=sorted_at WHERE thread_id=?", (tid,))
                    imported += 1
                offset += len(rows)
                save_settings({"rollup_imported": offset}, conn)
                conn.commit()
            finally:
                conn.close()
            if offset >= int(page.get("total") or 0):
                break
        _update_batch(batch_id, status="done", step="", applied=imported, total=imported,
                      message=f"Imported {imported} emails from the Rollup sheet.", finished_at=_iso())
    except ScriptError as e:
        _update_batch(batch_id, status="failed", step="", error=str(e), applied=imported, finished_at=_iso())


@inbox_bp.post("/import")
@require_owner
def import_rollup():
    batch_id, busy = _start("import", run_import, step="Starting import")
    if busy:
        return jsonify({"error": "Something is already running", "batch_id": busy}), 409
    return _batch_response(batch_id)


# ─── Connection check ─────────────────────────────────────────────────────────

@inbox_bp.get("/connection")
@require_owner
def connection():
    client = get_script_client()
    out = {"script": {"configured": client.configured, "url": client.url_hint() if client.configured else ""}}
    if client.configured:
        try:
            ping = client.call("ping", timeout=60)
            out["script"].update({"ok": True, "version": ping.get("version"), "query": ping.get("query"),
                                  "inbox_unread": ping.get("inboxUnread"), "reports_to": ping.get("reportsTo")})
        except ScriptError as e:
            out["script"].update({"ok": False, "error": str(e), "code": e.code})
    return jsonify(out)


# ─── Ingest from Apps Script (Sheet / old web page batches) ───────────────────

@inbox_bp.post("/ingest")
def ingest():
    raw = request.get_data() or b""
    err = core.verify(SCRIPT_SECRET, request.headers.get("X-Inbox-Timestamp", "").strip(), raw,
                      request.headers.get("X-Inbox-Signature", ""))
    if err:
        return jsonify({"error": err}), 503 if "is off" in err else 401
    _ensure_tables()
    try:
        body = json.loads(raw.decode("utf-8"))
    except ValueError:
        return jsonify({"error": "Body is not JSON"}), 400
    items = body.get("items") if isinstance(body.get("items"), list) else []
    if not items:
        return jsonify({"ok": True, "stored": 0})
    source = str(body.get("source") or "sheet")[:20]
    conn = _db()
    try:
        people = load_settings(conn)["people"]
        batch_id = _new_batch(conn, source, status="done", total=len(items))
        counts = store_sorted_items(conn, items[:200], batch_id, source, people)
        conn.execute("UPDATE inbox_batches SET applied=?, archived=?, important=?, personal=?, summary=?, "
                     "message=?, finished_at=? WHERE id=?",
                     (counts["applied"], counts["archived"], counts["important"], counts["personal"],
                      str(body.get("summary") or "")[:4000], "Applied from the Sheet.", _iso(), batch_id))
        conn.commit()
    finally:
        conn.close()
    _forget("status")
    return jsonify({"ok": True, "stored": counts["applied"], "batch_id": batch_id})


@inbox_bp.get("/health")
def health():
    return jsonify({"ok": True, "service": "inbox", "script_configured": get_script_client().configured,
                    "ingest": bool(SCRIPT_SECRET)})
