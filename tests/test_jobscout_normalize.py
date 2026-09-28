"""Pure normalisation logic: salary, titles, rule score, workplace, locations, HTML, gazetteer."""
import pytest

from jobscout import geo
from jobscout.normalize import (content_hash, detect_workplace, html_to_text, is_local, make_salary,
                                parse_location, parse_salary, prefilter, rule_score, sanitize_html, title_matches,
                                title_tier)


@pytest.mark.parametrize("text,lo,hi,period", [
    ("$164,612 - $201,193", 164612, 201193, "year"),
    ("$120K–$150K", 120000, 150000, "year"),
    ("$55.00 - $70.00 per hour", 114400, 145600, "hour"),
    ("120,000 to 150,000 annually", 120000, 150000, "year"),
    ("Pay Range: $95,000.00 - $120,000.00/yr", 95000, 120000, "year"),
    ("$46.06 - $69.10 hourly", 95805, 143728, "hour"),
    ("$211.4K – $290.6K • Offers Equity", 211400, 290600, "year"),
    ("Salary: $120-150K", 120000, 150000, "year"),
    ("between $70,000 and $90,000 annually", 70000, 90000, "year"),
    ("Starting at $85,000 per year plus 10% bonus", 85000, 85000, "year"),
    ("The pay range for this role is $95,000 - $120,000. Eligible for a $2,000 sign-on bonus.", 95000, 120000, "year"),
    ("Hourly range: $22 - $28", 45760, 58240, "hour"),
    ("$4,000 - $6,000 per month", 48000, 72000, "year"),
])
def test_parse_salary(text, lo, hi, period):
    s = parse_salary(text)
    assert s is not None, text
    assert (s.min, s.max, s.period) == (lo, hi, period)


@pytest.mark.parametrize("text", [
    "401(k) match up to 6%; $5,000 signing bonus", "$12/hour", "$2,000,000 per year", "annual bonus target of $10,000",
    "tuition reimbursement up to $5,250 per year", "Call 651-555-0100 or visit 2024 events", "", None,
    "Up to 15% annual bonus", "Founded in 1972 with 240 employees",
])
def test_parse_salary_rejects(text):
    assert parse_salary(text) is None


def test_salary_text_keeps_original_snippet():
    assert parse_salary("Pay: $55.00 - $70.00 per hour, DOE").text == "$55.00 - $70.00 per hour"


def test_make_salary_structured():
    assert make_salary(211400, 290600, "1 YEAR").max == 290600
    hourly = make_salary(30, 40, "per-hour-wage")
    assert (hourly.min, hourly.max, hourly.period) == (62400, 83200, "hour")
    assert make_salary(None, None, "year") is None
    assert make_salary(5, 8, "HOUR") is None  # below $15/hr
    assert make_salary(90000, None, "YEAR").min == 90000


@pytest.mark.parametrize("title,tier,pf", [
    ("Data Center Vertical Marketing Manager", "manager", "pass"),
    ("Director of Marketing", "director", "pass"),
    ("VP, Brand", "exec", "pass"),
    ("Head of Communications", "exec", "pass"),
    ("Labor Communications Principal Consultant", "lead", "pass"),
    ("Senior Content Strategist", "lead", "pass"),
    ("Global Product Marketer", "ic", "maybe"),
    ("Events Coordinator", "ic", "maybe"),
    ("Senior Manager - Global Product Strategy", "manager", "maybe"),
    ("Director, Strategy & Business Development", "director", "maybe"),
    ("Manager, Investor Relations", "manager", "maybe"),
    ("Internship - 2027 Undergraduate Marketing Intern", "intern", "fail"),
    ("Marketing Co-op", "intern", "fail"),
    ("Procurement Director - Global 3rd Party Materials", "director", "fail"),
    ("Master Data Management Operations Mgr", "manager", "fail"),
    ("Social Worker", "ic", "fail"),
    ("Digital Success Engineer", "ic", "fail"),
    ("Staffing Coordinator", "ic", "fail"),
    ("Strategic Account Manager", "manager", "fail"),
    ("Communications Technician", "ic", "fail"),
    ("Senior Manager, IT Digital Commercial Excellence", "manager", "fail"),
    ("Sr. Director, Digital Leader – Consumer Business Group", "director", "pass"),
])
def test_title_tier_and_prefilter(title, tier, pf):
    assert title_tier(title) == tier
    assert prefilter(title, tier) == pf


def test_title_matches_fuzzy():
    targets = ["Marketing Director", "Communications Manager"]
    assert title_matches("Director of Marketing", targets)
    assert title_matches("Sr. Marketing Communications Mgr", targets)
    assert not title_matches("Finance Director", targets)


def test_rule_score_contract_math():
    profile = {"target_titles": ["Marketing Manager"], "workplace_pref": ["hybrid"], "salary_floor": 100000,
               "industries_want": ["mfg_plastics_packaging"], "industries_avoid": ["healthcare"]}
    job = {"title": "Marketing Manager", "workplace": "hybrid", "salary_min": 95000, "salary_max": 120000}
    # manager 32 + function 30 + target 10 + workplace 5 + salary ≥ floor 10 + industry want 5
    assert rule_score(job, profile, "mfg_plastics_packaging") == 92
    assert rule_score(job, profile, "healthcare") == 62          # − 25 avoid, no want bonus
    unknown_salary = dict(job, salary_min=None, salary_max=None)
    assert rule_score(unknown_salary, profile, None) == 82      # unknown salary +5
    low = dict(job, salary_min=60000, salary_max=70000)
    assert rule_score(low, profile, None) == 77                 # below floor: +0
    assert rule_score({"title": "Procurement Director"}, profile, None) <= 15  # prefilter fail cap
    assert rule_score({"title": "Marketing Manager", "workplace": "unknown"}, None, None) == 67


@pytest.mark.parametrize("args,expected", [
    (("On-site",), "onsite"), (("Hybrid",), "hybrid"), (("ORA_REMOTE",), "remote"), (("OnSite",), "onsite"),
    ((None, "Marketing Manager (Remote)"), "remote"),
    ((None, "Manager", "Remote - US"), "remote"),
    ((None, "Manager", "Oakdale, MN", "Monday-Friday: business hours \nHyrbid work schedule."), "hybrid"),
    ((None, "Manager", "", "This is not a remote position."), "onsite"),
    ((None, "Manager", "", "This role is fully remote within the US."), "remote"),
    ((None, "Manager", "", "Work on-site at our Oakdale plant."), "onsite"),
    ((None, "Manager", "", "Great team."), "unknown"),
])
def test_detect_workplace(args, expected):
    assert detect_workplace(*args) == expected


@pytest.mark.parametrize("text,city,state,country,local", [
    ("US, Minnesota, Maplewood", "Maplewood", "MN", "US", True),
    ("Bloomington, MN, United States", "Bloomington", "MN", "US", True),
    ("Maplewood, MN 55144", "Maplewood", "MN", "US", True),
    ("Remote - US", None, None, "US", True),
    ("3 Locations", None, None, None, True),
    ("IN, Bangalore Kar", "Bangalore Kar", None, "IN", False),
    ("Remote - California", None, "CA", "US", False),
    ("Austin, TX; Eau Claire, WI; Minneapolis, MN", "Eau Claire", "WI", "US", True),
    ("Poland - Remote", None, None, "PL", False),
    ("Saint Paul, MN", "Saint Paul", "MN", "US", True),
    ("Hudson, Wisconsin", "Hudson", "WI", "US", True),
    ("New York, NY (HQ)", "New York", "NY", "US", False),
    ("Brno, Czechia", "Brno", None, "CZ", False),
])
def test_parse_location(text, city, state, country, local):
    loc = parse_location(text)
    assert (loc.city, loc.state, loc.country) == (city, state, country)
    assert is_local(loc) is local


def test_parse_location_flags():
    assert parse_location("3 Locations").multi
    assert parse_location("Remote - US").remote
    assert parse_location("Maplewood, MN 55144").zip == "55144"


def test_sanitize_html_allowlist():
    dirty = ('<div class="x"><h1 style="c">Title</h1><p onclick="x()">Hi <span>there</span> <b>bold</b></p>'
             '<script>alert(1)</script><a href="javascript:bad()">bad</a><a href="https://x.com/a" class="y">ok</a>'
             '<table><tr><td>cell</td></tr></table><img src="x.png"><p>&nbsp;</p><ul><li>one</li></ul></div>')
    out = sanitize_html(dirty)
    assert "<script" not in out and "onclick" not in out and "style" not in out and "<img" not in out
    assert "<h2>Title</h2>" in out and "<b>bold</b>" in out and "<li>one</li>" in out
    assert '<a href="https://x.com/a" rel="noopener" target="_blank">ok</a>' in out
    assert "javascript" not in out and "<p>cell</p>" in out and "<p>\xa0</p>" not in out


def test_html_to_text():
    text = html_to_text("<p>One</p><ul><li>Two</li><li>Three</li></ul><br>Four&nbsp;five")
    assert text.split("\n")[0] == "One" and "Two" in text and "Four five" in text


def test_gazetteer_and_distance():
    lat, lng = geo.lookup_city("Birchwood Village", "MN")
    assert abs(lat - 45.06) < 0.02 and abs(lng + 92.98) < 0.02
    assert geo.lookup_city("St. Paul", "MN") == geo.lookup_city("Saint Paul", "MN")
    assert geo.find_city("Hudson")[2] in ("MN", "WI")
    assert geo.find_city("River Falls", "WI")[2] == "WI"
    assert geo.lookup_city("Tel Aviv") is None
    miles = geo.haversine_miles(45.0619, -92.9766, *geo.lookup_city("Maplewood", "MN"))
    assert 3 < miles < 7


def test_content_hash_stable():
    assert content_hash("a", None, 1) == content_hash("a", None, 1)
    assert content_hash("a", "b") != content_hash("ab", "")
