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
# `appointment` and `generic` were removed deliberately. They were the two
# widest nets in the taxonomy and between them produced most of the noise — a
# "Confirm <X> appointment" for every doctor visit, haircut and virtual
# check-in, and a "Prep for <X>" for anything else that scrolled past. An event
# that is already on the calendar, at a time, with a place, does not need a
# reminder to attend it.
LEAD = {
    "birthday":    {"gift": (7, 2),  "plan": (10, 2), "message": (0, 1)},
    "anniversary": {"gift": (10, 2), "plan": (14, 3), "message": (0, 1)},
    "wedding":     {"rsvp": (30, 1), "gift": (14, 2), "outfit": (10, 2), "travel": (7, 2)},
    "trip":        {"arrange": (10, 2), "prep": (3, 2), "pack": (1, 1), "checkin": (1, 1)},
    "holiday":     {"plan": (14, 2), "shop": (10, 2), "gift": (14, 2)},
    "social":      {"rsvp": (5, 1), "plan": (3, 1), "bring": (1, 1)},
    "deadline":    {"prep": (5, 2), "complete": (2, 2)},
}
# Categories whose own titles are trusted — a "dinner" in a Thanksgiving task is
# the point of the task, not noise. Everything else has its generated title
# re-checked against the exclusion rules below.
TRUSTED_CATEGORIES = {"birthday", "anniversary", "wedding", "holiday"}

MAX_TASKS_PER_EVENT = 2
# A reminder list is only useful if it can be read in one sitting. 100 was not a
# cap, it was an invitation; the real limit is how many things a person can hold.
MAX_TASKS_TOTAL = 30
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
# ── Exclusions ────────────────────────────────────────────────────────────────
# The old filter was anchored (^...$), so it only caught an event whose entire
# title was "standup". "Prepare for BVCC meeting" and "Submit Turnberry hours"
# walked straight through. These are substring rules, applied to the title AND
# the location, before the model ever sees the event — and again to the titles
# the model invents, since it will happily write "Confirm the reservation" for
# an event that was only ever a dinner booking.
#
# The principle: an event that already has a time and a place on the calendar
# does not need a reminder to attend it. Reminders are for work that happens
# BEFORE an event and would otherwise be forgotten.
_EXCLUDE_RULES = [
    ("healthcare", re.compile(
        r"\bappointments?\b|\bappt\b|\bdoctor\b|\bdr\.?\s|\bdentist\b|\bdental\b"
        r"|\borthodont|\bclinic\b|\bhospital\b|\btherapy\b|\btherapist\b"
        r"|\bcounsel(?:ing|or)\b|\bcheck[\s\-]?up\b|\bfollow[\s\-]?up\b"
        r"|\bvirtual\s+visit\b|\btelehealth\b|\bvaccin|\bimmuniz"
        r"|\blab\s+work\b|\bbloodwork\b|\bx[\s\-]?ray\b|\bmri\b|\bultrasound\b"
        r"|\bscreening\b|\bcolonoscopy\b|\bmammogram\b|\bphysical\b"
        r"|\beye\s+exam\b|\boptometr|\bchiropract|\bpediatric|\bsurgery\b"
        r"|\bprescription\b|\brefill\b|\bremedy\b", re.IGNORECASE)),
    ("meal", re.compile(
        r"\bdinner\b|\blunch\b|\bbreakfast\b|\bbrunch\b|\bsupper\b"
        r"|\breservations?\b|\bresy\b|\btable\s+for\b|\bdine\b|\bdining\b"
        r"|\bhappy\s+hour\b|\btakeout\b|\bmeal\b|\bcoffee\b|\bdrinks\b",
        re.IGNORECASE)),
    ("routine-service", re.compile(
        r"\bhaircut\b|\bhair\b|\bsalon\b|\bbarber\b|\bcleaning\b|\bhousekeep"
        r"|\boil\s+change\b|\bcar\s+wash\b|\bgrooming\b|\blawn\b|\bmassage\b"
        r"|\bmanicure\b|\bpedicure\b|\bnails\b|\bdry\s+clean"
        r"|\bdrop[\s\-]?off\b|\bpick[\s\-]?up\b|\bservice\b|\brepair\b"
        r"|\binspection\b|\bestimate\b|\btune[\s\-]?up\b|\bmaintenance\b",
        re.IGNORECASE)),
    ("work-routine", re.compile(
        r"\bmeetings?\b|\bstand[\s\-]?up\b|\bsync\b|\b1:?1\b|\bone[\s\-]on[\s\-]one\b"
        r"|\bsprint\b|\bretro(?:spective)?\b|\ball[\s\-]hands\b|\boffice\s+hours\b"
        r"|\bstandup\b|\bhuddle\b|\bon[\s\-]?call\b|\bshift\b|\btraining\b"
        r"|\bwebinar\b|\bconference\s+call\b|\bcall\s+with\b|\bzoom\b"
        r"|\bfocus\s+(?:time|block)\b|\bheads[\s\-]down\b|\bcommute\b"
        r"|\bbusy\b|\bhold\b|\bOOO\b|\bPTO\b|\btime\s?sheet\b|\btimesheet\b"
        r"|\bpay\s+period\b|\bpayroll\b|\bhours\b|\bstatus\b|\bcheck[\s\-]in\b"
        r"|\bboard\b|\bcommittee\b|\bopen\s+house\b", re.IGNORECASE)),
    # Recurring money movement. Distinct from a real deadline (a renewal, a
    # filing, a registration) — a mortgage payment recurs forever and reminding
    # about it every month is pure noise.
    ("recurring-bill", re.compile(
        r"\bpay\b|\bpayment\b|\bbill\b|\bloan\b|\bmortgage\b|\brent\b"
        r"|\bautopay\b|\bstatement\b|\binvoice\b|\bvenmo\b|\bzelle\b"
        r"|\bsubscription\b|\bdues\b", re.IGNORECASE)),
    ("class-or-practice", re.compile(
        r"\bclass\b|\blesson\b|\bpractice\b|\brehearsal\b|\bworkout\b"
        r"|\bgym\b|\byoga\b|\btutoring\b", re.IGNORECASE)),
]

# Holidays worth planning around. An allowlist rather than a blocklist, because
# the calendar feed carries hundreds of observances — World Mental Health Day,
# Columbus Day, National Whatever Day — and enumerating the ones to skip is a
# losing game. Add to this list rather than trying to exclude the rest.
_MAJOR_HOLIDAY_RE = re.compile(
    r"\bthanksgiving\b|\bchristmas\b|\bxmas\b|\bhanukkah\b|\bchanukah\b"
    r"|\bnew\s+year|\bnye\b|\beaster\b|\bpassover\b|\bhalloween\b"
    r"|\bvalentine|\bmother'?s\s+day\b|\bfather'?s\s+day\b"
    r"|\bindependence\s+day\b|\bjuly\s*4|\b4th\s+of\s+july\b|\bfourth\s+of\s+july\b"
    r"|\beid\b|\bdiwali\b|\blunar\s+new\s+year\b|\brosh\s+hashanah\b"
    r"|\byom\s+kippur\b|\bthanksgiving\s+eve\b",
    re.IGNORECASE,
)


def is_major_holiday(text):
    return bool(_MAJOR_HOLIDAY_RE.search(text or ""))


def exclusion_reason(text):
    """Why this text disqualifies an event, or None to keep it.

    A major holiday always wins: "Christmas dinner" is a holiday whose task
    legitimately mentions dinner, not a restaurant booking."""
    text = text or ""
    if is_major_holiday(text):
        return None
    for name, rx in _EXCLUDE_RULES:
        if rx.search(text):
            return name
    return None


def task_exclusion_reason(title, category):
    """Re-check a title the model wrote. Trusted categories are exempt — the
    model saying "dinner" about Thanksgiving is correct."""
    if category in TRUSTED_CATEGORIES:
        return None
    return exclusion_reason(title)

_SYSTEM_PROMPT = None


def _taxonomy_lines():
    return "\n".join(f"- {cat}: {', '.join(kinds)}" for cat, kinds in LEAD.items())


def _system_prompt():
    """Restrictive by construction. The previous version opened with "ALWAYS
    generate tasks for ... appointments ... and social events" and then said
    "OMIT ONLY routine work events", which is an instruction to say yes to
    nearly everything — and it did, 80 reminders' worth. This one inverts the
    default: omit unless the event clears a bar."""
    global _SYSTEM_PROMPT
    if _SYSTEM_PROMPT is None:
        _SYSTEM_PROMPT = (
            "You turn calendar events into a SHORT list of prep reminders.\n\n"
            "DEFAULT TO OMITTING. Most calendar events need no reminder at all. "
            "An event already has a time and a place — the person will simply "
            "attend it. Only create a task when there is real work to do BEFORE "
            "the event that would otherwise be forgotten.\n\n"
            "OMIT (never produce a task for):\n"
            "- medical or dental anything: appointments, follow-ups, virtual "
            "visits, therapy, checkups, labs, procedures\n"
            "- meals and bookings: dinner, lunch, brunch, coffee, drinks, "
            "restaurant reservations\n"
            "- meetings of any kind, work or volunteer: syncs, 1:1s, standups, "
            "board or committee meetings, open houses, calls, classes, "
            "lessons, practices, rehearsals, shifts\n"
            "- work admin: timesheets, hours, pay periods, status updates\n"
            "- recurring money: bills, loan or mortgage payments, rent, "
            "subscriptions, reimbursing someone\n"
            "- routine services: haircuts, house cleaning, oil changes, "
            "grooming, workouts\n"
            "- minor observances and awareness days. ONLY these holidays "
            "qualify: Thanksgiving, Christmas, Christmas Eve, Hanukkah, New "
            "Year's Eve/Day, Easter, Passover, Halloween, Valentine's Day, "
            "Mother's Day, Father's Day, Independence Day, Eid, Diwali, Lunar "
            "New Year, Rosh Hashanah, Yom Kippur.\n"
            "- anything you are not confident is a personal life event\n\n"
            "INCLUDE only these, and only when a genuine prep task exists:\n"
            + _taxonomy_lines() + "\n\n"
            "Notes on the harder ones:\n"
            "- deadline means a one-off obligation with a consequence for "
            "missing it — a renewal, a filing, a registration, taxes. NOT a "
            "recurring bill and NOT routine work admin.\n"
            "- trip means being away from home: a flight, lodging, a "
            "destination in the title or location, a multi-day stay somewhere "
            "else. A multi-day block by itself is not a trip.\n"
            "- social means a gathering that needs something brought, an RSVP, "
            "or real planning. A booked restaurant table or a ticketed show is "
            "NOT social — it is already handled.\n\n"
            "Rules: every task's kind MUST be one of its category's kinds "
            "above. One task per event is usually right; two only when both are "
            "genuinely separate work. title <=8 words, specific, warm, "
            "imperative, include the person's name if present. Never write a "
            "task that just restates the event ('Confirm X', 'Attend Y', 'Go to "
            "Z'). NO dates, points, emojis, or quotes.\n\n"
            "Output ONLY this JSON object, nothing else:\n"
            '{"items":[{"eventId":"<id>","category":"<category>","tasks":[{"kind":"<kind>","title":"<title>"}]}]}\n'
            'Include only events that get >=1 task; if none, {"items":[]}.\n\n'
            'Example input [{"id":"e1","title":"Mom\'s Birthday","date":"2026-06-20"},'
            '{"id":"e2","title":"Rwanda Safari Trip","date":"2026-07-10"},'
            '{"id":"e3","title":"Dentist appointment","date":"2026-06-03"},'
            '{"id":"e4","title":"Dinner at Nightingale","date":"2026-06-05"},'
            '{"id":"e5","title":"BVCC meeting","date":"2026-06-06"}] -> '
            '{"items":['
            '{"eventId":"e1","category":"birthday","tasks":['
            '{"kind":"gift","title":"Buy Mom a birthday gift"}]},'
            '{"eventId":"e2","category":"trip","tasks":['
            '{"kind":"arrange","title":"Arrange for Rwanda Safari"},'
            '{"kind":"pack","title":"Pack for Rwanda Safari"}]}'
            "]} — e3, e4 and e5 all omitted: a dental appointment, a booked "
            "dinner and a meeting each need no preparation."
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
        # The holiday allowlist is enforced here rather than left to the prompt,
        # because a calendar feed carries hundreds of observances and the model
        # will cheerfully call any of them a holiday. "Columbus Day" and "World
        # Mental Health Day" are days, not occasions that need planning.
        if cat == "holiday":
            ev_text = (ev.get("title") or "") + " " + (ev.get("location") or "")
            if not is_major_holiday(ev_text):
                log.debug("[life] dropped non-major holiday %r", ev.get("title"))
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
            # The model still reaches for "Confirm the reservation" on a dinner
            # it should have skipped. Re-check what it wrote, not just what it
            # was given.
            why = task_exclusion_reason(title, cat)
            if why:
                log.debug("[life] dropped task %r (%s)", title, why)
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
                "kind": kind,            # carried so the upsert key is per-task, not per-event
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
    dropped = {}
    for e in events:
        title = e.get("title", "")
        # Match against the title AND the location — a trip's destination is
        # often only in the location field, and so is "Nightingale".
        haystack = (title + " " + (e.get("location") or "")).strip()
        why = exclusion_reason(haystack)
        if why:
            # Never reaches the model, so it can't be talked into a task and
            # costs no tokens either.
            dropped[why] = dropped.get(why, 0) + 1
            continue
        matched_cat = None
        for cat, pattern in _KEYWORD_CATS:
            if pattern.search(haystack):
                matched_cat = cat
                break
        # NOTE: we intentionally do NOT auto-classify every multi-day event as a
        # trip (per the multi-day policy in the skill docs). Real trips are caught
        # by the travel keywords above, and the model also sees multiDay + loc.
        if matched_cat and e.get("date"):
            real_id = str(e.get("id") or "")
            try:
                event_date = datetime.date.fromisoformat(e["date"]).isoformat()
            except Exception:
                remaining.append(e)
                continue
            kinds_to_use = _AUTO_KINDS.get(matched_cat, list(LEAD[matched_cat].keys())[:2])
            if matched_cat not in LEAD:
                remaining.append(e)
                continue
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
                        "kind": kind,
                        "sourceEventId": real_id,
                    })
            # Still send to AI so it can produce a nicer/more specific title
            # (pre-classified tasks act as a safety net, not a replacement)
            remaining.append(e)
        else:
            remaining.append(e)
    if dropped:
        log.info("[life] excluded %d events before AI: %s",
                 sum(dropped.values()),
                 ", ".join(f"{k}={v}" for k, v in sorted(dropped.items())))
    return preclassified, remaining


# ── Deduplication ─────────────────────────────────────────────────────────────

_STOPWORDS = {"a", "an", "the", "for", "to", "of", "and", "at", "on", "in",
              "with", "your", "my", "some", "something"}


def _title_tokens(title):
    words = re.findall(r"[a-z0-9']+", (title or "").lower())
    return {w for w in words if w not in _STOPWORDS}


def _norm_title(title):
    return " ".join(sorted(_title_tokens(title)))


def _too_similar(a, b, threshold=0.6):
    """Jaccard overlap on content words. Catches the pair this produced for one
    event — "Plan trip to White Christmas show" and "Plan for White Christmas
    show" — which are not equal strings but are plainly the same task."""
    ta, tb = _title_tokens(a), _title_tokens(b)
    if not ta or not tb:
        return False
    return len(ta & tb) / len(ta | tb) >= threshold


def dedupe_tasks(tasks):
    """Collapse repeats, keeping the soonest occurrence of each.

    Two shapes of duplicate showed up in practice. The same task for a recurring
    series, once per occurrence ("Confirm Ethan's virtual visit" five times over
    three months). And two near-identical titles for a single event, because the
    model was asked for up to two tasks and obliged with a paraphrase."""
    kept = []
    by_norm = {}
    for t in sorted(tasks, key=lambda x: (x.get("date") or "", x.get("title") or "")):
        norm = _norm_title(t.get("title"))
        if norm in by_norm:
            continue                      # identical content words, later date
        near = next(
            (k for k in kept
             if k.get("sourceEventId") == t.get("sourceEventId")
             and _too_similar(k.get("title"), t.get("title"))),
            None,
        )
        if near:
            continue
        by_norm[norm] = t
        kept.append(t)
    return kept


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
    out = dedupe_tasks(ai_tasks + gap_fillers)
    # Soonest first, so if the cap does bite it drops the most distant reminders
    # rather than an arbitrary slice.
    out.sort(key=lambda t: (t.get("date") or "", t.get("title") or ""))
    if len(out) > MAX_TASKS_TOTAL:
        log.info("[life] %d reminders trimmed to the %d soonest",
                 len(out), MAX_TASKS_TOTAL)
    return out[:MAX_TASKS_TOTAL]
