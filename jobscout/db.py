"""SQLite storage for Job Scout (data/jobscout.db, contract §3).

`init()` is idempotent: it creates missing tables and adds missing columns with
ALTER TABLE, so the schema below can grow additively without a migration tool.
Every thread opens its own connection via `connect()`.
"""
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from . import config

# table -> (columns [(name, declaration)], table constraints)
SCHEMA = {
    "companies": ([
        ("id", "INTEGER PRIMARY KEY"), ("name", "TEXT NOT NULL"), ("domain", "TEXT UNIQUE NOT NULL"),
        ("homepage_url", "TEXT"), ("careers_url", "TEXT"),
        ("ats_type", "TEXT"), ("ats_host", "TEXT"), ("ats_key", "TEXT"), ("ats_site", "TEXT"),
        ("ats_detected_at", "TEXT"),
        ("hq_address", "TEXT"), ("hq_city", "TEXT"), ("hq_state", "TEXT"), ("lat", "REAL"), ("lng", "REAL"),
        ("geo_precision", "TEXT"),
        ("industry", "TEXT"), ("sub_industry", "TEXT"), ("products", "TEXT DEFAULT '[]'"), ("summary", "TEXT"),
        ("business_model", "TEXT"), ("ownership", "TEXT"), ("parent_company", "TEXT"),
        ("employee_band", "TEXT DEFAULT 'unknown'"), ("founded_year", "INTEGER"), ("well_known", "INTEGER"),
        ("gem_score", "INTEGER"), ("hidden_gem", "INTEGER DEFAULT 0"), ("tags", "TEXT DEFAULT '[]'"),
        ("logo_url", "TEXT"), ("facts", "TEXT DEFAULT '{}'"),
        ("enrich_source", "TEXT"), ("enrich_status", "TEXT DEFAULT 'pending'"), ("enriched_at", "TEXT"),
        ("source", "TEXT"), ("source_detail", "TEXT"),
        ("status", "TEXT DEFAULT 'pending'"), ("status_reason", "TEXT"),
        ("last_swept_at", "TEXT"), ("last_error", "TEXT"), ("consecutive_failures", "INTEGER DEFAULT 0"),
        ("created_at", "TEXT"), ("updated_at", "TEXT"),
        # §9 discovery-first additions
        ("entity_type", "TEXT"), ("local_presence", "TEXT"), ("discovered_via", "TEXT"), ("discovered_at", "TEXT"),
    ], []),
    "jobs": ([
        ("id", "INTEGER PRIMARY KEY"),
        ("company_id", "INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE"),
        ("ats_job_id", "TEXT NOT NULL"), ("source", "TEXT"),
        ("url", "TEXT"), ("apply_url", "TEXT"), ("title", "TEXT NOT NULL"), ("title_tier", "TEXT"),
        ("prefilter", "TEXT"), ("rule_score", "INTEGER"),
        ("location_text", "TEXT"), ("city", "TEXT"), ("state", "TEXT"), ("country", "TEXT"),
        ("lat", "REAL"), ("lng", "REAL"), ("geo_precision", "TEXT"),
        ("workplace", "TEXT DEFAULT 'unknown'"), ("employment_type", "TEXT"),
        ("salary_min", "REAL"), ("salary_max", "REAL"), ("salary_period", "TEXT"), ("salary_text", "TEXT"),
        ("description_html", "TEXT"), ("description_text", "TEXT"), ("detail_fetched_at", "TEXT"),
        ("posted_at", "TEXT"), ("first_seen_at", "TEXT"), ("last_seen_at", "TEXT"),
        ("missed_sweeps", "INTEGER DEFAULT 0"), ("closed_at", "TEXT"), ("content_hash", "TEXT"),
    ], ["UNIQUE(company_id, ats_job_id)"]),
    "profiles": ([
        ("id", "INTEGER PRIMARY KEY"), ("user_id", "INTEGER UNIQUE NOT NULL"), ("name", "TEXT"),
        ("resume_text", "TEXT DEFAULT ''"), ("want_text", "TEXT DEFAULT ''"), ("avoid_text", "TEXT DEFAULT ''"),
        ("target_titles", "TEXT DEFAULT '[]'"), ("industries_want", "TEXT DEFAULT '[]'"),
        ("industries_avoid", "TEXT DEFAULT '[]'"), ("salary_floor", "INTEGER"),
        ("workplace_pref", "TEXT DEFAULT '[\"onsite\",\"hybrid\",\"remote\"]'"),
        ("home_address", "TEXT"), ("home_lat", "REAL"), ("home_lng", "REAL"),
        ("radius_miles", "INTEGER DEFAULT 35"), ("radius_minutes", "INTEGER DEFAULT 45"),
        ("last_visit_at", "TEXT"), ("prev_visit_at", "TEXT"), ("input_hash", "TEXT"), ("updated_at", "TEXT"),
        # §9 discovery-first additions
        ("discover_industries", "TEXT DEFAULT '[]'"), ("discover_keywords", "TEXT DEFAULT '[]'"),
        ("discover_sources", "TEXT DEFAULT '[\"maps\",\"search\",\"osm\"]'"),
        # §12 what the person is looking for (interests.py)
        ("job_categories", "TEXT DEFAULT '[]'"), ("seniority", "TEXT DEFAULT '[]'"),
    ], []),
    "job_scores": ([
        ("job_id", "INTEGER"), ("profile_id", "INTEGER"), ("fit", "INTEGER"), ("fit_source", "TEXT"),
        ("role_family", "TEXT"), ("seniority", "TEXT"), ("workplace", "TEXT"), ("tags", "TEXT DEFAULT '[]'"),
        ("why", "TEXT"), ("dealbreakers", "TEXT DEFAULT '[]'"), ("model", "TEXT"), ("input_hash", "TEXT"),
        ("scored_at", "TEXT"),
    ], ["PRIMARY KEY(job_id, profile_id)"]),
    "job_user_state": ([
        ("job_id", "INTEGER"), ("user_id", "INTEGER"), ("status", "TEXT DEFAULT 'new'"),
        ("notes", "TEXT DEFAULT ''"), ("applied_at", "TEXT"), ("updated_at", "TEXT"),
    ], ["PRIMARY KEY(job_id, user_id)"]),
    "company_user_state": ([
        ("company_id", "INTEGER"), ("user_id", "INTEGER"), ("following", "INTEGER DEFAULT 0"),
        ("notes", "TEXT DEFAULT ''"), ("updated_at", "TEXT"),
    ], ["PRIMARY KEY(company_id, user_id)"]),
    "distances": ([
        ("profile_id", "INTEGER"), ("lat", "REAL"), ("lng", "REAL"), ("miles", "REAL"), ("minutes", "REAL"),
        ("computed_at", "TEXT"),
    ], ["PRIMARY KEY(profile_id, lat, lng)"]),
    "ai_tasks": ([
        ("id", "INTEGER PRIMARY KEY"), ("kind", "TEXT"), ("job_id", "INTEGER"), ("profile_id", "INTEGER"),
        ("company_id", "INTEGER"), ("payload_hash", "TEXT"), ("status", "TEXT DEFAULT 'queued'"),
        ("attempts", "INTEGER DEFAULT 0"), ("lease_until", "TEXT"), ("worker_id", "TEXT"), ("error", "TEXT"),
        ("created_at", "TEXT"), ("updated_at", "TEXT"),
    ], []),
    "workers": ([
        ("worker_id", "TEXT PRIMARY KEY"), ("instance", "TEXT"), ("role", "TEXT"), ("model", "TEXT"),
        ("lmstudio_ok", "INTEGER"), ("commit_sha", "TEXT"), ("protocol", "INTEGER"),
        ("last_heartbeat_at", "TEXT"), ("tasks_done_today", "INTEGER DEFAULT 0"), ("day", "TEXT"),
        ("last_error", "TEXT"),
    ], []),
    "runs": ([
        ("id", "INTEGER PRIMARY KEY"), ("kind", "TEXT"), ("status", "TEXT"), ("started_at", "TEXT"),
        ("finished_at", "TEXT"), ("stats", "TEXT DEFAULT '{}'"), ("log", "TEXT DEFAULT ''"),
        ("requested_by", "INTEGER"),
    ], []),
    "meta": ([("key", "TEXT PRIMARY KEY"), ("value", "TEXT")], []),
}

INDEXES = [
    "CREATE INDEX IF NOT EXISTS ix_jobs_company ON jobs(company_id)",
    "CREATE INDEX IF NOT EXISTS ix_jobs_open ON jobs(closed_at, prefilter)",
    "CREATE INDEX IF NOT EXISTS ix_companies_status ON companies(status)",
    "CREATE INDEX IF NOT EXISTS ix_tasks_status ON ai_tasks(status, kind)",
    # One active (queued/leased) task per (kind, job, profile, company).
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_tasks_active ON ai_tasks("
    "kind, IFNULL(job_id, 0), IFNULL(profile_id, 0), IFNULL(company_id, 0)) "
    "WHERE status IN ('queued', 'leased')",
    "CREATE INDEX IF NOT EXISTS ix_runs_kind ON runs(kind, status)",
]


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(config.db_path()), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


@contextmanager
def session():
    """`with db.session() as conn:` — commits on success, rolls back on error, always closes."""
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init():
    """Create tables/indexes and add any missing columns. Safe to call repeatedly."""
    with session() as conn:
        for table, (columns, constraints) in SCHEMA.items():
            body = ", ".join([f"{n} {d}" for n, d in columns] + constraints)
            conn.execute(f"CREATE TABLE IF NOT EXISTS {table} ({body})")
            existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
            for name, decl in columns:
                if name not in existing:
                    # ADD COLUMN cannot carry PRIMARY KEY/UNIQUE; strip them for late additions.
                    decl = decl.replace("PRIMARY KEY", "").replace("UNIQUE", "")
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
        for stmt in INDEXES:
            conn.execute(stmt)


# ── small helpers ────────────────────────────────────────────────────────────

def now_iso(dt=None) -> str:
    dt = dt or datetime.now(timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def iso_in(**delta) -> str:
    return now_iso(datetime.now(timezone.utc) + timedelta(**delta))


def parse_iso(value):
    """Parse our ISO strings (and plain dates) into aware UTC datetimes; None on failure."""
    if not value:
        return None
    text = str(value).strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        try:
            dt = datetime.strptime(text[:10], "%Y-%m-%d")
        except ValueError:
            return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def loads(value, default):
    if value in (None, ""):
        return default
    try:
        out = json.loads(value)
    except (TypeError, ValueError):
        return default
    return out if isinstance(out, type(default)) else default


def dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def row_dict(row):
    return dict(row) if row is not None else None


def get_meta(conn, key, default=None):
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_meta(conn, key, value):
    conn.execute("INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                 (key, str(value)))


def update(conn, table, key_col, key, fields: dict):
    """UPDATE table SET <fields> WHERE key_col=key (no-op for empty fields)."""
    if not fields:
        return
    cols = ", ".join(f"{c}=?" for c in fields)
    conn.execute(f"UPDATE {table} SET {cols} WHERE {key_col}=?", (*fields.values(), key))
