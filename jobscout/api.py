"""Flask blueprint `jobscout_bp` mounted at /jobs (contract §5, §9.6).

User endpoints: JWT + JOBS_ALLOWED_EMAILS. Worker endpoints: X-Worker-Token. /jobs/health: open.
CORS is handled app-wide by server.py.
"""
import json
import queue

from flask import Blueprint, Response, g, jsonify, request

from . import config, db, discovery, find, pipeline, runs, sweep, taxonomy, tasks, views
from .auth import require_user, require_worker
from .worker import record_heartbeat

jobscout_bp = Blueprint("jobscout", __name__, url_prefix="/jobs")

HEARTBEAT_SECONDS = 15


def _err(code, status, **extra):
    return jsonify({"error": code, **extra}), status


def _body():
    body = request.get_json(silent=True)
    return body if isinstance(body, dict) else {}


def _profile(conn):
    return views.get_profile(conn, g.jobs_user["id"])


# ── open ─────────────────────────────────────────────────────────────────────

@jobscout_bp.get("/health")
def health():
    return jsonify({"ok": True, "role": config.role(), "instance": config.instance(), "commit": config.commit_sha(),
                    "protocol": config.PROTOCOL})


# ── user API ────────────────────────────────────────────────────────────────

@jobscout_bp.get("/api/me")
@require_user
def me():
    with db.session() as conn:
        has_profile = views.touch_visit(conn, g.jobs_user["id"])
    return jsonify({"user": g.jobs_user, "has_profile": has_profile})


@jobscout_bp.get("/api/meta")
@require_user
def meta():
    return jsonify(taxonomy.meta(places_enabled=bool(config.google_places_api_key())))


@jobscout_bp.get("/api/jobs")
@require_user
def jobs_list():
    with db.session() as conn:
        return jsonify(views.list_jobs(conn, g.jobs_user, _profile(conn), request.args))


@jobscout_bp.get("/api/jobs/<int:job_id>")
@require_user
def job_get(job_id):
    with db.session() as conn:
        item = views.job_detail(conn, g.jobs_user, _profile(conn), job_id)
    return jsonify(item) if item else _err("not_found", 404)


@jobscout_bp.put("/api/jobs/<int:job_id>/state")
@require_user
def job_state(job_id):
    with db.session() as conn:
        if not conn.execute("SELECT 1 FROM jobs WHERE id=?", (job_id,)).fetchone():
            return _err("not_found", 404)
        try:
            state = views.set_job_state(conn, g.jobs_user, job_id, _body())
        except ValueError as exc:
            return _err(str(exc), 400)
    return jsonify({"ok": True, "state": state})


@jobscout_bp.get("/api/companies")
@require_user
def companies_list():
    with db.session() as conn:
        return jsonify(views.list_companies(conn, g.jobs_user, _profile(conn), request.args))


@jobscout_bp.get("/api/companies/<int:company_id>")
@require_user
def company_get(company_id):
    with db.session() as conn:
        item = views.company_detail(conn, g.jobs_user, _profile(conn), company_id)
    return jsonify(item) if item else _err("not_found", 404)


def _start_company_run(company_id):
    try:
        return runs.start("company", pipeline.company_run, user_id=g.jobs_user["id"], key=f"company:{company_id}",
                          company_id=company_id).id
    except runs.AlreadyRunning as exc:
        return exc.run_id


@jobscout_bp.post("/api/companies")
@require_user
def company_add():
    body = _body()
    domain = discovery.normalize_domain(body.get("url"))
    if not domain or discovery.filter_reason(domain) in ("invalid",):
        return _err("invalid_url", 400)
    now = db.now_iso()
    with db.session() as conn:
        row = conn.execute("SELECT id FROM companies WHERE domain=?", (domain,)).fetchone()
        created = row is None
        if created:
            url = body["url"].strip()
            cur = conn.execute(
                "INSERT INTO companies(name, domain, homepage_url, source, source_detail, status, enrich_status, "
                "employee_band, discovered_via, discovered_at, created_at, updated_at) "
                "VALUES(?,?,?,'manual','added by URL','pending','pending','unknown',?,?,?,?)",
                ((body.get("name") or domain.split(".")[0].replace("-", " ").title())[:120], domain,
                 url if url.startswith("http") else f"https://{domain}", "manual: added by URL", now, now, now))
            company_id = cur.lastrowid
        else:
            company_id = row["id"]
    run_id = _start_company_run(company_id)
    with db.session() as conn:
        item = views.company_detail(conn, g.jobs_user, _profile(conn), company_id)
    item.pop("jobs", None)
    item.pop("facts", None)
    return jsonify({"company": item, "run_id": run_id}), 201 if created else 200


@jobscout_bp.patch("/api/companies/<int:company_id>")
@require_user
def company_patch(company_id):
    body = _body()
    if "status" in body and body["status"] not in ("ignored", "active"):
        return _err("invalid_status", 400)
    with db.session() as conn:
        if not conn.execute("SELECT 1 FROM companies WHERE id=?", (company_id,)).fetchone():
            return _err("not_found", 404)
        views.patch_company(conn, g.jobs_user, company_id, body)
        item = views.company_detail(conn, g.jobs_user, _profile(conn), company_id)
    item.pop("jobs", None)
    item.pop("facts", None)
    return jsonify({"company": item})


@jobscout_bp.get("/api/profile")
@require_user
def profile_get():
    with db.session() as conn:
        return jsonify({"profile": views.profile_json(_profile(conn))})


@jobscout_bp.put("/api/profile")
@require_user
def profile_put():
    with db.session() as conn:
        saved = views.save_profile(conn, g.jobs_user, _body())
    return jsonify({"profile": views.profile_json(saved)})


@jobscout_bp.get("/api/discovery/plan")
@require_user
def discovery_plan():
    args = request.args
    options = {"industries": views._csv(args, "industries"), "keywords": views._csv(args, "keywords"),
               "sources": views._csv(args, "sources"), "max_queries": views._num(args, "max_queries", int)}
    with db.session() as conn:
        plan = discovery.plan_for(_profile(conn), options)
    return jsonify(plan)


# ── runs ────────────────────────────────────────────────────────────────────

def _ids(value):
    if isinstance(value, list):
        return [int(v) for v in value if str(v).isdigit()] or None
    return None


_HEAVY_RUNS = ("find", "sweep", "discover", "pipeline")


@jobscout_bp.post("/api/runs")
@require_user
def run_create():
    body = _body()
    kind, opts = body.get("kind"), body.get("options") if isinstance(body.get("options"), dict) else {}
    user_id = g.jobs_user["id"]
    company_ids = _ids(opts.get("company_ids"))
    if company_ids is None and kind != "company" and str(body.get("company_id", "")).isdigit():
        company_ids = [int(body["company_id"])]  # e.g. {"kind": "sweep", "company_id": 44}
    limit = opts.get("limit") if isinstance(opts.get("limit"), int) else None
    busy = {k: v for k, v in runs.active_ids().items() if k in _HEAVY_RUNS}
    if kind in _HEAVY_RUNS and busy and kind not in busy:
        # find / sweep / discover / pipeline all read the same companies; one at a time.
        other, run_id = next(iter(busy.items()))
        return _err("already_running", 409, run_id=run_id, kind=other)
    try:
        if kind == "find":
            with db.session() as conn:
                profile = _profile(conn)
            options = {k: opts.get(k) for k in ("industries", "keywords", "sources", "max_queries", "skip_discovery")}
            run = runs.start("find", find.run_find, user_id, options=options, profile=profile)
        elif kind == "sweep":
            run = runs.start("sweep", sweep.run_sweep, user_id, company_ids=company_ids, limit=limit)
        elif kind == "discover":
            with db.session() as conn:
                profile = _profile(conn)
            options = {k: opts.get(k) for k in ("industries", "keywords", "sources", "max_queries")}
            run = runs.start("discover", discovery.run_discovery, user_id, options=options, profile=profile)
        elif kind == "pipeline":
            run = runs.start("pipeline", pipeline.run_pipeline, user_id, company_ids=company_ids, limit=limit)
        elif kind == "enrich":
            run = runs.start("enrich", pipeline.run_enrich, user_id, company_ids=company_ids, limit=limit,
                             only_pending=not opts.get("all"))
        elif kind == "detect":
            run = runs.start("detect", pipeline.run_detect, user_id, company_ids=company_ids, limit=limit)
        elif kind == "company":
            if not str(body.get("company_id", "")).isdigit():
                return _err("company_id_required", 400)
            cid = int(body["company_id"])
            run = runs.start("company", pipeline.company_run, user_id, key=f"company:{cid}", company_id=cid)
        else:
            return _err("invalid_kind", 400)
    except runs.AlreadyRunning as exc:
        return _err("already_running", 409, run_id=exc.run_id)
    return jsonify({"run_id": run.id})


@jobscout_bp.get("/api/runs")
@require_user
def run_list():
    limit = max(1, min(views._num(request.args, "limit", int, 10), 100))
    with db.session() as conn:
        rows = conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return jsonify({"items": [runs.run_dict(r) for r in rows]})


@jobscout_bp.get("/api/runs/<int:run_id>")
@require_user
def run_get(run_id):
    with db.session() as conn:
        row = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
    return jsonify({"run": runs.run_dict(row)}) if row else _err("not_found", 404)


@jobscout_bp.post("/api/runs/<int:run_id>/cancel")
@require_user
def run_cancel(run_id):
    """Stop: the run ends now (status 'cancelled') and its slot is free for the next Find matches, even if its
    thread is stuck mid-request; the thread quits at its next checkpoint."""
    with db.session() as conn:
        if not conn.execute("SELECT 1 FROM runs WHERE id=?", (run_id,)).fetchone():
            return _err("not_found", 404)
    stopped = runs.cancel(run_id)
    with db.session() as conn:
        row = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
    if not stopped:
        return _err("not_running", 409, run=runs.run_dict(row))
    return jsonify({"run": runs.run_dict(row)})


def _sse(event):
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


@jobscout_bp.get("/api/runs/<int:run_id>/stream")
@require_user
def run_stream(run_id):
    live = runs.live(run_id)
    if live is None:
        with db.session() as conn:
            row = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if not row:
            return _err("not_found", 404)
        history = [{"type": "log", "line": ln} for ln in (row["log"] or "").splitlines()]
        history.append({"type": "done", "status": row["status"], "stats": db.loads(row["stats"], {})})

        def replay():
            for event in history:
                yield _sse(event)
        return Response(replay(), mimetype="text/event-stream",
                        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    subscription = live.subscribe()

    def generate():
        try:
            yield ": connected\n\n"
            while True:
                try:
                    event = subscription.get(timeout=HEARTBEAT_SECONDS)
                except queue.Empty:
                    yield ": ping\n\n"
                    continue
                yield _sse(event)
                if event.get("type") == "done":
                    break
        finally:
            live.unsubscribe(subscription)

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@jobscout_bp.get("/api/status")
@require_user
def status():
    with db.session() as conn:
        return jsonify(views.status(conn, g.jobs_user, _profile(conn)))


# ── worker API ──────────────────────────────────────────────────────────────

@jobscout_bp.post("/worker/heartbeat")
@require_worker
def worker_heartbeat():
    with db.session() as conn:
        record_heartbeat(conn, _body())
        queued = tasks.counts(conn)["queued"]
    return jsonify({"ok": True, "protocol": config.PROTOCOL, "queue": queued})


@jobscout_bp.post("/worker/claim")
@require_worker
def worker_claim():
    body = _body()
    if body.get("protocol") != config.PROTOCOL:
        return _err("protocol_mismatch", 409, protocol=config.PROTOCOL)
    kinds = body.get("kinds") if isinstance(body.get("kinds"), list) else None
    try:
        max_n = int(body.get("max") or 4)
    except (TypeError, ValueError):
        max_n = 4
    with db.session() as conn:
        claimed = tasks.claim(conn, str(body.get("worker_id") or "worker")[:80], kinds, max_n)
    return jsonify({"protocol": config.PROTOCOL, "tasks": claimed})


@jobscout_bp.post("/worker/complete")
@require_worker
def worker_complete():
    body = _body()
    try:
        task_id = int(body.get("task_id"))
    except (TypeError, ValueError):
        return _err("task_id_required", 400)
    with db.session() as conn:
        try:
            tasks.complete(conn, task_id, body.get("worker_id"), body.get("result"), body.get("model"))
        except tasks.TaskError as exc:
            return _err(exc.code, exc.status, **({"detail": exc.detail} if exc.detail else {}))
    return jsonify({"ok": True})


@jobscout_bp.post("/worker/fail")
@require_worker
def worker_fail():
    body = _body()
    try:
        task_id = int(body.get("task_id"))
    except (TypeError, ValueError):
        return _err("task_id_required", 400)
    with db.session() as conn:
        if tasks.fail(conn, task_id, body.get("worker_id"), str(body.get("error") or "")) is None:
            return _err("task_not_active", 409)
    return jsonify({"ok": True})
