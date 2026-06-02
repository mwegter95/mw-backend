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

# The life_skills/*.md files are the human-readable spec for each skill. But
# gpt-5-mini caps requests at ~4000 input tokens, so at RUNTIME we don't ship all
# of them (~2,100 tokens) — we compile a compact (~400-token) prompt from the
# LEAD taxonomy above (the authoritative source those files mirror). Edit a skill
# file and the LEAD table together; robustness comes from the code validation
# below, not from prompt verbosity.
SKILLS_DIR = Path(__file__).parent / "life_skills"

# Events per model call. compact prompt (~400 tok) + one chunk must stay well
# under the 4000-token request cap, so we batch the events and merge results.
BATCH_SIZE = 40
TITLE_INPUT_MAX = 80

_SYSTEM_PROMPT = None


def _taxonomy_lines():
    return "\n".join(f"- {cat}: {', '.join(kinds)}" for cat, kinds in LEAD.items())


def _system_prompt():
    global _SYSTEM_PROMPT
    if _SYSTEM_PROMPT is None:
        _SYSTEM_PROMPT = (
            "You turn calendar events into short prep reminders. For EACH event, pick "
            "ONE category and 0-2 tasks; OMIT events that don't need prep. Most work/"
            "routine events are omitted: meetings, standups, syncs, 1:1s, reviews, "
            "focus/busy/holds, commutes, unnamed lunches or coffees. When unsure, omit.\n\n"
            "Categories and their ONLY allowed task kinds:\n" + _taxonomy_lines() + "\n\n"
            "Rules: every task's kind MUST be one of its category's kinds above. "
            "title <=8 words, specific, warm, imperative, include the person's name if "
            "present. NO dates, points, emojis, or quotes (the app computes dates+points). "
            "Max 2 tasks per event, no duplicates.\n\n"
            "Output ONLY this JSON object, nothing else:\n"
            '{"items":[{"eventId":"<id>","category":"<category>","tasks":[{"kind":"<kind>","title":"<title>"}]}]}\n'
            'Include only events that get >=1 task; if none, {"items":[]}.\n\n'
            'Example input [{"id":"e1","title":"Mom\'s Birthday","date":"2026-06-20"},'
            '{"id":"e2","title":"Standup","date":"2026-06-03"}] -> '
            '{"items":[{"eventId":"e1","category":"birthday","tasks":['
            '{"kind":"gift","title":"Buy Mom a birthday gift"},'
            '{"kind":"plan","title":"Plan something for Mom\'s birthday"}]}]} '
            "(e2 omitted as routine work)."
        )
    return _SYSTEM_PROMPT


def _collapse_recurring(events):
    """Keep only the soonest instance of each recurring series (events arrive
    sorted ascending). Collapses e.g. weekly standups to one while keeping the
    next birthday — a big token saver and cleaner signal."""
    seen, out = set(), []
    for e in events:
        rid = e.get("recurringEventId")
        if rid:
            if rid in seen:
                continue
            seen.add(rid)
        out.append(e)
    return out


def _chunks(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def build_messages(events_chunk, today_iso):
    compact = [
        {"id": e["id"], "title": (e.get("title") or "")[:TITLE_INPUT_MAX], "date": e["date"]}
        for e in events_chunk
    ]
    user = (
        f"TODAY: {today_iso}\nEVENTS:\n{json.dumps(compact, ensure_ascii=False)}\n"
        'Return ONLY the JSON object {"items":[...]}.'
    )
    return [
        {"role": "system", "content": _system_prompt()},
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


def _generate_chunk(events_chunk, today_iso, model, events_by_id):
    messages = build_messages(events_chunk, today_iso)
    content = gh_models.chat_completion(messages, model=model, json_object=True, max_tokens=1200)
    data = _parse_json(content)
    if not (isinstance(data, dict) and isinstance(data.get("items"), list)):
        repair = messages + [
            {"role": "assistant", "content": (content or "")[:600]},
            {"role": "user", "content":
                'That was not valid. Return ONLY a JSON object of the exact form '
                '{"items":[{"eventId":"...","category":"...","tasks":[{"kind":"...","title":"..."}]}]}. '
                'No prose, no markdown.'},
        ]
        content = gh_models.chat_completion(repair, model=model, json_object=True, max_tokens=1200)
        data = _parse_json(content)
    return resolve_items(data, events_by_id, today_iso)


def generate_tasks(events, today_iso, model=None):
    """Collapse recurring series, classify in token-bounded batches (gpt-5-mini
    caps requests at ~4000 tokens), and merge the deterministically-resolved
    reminders. One repair retry per batch on un-parseable output."""
    if not events:
        return []
    events = _collapse_recurring(events)[:MAX_EVENTS_FOR_AI]
    events_by_id = {e["id"]: e for e in events}

    out = []
    for i, chunk in enumerate(_chunks(events, BATCH_SIZE)):
        try:
            out.extend(_generate_chunk(chunk, today_iso, model, events_by_id))
        except gh_models.GitHubModelsError:
            if i == 0:
                raise          # surface real config errors (401, unknown_model, token cap)
            break              # a later transient failure — keep what we have
        if len(out) >= MAX_TASKS_TOTAL:
            break
    return out[:MAX_TASKS_TOTAL]
