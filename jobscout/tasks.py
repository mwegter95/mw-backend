"""AI task queue (ai_tasks table).

Workers claim tasks with a 10-minute lease; payloads are built at claim time from current data.
A failed attempt (explicit fail, invalid result or an expired lease) requeues the task until
it has been tried 3 times, then marks it failed.
"""
import logging
from datetime import datetime, timezone

from . import ai_schemas, db

log = logging.getLogger("jobscout")

LEASE_MINUTES = 10
MAX_ATTEMPTS = 3
KIND_PRIORITY = ("score_job", "enrich_company", "parse_page")


class TaskError(Exception):
    """Raised by complete(); `code` is the API error code, `status` the HTTP status."""

    def __init__(self, code, status=400, detail=None):
        super().__init__(detail or code)
        self.code, self.status, self.detail = code, status, detail


def enqueue(conn, kind, job_id=None, profile_id=None, company_id=None):
    """Queue a task unless an identical one is already queued/leased. Returns the new id or None."""
    now = db.now_iso()
    cur = conn.execute(
        "INSERT OR IGNORE INTO ai_tasks(kind, job_id, profile_id, company_id, status, attempts, created_at, updated_at) "
        "VALUES(?, ?, ?, ?, 'queued', 0, ?, ?)", (kind, job_id, profile_id, company_id, now, now))
    return cur.lastrowid if cur.rowcount else None


def counts(conn):
    out = {"queued": 0, "leased": 0, "failed": 0}
    for r in conn.execute("SELECT status, COUNT(*) n FROM ai_tasks WHERE status IN ('queued','leased','failed') "
                          "GROUP BY status"):
        out[r["status"]] = r["n"]
    return out


def claim(conn, worker_id, kinds=None, max_n=4):
    """Atomically lease up to max_n queued tasks (score_job > enrich_company > parse_page, then oldest)
    and return [{id, kind, lease_until, payload}]. Tasks whose payload can't be built are cancelled."""
    kinds = [k for k in (kinds or KIND_PRIORITY) if k in KIND_PRIORITY]
    if not kinds:
        return []
    if conn.in_transaction:
        conn.commit()
    lease_until = db.iso_in(minutes=LEASE_MINUTES)
    order = " ".join(f"WHEN '{k}' THEN {i}" for i, k in enumerate(KIND_PRIORITY))
    conn.execute("BEGIN IMMEDIATE")
    try:
        rows = conn.execute(
            f"SELECT * FROM ai_tasks WHERE status='queued' AND kind IN ({','.join('?' * len(kinds))}) "
            f"ORDER BY CASE kind {order} END, id LIMIT ?", (*kinds, max(1, min(int(max_n), 16)))).fetchall()
        now = db.now_iso()
        for r in rows:
            conn.execute("UPDATE ai_tasks SET status='leased', lease_until=?, worker_id=?, updated_at=? WHERE id=?",
                         (lease_until, worker_id, now, r["id"]))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    out = []
    for r in rows:
        task = dict(r)
        try:
            payload, payload_hash = build_payload(conn, task)
        except Exception as exc:  # noqa: BLE001 — a broken payload must not poison the batch
            log.warning("tasks: payload for task %s failed: %s", task["id"], exc)
            payload = None
        if payload is None:
            db.update(conn, "ai_tasks", "id", task["id"], {"status": "cancelled", "updated_at": db.now_iso(),
                                                           "error": "payload unavailable"})
            continue
        db.update(conn, "ai_tasks", "id", task["id"], {"payload_hash": payload_hash})
        out.append({"id": task["id"], "kind": task["kind"], "lease_until": lease_until, "payload": payload})
    conn.commit()
    return out


def build_payload(conn, task):
    """(payload, payload_hash) for a task, from current rows; (None, None) when the subject is gone."""
    from . import enrich, scoring
    kind = task["kind"]
    if kind == "score_job":
        return scoring.build_score_payload(conn, task["job_id"], task["profile_id"])
    company = db.row_dict(conn.execute("SELECT * FROM companies WHERE id=?", (task["company_id"],)).fetchone())
    if not company:
        return None, None
    if kind == "enrich_company":
        return enrich.build_payload(company), None
    if kind == "parse_page":
        facts = db.loads(company.get("facts"), {})
        if not facts.get("_careers_text"):
            return None, None
        return {"company_name": company["name"], "page_url": facts.get("_careers_url") or company.get("careers_url"),
                "page_text": facts["_careers_text"][:12000]}, facts.get("_careers_hash")
    return None, None


def complete(conn, task_id, worker_id, result, model=None):
    """Validate + apply a result. Raises TaskError for unknown/finished tasks or invalid results."""
    from . import enrich, scoring, sweep
    task = db.row_dict(conn.execute("SELECT * FROM ai_tasks WHERE id=?", (task_id,)).fetchone())
    if not task:
        raise TaskError("task_not_found", 404)
    if task["status"] not in ("leased", "queued"):
        raise TaskError("task_not_active", 409)
    try:
        cleaned = ai_schemas.validate(task["kind"], result)
    except ValueError as exc:
        fail(conn, task_id, worker_id, f"invalid result: {exc}")
        raise TaskError("invalid_result", 400, str(exc)) from exc
    if task["kind"] == "score_job":
        scoring.apply_score(conn, task, cleaned, model)
    elif task["kind"] == "enrich_company":
        enrich.apply_enrichment(conn, task["company_id"], cleaned)
    elif task["kind"] == "parse_page":
        sweep.apply_parsed_jobs(conn, task["company_id"], cleaned)
    now = db.now_iso()
    db.update(conn, "ai_tasks", "id", task_id, {"status": "done", "error": None, "updated_at": now,
                                                "worker_id": worker_id or task["worker_id"]})
    _count_done(conn, worker_id or task["worker_id"])
    conn.commit()


def _count_done(conn, worker_id):
    if not worker_id:
        return
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    conn.execute("UPDATE workers SET tasks_done_today = CASE WHEN day=? THEN tasks_done_today + 1 ELSE 1 END, day=? "
                 "WHERE worker_id=?", (today, today, worker_id))


def fail(conn, task_id, worker_id=None, error=None):
    """Record a failed attempt; requeue until MAX_ATTEMPTS. Returns the new status or None."""
    task = db.row_dict(conn.execute("SELECT * FROM ai_tasks WHERE id=?", (task_id,)).fetchone())
    if not task or task["status"] not in ("leased", "queued"):
        return None
    attempts = (task["attempts"] or 0) + 1
    status = "failed" if attempts >= MAX_ATTEMPTS else "queued"
    db.update(conn, "ai_tasks", "id", task_id, {"attempts": attempts, "status": status, "lease_until": None,
                                                "error": (error or "")[:500], "updated_at": db.now_iso()})
    if status == "failed" and task["kind"] == "enrich_company":
        db.update(conn, "companies", "id", task["company_id"], {"enrich_status": "failed"})
    conn.commit()
    return status


def reap(conn):
    """Expired leases count as a failed attempt and go back to the queue (or fail after 3)."""
    now = db.now_iso()
    expired = conn.execute("SELECT id FROM ai_tasks WHERE status='leased' AND lease_until < ?", (now,)).fetchall()
    for r in expired:
        fail(conn, r["id"], error="lease expired")
    return len(expired)
