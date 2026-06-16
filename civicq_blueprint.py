"""
CivicQ Landing Page -- Flask Blueprint
Mounts under /api/civicq on the mw-backend server.

Backs the email-capture proof-of-backend on the CivicQ landing page demo at
https://michaelwegter.com/demos/civicq/ (Organization Hub section). Proves the
posting's "Mailchimp/ConvertKit level integration with CSV backup" requirement
with real persistence, not a claim.

Endpoints:
  POST /api/civicq/subscribe    Validate + store an email signup (idempotent)
  GET  /api/civicq/csv-export   Stream all signups as a CSV download

Auth: none, this is a public landing-page lead-capture form.
CORS: handled globally by server.py -- no per-blueprint CORS config needed.
Storage: SQLite at data/mw.db (shared file, namespaced table), created lazily.
"""

import csv
import io
import re
import sqlite3
import datetime
from pathlib import Path

from flask import Blueprint, request, jsonify, Response

# ─── Blueprint ────────────────────────────────────────────────────────────────

civicq_bp = Blueprint("civicq", __name__, url_prefix="/api/civicq")

DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "mw.db"

EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


# ─── DB helpers ───────────────────────────────────────────────────────────────

def _db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _init_db():
    conn = _db()
    try:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS civicq_subscribers (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                email      TEXT UNIQUE NOT NULL,
                created_at TEXT NOT NULL
            );
        """)
        conn.commit()
    finally:
        conn.close()


_init_db()


# ─── Routes ───────────────────────────────────────────────────────────────────

@civicq_bp.route("/subscribe", methods=["POST"])
def subscribe():
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip().lower()

    if not email or not EMAIL_RE.match(email):
        return jsonify({"status": "invalid", "message": "Enter a valid email address."}), 400

    conn = _db()
    try:
        existing = conn.execute(
            "SELECT id FROM civicq_subscribers WHERE email = ?", (email,)
        ).fetchone()
        if existing:
            return jsonify({"status": "duplicate", "message": "You're already on the list."}), 200

        conn.execute(
            "INSERT INTO civicq_subscribers (email, created_at) VALUES (?, ?)",
            (email, datetime.datetime.utcnow().isoformat()),
        )
        conn.commit()
        return jsonify({"status": "success", "message": "Subscribed."}), 201
    finally:
        conn.close()


@civicq_bp.route("/csv-export", methods=["GET"])
def csv_export():
    conn = _db()
    try:
        rows = conn.execute(
            "SELECT email, created_at FROM civicq_subscribers ORDER BY created_at ASC"
        ).fetchall()
    finally:
        conn.close()

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["email", "created_at"])
    for row in rows:
        writer.writerow([row["email"], row["created_at"]])

    return Response(
        buf.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=civicq-signups.csv"},
    )
