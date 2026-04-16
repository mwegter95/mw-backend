"""
mw-backend — Shared backend for michaelwegter.com projects.
Handles auth + the Gallery Wall Planner API in one server.

To add more projects later, just add more route sections below.

Run:
  ./start.sh            (local only)
  ./start.sh --tunnel   (local + Cloudflare Tunnel for internet access)
"""

import os
import json
import sqlite3
import secrets
import datetime
import mimetypes
import subprocess
import tempfile
from pathlib import Path
from functools import wraps

from flask import Flask, request, jsonify, g, send_from_directory
from flask_cors import CORS
import bcrypt
import jwt

# ─── Paths & config ──────────────────────────────────────────────────────────

BASE_DIR     = Path(__file__).parent
DATA_DIR     = BASE_DIR / "data"
UPLOADS_DIR  = DATA_DIR / "uploads"

for d in [DATA_DIR, UPLOADS_DIR / "walls", UPLOADS_DIR / "pieces", UPLOADS_DIR / "library"]:
    d.mkdir(parents=True, exist_ok=True)

DB_PATH    = DATA_DIR / "mw.db"
PORT       = int(os.environ.get("PORT", 5050))
ACCESS_TTL = datetime.timedelta(days=7)

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

# ─── Flask app ────────────────────────────────────────────────────────────────

app = Flask(__name__)
CORS(app, origins=_CORS_ORIGINS, supports_credentials=True)

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

def make_token(user_id):
    payload = {"sub": user_id, "exp": datetime.datetime.utcnow() + ACCESS_TTL}
    return jwt.encode(payload, SECRET_KEY, algorithm="HS256")

def _resolve_principal():
    """Returns (user_row | None, device_token | None)."""
    auth = request.headers.get("Authorization", "")
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
    return jsonify({"token": make_token(user["id"]), "user": _user_dict(user)}), 201


@app.post("/auth/login")
def auth_login():
    d     = request.get_json(silent=True) or {}
    email = (d.get("email") or "").strip().lower()
    pw    = d.get("password") or ""
    if not email or not pw:
        return jsonify({"error": "email and password required"}), 400
    db   = get_db()
    user = db.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
    if not user or not bcrypt.checkpw(pw.encode(), user["password_hash"].encode()):
        return jsonify({"error": "Invalid email or password"}), 401
    _claim_device(db, str(user["id"]), d.get("device_token"))
    return jsonify({"token": make_token(user["id"]), "user": _user_dict(user)})


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
        "ON CONFLICT(id) DO UPDATE SET data=excluded.data, updated_at=excluded.updated_at",
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
    path.write_bytes(buf)
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
    path.write_bytes(buf)
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
    path.write_bytes(buf)
    return jsonify({"url": f"/uploads/library/{lib_id}.{ext}"})

# ─── HEIC → JPEG conversion ──────────────────────────────────────────────────

@app.post("/api/heic-to-jpeg")
def heic_to_jpeg():
    """Accepts a HEIC file upload (multipart field 'file') and returns JPEG bytes."""
    f = request.files.get("file")
    if not f:
        return jsonify({"error": "No file provided"}), 400
    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, "input.heic")
        dst = os.path.join(tmp, "output.jpg")
        f.save(src)
        result = subprocess.run(
            ["sips", "-s", "format", "jpeg", src, "--out", dst],
            capture_output=True, timeout=30
        )
        if result.returncode != 0 or not os.path.exists(dst):
            return jsonify({"error": "HEIC conversion failed"}), 500
        with open(dst, "rb") as fh:
            jpeg_bytes = fh.read()
    from flask import Response
    return Response(jpeg_bytes, mimetype="image/jpeg")

# ─── Static uploads ───────────────────────────────────────────────────────────

@app.get("/uploads/<path:filename>")
def serve_upload(filename):
    return send_from_directory(str(UPLOADS_DIR), filename)

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
    print(f"✓ mw-backend running on http://0.0.0.0:{PORT}")
    app.run(host="0.0.0.0", port=PORT, debug=False)
