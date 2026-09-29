"""What a profile is looking for: job categories, target titles and levels (jobscout/interests.py)."""
import pytest

from jobscout import interests
from jobscout.interests import Interests
from jobscout.normalize import title_tier

LEADERSHIP = ["exec", "director", "manager", "lead"]
MARKETING_COMMS = Interests.build(["marketing", "communications"], [], LEADERSHIP)


@pytest.mark.parametrize("title,tier,pf", [
    ("Data Center Vertical Marketing Manager", "manager", "pass"),
    ("Director of Marketing", "director", "pass"),
    ("VP, Brand", "exec", "pass"),
    ("Head of Communications", "exec", "pass"),
    ("Labor Communications Principal Consultant", "lead", "pass"),
    ("Senior Content Strategist", "lead", "pass"),
    ("Sr. Director, Digital Leader – Consumer Business Group", "director", "pass"),
    ("Global Product Marketer", "ic", "maybe"),        # the field, but not a level they asked for
    ("Events Coordinator", "ic", "maybe"),
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
    ("Director, Strategy & Business Development", "director", "fail"),
])
def test_marketing_and_communications(title, tier, pf):
    assert title_tier(title) == tier
    assert MARKETING_COMMS.match(title, tier) == pf


@pytest.mark.parametrize("categories,title,expected", [
    (["sales"], "Strategic Account Manager", "pass"),
    (["sales"], "Director, Strategy & Business Development", "pass"),
    (["software"], "Senior Software Engineer", "pass"),
    (["software"], "Full Stack Developer", "pass"),
    (["it"], "IT Manager", "pass"),
    (["it"], "Make It Happen Coordinator", "fail"),       # the word "it" isn't IT
    (["finance"], "Controller", "pass"),
    (["finance"], "Accounts Payable Specialist", "pass"),
    (["hr"], "Talent Acquisition Partner", "pass"),
    (["operations"], "Supply Chain Planner", "pass"),
    (["engineering"], "Manufacturing Engineer", "pass"),
    (["healthcare"], "RN - Med/Surg", "pass"),
    (["design"], "Senior UX Designer", "pass"),
    (["data"], "Data Analyst II", "pass"),
    (["project"], "Program Manager", "pass"),
    (["finance"], "Marketing Manager", "fail"),
])
def test_other_fields(categories, title, expected):
    assert Interests.build(categories).match(title) == expected


def test_target_titles_work_without_categories():
    wanted = Interests.build([], ["Supply Chain Manager", "Director of Sustainability"])
    assert wanted.match("Director, Supply Chain") == "pass"
    assert wanted.match("Supply Chain Analyst") == "pass"          # no levels given: any level
    assert wanted.match("Sustainability Program Lead") == "pass"
    assert wanted.match("Payroll Manager") == "fail"
    leveled = Interests.build([], ["Supply Chain Manager"], ["manager", "director"])
    assert leveled.match("Supply Chain Analyst") == "maybe"


def test_empty_interests_match_nothing():
    nothing = Interests.build()
    assert nothing.empty and nothing.match("Marketing Director") == "fail" and nothing.search_terms() == []


def test_search_terms_for_big_boards():
    terms = Interests.build(["marketing", "finance"], ["Director of Sustainability"]).search_terms()
    assert terms[:2] == ["marketing", "brand"] and "finance" in terms and "sustainability" in terms


def test_combined_takes_everyones_interests(conn):
    conn.execute("INSERT INTO profiles(user_id, job_categories, target_titles, seniority) VALUES(1, ?, ?, ?)",
                 ('["marketing"]', '[]', '["director"]'))
    conn.execute("INSERT INTO profiles(user_id, job_categories, target_titles, seniority) VALUES(2, ?, ?, ?)",
                 ('[]', '["Controller"]', '[]'))
    conn.commit()
    both = interests.combined(conn)
    assert both.match("Marketing Coordinator") == "pass"   # one profile set no levels, so any level passes
    assert both.match("Controller") == "pass" and both.match("Welder") == "fail"
