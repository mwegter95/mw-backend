"""Per-company pipeline (contract §9.4):

  1. collect facts from the company's own site and locate the HQ,
  2. heuristic categorisation (industry, entity type, local presence…) right away,
  3. queue the enrich_company AI task,
  4. find the careers page, detect the ATS and sweep the company's jobs.

Run kinds: pipeline (1–4 for pending companies), enrich (1–3), detect (careers/ATS only),
company (1–4 for one company).
"""
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

from . import db, enrich, runs, scoring, sweep, taxonomy, tasks

log = logging.getLogger("jobscout")

MAX_WORKERS = 4


def _company(conn, company_id):
    return db.row_dict(conn.execute("SELECT * FROM companies WHERE id=?", (company_id,)).fetchone())


def _say(run, line):
    (run.log if run else log.info)(line)


def prepare_company(conn, company, fetcher=None, run=None):
    """Steps 1–3. Updates the row; returns the fetched homepage (html, url) or None."""
    old_facts = db.loads(company.get("facts"), {})
    facts, homepage = enrich.collect_facts(company, fetcher)
    kept = {k: v for k, v in old_facts.items() if k.startswith("_") or k.startswith("discover")}
    facts = {**kept, **facts}
    fields = {"facts": db.dumps(facts), "updated_at": db.now_iso()}
    if homepage is None:
        fields.update(enrich_status="failed", last_error=(facts.get("fetch_error") or "homepage unavailable")[:500])
        _say(run, f"{company['name']}: homepage unavailable ({facts.get('fetch_error', '')[:120]})")
    else:
        fields["homepage_url"] = homepage[1]  # the URL that actually answered
        fields.update(enrich.categorize(company, facts, fetcher))
        if tasks.enqueue(conn, "enrich_company", company_id=company["id"]) or company.get("enrich_status") == "queued":
            fields["enrich_status"] = "queued"
    company.update(fields)
    fields.update(enrich.gem_fields(conn, company))
    fields.update(enrich.auto_ignore(company))
    company.update(fields)
    db.update(conn, "companies", "id", company["id"], fields)
    conn.commit()
    if homepage is not None:
        _say(run, f"{company['name']}: {taxonomy.industry_label(company.get('industry')) or 'uncategorized'} · "
                  f"{company.get('entity_type')} · {company.get('local_presence')} (heuristic)"
                  f"{' → ignored: ' + company['status_reason'] if company.get('status') == 'ignored' else ''}")
    return homepage


def run_company(company_id, run=None, fetcher=None, sweep_jobs=True):
    """Steps 1–4 for one company → stats."""
    with db.session() as conn:
        company = _company(conn, company_id)
        if not company:
            return {"skipped": 1}
        homepage = None
        if company.get("enrich_status") in (None, "pending") or not db.loads(company.get("facts"), {}).get("fetched_at"):
            homepage = prepare_company(conn, company, fetcher, run)
        if company["status"] == "ignored":
            return {"companies": 1, "ignored": 1}
        if not sweep_jobs:
            return {"companies": 1}
        if company["status"] == "pending":
            sweep.detect_company(conn, company, fetcher, run, homepage=homepage)
    stats = sweep.sweep_company(company_id, run, fetcher)
    with db.session() as conn:
        scoring.enqueue_scores(conn)
    return stats


def _parallel(run, ids, fn, phase):
    stats, total = {}, len(ids)
    run.progress(0, total, phase)
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = [pool.submit(fn, cid) for cid in ids]
        try:
            for done, fut in enumerate(as_completed(futures), 1):
                try:
                    result = fut.result() or {}
                except Exception as exc:  # noqa: BLE001
                    run.log(f"error: {type(exc).__name__}: {exc}")
                    result = {"errors": 1}
                for key, value in result.items():
                    if isinstance(value, (int, float)):
                        stats[key] = stats.get(key, 0) + value
                run.progress(done, total, phase)
        except BaseException:  # Stop (runs.Cancelled) or a crash: don't start the companies still queued
            runs.cancel_pending(futures)
            raise
    return stats


def pending_ids(conn, limit=None):
    q = ("SELECT id FROM companies WHERE status != 'ignored' AND (status='pending' OR enrich_status='pending') "
         "ORDER BY id")
    return [r["id"] for r in conn.execute(q + (f" LIMIT {int(limit)}" if limit else ""))]


def run_pipeline(run, company_ids=None, limit=None, fetcher=None):
    with db.session() as conn:
        ids = list(company_ids or pending_ids(conn, limit))
    run.log(f"pipeline for {len(ids)} companies")
    stats = _parallel(run, ids, lambda cid: run_company(cid, run, fetcher), "pipeline")
    run.log(f"pipeline done: {stats.get('jobs_new', 0)} new jobs, {stats.get('ignored', 0)} ignored, "
            f"{stats.get('errors', 0)} errors")
    return stats


def run_enrich(run, company_ids=None, limit=None, only_pending=True, fetcher=None):
    """Re-collect facts + heuristics + queue AI for pending (or all given) companies."""
    with db.session() as conn:
        if company_ids:
            ids = list(company_ids)
        else:
            where = "enrich_status IN ('pending','failed')" if only_pending else "1=1"
            ids = [r["id"] for r in conn.execute(f"SELECT id FROM companies WHERE status != 'ignored' AND {where} "
                                                 f"ORDER BY id" + (f" LIMIT {int(limit)}" if limit else ""))]
    run.log(f"enriching {len(ids)} companies")

    def one(cid):
        with db.session() as conn:
            company = _company(conn, cid)
            if company:
                prepare_company(conn, company, fetcher, run)
        return {"companies": 1}
    return _parallel(run, ids, one, "enrich")


def run_detect(run, company_ids=None, limit=None, fetcher=None):
    with db.session() as conn:
        ids = list(company_ids or [r["id"] for r in conn.execute(
            "SELECT id FROM companies WHERE status NOT IN ('ignored') AND (status='pending' OR ats_type IS NULL) "
            "ORDER BY id" + (f" LIMIT {int(limit)}" if limit else ""))])
    run.log(f"detecting careers/ATS for {len(ids)} companies")

    def one(cid):
        with db.session() as conn:
            company = _company(conn, cid)
            if company:
                sweep.detect_company(conn, company, fetcher, run)
                return {"companies": 1, company["status"]: 1}
        return {}
    return _parallel(run, ids, one, "detect")


def company_run(run, company_id, fetcher=None):
    run.log(f"company {company_id}: facts → categorize → careers/ATS → sweep")
    stats = run_company(company_id, run, fetcher)
    return stats
