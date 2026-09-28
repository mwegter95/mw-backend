"""Job sweep: for each active/pending company, detect the ATS when needed, list jobs, keep the local
ones, fetch details only for new/changed pass/maybe titles, normalise, upsert, close jobs missing
from two sweeps in a row, then queue AI scoring. Up to 4 companies run concurrently; the shared
PoliteFetcher enforces per-host spacing.
"""
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from urllib.parse import urljoin

from . import ats, browser, careers, config, db, geo, scoring, tasks
from .ats.base import RawJob
from .http import Blocked, FetchError
from .normalize import (Location, content_hash, detect_workplace, html_to_text, is_local, make_salary,
                        parse_location, parse_salary, prefilter, rule_score, sanitize_html, title_tier)

log = logging.getLogger("jobscout")

MAX_WORKERS = 4
DETAIL_STALE_DAYS = 14
REDETECT_DAYS = 30
MAX_FAILURES = 3
_KEEP_WHEN_MISSING = ("salary_min", "salary_max", "salary_period", "salary_text", "description_html",
                      "description_text", "lat", "lng", "geo_precision", "employment_type", "apply_url",
                      "detail_fetched_at")


def _age_days(iso):
    dt = db.parse_iso(iso)
    return (datetime.now(timezone.utc) - dt).days if dt else 10 ** 6


def needs_detection(company) -> bool:
    """Pending companies, and everyone else once their detection is older than REDETECT_DAYS."""
    return company.get("status") == "pending" or _age_days(company.get("ats_detected_at")) > REDETECT_DAYS


# ── normalisation of one RawJob ──────────────────────────────────────────────

def _location(raw):
    if raw.state or raw.city:
        loc = Location(city=raw.city, state=raw.state, country=raw.country)
    else:
        loc = parse_location(raw.location_text)
    if raw.country and not loc.country:
        loc.country = raw.country.upper() if len(raw.country) == 2 else loc.country
    return loc


def commute_area(conn):
    """(home points, miles): on-site/hybrid jobs farther than `miles` from every profile home are dropped.
    Big parent-company boards (a local plant's owner posting nationwide) list jobs across MN/WI that are
    far outside any commute; the margin keeps edge-of-radius jobs and the map's context."""
    from .enrich import home_points
    row = conn.execute("SELECT MAX(radius_miles) FROM profiles").fetchone()
    return home_points(conn), max(60, (row[0] or 35) + 25)


def normalize_job(raw: RawJob, company: dict, area=None):
    """RawJob → job column dict, or None when the job is outside MN/WI (and not US-remote), or — given
    `area` from commute_area() — an on-site/hybrid job beyond commuting distance of every home."""
    loc = _location(raw)
    if not is_local(loc):
        return None
    city_hit = geo.find_city(loc.city, loc.state) if loc.city else None
    if loc.city and not loc.state and not city_hit and not loc.remote:
        return None  # a bare city we can't place in the region ("London", "Tel Aviv")
    if city_hit and not loc.state:
        loc.state, loc.country = city_hit[2], loc.country or "US"
    description_html = sanitize_html(raw.description_html) if raw.description_html else None
    description_text = html_to_text(description_html) if description_html else None
    schedule = raw.extra.get("schedule_text") or ""
    salary = None
    if raw.salary_min or raw.salary_max:
        salary = make_salary(raw.salary_min, raw.salary_max, raw.salary_period, raw.salary_text)
    salary = salary or parse_salary(raw.salary_text) or parse_salary(description_text)
    workplace = detect_workplace(raw.workplace, raw.title, raw.location_text, f"{schedule}\n{description_text or ''}")
    if loc.remote and workplace == "unknown":
        workplace = "remote"
    lat, lng, precision = raw.extra.get("lat"), raw.extra.get("lng"), "city"
    if lat is None and city_hit:
        same_city = (company.get("hq_city") or "").lower() == (loc.city or "").lower()
        if same_city and company.get("geo_precision") == "address":
            lat, lng, precision = company["lat"], company["lng"], "address"
        else:
            lat, lng = city_hit[0], city_hit[1]
    if lat is None and company.get("lat") is not None:
        lat, lng, precision = company["lat"], company["lng"], company.get("geo_precision") or "city"
    if area and lat is not None and workplace != "remote" and not loc.remote:
        homes, miles = area
        if all(geo.haversine_miles(lat, lng, h[0], h[1]) > miles for h in homes):
            return None
    tier = title_tier(raw.title)
    pf = prefilter(raw.title, tier)
    fields = {
        "title": raw.title, "title_tier": tier, "prefilter": pf, "url": raw.url, "apply_url": raw.apply_url,
        "location_text": raw.location_text, "city": loc.city, "state": loc.state, "country": loc.country,
        "lat": lat, "lng": lng, "geo_precision": precision if lat is not None else "none",
        "workplace": workplace, "employment_type": raw.employment_type, "posted_at": raw.posted_at,
        "salary_min": salary.min if salary else None, "salary_max": salary.max if salary else None,
        "salary_period": salary.period if salary else None, "salary_text": salary.text if salary else None,
        "description_html": description_html, "description_text": description_text,
        "content_hash": content_hash(raw.title, raw.location_text, description_text, raw.salary_text, raw.workplace,
                                     raw.employment_type),
    }
    fields["rule_score"] = rule_score(fields, None, company.get("industry"))
    return fields


# ── persistence ──────────────────────────────────────────────────────────────

def _upsert(conn, company_id, raw, fields, existing, source, now):
    """Insert or merge one job; returns "new", "changed" or "same"."""
    if raw.detailed:
        fields["detail_fetched_at"] = now
    if existing is None:
        cols = {"company_id": company_id, "ats_job_id": raw.ats_job_id, "source": source, "first_seen_at": now,
                "last_seen_at": now, "missed_sweeps": 0, **fields}
        conn.execute(f"INSERT INTO jobs({','.join(cols)}) VALUES({','.join('?' * len(cols))})", tuple(cols.values()))
        return "new"
    merged = dict(fields)
    for key in _KEEP_WHEN_MISSING:
        if merged.get(key) is None:
            merged[key] = existing.get(key)
    if merged.get("workplace") == "unknown":
        merged["workplace"] = existing.get("workplace") or "unknown"
    if existing.get("posted_at") and not raw.detailed:
        merged["posted_at"] = existing["posted_at"]  # list-level dates are approximate
    merged.update(last_seen_at=now, missed_sweeps=0, closed_at=None, source=source)
    db.update(conn, "jobs", "id", existing["id"], merged)
    return "changed" if merged["content_hash"] != existing.get("content_hash") else "same"


def _touch(conn, existing, industry, now):
    """Mark an unchanged, already-detailed job seen; re-derive title rules so rule changes apply."""
    tier = title_tier(existing["title"])
    pf = prefilter(existing["title"], tier)
    score = rule_score({**existing, "title_tier": tier, "prefilter": pf}, None, industry)
    db.update(conn, "jobs", "id", existing["id"], {"last_seen_at": now, "missed_sweeps": 0, "closed_at": None,
                                                   "title_tier": tier, "prefilter": pf, "rule_score": score})


def close_missing(conn, company_id, seen_ids, now, source=None):
    """Jobs not seen this sweep get a miss; two misses in a row close them."""
    q = "SELECT id, ats_job_id, missed_sweeps FROM jobs WHERE company_id=? AND closed_at IS NULL"
    args = [company_id]
    if source:
        q += " AND source=?"
        args.append(source)
    closed = 0
    for r in conn.execute(q, args).fetchall():
        if r["ats_job_id"] in seen_ids:
            continue
        missed = (r["missed_sweeps"] or 0) + 1
        db.update(conn, "jobs", "id", r["id"], {"missed_sweeps": missed, "closed_at": now if missed >= 2 else None})
        closed += missed >= 2
    return closed


def upsert_jobs(conn, company, raws, adapter=None, source="ats", log_fn=None):
    """Normalise + persist listed jobs for one company. Returns (stats, seen ats_job_ids)."""
    now = db.now_iso()
    existing = {r["ats_job_id"]: dict(r) for r in conn.execute("SELECT * FROM jobs WHERE company_id=?", (company["id"],))}
    stats = {"jobs_seen": 0, "jobs_new": 0, "matching_new": 0, "details": 0, "changed": 0}
    seen = set()
    area = commute_area(conn)
    for raw in raws:
        if not raw.title or not raw.ats_job_id:
            continue
        if not is_local(_location(raw)):
            continue  # clearly out of area at list level: no detail fetch, not stored
        ex = existing.get(raw.ats_job_id)
        pf = prefilter(raw.title)
        stale = ex is not None and _age_days(ex.get("detail_fetched_at")) > DETAIL_STALE_DAYS
        needs_detail = (adapter is not None and adapter.has_detail and pf != "fail" and not raw.detailed and
                        (ex is None or not ex.get("detail_fetched_at") or ex["title"] != raw.title or stale))
        if needs_detail:
            try:
                raw = adapter.get_detail(company, raw)
                stats["details"] += 1
            except (FetchError, ValueError, KeyError) as exc:
                (log_fn or log.info)(f"{company['name']}: detail failed for {raw.title!r}: {exc}")
        if ex is not None and ex.get("detail_fetched_at") and adapter is not None and adapter.has_detail \
                and not raw.detailed:
            _touch(conn, ex, company.get("industry"), now)  # unchanged and already detailed
            seen.add(raw.ats_job_id)
            stats["jobs_seen"] += 1
            continue
        fields = normalize_job(raw, company, area)
        if fields is None:
            continue
        outcome = _upsert(conn, company["id"], raw, fields, ex, source, now)
        seen.add(raw.ats_job_id)
        stats["jobs_seen"] += 1
        if outcome == "new":
            stats["jobs_new"] += 1
            stats["matching_new"] += fields["prefilter"] in scoring.MATCHING
        elif outcome == "changed":
            stats["changed"] += 1
    return stats, seen


# ── per company ──────────────────────────────────────────────────────────────

def _company(conn, company_id):
    return db.row_dict(conn.execute("SELECT * FROM companies WHERE id=?", (company_id,)).fetchone())


def _say(run, line):
    if run:
        run.log(line)
    else:
        log.info(line)


def detect_company(conn, company, fetcher=None, run=None, homepage=None):
    """Careers page + ATS detection; persists and returns the updated company dict."""
    fields = careers.detect(company, fetcher, homepage=homepage)
    fields["updated_at"] = db.now_iso()
    db.update(conn, "companies", "id", company["id"], fields)
    conn.commit()
    company.update(fields)
    _say(run, f"{company['name']}: {company['status']} "
              f"({company.get('ats_type') or '-'}{', ' + company['status_reason'] if company.get('status_reason') else ''})")
    return company


def sweep_company(company_id, run=None, fetcher=None, force_detect=False):
    """Detect (if needed) and sweep one company. Returns stats. Never raises for site problems."""
    conn = db.connect()
    stats = {"companies": 1, "jobs_seen": 0, "jobs_new": 0, "matching_new": 0, "errors": 0}
    try:
        company = _company(conn, company_id)
        if not company or company["status"] == "ignored":
            return {"skipped": 1}
        if force_detect or needs_detection(company):
            company = detect_company(conn, company, fetcher, run)
        if company["status"] != "active":
            return {**stats, "skipped": 1}
        if company["ats_type"] == "html" or not ats.has_adapter(company["ats_type"]):
            return {**stats, **sweep_html(conn, company, fetcher, run)}
        adapter = ats.get_adapter(company["ats_type"], fetcher)
        raws = adapter.list_jobs(company, config.FUNCTION_KEYWORDS)
        job_stats, seen = upsert_jobs(conn, company, raws, adapter, "jsonld" if company["ats_type"] == "jsonld" else "ats",
                                      log_fn=lambda line: _say(run, line))
        closed = close_missing(conn, company_id, seen, db.now_iso())
        db.update(conn, "companies", "id", company_id, {"last_swept_at": db.now_iso(), "consecutive_failures": 0,
                                                         "last_error": None, "updated_at": db.now_iso()})
        conn.commit()
        _say(run, f"{company['name']}: {len(raws)} listed, {job_stats['jobs_seen']} local, {job_stats['jobs_new']} new "
                  f"({job_stats['matching_new']} matching), {job_stats['details']} details, {closed} closed")
        stats.update({k: job_stats[k] for k in ("jobs_seen", "jobs_new", "matching_new")}, closed=closed)
        return stats
    except Blocked as exc:
        _record_failure(conn, company_id, f"blocked: {exc}", blocked=True)
        _say(run, f"company {company_id}: blocked ({exc})")
        return {**stats, "errors": 1}
    except Exception as exc:  # noqa: BLE001 — one company's failure must not stop the sweep
        _record_failure(conn, company_id, f"{type(exc).__name__}: {exc}")
        _say(run, f"company {company_id}: error {type(exc).__name__}: {str(exc)[:160]}")
        return {**stats, "errors": 1}
    finally:
        conn.close()


def _record_failure(conn, company_id, error, blocked=False):
    conn.rollback()
    row = _company(conn, company_id) or {}
    failures = (row.get("consecutive_failures") or 0) + 1
    fields = {"consecutive_failures": failures, "last_error": error[:500], "updated_at": db.now_iso()}
    if blocked and failures >= MAX_FAILURES:
        fields.update(status="blocked", status_reason=error[:200])
    elif failures >= MAX_FAILURES:
        fields.update(status="manual_check", status_reason=f"{failures} failed sweeps: {error[:160]}")
    db.update(conn, "companies", "id", company_id, fields)
    conn.commit()


# ── careers pages read by AI (ats_type "html") ───────────────────────────────

def sweep_html(conn, company, fetcher, run=None):
    """Store the careers page text and queue parse_page when it changed."""
    html, final, _ = browser.page_html(company["careers_url"], fetcher)
    text = html_to_text(html)
    if len(text) < 400 and browser.available():
        rendered = browser.fetch_rendered(company["careers_url"])
        if rendered:
            text = html_to_text(rendered["html"])
    facts = db.loads(company.get("facts"), {})
    page_hash = content_hash(text[:12000])
    queued = False
    if page_hash != facts.get("_careers_hash"):
        facts.update(_careers_text=text[:12000], _careers_url=final, _careers_hash=page_hash)
        db.update(conn, "companies", "id", company["id"], {"facts": db.dumps(facts)})
        queued = bool(tasks.enqueue(conn, "parse_page", company_id=company["id"]))
    db.update(conn, "companies", "id", company["id"], {"last_swept_at": db.now_iso(), "consecutive_failures": 0})
    conn.commit()
    _say(run, f"{company['name']}: careers page {'changed → AI parse queued' if queued else 'unchanged'}")
    return {"pages_queued": int(queued)}


def apply_parsed_jobs(conn, company_id, result):
    """Upsert jobs an AI read off a careers page (source page_ai); jobs no longer listed get a miss."""
    company = _company(conn, company_id)
    if not company:
        return
    base = db.loads(company.get("facts"), {}).get("_careers_url") or company.get("careers_url") or ""
    raws = []
    for j in result["jobs"]:
        url = urljoin(base, j["url"]) if j.get("url") else None
        raws.append(RawJob(ats_job_id=url or content_hash(j["title"], j.get("location_text")), title=j["title"],
                           url=url or base, location_text=j.get("location_text"), workplace=j.get("workplace"),
                           salary_text=j.get("salary_text"),
                           description_html=f"<p>{j['summary']}</p>" if j.get("summary") else None))
    _, seen = upsert_jobs(conn, company, raws, None, "page_ai")
    close_missing(conn, company_id, seen, db.now_iso(), source="page_ai")
    scoring.enqueue_scores(conn)


# ── whole sweep ──────────────────────────────────────────────────────────────

def select_companies(conn, company_ids=None, limit=None, skip_swept_since=None):
    if company_ids:
        q = f"SELECT id FROM companies WHERE id IN ({','.join('?' * len(company_ids))}) AND status != 'ignored'"
        args = list(company_ids)
    else:
        q = "SELECT id FROM companies WHERE status IN ('active','pending')"
        args = []
        if skip_swept_since:
            q += " AND (last_swept_at IS NULL OR last_swept_at < ?)"
            args.append(skip_swept_since)
        q += " ORDER BY (last_swept_at IS NOT NULL), last_swept_at, id"
    if limit:
        q += " LIMIT ?"
        args.append(int(limit))
    return [r["id"] for r in conn.execute(q, args)]


def run_sweep(run, company_ids=None, limit=None, fetcher=None, skip_swept_since=None):
    """Sweep companies with up to MAX_WORKERS in parallel; returns run stats."""
    with db.session() as conn:
        ids = select_companies(conn, company_ids, limit, skip_swept_since)
    total = len(ids)
    run.log(f"sweeping {total} companies")
    run.progress(0, total, "sweep")
    stats = {"companies": 0, "jobs_seen": 0, "jobs_new": 0, "matching_new": 0, "errors": 0}
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = [pool.submit(sweep_company, cid, run, fetcher) for cid in ids]
        for done, fut in enumerate(as_completed(futures), 1):
            for key, value in (fut.result() or {}).items():
                stats[key] = stats.get(key, 0) + value
            run.progress(done, total, "sweep")
    with db.session() as conn:
        queued = scoring.enqueue_scores(conn)
        refresh_drive_times(conn)
    run.log(f"sweep done: {stats['jobs_new']} new jobs ({stats['matching_new']} matching), "
            f"{stats['errors']} errors; {queued} AI scoring tasks queued")
    stats["score_tasks"] = queued
    return stats


def refresh_drive_times(conn):
    """ORS drive minutes for open matching jobs (no-op without ORS_API_KEY)."""
    if not config.ors_api_key():
        return
    coords = [(r["lat"], r["lng"]) for r in conn.execute(
        "SELECT DISTINCT lat, lng FROM jobs WHERE closed_at IS NULL AND lat IS NOT NULL AND prefilter IN ('pass','maybe')")]
    for p in conn.execute("SELECT * FROM profiles WHERE home_lat IS NOT NULL").fetchall():
        geo.refresh_drive_minutes(conn, dict(p), coords)
