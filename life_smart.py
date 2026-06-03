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
import logging
import re
import time
from pathlib import Path

import gh_models

log = logging.getLogger("mw-backend")

# Output budget. gpt-5-mini is a REASONING model: reasoning tokens count against
# max_completion_tokens, so a small value can be fully consumed by reasoning,
# leaving empty content. Give it real headroom (this is the OUTPUT cap and is
# independent of the ~4000-token INPUT/request cap).
GEN_MAX_TOKENS = 4000
GEN_TIMEOUT = 90

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

# Events per model call. We send SHORT integer ids (Google ids are long and
# dominate tokens), so ~40 events + the compact prompt stays well under the
# 4000-token request cap.
BATCH_SIZE = 40
TITLE_INPUT_MAX = 60
INTER_BATCH_SLEEP = 2   # seconds between batches, to be gentle on the rate limit

# ── Keyword pre-classifier: high-signal events that MUST get tasks regardless
#    of what the AI says. These bypass the model entirely so they're never missed.
_KEYWORD_CATS = [
    ("birthday",    re.compile(r"\bbirthday\b|\bbday\b", re.IGNORECASE)),
    ("anniversary", re.compile(r"\banniversary\b", re.IGNORECASE)),
    ("wedding",     re.compile(r"\bwedding\b|\bbridal\b|\bbachelor(?:ette)?\b", re.IGNORECASE)),
    ("trip",        re.compile(
        r"\btrip\b|\btravel\b|\bflight\b|\bvacation\b|\bsafari\b|\bcruise\b"
        r"|\bhoneymoon\b|\bretreat\b|\bgetaway\b|\brwanda\b|\bafrica\b", re.IGNORECASE)),
    ("deadline",    re.compile(r"\bdeadline\b|\bdue\b(?:\s|$)", re.IGNORECASE)),
]
# Which kinds to auto-generate for each keyword category (max 2)
_AUTO_KINDS = {
    "birthday":    ["gift", "plan"],
    "anniversary": ["gift", "plan"],
    "wedding":     ["rsvp", "gift"],
    "trip":        ["arrange", "pack"],
    "deadline":    ["prep", "complete"],
}
# Title templates per kind
_AUTO_TITLE_TEMPLATES = {
    "gift":      "Get gift for {}",
    "plan":      "Plan for {}",
    "message":   "Send message for {}",
    "rsvp":      "RSVP for {}",
    "outfit":    "Plan outfit for {}",
    "travel":    "Arrange travel for {}",
    "arrange":   "Arrange for {}",
    "prep":      "Prep for {}",
    "pack":      "Pack for {}",
    "checkin":   "Check in for {}",
    "documents": "Gather documents for {}",
    "confirm":   "Confirm {}",
    "shop":      "Shop for {}",
    "bring":     "Bring something for {}",
    "complete":  "Complete {}",
}
# Work/routine events to strip BEFORE sending to AI (reduces noise and token cost)
_WORK_ROUTINE_RE = re.compile(
    r"^\s*(?:standup|stand[\s\-]up|sync|1:?1|one[\s\-]on[\s\-]one|"
    r"weekly\s+(?:team\s+)?(?:meeting|sync)|daily\s+(?:standup|sync)|"
    r"sprint\s+(?:planning|review|retro)|retrospective|"
    r"all[\s\-]hands|team\s+meeting|focus\s+(?:time|block)|"
    r"heads[\s\-]down|busy|hold(?:\s+the\s+spot)?|commute|"
    r"office\s+hours)\s*$",
    re.IGNORECASE,
)

_SYSTEM_PROMPT = None


def _taxonomy_lines():
    return "\n".join(f"- {cat}: {', '.join(kinds)}" for cat, kinds in LEAD.items())


def _system_prompt():
    global _SYSTEM_PROMPT
    if _SYSTEM_PROMPT is None:
        _SYSTEM_PROMPT = (
            "You turn calendar events into short prep reminders.\n\n"
            "ALWAYS generate tasks for: birthdays, anniversaries, weddings, trips, "
            "deadlines, appointments, holidays, and social events.\n"
            "A TRIP is ANY event with a destination/place/city/country/airport in its "
            'title OR its "loc" (location) field, OR any multi-day event ("multiDay":true) '
            '— even when the title is just a place or person name (e.g. "Rwanda", "Kigali").\n\n'
            "OMIT ONLY these routine work events: standups, syncs, 1:1s, team meetings, "
            "sprint reviews/retros, focus blocks, busy/hold blocks, commutes.\n\n"
            "For each qualifying event, pick ONE category and 1-2 tasks:\n"
            + _taxonomy_lines() + "\n\n"
            "Rules: every task's kind MUST be one of its category's kinds above. "
            "title <=8 words, specific, warm, imperative, include the person's name if "
            "present. NO dates, points, emojis, or quotes (the app computes dates+points).\n\n"
            "Output ONLY this JSON object, nothing else:\n"
            '{"items":[{"eventId":"<id>","category":"<category>","tasks":[{"kind":"<kind>","title":"<title>"}]}]}\n'
            'Include only events that get >=1 task; if none, {"items":[]}.\n\n'
            'Example input [{"id":"e1","title":"Mom\'s Birthday","date":"2026-06-20"},'
            '{"id":"e2","title":"Rwanda Safari Trip","date":"2026-07-10"},'
            '{"id":"e3","title":"Standup","date":"2026-06-03"}] -> '
            '{"items":['
            '{"eventId":"e1","category":"birthday","tasks":['
            '{"kind":"gift","title":"Buy Mom a birthday gift"},'
            '{"kind":"plan","title":"Plan something for Mom\'s birthday"}]},'
            '{"eventId":"e2","category":"trip","tasks":['
            '{"kind":"arrange","title":"Arrange for Rwanda Safari"},'
            '{"kind":"pack","title":"Pack for Rwanda Safari"}]}'
            "]} (e3 omitted as routine work)."
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


def build_messages(compact_events, today_iso):
    user = (
        f"TODAY: {today_iso}\nEVENTS:\n{json.dumps(compact_events, ensure_ascii=False)}\n"
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
        # `eid` is the short prompt id; the real Google event id is what we
        # persist (so re-runs dedup to the same reminder, not a new one).
        real_id = str(ev.get("id") or eid)
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
            key = (real_id, title.lower())
            if key in seen:
                continue
            seen.add(key)
            out.append({
                "title": title,
                "date": date,
                "points": points,
                "category": cat,
                "sourceEventId": real_id,
            })
            per_event += 1
            if len(out) >= MAX_TASKS_TOTAL:
                return out
    return out


def _generate_chunk(events_chunk, today_iso, model):
    # Short integer ids keep the request tiny; map them back to real events.
    idx = {str(i): e for i, e in enumerate(events_chunk)}
    def _compact(sid, e):
        c = {"id": sid, "title": (e.get("title") or "")[:TITLE_INPUT_MAX], "date": e["date"]}
        loc = (e.get("location") or "").strip()
        if loc:
            c["loc"] = loc[:60]          # destination is a strong trip signal
        if e.get("multiDay"):
            c["multiDay"] = True
        return c
    compact = [_compact(sid, e) for sid, e in idx.items()]
    messages = build_messages(compact, today_iso)
    content = gh_models.chat_completion(
        messages, model=model, json_object=True, max_tokens=GEN_MAX_TOKENS, timeout=GEN_TIMEOUT)
    data = _parse_json(content)
    if not (isinstance(data, dict) and isinstance(data.get("items"), list)):
        log.info("[life] batch output not valid JSON (content len=%d); retrying", len(content or ""))
        repair = messages + [
            {"role": "assistant", "content": (content or "")[:600]},
            {"role": "user", "content":
                'That was not valid. Return ONLY a JSON object of the exact form '
                '{"items":[{"eventId":"...","category":"...","tasks":[{"kind":"...","title":"..."}]}]}. '
                'No prose, no markdown.'},
        ]
        content = gh_models.chat_completion(
            repair, model=model, json_object=True, max_tokens=GEN_MAX_TOKENS, timeout=GEN_TIMEOUT)
        data = _parse_json(content)

    items = data.get("items") if isinstance(data, dict) else None
    n_items = len(items) if isinstance(items, list) else 0
    resolved = resolve_items(data, idx, today_iso)
    if n_items and not resolved:
        log.warning("[life] batch: %d model items but 0 resolved (enum/id mismatch?); sample=%s",
                    n_items, json.dumps(items[:2])[:400])
    else:
        log.info("[life] batch: %d events -> %d model items -> %d reminders (content len=%d)",
                 len(events_chunk), n_items, len(resolved), len(content or ""))
    return resolved


def _preclassify_keyword_events(events, today_iso):
    """Guarantee that high-signal events (birthdays, trips, etc.) always get
    tasks, regardless of what the AI decides. Returns (preclassified_tasks,
    remaining_events) where remaining_events have work/routine noise stripped."""
    preclassified, remaining, seen_ids = [], [], set()
    for e in events:
        title = e.get("title", "")
        if _WORK_ROUTINE_RE.match(title):
            continue  # drop work/routine noise before AI
        # Match keywords against the title AND the location — a trip's
        # destination is often only in the location field, not the title.
        haystack = (title + " " + (e.get("location") or "")).strip()
        matched_cat = None
        for cat, pattern in _KEYWORD_CATS:
            if pattern.search(haystack):
                matched_cat = cat
                break
        # A multi-day event with no keyword is almost certainly a trip (catches
        # bare place-name titles like "Rwanda" / "Kigali").
        if not matched_cat and e.get("multiDay"):
            matched_cat = "trip"
        if matched_cat and e.get("date"):
            real_id = str(e.get("id") or "")
            try:
                event_date = datetime.date.fromisoformat(e["date"]).isoformat()
            except Exception:
                remaining.append(e)
                continue
            kinds_to_use = _AUTO_KINDS.get(matched_cat, list(LEAD[matched_cat].keys())[:2])
            for kind in kinds_to_use:
                if kind not in LEAD[matched_cat]:
                    continue
                days_before, points = LEAD[matched_cat][kind]
                lead = _minus_days(event_date, days_before)
                date = max(today_iso, min(lead, event_date))
                tmpl = _AUTO_TITLE_TEMPLATES.get(kind, "{}")
                task_title = tmpl.format(title)[:80]
                key = (real_id, task_title.lower())
                if key not in seen_ids:
                    seen_ids.add(key)
                    preclassified.append({
                        "title": task_title,
                        "date": date,
                        "points": points,
                        "category": matched_cat,
                        "sourceEventId": real_id,
                    })
            # Still send to AI so it can produce a nicer/more specific title
            # (pre-classified tasks act as a safety net, not a replacement)
            remaining.append(e)
        else:
            remaining.append(e)
    return preclassified, remaining


def generate_tasks(events, today_iso, model=None):
    """Collapse recurring series, pre-classify high-signal events (birthdays,
    trips, etc.), classify remaining events in token-bounded batches via AI,
    and merge the deterministically-resolved reminders."""
    if not events:
        return []
    events = _collapse_recurring(events)[:MAX_EVENTS_FOR_AI]

    # Step 1: Pre-classify keyword events and strip work/routine noise.
    preclassified, ai_events = _preclassify_keyword_events(events, today_iso)
    log.info("[life] pre-classified %d tasks from %d keyword events; %d events for AI",
             len(preclassified), len(events) - len(ai_events), len(ai_events))

    # Step 2: Send remaining events to AI in batches.
    ai_tasks = []
    for i, chunk in enumerate(_chunks(ai_events, BATCH_SIZE)):
        if i > 0:
            time.sleep(INTER_BATCH_SLEEP)   # space out calls under the rate limit
        try:
            ai_tasks.extend(_generate_chunk(chunk, today_iso, model))
        except gh_models.GitHubModelsError:
            if i == 0:
                raise          # surface real config errors (401, unknown_model, token cap)
            break              # a later transient failure — keep what we have
        if len(ai_tasks) >= MAX_TASKS_TOTAL:
            break

    # Step 3: Merge — AI tasks take precedence (richer titles) but pre-classified
    # tasks fill any gaps for events the AI missed.
    ai_source_ids = {t["sourceEventId"] for t in ai_tasks}
    gap_fillers = [t for t in preclassified if t["sourceEventId"] not in ai_source_ids]
    out = ai_tasks + gap_fillers
    return out[:MAX_TASKS_TOTAL]
