"""
Smart-task engine for the Life Dashboard.

Robustness model: the AI (GPT-5.4 mini) only does what small models do well —
classify each calendar event into a closed category and phrase 0-2 short task
titles from a closed set of "kinds". Everything mechanical is owned by THIS
code, not the model:

  • reminder dates      → computed from a lead-time table (model never does date math)
  • points              → fixed per (category, kind)
  • schema/enums        → validated; anything off-contract is dropped
  • caps + dedup        → enforced here

The instruction content lives as composable "skills" in ./life_skills (a core
contract + a router/triage skill + one skill per category). The orchestration
is: router classifies each event → the matching category skill shapes its
tasks → this engine resolves them deterministically.

Output of generate_tasks() matches what server._apply_smart_tasks expects:
    {title, date, points, category, sourceEventId}
"""
from __future__ import annotations

import datetime
import json
import re
from pathlib import Path

import gh_models

# ── Taxonomy (authoritative). Model picks (category, kind); code owns the rest.
#    kind -> (days_before_event, points)
LEAD = {
    "birthday":    {"gift": (7, 2),  "plan": (10, 2), "message": (0, 1)},
    "anniversary": {"gift": (10, 2), "plan": (14, 3), "message": (0, 1)},
    "wedding":     {"rsvp": (30, 1), "gift": (14, 2), "outfit": (10, 2), "travel": (7, 2)},
    "trip":        {"arrange": (10, 2), "prep": (3, 2), "pack": (1, 1), "checkin": (1, 1)},
    "appointment": {"prep": (2, 1), "documents": (2, 1), "confirm": (1, 1)},
    "holiday":     {"plan": (14, 2), "shop": (10, 2), "gift": (14, 2)},
    "social":      {"rsvp": (5, 1), "plan": (3, 1), "bring": (1, 1)},
    "deadline":    {"prep": (5, 2), "complete": (2, 2)},
    "generic":     {"prep": (2, 1)},
}
MAX_TASKS_PER_EVENT = 2
MAX_TASKS_TOTAL = 25
MAX_EVENTS_FOR_AI = 150

SKILLS_DIR = Path(__file__).parent / "life_skills"
# Order matters: core contract first, router (triage) next, then the category
# skills the AI orchestrates between, then worked examples.
SKILL_FILES = [
    "core.md",
    "router.md",
    "skills/birthday.md",
    "skills/anniversary.md",
    "skills/wedding.md",
    "skills/trip.md",
    "skills/appointment.md",
    "skills/holiday.md",
    "skills/social.md",
    "skills/deadline.md",
    "skills/generic.md",
    "examples.md",
]

_PLAYBOOK = None


def _load_playbook():
    global _PLAYBOOK
    if _PLAYBOOK is not None:
        return _PLAYBOOK
    parts = []
    for rel in SKILL_FILES:
        try:
            text = (SKILLS_DIR / rel).read_text(encoding="utf-8").strip()
            if text:
                parts.append(text)
        except Exception:
            continue
    _PLAYBOOK = "\n\n---\n\n".join(parts) if parts else _FALLBACK_PLAYBOOK
    return _PLAYBOOK


# Minimal inline fallback so generation still works if the skill files are
# missing on disk for some reason.
_FALLBACK_PLAYBOOK = (
    "You convert calendar events into prep reminders. For each event return "
    '{"eventId","category","tasks":[{"kind","title"}]}. Categories: '
    + ", ".join(sorted(LEAD)) + ", ignore. Ignore routine work/noise. "
    'Output ONLY {"items":[...]}.'
)


def build_messages(events, today_iso):
    compact = [
        {"id": e["id"], "title": e["title"], "date": e["date"], "allDay": e["allDay"]}
        for e in events
    ]
    user = (
        f"TODAY: {today_iso}\n\n"
        f"EVENTS (JSON array):\n{json.dumps(compact, ensure_ascii=False)}\n\n"
        "Classify each event and produce reminders following the skills above. "
        'Return ONLY the JSON object: {"items":[...]}. No prose, no code fences.'
    )
    return [
        {"role": "system", "content": _load_playbook()},
        {"role": "user", "content": user},
    ]


def _parse_json(content):
    content = (content or "").strip()
    if content.startswith("```"):
        content = content.strip("`")
        nl = content.find("\n")
        if nl != -1:
            content = content[nl + 1:]
    try:
        return json.loads(content)
    except Exception:
        a, b = content.find("{"), content.rfind("}")
        if a != -1 and b != -1 and b > a:
            try:
                return json.loads(content[a:b + 1])
            except Exception:
                return {}
        return {}


def _minus_days(date_iso, days):
    d = datetime.date.fromisoformat(date_iso)
    return (d - datetime.timedelta(days=int(days))).isoformat()


def resolve_items(data, events_by_id, today_iso):
    """Turn the model's classification into validated, dated reminders.
    Everything here is deterministic — the model's date/points are ignored."""
    out, seen = [], set()
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return out

    for item in items:
        if not isinstance(item, dict):
            continue
        eid = str(item.get("eventId") or "").strip()
        ev = events_by_id.get(eid)
        if not ev:
            continue
        cat = str(item.get("category") or "").strip().lower()
        if cat not in LEAD:        # "ignore" or anything unknown → no tasks
            continue
        kinds = LEAD[cat]
        try:
            event_date = datetime.date.fromisoformat(ev["date"]).isoformat()
        except Exception:
            continue

        per_event = 0
        for t in (item.get("tasks") or []):
            if per_event >= MAX_TASKS_PER_EVENT:
                break
            if not isinstance(t, dict):
                continue
            kind = str(t.get("kind") or "").strip().lower()
            if kind not in kinds:
                continue
            title = re.sub(r"\s+", " ", str(t.get("title") or "")).strip()[:120]
            if not title:
                continue
            days_before, points = kinds[kind]
            lead = _minus_days(event_date, days_before)
            # Clamp to [today, event_date]; ISO date strings sort chronologically.
            date = max(today_iso, min(lead, event_date))
            key = (eid, title.lower())
            if key in seen:
                continue
            seen.add(key)
            out.append({
                "title": title,
                "date": date,
                "points": points,
                "category": cat,
                "sourceEventId": eid,
            })
            per_event += 1
            if len(out) >= MAX_TASKS_TOTAL:
                return out
    return out


def generate_tasks(events, today_iso, model=None):
    """Full pipeline: classify via the skill playbook, resolve deterministically.
    One repair retry if the model returns something un-parseable."""
    if not events:
        return []
    events = events[:MAX_EVENTS_FOR_AI]
    events_by_id = {e["id"]: e for e in events}
    messages = build_messages(events, today_iso)

    content = gh_models.chat_completion(messages, model=model, json_object=True, max_tokens=2000)
    data = _parse_json(content)

    if not (isinstance(data, dict) and isinstance(data.get("items"), list)):
        repair = messages + [
            {"role": "assistant", "content": (content or "")[:800]},
            {"role": "user", "content":
                'That was not valid. Return ONLY a JSON object of the exact form '
                '{"items":[{"eventId":"...","category":"...","tasks":[{"kind":"...","title":"..."}]}]}. '
                'No prose, no markdown.'},
        ]
        content = gh_models.chat_completion(repair, model=model, json_object=True, max_tokens=2000)
        data = _parse_json(content)

    return resolve_items(data, events_by_id, today_iso)
