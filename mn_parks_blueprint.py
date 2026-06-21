"""
mn_parks_blueprint.py — MN State Park Tracker backend
Routes: /mn-parks/*
Tables: mn_users, mn_visits, mn_photos, mn_user_settings (isolated)
"""
import os, sqlite3, datetime, mimetypes
from pathlib import Path
from functools import wraps
import bcrypt, jwt
from flask import Blueprint, request, jsonify, send_file, g

mn_parks_bp = Blueprint("mn_parks", __name__, url_prefix="/mn-parks")

BASE_DIR   = Path(__file__).parent
DATA_DIR   = BASE_DIR / "data"
PHOTOS_DIR = DATA_DIR / "mn-parks-photos"
PHOTOS_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH    = DATA_DIR / "mw.db"
SECRET_KEY_FILE = DATA_DIR / ".secret_key"
SECRET_KEY = SECRET_KEY_FILE.read_text().strip() if SECRET_KEY_FILE.exists() else "mn_parks_secret"
JWT_ALGO   = "HS256"
TTL        = datetime.timedelta(days=30)

DEMO_EMAIL = "demo@mnparks.test"
DEMO_PASS  = "Parks2024!"

# ── DB helpers ──────────────────────────────────────────────────────────────

def get_db():
    if "mn_db" not in g:
        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        g.mn_db = conn
    return g.mn_db

@mn_parks_bp.teardown_app_request
def close_db(exc):
    db = g.pop("mn_db", None)
    if db: db.close()

def init_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS mn_users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS mn_visits (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            park_id INTEGER NOT NULL,
            date_visited TEXT,
            attendees TEXT,
            notes TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY(user_id) REFERENCES mn_users(id)
        );
        CREATE TABLE IF NOT EXISTS mn_photos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            visit_id INTEGER NOT NULL,
            file_path TEXT NOT NULL,
            created_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY(visit_id) REFERENCES mn_visits(id)
        );
        CREATE TABLE IF NOT EXISTS mn_user_settings (
            user_id INTEGER PRIMARY KEY,
            home_lat REAL,
            home_lng REAL,
            FOREIGN KEY(user_id) REFERENCES mn_users(id)
        );
    """)
    # Seed demo account
    existing = conn.execute("SELECT id FROM mn_users WHERE email=?", (DEMO_EMAIL,)).fetchone()
    if not existing:
        pw_hash = bcrypt.hashpw(DEMO_PASS.encode(), bcrypt.gensalt()).decode()
        conn.execute("INSERT INTO mn_users (email, password_hash) VALUES (?,?)", (DEMO_EMAIL, pw_hash))
        conn.commit()
        # Seed a few demo visits for the demo account
        uid = conn.execute("SELECT id FROM mn_users WHERE email=?", (DEMO_EMAIL,)).fetchone()["id"]
        demo_visits = [
            (uid, 26, "2024-07-04", "Sarah, Tom", "Gooseberry Falls was stunning — waterfalls were roaring after the rain."),
            (uid, 67, "2024-08-10", "Family", "Split Rock Lighthouse tour was incredible. The kids loved it."),
            (uid, 14, "2024-06-15", "Mike", "Cuyuna mine pits are unreal. That turquoise water!"),
        ]
        conn.executemany(
            "INSERT INTO mn_visits (user_id, park_id, date_visited, attendees, notes) VALUES (?,?,?,?,?)",
            demo_visits
        )
        conn.execute("INSERT INTO mn_user_settings (user_id, home_lat, home_lng) VALUES (?,?,?)",
                     (uid, 44.9778, -93.2650))  # Minneapolis
        conn.commit()
    conn.close()

init_db()

# ── Auth helpers ─────────────────────────────────────────────────────────────

def make_token(user_id):
    payload = {"sub": str(user_id), "exp": datetime.datetime.utcnow() + TTL}
    return jwt.encode(payload, SECRET_KEY, algorithm=JWT_ALGO)

def require_auth(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return jsonify({"error": "unauthorized"}), 401
        try:
            payload = jwt.decode(auth[7:], SECRET_KEY, algorithms=[JWT_ALGO])
            g.user_id = int(payload["sub"])
        except jwt.ExpiredSignatureError:
            return jsonify({"error": "token expired"}), 401
        except Exception:
            return jsonify({"error": "invalid token"}), 401
        return f(*args, **kwargs)
    return wrapper

# ── Auth routes ──────────────────────────────────────────────────────────────

@mn_parks_bp.route("/auth/register", methods=["POST"])
def register():
    data = request.get_json(force=True) or {}
    email = (data.get("email") or "").strip().lower()
    password = data.get("password", "")
    if not email or len(password) < 6:
        return jsonify({"error": "Valid email and password (min 6 chars) required"}), 400
    db = get_db()
    if db.execute("SELECT id FROM mn_users WHERE email=?", (email,)).fetchone():
        return jsonify({"error": "Email already registered"}), 409
    pw_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
    cur = db.execute("INSERT INTO mn_users (email, password_hash) VALUES (?,?)", (email, pw_hash))
    db.commit()
    token = make_token(cur.lastrowid)
    return jsonify({"token": token, "email": email}), 201

@mn_parks_bp.route("/auth/login", methods=["POST"])
def login():
    data = request.get_json(force=True) or {}
    email = (data.get("email") or "").strip().lower()
    password = data.get("password", "")
    db = get_db()
    row = db.execute("SELECT id, password_hash FROM mn_users WHERE email=?", (email,)).fetchone()
    if not row or not bcrypt.checkpw(password.encode(), row["password_hash"].encode()):
        return jsonify({"error": "Invalid email or password"}), 401
    token = make_token(row["id"])
    return jsonify({"token": token, "email": email})

# ── Visits ───────────────────────────────────────────────────────────────────

@mn_parks_bp.route("/visits", methods=["GET"])
@require_auth
def get_visits():
    db = get_db()
    rows = db.execute(
        "SELECT v.id, v.park_id, v.date_visited, v.attendees, v.notes, v.created_at, "
        "GROUP_CONCAT(p.id) as photo_ids "
        "FROM mn_visits v LEFT JOIN mn_photos p ON p.visit_id=v.id "
        "WHERE v.user_id=? GROUP BY v.id ORDER BY v.date_visited DESC",
        (g.user_id,)
    ).fetchall()
    visits = []
    for r in rows:
        visits.append({
            "id": r["id"],
            "park_id": r["park_id"],
            "date_visited": r["date_visited"],
            "attendees": r["attendees"],
            "notes": r["notes"],
            "created_at": r["created_at"],
            "photo_ids": [int(x) for x in r["photo_ids"].split(",")] if r["photo_ids"] else []
        })
    return jsonify(visits)

@mn_parks_bp.route("/visits", methods=["POST"])
@require_auth
def create_visit():
    data = request.get_json(force=True) or {}
    park_id = data.get("park_id")
    if not park_id:
        return jsonify({"error": "park_id required"}), 400
    db = get_db()
    cur = db.execute(
        "INSERT INTO mn_visits (user_id, park_id, date_visited, attendees, notes) VALUES (?,?,?,?,?)",
        (g.user_id, park_id, data.get("date_visited"), data.get("attendees"), data.get("notes"))
    )
    db.commit()
    return jsonify({"id": cur.lastrowid, "park_id": park_id}), 201

@mn_parks_bp.route("/visits/<int:vid>", methods=["PATCH"])
@require_auth
def update_visit(vid):
    db = get_db()
    row = db.execute("SELECT id FROM mn_visits WHERE id=? AND user_id=?", (vid, g.user_id)).fetchone()
    if not row:
        return jsonify({"error": "not found"}), 404
    data = request.get_json(force=True) or {}
    fields = []
    vals = []
    for col in ["date_visited", "attendees", "notes"]:
        if col in data:
            fields.append(f"{col}=?")
            vals.append(data[col])
    if fields:
        vals.append(vid)
        db.execute(f"UPDATE mn_visits SET {', '.join(fields)} WHERE id=?", vals)
        db.commit()
    return jsonify({"ok": True})

@mn_parks_bp.route("/visits/<int:vid>", methods=["DELETE"])
@require_auth
def delete_visit(vid):
    db = get_db()
    row = db.execute("SELECT id FROM mn_visits WHERE id=? AND user_id=?", (vid, g.user_id)).fetchone()
    if not row:
        return jsonify({"error": "not found"}), 404
    # Delete photos from filesystem
    photos = db.execute("SELECT file_path FROM mn_photos WHERE visit_id=?", (vid,)).fetchall()
    for p in photos:
        fp = Path(p["file_path"])
        if fp.exists():
            fp.unlink()
    db.execute("DELETE FROM mn_photos WHERE visit_id=?", (vid,))
    db.execute("DELETE FROM mn_visits WHERE id=?", (vid,))
    db.commit()
    return jsonify({"ok": True})

# ── Photos ───────────────────────────────────────────────────────────────────

@mn_parks_bp.route("/visits/<int:vid>/photos", methods=["POST"])
@require_auth
def upload_photo(vid):
    db = get_db()
    row = db.execute("SELECT id FROM mn_visits WHERE id=? AND user_id=?", (vid, g.user_id)).fetchone()
    if not row:
        return jsonify({"error": "not found"}), 404
    file = request.files.get("photo")
    if not file:
        return jsonify({"error": "no photo"}), 400
    user_dir = PHOTOS_DIR / str(g.user_id) / str(vid)
    user_dir.mkdir(parents=True, exist_ok=True)
    ext = Path(file.filename).suffix if file.filename else ".jpg"
    import time
    fname = f"{int(time.time()*1000)}{ext}"
    fpath = user_dir / fname
    file.save(str(fpath))
    cur = db.execute("INSERT INTO mn_photos (visit_id, file_path) VALUES (?,?)",
                     (vid, str(fpath)))
    db.commit()
    return jsonify({"id": cur.lastrowid}), 201

@mn_parks_bp.route("/visits/<int:vid>/photos/<int:pid>", methods=["GET"])
@require_auth
def get_photo(vid, pid):
    db = get_db()
    # Verify ownership via visit
    row = db.execute(
        "SELECT p.file_path FROM mn_photos p "
        "JOIN mn_visits v ON v.id=p.visit_id "
        "WHERE p.id=? AND p.visit_id=? AND v.user_id=?",
        (pid, vid, g.user_id)
    ).fetchone()
    if not row:
        return jsonify({"error": "not found"}), 404
    fpath = Path(row["file_path"])
    if not fpath.exists():
        return jsonify({"error": "file missing"}), 404
    mime = mimetypes.guess_type(str(fpath))[0] or "image/jpeg"
    return send_file(str(fpath), mimetype=mime)

@mn_parks_bp.route("/visits/<int:vid>/photos/<int:pid>", methods=["DELETE"])
@require_auth
def delete_photo(vid, pid):
    db = get_db()
    row = db.execute(
        "SELECT p.file_path FROM mn_photos p "
        "JOIN mn_visits v ON v.id=p.visit_id "
        "WHERE p.id=? AND p.visit_id=? AND v.user_id=?",
        (pid, vid, g.user_id)
    ).fetchone()
    if not row:
        return jsonify({"error": "not found"}), 404
    fpath = Path(row["file_path"])
    if fpath.exists():
        fpath.unlink()
    db.execute("DELETE FROM mn_photos WHERE id=?", (pid,))
    db.commit()
    return jsonify({"ok": True})

# ── User settings (home location) ────────────────────────────────────────────

@mn_parks_bp.route("/user/home", methods=["GET"])
@require_auth
def get_home():
    db = get_db()
    row = db.execute("SELECT home_lat, home_lng FROM mn_user_settings WHERE user_id=?",
                     (g.user_id,)).fetchone()
    if not row or row["home_lat"] is None:
        return jsonify({"home_lat": None, "home_lng": None})
    return jsonify({"home_lat": row["home_lat"], "home_lng": row["home_lng"]})

@mn_parks_bp.route("/user/home", methods=["PUT"])
@require_auth
def set_home():
    data = request.get_json(force=True) or {}
    lat = data.get("home_lat")
    lng = data.get("home_lng")
    if lat is None or lng is None:
        return jsonify({"error": "home_lat and home_lng required"}), 400
    db = get_db()
    db.execute(
        "INSERT INTO mn_user_settings (user_id, home_lat, home_lng) VALUES (?,?,?) "
        "ON CONFLICT(user_id) DO UPDATE SET home_lat=excluded.home_lat, home_lng=excluded.home_lng",
        (g.user_id, float(lat), float(lng))
    )
    db.commit()
    return jsonify({"ok": True})
