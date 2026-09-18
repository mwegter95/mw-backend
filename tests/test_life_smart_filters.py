"""Filter + dedup regression tests for the smart-reminder engine.

These are built from a real 80-reminder generation that was almost all noise:
healthcare appointments, restaurant bookings, recurring bills, volunteer
meetings and awareness days. Each KEEP/DROP entry below is an event that
actually appeared. Run with: python -m pytest tests/test_life_smart_filters.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import life_smart as L


# Events that must still produce reminders.
KEEP = [
    "Aubrey's Wedding", "Nils and Julia Wedding",
    "Jeremy Storvick's Birthday", "Miles' Birthday", "Jason's Birthday",
    "Mom's Birthday", "PT's Birthday", "Ethan Boote's Birthday", "Dan's Birthday",
    "Thanksgiving", "Christmas Eve", "Christmas Day", "Halloween",
    "New Year's Eve", "New Year's Day", "Easter",
    "Rwanda Safari Trip", "Flight to Denver", "Renew passport",
    "Ben's play", "Run for the Apples",
]

# Events that must not.
DROP = {
    "E McCallum follow up visit": "healthcare",
    "S Bolstad sleep followup": "healthcare",
    "Ethan's virtual visit": "healthcare",
    "Dr. Akram appointment": "healthcare",
    "Dr. Ali appointment": "healthcare",
    "Dentist appointment": "healthcare",
    "The remedy appointment": "healthcare",
    "Hair appointment": "healthcare",
    "House cleaning appointment": "healthcare",
    "Family dinner at Nightingale": "meal",
    "Kincaid's reservation for two": "meal",
    "Olio Bayport reservation": "meal",
    "Dinner before MN Orch show": "meal",
    "Chili gathering dinner": "meal",
    "Community meal volunteering": "meal",
    "Haircut": "routine-service",
    "Subaru drop off at Lamotte": "routine-service",
    "Oil change": "routine-service",
    "BVCC meeting": "work-routine",
    "Roads open house": "work-routine",
    "Submit Turnberry hours": "work-routine",
    "Pay period tasks": "work-routine",
    "Weekly standup": "work-routine",
    "1:1 with manager": "work-routine",
    "Pay house loan": "recurring-bill",
    "Pay house down payment loan": "recurring-bill",
    "Pay Brianne for phone": "recurring-bill",
}


def test_wanted_events_survive():
    kept = [t for t in KEEP if L.exclusion_reason(t) is None]
    assert kept == KEEP, f"false drops: {sorted(set(KEEP) - set(kept))}"


def test_noise_is_excluded_with_the_right_reason():
    for title, expected in DROP.items():
        assert L.exclusion_reason(title) == expected, title


def test_major_holiday_beats_a_meal_word():
    # "Christmas dinner" is a holiday whose task legitimately says dinner.
    for t in ["Thanksgiving dinner", "Christmas Eve dinner", "Easter brunch",
              "Valentine's Day dinner"]:
        assert L.exclusion_reason(t) is None, t
    assert L.exclusion_reason("Dinner at Nightingale") == "meal"


def test_trusted_categories_skip_the_task_title_recheck():
    assert L.task_exclusion_reason("Plan Thanksgiving dinner", "holiday") is None
    assert L.task_exclusion_reason("Buy Mom a birthday gift", "birthday") is None
    # ...but an untrusted category gets its invented title checked
    assert L.task_exclusion_reason("Confirm family dinner at Nightingale", "social") == "meal"
    assert L.task_exclusion_reason("Plan dinner before MN Orch show", "social") == "meal"


def test_retired_categories_are_rejected():
    assert "appointment" not in L.LEAD
    assert "generic" not in L.LEAD
    events = {"1": {"id": "1", "title": "x", "date": "2026-10-01"}}
    data = {"items": [
        {"eventId": "1", "category": "appointment",
         "tasks": [{"kind": "confirm", "title": "Confirm appointment"}]},
        {"eventId": "1", "category": "generic",
         "tasks": [{"kind": "prep", "title": "Prep for TFTs"}]},
    ]}
    assert L.resolve_items(data, events, "2026-09-18") == []


def test_only_major_holidays_resolve():
    events = {
        "a": {"id": "a", "title": "World Mental Health Day", "date": "2026-10-10"},
        "b": {"id": "b", "title": "Columbus Day", "date": "2026-10-12"},
        "c": {"id": "c", "title": "Fall Fest", "date": "2026-10-22"},
        "d": {"id": "d", "title": "Thanksgiving", "date": "2026-11-26"},
    }
    data = {"items": [
        {"eventId": k, "category": "holiday",
         "tasks": [{"kind": "plan", "title": f"Plan for {v['title']}"}]}
        for k, v in events.items()
    ]}
    titles = [r["title"] for r in L.resolve_items(data, events, "2026-09-18")]
    assert titles == ["Plan for Thanksgiving"]


def test_dedupe_keeps_the_soonest_of_a_repeated_task():
    # A recurring appointment generated one reminder per occurrence.
    tasks = [
        {"title": "Confirm Ethan's virtual visit", "date": d, "sourceEventId": str(i)}
        for i, d in enumerate(["2026-10-27", "2026-11-08", "2026-11-22",
                               "2026-12-06", "2026-12-20"])
    ]
    out = L.dedupe_tasks(tasks)
    assert len(out) == 1
    assert out[0]["date"] == "2026-10-27"


def test_dedupe_collapses_near_identical_titles_for_one_event():
    tasks = [
        {"title": "Plan trip to White Christmas show", "date": "2026-12-09", "sourceEventId": "w"},
        {"title": "Plan for White Christmas show", "date": "2026-12-09", "sourceEventId": "w"},
    ]
    assert len(L.dedupe_tasks(tasks)) == 1


def test_dedupe_keeps_genuinely_different_tasks():
    tasks = [
        {"title": "Buy Mom a birthday gift", "date": "2026-12-10", "sourceEventId": "m"},
        {"title": "Send Mom a birthday message", "date": "2026-12-17", "sourceEventId": "m"},
        {"title": "Pack for Rwanda Safari", "date": "2026-07-09", "sourceEventId": "r"},
    ]
    assert len(L.dedupe_tasks(tasks)) == 3


def test_volume_is_capped_to_something_readable():
    assert L.MAX_TASKS_TOTAL <= 30
