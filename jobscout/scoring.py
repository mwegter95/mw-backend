"""Fit scoring: rule scores, AI score payloads/results, and which score the UI sees.

A job's AI score is current when job_scores.input_hash == hash(profile.input_hash, job.content_hash);
otherwise the API falls back to the rule score for that profile (computed on the fly — cheap).
"""
import hashlib

from . import db, normalize, taxonomy, tasks

PROFILE_LIST_FIELDS = ("target_titles", "industries_want", "industries_avoid", "workplace_pref",
                       "discover_industries", "discover_keywords", "discover_sources")
_HASHED_FIELDS = ("resume_text", "want_text", "avoid_text", "target_titles", "industries_want", "industries_avoid",
                  "salary_floor", "workplace_pref")
MATCHING = ("pass", "maybe")


def profile_dict(row):
    """Profile row → dict with JSON list columns decoded."""
    if row is None:
        return None
    p = dict(row)
    for key in PROFILE_LIST_FIELDS:
        p[key] = db.loads(p.get(key), [])
    return p


def profile_input_hash(profile) -> str:
    blob = db.dumps([profile.get(k) for k in _HASHED_FIELDS])
    return hashlib.sha1(blob.encode()).hexdigest()[:16]


def score_input_hash(profile_hash, job_content_hash) -> str:
    return hashlib.sha1(f"{profile_hash}|{job_content_hash}".encode()).hexdigest()[:16]


def rule_score(job, profile=None, industry=None) -> int:
    return normalize.rule_score(job, profile, industry)


def resolve_fit(job, score_row, profile, industry):
    """{fit, fit_source, tags, why, dealbreakers, seniority, role_family} for the UI."""
    if profile and score_row and score_row.get("fit_source") == "ai" and \
            score_row.get("input_hash") == score_input_hash(profile.get("input_hash"), job.get("content_hash")):
        return {"fit": score_row["fit"], "fit_source": "ai", "tags": db.loads(score_row.get("tags"), []),
                "why": score_row.get("why") or "", "dealbreakers": db.loads(score_row.get("dealbreakers"), []),
                "seniority": score_row.get("seniority"), "role_family": score_row.get("role_family")}
    return {"fit": rule_score(job, profile, industry), "fit_source": "rules", "tags": [], "why": "",
            "dealbreakers": [], "seniority": None, "role_family": None}


def _salary_text(job):
    if job.get("salary_text"):
        return job["salary_text"]
    if job.get("salary_min"):
        return f"${job['salary_min']:,.0f} - ${job['salary_max'] or job['salary_min']:,.0f} per year"
    return ""


def build_score_payload(conn, job_id, profile_id):
    """(payload, input_hash) for score_job; (None, None) when the job is closed, gone or no longer matching."""
    job = db.row_dict(conn.execute("SELECT * FROM jobs WHERE id=? AND closed_at IS NULL AND prefilter IN ('pass','maybe')",
                                   (job_id,)).fetchone())
    profile = profile_dict(conn.execute("SELECT * FROM profiles WHERE id=?", (profile_id,)).fetchone())
    if not job or not profile:
        return None, None
    company = db.row_dict(conn.execute("SELECT * FROM companies WHERE id=?", (job["company_id"],)).fetchone()) or {}
    labels = lambda slugs: [taxonomy.INDUSTRY_LABEL.get(s, s) for s in slugs]  # noqa: E731
    payload = {
        "job": {
            "title": job["title"], "company_name": company.get("name"), "company_summary": company.get("summary") or "",
            "industry_label": taxonomy.industry_label(company.get("industry")) or "",
            "employee_band": company.get("employee_band") or "unknown", "hidden_gem": bool(company.get("hidden_gem")),
            "location_text": job.get("location_text") or "", "workplace": job.get("workplace") or "unknown",
            "salary_text": _salary_text(job), "description_text": (job.get("description_text") or "")[:9000],
        },
        "profile": {
            "resume_text": (profile.get("resume_text") or "")[:8000], "want_text": profile.get("want_text") or "",
            "avoid_text": profile.get("avoid_text") or "", "target_titles": profile["target_titles"],
            "industries_want": labels(profile["industries_want"]), "industries_avoid": labels(profile["industries_avoid"]),
            "salary_floor": profile.get("salary_floor"), "workplace_pref": profile["workplace_pref"],
        },
    }
    return payload, score_input_hash(profile.get("input_hash"), job.get("content_hash"))


def apply_score(conn, task, result, model):
    """Persist a validated score; fill unknown job salary/workplace from the AI's reading."""
    conn.execute(
        "INSERT OR REPLACE INTO job_scores(job_id, profile_id, fit, fit_source, role_family, seniority, workplace, tags, "
        "why, dealbreakers, model, input_hash, scored_at) VALUES(?,?,?,'ai',?,?,?,?,?,?,?,?,?)",
        (task["job_id"], task["profile_id"], result["fit"], result["role_family"], result["seniority"],
         result["workplace"], db.dumps(result["tags"]), result["why"], db.dumps(result["dealbreakers"]), model,
         task["payload_hash"], db.now_iso()))
    job = db.row_dict(conn.execute("SELECT * FROM jobs WHERE id=?", (task["job_id"],)).fetchone())
    if not job:
        return
    fields = {}
    if job.get("salary_min") is None and (result["salary_min"] or result["salary_max"]):
        sal = normalize.make_salary(result["salary_min"], result["salary_max"], result["salary_period"] or "year")
        if sal:
            fields.update(salary_min=sal.min, salary_max=sal.max, salary_period=sal.period, salary_text=sal.text)
    if (job.get("workplace") or "unknown") == "unknown" and result["workplace"] != "unknown":
        fields["workplace"] = result["workplace"]
    db.update(conn, "jobs", "id", job["id"], fields)


def enqueue_scores(conn, profile_ids=None, job_ids=None) -> int:
    """Queue score_job for every (open pass/maybe job × profile) whose AI score is missing or stale."""
    q = "SELECT * FROM profiles" + (f" WHERE id IN ({','.join('?' * len(profile_ids))})" if profile_ids else "")
    profiles = [profile_dict(r) for r in conn.execute(q, tuple(profile_ids or ()))]
    jq = ("SELECT j.id, j.content_hash FROM jobs j JOIN companies c ON c.id = j.company_id "
          "WHERE j.closed_at IS NULL AND j.prefilter IN ('pass','maybe') AND c.status != 'ignored'")
    if job_ids:
        jq += f" AND j.id IN ({','.join('?' * len(job_ids))})"
    jobs = conn.execute(jq, tuple(job_ids or ())).fetchall()
    queued = 0
    for p in profiles:
        if not p.get("input_hash"):
            continue
        current = {r["job_id"]: r["input_hash"] for r in
                   conn.execute("SELECT job_id, input_hash FROM job_scores WHERE profile_id=?", (p["id"],))}
        for j in jobs:
            if current.get(j["id"]) != score_input_hash(p["input_hash"], j["content_hash"]):
                if tasks.enqueue(conn, "score_job", job_id=j["id"], profile_id=p["id"]):
                    queued += 1
    conn.commit()
    return queued
