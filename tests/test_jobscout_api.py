"""HTTP API: a tiny Flask app with jobscout_bp, a temp data dir holding mw.db + .secret_key + jobscout.db."""
import json
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone

import jwt
import pytest
from flask import Flask

from conftest import insert_company, insert_job
from jobscout import db, find, geo, pipeline, runs, sweep, tasks
from jobscout.api import jobscout_bp

SECRET = "test-secret-with-at-least-thirty-two-bytes"
JOB_KEYS = {"id", "title", "title_tier", "prefilter", "company", "location_text", "city", "state_code", "lat", "lng",
            "geo_precision", "workplace", "employment_type", "salary_min", "salary_max", "salary_period", "salary_text",
            "posted_at", "first_seen_at", "is_new", "closed_at", "miles", "minutes", "fit", "fit_source", "tags", "why",
            "state", "url", "apply_url"}
SMALL_COMPANY_KEYS = {"id", "name", "domain", "industry", "industry_label", "employee_band", "hidden_gem", "gem_score",
                      "logo_url", "hq_city", "hq_state"}
COMPANY_KEYS = {"id", "name", "domain", "homepage_url", "careers_url", "ats_type", "status", "status_reason", "industry",
                "industry_label", "sub_industry", "products", "summary", "business_model", "ownership", "parent_company",
                "employee_band", "founded_year", "well_known", "gem_score", "hidden_gem", "tags", "hq_city", "hq_state",
                "lat", "lng", "miles", "open_jobs", "matching_jobs", "enrich_status", "enrich_source", "last_swept_at",
                "following", "notes", "entity_type", "local_presence", "discovered_via", "discovered_at", "source"}
PROFILE_KEYS = {"name", "resume_text", "want_text", "avoid_text", "target_titles", "job_categories", "seniority",
                "industries_want", "industries_avoid",
                "salary_floor", "workplace_pref", "home_address", "home_lat", "home_lng", "radius_miles", "radius_minutes",
                "discover_industries", "discover_keywords", "discover_sources"}


@pytest.fixture
def app_env(data_dir, monkeypatch):
    (data_dir / ".secret_key").write_text(SECRET)
    users = sqlite3.connect(str(data_dir / "mw.db"))
    users.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, email TEXT, password_hash TEXT, display_name TEXT)")
    users.executemany("INSERT INTO users VALUES (?,?,?,?)", [(1, "zweetztuph@gmail.com", "x", "Michael"),
                                                             (2, "stranger@example.com", "x", "Stranger")])
    users.commit()
    users.close()
    monkeypatch.setenv("JOBS_ALLOWED_EMAILS", "zweetztuph@gmail.com")
    monkeypatch.setenv("JOBS_WORKER_TOKEN", "worker-secret")
    monkeypatch.setattr(geo, "geocode_address", lambda address, fetcher=None: (45.0619, -92.9766, "city"))
    app = Flask(__name__)
    app.register_blueprint(jobscout_bp)
    return app.test_client()


def token(user_id=1, email="zweetztuph@gmail.com", **extra):
    payload = {"sub": str(user_id), "email": email, "exp": datetime.now(timezone.utc) + timedelta(hours=1), **extra}
    return jwt.encode(payload, SECRET, algorithm="HS256")


def auth(user_id=1):
    return {"Authorization": f"Bearer {token(user_id)}"}


WORKER = {"X-Worker-Token": "worker-secret"}


@pytest.fixture
def seeded(app_env, conn):
    gem = insert_company(conn, name="Acme Plastics", domain="acmeplastics.com", gem_score=85, hidden_gem=1,
                         source="discovery", discovered_via='maps: "injection molding" Oakdale, MN',
                         discovered_at=db.now_iso(), facts=db.dumps({"meta_description": "x", "_careers_text": "y"}))
    big = insert_company(conn, name="Big Health", domain="bighealth.org", industry="healthcare", employee_band="5000+",
                         lat=44.83, lng=-93.31, hq_city="Bloomington", gem_score=20, source="manual")
    junk = insert_company(conn, name="Plastics Directory", domain="dir.example", status="ignored", entity_type="directory")
    ids = {
        "mm": insert_job(conn, gem, ats_job_id="mm", title="Marketing Manager"),
        "cd": insert_job(conn, gem, ats_job_id="cd", title="Communications Director", title_tier="director",
                         workplace="remote", salary_min=None, salary_max=None, salary_text=None, salary_period=None),
        "pm": insert_job(conn, big, ats_job_id="pm", title="Global Product Marketer", title_tier="ic", prefilter="maybe",
                         workplace="onsite", salary_min=140000, salary_max=170000, lat=44.83, lng=-93.31,
                         city="Bloomington", location_text="Bloomington, MN"),
        "welder": insert_job(conn, gem, ats_job_id="w", title="Welder", title_tier="ic", prefilter="fail"),
        "closed": insert_job(conn, big, ats_job_id="old", title="Brand Manager", closed_at=db.now_iso()),
        "ignored": insert_job(conn, junk, ats_job_id="x", title="Marketing Manager"),
    }
    return {"client": app_env, "gem": gem, "big": big, "junk": junk, **ids}


def get(client, url, user=1):
    resp = client.get(url, headers=auth(user))
    return resp.status_code, resp.get_json()


# ── auth / basics ───────────────────────────────────────────────────────────

def test_health_is_open(app_env):
    body = app_env.get("/jobs/health").get_json()
    assert body["ok"] and body["protocol"] == 1 and body["role"] == "jobscout" and body["commit"]


def test_auth_paths(app_env):
    assert app_env.get("/jobs/api/me").status_code == 401
    assert app_env.get("/jobs/api/me", headers={"Authorization": "Bearer nope"}).get_json() == {"error": "invalid_token"}
    expired = jwt.encode({"sub": "1", "exp": datetime.now(timezone.utc) - timedelta(minutes=1)}, SECRET, algorithm="HS256")
    assert app_env.get("/jobs/api/me", headers={"Authorization": f"Bearer {expired}"}).get_json()["error"] == "token_expired"
    assert app_env.get(f"/jobs/api/me?_tok={token()}").status_code == 200
    assert app_env.get("/jobs/api/me", headers={"X-Auth-Token": token()}).status_code == 200
    resp = app_env.get("/jobs/api/me", headers=auth(2))
    assert resp.status_code == 403 and resp.get_json() == {"error": "not_allowed"}


def test_me_meta_and_visits(app_env, conn):
    code, body = get(app_env, "/jobs/api/me")
    assert body == {"user": {"id": 1, "email": "zweetztuph@gmail.com", "display_name": "Michael"}, "has_profile": False}
    app_env.put("/jobs/api/profile", headers=auth(), json={"name": "Ashley"})
    old = db.now_iso(datetime.now(timezone.utc) - timedelta(hours=3))
    conn.execute("UPDATE profiles SET last_visit_at=?", (old,))
    conn.commit()
    assert get(app_env, "/jobs/api/me")[1]["has_profile"] is True
    row = conn.execute("SELECT last_visit_at, prev_visit_at FROM profiles").fetchone()
    assert row["prev_visit_at"] == old and row["last_visit_at"] > old
    meta = get(app_env, "/jobs/api/meta")[1]
    assert len(meta["industries"]) == 26 and meta["industries"][0] == {"id": "construction_real_estate",
                                                                        "label": "Construction & Real Estate",
                                                                        "group": "Built environment"}
    assert meta["industries"][-1]["id"] == "other"  # alphabetical by group and name, "Other" last
    assert {"id": "marketing", "label": "Marketing"} in meta["job_categories"] and len(meta["job_categories"]) == 21
    assert meta["levels"] == ["exec", "director", "manager", "lead", "ic"]
    assert meta["places_enabled"] is False and "hidden" in meta["statuses"] and meta["employee_bands"][-1] == "unknown"


def test_profile_get_put(app_env):
    assert get(app_env, "/jobs/api/profile")[1] == {"profile": None}
    resp = app_env.put("/jobs/api/profile", headers=auth(),
                       json={"name": "Ashley", "home_address": "Birchwood Village, MN", "salary_floor": "110000",
                             "industries_want": ["tech_software", "bogus"], "discover_keywords": ["precast concrete"],
                             "workplace_pref": ["hybrid", "remote"], "job_categories": ["marketing", "nope"],
                             "seniority": ["director", "intern"], "target_titles": ["Director of Brand"]})
    profile = resp.get_json()["profile"]
    assert set(profile) == PROFILE_KEYS
    assert profile["salary_floor"] == 110000 and profile["industries_want"] == ["tech_software"]
    assert profile["discover_industries"] == []  # nothing assumed: discovery then covers every industry
    assert profile["job_categories"] == ["marketing"] and profile["seniority"] == ["director"]
    assert profile["target_titles"] == ["Director of Brand"] and profile["home_lat"] == 45.0619
    assert get(app_env, "/jobs/api/profile")[1]["profile"]["discover_keywords"] == ["precast concrete"]


# ── jobs ────────────────────────────────────────────────────────────────────

def test_jobs_list_defaults_shape_and_facets(seeded):
    code, body = get(seeded["client"], "/jobs/api/jobs")
    assert code == 200 and body["total"] == 3
    ids = {it["id"] for it in body["items"]}
    assert ids == {seeded["mm"], seeded["cd"], seeded["pm"]}  # no fail, closed or ignored-company jobs
    item = body["items"][0]
    assert set(item) == JOB_KEYS and set(item["company"]) == SMALL_COMPANY_KEYS
    assert item["state"] == {"status": "new", "notes": ""} and item["fit_source"] == "rules" and item["is_new"]
    assert body["facets"]["workplace"] == {"onsite": 1, "hybrid": 1, "remote": 1, "unknown": 0}
    assert body["facets"]["tiers"] == {"manager": 1, "director": 1, "ic": 1}
    assert body["facets"]["salary"] == {"min": 95000, "max": 170000, "unlisted": 1}
    assert [it["id"] for it in body["items"]][:1] == [seeded["cd"]]  # director + unknown salary scores highest


@pytest.mark.parametrize("query,expected", [
    ("workplace=remote,hybrid", {"mm", "cd"}),
    ("salary_min=130000&include_unlisted=0", {"pm"}),
    ("salary_min=130000", {"pm", "cd"}),
    ("fit_min=70", {"mm", "cd"}),
    ("max_miles=10", {"mm", "cd"}),
    ("q=bloomington", {"pm"}),
    ("tiers=director", {"cd"}),
    ("industries=healthcare", {"pm"}),
    ("gems_only=1", {"mm", "cd"}),
    ("prefilter=fail", {"welder"}),
    ("include_closed=1", {"mm", "cd", "pm", "closed"}),
    ("bbox=-93.0,44.9,-92.9,45.0", {"mm", "cd"}),
    ("posted_within=3", {"mm", "cd", "pm"}),
])
def test_jobs_filters(seeded, query, expected):
    body = get(seeded["client"], f"/jobs/api/jobs?{query}")[1]
    assert {it["id"] for it in body["items"]} == {seeded[k] for k in expected}


def test_jobs_sort_and_facet_excludes_own_dimension(seeded):
    body = get(seeded["client"], "/jobs/api/jobs?sort=salary")[1]
    assert [it["id"] for it in body["items"]] == [seeded["pm"], seeded["mm"], seeded["cd"]]
    body = get(seeded["client"], "/jobs/api/jobs?workplace=remote")[1]
    assert body["total"] == 1 and body["facets"]["workplace"]["onsite"] == 1  # other chips keep their counts
    body = get(seeded["client"], "/jobs/api/jobs?sort=distance&limit=1&offset=1")[1]
    assert body["total"] == 3 and len(body["items"]) == 1


def test_job_detail_and_state(seeded):
    client = seeded["client"]
    code, body = get(client, f"/jobs/api/jobs/{seeded['mm']}")
    assert code == 200 and set(body) >= JOB_KEYS | {"description_html", "dealbreakers", "seniority", "role_family", "other_jobs"}
    assert set(body["company"]) == COMPANY_KEYS and body["other_jobs"] == [{"id": seeded["cd"], "title": "Communications Director",
                                                                             "fit": body["other_jobs"][0]["fit"]}]
    assert get(client, "/jobs/api/jobs/99999")[0] == 404
    r1 = client.put(f"/jobs/api/jobs/{seeded['mm']}/state", headers=auth(), json={"status": "applied", "notes": "sent"}).get_json()
    assert r1["ok"] and r1["state"]["status"] == "applied" and r1["state"]["applied_at"]
    time.sleep(1.1)
    r2 = client.put(f"/jobs/api/jobs/{seeded['mm']}/state", headers=auth(), json={"status": "interviewing"}).get_json()
    assert r2["state"]["applied_at"] == r1["state"]["applied_at"] and r2["state"]["notes"] == "sent"
    assert client.put(f"/jobs/api/jobs/{seeded['mm']}/state", headers=auth(), json={"status": "bogus"}).status_code == 400
    client.put(f"/jobs/api/jobs/{seeded['pm']}/state", headers=auth(), json={"status": "hidden"})
    ids = {it["id"] for it in get(client, "/jobs/api/jobs")[1]["items"]}
    assert seeded["pm"] not in ids and seeded["pm"] in {it["id"] for it in get(client, "/jobs/api/jobs?status=hidden")[1]["items"]}


# ── companies ───────────────────────────────────────────────────────────────

def test_companies_list_filters_and_detail(seeded):
    client = seeded["client"]
    body = get(client, "/jobs/api/companies")[1]
    assert body["total"] == 2 and set(body["items"][0]) == COMPANY_KEYS
    first = body["items"][0]
    assert first["id"] == seeded["gem"] and first["open_jobs"] == 3 and first["matching_jobs"] == 2 and first["hidden_gem"]
    assert body["facets"]["status"] == {"active": 2, "ignored": 1}
    assert get(client, "/jobs/api/companies?status=ignored")[1]["items"][0]["entity_type"] == "directory"
    assert get(client, "/jobs/api/companies?source=manual")[1]["items"][0]["id"] == seeded["big"]
    assert get(client, "/jobs/api/companies?discovered_since=7")[1]["total"] == 1
    assert get(client, "/jobs/api/companies?sort=name")[1]["items"][0]["name"] == "Acme Plastics"
    assert get(client, "/jobs/api/companies?max_miles=10")[1]["total"] == 1
    assert get(client, "/jobs/api/companies?q=health")[1]["total"] == 1
    detail = get(client, f"/jobs/api/companies/{seeded['gem']}")[1]
    assert len(detail["jobs"]) == 2 and detail["facts"] == {"meta_description": "x"}


def test_company_add_and_patch(seeded, monkeypatch):
    calls = []
    monkeypatch.setattr(pipeline, "company_run", lambda run, company_id, fetcher=None: calls.append(company_id) or {})
    client = seeded["client"]
    resp = client.post("/jobs/api/companies", headers=auth(), json={"url": "https://www.NewCo.example/about"})
    body = resp.get_json()
    assert resp.status_code == 201 and body["company"]["domain"] == "newco.example" and body["run_id"]
    assert body["company"]["source"] == "manual" and set(body["company"]) == COMPANY_KEYS
    again = client.post("/jobs/api/companies", headers=auth(), json={"url": "newco.example"})
    assert again.status_code == 200 and again.get_json()["company"]["id"] == body["company"]["id"]
    assert client.post("/jobs/api/companies", headers=auth(), json={"url": "nope"}).status_code == 400
    patched = client.patch(f"/jobs/api/companies/{seeded['junk']}", headers=auth(),
                           json={"following": True, "notes": "hm", "status": "active"}).get_json()["company"]
    assert (patched["following"], patched["notes"], patched["status"]) == (True, "hm", "pending")
    assert client.patch(f"/jobs/api/companies/{seeded['gem']}", headers=auth(), json={"status": "bad"}).status_code == 400
    ignored = client.patch(f"/jobs/api/companies/{seeded['gem']}", headers=auth(), json={"status": "ignored"}).get_json()
    assert ignored["company"]["status"] == "ignored"


# ── runs ────────────────────────────────────────────────────────────────────

def test_runs_create_conflict_list_and_stream(seeded, monkeypatch):
    release = threading.Event()

    def fake_sweep(run, company_ids=None, limit=None):
        run.log("hello from fake sweep")
        run.progress(1, 2, "sweep")
        release.wait(5)
        return {"companies": 2, "jobs_new": 1}
    monkeypatch.setattr(sweep, "run_sweep", fake_sweep)
    client = seeded["client"]
    run_id = client.post("/jobs/api/runs", headers=auth(), json={"kind": "sweep"}).get_json()["run_id"]
    conflict = client.post("/jobs/api/runs", headers=auth(), json={"kind": "sweep"})
    assert conflict.status_code == 409 and conflict.get_json() == {"error": "already_running", "run_id": run_id}
    assert client.post("/jobs/api/runs", headers=auth(), json={"kind": "nope"}).status_code == 400
    assert client.post("/jobs/api/runs", headers=auth(), json={"kind": "company"}).status_code == 400
    release.set()
    stream = client.get(f"/jobs/api/runs/{run_id}/stream?_tok={token()}")
    assert stream.mimetype == "text/event-stream"
    events = [json.loads(line[6:]) for line in stream.get_data(as_text=True).splitlines() if line.startswith("data: ")]
    assert events[0] == {"type": "log", "line": "hello from fake sweep"}
    assert {"type": "progress", "done": 1, "total": 2, "phase": "sweep"} in events
    assert events[-1]["type"] == "done" and events[-1]["stats"]["jobs_new"] == 1
    run = get(client, f"/jobs/api/runs/{run_id}")[1]["run"]
    assert set(run) == {"id", "kind", "status", "started_at", "finished_at", "stats"} and run["status"] == "done"
    assert get(client, "/jobs/api/runs?limit=5")[1]["items"][0]["id"] == run_id
    runs._recent.clear()  # finished run no longer in memory → replay from the database
    replay = client.get(f"/jobs/api/runs/{run_id}/stream", headers=auth()).get_data(as_text=True)
    assert "hello from fake sweep" in replay and '"type": "done"' in replay


def test_status_and_discovery_plan(seeded):
    client = seeded["client"]
    status = get(client, "/jobs/api/status")[1]
    counts = status["counts"]
    assert counts["companies"] == 2 and counts["companies_by_status"]["ignored"] == 1 and counts["hidden_gems"] == 1
    assert counts["jobs_open"] == 4 and counts["jobs_matching"] == 3 and counts["unscored"] == 3  # ignored co. excluded
    assert status["queue"] == {"queued": 0, "leased": 0, "failed": 0} and status["instances"][0]["role"] == "jobscout"
    assert status["next_sweep_at"] is None  # on demand by default: nothing scheduled
    plan = get(client, "/jobs/api/discovery/plan?keywords=precast%20concrete&sources=maps&max_queries=5")[1]
    assert plan["total"] == 5 and plan["queries"][0] == {"source": "maps", "query": "precast concrete company",
                                                          "town": plan["queries"][0]["town"], "industry": None}
    assert {"name", "state", "miles"} == set(plan["towns"][0])


# ── worker ──────────────────────────────────────────────────────────────────

def test_worker_token_checks(app_env, monkeypatch):
    assert app_env.post("/jobs/worker/heartbeat", json={}).status_code == 401
    assert app_env.post("/jobs/worker/heartbeat", json={}, headers={"X-Worker-Token": "wrong"}).status_code == 401
    monkeypatch.delenv("JOBS_WORKER_TOKEN")
    assert app_env.post("/jobs/worker/heartbeat", json={}, headers=WORKER).status_code == 503


def test_worker_lease_flow_updates_fit(seeded):
    client = seeded["client"]
    client.put("/jobs/api/profile", headers=auth(),  # queues score_job for the open jobs in her field
               json={"name": "Ashley", "job_categories": ["marketing", "communications"]})
    hb = client.post("/jobs/worker/heartbeat", headers=WORKER, json={
        "worker_id": "wegter-pc", "instance": "wegter-pc", "model": "google/gemma-4-12b", "lmstudio_ok": True,
        "commit": "abc1234", "protocol": 1}).get_json()
    assert hb == {"ok": True, "protocol": 1, "queue": 3}  # the ignored company's job is not scored
    mismatch = client.post("/jobs/worker/claim", headers=WORKER, json={"worker_id": "wegter-pc", "max": 4, "protocol": 99})
    assert mismatch.status_code == 409 and mismatch.get_json() == {"error": "protocol_mismatch", "protocol": 1}
    claimed = client.post("/jobs/worker/claim", headers=WORKER, json={"worker_id": "wegter-pc", "kinds": ["score_job"],
                                                                      "max": 2, "protocol": 1}).get_json()
    assert claimed["protocol"] == 1 and len(claimed["tasks"]) == 2
    task = claimed["tasks"][0]
    assert task["kind"] == "score_job" and task["lease_until"] and set(task["payload"]) == {"job", "profile"}
    result = {"fit": 91, "role_family": "marketing", "seniority": "manager", "workplace": "hybrid", "salary_min": None,
              "salary_max": None, "salary_period": None, "tags": ["B2B"], "why": "Great fit.", "dealbreakers": ["commute"]}
    bad = client.post("/jobs/worker/complete", headers=WORKER, json={"task_id": task["id"], "result": {"fit": "x"}})
    assert bad.status_code == 400 and bad.get_json()["error"] == "invalid_result"
    ok = client.post("/jobs/worker/complete", headers=WORKER,
                     json={"task_id": task["id"], "worker_id": "wegter-pc", "result": result, "model": "gemma"})
    assert ok.get_json() == {"ok": True}
    assert client.post("/jobs/worker/fail", headers=WORKER,
                       json={"task_id": claimed["tasks"][1]["id"], "error": "timeout"}).get_json() == {"ok": True}
    assert client.post("/jobs/worker/fail", headers=WORKER, json={"task_id": 999}).status_code == 409
    items = get(client, "/jobs/api/jobs")[1]["items"]
    scored = [it for it in items if it["fit_source"] == "ai"]
    assert len(scored) == 1 and scored[0]["fit"] == 91 and scored[0]["why"] == "Great fit."
    detail = get(client, f"/jobs/api/jobs/{scored[0]['id']}")[1]
    assert detail["dealbreakers"] == ["commute"] and detail["seniority"] == "manager"
    status = get(client, "/jobs/api/status")[1]
    pc = next(i for i in status["instances"] if i["instance"] == "wegter-pc")
    assert pc["online"] and pc["lmstudio_ok"] and pc["tasks_done_today"] == 1 and pc["model"] == "google/gemma-4-12b"
    assert status["queue"]["queued"] == 2 and status["counts"]["unscored"] == 2


# ── find matches / activity (on demand) ─────────────────────────────────────

def test_activity_follows_the_run_and_the_ai(seeded, conn):
    client = seeded["client"]
    release = threading.Event()

    def slow(run, **_):
        run.log("step 1 of 3: looking for employers you haven't seen yet")
        run.progress(12, 60, "discover")
        release.wait(5)
        return {"new_companies": 2}

    running = runs.start("find", slow)
    try:
        for _ in range(50):
            if running.current:
                break
            threading.Event().wait(0.02)
        tasks.enqueue(conn, "score_job", job_id=seeded["mm"], profile_id=1)
        tasks.enqueue(conn, "enrich_company", company_id=seeded["gem"])
        conn.commit()
        act = get(client, "/jobs/api/status")[1]["activity"]
        assert act["run"]["kind"] == "find" and act["run"]["progress"] == {"done": 12, "total": 60, "phase": "discover"}
        assert act["run"]["last_line"].startswith("step 1 of 3")
        assert act["ai"]["queued"] == 2 and act["ai"]["total"] == 2
        assert act["ai"]["pending_by_kind"] == {"score_job": 1, "enrich_company": 1, "parse_page": 0}
        # a second heavy run is refused while this one works
        busy = client.post("/jobs/api/runs", headers=auth(), json={"kind": "sweep"})
        assert busy.status_code == 409 and busy.get_json() == {"error": "already_running", "run_id": running.id,
                                                             "kind": "find"}
    finally:
        release.set()
    for _ in range(100):
        if running.status != "running":
            break
        threading.Event().wait(0.02)
    conn.execute("UPDATE ai_tasks SET status='done', updated_at=?", (db.now_iso(),))
    conn.commit()
    act = get(client, "/jobs/api/status")[1]["activity"]
    assert act["run"] is None and act["last_run"]["id"] == running.id and act["last_run"]["stats"]["new_companies"] == 2
    assert act["ai"]["done"] == 2 and act["ai"]["queued"] == 0


def test_stop_button_cancels_the_running_find(seeded):
    client = seeded["client"]
    release = threading.Event()

    def stuck(run, **_):
        run.progress(39, 60, "discover")
        release.wait(5)
        run.progress(40, 60, "discover")

    running = runs.start("find", stuck)
    try:
        for _ in range(100):
            if running.current:
                break
            time.sleep(0.02)
        resp = client.post(f"/jobs/api/runs/{running.id}/cancel", headers=auth())
        assert resp.status_code == 200 and resp.get_json()["run"]["status"] == "cancelled"
        act = get(client, "/jobs/api/status")[1]["activity"]
        assert act["run"] is None and act["last_run"]["status"] == "cancelled"
        again = client.post(f"/jobs/api/runs/{running.id}/cancel", headers=auth())
        assert again.status_code == 409 and again.get_json()["error"] == "not_running"
        assert client.post("/jobs/api/runs/99999/cancel", headers=auth()).status_code == 404
        assert client.post(f"/jobs/api/runs/{running.id}/cancel").status_code == 401
    finally:
        release.set()


def test_changing_what_you_look_for_rerates_stored_jobs(seeded, conn):
    client = seeded["client"]
    welder = conn.execute("SELECT prefilter FROM jobs WHERE id=?", (seeded["welder"],)).fetchone()["prefilter"]
    assert welder == "fail"
    client.put("/jobs/api/profile", headers=auth(), json={"job_categories": ["trades"], "target_titles": []})
    assert conn.execute("SELECT prefilter FROM jobs WHERE id=?", (seeded["welder"],)).fetchone()["prefilter"] == "pass"
    items = get(client, "/jobs/api/jobs")[1]["items"]
    assert [it["title"] for it in items] == ["Welder"]  # her own list follows her own categories
    status = get(client, "/jobs/api/status")[1]
    assert status["looking_for"] == {"categories": ["Skilled Trades & Production"], "titles": [], "levels": []}


def test_find_is_a_run_kind(seeded, monkeypatch):
    monkeypatch.setattr(find, "run_find", lambda run, **kw: {"new_companies": 0})
    resp = seeded["client"].post("/jobs/api/runs", headers=auth(), json={"kind": "find", "options": {"skip_discovery": True}})
    assert resp.status_code == 200 and resp.get_json()["run_id"]


def test_profile_without_interests_queues_no_scores(seeded, conn):
    client = seeded["client"]
    client.put("/jobs/api/profile", headers=auth(), json={"name": "New person"})
    assert conn.execute("SELECT COUNT(*) FROM ai_tasks WHERE kind='score_job'").fetchone()[0] == 0
