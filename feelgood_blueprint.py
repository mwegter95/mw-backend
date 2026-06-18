"""
Feel-Good Productivity — Flask Blueprint
Mounts under /feelgood on the mw-backend server.

Backs the interactive workbook at
https://mwegter95.github.io/feel-good-productivity/ (iframed by
michaelwegter.com/apps/feel-good-productivity).

WHY THIS LOOKS A LITTLE DIFFERENT FROM repsetta_blueprint.py:
Sign-in is REQUIRED here, and we deliberately reuse the SAME accounts as the
rest of the site (Life Dashboard, Growyard) so a user signs in once. To do that
without a circular import of server.py, this blueprint:
  - reads the shared JWT secret straight from data/.secret_key (the exact file
    server.py's _get_secret() writes), and
  - resolves the caller against the shared `users` table in data/mw.db.
The frontend reuses the existing /auth/register and /auth/login routes, so no
new auth endpoints are added here.

Endpoints:
  GET  /feelgood/health         liveness
  GET  /feelgood/state          the caller's saved workbook state (auth required)
  PUT  /feelgood/state          replace the caller's workbook state (auth required)

Storage: a single table this blueprint owns (feelgood_state) inside the existing
SQLite DB at data/mw.db, created lazily with CREATE TABLE IF NOT EXISTS. It does
NOT modify the shared SCHEMA, auth code, or any other table/blueprint.

CORS: handled globally by server.py's CORS(app, ...), which already allow-lists
https://mwegter95.github.io and https://michaelwegter.com.
"""

import json
import sqlite3
from functools import wraps
from pathlib import Path
from datetime import datetime, timezone

import jwt as _jwt
from flask import Blueprint, request, jsonify, g

feelgood_bp = Blueprint("feelgood", __name__, url_prefix="/feelgood")

# Same data dir convention as the rest of the server.
DATA_DIR = Path(__file__).parent / "data"
DB_PATH = DATA_DIR / "mw.db"
SECRET_FILE = DATA_DIR / ".secret_key"

MAX_STATE_BYTES = 256 * 1024  # generous cap for a JSON workbook blob

_secret_cache = None


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


def _ensure_table():
    conn = _db()
    try:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS feelgood_state (
                user_id    TEXT PRIMARY KEY,
                state_json TEXT NOT NULL,
                updated_at TEXT
            )"""
        )
        conn.commit()
    finally:
        conn.close()


def require_auth(f):
    """Authenticate against the SHARED users table using the SHARED JWT secret.
    Populates g.user_id. Same Bearer-token contract as server.py's require_auth."""
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
        # Confirm the user still exists in the shared accounts table.
        conn = _db()
        try:
            row = conn.execute("SELECT id FROM users WHERE id=?", (user_id,)).fetchone()
        finally:
            conn.close()
        if not row:
            return jsonify({"error": "Account not found"}), 401
        g.user_id = user_id
        return f(*args, **kwargs)
    return wrapper


@feelgood_bp.get("/health")
def health():
    return jsonify({"ok": True, "service": "feelgood"})


@feelgood_bp.get("/state")
@require_auth
def get_state():
    _ensure_table()
    conn = _db()
    try:
        row = conn.execute(
            "SELECT state_json, updated_at FROM feelgood_state WHERE user_id=?",
            (g.user_id,),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return jsonify({"state": {"done": {}, "fields": {}}, "updated_at": None})
    try:
        state = json.loads(row["state_json"])
    except Exception:
        state = {"done": {}, "fields": {}}
    return jsonify({"state": state, "updated_at": row["updated_at"]})


@feelgood_bp.put("/state")
@require_auth
def put_state():
    body = request.get_json(silent=True) or {}
    state = body.get("state")
    if not isinstance(state, dict):
        return jsonify({"error": "state must be an object"}), 400
    # Normalise to the shape the app uses; ignore anything unexpected.
    clean = {
        "done": state.get("done") if isinstance(state.get("done"), dict) else {},
        "fields": state.get("fields") if isinstance(state.get("fields"), dict) else {},
    }
    blob = json.dumps(clean, ensure_ascii=False)
    if len(blob.encode("utf-8")) > MAX_STATE_BYTES:
        return jsonify({"error": "state too large"}), 413

    _ensure_table()
    now = datetime.now(timezone.utc).isoformat()
    conn = _db()
    try:
        conn.execute(
            """INSERT INTO feelgood_state (user_id, state_json, updated_at)
               VALUES (?,?,?)
               ON CONFLICT(user_id) DO UPDATE SET
                 state_json=excluded.state_json,
                 updated_at=excluded.updated_at""",
            (g.user_id, blob, now),
        )
        conn.commit()
    finally:
        conn.close()
    return jsonify({"ok": True, "updated_at": now})
