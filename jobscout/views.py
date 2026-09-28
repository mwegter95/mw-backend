"""Response builders for the HTTP API and `cli export-snapshot` (contract §5 shapes).

Filtering/sorting happens in Python over small row sets (hundreds of companies, low thousands of
jobs). Facets are computed "all filters except the facet's own dimension", so a chip still shows
counts for the alternatives after it is selected.

Note on JobSummary: the contract example uses the key "state" twice (US state, then the user's
{status, notes}); JSON parsers keep the last, so "state" is the user state object and the US state
is also exposed as "state_code".
"""
from datetime import datetime, timedelta, timezone

from . import config, db, discovery, geo, scoring, taxonomy, tasks
from .runs import run_dict

JOB_STATUS_DEFAULT_HIDDEN = {"hidden"}
_TARGET_TITLES = ["Marketing Director", "Marketing Manager", "Communications Director", "Communications Manager"]
PROFILE_KEYS = ["name", "resume_text", "want_text", "avoid_text", "target_titles", "industries_want",
                "industries_avoid", "salary_floor", "workplace_pref", "home_address", "home_lat", "home_lng",
                "radius_miles", "radius_minutes", "discover_industries", "discover_keywords", "discover_sources"]


# ── param helpers ───────────────────────────────────────────────────────────

def _csv(params, key):
    raw = params.get(key)
    return [v.strip() for v in str(raw).split(",") if v.strip()] if raw not in (None, "") else []


def _num(params, key, cast=float, default=None):
    try:
        return cast(params.get(key)) if params.get(key) not in (None, "") else default
    except (TypeError, ValueError):
        return default


def _flag(params, key, default=False):
    raw = params.get(key)
    return default if raw in (None, "") else str(raw).lower() in ("1", "true", "yes", "on")


def _bbox(params):
    parts = _csv(params, "bbox")
    try:
        return [float(p) for p in parts] if len(parts) == 4 else None
    except ValueError:
        return None


def _in_bbox(lat, lng, bbox):
    return lat is not None and lng is not None and bbox[0] <= lng <= bbox[2] and bbox[1] <= lat <= bbox[3]


def _days_ago(days):
    return db.now_iso(datetime.now(timezone.utc) - timedelta(days=days))


# ── profiles ────────────────────────────────────────────────────────────────

def default_profile(user=None):
    return {
        "name": (user or {}).get("display_name") or "", "resume_text": "", "want_text": "", "avoid_text": "",
        "target_titles": list(_TARGET_TITLES), "industries_want": [], "industries_avoid": [], "salary_floor": None,
        "workplace_pref": ["onsite", "hybrid", "remote"], "home_address": config.HOME_LABEL,
        "home_lat": config.HOME_LAT, "home_lng": config.HOME_LNG, "radius_miles": 35, "radius_minutes": 45,
        "discover_industries": list(discovery.DEFAULT_INDUSTRIES), "discover_keywords": [],
        "discover_sources": list(discovery.DEFAULT_SOURCES),
    }


def get_profile(conn, user_id):
    return scoring.profile_dict(conn.execute("SELECT * FROM profiles WHERE user_id=?", (user_id,)).fetchone())


def profile_json(p):
    return {k: p.get(k) for k in PROFILE_KEYS} if p else None


def _clean_profile(body, current):
    """Merge a PUT body over the current/default profile with type and enum checks."""
    out = dict(current)
    lists = {"target_titles": None, "industries_want": taxonomy.INDUSTRY_IDS, "industries_avoid": taxonomy.INDUSTRY_IDS,
             "workplace_pref": taxonomy.WORKPLACE, "discover_industries": taxonomy.INDUSTRY_IDS,
             "discover_keywords": None, "discover_sources": taxonomy.DISCOVER_SOURCES}
    for key, allowed in lists.items():
        if key in body and isinstance(body[key], list):
            vals = [str(v).strip()[:120] for v in body[key] if str(v).strip()]
            out[key] = [v for v in dict.fromkeys(vals) if allowed is None or v in allowed][:30]
    for key, limit in (("name", 120), ("resume_text", 20000), ("want_text", 4000), ("avoid_text", 4000),
                       ("home_address", 300)):
        if key in body:
            out[key] = str(body[key] or "")[:limit]
    for key, cast in (("salary_floor", int), ("radius_miles", int), ("radius_minutes", int),
                      ("home_lat", float), ("home_lng", float)):
        if key in body:
            try:
                out[key] = cast(body[key]) if body[key] not in (None, "") else None
            except (TypeError, ValueError):
                pass
    return out


def save_profile(conn, user, body, geocode=None):
    """PUT /profile: merge, geocode a changed address, re-hash, queue AI re-scores when inputs changed."""
    geocode = geocode or geo.geocode_address
    current = get_profile(conn, user["id"])
    base = current or default_profile(user)
    p = _clean_profile(body, base)
    address_changed = p.get("home_address") != base.get("home_address") or (current is None and p.get("home_address"))
    coords_given = "home_lat" in body and "home_lng" in body and body.get("home_lat") is not None
    if p.get("home_address") and address_changed and not coords_given:
        hit = geocode(p["home_address"])
        if hit:
            p["home_lat"], p["home_lng"] = hit[0], hit[1]
    p["input_hash"] = scoring.profile_input_hash(p)
    now = db.now_iso()
    cols = {k: (db.dumps(p[k]) if k in scoring.PROFILE_LIST_FIELDS else p.get(k)) for k in PROFILE_KEYS}
    cols.update(input_hash=p["input_hash"], updated_at=now)
    if current:
        db.update(conn, "profiles", "id", current["id"], cols)
    else:
        conn.execute(f"INSERT INTO profiles(user_id, {','.join(cols)}) VALUES(?, {','.join('?' * len(cols))})",
                     (user["id"], *cols.values()))
    conn.commit()
    saved = get_profile(conn, user["id"])
    if not current or current.get("input_hash") != saved["input_hash"]:
        scoring.enqueue_scores(conn, profile_ids=[saved["id"]])
    return saved


def touch_visit(conn, user_id):
    """GET /me: last_visit_at = now; prev_visit_at = old last visit when that was > 30 min ago."""
    p = conn.execute("SELECT id, last_visit_at FROM profiles WHERE user_id=?", (user_id,)).fetchone()
    if not p:
        return False
    fields = {"last_visit_at": db.now_iso()}
    last = db.parse_iso(p["last_visit_at"])
    if last and datetime.now(timezone.utc) - last > timedelta(minutes=30):
        fields["prev_visit_at"] = p["last_visit_at"]
    db.update(conn, "profiles", "id", p["id"], fields)
    conn.commit()
    return True


def _home(profile):
    if profile and profile.get("home_lat") is not None:
        return profile["home_lat"], profile["home_lng"]
    return config.HOME_LAT, config.HOME_LNG


# ── companies ───────────────────────────────────────────────────────────────

def _job_counts(conn):
    rows = conn.execute("SELECT company_id, COUNT(*) open_jobs, SUM(prefilter IN ('pass','maybe')) matching "
                        "FROM jobs WHERE closed_at IS NULL GROUP BY company_id")
    return {r["company_id"]: (r["open_jobs"], r["matching"] or 0) for r in rows}


def _company_states(conn, user_id):
    return {r["company_id"]: dict(r) for r in
            conn.execute("SELECT * FROM company_user_state WHERE user_id=?", (user_id,))}


def company_small(c):
    return {"id": c["id"], "name": c["name"], "domain": c["domain"], "industry": c.get("industry"),
            "industry_label": taxonomy.industry_label(c.get("industry")), "employee_band": c.get("employee_band"),
            "hidden_gem": bool(c.get("hidden_gem")), "gem_score": c.get("gem_score"), "logo_url": c.get("logo_url"),
            "hq_city": c.get("hq_city"), "hq_state": c.get("hq_state")}


def company_summary(c, counts=(0, 0), ustate=None, home=None):
    home = home or (config.HOME_LAT, config.HOME_LNG)
    wk = c.get("well_known")
    return {
        "id": c["id"], "name": c["name"], "domain": c["domain"], "homepage_url": c.get("homepage_url"),
        "careers_url": c.get("careers_url"), "ats_type": c.get("ats_type"), "status": c.get("status"),
        "status_reason": c.get("status_reason"), "industry": c.get("industry"),
        "industry_label": taxonomy.industry_label(c.get("industry")), "sub_industry": c.get("sub_industry"),
        "products": db.loads(c.get("products"), []), "summary": c.get("summary"),
        "business_model": c.get("business_model"), "ownership": c.get("ownership"),
        "parent_company": c.get("parent_company"), "employee_band": c.get("employee_band") or "unknown",
        "founded_year": c.get("founded_year"), "well_known": None if wk is None else bool(wk),
        "gem_score": c.get("gem_score"), "hidden_gem": bool(c.get("hidden_gem")), "tags": db.loads(c.get("tags"), []),
        "hq_city": c.get("hq_city"), "hq_state": c.get("hq_state"), "lat": c.get("lat"), "lng": c.get("lng"),
        "miles": geo.miles_between(home[0], home[1], c.get("lat"), c.get("lng")),
        "open_jobs": counts[0], "matching_jobs": counts[1], "enrich_status": c.get("enrich_status"),
        "enrich_source": c.get("enrich_source"), "last_swept_at": c.get("last_swept_at"),
        "following": bool((ustate or {}).get("following")), "notes": (ustate or {}).get("notes") or "",
        "entity_type": c.get("entity_type"), "local_presence": c.get("local_presence"),
        "discovered_via": c.get("discovered_via"), "discovered_at": c.get("discovered_at"), "source": c.get("source"),
    }


def _company_filters(params, bbox):
    q = [t.lower() for t in str(params.get("q") or "").split() if t]
    industries, sizes, ownership = set(_csv(params, "industries")), set(_csv(params, "sizes")), set(_csv(params, "ownership"))
    statuses = set(_csv(params, "status"))
    sources, entity_types = set(_csv(params, "source")), set(_csv(params, "entity_types"))
    enrich_statuses = set(_csv(params, "enrich_status"))
    max_miles = _num(params, "max_miles")
    since = _num(params, "discovered_since")
    since_iso = _days_ago(since) if since is not None else None

    def text(s):
        return " ".join(str(x or "") for x in (s["name"], s["domain"], s["summary"], s["sub_industry"],
                                                " ".join(s["products"]), s["industry_label"], s["hq_city"])).lower()
    return {
        "q": (lambda s: all(t in text(s) for t in q)) if q else None,
        "industries": (lambda s: s["industry"] in industries) if industries else None,
        "sizes": (lambda s: s["employee_band"] in sizes) if sizes else None,
        "ownership": (lambda s: s["ownership"] in ownership) if ownership else None,
        "gems": (lambda s: s["hidden_gem"]) if _flag(params, "gems_only") else None,
        "following": (lambda s: s["following"]) if _flag(params, "following_only") else None,
        "openings": (lambda s: s["matching_jobs"] > 0) if _flag(params, "has_openings") else None,
        "status": (lambda s: s["status"] in statuses) if statuses else (lambda s: s["status"] != "ignored"),
        "miles": (lambda s: s["miles"] is not None and s["miles"] <= max_miles) if max_miles is not None else None,
        "bbox": (lambda s: _in_bbox(s["lat"], s["lng"], bbox)) if bbox else None,
        "source": (lambda s: s["source"] in sources) if sources else None,
        "entity_types": (lambda s: s["entity_type"] in entity_types) if entity_types else None,
        "enrich_status": (lambda s: s["enrich_status"] in enrich_statuses) if enrich_statuses else None,
        "discovered": (lambda s: (s["discovered_at"] or "") >= since_iso) if since_iso else None,
    }


def _passes(item, filters, skip=None):
    return all(f(item) for k, f in filters.items() if f is not None and k != skip)


def _facet(items, filters, dim, key):
    out = {}
    for it in items:
        if _passes(it, filters, skip=dim):
            value = key(it)
            out[value] = out.get(value, 0) + 1
    return out


_COMPANY_SORTS = {
    "gem": lambda s: (-(s["gem_score"] or 0), s["name"].lower()),
    "name": lambda s: s["name"].lower(),
    "distance": lambda s: (s["miles"] is None, s["miles"] or 0),
    "openings": lambda s: (-s["matching_jobs"], -s["open_jobs"], s["name"].lower()),
}


def list_companies(conn, user, profile, params):
    home = _home(profile)
    counts, states = _job_counts(conn), _company_states(conn, user["id"])
    items = [company_summary(dict(r), counts.get(r["id"], (0, 0)), states.get(r["id"]), home)
             for r in conn.execute("SELECT * FROM companies")]
    filters = _company_filters(params, _bbox(params))
    matched = [s for s in items if _passes(s, filters)]
    sort = params.get("sort") or "gem"
    if sort == "recent":  # newest discoveries first
        matched.sort(key=lambda s: (s["discovered_at"] or "", s["id"]), reverse=True)
    else:
        matched.sort(key=_COMPANY_SORTS.get(sort, _COMPANY_SORTS["gem"]))
    limit = max(1, min(_num(params, "limit", int, 500), 1000))
    offset = max(0, _num(params, "offset", int, 0))
    return {"total": len(matched), "items": matched[offset:offset + limit],
            "facets": {"industries": _facet(items, filters, "industries", lambda s: s["industry"] or "unknown"),
                       "sizes": _facet(items, filters, "sizes", lambda s: s["employee_band"]),
                       "status": _facet(items, filters, "status", lambda s: s["status"])}}


def _public_facts(facts):
    return {k: v for k, v in facts.items() if not k.startswith("_")}


# ── jobs ────────────────────────────────────────────────────────────────────

def _scores(conn, profile):
    if not profile or not profile.get("id"):
        return {}
    return {r["job_id"]: dict(r) for r in conn.execute("SELECT * FROM job_scores WHERE profile_id=?", (profile["id"],))}


def _job_states(conn, user_id):
    return {r["job_id"]: dict(r) for r in conn.execute("SELECT * FROM job_user_state WHERE user_id=?", (user_id,))}


def _is_new(job, profile):
    first = job.get("first_seen_at") or ""
    if first >= _days_ago(7):
        return True
    prev = (profile or {}).get("prev_visit_at")
    return bool(prev and first > prev)


class _Ctx:
    """Everything needed to render JobSummaries for one caller."""

    def __init__(self, conn, user, profile):
        self.conn, self.user, self.profile = conn, user, profile
        self.home = _home(profile)
        self.scores = _scores(conn, profile)
        self.states = _job_states(conn, user["id"])
        self.companies = {r["id"]: dict(r) for r in conn.execute("SELECT * FROM companies")}
        self.minutes = geo.cached_minutes(conn, profile["id"]) if profile and profile.get("id") and \
            config.ors_api_key() else {}


def job_summary(job, ctx):
    c = ctx.companies.get(job["company_id"]) or {}
    fit = scoring.resolve_fit(job, ctx.scores.get(job["id"]), ctx.profile, c.get("industry"))
    lat, lng = (job["lat"], job["lng"]) if job.get("lat") is not None else (c.get("lat"), c.get("lng"))
    state = ctx.states.get(job["id"]) or {}
    minutes = ctx.minutes.get((round(lat, 4), round(lng, 4))) if lat is not None and ctx.minutes else None
    return {
        "id": job["id"], "title": job["title"], "title_tier": job.get("title_tier"), "prefilter": job.get("prefilter"),
        "company": company_small(c) if c else None,
        "location_text": job.get("location_text"), "city": job.get("city"), "state_code": job.get("state"),
        "lat": lat, "lng": lng, "geo_precision": job.get("geo_precision") or "none",
        "workplace": job.get("workplace") or "unknown", "employment_type": job.get("employment_type"),
        "salary_min": job.get("salary_min"), "salary_max": job.get("salary_max"),
        "salary_period": job.get("salary_period"), "salary_text": job.get("salary_text"),
        "posted_at": job.get("posted_at"), "first_seen_at": job.get("first_seen_at"),
        "is_new": _is_new(job, ctx.profile), "closed_at": job.get("closed_at"),
        "miles": geo.miles_between(ctx.home[0], ctx.home[1], lat, lng), "minutes": minutes,
        "fit": fit["fit"], "fit_source": fit["fit_source"], "tags": fit["tags"], "why": fit["why"],
        "state": {"status": state.get("status") or "new", "notes": state.get("notes") or ""},
        "url": job.get("url"), "apply_url": job.get("apply_url"),
        "_fit": fit,
    }


def _strip(item):
    item.pop("_fit", None)
    return item


def _job_filters(params, ctx, jobs_by_id):
    q = [t.lower() for t in str(params.get("q") or "").split() if t]
    fit_min = _num(params, "fit_min")
    smin, smax = _num(params, "salary_min"), _num(params, "salary_max")
    unlisted = _flag(params, "include_unlisted", True)
    workplaces, industries = set(_csv(params, "workplace")), set(_csv(params, "industries"))
    sizes, tiers = set(_csv(params, "sizes")), set(_csv(params, "tiers"))
    statuses = set(_csv(params, "status"))
    max_miles, max_minutes = _num(params, "max_miles"), _num(params, "max_minutes")
    within = _num(params, "posted_within")
    within_iso = _days_ago(within) if within is not None else None
    bbox = _bbox(params)
    follows = {cid for cid, s in _company_states(ctx.conn, ctx.user["id"]).items() if s.get("following")}

    def text(it):
        job = jobs_by_id[it["id"]]
        comp = it["company"] or {}
        return " ".join(str(x or "") for x in (it["title"], comp.get("name"), it["location_text"],
                                                comp.get("industry_label"), " ".join(it["tags"]), it["why"],
                                                (job.get("description_text") or "")[:6000])).lower()

    def salary_ok(it):
        if it["salary_min"] is None and it["salary_max"] is None:
            return unlisted
        top, bottom = it["salary_max"] or it["salary_min"], it["salary_min"] or it["salary_max"]
        return (smin is None or top >= smin) and (smax is None or bottom <= smax)

    def posted(it):
        return (it["posted_at"] or it["first_seen_at"] or "") >= within_iso

    return {
        "q": (lambda it: all(t in text(it) for t in q)) if q else None,
        "fit": (lambda it: it["fit"] >= fit_min) if fit_min is not None else None,
        "salary": salary_ok if (smin is not None or smax is not None or not unlisted) else None,
        "workplace": (lambda it: it["workplace"] in workplaces) if workplaces else None,
        "miles": (lambda it: it["miles"] is not None and it["miles"] <= max_miles) if max_miles is not None else None,
        "minutes": (lambda it: it["minutes"] is None or it["minutes"] <= max_minutes) if max_minutes is not None else None,
        "posted": posted if within_iso else None,
        "industries": (lambda it: (it["company"] or {}).get("industry") in industries) if industries else None,
        "sizes": (lambda it: (it["company"] or {}).get("employee_band") in sizes) if sizes else None,
        "gems": (lambda it: (it["company"] or {}).get("hidden_gem")) if _flag(params, "gems_only") else None,
        "status": (lambda it: it["state"]["status"] in statuses) if statuses
        else (lambda it: it["state"]["status"] not in JOB_STATUS_DEFAULT_HIDDEN),
        "tiers": (lambda it: it["title_tier"] in tiers) if tiers else None,
        "following": (lambda it: jobs_by_id[it["id"]]["company_id"] in follows) if _flag(params, "following_only") else None,
        "bbox": (lambda it: _in_bbox(it["lat"], it["lng"], bbox)) if bbox else None,
    }


_JOB_SORTS = {
    "fit": lambda it: (-it["fit"], _neg(it["first_seen_at"])),
    "new": lambda it: (_neg(it["first_seen_at"]), -it["fit"]),
    "salary": lambda it: (it["salary_max"] is None and it["salary_min"] is None,
                          -(it["salary_max"] or it["salary_min"] or 0), -it["fit"]),
    "distance": lambda it: (it["miles"] is None, it["miles"] or 0, -it["fit"]),
    "gem": lambda it: (-((it["company"] or {}).get("gem_score") or 0), -it["fit"]),
}


def _neg(iso):
    """Sort key that orders ISO strings descending."""
    return tuple(-ord(ch) for ch in (iso or ""))


def _load_jobs(conn, where, args):
    return [dict(r) for r in conn.execute(
        "SELECT j.* FROM jobs j JOIN companies c ON c.id = j.company_id WHERE c.status != 'ignored' AND " + where, args)]


def list_jobs(conn, user, profile, params):
    ctx = _Ctx(conn, user, profile)
    prefilters = _csv(params, "prefilter") or ["pass", "maybe"]
    where = f"j.prefilter IN ({','.join('?' * len(prefilters))})"
    args = list(prefilters)
    if not _flag(params, "include_closed"):
        where += " AND j.closed_at IS NULL"
    company_id = _num(params, "company_id", int)
    if company_id is not None:
        where += " AND j.company_id = ?"
        args.append(company_id)
    jobs = _load_jobs(conn, where, args)
    by_id = {j["id"]: j for j in jobs}
    items = [job_summary(j, ctx) for j in jobs]
    filters = _job_filters(params, ctx, by_id)
    matched = [it for it in items if _passes(it, filters)]
    matched.sort(key=_JOB_SORTS.get(params.get("sort") or "fit", _JOB_SORTS["fit"]))
    salaried = [it for it in items if _passes(it, filters, skip="salary")]
    known = [v for it in salaried for v in (it["salary_min"], it["salary_max"]) if v is not None]
    workplace = {w: 0 for w in taxonomy.WORKPLACE}
    workplace.update(_facet(items, filters, "workplace", lambda it: it["workplace"]))
    limit = max(1, min(_num(params, "limit", int, 300), 1000))
    offset = max(0, _num(params, "offset", int, 0))
    return {"total": len(matched), "items": [_strip(it) for it in matched[offset:offset + limit]],
            "facets": {"workplace": workplace,
                       "industries": _facet(items, filters, "industries",
                                            lambda it: (it["company"] or {}).get("industry") or "unknown"),
                       "tiers": _facet(items, filters, "tiers", lambda it: it["title_tier"]),
                       "salary": {"min": min(known) if known else None, "max": max(known) if known else None,
                                  "unlisted": sum(1 for it in salaried if it["salary_min"] is None and it["salary_max"] is None)}}}


def job_detail(conn, user, profile, job_id, ctx=None):
    ctx = ctx or _Ctx(conn, user, profile)
    job = db.row_dict(conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())
    if not job:
        return None
    item = job_summary(job, ctx)
    fit = item.pop("_fit")
    company = ctx.companies.get(job["company_id"]) or {}
    counts = _job_counts(conn).get(company.get("id"), (0, 0))
    others = _load_jobs(conn, "j.company_id = ? AND j.id != ? AND j.closed_at IS NULL AND j.prefilter IN ('pass','maybe')",
                        (job["company_id"], job_id))
    other_items = sorted((job_summary(o, ctx) for o in others), key=lambda it: -it["fit"])[:20]
    item.update({
        "description_html": job.get("description_html") or "", "dealbreakers": fit["dealbreakers"],
        "seniority": fit["seniority"], "role_family": fit["role_family"],
        "company": company_summary(company, counts, _company_states(conn, user["id"]).get(company.get("id")), ctx.home)
        if company else None,
        "other_jobs": [{"id": o["id"], "title": o["title"], "fit": o["fit"]} for o in other_items],
    })
    return item


def company_detail(conn, user, profile, company_id):
    row = db.row_dict(conn.execute("SELECT * FROM companies WHERE id=?", (company_id,)).fetchone())
    if not row:
        return None
    home = _home(profile)
    out = company_summary(row, _job_counts(conn).get(company_id, (0, 0)),
                          _company_states(conn, user["id"]).get(company_id), home)
    ctx = _Ctx(conn, user, profile)
    jobs = _load_jobs(conn, "j.company_id = ? AND j.closed_at IS NULL AND j.prefilter IN ('pass','maybe')", (company_id,)) \
        if row["status"] != "ignored" else []
    out["jobs"] = sorted((_strip(job_summary(j, ctx)) for j in jobs), key=lambda it: -it["fit"])
    out["facts"] = _public_facts(db.loads(row.get("facts"), {}))
    return out


def set_job_state(conn, user, job_id, body):
    now = db.now_iso()
    cur = db.row_dict(conn.execute("SELECT * FROM job_user_state WHERE job_id=? AND user_id=?",
                                   (job_id, user["id"])).fetchone()) or {"status": "new", "notes": "", "applied_at": None}
    status = body.get("status", cur["status"])
    if status not in taxonomy.JOB_STATUSES:
        raise ValueError("invalid_status")
    notes = str(body.get("notes", cur["notes"]) or "")[:10000]
    applied_at = cur.get("applied_at") or (now if status == "applied" else None)
    conn.execute("INSERT INTO job_user_state(job_id, user_id, status, notes, applied_at, updated_at) VALUES(?,?,?,?,?,?) "
                 "ON CONFLICT(job_id, user_id) DO UPDATE SET status=excluded.status, notes=excluded.notes, "
                 "applied_at=excluded.applied_at, updated_at=excluded.updated_at",
                 (job_id, user["id"], status, notes, applied_at, now))
    conn.commit()
    return {"status": status, "notes": notes, "applied_at": applied_at}


def patch_company(conn, user, company_id, body):
    from . import ats
    row = db.row_dict(conn.execute("SELECT * FROM companies WHERE id=?", (company_id,)).fetchone())
    if "following" in body or "notes" in body:
        st = _company_states(conn, user["id"]).get(company_id) or {"following": 0, "notes": ""}
        following = int(bool(body.get("following", st["following"])))
        notes = str(body.get("notes", st["notes"]) or "")[:10000]
        conn.execute("INSERT INTO company_user_state(company_id, user_id, following, notes, updated_at) VALUES(?,?,?,?,?) "
                     "ON CONFLICT(company_id, user_id) DO UPDATE SET following=excluded.following, notes=excluded.notes, "
                     "updated_at=excluded.updated_at", (company_id, user["id"], following, notes, db.now_iso()))
    if body.get("status") in ("ignored", "active"):
        facts = db.loads(row.get("facts"), {})
        if body["status"] == "ignored":
            fields = {"status": "ignored", "status_reason": "ignored by user"}
            facts.pop("_user_restored", None)
        else:
            ready = row.get("ats_type") and (ats.has_adapter(row["ats_type"]) or row["ats_type"] == "html")
            fields = {"status": "active" if ready else "pending", "status_reason": None}
            facts["_user_restored"] = True
        fields.update(facts=db.dumps(facts), updated_at=db.now_iso())
        db.update(conn, "companies", "id", company_id, fields)
    conn.commit()


# ── status ──────────────────────────────────────────────────────────────────

def status(conn, user, profile):
    by_status = {s: 0 for s in taxonomy.COMPANY_STATUSES}
    for r in conn.execute("SELECT status, COUNT(*) n FROM companies GROUP BY status"):
        by_status[r["status"] or "pending"] = r["n"]
    open_jobs = "FROM jobs j JOIN companies c ON c.id = j.company_id WHERE j.closed_at IS NULL AND c.status != 'ignored'"
    open_matching = conn.execute(f"SELECT j.id, j.content_hash, j.first_seen_at {open_jobs} "
                                 "AND j.prefilter IN ('pass','maybe')").fetchall()
    scores = _scores(conn, profile)
    unscored = 0
    if profile:
        for j in open_matching:
            s = scores.get(j["id"])
            if not s or s.get("input_hash") != scoring.score_input_hash(profile.get("input_hash"), j["content_hash"]):
                unscored += 1
    else:
        unscored = len(open_matching)
    week = _days_ago(7)
    last = conn.execute("SELECT * FROM runs WHERE kind='sweep' ORDER BY id DESC LIMIT 1").fetchone()
    from .scheduler import next_sweep_at
    return {
        "counts": {
            "companies": sum(v for k, v in by_status.items() if k != "ignored"),
            "companies_by_status": by_status,
            "hidden_gems": conn.execute("SELECT COUNT(*) n FROM companies WHERE hidden_gem=1 AND status != 'ignored'").fetchone()["n"],
            "jobs_open": conn.execute(f"SELECT COUNT(*) n {open_jobs}").fetchone()["n"],
            "jobs_matching": len(open_matching),
            "new_this_week": sum(1 for j in open_matching if (j["first_seen_at"] or "") >= week),
            "unscored": unscored,
        },
        "last_sweep": run_dict(last) if last else None,
        "next_sweep_at": next_sweep_at() if config.scheduler_enabled() else None,
        "queue": tasks.counts(conn),
        "instances": instances(conn),
    }


def instances(conn):
    primary_commit = config.commit_sha()
    out = [{"instance": config.instance(), "role": "primary", "commit": primary_commit, "protocol": config.PROTOCOL,
            "online": True}]
    cutoff = db.now_iso(datetime.now(timezone.utc) - timedelta(seconds=90))
    for w in conn.execute("SELECT * FROM workers ORDER BY worker_id"):
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        mismatch = w["protocol"] != config.PROTOCOL or (
            w["commit_sha"] not in (None, "", "unknown") and primary_commit != "unknown" and w["commit_sha"] != primary_commit)
        out.append({"instance": w["instance"], "role": w["role"], "model": w["model"], "lmstudio_ok": bool(w["lmstudio_ok"]),
                    "online": (w["last_heartbeat_at"] or "") >= cutoff, "last_heartbeat_at": w["last_heartbeat_at"],
                    "commit": w["commit_sha"], "protocol": w["protocol"],
                    "tasks_done_today": w["tasks_done_today"] if w["day"] == today else 0,
                    "version_mismatch": bool(mismatch)})
    return out


def export_snapshot(conn, user, profile):
    """Everything the frontend needs as fixtures, exactly as the API renders it (an unsaved default
    profile stands in when the user has none, so rule fits use the default target titles)."""
    profile = profile or default_profile(user)
    ctx = _Ctx(conn, user, profile)
    ids = [j["id"] for j in _load_jobs(conn, "j.closed_at IS NULL AND j.prefilter IN ('pass','maybe')", ())]
    jobs = [job_detail(conn, user, profile, jid, ctx) for jid in ids]
    jobs.sort(key=lambda it: -it["fit"])
    companies = list_companies(conn, user, profile, {"status": ",".join(taxonomy.COMPANY_STATUSES), "limit": 1000})
    return {"generated_at": db.now_iso(), "jobs": jobs, "companies": companies["items"],
            "status": status(conn, user, profile), "profile": profile_json(profile or default_profile(user))}
