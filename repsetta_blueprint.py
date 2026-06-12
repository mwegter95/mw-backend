"""
Repsetta Fitness — Flask Blueprint
Mounts under /repsetta on the mw-backend server.

Backs the Repsetta strength-training demo at
https://michaelwegter.com/demos/repsetta-fitness/ (a real React Native /
react-native-web app iframed by the /work-samples route).

Endpoints:
  GET  /repsetta/exercises       exercise catalog
  GET  /repsetta/program/today   the seeded "today" guided program
  GET  /repsetta/workouts        list past workouts for a demo user
  POST /repsetta/workouts        persist a completed workout session
  GET  /repsetta/progress        aggregated stats + volume trend

Storage: a single table this blueprint owns (repsetta_workouts) inside the
existing SQLite DB at data/mw.db, created lazily with CREATE TABLE IF NOT EXISTS.
It does NOT touch auth, the DB schema, or any existing table/blueprint.

CORS: handled globally by server.py's CORS(app, origins=_CORS_ORIGINS, ...),
which already allow-lists https://michaelwegter.com (the demo's origin). No
per-blueprint CORS config is needed.
"""

import json
import sqlite3
from pathlib import Path
from datetime import datetime

from flask import Blueprint, request, jsonify

# ─── Blueprint setup ──────────────────────────────────────────────────────────

repsetta_bp = Blueprint(
    "repsetta",
    __name__,
    url_prefix="/repsetta",
)

# Reuse the same data dir convention as the rest of the server.
DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "mw.db"

# ─── Static catalog + seed program (read-only) ───────────────────────────────

EXERCISES = [
    {"id": 1, "name": "Barbell Bench Press", "muscle": "Chest", "equipment": "Barbell"},
    {"id": 2, "name": "Incline Dumbbell Press", "muscle": "Chest", "equipment": "Dumbbell"},
    {"id": 3, "name": "Overhead Press", "muscle": "Shoulders", "equipment": "Barbell"},
    {"id": 4, "name": "Lateral Raise", "muscle": "Shoulders", "equipment": "Dumbbell"},
    {"id": 5, "name": "Tricep Pushdown", "muscle": "Triceps", "equipment": "Cable"},
    {"id": 6, "name": "Skull Crusher", "muscle": "Triceps", "equipment": "EZ Bar"},
]

TODAY_PROGRAM = {
    "name": "Push Day A",
    "focus": "Chest / Shoulders / Triceps",
    "exercises": [
        {"exerciseId": 1, "targetSets": 4, "targetReps": 8, "targetWeight": 135},
        {"exerciseId": 3, "targetSets": 3, "targetReps": 10, "targetWeight": 95},
        {"exerciseId": 5, "targetSets": 3, "targetReps": 12, "targetWeight": 50},
    ],
}

SEED_WORKOUTS = [
    {"date": "2026-06-10", "name": "Push Day A",
     "sets": [{"exerciseId": 1, "reps": 8, "weight": 135}, {"exerciseId": 1, "reps": 8, "weight": 135},
              {"exerciseId": 1, "reps": 7, "weight": 135}, {"exerciseId": 3, "reps": 10, "weight": 95},
              {"exerciseId": 3, "reps": 9, "weight": 95}, {"exerciseId": 5, "reps": 12, "weight": 50}]},
    {"date": "2026-06-08", "name": "Push Day A",
     "sets": [{"exerciseId": 1, "reps": 8, "weight": 130}, {"exerciseId": 1, "reps": 7, "weight": 130},
              {"exerciseId": 3, "reps": 10, "weight": 90}, {"exerciseId": 5, "reps": 12, "weight": 45}]},
    {"date": "2026-06-05", "name": "Push Day A",
     "sets": [{"exerciseId": 1, "reps": 8, "weight": 125}, {"exerciseId": 3, "reps": 9, "weight": 85},
              {"exerciseId": 5, "reps": 10, "weight": 45}]},
]

# ─── DB helpers ───────────────────────────────────────────────────────────────

def _db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_table():
    conn = _db()
    try:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS repsetta_workouts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user TEXT NOT NULL,
                name TEXT,
                date TEXT,
                sets_json TEXT NOT NULL,
                created_at TEXT
            )"""
        )
        cur = conn.execute("SELECT COUNT(*) AS c FROM repsetta_workouts WHERE user='demo'")
        if cur.fetchone()["c"] == 0:
            for w in SEED_WORKOUTS:
                conn.execute(
                    "INSERT INTO repsetta_workouts (user, name, date, sets_json, created_at) "
                    "VALUES (?,?,?,?,?)",
                    ("demo", w["name"], w["date"], json.dumps(w["sets"]), w["date"]),
                )
        conn.commit()
    finally:
        conn.close()


def _volume(sets):
    return sum(int(s["reps"]) * float(s["weight"]) for s in sets)


# ─── Routes ───────────────────────────────────────────────────────────────────

@repsetta_bp.route("/exercises")
def exercises():
    return jsonify({"exercises": EXERCISES})


@repsetta_bp.route("/program/today")
def program_today():
    return jsonify(TODAY_PROGRAM)


@repsetta_bp.route("/workouts", methods=["GET"])
def list_workouts():
    _ensure_table()
    user = request.args.get("user", "demo")
    conn = _db()
    try:
        rows = conn.execute(
            "SELECT * FROM repsetta_workouts WHERE user=? ORDER BY date DESC, id DESC",
            (user,),
        ).fetchall()
    finally:
        conn.close()
    out = [{"id": str(r["id"]), "name": r["name"], "date": r["date"],
            "sets": json.loads(r["sets_json"])} for r in rows]
    return jsonify({"workouts": out})


@repsetta_bp.route("/workouts", methods=["POST"])
def create_workout():
    _ensure_table()
    data = request.get_json(silent=True) or {}
    user = data.get("user", "demo")
    sets = data.get("sets", [])
    name = data.get("name", "Workout")
    date = data.get("date") or datetime.now().strftime("%Y-%m-%d")
    if not isinstance(sets, list) or not sets:
        return jsonify({"success": False, "error": "sets must be a non-empty list"}), 400
    conn = _db()
    try:
        cur = conn.execute(
            "INSERT INTO repsetta_workouts (user, name, date, sets_json, created_at) "
            "VALUES (?,?,?,?,?)",
            (user, name, date, json.dumps(sets), datetime.now().isoformat()),
        )
        conn.commit()
        wid = cur.lastrowid
    finally:
        conn.close()
    return jsonify({"workout": {"id": str(wid), "name": name, "date": date, "sets": sets}})


@repsetta_bp.route("/progress")
def progress():
    _ensure_table()
    user = request.args.get("user", "demo")
    conn = _db()
    try:
        rows = conn.execute(
            "SELECT * FROM repsetta_workouts WHERE user=? ORDER BY date ASC, id ASC",
            (user,),
        ).fetchall()
    finally:
        conn.close()
    workouts = [{"date": r["date"], "sets": json.loads(r["sets_json"])} for r in rows]
    trend = [{"date": w["date"], "volume": int(_volume(w["sets"]))} for w in workouts]
    return jsonify({
        "totalWorkouts": len(workouts),
        "totalVolume": int(sum(t["volume"] for t in trend)),
        "currentStreak": len(workouts),
        "trend": trend[-7:],
    })
