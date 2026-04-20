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
import urllib.request as _urllib_req
from pathlib import Path
from functools import wraps

from flask import Flask, request, jsonify, g, send_from_directory, send_file, Response
from flask_cors import CORS
import bcrypt
import jwt
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

# ─── Paths & config ──────────────────────────────────────────────────────────

BASE_DIR     = Path(__file__).parent
DATA_DIR     = BASE_DIR / "data"
UPLOADS_DIR  = DATA_DIR / "uploads"

for d in [DATA_DIR, UPLOADS_DIR / "walls", UPLOADS_DIR / "pieces", UPLOADS_DIR / "library"]:
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
CORS(app, origins=_CORS_ORIGINS, supports_credentials=True)

from werkzeug.exceptions import HTTPException

@app.errorhandler(Exception)
def handle_exception(e):
    """Return JSON instead of HTML for unhandled non-HTTP exceptions."""
    if isinstance(e, HTTPException):
        return e  # let Flask handle normal HTTP errors normally
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
    wall_id     TEXT NOT NULL,
    owner_type  TEXT NOT NULL,
    owner_id    TEXT NOT NULL,
    name        TEXT NOT NULL,
    pieces      TEXT NOT NULL,   -- JSON array
    updated_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
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
    conn.commit()
    conn.close()
    print(f"✓ Database ready: {DB_PATH}")

# ─── Auth helpers ─────────────────────────────────────────────────────────────

def make_token(user_id, user=None):
    payload = {
        "sub": user_id,
        "exp": datetime.datetime.utcnow() + ACCESS_TTL,
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
    expires_at = (datetime.datetime.utcnow() + datetime.timedelta(hours=RESET_TOKEN_TTL_H)).isoformat()
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
    expires = datetime.datetime.fromisoformat(row["expires_at"])
    if datetime.datetime.utcnow() > expires:
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
        "SELECT wall_id, name, pieces FROM gallery_layouts WHERE owner_type=? AND owner_id=?",
        (g.owner_type, g.owner_id)
    ).fetchall()
    layouts = {}
    for r in layout_rows:
        layouts.setdefault(r["wall_id"], {})[r["name"]] = json.loads(r["pieces"])

    # library
    lib_rows = db.execute(
        "SELECT id, data FROM gallery_library WHERE owner_type=? AND owner_id=?",
        (g.owner_type, g.owner_id)
    ).fetchall()
    library = {r["id"]: json.loads(r["data"]) for r in lib_rows}

    return jsonify({"walls": walls, "layouts": layouts, "library": library})

# ─── Gallery: walls ───────────────────────────────────────────────────────────

@app.put("/api/walls/<wall_id>")
@require_owner
def gallery_put_wall(wall_id):
    db  = get_db()
    now = datetime.datetime.utcnow().isoformat()
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
    pieces = (request.get_json(silent=True) or {}).get("pieces", [])
    now    = datetime.datetime.utcnow().isoformat()
    db     = get_db()
    db.execute(
        "INSERT INTO gallery_layouts (wall_id, owner_type, owner_id, name, pieces, updated_at) VALUES (?,?,?,?,?,?) "
        "ON CONFLICT(wall_id, owner_type, owner_id, name) DO UPDATE SET pieces=excluded.pieces, updated_at=excluded.updated_at",
        (wall_id, g.owner_type, g.owner_id, name, json.dumps(pieces), now)
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
    now = datetime.datetime.utcnow().isoformat()
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
        payload = jwt.decode(token, SECRET_KEY, algorithms=["HS256"])
        user = get_db().execute("SELECT * FROM users WHERE id=?", (payload["sub"],)).fetchone()
        if not user:
            raise ValueError("unknown user")
    except Exception:
        return jsonify({"error": "Forbidden"}), 403

    user_id = str(user["id"])
    db = get_db()
    now = datetime.datetime.utcnow().isoformat()
    counts = {"walls": 0, "layouts": 0, "library": 0, "images": 0}

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
        for name, pieces in named.items():
            db.execute(
                "INSERT INTO gallery_layouts(wall_id,owner_type,owner_id,name,pieces,updated_at) "
                "VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(wall_id,owner_type,owner_id,name) DO UPDATE SET "
                "pieces=excluded.pieces, updated_at=excluded.updated_at",
                (wall_id, "user", user_id, name, json.dumps(pieces), now)
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
