"""
mw-backend — Shared backend for michaelwegter.com projects.
Handles auth + the Gallery Wall Planner API in one server.

To add more projects later, just add more route sections below.

Run:
  ./start.sh            (local only)
  ./start.sh --tunnel   (local + Cloudflare Tunnel for internet access)
"""

import os
import sys
import json
import logging
import sqlite3
import secrets
import datetime
import mimetypes
import subprocess
import tempfile
import traceback
import threading
import struct
import urllib.request as _urllib_req
from pathlib import Path
from functools import wraps

# Load .env before anything else so all os.getenv() calls see the values
from dotenv import load_dotenv
load_dotenv(Path(__file__).parent / ".env")

from flask import Flask, request, jsonify, g, send_from_directory, send_file, Response
from flask_cors import CORS
import bcrypt
import jwt
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

# ─── Paths & config ──────────────────────────────────────────────────────────

BASE_DIR     = Path(__file__).parent
DATA_DIR     = BASE_DIR / "data"
UPLOADS_DIR  = DATA_DIR / "uploads"
CHUNK_UPLOADS_DIR = DATA_DIR / "chunk_uploads"

for d in [DATA_DIR, UPLOADS_DIR / "walls", UPLOADS_DIR / "pieces", UPLOADS_DIR / "library", CHUNK_UPLOADS_DIR]:
    d.mkdir(parents=True, exist_ok=True)

DB_PATH           = DATA_DIR / "mw.db"
PORT              = int(os.environ.get("PORT", 5050))
ACCESS_TTL        = datetime.timedelta(hours=24)
RESET_TOKEN_TTL_H = int(os.environ.get("RESET_TOKEN_TTL_HOURS", 1))
GAS_WEBHOOK_URL   = os.environ.get("GAS_WEBHOOK_URL", "")  # Google Apps Script email sender
FRONTEND_BASE     = os.environ.get("FRONTEND_URL", "https://mwegter95.github.io")

# Allowed frontend origins (add Netlify URL once deployed)
_CORS_ORIGINS = list({o for o in [
    "http://localhost:5173",
    "http://localhost:4173",
    "http://localhost:3000",
    "http://localhost:5015",  # SEO Analyzer local dev
    "https://mwegter95.github.io",
    "https://michaelwegter.com",
    "https://www.michaelwegter.com",
    os.environ.get("FRONTEND_URL", ""),
    os.environ.get("PORTFOLIO_URL", ""),
] if o})

# ─── Secret key ───────────────────────────────────────────────────────────────

def _get_secret():
    key_file = DATA_DIR / ".secret_key"
    if key_file.exists():
        return key_file.read_text().strip()
    key = secrets.token_hex(32)
    key_file.write_text(key)
    return key

SECRET_KEY = _get_secret()

# ─── File encryption helpers ──────────────────────────────────────────────────

def _get_file_key():
    """AES-256-GCM key derived from the server secret. Files on disk are not
    viewable as images -- only the running server can decrypt them."""
    import hashlib
    return hashlib.sha256((SECRET_KEY + ":file-encryption").encode()).digest()

_FILE_KEY = None
def _file_key():
    global _FILE_KEY
    if _FILE_KEY is None:
        _FILE_KEY = _get_file_key()
    return _FILE_KEY

def encrypt_bytes(plaintext: bytes) -> bytes:
    """Returns nonce (12 bytes) || ciphertext+tag."""
    nonce = os.urandom(12)
    ct = AESGCM(_file_key()).encrypt(nonce, plaintext, None)
    return nonce + ct

def decrypt_bytes(blob: bytes) -> bytes:
    nonce, ct = blob[:12], blob[12:]
    return AESGCM(_file_key()).decrypt(nonce, ct, None)

def write_encrypted(path: Path, data: bytes):
    path.write_bytes(encrypt_bytes(data))

def read_encrypted(path: Path) -> bytes:
    return decrypt_bytes(path.read_bytes())

# ─── Logging ──────────────────────────────────────────────────────────────────
# Force stdout to be unbuffered so print() shows up immediately in the terminal.
sys.stdout.reconfigure(line_buffering=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger("mw-backend")

# Make Werkzeug (Flask dev server) request logs visible too
logging.getLogger("werkzeug").setLevel(logging.INFO)
logging.getLogger("werkzeug").handlers = []   # remove default stderr handler
logging.getLogger("werkzeug").addHandler(logging.StreamHandler(sys.stdout))

app = Flask(__name__)
app.secret_key = SECRET_KEY   # enables Flask sessions (used by Spotify OAuth blueprint)
# SameSite=None + Secure required so session cookies work when SSUT is embedded
# in an iframe on michaelwegter.com (cross-site context in modern browsers).
app.config["SESSION_COOKIE_SAMESITE"] = "None"
app.config["SESSION_COOKIE_SECURE"]   = True
CORS(app, origins=_CORS_ORIGINS, supports_credentials=True)


def utc_now():
    return datetime.datetime.now(datetime.UTC)


def utc_now_iso_legacy():
    # Keep DB timestamp format compatible with existing naive UTC values.
    return utc_now().replace(tzinfo=None).isoformat()


def parse_utc_iso(value: str):
    dt = datetime.datetime.fromisoformat(value)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=datetime.UTC)
    return dt.astimezone(datetime.UTC)


def is_sqlite_storage_full_error(err):
    return isinstance(err, sqlite3.OperationalError) and "database or disk is full" in str(err).lower()

# ─── Spotify Super User Tools blueprint ──────────────────────────────────────
from spotify_blueprint import spotify_bp
app.register_blueprint(spotify_bp)

from werkzeug.exceptions import HTTPException

@app.errorhandler(Exception)
def handle_exception(e):
    """Return JSON instead of HTML for unhandled non-HTTP exceptions."""
    if isinstance(e, HTTPException):
        return e  # let Flask handle normal HTTP errors normally

    if is_sqlite_storage_full_error(e):
        db = g.get("db")
        if db:
            try:
                db.rollback()
            except Exception:
                pass
        log.error("[storage full] %s", str(e))
        return jsonify({"error": "Storage full", "detail": "Server database or disk is full. Free up disk space and retry."}), 507

    tb = traceback.format_exc()
    log.error("[unhandled exception] %s\n%s", str(e), tb)
    return jsonify({"error": "Internal server error", "detail": str(e)}), 500

# ─── Database ─────────────────────────────────────────────────────────────────

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    email         TEXT    UNIQUE NOT NULL,
    password_hash TEXT    NOT NULL,
    display_name  TEXT    DEFAULT '',
    created_at    DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- Gallery walls (metadata only; images stored as files)
CREATE TABLE IF NOT EXISTS gallery_walls (
    id          TEXT PRIMARY KEY,
    owner_type  TEXT NOT NULL,   -- 'user' | 'device'
    owner_id    TEXT NOT NULL,
    data        TEXT NOT NULL,   -- JSON (name, width, height, imageUrl, createdAt)
    updated_at  DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- Gallery layouts (list of pieces per wall per named layout)
CREATE TABLE IF NOT EXISTS gallery_layouts (
    wall_id          TEXT NOT NULL,
    owner_type       TEXT NOT NULL,
    owner_id         TEXT NOT NULL,
    name             TEXT NOT NULL,
    pieces           TEXT NOT NULL,   -- JSON array
    paint_layer_ids  TEXT,            -- JSON array of paint layer IDs (nullable = none)
    updated_at       DATETIME DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (wall_id, owner_type, owner_id, name)
);

-- Password reset tokens
CREATE TABLE IF NOT EXISTS password_reset_tokens (
    token       TEXT PRIMARY KEY,
    user_id     INTEGER NOT NULL,
    expires_at  DATETIME NOT NULL,
    used        INTEGER DEFAULT 0
);

-- Gallery piece library
CREATE TABLE IF NOT EXISTS gallery_library (
    id          TEXT PRIMARY KEY,
    owner_type  TEXT NOT NULL,
    owner_id    TEXT NOT NULL,
    data        TEXT NOT NULL,   -- JSON
    updated_at  DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- Gallery paint layers (per-wall virtual paint layers)
CREATE TABLE IF NOT EXISTS gallery_paint_layers (
    wall_id     TEXT NOT NULL,
    layer_id    TEXT NOT NULL,
    owner_type  TEXT NOT NULL,
    owner_id    TEXT NOT NULL,
    data        TEXT NOT NULL,   -- JSON (id, name, color, maskDataUrl, visible, createdAt)
    updated_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (wall_id, layer_id, owner_type, owner_id)
);

-- 3D Rooms (rectangular prisms with up to 6 photo-mapped surfaces)
CREATE TABLE IF NOT EXISTS gallery_rooms (
    id          TEXT PRIMARY KEY,
    owner_type  TEXT NOT NULL,
    owner_id    TEXT NOT NULL,
    data        TEXT NOT NULL,   -- JSON (id, name, roomWidth, roomHeight, roomDepth, surfaces, createdAt)
    updated_at  DATETIME DEFAULT CURRENT_TIMESTAMP
);
"""

def get_db():
    if "db" not in g:
        conn = sqlite3.connect(str(DB_PATH))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        g.db = conn
    return g.db

@app.teardown_appcontext
def close_db(exc):
    db = g.pop("db", None)
    if db:
        db.close()

def init_db():
    conn = sqlite3.connect(str(DB_PATH))
    conn.executescript(SCHEMA)
    # Migrate: add paint_layer_ids column to gallery_layouts if it doesn't exist yet
    try:
        conn.execute("ALTER TABLE gallery_layouts ADD COLUMN paint_layer_ids TEXT")
        conn.commit()
    except Exception:
        pass  # column already exists
    conn.commit()
    conn.close()
    print(f"✓ Database ready: {DB_PATH}")

# ─── Auth helpers ─────────────────────────────────────────────────────────────

def make_token(user_id, user=None):
    payload = {
        "sub": str(user_id),
        "exp": utc_now() + ACCESS_TTL,
    }
    if user:
        payload["email"]        = user["email"]
        payload["display_name"] = user["display_name"]
    return jwt.encode(payload, SECRET_KEY, algorithm="HS256")

def _resolve_principal():
    """Returns (user_row | None, device_token | None)."""
    _bearer_tok = (
        request.headers.get("X-Auth-Token", "").strip()
        or request.args.get("_tok", "").strip()
    )
    auth = request.headers.get("Authorization", "") or ("Bearer " + _bearer_tok if _bearer_tok else "")
    device = request.headers.get("X-Device-Token", "").strip() or None
    if auth.startswith("Bearer "):
        try:
            payload = jwt.decode(auth[7:], SECRET_KEY, algorithms=["HS256"])
            user = get_db().execute("SELECT * FROM users WHERE id=?", (payload["sub"],)).fetchone()
            if user:
                return user, device
        except jwt.PyJWTError:
            pass
    return None, device

def owner_of(user, device_token):
    if user:
        return "user", str(user["id"])
    if device_token:
        return "device", device_token
    return None, None

def require_owner(f):
    """Accepts JWT or device token; populates g.owner_type + g.owner_id."""
    @wraps(f)
    def wrapper(*args, **kwargs):
        user, device = _resolve_principal()
        ot, oi = owner_of(user, device)
        if not oi:
            return jsonify({"error": "Provide Authorization: Bearer <token> or X-Device-Token header"}), 401
        g.owner_type   = ot
        g.owner_id     = oi
        g.current_user = user
        return f(*args, **kwargs)
    return wrapper

def require_auth(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        user, device = _resolve_principal()
        if not user:
            return jsonify({"error": "Authentication required"}), 401
        g.current_user = user
        g.device_token = device
        return f(*args, **kwargs)
    return wrapper

# ─── Auth routes ─────────────────────────────────────────────────────────────

@app.post("/auth/register")
def auth_register():
    d       = request.get_json(silent=True) or {}
    email   = (d.get("email") or "").strip().lower()
    pw      = d.get("password") or ""
    name    = (d.get("display_name") or email.split("@")[0]).strip()
    if not email or not pw:
        return jsonify({"error": "email and password required"}), 400
    if len(pw) < 8:
        return jsonify({"error": "Password must be at least 8 characters"}), 400
    pw_hash = bcrypt.hashpw(pw.encode(), bcrypt.gensalt()).decode()
    db = get_db()
    try:
        db.execute("INSERT INTO users (email, password_hash, display_name) VALUES (?,?,?)",
                   (email, pw_hash, name))
        db.commit()
    except sqlite3.IntegrityError:
        return jsonify({"error": "Email already registered"}), 409
    user = db.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
    # Migrate any existing device data into this new account
    _claim_device(db, str(user["id"]), d.get("device_token"))
    return jsonify({"token": make_token(user["id"], user=user), "user": _user_dict(user)}), 201


@app.post("/auth/login")
def auth_login():
    d     = request.get_json(silent=True) or {}
    email = (d.get("email") or "").strip().lower()
    pw    = d.get("password") or ""
    if not email or not pw:
        return jsonify({"error": "email and password required"}), 400
    try:
        db   = get_db()
        user = db.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
        if not user or not bcrypt.checkpw(pw.encode(), user["password_hash"].encode()):
            log.warning("[auth/login] failed for email=%s", email)
            return jsonify({"error": "Invalid email or password"}), 401
        log.info("[auth/login] success for email=%s user_id=%s", email, user["id"])
        _claim_device(db, str(user["id"]), d.get("device_token"))
        return jsonify({"token": make_token(user["id"], user=user), "user": _user_dict(user)})
    except Exception:
        tb = traceback.format_exc()
        log.error("[auth/login ERROR] email=%s\n%s", email, tb)
        return jsonify({"error": "Login failed due to a server error. Please try again."}), 500


@app.get("/auth/me")
@require_auth
def auth_me():
    return jsonify({"user": _user_dict(g.current_user)})


@app.post("/auth/claim")
@require_auth
def auth_claim():
    d = request.get_json(silent=True) or {}
    device = (d.get("device_token") or "").strip()
    if not device:
        return jsonify({"error": "device_token required"}), 400
    _claim_device(get_db(), str(g.current_user["id"]), device)
    return jsonify({"claimed": True})


def _claim_device(db, user_id, device_token):
    if not device_token:
        return
    for table in ("gallery_walls", "gallery_layouts", "gallery_library"):
        db.execute(
            f"UPDATE {table} SET owner_type='user', owner_id=? "
            "WHERE owner_type='device' AND owner_id=?",
            (user_id, device_token)
        )
    db.commit()

def _user_dict(u):
    return {"id": u["id"], "email": u["email"], "display_name": u["display_name"]}


def _send_reset_email(to_email, reset_url):
    """POSTs to the Google Apps Script webhook to send the reset email."""
    if not GAS_WEBHOOK_URL:
        print(f"[forgot-password] GAS_WEBHOOK_URL not set. Reset URL: {reset_url}")
        return
    plain = (
        f"You requested a password reset for your Gallery Wall Planner account.\n\n"
        f"Click the link below to reset your password (valid for {RESET_TOKEN_TTL_H} hour(s)):\n\n"
        f"{reset_url}\n\n"
        f"If you did not request this, you can ignore this email."
    )
    html = f"""
<div style="font-family:sans-serif;max-width:480px;margin:0 auto;padding:24px;color:#222">
  <h2 style="margin-top:0">Reset your password</h2>
  <p>You requested a password reset for your <strong>Gallery Wall Planner</strong> account.</p>
  <p>This link expires in {RESET_TOKEN_TTL_H} hour(s).</p>
  <p style="margin:28px 0">
    <a href="{reset_url}"
       style="background:#6c8ebf;color:#fff;padding:12px 24px;border-radius:6px;
              text-decoration:none;font-weight:600;display:inline-block">
      Reset Password
    </a>
  </p>
  <p style="font-size:12px;color:#666">
    Or paste this link into your browser:<br>
    <a href="{reset_url}" style="color:#6c8ebf">{reset_url}</a>
  </p>
  <hr style="border:none;border-top:1px solid #eee;margin:24px 0">
  <p style="font-size:11px;color:#999">If you did not request this, you can safely ignore this email.</p>
</div>
"""
    payload = json.dumps({"to": to_email, "subject": "Reset your Gallery Wall password", "body": plain, "htmlBody": html}).encode()
    req = _urllib_req.Request(GAS_WEBHOOK_URL, data=payload, headers={"Content-Type": "application/json"}, method="POST")
    try:
        _urllib_req.urlopen(req, timeout=10)
    except Exception as e:
        print(f"[forgot-password] Email send failed: {e}")


@app.post("/auth/forgot-password")
def auth_forgot_password():
    d     = request.get_json(silent=True) or {}
    email = (d.get("email") or "").strip().lower()
    if not email:
        return jsonify({"error": "email required"}), 400
    db   = get_db()
    user = db.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
    # Always return 200 to prevent email enumeration
    if not user:
        return jsonify({"ok": True})
    # Invalidate old tokens for this user
    db.execute("UPDATE password_reset_tokens SET used=1 WHERE user_id=? AND used=0", (user["id"],))
    token      = secrets.token_urlsafe(32)
    expires_at = (utc_now() + datetime.timedelta(hours=RESET_TOKEN_TTL_H)).isoformat()
    db.execute("INSERT INTO password_reset_tokens (token, user_id, expires_at) VALUES (?,?,?)",
               (token, user["id"], expires_at))
    db.commit()
    reset_url = f"{FRONTEND_BASE}?reset_token={token}"
    _send_reset_email(email, reset_url)
    return jsonify({"ok": True})


@app.post("/auth/reset-password")
def auth_reset_password():
    d        = request.get_json(silent=True) or {}
    token    = (d.get("token") or "").strip()
    password = d.get("password") or ""
    if not token or not password:
        return jsonify({"error": "token and password required"}), 400
    if len(password) < 8:
        return jsonify({"error": "Password must be at least 8 characters"}), 400
    db  = get_db()
    row = db.execute(
        "SELECT * FROM password_reset_tokens WHERE token=? AND used=0", (token,)
    ).fetchone()
    if not row:
        return jsonify({"error": "Invalid or expired reset link"}), 400
    # Check expiry
    expires = parse_utc_iso(row["expires_at"])
    if utc_now() > expires:
        db.execute("UPDATE password_reset_tokens SET used=1 WHERE token=?", (token,))
        db.commit()
        return jsonify({"error": "Reset link has expired. Please request a new one."}), 400
    # Update password
    new_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
    db.execute("UPDATE users SET password_hash=? WHERE id=?", (new_hash, row["user_id"]))
    db.execute("UPDATE password_reset_tokens SET used=1 WHERE token=?", (token,))
    db.commit()
    user = db.execute("SELECT * FROM users WHERE id=?", (row["user_id"],)).fetchone()
    return jsonify({"token": make_token(user["id"], user=user), "user": _user_dict(user)})

# ─── Gallery: full state ──────────────────────────────────────────────────────

@app.get("/api/state")
@require_owner
def gallery_state():
    db = get_db()
    # walls
    wall_rows = db.execute(
        "SELECT id, data FROM gallery_walls WHERE owner_type=? AND owner_id=?",
        (g.owner_type, g.owner_id)
    ).fetchall()
    walls = {r["id"]: json.loads(r["data"]) for r in wall_rows}

    # layouts
    layout_rows = db.execute(
        "SELECT wall_id, name, pieces, paint_layer_ids FROM gallery_layouts WHERE owner_type=? AND owner_id=?",
        (g.owner_type, g.owner_id)
    ).fetchall()
    layouts = {}
    for r in layout_rows:
        layouts.setdefault(r["wall_id"], {})[r["name"]] = {
            "pieces":         json.loads(r["pieces"]),
            "paintLayerIds":  json.loads(r["paint_layer_ids"]) if r["paint_layer_ids"] else [],
        }

    # library
    lib_rows = db.execute(
        "SELECT id, data FROM gallery_library WHERE owner_type=? AND owner_id=?",
        (g.owner_type, g.owner_id)
    ).fetchall()
    library = {r["id"]: json.loads(r["data"]) for r in lib_rows}

    # paint layers — keyed by wall_id, then layer_id
    paint_rows = db.execute(
        "SELECT wall_id, layer_id, data FROM gallery_paint_layers WHERE owner_type=? AND owner_id=?",
        (g.owner_type, g.owner_id)
    ).fetchall()
    paint_layers = {}
    for r in paint_rows:
        paint_layers.setdefault(r["wall_id"], {})[r["layer_id"]] = json.loads(r["data"])

    # rooms
    room_rows = db.execute(
        "SELECT id, data FROM gallery_rooms WHERE owner_type=? AND owner_id=? ORDER BY updated_at DESC",
        (g.owner_type, g.owner_id)
    ).fetchall()
    rooms = {r["id"]: json.loads(r["data"]) for r in room_rows}

    return jsonify({"walls": walls, "layouts": layouts, "library": library, "paintLayers": paint_layers, "rooms": rooms})

# ─── Gallery: walls ───────────────────────────────────────────────────────────

@app.put("/api/walls/<wall_id>")
@require_owner
def gallery_put_wall(wall_id):
    db  = get_db()
    now = utc_now_iso_legacy()
    db.execute(
        "INSERT INTO gallery_walls (id, owner_type, owner_id, data, updated_at) VALUES (?,?,?,?,?) "
        "ON CONFLICT(id) DO UPDATE SET owner_type=excluded.owner_type, owner_id=excluded.owner_id, "
        "data=excluded.data, updated_at=excluded.updated_at",
        (wall_id, g.owner_type, g.owner_id, json.dumps(request.get_json()), now)
    )
    db.commit()
    return jsonify({"ok": True})


@app.delete("/api/walls/<wall_id>")
@require_owner
def gallery_delete_wall(wall_id):
    db = get_db()
    db.execute("DELETE FROM gallery_walls WHERE id=? AND owner_type=? AND owner_id=?",
               (wall_id, g.owner_type, g.owner_id))
    db.execute("DELETE FROM gallery_layouts WHERE wall_id=? AND owner_type=? AND owner_id=?",
               (wall_id, g.owner_type, g.owner_id))
    db.execute("DELETE FROM gallery_paint_layers WHERE wall_id=? AND owner_type=? AND owner_id=?",
               (wall_id, g.owner_type, g.owner_id))
    db.commit()
    # Remove wall image file
    for ext in ["jpg", "jpeg", "png", "webp"]:
        p = UPLOADS_DIR / "walls" / f"{wall_id}.{ext}"
        if p.exists():
            p.unlink()
    return jsonify({"ok": True})


@app.post("/api/walls/<wall_id>/image")
@require_owner
def gallery_wall_image(wall_id):
    data_url = (request.get_json(silent=True) or {}).get("dataUrl", "")
    buf, ext = _decode_data_url(data_url)
    if buf is None:
        return jsonify({"error": "Invalid dataUrl"}), 400
    # Remove old images with different extension
    for e in ["jpg", "jpeg", "png", "webp"]:
        old = UPLOADS_DIR / "walls" / f"{wall_id}.{e}"
        if e != ext and old.exists():
            old.unlink()
    path = UPLOADS_DIR / "walls" / f"{wall_id}.{ext}"
    write_encrypted(path, buf)
    return jsonify({"url": f"/uploads/walls/{wall_id}.{ext}"})

# ─── Gallery: layouts ─────────────────────────────────────────────────────────

@app.put("/api/layouts/<wall_id>/<name>")
@require_owner
def gallery_put_layout(wall_id, name):
    body            = request.get_json(silent=True) or {}
    pieces          = body.get("pieces", [])
    paint_layer_ids = body.get("paintLayerIds", [])
    now             = utc_now_iso_legacy()
    db              = get_db()
    db.execute(
        "INSERT INTO gallery_layouts (wall_id, owner_type, owner_id, name, pieces, paint_layer_ids, updated_at) VALUES (?,?,?,?,?,?,?) "
        "ON CONFLICT(wall_id, owner_type, owner_id, name) DO UPDATE SET "
        "pieces=excluded.pieces, paint_layer_ids=excluded.paint_layer_ids, updated_at=excluded.updated_at",
        (wall_id, g.owner_type, g.owner_id, name, json.dumps(pieces), json.dumps(paint_layer_ids), now)
    )
    db.commit()
    return jsonify({"ok": True})


@app.delete("/api/layouts/<wall_id>/<name>")
@require_owner
def gallery_delete_layout(wall_id, name):
    db = get_db()
    db.execute(
        "DELETE FROM gallery_layouts WHERE wall_id=? AND owner_type=? AND owner_id=? AND name=?",
        (wall_id, g.owner_type, g.owner_id, name)
    )
    db.commit()
    return jsonify({"ok": True})

# ─── Gallery: paint layers ───────────────────────────────────────────────────

@app.put("/api/paint-layers/<wall_id>/<layer_id>")
@require_owner
def gallery_put_paint_layer(wall_id, layer_id):
    data = request.get_json(silent=True) or {}
    now  = utc_now_iso_legacy()
    db   = get_db()
    db.execute(
        "INSERT INTO gallery_paint_layers (wall_id, layer_id, owner_type, owner_id, data, updated_at) "
        "VALUES (?,?,?,?,?,?) "
        "ON CONFLICT(wall_id, layer_id, owner_type, owner_id) DO UPDATE SET data=excluded.data, updated_at=excluded.updated_at",
        (wall_id, layer_id, g.owner_type, g.owner_id, json.dumps(data), now)
    )
    db.commit()
    return jsonify({"ok": True})


@app.delete("/api/paint-layers/<wall_id>/<layer_id>")
@require_owner
def gallery_delete_paint_layer(wall_id, layer_id):
    db = get_db()
    db.execute(
        "DELETE FROM gallery_paint_layers WHERE wall_id=? AND layer_id=? AND owner_type=? AND owner_id=?",
        (wall_id, layer_id, g.owner_type, g.owner_id)
    )
    db.commit()
    return jsonify({"ok": True})


# ─── Gallery: 3D rooms ───────────────────────────────────────────────────────

@app.get("/api/rooms")
@require_owner
def gallery_list_rooms():
    db = get_db()
    rows = db.execute(
        "SELECT id, data FROM gallery_rooms WHERE owner_type=? AND owner_id=? ORDER BY updated_at DESC",
        (g.owner_type, g.owner_id)
    ).fetchall()
    rooms = {}
    for r in rows:
        room_data = json.loads(r["data"])
        # Fix relative image URLs in surface warpedImageUrls
        for surface in (room_data.get("surfaces") or {}).values():
            if surface.get("warpedImageUrl") and surface["warpedImageUrl"].startswith("/"):
                surface["warpedImageUrl"] = surface["warpedImageUrl"]  # kept relative; frontend prepends BASE
        rooms[r["id"]] = room_data
    return jsonify({"rooms": rooms})


@app.put("/api/rooms/<room_id>")
@require_owner
def gallery_put_room(room_id):
    db  = get_db()
    now = utc_now_iso_legacy()
    data = request.get_json(silent=True) or {}
    # Strip any large data URL payloads stored in warpedImageUrl before persisting
    # (images are saved separately via /api/rooms/<id>/surfaces/<face>/image)
    db.execute(
        "INSERT INTO gallery_rooms (id, owner_type, owner_id, data, updated_at) VALUES (?,?,?,?,?) "
        "ON CONFLICT(id) DO UPDATE SET owner_type=excluded.owner_type, owner_id=excluded.owner_id, "
        "data=excluded.data, updated_at=excluded.updated_at",
        (room_id, g.owner_type, g.owner_id, json.dumps(data), now)
    )
    db.commit()
    return jsonify({"ok": True})


@app.post("/api/rooms/<room_id>/pointcloud")
@require_owner
def gallery_upload_pointcloud(room_id):
    """Accept raw Float32 binary point cloud, store encrypted, return URL.
    Client sends Content-Type: application/octet-stream with the raw bytes."""
    buf = request.data
    if not buf:
        return jsonify({"error": "No data"}), 400
    MAX_PC_SIZE = 640 * 1024 * 1024  # 640 MB cap (~26M points)
    if len(buf) > MAX_PC_SIZE:
        return jsonify({"error": "Point cloud exceeds size limit"}), 413
    path = UPLOADS_DIR / "walls" / f"{room_id}_pointcloud.bin"
    write_encrypted(path, buf)
    return jsonify({"url": f"/uploads/walls/{room_id}_pointcloud.bin"})


@app.post("/api/rooms/<room_id>/pointcloud/chunk")
@require_owner
def gallery_upload_pointcloud_chunk(room_id):
    """Accept chunked binary uploads and assemble server-side before encrypting."""
    buf = request.data
    if not buf:
        return jsonify({"error": "No data"}), 400

    upload_id = (request.headers.get("X-Upload-Id") or "").strip()
    try:
        chunk_index = int(request.headers.get("X-Chunk-Index", "-1"))
        chunk_total = int(request.headers.get("X-Chunk-Total", "-1"))
    except Exception:
        return jsonify({"error": "Invalid chunk headers"}), 400

    if not upload_id or chunk_index < 0 or chunk_total <= 0 or chunk_index >= chunk_total:
        return jsonify({"error": "Invalid chunk metadata"}), 400

    safe_room = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in room_id)
    safe_upload = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in upload_id)
    safe_owner = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in f"{g.owner_type}_{g.owner_id}")
    stem = f"{safe_owner}_{safe_room}_{safe_upload}"
    part_path = CHUNK_UPLOADS_DIR / f"{stem}.part"
    meta_path = CHUNK_UPLOADS_DIR / f"{stem}.json"

    if chunk_index == 0:
        if part_path.exists():
            part_path.unlink()
        if meta_path.exists():
            meta_path.unlink()
        meta = {"next_index": 0, "total": chunk_total, "size": 0}
    else:
        if not part_path.exists() or not meta_path.exists():
            return jsonify({"error": "Upload session missing or expired"}), 409
        try:
            meta = json.loads(meta_path.read_text())
        except Exception:
            return jsonify({"error": "Upload session corrupted"}), 409
        if int(meta.get("total", -1)) != chunk_total:
            return jsonify({"error": "Chunk total mismatch"}), 409

    if int(meta.get("next_index", 0)) != chunk_index:
        return jsonify({"error": "Out-of-order chunk", "expected": int(meta.get("next_index", 0))}), 409

    with part_path.open("ab") as f:
        f.write(buf)

    meta["next_index"] = chunk_index + 1
    meta["size"] = int(meta.get("size", 0)) + len(buf)

    MAX_PC_SIZE = 640 * 1024 * 1024  # 640 MB cap (~26M points)
    if meta["size"] > MAX_PC_SIZE:
        if part_path.exists():
            part_path.unlink()
        if meta_path.exists():
            meta_path.unlink()
        return jsonify({"error": "Point cloud exceeds size limit"}), 413

    if chunk_index + 1 < chunk_total:
        meta_path.write_text(json.dumps(meta))
        return jsonify({"ok": True, "received": chunk_index + 1, "total": chunk_total, "complete": False})

    # Final chunk received -> encrypt assembled payload and return room URL.
    assembled = part_path.read_bytes()
    path = UPLOADS_DIR / "walls" / f"{room_id}_pointcloud.bin"
    write_encrypted(path, assembled)
    if part_path.exists():
        part_path.unlink()
    if meta_path.exists():
        meta_path.unlink()

    # Kick off background Poisson mesh reconstruction.
    # Runs in a daemon thread so the upload response is instant.
    threading.Thread(
        target=_build_poisson_mesh,
        args=(room_id, path),
        daemon=True,
    ).start()

    return jsonify({"url": f"/uploads/walls/{room_id}_pointcloud.bin", "complete": True})


# ─── Poisson mesh reconstruction ─────────────────────────────────────────────
# Runs in a background daemon thread after the final chunk arrives.
# Writes a GLB file alongside the encrypted point cloud:
#   uploads/walls/<room_id>_mesh.glb   (plain, not encrypted — same auth as PC)
# Status is tracked with a lightweight sentinel file:
#   uploads/walls/<room_id>_mesh.status  ("processing" | "ready" | "failed")

def _build_poisson_mesh(room_id: str, pc_path: Path):
    """Decode encrypted point cloud, run Open3D Poisson, export GLB.
    Writes <room_id>_mesh.progress (JSON: {pct, phase}) at each stage so
    the frontend can show a real stage-informed progress bar.
    """
    status_path   = UPLOADS_DIR / "walls" / f"{room_id}_mesh.status"
    progress_path = UPLOADS_DIR / "walls" / f"{room_id}_mesh.progress"
    glb_path      = UPLOADS_DIR / "walls" / f"{room_id}_mesh.glb"

    import json as _json
    import time as _time

    def _progress(pct: int, phase: str):
        """Write progress file and emit a log line."""
        try:
            progress_path.write_text(_json.dumps({"pct": pct, "phase": phase}))
        except Exception:
            pass
        logging.info(f"[mesh] {room_id}: {pct:3d}%  {phase}")

    status_path.write_text("processing")
    _progress(0, "Starting reconstruction")
    t0 = _time.time()
    try:
        import numpy as np
        import open3d as o3d
        import trimesh

        # ── 1. Decode + load point cloud ──────────────────────────────────
        _progress(2, "Decoding point cloud")
        raw = read_encrypted(pc_path)
        arr = np.frombuffer(raw, dtype=np.float32).reshape(-1, 6)
        xyz = arr[:, :3].astype(np.float64)
        rgb = np.clip(arr[:, 3:6], 0.0, 1.0).astype(np.float64)
        n_pts = len(xyz)
        logging.info(f"[mesh] {room_id}: loaded {n_pts:,} points")

        pcd_full = o3d.geometry.PointCloud()
        pcd_full.points = o3d.utility.Vector3dVector(xyz)
        pcd_full.colors = o3d.utility.Vector3dVector(rgb)

        # ── 2. Uniform resampling ──────────────────────────────────────────
        _progress(10, f"Resampling {n_pts:,} points (5 mm grid)")
        pcd = pcd_full.voxel_down_sample(voxel_size=0.005)
        n_down = len(pcd.points)
        logging.info(f"[mesh] {room_id}: downsampled to {n_down:,} points")

        # ── 3. Normal estimation ───────────────────────────────────────────
        _progress(20, f"Estimating normals ({n_down:,} pts)")
        pcd.estimate_normals(
            search_param=o3d.geometry.KDTreeSearchParamHybrid(radius=0.05, max_nn=30)
        )
        _progress(32, "Orienting normals (consistent tangent plane)")
        pcd.orient_normals_consistent_tangent_plane(k=15)

        # ── 4. Screened Poisson reconstruction ────────────────────────────
        _progress(42, "Running Screened Poisson (depth=9) — this takes ~30–90 s")
        mesh, densities = o3d.geometry.TriangleMesh.create_from_point_cloud_poisson(
            pcd, depth=9, linear_fit=False
        )
        n_verts_raw = len(mesh.vertices)
        n_faces_raw = len(mesh.triangles)
        logging.info(f"[mesh] {room_id}: Poisson produced {n_verts_raw:,} verts, {n_faces_raw:,} faces")

        # ── 5. Trim low-density exterior artifacts ─────────────────────────
        _progress(62, "Trimming low-density exterior")
        import numpy as _np
        d = _np.asarray(densities)
        threshold = _np.percentile(d, 2)
        mesh.remove_vertices_by_mask(d < threshold)
        mesh.remove_degenerate_triangles()
        mesh.remove_duplicated_vertices()
        mesh.remove_non_manifold_edges()
        logging.info(
            f"[mesh] {room_id}: after trim — {len(mesh.vertices):,} verts, {len(mesh.triangles):,} faces"
        )

        # ── 6. Color transfer from full-resolution cloud ───────────────────
        _progress(70, f"Transferring colors from {n_pts:,}-point cloud")
        from scipy.spatial import cKDTree
        pcd_pts  = _np.asarray(pcd_full.points)
        pcd_rgb  = _np.asarray(pcd_full.colors)
        mesh_pts = _np.asarray(mesh.vertices)
        kd = cKDTree(pcd_pts)
        _progress(74, "KD-tree built — querying nearest neighbors")
        _, idxs = kd.query(mesh_pts, k=5, workers=-1)
        vtx_colors = pcd_rgb[idxs].mean(axis=1)
        mesh.vertex_colors = o3d.utility.Vector3dVector(vtx_colors)
        logging.info(f"[mesh] {room_id}: color transfer done")

        # ── 7. Export as GLB ───────────────────────────────────────────────
        _progress(88, "Exporting GLB")
        verts      = _np.asarray(mesh.vertices).astype(_np.float32)
        faces      = _np.asarray(mesh.triangles).astype(_np.uint32)
        colors_u8  = (_np.clip(vtx_colors, 0, 1) * 255).astype(_np.uint8)
        colors_rgba = _np.concatenate(
            [colors_u8, _np.full((len(colors_u8), 1), 255, dtype=_np.uint8)], axis=1
        )
        tm = trimesh.Trimesh(vertices=verts, faces=faces, vertex_colors=colors_rgba, process=False)
        glb_bytes = tm.export(file_type="glb")
        _progress(96, "Writing GLB to disk")
        glb_path.write_bytes(glb_bytes)

        elapsed = _time.time() - t0
        _progress(100, f"Done — {len(verts):,} verts, {len(faces):,} faces, {len(glb_bytes)//1024} KB, {elapsed:.0f}s")
        status_path.write_text("ready")
        logging.info(
            f"[mesh] {room_id}: COMPLETE in {elapsed:.1f}s — "
            f"{len(verts):,} verts, {len(faces):,} faces, {len(glb_bytes)//1024} KB"
        )

    except Exception:
        logging.exception(f"[mesh] {room_id}: Poisson reconstruction FAILED")
        _progress(0, "Build failed — check server logs")
        status_path.write_text("failed")


@app.get("/api/rooms/<room_id>/mesh")
@require_owner
def gallery_get_mesh(room_id):
    """Return mesh build status and URL (if ready).
    Pass ?rebuild=1 to discard the existing mesh and re-run Poisson."""
    status_path = UPLOADS_DIR / "walls" / f"{room_id}_mesh.status"
    glb_path    = UPLOADS_DIR / "walls" / f"{room_id}_mesh.glb"
    pc_path     = UPLOADS_DIR / "walls" / f"{room_id}_pointcloud.bin"

    progress_path = UPLOADS_DIR / "walls" / f"{room_id}_mesh.progress"

    rebuild = request.args.get("rebuild") == "1"
    if rebuild and status_path.exists():
        status_path.unlink(missing_ok=True)
        glb_path.unlink(missing_ok=True)
        progress_path.unlink(missing_ok=True)

    if not status_path.exists():
        if pc_path.exists():
            threading.Thread(
                target=_build_poisson_mesh,
                args=(room_id, pc_path),
                daemon=True,
            ).start()
            return jsonify({"status": "processing", "pct": 0, "phase": "Queued"})
        return jsonify({"status": "unavailable"})

    status = status_path.read_text().strip()

    # Read stage progress if available
    pct, phase = 0, ""
    if progress_path.exists():
        try:
            import json as _j
            prog = _j.loads(progress_path.read_text())
            pct   = prog.get("pct",   0)
            phase = prog.get("phase", "")
        except Exception:
            pass

    if status == "ready" and glb_path.exists():
        return jsonify({"status": "ready", "url": f"/uploads/walls/{room_id}_mesh.glb", "pct": 100, "phase": phase})
    return jsonify({"status": status, "pct": pct, "phase": phase})


@app.get("/uploads/walls/<filename>")
def serve_wall_upload(filename):
    """Serve wall uploads — images plain, point clouds and meshes decrypted on the fly."""
    path = UPLOADS_DIR / "walls" / filename
    if not path.exists():
        return jsonify({"error": "Not found"}), 404
    if filename.endswith("_mesh.glb"):
        # GLB files are stored plain (large binary, no PII)
        return send_file(path, mimetype="model/gltf-binary")
    # All other uploads are AES-GCM encrypted
    try:
        data = read_encrypted(path)
    except Exception:
        return jsonify({"error": "Decryption failed"}), 500
    mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    return Response(data, mimetype=mime)


@app.delete("/api/rooms/<room_id>")
@require_owner
def gallery_delete_room(room_id):
    db = get_db()
    db.execute("DELETE FROM gallery_rooms WHERE id=? AND owner_type=? AND owner_id=?",
               (room_id, g.owner_type, g.owner_id))
    db.commit()
    # Remove any surface image files for this room
    for face_id in ["north", "south", "east", "west", "floor", "ceiling"]:
        for ext in ["jpg", "jpeg", "png", "webp"]:
            p = UPLOADS_DIR / "walls" / f"{room_id}_{face_id}.{ext}"
            if p.exists():
                p.unlink()
    return jsonify({"ok": True})


@app.post("/api/rooms/<room_id>/surfaces/<face_id>/image")
@require_owner
def gallery_room_surface_image(room_id, face_id):
    valid_faces = {"north", "south", "east", "west", "floor", "ceiling"}
    if face_id not in valid_faces:
        return jsonify({"error": "Invalid face_id"}), 400
    data_url = (request.get_json(silent=True) or {}).get("dataUrl", "")
    buf, ext = _decode_data_url(data_url)
    if buf is None:
        return jsonify({"error": "Invalid dataUrl"}), 400
    slug = f"{room_id}_{face_id}"
    for e in ["jpg", "jpeg", "png", "webp"]:
        old = UPLOADS_DIR / "walls" / f"{slug}.{e}"
        if e != ext and old.exists():
            old.unlink()
    path = UPLOADS_DIR / "walls" / f"{slug}.{ext}"
    write_encrypted(path, buf)
    return jsonify({"url": f"/uploads/walls/{slug}.{ext}"})


# ─── Gallery: piece images ───────────────────────────────────────────────────

@app.post("/api/piece-images/<piece_id>")
@require_owner
def gallery_put_piece_image(piece_id):
    data_url = (request.get_json(silent=True) or {}).get("dataUrl", "")
    buf, ext = _decode_data_url(data_url)
    if buf is None:
        return jsonify({"error": "Invalid dataUrl"}), 400
    for e in ["jpg", "jpeg", "png", "webp"]:
        old = UPLOADS_DIR / "pieces" / f"{piece_id}.{e}"
        if e != ext and old.exists():
            old.unlink()
    path = UPLOADS_DIR / "pieces" / f"{piece_id}.{ext}"
    write_encrypted(path, buf)
    return jsonify({"url": f"/uploads/pieces/{piece_id}.{ext}"})


@app.delete("/api/piece-images/<piece_id>")
@require_owner
def gallery_delete_piece_image(piece_id):
    for ext in ["jpg", "jpeg", "png", "webp"]:
        p = UPLOADS_DIR / "pieces" / f"{piece_id}.{ext}"
        if p.exists():
            p.unlink()
    return jsonify({"ok": True})

# ─── Gallery: library ─────────────────────────────────────────────────────────

@app.put("/api/library/<lib_id>")
@require_owner
def gallery_put_library(lib_id):
    now = utc_now_iso_legacy()
    db  = get_db()
    db.execute(
        "INSERT INTO gallery_library (id, owner_type, owner_id, data, updated_at) VALUES (?,?,?,?,?) "
        "ON CONFLICT(id) DO UPDATE SET data=excluded.data, updated_at=excluded.updated_at",
        (lib_id, g.owner_type, g.owner_id, json.dumps(request.get_json()), now)
    )
    db.commit()
    return jsonify({"ok": True})


@app.delete("/api/library/<lib_id>")
@require_owner
def gallery_delete_library(lib_id):
    db = get_db()
    db.execute("DELETE FROM gallery_library WHERE id=? AND owner_type=? AND owner_id=?",
               (lib_id, g.owner_type, g.owner_id))
    db.commit()
    return jsonify({"ok": True})


@app.post("/api/library/<lib_id>/image")
@require_owner
def gallery_put_library_image(lib_id):
    data_url = (request.get_json(silent=True) or {}).get("dataUrl", "")
    buf, ext = _decode_data_url(data_url)
    if buf is None:
        return jsonify({"error": "Invalid dataUrl"}), 400
    for e in ["jpg", "jpeg", "png", "webp"]:
        old = UPLOADS_DIR / "library" / f"{lib_id}.{e}"
        if e != ext and old.exists():
            old.unlink()
    path = UPLOADS_DIR / "library" / f"{lib_id}.{ext}"
    write_encrypted(path, buf)
    return jsonify({"url": f"/uploads/library/{lib_id}.{ext}"})

# ─── Admin: bulk import (migration) ─────────────────────────────────────────

@app.post("/api/admin/import")
def admin_import():
    """One-shot bulk import for migration.  Authenticated by the JWT token
    in the request body — never a header, so Cloudflare cannot strip it.

    Body JSON:
      {
        "token": "<JWT>",
        "user_id": 1,
        "walls":   { id: {data dict} },
        "layouts": { wall_id: { name: [pieces] } },
        "library": { id: {data dict} },
        "images":  { "walls/id.ext": "<base64>", ... }
      }
    """
    body = request.get_json(silent=True) or {}
    token = body.get("token", "")
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=["HS256"], options={"verify_sub": False})
        user_id = str(payload["sub"])
    except Exception as exc:
        log.warning("admin_import auth failed: %s", exc)
        return jsonify({"error": "Forbidden"}), 403
    db = get_db()
    now = utc_now_iso_legacy()
    counts = {"walls": 0, "layouts": 0, "library": 0, "images": 0}

    # Optional: wipe existing user data before import so re-runs are clean
    if body.get("clear_first"):
        db.execute("DELETE FROM gallery_walls   WHERE owner_type='user' AND owner_id=?", (user_id,))
        db.execute("DELETE FROM gallery_layouts WHERE owner_type='user' AND owner_id=?", (user_id,))
        db.execute("DELETE FROM gallery_library WHERE owner_type='user' AND owner_id=?", (user_id,))
        db.commit()

    # Walls
    for wid, wdata in (body.get("walls") or {}).items():
        db.execute(
            "INSERT INTO gallery_walls(id,owner_type,owner_id,data,updated_at) "
            "VALUES(?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET owner_type=excluded.owner_type, "
            "owner_id=excluded.owner_id, data=excluded.data, updated_at=excluded.updated_at",
            (wid, "user", user_id, json.dumps(wdata), now)
        )
        counts["walls"] += 1

    # Layouts
    for wall_id, named in (body.get("layouts") or {}).items():
        for name, layout_data in named.items():
            # layout_data may be the new { pieces, paintLayerIds } object or a legacy array
            if isinstance(layout_data, list):
                pieces          = layout_data
                paint_layer_ids = []
            else:
                pieces          = layout_data.get("pieces", [])
                paint_layer_ids = layout_data.get("paintLayerIds", [])
            db.execute(
                "INSERT INTO gallery_layouts(wall_id,owner_type,owner_id,name,pieces,paint_layer_ids,updated_at) "
                "VALUES(?,?,?,?,?,?,?) "
                "ON CONFLICT(wall_id,owner_type,owner_id,name) DO UPDATE SET "
                "pieces=excluded.pieces, paint_layer_ids=excluded.paint_layer_ids, updated_at=excluded.updated_at",
                (wall_id, "user", user_id, name, json.dumps(pieces), json.dumps(paint_layer_ids), now)
            )
            counts["layouts"] += 1

    # Library
    for lid, ldata in (body.get("library") or {}).items():
        db.execute(
            "INSERT INTO gallery_library(id,owner_type,owner_id,data,updated_at) "
            "VALUES(?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET owner_type=excluded.owner_type, "
            "owner_id=excluded.owner_id, data=excluded.data, updated_at=excluded.updated_at",
            (lid, "user", user_id, json.dumps(ldata), now)
        )
        counts["library"] += 1

    db.commit()

    # Images — base64 encoded, written encrypted to uploads/
    import base64
    for rel_path, b64 in (body.get("images") or {}).items():
        try:
            raw = base64.b64decode(b64)
            dest = UPLOADS_DIR / rel_path
            dest.parent.mkdir(parents=True, exist_ok=True)
            write_encrypted(dest, raw)
            counts["images"] += 1
        except Exception as e:
            log.warning("Failed to write image %s: %s", rel_path, e)

    return jsonify({"ok": True, "counts": counts})


# ─── SEO Analyzer ─────────────────────────────────────────────────────────────
# All routes require a logged-in user (require_auth).
# Reports are stored per-user under data/seo_reports/<user_id>/.
# Crawl state is tracked per-user in _seo_crawl_states so concurrent users
# don't interfere with each other.

try:
    import re as _re
    import asyncio as _asyncio
    import queue as _queue
    import threading as _threading
    import time as _time
    import xml.etree.ElementTree as _ET
    from urllib.parse import urlparse as _urlparse, urljoin as _urljoin
    from collections import Counter as _Counter
    from bs4 import BeautifulSoup as _BeautifulSoup, Comment as _Comment
    import textstat as _textstat
    _SEO_AVAILABLE = True
except ImportError as _e:
    _SEO_AVAILABLE = False
    log.warning("SEO Analyzer dependencies not installed: %s", _e)

SEO_REPORTS_DIR = DATA_DIR / "seo_reports"
SEO_REPORTS_DIR.mkdir(parents=True, exist_ok=True)

# Per-user crawl state: { user_id_str: { "q": Queue, "started": bool } }
_seo_crawl_states: dict = {}

# Browser-like User-Agent for HTTP fetches
_SEO_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
           "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36")

_SEO_PARALLEL_TABS = 10


def _seo_user_dir(user_id: str) -> "Path":
    d = SEO_REPORTS_DIR / user_id
    d.mkdir(parents=True, exist_ok=True)
    return d


# ---- SEO analysis helpers ------------------------------------------------

def _seo_clean_text(soup) -> str:
    for tag in soup(["script", "style", "noscript", "iframe", "svg"]):
        tag.decompose()
    for comment in soup.find_all(string=lambda t: isinstance(t, _Comment)):
        comment.extract()
    text = soup.get_text(separator=" ", strip=True)
    return _re.sub(r"\s+", " ", text).strip()


def _seo_word_list(text: str):
    return _re.findall(r"[a-zA-Z''\-]+", text.lower())


def _seo_extract_keywords(words, top_n=20):
    stop = set(
        "a an the and or but in on at to for of is it this that was were be "
        "been being have has had do does did will would shall should may might "
        "can could i me my we our you your he him his she her they them their "
        "its not no nor so if then else when where how what which who whom why "
        "with from by as into through during before after above below between "
        "out off over under again further once here there all each every both "
        "few more most other some such only own same than too very just about "
        "up also back still even new now old well way because thing things "
        "much get got go going know like make us am are".split()
    )
    filtered = [w for w in words if w not in stop and len(w) >= 2]
    return _Counter(filtered).most_common(top_n)


def _seo_extract_ngrams(words, n=2, top_k=10):
    stop = set(
        "a an the and or but in on at to for of is it this that was were be "
        "been being have has had do does did will would shall should may might "
        "can could i me my we our you your he him his she her they them their "
        "its not no nor so if then else".split()
    )
    ngrams = []
    for i in range(len(words) - n + 1):
        gram = words[i: i + n]
        if not any(w in stop for w in gram) and all(len(w) >= 2 for w in gram):
            ngrams.append(" ".join(gram))
    return _Counter(ngrams).most_common(top_k)


def _seo_analyze_html(html: str, url: str, timing: dict) -> dict:
    """Run all SEO checks on rendered HTML. Return structured report."""
    soup = _BeautifulSoup(html, "lxml")
    parsed = _urlparse(url)
    report: dict = {"url": url, "timing": timing, "scores": {}, "sections": {}}

    # --- Title ---
    title_tag = soup.find("title")
    title = title_tag.get_text(strip=True) if title_tag else ""
    title_issues = []
    if not title:
        title_issues.append("Missing title tag.")
    elif len(title) < 30:
        title_issues.append(f"Title is short ({len(title)} chars); aim for 50–60.")
    elif len(title) > 60:
        title_issues.append(f"Title is long ({len(title)} chars); Google truncates at ~60.")
    report["sections"]["title"] = {
        "value": title, "length": len(title),
        "issues": title_issues, "pass": len(title_issues) == 0
    }

    # --- Meta description ---
    meta_tag = soup.find("meta", attrs={"name": _re.compile(r"^description$", _re.I)})
    meta_desc = meta_tag.get("content", "").strip() if meta_tag else ""
    meta_issues = []
    if not meta_desc:
        meta_issues.append("Missing meta description.")
    elif len(meta_desc) < 70:
        meta_issues.append(f"Meta description is short ({len(meta_desc)} chars); aim for 120–160.")
    elif len(meta_desc) > 160:
        meta_issues.append(f"Meta description is long ({len(meta_desc)} chars); Google truncates at ~160.")
    report["sections"]["meta_description"] = {
        "value": meta_desc, "length": len(meta_desc),
        "issues": meta_issues, "pass": len(meta_issues) == 0
    }

    # --- Canonical ---
    canon_tag = soup.find("link", rel="canonical")
    canon = canon_tag.get("href", "").strip() if canon_tag else ""
    canon_issues = [] if canon else ["No canonical tag found."]
    report["sections"]["canonical"] = {"value": canon, "issues": canon_issues, "pass": len(canon_issues) == 0}

    # --- Robots meta ---
    robots_tag = soup.find("meta", attrs={"name": _re.compile(r"^robots$", _re.I)})
    robots_val = robots_tag.get("content", "").strip() if robots_tag else ""
    robots_issues = []
    if robots_val and ("noindex" in robots_val.lower() or "nofollow" in robots_val.lower()):
        robots_issues.append(f"Robots meta restricts indexing: '{robots_val}'")
    report["sections"]["robots"] = {"value": robots_val, "issues": robots_issues, "pass": len(robots_issues) == 0}

    # --- Viewport ---
    vp_tag = soup.find("meta", attrs={"name": _re.compile(r"^viewport$", _re.I)})
    vp_val = vp_tag.get("content", "").strip() if vp_tag else ""
    vp_issues = [] if vp_val else ["No viewport meta tag — page may not be mobile-friendly."]
    report["sections"]["viewport"] = {"value": vp_val, "issues": vp_issues, "pass": len(vp_issues) == 0}

    # --- Charset ---
    charset_tag = soup.find("meta", charset=True) or soup.find("meta", attrs={"http-equiv": _re.compile(r"content-type", _re.I)})
    charset_val = charset_tag.get("charset", "") if charset_tag else ""
    charset_issues = [] if charset_val else ["No charset declaration found."]
    report["sections"]["charset"] = {"value": charset_val, "issues": charset_issues, "pass": len(charset_issues) == 0}

    # --- Language ---
    html_tag = soup.find("html")
    lang_val = html_tag.get("lang", "").strip() if html_tag else ""
    lang_issues = [] if lang_val else ["No lang attribute on <html> tag."]
    report["sections"]["language"] = {"value": lang_val, "issues": lang_issues, "pass": len(lang_issues) == 0}

    # --- Headings ---
    heading_counts = {f"h{i}": len(soup.find_all(f"h{i}")) for i in range(1, 7)}
    h1_texts = [h.get_text(strip=True) for h in soup.find_all("h1")]
    h2_texts = [h.get_text(strip=True) for h in soup.find_all("h2")][:5]
    heading_issues = []
    if heading_counts["h1"] == 0:
        heading_issues.append("No H1 tag found.")
    elif heading_counts["h1"] > 1:
        heading_issues.append(f"Multiple H1 tags ({heading_counts['h1']}) — use only one.")
    report["sections"]["headings"] = {
        "counts": heading_counts, "h1": h1_texts, "h2": h2_texts,
        "issues": heading_issues, "pass": len(heading_issues) == 0
    }

    # --- Images ---
    imgs = soup.find_all("img")
    missing_alt = [str(i) for i in imgs if not i.get("alt") and i.get("alt") is None]
    empty_alt = [str(i) for i in imgs if i.get("alt") == ""]
    img_issues = []
    if missing_alt:
        img_issues.append(f"{len(missing_alt)} image(s) missing alt attribute.")
    report["sections"]["images"] = {
        "total": len(imgs), "missing_alt": missing_alt[:5], "empty_alt": empty_alt[:5],
        "issues": img_issues, "pass": len(img_issues) == 0
    }

    # --- Links ---
    all_links = soup.find_all("a", href=True)
    internal_links = [a for a in all_links if _urlparse(_urljoin(url, a["href"])).netloc == parsed.netloc or a["href"].startswith("/")]
    external_links = [a for a in all_links if a not in internal_links]
    nofollow_links = [a for a in all_links if "nofollow" in a.get("rel", [])]
    link_issues = []
    if not internal_links:
        link_issues.append("No internal links found.")
    report["sections"]["links"] = {
        "internal": len(internal_links), "external": len(external_links),
        "nofollow": len(nofollow_links), "issues": link_issues, "pass": len(link_issues) == 0
    }

    # --- Open Graph ---
    og_tags = {t.get("property", "").replace("og:", ""): t.get("content", "")
               for t in soup.find_all("meta", property=_re.compile(r"^og:", _re.I))}
    og_issues = []
    for required in ["title", "description", "image"]:
        if required not in og_tags:
            og_issues.append(f"Missing og:{required} tag.")
    report["sections"]["open_graph"] = {"tags": og_tags, "issues": og_issues, "pass": len(og_issues) == 0}

    # --- Twitter Card ---
    tc_tags = {t.get("name", "").replace("twitter:", ""): t.get("content", "")
               for t in soup.find_all("meta", attrs={"name": _re.compile(r"^twitter:", _re.I)})}
    tc_issues = [] if tc_tags else ["No Twitter Card meta tags found."]
    report["sections"]["twitter_card"] = {"tags": tc_tags, "issues": tc_issues, "pass": len(tc_issues) == 0}

    # --- Structured data ---
    json_ld_blocks = soup.find_all("script", type="application/ld+json")
    sd_types = []
    for block in json_ld_blocks:
        try:
            import json as _json
            d = _json.loads(block.string or "{}")
            t = d.get("@type", "")
            if t:
                sd_types.append(t if isinstance(t, str) else str(t))
        except Exception:
            pass
    sd_issues = [] if json_ld_blocks else ["No structured data (JSON-LD) found."]
    report["sections"]["structured_data"] = {
        "count": len(json_ld_blocks), "types": sd_types,
        "issues": sd_issues, "pass": len(sd_issues) == 0
    }

    # --- Content & keywords ---
    visible_text = _seo_clean_text(soup)
    words = _seo_word_list(visible_text)
    word_count = len(words)
    top_keywords = _seo_extract_keywords(words, top_n=20)
    bigrams = _seo_extract_ngrams(words, n=2, top_k=10)
    trigrams = _seo_extract_ngrams(words, n=3, top_k=10)
    content_issues = []
    if word_count < 300:
        content_issues.append(f"Low word count ({word_count} words); aim for 300+.")

    keyword_placement = {}
    if top_keywords:
        primary = top_keywords[0][0]
        keyword_placement["primary_keyword"] = primary
        keyword_placement["in_title"] = primary in title.lower()
        keyword_placement["in_meta_desc"] = primary in meta_desc.lower()
        keyword_placement["in_h1"] = any(primary in h.lower() for h in h1_texts)
        keyword_placement["in_url"] = primary in url.lower()
        if not keyword_placement["in_title"]:
            content_issues.append(f"Primary keyword '{primary}' not in title tag.")
        if not keyword_placement["in_meta_desc"]:
            content_issues.append(f"Primary keyword '{primary}' not in meta description.")
        if not keyword_placement["in_h1"]:
            content_issues.append(f"Primary keyword '{primary}' not in H1.")

    readability = {}
    if visible_text and word_count > 50:
        try:
            readability["flesch_reading_ease"] = round(_textstat.flesch_reading_ease(visible_text), 1)
            readability["flesch_kincaid_grade"] = round(_textstat.flesch_kincaid_grade(visible_text), 1)
            readability["gunning_fog"] = round(_textstat.gunning_fog(visible_text), 1)
            readability["avg_sentence_length"] = round(_textstat.avg_sentence_length(visible_text), 1)
        except Exception:
            pass

    report["sections"]["content"] = {
        "word_count": word_count,
        "top_keywords": [{"word": w, "count": c, "density": round(c / word_count * 100, 2) if word_count else 0} for w, c in top_keywords],
        "bigrams": [{"phrase": p, "count": c} for p, c in bigrams],
        "trigrams": [{"phrase": p, "count": c} for p, c in trigrams],
        "keyword_placement": keyword_placement,
        "readability": readability,
        "issues": content_issues, "pass": len(content_issues) == 0,
    }

    # --- URL Structure ---
    url_issues = []
    path = parsed.path
    if len(url) > 75:
        url_issues.append("URL is quite long (>75 chars).")
    if _re.search(r"[A-Z]", path):
        url_issues.append("URL path contains uppercase letters.")
    if "_" in path:
        url_issues.append("URL uses underscores; prefer hyphens.")
    if _re.search(r"[?&]\w+=\w+", url):
        url_issues.append("URL has query parameters — may cause duplicate content.")
    report["sections"]["url_structure"] = {"url": url, "path": path, "issues": url_issues, "pass": len(url_issues) == 0}

    # --- HTTPS ---
    https_ok = parsed.scheme == "https"
    report["sections"]["https"] = {"secure": https_ok, "issues": [] if https_ok else ["Site not using HTTPS!"], "pass": https_ok}

    # --- Hreflang ---
    hreflangs = [{"lang": link["hreflang"], "href": link.get("href", "")}
                 for link in soup.find_all("link", rel="alternate", hreflang=True)]
    report["sections"]["hreflang"] = {"tags": hreflangs, "issues": [], "pass": True}

    # --- Performance hints ---
    perf_issues = []
    inline_styles = soup.find_all("style")
    if len(inline_styles) > 5:
        perf_issues.append(f"{len(inline_styles)} inline <style> blocks — consider external CSS.")
    scripts = soup.find_all("script", src=True)
    render_blocking = [s for s in scripts if not s.get("async") and not s.get("defer")]
    if render_blocking:
        perf_issues.append(f"{len(render_blocking)} render-blocking scripts (no async/defer).")
    report["sections"]["performance_hints"] = {
        "inline_styles": len(inline_styles), "total_scripts": len(scripts),
        "render_blocking_scripts": len(render_blocking),
        "issues": perf_issues, "pass": len(perf_issues) == 0
    }

    # --- Overall score ---
    scored_sections = [
        "title", "meta_description", "canonical", "robots", "viewport",
        "headings", "images", "links", "open_graph", "twitter_card",
        "structured_data", "content", "url_structure", "https", "performance_hints",
        "charset", "language",
    ]
    passed = sum(1 for s in scored_sections if report["sections"].get(s, {}).get("pass", False))
    total = len(scored_sections)
    report["scores"]["passed"] = passed
    report["scores"]["total"] = total
    report["scores"]["percentage"] = round(passed / total * 100)

    all_issues = []
    for s in scored_sections:
        sec = report["sections"].get(s, {})
        for issue in sec.get("issues", []):
            all_issues.append({"section": s, "issue": issue})
    report["all_issues"] = all_issues
    return report


# ---- SEO Playwright helpers -----------------------------------------------

async def _seo_fetch_rendered_html(url: str, page) -> tuple:
    timing = {}
    t0 = _time.time()
    try:
        response = await page.goto(url, wait_until="domcontentloaded", timeout=20000)
        timing["status_code"] = response.status if response else None
        try:
            await page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass
    except Exception as exc:
        timing["status_code"] = None
        timing["error"] = str(exc)[:200]
    await page.wait_for_timeout(500)
    html = await page.content()
    timing["total_ms"] = round((_time.time() - t0) * 1000)
    return html, timing


def _seo_normalise_url(url: str) -> str:
    parsed = _urlparse(url)
    path = parsed.path.rstrip("/") or "/"
    # Preserve SPA hash-routes where the fragment looks like a path (e.g. #/resume).
    # Strip plain page anchors (#section) since those aren't separate pages.
    frag = parsed.fragment if parsed.fragment.startswith("/") else ""
    clean = parsed._replace(fragment=frag, path=path)
    return clean.geturl()


def _seo_is_skip_href(href: str) -> bool:
    """True for page anchors, mailto, tel, javascript — but NOT SPA hash-routes like #/path."""
    if href.startswith(("mailto:", "tel:", "javascript:")):
        return True
    if href.startswith("#") and not href.startswith("#/"):
        return True
    return False


def _seo_same_site(netloc_a: str, netloc_b: str) -> bool:
    def strip_www(n):
        return n.lower().removeprefix("www.")
    return strip_www(netloc_a) == strip_www(netloc_b)


def _seo_discover_internal_links(html: str, base_url: str, root_netloc: str) -> set:
    soup = _BeautifulSoup(html, "lxml")
    found: set = set()
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if _seo_is_skip_href(href):
            continue
        full = _urljoin(base_url, href)
        parsed = _urlparse(full)
        if not _seo_same_site(parsed.netloc, root_netloc):
            continue
        clean = _seo_normalise_url(full)
        last_segment = parsed.path.split("/")[-1]
        ext = last_segment.rsplit(".", 1)[-1].lower() if "." in last_segment else ""
        if ext in ("jpg", "jpeg", "png", "gif", "svg", "webp", "pdf", "zip",
                   "mp3", "mp4", "wav", "css", "js", "ico", "woff", "woff2", "ttf",
                   "eot", "xml", "json", "txt", "map"):
            continue
        found.add(clean)
    return found


def _seo_parse_sitemap_xml(content: str, root_netloc: str) -> tuple:
    page_urls = []
    child_sitemaps = []
    try:
        content = _re.sub(r'\s+xmlns(?::\w+)?\s*=\s*"[^"]*"', '', content)
        content = _re.sub(r'\s+\w+:\w+\s*=\s*"[^"]*"', '', content)
        root_elem = _ET.fromstring(content)
        for sm_elem in root_elem.iter("sitemap"):
            loc = sm_elem.find("loc")
            if loc is not None and loc.text:
                child_sitemaps.append(loc.text.strip())
        for url_elem in root_elem.iter("url"):
            loc = url_elem.find("loc")
            if loc is not None and loc.text:
                page_url = loc.text.strip()
                p = _urlparse(page_url)
                if _seo_same_site(p.netloc, root_netloc):
                    page_urls.append(_seo_normalise_url(page_url))
    except Exception:
        pass
    return page_urls, child_sitemaps


def _seo_sitemap_candidates_from_robots(robots_text: str) -> list:
    candidates = []
    for line in robots_text.splitlines():
        m = _re.match(r'^\s*sitemap:\s*(.+)', line, _re.I)
        if m:
            sm_url = m.group(1).strip()
            if sm_url.startswith("http"):
                candidates.append(sm_url)
    return candidates


def _seo_get_sitemap_candidates(start_url: str) -> tuple:
    parsed = _urlparse(start_url)
    base = f"{parsed.scheme}://{parsed.netloc}"
    www_base = (f"{parsed.scheme}://www.{parsed.netloc}"
                if not parsed.netloc.startswith("www.") else base)
    fallback = []
    for b in (base, www_base):
        fallback.extend([f"{b}/sitemap.xml", f"{b}/sitemap_index.xml", f"{b}/wp-sitemap.xml"])
    return base, www_base, fallback


def _seo_fetch_sitemap_urls(start_url: str, root_netloc: str) -> list:
    urls = []
    _base, _www_base, fallback = _seo_get_sitemap_candidates(start_url)
    sitemap_candidates = []
    for b in (_base, _www_base):
        try:
            req = _urllib_req.Request(f"{b}/robots.txt", headers={"User-Agent": _SEO_UA})
            with _urllib_req.urlopen(req, timeout=10) as resp:
                robots_text = resp.read().decode("utf-8", errors="ignore")
            sitemap_candidates.extend(_seo_sitemap_candidates_from_robots(robots_text))
        except Exception:
            pass
    if not sitemap_candidates:
        sitemap_candidates = fallback
    visited_sitemaps: set = set()

    def _parse_sitemap(sm_url: str, depth: int = 0):
        if depth > 3 or sm_url in visited_sitemaps:
            return
        visited_sitemaps.add(sm_url)
        try:
            req = _urllib_req.Request(sm_url, headers={"User-Agent": _SEO_UA})
            with _urllib_req.urlopen(req, timeout=15) as resp:
                content = resp.read().decode("utf-8", errors="ignore")
            page_urls, child_sitemaps = _seo_parse_sitemap_xml(content, root_netloc)
            urls.extend(page_urls)
            for child in child_sitemaps:
                _parse_sitemap(child, depth + 1)
        except Exception:
            pass

    for sm in sitemap_candidates:
        _parse_sitemap(sm)
    return list(set(urls))


def _seo_resolve_url(start_url: str) -> tuple:
    resolved_url = start_url
    try:
        req = _urllib_req.Request(start_url, headers={"User-Agent": _SEO_UA}, method="HEAD")
        with _urllib_req.urlopen(req, timeout=10) as resp:
            resolved_url = resp.url
    except Exception:
        try:
            req = _urllib_req.Request(start_url, headers={"User-Agent": _SEO_UA})
            with _urllib_req.urlopen(req, timeout=10) as resp:
                resolved_url = resp.url
        except Exception:
            pass
    parsed = _urlparse(resolved_url)
    return _seo_normalise_url(resolved_url), parsed.netloc


def _seo_extract_navbar_links(html: str, base_url: str, root_netloc: str) -> list:
    soup = _BeautifulSoup(html, "lxml")
    nav_links: set = set()
    nav_containers = soup.find_all("nav") + soup.find_all("header")
    for attr in ["class", "id"]:
        for pattern in ["nav", "menu", "navbar", "main-nav", "primary-nav",
                        "site-nav", "site-header", "main-menu", "primary-menu"]:
            nav_containers += soup.find_all(attrs={attr: _re.compile(pattern, _re.I)})
    for container in nav_containers:
        for a in container.find_all("a", href=True):
            href = a["href"].strip()
            if _seo_is_skip_href(href):
                continue
            full = _urljoin(base_url, href)
            parsed = _urlparse(full)
            if not _seo_same_site(parsed.netloc, root_netloc):
                continue
            nav_links.add(_seo_normalise_url(full))
    return sorted(nav_links)


def _seo_group_urls_by_branch(urls: list) -> dict:
    branches: dict = {}
    for u in urls:
        parsed = _urlparse(u)
        parts = [p for p in parsed.path.strip("/").split("/") if p]
        branch = "/" + parts[0] + "/" if parts else "/"
        if branch not in branches:
            branches[branch] = []
        branches[branch].append(u)
    return dict(sorted(branches.items(), key=lambda x: -len(x[1])))


def _seo_clean_site_name(url: str) -> str:
    parsed = _urlparse(url)
    netloc = parsed.netloc.lower().removeprefix("www.").split(":")[0]
    return netloc


def _seo_save_report_files(url: str, summary: dict, page_reports: list, outputs_dir: "Path"):
    """Save JSON report to user's seo_reports directory."""
    from datetime import datetime as _dt
    site_name = _seo_clean_site_name(url)
    now = _dt.now()
    date_str = now.strftime("%Y%m%d")
    time_str = now.strftime("%H_%M_%S")
    base_name = f"{site_name}_seo_report_{date_str}_{time_str}"
    json_path = outputs_dir / f"{base_name}.json"
    with open(json_path, "w", encoding="utf-8") as f:
        import json as _json2
        _json2.dump({"summary": summary, "pages": page_reports}, f, indent=2, ensure_ascii=False)
    return {"json": str(json_path)}


def _seo_build_prescan_result(resolved_url: str, root_netloc: str, all_urls: list, navbar_urls: list) -> dict:
    branches = _seo_group_urls_by_branch(all_urls)
    navbar_branches: set = set()
    for u in navbar_urls:
        parsed = _urlparse(u)
        parts = [p for p in parsed.path.strip("/").split("/") if p]
        branch = "/" + parts[0] + "/" if parts else "/"
        navbar_branches.add(branch)
    return {
        "resolved_url": resolved_url,
        "root_netloc": root_netloc,
        "total_urls": len(all_urls),
        "navbar_urls": navbar_urls,
        "navbar_branches": sorted(navbar_branches),
        "branches": {b: {"count": len(urls), "sample_urls": urls[:5]} for b, urls in branches.items()},
        "all_urls": all_urls,
    }


async def _seo_prescan_async(start_url: str) -> dict:
    from playwright.async_api import async_playwright
    resolved_url = start_url
    root_netloc = _urlparse(start_url).netloc
    navbar_urls = []
    homepage_links = []
    sitemap_urls = []

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()
        try:
            await page.goto(start_url, wait_until="domcontentloaded", timeout=30000)
            resolved_url = page.url
            root_netloc = _urlparse(resolved_url).netloc
        except Exception:
            pass
        try:
            await page.wait_for_timeout(1500)
            html = await page.content()
            navbar_urls = _seo_extract_navbar_links(html, resolved_url, root_netloc)
            soup = _BeautifulSoup(html, "lxml")
            for a in soup.find_all("a", href=True):
                href = a["href"].strip()
                if _seo_is_skip_href(href):
                    continue
                full = _urljoin(resolved_url, href)
                p = _urlparse(full)
                if _seo_same_site(p.netloc, root_netloc):
                    homepage_links.append(_seo_normalise_url(full))

            # Fetch sitemaps using in-page fetch context
            _base, _www_base, fallback = _seo_get_sitemap_candidates(resolved_url)
            async def _fetch_text_in_page(url_to_fetch: str) -> str:
                try:
                    return await page.evaluate("""
                        async (url) => {
                            try {
                                const resp = await fetch(url, {credentials: 'include'});
                                if (!resp.ok) return '';
                                return await resp.text();
                            } catch(e) { return ''; }
                        }
                    """, url_to_fetch)
                except Exception:
                    return ""
            sitemap_candidates = []
            for b in (_base, _www_base):
                robots_text = await _fetch_text_in_page(f"{b}/robots.txt")
                if robots_text:
                    sitemap_candidates.extend(_seo_sitemap_candidates_from_robots(robots_text))
            if not sitemap_candidates:
                sitemap_candidates = fallback
            visited_sms: set = set()
            async def _parse_sm(sm_url: str, depth: int = 0):
                if depth > 3 or sm_url in visited_sms:
                    return
                visited_sms.add(sm_url)
                content = await _fetch_text_in_page(sm_url)
                if not content:
                    return
                page_urls, child_sms = _seo_parse_sitemap_xml(content, root_netloc)
                sitemap_urls.extend(page_urls)
                for child in child_sms:
                    await _parse_sm(child, depth + 1)
            for sm in sitemap_candidates:
                await _parse_sm(sm)
        except Exception:
            pass
        await browser.close()

    all_urls = list(set([_seo_normalise_url(resolved_url)] + sitemap_urls + navbar_urls + homepage_links))
    return _seo_build_prescan_result(_seo_normalise_url(resolved_url), root_netloc, all_urls, navbar_urls)


def _seo_prescan(start_url: str) -> dict:
    try:
        return _asyncio.run(_seo_prescan_async(start_url))
    except Exception:
        # Fallback to urllib
        resolved_url, root_netloc = _seo_resolve_url(start_url)
        sitemap_urls = []
        try:
            sitemap_urls = _seo_fetch_sitemap_urls(resolved_url, root_netloc)
        except Exception:
            pass
        navbar_urls = []
        homepage_links = []
        try:
            req = _urllib_req.Request(resolved_url, headers={"User-Agent": _SEO_UA})
            with _urllib_req.urlopen(req, timeout=15) as resp:
                html = resp.read().decode("utf-8", errors="ignore")
            navbar_urls = _seo_extract_navbar_links(html, resolved_url, root_netloc)
            soup = _BeautifulSoup(html, "lxml")
            for a in soup.find_all("a", href=True):
                href = a["href"].strip()
                if _seo_is_skip_href(href):
                    continue
                full = _urljoin(resolved_url, href)
                parsed = _urlparse(full)
                if _seo_same_site(parsed.netloc, root_netloc):
                    homepage_links.append(_seo_normalise_url(full))
        except Exception:
            pass
        all_urls = list(set([resolved_url] + sitemap_urls + navbar_urls + homepage_links))
        return _seo_build_prescan_result(resolved_url, root_netloc, all_urls, navbar_urls)


def _seo_build_site_summary(reports: list) -> dict:
    total_pages = len(reports)
    if total_pages == 0:
        return {"total_pages": 0}
    avg_score = round(sum(r["scores"]["percentage"] for r in reports) / total_pages)
    all_issues = []
    section_pass_counts: dict = {}
    site_keywords: _Counter = _Counter()
    site_bigrams: _Counter = _Counter()
    total_words = 0
    for r in reports:
        for iss in r.get("all_issues", []):
            all_issues.append({**iss, "url": r["url"]})
        for sec_name, sec in r.get("sections", {}).items():
            if sec_name not in section_pass_counts:
                section_pass_counts[sec_name] = {"pass": 0, "total": 0}
            section_pass_counts[sec_name]["total"] += 1
            if sec.get("pass"):
                section_pass_counts[sec_name]["pass"] += 1
        content = r.get("sections", {}).get("content", {})
        total_words += content.get("word_count", 0)
        for kw in content.get("top_keywords", []):
            site_keywords[kw["word"]] += kw["count"]
        for bg in content.get("bigrams", []):
            site_bigrams[bg["phrase"]] += bg["count"]
    sorted_reports = sorted(reports, key=lambda r: r["scores"]["percentage"])
    worst_pages = [{"url": r["url"], "score": r["scores"]["percentage"],
                    "issue_count": len(r.get("all_issues", []))} for r in sorted_reports[:5]]
    return {
        "total_pages": total_pages, "avg_score": avg_score, "total_words": total_words,
        "total_issues": len(all_issues), "all_issues": all_issues,
        "section_pass_rates": section_pass_counts,
        "site_keywords": [{"word": w, "count": c} for w, c in site_keywords.most_common(30)],
        "site_bigrams": [{"phrase": p, "count": c} for p, c in site_bigrams.most_common(15)],
        "worst_pages": worst_pages,
    }


def _seo_run_crawl(start_url: str, max_pages: int, state: dict, outputs_dir: "Path",
                   seed_urls=None, allowed_branches=None):
    _asyncio.run(_seo_async_crawl(start_url, max_pages, state, outputs_dir, seed_urls, allowed_branches))


async def _seo_async_crawl(start_url: str, max_pages: int, state: dict, outputs_dir: "Path",
                            seed_urls=None, allowed_branches=None):
    from playwright.async_api import async_playwright

    if seed_urls is not None:
        parsed_root = _urlparse(start_url)
        root_netloc = parsed_root.netloc
        state["q"].put({"type": "status", "page": 0, "url": start_url,
                        "queued": len(seed_urls), "total_found": len(seed_urls),
                        "message": f"Starting crawl of {len(seed_urls)} selected URLs…"})
    else:
        resolved_url = start_url
        try:
            req = _urllib_req.Request(start_url, headers={"User-Agent": _SEO_UA}, method="HEAD")
            with _urllib_req.urlopen(req, timeout=10) as resp:
                resolved_url = resp.url
        except Exception:
            try:
                req = _urllib_req.Request(start_url, headers={"User-Agent": _SEO_UA})
                with _urllib_req.urlopen(req, timeout=10) as resp:
                    resolved_url = resp.url
            except Exception:
                pass
        parsed_root = _urlparse(resolved_url)
        root_netloc = parsed_root.netloc
        start_url = _seo_normalise_url(resolved_url)
        state["q"].put({"type": "status", "page": 0, "url": start_url,
                        "queued": 0, "total_found": 1,
                        "message": f"Resolved to {root_netloc}, checking sitemap…"})
        sitemap_urls_found = []
        try:
            sitemap_urls_found = _seo_fetch_sitemap_urls(start_url, root_netloc)
            if sitemap_urls_found:
                state["q"].put({"type": "status", "page": 0, "url": start_url,
                                "queued": len(sitemap_urls_found), "total_found": len(sitemap_urls_found),
                                "message": f"Found {len(sitemap_urls_found)} URLs in sitemap"})
        except Exception:
            pass
        seed_urls = [start_url] + sitemap_urls_found

    def _url_matches_branches(u: str) -> bool:
        if allowed_branches is None:
            return True
        if allowed_branches == []:
            return False
        parsed = _urlparse(u)
        parts = [p for p in parsed.path.strip("/").split("/") if p]
        branch = "/" + parts[0] + "/" if parts else "/"
        return branch in allowed_branches

    visited: set = set()
    to_visit_set: set = set()
    to_visit: list = []

    def _enqueue(u: str, force: bool = False):
        norm = _seo_normalise_url(u)
        if norm not in visited and norm not in to_visit_set and (force or _url_matches_branches(norm)):
            to_visit_set.add(norm)
            to_visit.append(norm)

    for su in seed_urls:
        _enqueue(su, force=True)

    page_reports: list = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent=_SEO_UA, viewport={"width": 1920, "height": 1080},
        )
        pages_tabs = [await context.new_page() for _ in range(_SEO_PARALLEL_TABS)]

        async def _process_url(tab, url_to_crawl):
            try:
                html, timing = await _seo_fetch_rendered_html(url_to_crawl, tab)
            except Exception as exc:
                state["q"].put({"type": "page_error", "url": url_to_crawl, "error": str(exc)[:300]})
                return
            try:
                new_links = _seo_discover_internal_links(html, url_to_crawl, root_netloc)
                for link in new_links:
                    _enqueue(link)
            except Exception:
                pass
            try:
                report = _seo_analyze_html(html, url_to_crawl, timing)
                page_reports.append(report)
                state["q"].put({"type": "page_done", "report": report})
            except Exception as exc:
                state["q"].put({"type": "page_error", "url": url_to_crawl, "error": str(exc)[:300]})

        while to_visit and len(visited) < max_pages:
            batch: list = []
            while to_visit and len(batch) < _SEO_PARALLEL_TABS and (len(visited) + len(batch)) < max_pages:
                url = to_visit.pop(0)
                norm = _seo_normalise_url(url)
                if norm in visited:
                    continue
                visited.add(norm)
                batch.append(norm)
            if not batch:
                break
            page_num = len(visited)
            state["q"].put({"type": "status", "page": page_num, "url": batch[0],
                            "queued": len(to_visit), "total_found": len(visited) + len(to_visit)})
            await _asyncio.gather(*[_process_url(pages_tabs[i % len(pages_tabs)], burl)
                                    for i, burl in enumerate(batch)])

        await browser.close()

    summary = _seo_build_site_summary(page_reports)
    try:
        saved = _seo_save_report_files(start_url, summary, page_reports, outputs_dir)
        state["q"].put({"type": "status", "page": len(visited), "url": "",
                        "queued": 0, "total_found": len(visited),
                        "message": f"Report saved: {Path(saved['json']).name}"})
    except Exception as e:
        state["q"].put({"type": "status", "page": len(visited), "url": "",
                        "queued": 0, "total_found": len(visited),
                        "message": f"Warning: could not save report: {e}"})
    state["q"].put({"type": "complete", "summary": summary})


# ---- SEO Routes -----------------------------------------------------------

def _seo_check_available():
    if not _SEO_AVAILABLE:
        from flask import jsonify as _j
        return _j({"error": "SEO Analyzer dependencies not installed on this server."}), 503
    return None


@app.post("/seo/analyze")
@require_auth
def seo_analyze():
    err = _seo_check_available()
    if err:
        return err
    data = request.get_json(force=True)
    url = data.get("url", "").strip()
    if not url:
        return jsonify({"error": "No URL provided."}), 400
    if not url.startswith("http"):
        url = "https://" + url

    async def _single():
        from playwright.async_api import async_playwright
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            ctx = await browser.new_context(user_agent=_SEO_UA, viewport={"width": 1920, "height": 1080})
            page = await ctx.new_page()
            html, timing = await _seo_fetch_rendered_html(url, page)
            await browser.close()
        return html, timing

    try:
        html, timing = _asyncio.run(_single())
    except Exception as e:
        return jsonify({"error": f"Failed to fetch page: {e}"}), 500
    try:
        report = _seo_analyze_html(html, url, timing)
    except Exception as e:
        return jsonify({"error": f"Analysis failed: {e}"}), 500
    return jsonify(report)


@app.post("/seo/prescan")
@require_auth
def seo_prescan():
    err = _seo_check_available()
    if err:
        return err
    data = request.get_json(force=True)
    url = data.get("url", "").strip()
    if not url:
        return jsonify({"error": "No URL provided."}), 400
    if not url.startswith("http"):
        url = "https://" + url
    try:
        result = _seo_prescan(url)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": f"Pre-scan failed: {e}"}), 500


@app.post("/seo/crawl")
@require_auth
def seo_crawl_start():
    err = _seo_check_available()
    if err:
        return err
    data = request.get_json(force=True)
    url = data.get("url", "").strip()
    max_pages = min(int(data.get("max_pages", 100)), 500)
    if not url:
        return jsonify({"error": "No URL provided."}), 400
    if not url.startswith("http"):
        url = "https://" + url

    user_id = str(g.current_user["id"])
    outputs_dir = _seo_user_dir(user_id)
    seed_urls = data.get("seed_urls")
    allowed_branches = data.get("allowed_branches")

    state: dict = {"q": _queue.Queue(), "started": True}
    _seo_crawl_states[user_id] = state

    t = _threading.Thread(
        target=_seo_run_crawl,
        args=(url, max_pages, state, outputs_dir, seed_urls, allowed_branches),
        daemon=True,
    )
    t.start()
    return jsonify({"status": "started", "url": url, "max_pages": max_pages})


@app.get("/seo/crawl/stream")
@require_auth
def seo_crawl_stream():
    user_id = str(g.current_user["id"])

    def generate():
        state = _seo_crawl_states.get(user_id)
        if not state or not state.get("started"):
            yield f"data: {json.dumps({'type': 'error', 'error': 'No crawl in progress'})}\n\n"
            return
        q = state["q"]
        while True:
            try:
                msg = q.get(timeout=120)
                yield f"data: {json.dumps(msg)}\n\n"
                if msg.get("type") == "complete":
                    break
            except Exception:
                yield f"data: {json.dumps({'type': 'error', 'error': 'Timeout waiting for crawl data'})}\n\n"
                break

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/seo/reports")
@require_auth
def seo_list_reports():
    user_id = str(g.current_user["id"])
    outputs_dir = _seo_user_dir(user_id)
    try:
        reports = []
        for f in sorted(outputs_dir.glob("*.json"), key=lambda x: x.stat().st_mtime, reverse=True):
            try:
                stat = f.stat()
                reports.append({
                    "filename": f.name,
                    "size": stat.st_size,
                    "modified": datetime.datetime.fromtimestamp(stat.st_mtime).isoformat(),
                })
            except Exception:
                continue
        return jsonify({"reports": reports}), 200
    except Exception as e:
        return jsonify({"error": f"Failed to list reports: {str(e)}"}), 500


@app.get("/seo/reports/<filename>")
@require_auth
def seo_get_report(filename):
    import re as _re2
    if not _re2.match(r'^[\w.\-]+\.json$', filename):
        return jsonify({"error": "Invalid filename"}), 400
    user_id = str(g.current_user["id"])
    outputs_dir = _seo_user_dir(user_id)
    file_path = outputs_dir / filename
    if not file_path.exists():
        return jsonify({"error": "Report not found"}), 404
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": f"Failed to load report: {e}"}), 500


# ─── HEIC → JPEG conversion ──────────────────────────────────────────────────

@app.post("/api/heic-to-jpeg")
def heic_to_jpeg():
    """Accepts a HEIC file — either multipart/form-data field 'file' or raw octet-stream body."""
    import io
    from pillow_heif import register_heif_opener
    from PIL import Image
    from flask import Response

    # Support both multipart upload and raw binary body
    f = request.files.get("file")
    if f:
        data = f.read()
    elif request.data:
        data = request.data
    else:
        return jsonify({"error": "No file provided"}), 400

    try:
        register_heif_opener()
        img = Image.open(io.BytesIO(data))
        buf = io.BytesIO()
        img.convert("RGB").save(buf, format="JPEG", quality=90)
        buf.seek(0)
    except Exception as e:
        return jsonify({"error": f"HEIC conversion failed: {e}"}), 500
    return Response(buf.read(), mimetype="image/jpeg")

# ─── Static uploads ───────────────────────────────────────────────────────────

@app.get("/uploads/<path:filename>")
def serve_upload(filename):
    path = UPLOADS_DIR / filename
    if not path.exists():
        return jsonify({"error": "Not found"}), 404
    try:
        data = read_encrypted(path)
    except Exception:
        # Fall back to serving unencrypted (for pre-existing files)
        return send_from_directory(str(UPLOADS_DIR), filename)
    mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    return Response(data, mimetype=mime)

# ─── Health ───────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return jsonify({"status": "ok", "version": "1.0.0"})

# ─── Helpers ──────────────────────────────────────────────────────────────────

def _decode_data_url(data_url):
    """Returns (bytes, ext) or (None, None)."""
    import base64, re
    m = re.match(r"^data:([^;]+);base64,(.+)$", data_url or "")
    if not m:
        return None, None
    mime = m.group(1)
    buf  = base64.b64decode(m.group(2))
    ext  = mime.split("/")[1].replace("jpeg", "jpg")
    return buf, ext

# ─── Startup ──────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    init_db()
    log.info("✓ mw-backend starting on http://0.0.0.0:%s", PORT)
    from waitress import serve
    serve(app, host="0.0.0.0", port=PORT)
