"""
EdTech School Connect Portal -- Flask Blueprint
Mounts under /api/edtech on the mw-backend server.

Backs the edtech-school-connect-web-portal demo at
https://michaelwegter.com/demos/edtech-school-connect-web-portal/.

Endpoints:
  POST  /api/edtech/login                  Credential check, returns JWT with role claim
  GET   /api/edtech/student                Student info (both roles)
  GET   /api/edtech/grades                 Student grades (both roles)
  PATCH /api/edtech/grades/<id>            Update a grade (teacher only)
  GET   /api/edtech/activities             Activity feed (both roles)
  POST  /api/edtech/activities             Create activity (teacher only)
  GET   /api/edtech/messages               Full message thread (both roles)
  POST  /api/edtech/messages               Send a message (both roles)
  GET   /api/edtech/messages/unread-count  Unread count for polling (both roles)
  PATCH /api/edtech/messages/read-all      Mark all other-role messages as read
  PATCH /api/edtech/messages/<int:id>/read Mark one message as read

Auth: Role-bearing JWT stored per-request. Hardcoded demo secret (isolated from server SECRET_KEY).
CORS: handled globally by server.py -- no per-blueprint CORS config needed.
Storage: SQLite at data/mw.db, tables created lazily.
"""

import sqlite3
import datetime
from pathlib import Path
from functools import wraps

import bcrypt
import jwt as _jwt
from flask import Blueprint, request, jsonify

# ─── Blueprint ────────────────────────────────────────────────────────────────

edtech_bp = Blueprint("edtech", __name__, url_prefix="/api/edtech")

EDTECH_JWT_SECRET = "edtech-demo-2026"
_JWT_ALG = "HS256"
_JWT_TTL = datetime.timedelta(hours=24)

DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "mw.db"


# ─── DB helpers ───────────────────────────────────────────────────────────────

def _db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _init_db():
    conn = _db()
    try:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS edtech_users (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                email         TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                role          TEXT NOT NULL,
                name          TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS edtech_students (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                name       TEXT NOT NULL,
                grade      TEXT NOT NULL,
                teacher_id INTEGER NOT NULL,
                parent_id  INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS edtech_grades (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                student_id   INTEGER NOT NULL,
                subject      TEXT NOT NULL,
                letter_grade TEXT NOT NULL,
                score        INTEGER NOT NULL,
                updated_at   TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS edtech_activities (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                student_id   INTEGER NOT NULL,
                category     TEXT NOT NULL,
                content      TEXT NOT NULL,
                teacher_name TEXT NOT NULL,
                created_at   TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS edtech_messages (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                student_id  INTEGER NOT NULL,
                sender_id   INTEGER NOT NULL,
                sender_name TEXT NOT NULL,
                sender_role TEXT NOT NULL,
                body        TEXT NOT NULL,
                is_read     INTEGER NOT NULL DEFAULT 0,
                created_at  TEXT NOT NULL
            );
        """)

        count = conn.execute("SELECT COUNT(*) FROM edtech_users").fetchone()[0]
        if count == 0:
            now = datetime.datetime.utcnow().isoformat()

            parent_hash  = bcrypt.hashpw(b"ParentDemo1",  bcrypt.gensalt()).decode()
            teacher_hash = bcrypt.hashpw(b"TeachDemo1",   bcrypt.gensalt()).decode()

            conn.execute(
                "INSERT INTO edtech_users (email, password_hash, role, name) VALUES (?, ?, ?, ?)",
                ("parent@demo.edu",  parent_hash,  "parent",  "Robert Johnson"),
            )
            conn.execute(
                "INSERT INTO edtech_users (email, password_hash, role, name) VALUES (?, ?, ?, ?)",
                ("teacher@demo.edu", teacher_hash, "teacher", "Mrs. Rivera"),
            )
            conn.commit()

            parent_id  = conn.execute("SELECT id FROM edtech_users WHERE email='parent@demo.edu'").fetchone()[0]
            teacher_id = conn.execute("SELECT id FROM edtech_users WHERE email='teacher@demo.edu'").fetchone()[0]

            conn.execute(
                "INSERT INTO edtech_students (name, grade, teacher_id, parent_id) VALUES (?, ?, ?, ?)",
                ("Alex Johnson", "Grade 7", teacher_id, parent_id),
            )
            conn.commit()

            student_id = conn.execute("SELECT id FROM edtech_students LIMIT 1").fetchone()[0]

            grades = [
                ("Math",    "A",   95),
                ("English", "B+",  88),
                ("Science", "A-",  91),
                ("History", "B",   84),
                ("Art",     "A",   97),
                ("PE",      "A+", 100),
            ]
            for subject, letter, score in grades:
                conn.execute(
                    "INSERT INTO edtech_grades (student_id, subject, letter_grade, score, updated_at) VALUES (?, ?, ?, ?, ?)",
                    (student_id, subject, letter, score, now),
                )

            activities = [
                ("Academic",     "Alex earned an A on the Math Midterm"),
                ("Event",        "Science Fair on June 20th in the gymnasium -- all parents welcome"),
                ("Attendance",   "Alex was marked present all 5 days this week"),
                ("Announcement", "Parent-Teacher conference slots open for booking -- sign up by Friday"),
            ]
            for category, content in activities:
                conn.execute(
                    "INSERT INTO edtech_activities (student_id, category, content, teacher_name, created_at) VALUES (?, ?, ?, ?, ?)",
                    (student_id, category, content, "Mrs. Rivera", now),
                )

            messages = [
                (teacher_id, "Mrs. Rivera", "teacher",
                 "Hi! I'm Mrs. Rivera, Alex's homeroom teacher. Alex had a great start to the term."),
                (parent_id,  "Robert Johnson", "parent",
                 "Thank you! We have been working on the science project at home. Any tips?"),
            ]
            for sender_id, sender_name, sender_role, body in messages:
                conn.execute(
                    "INSERT INTO edtech_messages "
                    "(student_id, sender_id, sender_name, sender_role, body, is_read, created_at) "
                    "VALUES (?, ?, ?, ?, ?, 0, ?)",
                    (student_id, sender_id, sender_name, sender_role, body, now),
                )

            conn.commit()
    finally:
        conn.close()


_init_db()


# ─── Auth helpers ─────────────────────────────────────────────────────────────

def _make_token(user_id: int, role: str, name: str) -> str:
    payload = {
        "user_id": user_id,
        "role":    role,
        "name":    name,
        "exp":     datetime.datetime.utcnow() + _JWT_TTL,
    }
    return _jwt.encode(payload, EDTECH_JWT_SECRET, algorithm=_JWT_ALG)


def _decode_token(token: str) -> dict:
    return _jwt.decode(token, EDTECH_JWT_SECRET, algorithms=[_JWT_ALG])


def require_auth(f):
    """Decorator: any valid JWT."""
    @wraps(f)
    def wrapper(*args, **kwargs):
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return jsonify({"error": "Unauthorized"}), 401
        try:
            request.et_user = _decode_token(auth[7:])
        except _jwt.ExpiredSignatureError:
            return jsonify({"error": "Token expired"}), 401
        except Exception:
            return jsonify({"error": "Invalid token"}), 401
        return f(*args, **kwargs)
    return wrapper


def require_role(allowed_roles):
    """Decorator factory: only allow tokens whose role is in allowed_roles."""
    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            auth = request.headers.get("Authorization", "")
            if not auth.startswith("Bearer "):
                return jsonify({"error": "Unauthorized"}), 401
            try:
                payload = _decode_token(auth[7:])
                if payload.get("role") not in allowed_roles:
                    return jsonify({"error": "Forbidden"}), 403
                request.et_user = payload
            except _jwt.ExpiredSignatureError:
                return jsonify({"error": "Token expired"}), 401
            except Exception:
                return jsonify({"error": "Invalid token"}), 401
            return f(*args, **kwargs)
        return wrapper
    return decorator


def _student_for_user(conn, user_id, role):
    """Return the student row linked to the authenticated user."""
    if role == "parent":
        return conn.execute(
            "SELECT s.*, u.name AS teacher_name "
            "FROM edtech_students s JOIN edtech_users u ON s.teacher_id = u.id "
            "WHERE s.parent_id = ?",
            (user_id,),
        ).fetchone()
    return conn.execute(
        "SELECT s.*, u.name AS teacher_name "
        "FROM edtech_students s JOIN edtech_users u ON s.teacher_id = u.id "
        "WHERE s.teacher_id = ?",
        (user_id,),
    ).fetchone()


# ─── Routes ───────────────────────────────────────────────────────────────────

@edtech_bp.route("/login", methods=["POST"])
def login():
    """POST { email, password } -> { token, role, name }"""
    data     = request.get_json(silent=True) or {}
    email    = (data.get("email")    or "").strip().lower()
    password = (data.get("password") or "").strip()

    conn = _db()
    try:
        user = conn.execute(
            "SELECT * FROM edtech_users WHERE email = ?", (email,)
        ).fetchone()
    finally:
        conn.close()

    if not user:
        return jsonify({"error": "Invalid credentials"}), 401
    if not bcrypt.checkpw(password.encode(), user["password_hash"].encode()):
        return jsonify({"error": "Invalid credentials"}), 401

    token = _make_token(user["id"], user["role"], user["name"])
    return jsonify({"token": token, "role": user["role"], "name": user["name"]})


@edtech_bp.route("/student", methods=["GET"])
@require_auth
def get_student():
    """Return the student associated with the authenticated user."""
    user = request.et_user
    conn = _db()
    try:
        student = _student_for_user(conn, user["user_id"], user["role"])
    finally:
        conn.close()

    if not student:
        return jsonify({"error": "Student not found"}), 404
    return jsonify(dict(student))


@edtech_bp.route("/grades", methods=["GET"])
@require_auth
def get_grades():
    """Return the student's grades."""
    user = request.et_user
    conn = _db()
    try:
        student = _student_for_user(conn, user["user_id"], user["role"])
        if not student:
            return jsonify({"grades": []})
        grades = conn.execute(
            "SELECT * FROM edtech_grades WHERE student_id = ? ORDER BY subject",
            (student["id"],),
        ).fetchall()
    finally:
        conn.close()

    return jsonify({"grades": [dict(g) for g in grades]})


@edtech_bp.route("/grades/<int:grade_id>", methods=["PATCH"])
@require_role(["teacher"])
def update_grade(grade_id):
    """PATCH { letter_grade, score } -> updated grade row (teacher only)."""
    data         = request.get_json(silent=True) or {}
    letter_grade = (data.get("letter_grade") or "").strip()
    score_raw    = data.get("score")

    if not letter_grade or score_raw is None:
        return jsonify({"error": "letter_grade and score are required"}), 400
    try:
        score = int(score_raw)
        if not 0 <= score <= 100:
            raise ValueError
    except (ValueError, TypeError):
        return jsonify({"error": "score must be an integer 0-100"}), 400

    now = datetime.datetime.utcnow().isoformat()
    conn = _db()
    try:
        conn.execute(
            "UPDATE edtech_grades SET letter_grade=?, score=?, updated_at=? WHERE id=?",
            (letter_grade, score, now, grade_id),
        )
        conn.commit()
        grade = conn.execute("SELECT * FROM edtech_grades WHERE id=?", (grade_id,)).fetchone()
    finally:
        conn.close()

    if not grade:
        return jsonify({"error": "Grade not found"}), 404
    return jsonify(dict(grade))


@edtech_bp.route("/activities", methods=["GET"])
@require_auth
def get_activities():
    """Return the activity feed for the student."""
    user = request.et_user
    conn = _db()
    try:
        student = _student_for_user(conn, user["user_id"], user["role"])
        if not student:
            return jsonify({"activities": []})
        activities = conn.execute(
            "SELECT * FROM edtech_activities WHERE student_id=? ORDER BY created_at DESC",
            (student["id"],),
        ).fetchall()
    finally:
        conn.close()

    return jsonify({"activities": [dict(a) for a in activities]})


@edtech_bp.route("/activities", methods=["POST"])
@require_role(["teacher"])
def create_activity():
    """POST { category, content } -> new activity row (teacher only)."""
    data     = request.get_json(silent=True) or {}
    category = (data.get("category") or "").strip()
    content  = (data.get("content")  or "").strip()

    valid = {"Academic", "Event", "Attendance", "Announcement", "Behavior"}
    if category not in valid:
        return jsonify({"error": f"category must be one of {sorted(valid)}"}), 400
    if not content:
        return jsonify({"error": "content is required"}), 400

    user = request.et_user
    now  = datetime.datetime.utcnow().isoformat()
    conn = _db()
    try:
        student = _student_for_user(conn, user["user_id"], "teacher")
        if not student:
            return jsonify({"error": "No student found for this teacher"}), 404
        conn.execute(
            "INSERT INTO edtech_activities (student_id, category, content, teacher_name, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (student["id"], category, content, user["name"], now),
        )
        conn.commit()
        activity = conn.execute(
            "SELECT * FROM edtech_activities ORDER BY id DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()

    return jsonify(dict(activity)), 201


@edtech_bp.route("/messages", methods=["GET"])
@require_auth
def get_messages():
    """Return the full message thread."""
    conn = _db()
    try:
        msgs = conn.execute(
            "SELECT * FROM edtech_messages ORDER BY created_at ASC"
        ).fetchall()
    finally:
        conn.close()

    return jsonify({"messages": [dict(m) for m in msgs]})


@edtech_bp.route("/messages", methods=["POST"])
@require_auth
def send_message():
    """POST { body } -> new message row."""
    data = request.get_json(silent=True) or {}
    body = (data.get("body") or "").strip()
    if not body:
        return jsonify({"error": "body is required"}), 400

    user = request.et_user
    now  = datetime.datetime.utcnow().isoformat()
    conn = _db()
    try:
        student = _student_for_user(conn, user["user_id"], user["role"])
        if not student:
            return jsonify({"error": "No student found for this user"}), 404
        conn.execute(
            "INSERT INTO edtech_messages "
            "(student_id, sender_id, sender_name, sender_role, body, is_read, created_at) "
            "VALUES (?, ?, ?, ?, ?, 0, ?)",
            (student["id"], user["user_id"], user["name"], user["role"], body, now),
        )
        conn.commit()
        msg = conn.execute(
            "SELECT * FROM edtech_messages ORDER BY id DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()

    return jsonify(dict(msg)), 201


@edtech_bp.route("/messages/unread-count", methods=["GET"])
@require_auth
def unread_count():
    """Return count of unread messages sent by the other role."""
    user       = request.et_user
    other_role = "teacher" if user["role"] == "parent" else "parent"
    conn = _db()
    try:
        count = conn.execute(
            "SELECT COUNT(*) FROM edtech_messages WHERE sender_role=? AND is_read=0",
            (other_role,),
        ).fetchone()[0]
    finally:
        conn.close()

    return jsonify({"unread": count})


@edtech_bp.route("/messages/read-all", methods=["PATCH"])
@require_auth
def mark_all_read():
    """Mark all messages from the other role as read (called when user opens messages view)."""
    user       = request.et_user
    other_role = "teacher" if user["role"] == "parent" else "parent"
    conn = _db()
    try:
        conn.execute(
            "UPDATE edtech_messages SET is_read=1 WHERE sender_role=? AND is_read=0",
            (other_role,),
        )
        conn.commit()
    finally:
        conn.close()

    return jsonify({"success": True})


@edtech_bp.route("/messages/<int:msg_id>/read", methods=["PATCH"])
@require_auth
def mark_read(msg_id):
    """Mark a single message as read."""
    conn = _db()
    try:
        conn.execute("UPDATE edtech_messages SET is_read=1 WHERE id=?", (msg_id,))
        conn.commit()
    finally:
        conn.close()

    return jsonify({"success": True})
