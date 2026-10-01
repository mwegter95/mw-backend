"""Queue, scoring, enrichment/categorisation, discovery planning/recording and the sweep flow."""
from datetime import datetime, timedelta, timezone

import pytest

from conftest import FakeFetcher, fixture_json, fixture_text, insert_company, insert_job
from jobscout import ai, ai_schemas, db, discovery, enrich, scoring, sweep, tasks, views
from jobscout.ats.base import Adapter, RawJob

SCORE = {"fit": 88, "role_family": "marketing", "seniority": "manager", "workplace": "hybrid", "salary_min": None,
         "salary_max": None, "salary_period": None, "tags": ["B2B", "brand"], "why": "Strong B2B brand role.",
         "dealbreakers": []}
ENRICH = {"industry": "mfg_plastics_packaging", "sub_industry": "Custom injection molding", "products": ["housings"],
          "summary": "Family-owned molder.", "business_model": "b2b", "ownership": "family", "parent_company": None,
          "employee_band": "200-999", "founded_year": 1972, "well_known": False, "hq_city": "Oakdale",
          "hq_state": "MN", "tags": ["ISO 9001"], "entity_type": "company", "local_presence": "hq"}


def _profile(conn, user_id=1, **extra):
    body = {"target_titles": ["Marketing Manager"], "salary_floor": 100000, "home_lat": 45.0619, "home_lng": -92.9766,
            **extra}
    return views.save_profile(conn, {"id": user_id, "display_name": "Ashley"}, body, geocode=lambda a: None)


# ── queue ───────────────────────────────────────────────────────────────────

def test_enqueue_dedupes_active_tasks(conn):
    cid = insert_company(conn)
    assert tasks.enqueue(conn, "enrich_company", company_id=cid)
    assert tasks.enqueue(conn, "enrich_company", company_id=cid) is None
    conn.execute("UPDATE ai_tasks SET status='done'")
    assert tasks.enqueue(conn, "enrich_company", company_id=cid)


def test_claim_priority_lease_and_payloads(conn):
    cid = insert_company(conn, facts=db.dumps({"homepage_title": "Acme", "about_text": "We mold plastic."}))
    jid = insert_job(conn, cid, ats_job_id="j1")
    profile = _profile(conn)  # enqueues score_job for the open pass job
    tasks.enqueue(conn, "enrich_company", company_id=cid)
    conn.commit()
    claimed = tasks.claim(conn, "pc", ["score_job", "enrich_company", "parse_page"], 4)
    assert [t["kind"] for t in claimed] == ["score_job", "enrich_company"]
    score = claimed[0]["payload"]
    assert set(score) == {"job", "profile"} and score["job"]["title"] == "Marketing Manager"
    assert score["profile"]["target_titles"] == ["Marketing Manager"] and score["job"]["salary_text"]
    assert claimed[1]["payload"]["about_text"].startswith("We mold plastic.")
    rows = conn.execute("SELECT status, lease_until, worker_id, payload_hash FROM ai_tasks ORDER BY id").fetchall()
    assert all(r["status"] == "leased" and r["worker_id"] == "pc" and r["lease_until"] for r in rows)
    assert tasks.claim(conn, "pc", None, 4) == []
    tasks.complete(conn, claimed[0]["id"], "pc", SCORE, "gemma")
    fit = scoring.resolve_fit(dict(conn.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()),
                              dict(conn.execute("SELECT * FROM job_scores").fetchone()),
                              views.get_profile(conn, profile["user_id"]), None)
    assert (fit["fit"], fit["fit_source"], fit["tags"]) == (88, "ai", ["B2B", "brand"])


def test_invalid_result_counts_as_failed_attempt(conn):
    cid = insert_company(conn)
    tid = tasks.enqueue(conn, "enrich_company", company_id=cid)
    conn.commit()
    for attempt in range(1, 4):
        tasks.claim(conn, "pc", ["enrich_company"], 1)
        with pytest.raises(tasks.TaskError) as err:
            tasks.complete(conn, tid, "pc", {"nope": 1})
        assert err.value.code == "invalid_result"
        row = conn.execute("SELECT status, attempts FROM ai_tasks WHERE id=?", (tid,)).fetchone()
        assert row["attempts"] == attempt
    assert row["status"] == "failed"
    assert conn.execute("SELECT enrich_status FROM companies WHERE id=?", (cid,)).fetchone()[0] == "failed"
    with pytest.raises(tasks.TaskError) as err:
        tasks.complete(conn, tid, "pc", ENRICH)
    assert err.value.status == 409


def test_reaper_requeues_expired_leases(conn):
    cid = insert_company(conn)
    tid = tasks.enqueue(conn, "enrich_company", company_id=cid)
    conn.commit()
    tasks.claim(conn, "pc", None, 1)
    past = db.now_iso(datetime.now(timezone.utc) - timedelta(minutes=1))
    conn.execute("UPDATE ai_tasks SET lease_until=? WHERE id=?", (past, tid))
    assert tasks.reap(conn) == 1
    row = conn.execute("SELECT status, attempts, lease_until FROM ai_tasks WHERE id=?", (tid,)).fetchone()
    assert (row["status"], row["attempts"], row["lease_until"]) == ("queued", 1, None)
    assert tasks.counts(conn) == {"queued": 1, "leased": 0, "failed": 0}


def test_ai_score_fills_unknown_salary_and_workplace(conn):
    cid = insert_company(conn)
    jid = insert_job(conn, cid, ats_job_id="j1", salary_min=None, salary_max=None, salary_text=None,
                     salary_period=None, workplace="unknown")
    _profile(conn)
    task = tasks.claim(conn, "pc", ["score_job"], 1)[0]
    tasks.complete(conn, task["id"], "pc", {**SCORE, "salary_min": 50, "salary_max": 60, "salary_period": "hour"}, "m")
    job = conn.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
    assert (job["salary_min"], job["salary_period"], job["workplace"]) == (104000, "hour", "hybrid")


def test_profile_change_requeues_scores(conn):
    cid = insert_company(conn)
    insert_job(conn, cid, ats_job_id="j1")
    _profile(conn)
    task = tasks.claim(conn, "pc", ["score_job"], 1)[0]
    tasks.complete(conn, task["id"], "pc", SCORE, "m")
    assert scoring.enqueue_scores(conn) == 0
    _profile(conn, want_text="B2B manufacturers only")
    assert tasks.counts(conn)["queued"] == 1


def test_schemas_strict_and_validation_clamps():
    for schema in ai_schemas.SCHEMAS.values():
        assert schema["additionalProperties"] is False and set(schema["required"]) == set(schema["properties"])
    assert "entity_type" in ai_schemas.ENRICH_COMPANY["required"]
    cleaned = ai_schemas.validate("score_job", {**SCORE, "fit": 140, "tags": ["x" * 40] * 9, "role_family": "bogus"})
    assert cleaned["fit"] == 100 and len(cleaned["tags"]) == 1 and len(cleaned["tags"][0]) == 24
    assert cleaned["role_family"] == "other"
    e = ai_schemas.validate("enrich_company", {**ENRICH, "industry": "nope", "entity_type": "weird"})
    assert (e["industry"], e["entity_type"]) == ("other", "company")
    p = ai_schemas.validate("parse_page", {"jobs": [{"title": "Brand Manager", "workplace": "sometimes"}, {"x": 1}]})
    assert p["jobs"] == [{"title": "Brand Manager", "location_text": None, "url": None, "workplace": None,
                          "salary_text": None, "summary": None}]


def test_ai_prompt_order_and_json_parsing():
    payload = {"job": {"title": "Brand Manager"}, "profile": {"resume_text": "10 yrs"}}
    msgs = ai.build_messages("score_job", payload)
    assert msgs[0]["role"] == "system" and msgs[1]["content"].index("PROFILE") < msgs[1]["content"].index("JOB")
    assert ai.parse_json_content('```json\n{"fit": 3}\n```') == {"fit": 3}
    assert ai.parse_json_content('Sure! {"fit": 4} hope that helps') == {"fit": 4}
    assert "mfg_plastics_packaging" in ai.build_messages("enrich_company", {"name": "x"})[0]["content"]


# ── categorisation / enrichment ─────────────────────────────────────────────

def _facts(name, domain="northstarmolding.example"):
    facts = enrich.page_facts(fixture_text(name), f"https://{domain}/", domain)
    facts.pop("_about_link")
    facts["hq_hint"] = enrich.hq_hint(facts)
    return facts


def test_heuristics_on_molder_homepage():
    facts = _facts("home_molder.html")
    assert facts["hq_hint"] == "Oakdale, MN" and facts["jsonld_org"]["numberOfEmployees"] == 240
    assert facts["logo_url"] == "https://northstarmolding.example/favicon-32.png"
    assert any(h.startswith("1200 Industrial Blvd") for h in facts["address_hints"])
    company = {"domain": "northstarmolding.example", "lat": None}
    fields = enrich.categorize(company, facts, fetcher=FakeFetcher())
    assert fields["industry"] == "mfg_plastics_packaging" and fields["entity_type"] == "company"
    assert (fields["hq_city"], fields["hq_state"], fields["geo_precision"]) == ("Oakdale", "MN", "city")
    assert fields["local_presence"] == "hq" and fields["ownership"] == "family"
    assert (fields["employee_band"], fields["founded_year"]) == ("200-999", 1972)
    assert fields["enrich_source"] == "heuristic" and fields["summary"].startswith("Family-owned")


@pytest.mark.parametrize("fixture,domain,entity", [
    ("home_directory.html", "plasticsdirectory.example", "directory"),
    ("home_news.html", "localbiznews.example", "news"),
    ("home_franchise.html", "fastsigns.example", "franchise_location"),
])
def test_entity_type_guesses_and_auto_ignore(fixture, domain, entity):
    facts = _facts(fixture, domain)
    fields = enrich.categorize({"domain": domain, "lat": None}, facts, fetcher=FakeFetcher())
    assert fields["entity_type"] == entity
    # A keyword guess alone doesn't ignore the company (it's often wrong); the AI's verdict does.
    assert enrich.auto_ignore({**fields, "status": "pending"}) == {}
    assert enrich.auto_ignore({**fields, "enrich_source": "ai", "status": "pending"})["status"] == "ignored"


def test_entity_type_for_public_bodies():
    assert enrich.guess_entity_type("ci.oakdale.mn.us", {}) == "government"
    assert enrich.guess_entity_type("isd622.org", {"homepage_title": "North St. Paul-Maplewood-Oakdale School District"}) == "school"


def test_apply_enrichment_gem_and_restore_rules(conn):
    cid = insert_company(conn, status="pending", enrich_source="heuristic", employee_band="unknown", ownership=None)
    fields = enrich.apply_enrichment(conn, cid, ai_schemas.validate("enrich_company", ENRICH))
    assert fields["enrich_source"] == "ai" and fields["hidden_gem"] == 1 and fields["gem_score"] == 100
    row = conn.execute("SELECT * FROM companies WHERE id=?", (cid,)).fetchone()
    assert row["well_known"] == 0 and db.loads(row["products"], []) == ["housings"]
    enrich.apply_enrichment(conn, cid, ai_schemas.validate("enrich_company", {**ENRICH, "entity_type": "directory"}))
    assert conn.execute("SELECT status FROM companies WHERE id=?", (cid,)).fetchone()[0] == "ignored"


def test_compute_gem_rules():
    home = [(45.0619, -92.9766)]
    base = {"lat": 44.96, "lng": -92.96, "well_known": 0, "ownership": "family", "employee_band": "200-999",
            "industry": "professional_services", "entity_type": "company", "local_presence": "hq",
            "enrich_source": "ai"}
    assert enrich.compute_gem(base, home) == (100, 1)
    # any industry: the same company as a software firm, a manufacturer or a health system scores the same
    for industry in ("tech_software", "mfg_other", "healthcare", "financial_insurance"):
        assert enrich.compute_gem({**base, "industry": industry}, home) == (100, 1)
    # keywords alone can't judge size or fame, so no badge until the AI has categorized the company
    assert enrich.compute_gem({**base, "enrich_source": "heuristic"}, home) == (100, 0)
    # a 12-person shop can be local and unknown, but it isn't an established employer
    assert enrich.compute_gem({**base, "employee_band": "1-49"}, home) == (75, 0)
    assert enrich.compute_gem({**base, "employee_band": "unknown"}, home) == (75, 0)
    assert enrich.compute_gem({**base, "local_presence": "branch"}, home)[1] == 0
    assert enrich.compute_gem({**base, "well_known": 1}, home) == (75, 0)
    far = enrich.compute_gem({**base, "lat": 40.0, "lng": -100.0}, home)
    assert far == (65, 0)
    assert enrich.compute_gem({**base, "well_known": None, "ownership": "subsidiary", "employee_band": "1000-4999"},
                              home) == (62, 0)


def _page_facts(title, meta="", home="", category=None):
    return {"homepage_title": title, "meta_description": meta, "home_text": home,
            "discovery": {"category": category} if category else {}}


def test_heuristic_industry_needs_manufacturing_evidence():
    # Installers and contractors mention products they don't make (first live OSM run: two exteriors
    # contractors came out as building-materials manufacturers and hidden-gem candidates).
    roofer = _page_facts("Hampton Exteriors | Roofing & Siding", "Roof replacement, siding and window installation. "
                    "Free estimates. Licensed and insured.")
    assert enrich.heuristic_industry(roofer) == "construction_real_estate"
    maker = _page_facts("Lakeside Precast", "We manufacture precast concrete products for builders across Minnesota.")
    assert enrich.heuristic_industry(maker) == "mfg_building_materials"
    osm_works = _page_facts("North Star Molding", "Custom injection molding", category="works")
    assert enrich.heuristic_industry(osm_works) == "mfg_plastics_packaging"


def test_heuristic_industry_generic_phrases_dont_decide():
    vineyard = _page_facts("7 Vines Vineyard", "Nestled in the city of Dellwood, MN, our winery and vineyard...")
    assert enrich.heuristic_industry(vineyard) == "mfg_food_beverage"
    paving = _page_facts("Doctor Asphalt", "Premium asphalt paving and sealcoating for homes, churches and schools.")
    assert enrich.heuristic_industry(paving) == "construction_real_estate"


def test_article_pages_and_non_local_sites_are_not_employers():
    article = _page_facts("Types of Injection: Understanding Uses and Injection Sites", "Learn about intramuscular...")
    assert enrich.guess_entity_type("healthsite.example", article) == "news"
    national = _page_facts("Acme Molding | Custom molder", "Serving customers nationwide", "Call 800-555-0100")
    assert enrich.guess_local_presence("company", None, None, national) == "none"
    assert enrich.auto_ignore({"entity_type": "company", "local_presence": "none", "status": "pending"})["status"] == "ignored"
    local = _page_facts("Mold Craft Inc", "Custom injection molder in Willernie, MN", "Call (651) 555-0100")
    assert enrich.guess_local_presence("company", None, None, local) == "unknown"


def test_keywords_search_as_businesses():
    assert discovery.business_phrase("injection molding") == "injection molding company"
    assert discovery.business_phrase("millwork manufacturer") == "millwork manufacturer"
    assert discovery.business_phrase("precast concrete suppliers") == "precast concrete suppliers"


def test_parse_maps_card_from_results_feed():
    card = {"name": "Taurus Engineering And Manufacturing", "website": "http://taurusengineering.net/",
            "href": "https://www.google.com/maps/place/Taurus/data=!4m7!3m6!1s0x0:0x1!8m2!3d45.0412!4d-93.0507!16s",
            "text": "Taurus Engineering And Manufacturing Taurus Engineering And Manufacturing 4.9(10) Plastic injection "
                    "molding service · 1375 Willow Lake Blvd Closed · Opens 7 AM Mon · (651) 484-9292 Website Directions"}
    c = discovery.parse_maps_card(card)
    assert c["category"] == "Plastic injection molding service" and c["address"] == "1375 Willow Lake Blvd"
    assert (c["lat"], c["lng"], c["phone"]) == (45.0412, -93.0507, "(651) 484-9292")
    assert discovery.parse_maps_card({**card, "website": ""}) is None  # no website → nothing to categorize


# ── discovery ───────────────────────────────────────────────────────────────

def test_plan_keywords_first_spread_and_capped():
    plan = discovery.build_plan(["mfg_plastics_packaging", "mfg_building_materials"], ["precast concrete"],
                                ["maps", "search", "osm", "places"], 20, 45.0619, -92.9766, 35)
    assert plan["total"] == 20 == len(plan["queries"])
    assert plan["queries"][0]["source"] == "osm"
    assert plan["queries"][1]["query"] == "precast concrete company" and {q["source"] for q in plan["queries"]} == {"osm", "maps", "search"}
    names = [t["name"] for t in plan["towns"]]
    assert names[0] == "Mahtomedi" and "Hudson" in names and "Chaska" not in names  # Chaska is > 35 mi
    assert all(t["miles"] <= 35 for t in plan["towns"])
    first_round = [q["town"] for q in plan["queries"][1::2]][:5]
    assert len(set(first_round)) == 5  # each term gets a different town
    small = discovery.build_plan([], ["injection molding"], ["maps"], 60, 45.0619, -92.9766, 10)
    assert all(q["query"] == "injection molding company" for q in small["queries"]) and small["total"] == len(small["towns"])


def test_plan_places_needs_key(monkeypatch):
    monkeypatch.setenv("GOOGLE_PLACES_API_KEY", "k")
    plan = discovery.build_plan(["mfg_other"], [], ["places"], 5, 45.0619, -92.9766, 35)
    assert {q["source"] for q in plan["queries"]} == {"places"}


def test_normalize_and_filter_domains():
    assert discovery.normalize_domain("https://www.Acme-Plastics.com/about?x=1") == "acme-plastics.com"
    assert discovery.normalize_domain("not a url") is None
    assert discovery.filter_reason("yelp.com") == "aggregator"
    assert discovery.filter_reason("homedepot.com") == "national_brand"
    assert discovery.filter_reason("ci.oakdale.mn.us") == "government"
    assert discovery.filter_reason("oakdale.gov") == "government"
    assert discovery.filter_reason("northstarmolding.com") is None


def test_parse_osm_and_places():
    osm = discovery.parse_osm(fixture_json("overpass.json"))
    assert [c["name"] for c in osm] == ["North Star Molding", "Lakeside Precast", "Yelp listing", "City Hall"]
    assert osm[0]["industry"] == "mfg_plastics_packaging" and osm[1]["industry"] == "mfg_building_materials"
    assert osm[1]["lat"] == 45.05
    q = {"query": "building materials manufacturer", "town": "Oakdale, MN", "industry": "mfg_building_materials"}
    places = discovery.parse_places(fixture_json("places.json"), q)
    assert places[0]["city"] == "Oakdale" and places[0]["state"] == "MN" and places[0]["lat"] == 44.98


def test_record_candidates_dedupes_filters_and_annotates(conn):
    insert_company(conn, domain="lakesideprecast.example", name="Lakeside Precast")
    stats = {"candidates": 0, "new_companies": 0, "known": 0, "filtered": 0, "by_source": {}}
    cands = [dict(c, query="industrial", town="within 35 mi") for c in discovery.parse_osm(fixture_json("overpass.json"))]
    cands.append(dict(cands[0], source="maps"))  # same domain from another source
    new_ids = discovery.record_candidates(conn, cands, stats, set())
    assert len(new_ids) == 1 and stats == {"candidates": 5, "new_companies": 1, "known": 1, "filtered": 2,
                                           "by_source": {"osm": 4, "maps": 1}}
    row = conn.execute("SELECT * FROM companies WHERE id=?", (new_ids[0],)).fetchone()
    assert (row["source"], row["status"], row["enrich_status"], row["geo_precision"]) == ("discovery", "pending", "pending", "address")
    assert row["discovered_via"].startswith('osm: "industrial"') and row["hq_city"] == "Oakdale"
    assert db.loads(row["facts"], {})["_query_industry"] == "mfg_plastics_packaging"
    known = conn.execute("SELECT facts FROM companies WHERE domain='lakesideprecast.example'").fetchone()
    assert db.loads(known["facts"], {})["discovered_via_all"]


def test_run_discovery_offline_sources_fail_soft(data_dir, monkeypatch):
    from jobscout import runs
    monkeypatch.setattr(discovery, "browser_search", lambda queries, cb, **kw: "Playwright browser not installed")
    monkeypatch.setattr(discovery, "osm_discover", lambda *a, **k: [dict(c) for c in discovery.parse_osm(fixture_json("overpass.json"))])
    fetcher = FakeFetcher([("northstarmolding.example", fixture_text("home_molder.html"))])
    run = runs.run_inline("discover", discovery.run_discovery, options={"max_queries": 5}, fetcher=fetcher,
                          start_pipeline=False)
    assert run.stats["new_companies"] == 2 and run.stats["filtered"] == 2 and run.stats["by_source"]["osm"] == 4
    with db.session() as c:
        molder = c.execute("SELECT * FROM companies WHERE domain='northstarmolding.example'").fetchone()
        assert (molder["industry"], molder["enrich_status"], molder["entity_type"]) == ("mfg_plastics_packaging", "queued", "company")
        assert tasks.counts(c)["queued"] == 1  # the unreachable site gets no AI task
    assert any("maps/search skipped" in e.get("line", "") for e in run.events)


def test_import_seeds_tolerant_upsert(conn, tmp_path):
    seeds = [{"name": "3M", "domain": "https://www.3M.com/", "hq_city": "Maplewood", "hq_state": "MN",
              "industry": "mfg_other", "employee_band": "5000+", "ownership": "public", "well_known": True,
              "summary": "Science company.", "careers_url": "https://3m.wd1.myworkdayjobs.com/Search", "extra": 1},
             {"name": "Bad Enum Co", "domain": "badenum.example", "industry": "rockets", "employee_band": "huge"},
             {"domain": "noname.example"}]
    assert discovery.import_seeds(seeds, "t") == {"inserted": 2, "updated": 0, "skipped": 1}
    row = conn.execute("SELECT * FROM companies WHERE domain='3m.com'").fetchone()
    assert (row["ats_type"], row["ats_site"], row["enrich_source"], row["enrich_status"]) == ("workday", "Search", "seed", "done")
    bad = conn.execute("SELECT * FROM companies WHERE domain='badenum.example'").fetchone()
    assert (bad["industry"], bad["employee_band"]) == ("other", "unknown")
    conn.execute("UPDATE companies SET summary='user edit' WHERE domain='3m.com'")
    conn.commit()
    assert discovery.import_seeds([dict(seeds[0], summary="seed v2")], "t")["updated"] == 0
    assert conn.execute("SELECT summary FROM companies WHERE domain='3m.com'").fetchone()[0] == "user edit"


# ── sweep flow ──────────────────────────────────────────────────────────────

class FakeAdapter(Adapter):
    has_detail = True

    def __init__(self, jobs):
        super().__init__(FakeFetcher())
        self.jobs, self.detail_calls = jobs, 0

    def list_jobs(self, company, keywords):
        return [RawJob(**j) for j in self.jobs]

    def get_detail(self, company, raw):
        self.detail_calls += 1
        raw.description_html = "<p>Salary: $100,000 - $130,000 per year. Hybrid schedule.</p>"
        raw.detailed = True
        return raw


def test_upsert_details_only_when_needed_and_close_after_two_misses(conn):
    conn.execute("""INSERT INTO profiles(user_id, job_categories) VALUES(1, '["marketing"]')""")
    cid = insert_company(conn)
    company = dict(conn.execute("SELECT * FROM companies WHERE id=?", (cid,)).fetchone())
    listing = [{"ats_job_id": "a", "title": "Brand Manager", "location_text": "Oakdale, MN"},
               {"ats_job_id": "b", "title": "Welder", "location_text": "Oakdale, MN"},
               {"ats_job_id": "c", "title": "Marketing Director", "location_text": "Dallas, TX"}]
    adapter = FakeAdapter(listing)
    stats, seen = sweep.upsert_jobs(conn, company, adapter.list_jobs(company, []), adapter)
    assert (stats["jobs_new"], stats["matching_new"], adapter.detail_calls, seen) == (2, 1, 1, {"a", "b"})
    a = conn.execute("SELECT * FROM jobs WHERE ats_job_id='a'").fetchone()
    assert (a["salary_min"], a["workplace"], a["prefilter"], a["lat"]) == (100000, "hybrid", "pass", pytest.approx(44.97, abs=0.05))
    stats, _ = sweep.upsert_jobs(conn, company, adapter.list_jobs(company, []), adapter)
    assert adapter.detail_calls == 1 and stats["jobs_new"] == 0  # unchanged → no second detail fetch
    now = db.now_iso()
    sweep.close_missing(conn, cid, {"b"}, now)
    assert conn.execute("SELECT closed_at, missed_sweeps FROM jobs WHERE ats_job_id='a'").fetchone()[:] == (None, 1)
    sweep.close_missing(conn, cid, {"b"}, now)
    assert conn.execute("SELECT closed_at FROM jobs WHERE ats_job_id='a'").fetchone()[0] == now
    sweep.upsert_jobs(conn, company, adapter.list_jobs(company, []), adapter)  # back again → reopened
    assert conn.execute("SELECT closed_at, missed_sweeps FROM jobs WHERE ats_job_id='a'").fetchone()[:] == (None, 0)


def test_apply_parsed_page_jobs(conn):
    cid = insert_company(conn, ats_type="html", careers_url="https://acme.com/careers",
                         facts=db.dumps({"_careers_url": "https://acme.com/careers"}))
    sweep.apply_parsed_jobs(conn, cid, {"jobs": [{"title": "Marketing Manager", "location_text": "Oakdale, MN",
                                                  "url": "/careers/mm", "workplace": "hybrid",
                                                  "salary_text": "$90,000 - $110,000", "summary": "Lead marketing."}]})
    job = conn.execute("SELECT * FROM jobs WHERE company_id=?", (cid,)).fetchone()
    assert (job["source"], job["url"], job["salary_max"], job["workplace"]) == ("page_ai", "https://acme.com/careers/mm", 110000, "hybrid")


def test_scheduler_is_on_demand_by_default(data_dir, monkeypatch):
    from jobscout import runs, scheduler
    started = []
    monkeypatch.setattr(runs, "start", lambda kind, target, **kw: started.append(kind) or runs.Run(len(started), kind, kind))
    monkeypatch.setenv("JOBS_SWEEP_HOUR", "2")
    scheduler._last_enrich = None
    scheduler.tick(datetime(2026, 10, 1, 2, 30).astimezone())  # sweep hour, 1st of the month
    assert started == []


def test_scheduler_nightly_once_per_day_and_monthly_discovery(data_dir, monkeypatch):
    from jobscout import runs, scheduler
    started = []
    monkeypatch.setattr(runs, "start", lambda kind, target, **kw: started.append(kind) or runs.Run(len(started), kind, kind))
    monkeypatch.setenv("JOBS_SWEEP_HOUR", "2")
    monkeypatch.setenv("JOBS_AUTO_RUNS", "1")
    scheduler._last_enrich = None
    local = datetime(2026, 10, 1, 2, 30).astimezone()
    scheduler.tick(local)
    scheduler.tick(local + timedelta(minutes=1))
    assert started.count("sweep") == 1
    scheduler.tick(local + timedelta(hours=1))
    assert started.count("discover") == 1  # the 1st of the month, once the sweep has been started
    scheduler.tick(datetime(2026, 10, 1, 14, 0).astimezone())
    assert started.count("sweep") == 1 and started.count("discover") == 1
    assert scheduler.next_sweep_at(datetime(2026, 10, 1, 1, 0).astimezone()) == db.now_iso(
        datetime(2026, 10, 1, 2, 0).astimezone())


def test_city_state_from_addresses():
    assert discovery._city_state("500 Hadley Ave N, Oakdale, MN 55128, USA") == ("Oakdale", "MN")
    assert discovery._city_state("1200 Industrial Blvd, Stillwater, MN") == ("Stillwater", "MN")
    assert discovery._city_state("") == (None, None)


def test_needs_detection():
    fresh = db.now_iso()
    assert sweep.needs_detection({"status": "pending", "ats_detected_at": fresh})
    assert not sweep.needs_detection({"status": "no_careers", "ats_type": None, "ats_detected_at": fresh})
    assert sweep.needs_detection({"status": "active", "ats_type": "workday", "ats_detected_at": None})
    old = db.now_iso(datetime.now(timezone.utc) - timedelta(days=31))
    assert sweep.needs_detection({"status": "no_ats", "ats_detected_at": old})


def test_redirected_domain_is_not_a_directory_and_ai_can_unignore(conn):
    html = "<html><title>Welcome to Plastech</title>" + "".join(
        f'<a href="https://plastech.example/p{i}">x</a>' for i in range(30)) + "</html>"
    facts = enrich.page_facts(html, "https://plastech.example/", "jdproducts.example")
    assert facts["external_link_ratio"] == 0.0 and enrich.guess_entity_type("jdproducts.example", facts) == "company"
    cid = insert_company(conn, status="ignored", status_reason="not an employer site", entity_type="directory")
    enrich.apply_enrichment(conn, cid, ai_schemas.validate("enrich_company", ENRICH))
    assert conn.execute("SELECT status FROM companies WHERE id=?", (cid,)).fetchone()[0] == "pending"
    user = insert_company(conn, domain="u.example", status="ignored", status_reason="ignored by user")
    enrich.apply_enrichment(conn, user, ai_schemas.validate("enrich_company", ENRICH))
    assert conn.execute("SELECT status FROM companies WHERE id=?", (user,)).fetchone()[0] == "ignored"


class _FakeLM:
    """Stands in for requests.Session against LM Studio: returns the queued responses in order."""

    def __init__(self, *choices):
        self.choices, self.bodies = list(choices), []

    def post(self, url, json=None, timeout=None, headers=None):  # noqa: A002 — requests' signature
        self.bodies.append({**json, "_timeout": timeout})
        content, finish, reasoning = self.choices.pop(0)

        class R:
            def raise_for_status(self):
                pass

            def json(self):
                return {"model": "gemma", "choices": [{"message": {"content": content}, "finish_reason": finish}],
                        "usage": {"completion_tokens_details": {"reasoning_tokens": reasoning}}}
        return R()


def test_ai_retries_an_answer_cut_off_by_thinking():
    lm = _FakeLM(('{"summary": "Makes wi', "length", 1480), ('{"summary": "Makes widgets"}', "stop", 2100))
    result, model = ai.chat_json("enrich_company", {"name": "x"}, base="http://lm", model="gemma", session=lm)
    assert result == {"summary": "Makes widgets"} and model == "gemma"
    assert [b["max_tokens"] for b in lm.bodies] == [ai.MAX_TOKENS["enrich_company"], ai.RETRY_MAX_TOKENS]
    assert lm.bodies[1]["_timeout"] >= ai.RETRY_TIMEOUT


def test_ai_gives_a_clear_error_when_thinking_uses_every_token():
    lm = _FakeLM(("", "length", 1500), ("<think>still going", "length", 6144))
    with pytest.raises(ai.AnswerCutOff) as err:
        ai.chat_json("enrich_company", {"name": "x"}, base="http://lm", model="gemma", session=lm)
    assert "no answer within 6144 tokens (6144 spent thinking)" in str(err.value)
    assert "Reasoning" in str(err.value)
    lm = _FakeLM(('{"a": "b', "length", 1400), ('{"a": "b', "length", 6000))
    with pytest.raises(ai.AnswerCutOff, match="cut off at 6144 tokens"):
        ai.chat_json("score_job", {"profile": {}, "job": {}}, base="http://lm", model="gemma", session=lm)


def test_requeue_cut_off_retries_only_token_budget_failures(conn):
    cid = conn.execute("INSERT INTO companies(name, domain, status, enrich_status) VALUES('A', 'a.example', 'active', "
                       "'failed')").lastrowid
    cut = tasks.enqueue(conn, "enrich_company", company_id=cid)
    other = tasks.enqueue(conn, "enrich_company", company_id=cid + 999)
    for tid, err in ((cut, "JSONDecodeError: Unterminated string starting at: line 9"), (other, "HTTPError: 500")):
        conn.execute("UPDATE ai_tasks SET status='failed', attempts=3, error=? WHERE id=?", (err, tid))
    conn.commit()
    assert tasks.requeue_cut_off(conn) == 1
    rows = {r["id"]: r for r in conn.execute("SELECT id, status, attempts, error FROM ai_tasks")}
    assert (rows[cut]["status"], rows[cut]["attempts"], rows[cut]["error"]) == ("queued", 0, None)
    assert rows[other]["status"] == "failed"
    assert conn.execute("SELECT enrich_status FROM companies WHERE id=?", (cid,)).fetchone()[0] == "queued"
    assert tasks.requeue_cut_off(conn) == 0
