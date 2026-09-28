"""ATS detection and adapters against recorded (live-captured, trimmed) fixtures — no network."""
from datetime import date

import pytest

from conftest import FakeFetcher, fixture_json, fixture_text
from jobscout import ats, browser, careers
from jobscout.ats.workday import location_from_path, posted_on_to_iso
from jobscout.http import Blocked
from jobscout.sweep import normalize_job

GUID = "0c7d1a9e-1111-2222-3333-444455556666"
COMPANY = {"id": 1, "name": "Acme", "domain": "acme.com", "industry": "mfg_plastics_packaging", "hq_city": "Oakdale",
           "lat": 44.96, "lng": -92.96, "geo_precision": "city"}


@pytest.mark.parametrize("url,ats_type,key,site", [
    ("https://3m.wd1.myworkdayjobs.com/en-US/Search", "workday", "3m", "Search"),
    ("https://3m.wd1.myworkdayjobs.com/wday/cxs/3m/Search/jobs", "workday", "3m", "Search"),
    ("https://wd5.myworkdaysite.com/en-US/recruiting/acme/External", "workday", "acme", "External"),
    ("https://fa-etnv-saasfaprod1.fa.ocs.oraclecloud.com/hcmUI/CandidateExperience/en/sites/healthpartners/jobs",
     "oracle", "fa-etnv-saasfaprod1", "healthpartners"),
    ("https://job-boards.greenhouse.io/jamf", "greenhouse", "jamf", None),
    ("https://boards.greenhouse.io/embed/job_board?for=jamf", "greenhouse", "jamf", None),
    ("https://boards-api.greenhouse.io/v1/boards/jamf/jobs", "greenhouse", "jamf", None),
    ("https://jobs.lever.co/spotify/abc-123", "lever", "spotify", None),
    ("https://jobs.ashbyhq.com/ramp", "ashby", "ramp", None),
    ("https://jobs.smartrecruiters.com/BoschGroup/123", "smartrecruiters", "BoschGroup", None),
    ("https://careers.smartrecruiters.com/Equinox", "smartrecruiters", "Equinox", None),
    ("https://collectivemeasures.bamboohr.com/careers/214", "bamboohr", "collectivemeasures", None),
    ("https://careers-acme.icims.com/jobs/1234/job", "icims", "careers-acme", None),
    (f"https://recruiting.ultipro.com/ACM1000ACME/JobBoard/{GUID}/?q=", "ukg", "ACM1000ACME", GUID),
    (f"https://workforcenow.adp.com/mascsr/default/mdf/recruitment/recruitment.html?cid={GUID}&ccId=1", "adp", GUID, None),
    ("https://myjobs.adp.com/acmecareers/cx", "adp", "acmecareers", None),
    (f"https://recruiting.paylocity.com/Recruiting/Jobs/All/{GUID}/Acme", "paylocity", GUID, None),
    ("https://www.paycomonline.net/v4/ats/web.php/jobs?clientkey=ABC123DEF", "paycom", "ABC123DEF", None),
    ("https://jobs.dayforcehcm.com/en-US/acme/CANDIDATEPORTAL", "dayforce", "acme", "CANDIDATEPORTAL"),
    ("https://career4.successfactors.com/career?company=acmecorp", "successfactors", "acmecorp", None),
    ("https://acme.taleo.net/careersection/2/jobsearch.ftl", "taleo", "acme", "2"),
    ("https://jobs.jobvite.com/acme/jobs", "jobvite", "acme", None),
    ("https://apply.workable.com/acme/", "workable", "acme", None),
    ("https://acme.applytojob.com/apply", "jazzhr", "acme", None),
    ("https://ats.rippling.com/acme/jobs", "rippling", "acme", None),
    ("https://acme.recruitee.com/o/marketing", "recruitee", "acme", None),
    ("https://acme.breezy.hr/p/123", "breezy", "acme", None),
])
def test_detect_from_url(url, ats_type, key, site):
    found = ats.detect_from_url(url)
    assert found and (found["ats_type"], found["ats_key"], found["ats_site"]) == (ats_type, key, site)


@pytest.mark.parametrize("url", ["https://acme.com/careers", "https://www.bamboohr.com/pricing",
                                 "https://www.icims.com/", "https://resources.workable.com/tips", None])
def test_detect_from_url_negative(url):
    assert ats.detect_from_url(url) is None


def test_detect_from_html_variants():
    gh = ats.detect_from_html(fixture_text("careers_greenhouse.html"), "https://acmeplastics.com/careers")
    assert (gh["ats_type"], gh["ats_key"], gh["careers_url"]) == ("greenhouse", "acmeplastics",
                                                                   "https://job-boards.greenhouse.io/acmeplastics")
    wd = ats.detect_from_html(fixture_text("careers_phenom_workday.html"), "https://careers.bigco.com/")
    assert (wd["ats_type"], wd["ats_host"], wd["ats_site"]) == ("workday", "bigco.wd5.myworkdayjobs.com", "BigCoCareers")
    js = ats.detect_from_html(fixture_text("careers_jsonld.html"), "https://birchbp.com/careers")
    assert js["ats_type"] == "jsonld"
    phenom_only = "<html><script src='https://cdn.phenompeople.com/x.js'></script></html>"
    assert ats.detect_from_html(phenom_only, "https://jobs.x.com")["ats_type"] == "phenom"
    assert ats.detect_from_html("<p>nothing</p>", "https://x.com") is None


def test_registry():
    for t in ("workday", "oracle", "greenhouse", "lever", "ashby", "smartrecruiters", "bamboohr", "jsonld", "breezy",
              "recruitee"):
        assert ats.has_adapter(t)
    with pytest.raises(NotImplementedError, match="adapter pending: icims"):
        ats.get_adapter("icims")


# ── Workday ──────────────────────────────────────────────────────────────────

def test_workday_helpers():
    assert location_from_path("/job/US-Minnesota-Maplewood/Title_R1") == ("Maplewood", "MN", "US")
    assert location_from_path("/job/US-North-Dakota-Fargo/X") == ("Fargo", "ND", "US")
    today = date(2026, 9, 27)
    assert posted_on_to_iso("Posted Today", today) == "2026-09-27T00:00:00Z"
    assert posted_on_to_iso("Posted Yesterday", today) == "2026-09-26T00:00:00Z"
    assert posted_on_to_iso("Posted 30+ Days Ago", today) == "2026-08-28T00:00:00Z"


def _workday_fetcher():
    def search(_url, payload):
        if not payload["appliedFacets"]:
            return fixture_json("workday_list_world.json")
        return fixture_json("workday_list_us.json") if payload["offset"] == 0 else {"total": 0, "jobPostings": []}
    return FakeFetcher([("/Search/jobs", search), ("/wday/cxs/3m/Search/job/", fixture_json("workday_detail.json"))])


def test_workday_list_restarts_with_us_facet_and_details():
    fetcher = _workday_fetcher()
    company = {**COMPANY, "ats_host": "3m.wd1.myworkdayjobs.com", "ats_key": "3m", "ats_site": "Search"}
    adapter = ats.get_adapter("workday", fetcher)
    raws = adapter.list_jobs(company, ["marketing"])
    assert len(raws) == 8
    applied = [p for u, p in fetcher.calls if p and p["appliedFacets"]]
    assert applied[0]["appliedFacets"] == {"Location_Country": ["bc33aa3152ec42d4995f4791a106ed09"]}
    multi = next(r for r in raws if r.location_text == "3 Locations")
    assert (multi.city, multi.state) == ("Maplewood", "MN")  # from externalPath
    assert multi.url.startswith("https://3m.wd1.myworkdayjobs.com/Search/job/")
    raw = adapter.get_detail(company, multi)
    assert raw.detailed and raw.posted_at == "2026-09-01T00:00:00Z" and raw.employment_type == "Full time"
    job = normalize_job(raw, company)
    assert (job["city"], job["state"], job["workplace"]) == ("Maplewood", "MN", "onsite")
    assert (job["salary_min"], job["salary_max"], job["salary_period"]) == (164612, 201193, "year")
    assert job["prefilter"] == "pass" and job["lat"] is not None


# ── Oracle ───────────────────────────────────────────────────────────────────

def test_oracle_list_and_detail():
    fetcher = FakeFetcher([
        ("recruitingCEJobRequisitions?", lambda u, p: fixture_json("oracle_list.json") if "offset=0" in u
         else {"items": [{"requisitionList": []}]}),
        ("recruitingCEJobRequisitionDetails", fixture_json("oracle_detail.json")),
    ])
    company = {**COMPANY, "ats_host": "fa-etnv-saasfaprod1.fa.ocs.oraclecloud.com", "ats_site": "healthpartners"}
    adapter = ats.get_adapter("oracle", fetcher)
    raws = adapter.list_jobs(company, ["marketing"])
    assert len(raws) == 6 and "siteNumber=healthpartners,keyword=marketing" in fetcher.calls[0][0]
    assert raws[0].url.endswith("/sites/healthpartners/job/" + raws[0].ats_job_id)
    raw = adapter.get_detail(company, raws[0])
    assert raw.salary_text == "$46.06 - $69.10 hourly" and raw.extra["lat"] == pytest.approx(44.82135)
    job = normalize_job(raw, company)
    assert job["workplace"] == "hybrid" and job["salary_period"] == "hour" and job["salary_min"] == 95805
    assert "Pay Range" in job["description_html"]


# ── Greenhouse / Lever / Ashby / SmartRecruiters / BambooHR ─────────────────

def test_greenhouse():
    fetcher = FakeFetcher([("pay_transparency=true", fixture_json("greenhouse_detail.json")),
                           ("/boards/jamf/jobs?content=true", fixture_json("greenhouse_jobs.json"))])
    company = {**COMPANY, "ats_key": "jamf"}
    adapter = ats.get_adapter("greenhouse", fetcher)
    raws = adapter.list_jobs(company, [])
    assert raws and all(r.description_html and "&lt;" not in r.description_html for r in raws)
    kept = {r.location_text for r in raws if normalize_job(r, company)}
    assert "Minneapolis, MN" in kept and "US Remote" in kept
    assert "Tel Aviv" not in kept and "Bangalore, India" not in kept
    brand = next(r for r in raws if "Brand" in r.title)
    detailed = adapter.get_detail(company, brand)
    assert (detailed.salary_min, detailed.salary_max) == (58100, 133200)


def test_lever_with_salary_range():
    postings = fixture_json("lever_postings.json")
    postings[0].update(salaryRange={"min": 90000, "max": 110000, "currency": "USD", "interval": "per-year-salary"},
                       categories={"location": "Minneapolis, MN", "allLocations": ["Minneapolis, MN"]})
    adapter = ats.get_adapter("lever", FakeFetcher([("api.lever.co/v0/postings/spotify", postings)]))
    raws = adapter.list_jobs({**COMPANY, "ats_key": "spotify", "ats_host": "jobs.lever.co"}, [])
    assert len(raws) == 3 and raws[0].apply_url.endswith("/apply") and raws[0].detailed
    job = normalize_job(raws[0], COMPANY)
    assert (job["salary_min"], job["salary_max"], job["state"]) == (90000, 110000, "MN")
    assert job["workplace"] in ("onsite", "hybrid", "remote")


def test_ashby_compensation():
    adapter = ats.get_adapter("ashby", FakeFetcher([("job-board/ramp", fixture_json("ashby_board.json"))]))
    raws = adapter.list_jobs({**COMPANY, "ats_key": "ramp"}, [])
    first = raws[0]
    assert (first.salary_min, first.salary_max) == (211400, 290600) and first.workplace == "Hybrid"
    assert "Remote (US)" in first.location_text


def test_smartrecruiters():
    fetcher = FakeFetcher([("/postings/744000148454651", fixture_json("smartrecruiters_detail.json")),
                           ("/postings?limit=100", fixture_json("smartrecruiters_list.json"))])
    adapter = ats.get_adapter("smartrecruiters", fetcher)
    company = {**COMPANY, "ats_key": "smartrecruiters"}
    raws = adapter.list_jobs(company, ["marketing"])
    assert len(raws) == 1 and raws[0].workplace == "remote" and raws[0].country == "PL"
    raw = adapter.get_detail(company, raws[0])
    assert raw.url.startswith("https://jobs.smartrecruiters.com/") and "Job Description" in raw.description_html
    assert normalize_job(raw, company) is None  # Poland


def test_bamboohr():
    fetcher = FakeFetcher([("/careers/214/detail", fixture_json("bamboohr_detail.json")),
                           ("/careers/list", fixture_json("bamboohr_list.json"))])
    adapter = ats.get_adapter("bamboohr", fetcher)
    company = {**COMPANY, "ats_key": "collectivemeasures"}
    raws = adapter.list_jobs(company, [])
    assert len(raws) == 4 and raws[0].location_text == "Minneapolis, Minnesota" and raws[0].workplace == "hybrid"
    raw = adapter.get_detail(company, raws[0])
    assert raw.posted_at == "2023-07-25T00:00:00Z" and "Collective Measures" in raw.description_html


# ── JSON-LD ──────────────────────────────────────────────────────────────────

def test_jsonld_postings_on_careers_page():
    fetcher = FakeFetcher([("birchbp.com/careers", fixture_text("careers_jsonld.html"))])
    adapter = ats.get_adapter("jsonld", fetcher)
    company = {**COMPANY, "careers_url": "https://birchbp.com/careers"}
    raws = adapter.list_jobs(company, [])
    mm, content = raws
    assert mm.ats_job_id == "BBP-101" and mm.extra["lat"] == 44.963 and mm.employment_type == "FULL_TIME"
    job = normalize_job(mm, company)
    assert (job["salary_min"], job["salary_max"], job["workplace"], job["city"]) == (95000, 120000, "hybrid", "Oakdale")
    assert "<strong>brand</strong>" in job["description_html"]
    remote = normalize_job(content, company)
    assert remote["workplace"] == "remote" and remote["salary_period"] == "hour" and remote["salary_min"] == 67600


def test_jsonld_link_mode_and_detail():
    fetcher = FakeFetcher([("/careers/jobs/communications-director", fixture_text("job_page_jsonld.html")),
                           ("lakesideprecast.com/careers", fixture_text("careers_links.html"))])
    adapter = ats.get_adapter("jsonld", fetcher)
    company = {**COMPANY, "careers_url": "https://lakesideprecast.com/careers"}
    raws = adapter.list_jobs(company, [])
    assert [r.title for r in raws] == ["Communications Director", "CDL Driver - Flatbed"]
    detail = adapter.get_detail(company, raws[0])
    job = normalize_job(detail, company)
    assert detail.ats_job_id == raws[0].ats_job_id and job["city"] == "Stillwater"
    assert (job["salary_min"], job["workplace"]) == (110000, "onsite")


# ── careers page detection ──────────────────────────────────────────────────

def test_careers_detect_via_homepage_link():
    fetcher = FakeFetcher([("acmeplastics.com/careers", fixture_text("careers_greenhouse.html")),
                           ("acmeplastics.com", fixture_text("home_molder.html"))])
    fields = careers.detect({"domain": "acmeplastics.com", "homepage_url": "https://acmeplastics.com"}, fetcher)
    assert (fields["status"], fields["ats_type"], fields["ats_key"]) == ("active", "greenhouse", "acmeplastics")


def test_careers_detect_known_ats_url_needs_no_fetch():
    fetcher = FakeFetcher()
    fields = careers.detect({"domain": "3m.com", "careers_url": "https://3m.wd1.myworkdayjobs.com/Search"}, fetcher)
    assert fields["ats_type"] == "workday" and fields["status"] == "active" and fetcher.calls == []


def test_careers_detect_no_adapter_html_and_none():
    # A careers system without an adapter is still swept: its board page goes to the AI reader.
    icims_page = '<html><title>Careers</title><a href="https://careers-acme.icims.com/jobs">Search jobs</a></html>'
    fields = careers.detect({"domain": "acme.com"}, FakeFetcher([("acme.com", icims_page)]))
    assert (fields["status"], fields["ats_type"]) == ("active", "icims")
    assert fields["status_reason"] == "icims board read by AI (no adapter yet)"
    plain = "<html><head><title>Careers at Acme</title></head><body>" + "<p>We are hiring great people.</p>" * 80
    fields = careers.detect({"domain": "acme.com"}, FakeFetcher([("acme.com/careers", plain), ("acme.com", "<a>x</a>")]))
    assert (fields["status"], fields["ats_type"]) == ("active", "html")
    fields = careers.detect({"domain": "acme.com"}, FakeFetcher([("acme.com", "<html><title>Home</title></html>")]))
    assert fields["status"] == "no_careers"


def test_careers_detect_blocked(monkeypatch):
    fetcher = FakeFetcher([("acme.com", Blocked("https://acme.com", "HTTP 403", 403))])
    monkeypatch.setattr(browser, "fetch_rendered", lambda url: None)
    monkeypatch.setattr(browser, "available", lambda: False)
    assert careers.detect({"domain": "acme.com"}, fetcher)["status"] == "manual_check"
    monkeypatch.setattr(browser, "available", lambda: True)
    assert careers.detect({"domain": "acme.com"}, fetcher)["status"] == "blocked"
    rendered = {"html": fixture_text("careers_greenhouse.html"), "final_url": "https://acme.com/careers",
                "links": [], "iframes": [], "scripts": [], "requests": []}
    monkeypatch.setattr(browser, "fetch_rendered", lambda url: rendered)
    assert careers.detect({"domain": "acme.com"}, fetcher)["ats_type"] == "greenhouse"


def test_breezy_list_and_detail():
    fetcher = FakeFetcher([("breezy.hr/json", fixture_json("breezy_list.json")),
                           ("breezy.hr/p/", fixture_text("breezy_posting.html"))])
    adapter = ats.get_adapter("breezy", fetcher)
    company = {**COMPANY, "ats_key": "breezy"}
    raws = adapter.list_jobs(company, [])
    assert len(raws) == 3 and raws[0].state == "FL" and raws[0].salary_text.startswith("$0.05")
    assert normalize_job(raws[0], company) is None  # Florida
    detail = adapter.get_detail(company, raws[0])
    assert "Roses and Rainbows" in detail.description_html


def test_recruitee_documented_shape():
    adapter = ats.get_adapter("recruitee", FakeFetcher([("/api/offers/", fixture_json("recruitee_offers_synthetic.json"))]))
    raw = adapter.list_jobs({**COMPANY, "ats_key": "acme"}, [])[0]
    job = normalize_job(raw, COMPANY)
    assert (job["state"], job["workplace"], job["salary_min"], job["salary_max"]) == ("WI", "hybrid", 85000, 100000)
    assert raw.apply_url.endswith("/c/new") and "<li>5 years</li>" in job["description_html"]


def test_careers_probe_skips_dead_hosts():
    from jobscout.http import FetchError
    fetcher = FakeFetcher([("acme.com", FetchError("https://acme.com", "SSLError"))])
    assert careers.detect({"domain": "acme.com"}, fetcher)["status"] == "no_careers"
    assert len([u for u, _ in fetcher.calls if "acme.com" in u]) == 1


def test_paylocity_board_and_detail():
    fetcher = FakeFetcher([("/Jobs/All/", fixture_text("paylocity_board.html")),
                           ("/Jobs/Details/", fixture_text("paylocity_detail.html"))])
    adapter = ats.get_adapter("paylocity", fetcher)
    company = {**COMPANY, "ats_key": "34ff8fcc-4d38-4a99-928b-fa2508914580"}
    raws = adapter.list_jobs(company, [])
    assert raws and raws[0].title == "Account Development Manager"
    assert raws[0].location_text is None  # "Headquarters" is an internal site name, not a place
    detailed = adapter.get_detail(company, raws[0])
    assert (detailed.salary_min, detailed.salary_max) == (50000, 65000)
    assert detailed.location_text.startswith("Bloomington") and detailed.detailed


def test_paylocity_page_data_ignores_trailing_script():
    from jobscout.ats.paylocity import page_data
    assert page_data('<script>window.pageData = {"Jobs": []};\nvar x = {"a": 1};</script>') == {"Jobs": []}
    assert page_data("<html>no data</html>") == {}


def test_workable_list_and_detail():
    fetcher = FakeFetcher([("/api/v3/accounts/midwest-special-services/jobs", fixture_json("workable_list.json")),
                           ("/api/v2/accounts/midwest-special-services/jobs/", fixture_json("workable_detail.json"))])
    adapter = ats.get_adapter("workable", fetcher)
    company = {**COMPANY, "ats_key": "midwest-special-services"}
    raws = adapter.list_jobs(company, [])
    assert len(raws) == 2 and raws[0].workplace == "onsite" and raws[0].state == "Minnesota"
    assert raws[0].url == f"https://apply.workable.com/midwest-special-services/j/{raws[0].ats_job_id}/"
    detailed = adapter.get_detail(company, raws[0])
    assert "<h3>Requirements</h3>" in detailed.description_html and detailed.detailed


def test_jobs_beyond_commuting_distance_are_dropped():
    from jobscout.ats.base import RawJob
    area = ([(45.0619, -92.9766)], 60)  # Birchwood Village, 60 mi
    company = {**COMPANY, "lat": None, "lng": None}
    milwaukee = RawJob(ats_job_id="1", title="Product Manager", location_text="Milwaukee, WI", workplace="Hybrid")
    assert normalize_job(milwaukee, company) is not None      # MN/WI is enough without an area
    assert normalize_job(milwaukee, company, area) is None      # 284 mi away: not commutable
    remote = RawJob(ats_job_id="2", title="Marketing Manager", location_text="Milwaukee, WI", workplace="Remote")
    assert normalize_job(remote, company, area) is not None     # remote roles stay
    woodbury = RawJob(ats_job_id="3", title="Marketing Manager", location_text="Woodbury, MN", workplace="On-site")
    assert normalize_job(woodbury, company, area) is not None
