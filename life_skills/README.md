# Life Dashboard — smart-reminder skills

A multi-file, model-orchestrated process that turns Google Calendar events into
dated reminders.

## These files are the human-readable spec — not necessarily the full runtime prompt
Small models and mini endpoints have tight token limits, so at runtime
`life_smart.py` should compile a compact prompt from:
- the category taxonomy,
- allowed `kind`s from `LEAD`,
- the router rules,
- terse hard rules,
- and 1–2 high-value examples.

These `.md` files remain the source of truth for behavior. Keep them, the runtime
compact prompt builder, and the `LEAD` table in sync.

## Pipeline
1. **Normalize events** into a stable shape: id, title, start/end, allDay,
   calendar, location, description snippet, recurrence key, and duration days.
2. **Collapse recurring series** to the soonest upcoming instance unless the
   series is a life-event calendar where each instance has a distinct person or
   title.
3. **Pre-filter obvious noise** before the model when safe: standups, focus
   blocks, holds, generic busy/OOO/PTO with no destination, and routine work
   meetings.
4. **Batch** remaining events into token-bounded calls and merge.
5. Per batch, the model returns ONLY `{eventId, category, tasks:[{kind,title}]}`.
6. The engine deterministically computes reminder date and points from
   `life_smart.LEAD`, validates enums, drops anything off-contract, dedups, and
   caps output.

## Robustness layers
- Closed enums for `category` and `kind`; unknown values are dropped in code.
- Strict JSON parse plus one repair retry if needed.
- Deterministic date/points in code; the model never emits dates or points.
- Stable idempotency key such as `gcal-ai:{eventId}:{category}:{kind}`.
- Dedupe near-identical titles per event and across recurring copies.
- Keep completed reminders unless the source event disappears; prune only future,
  incomplete suggestions no longer produced.
- Log dropped model outputs with reason: bad JSON, unknown category, unknown kind,
  too many tasks, extra fields, over cap, or stale event.
- Track model omissions separately from pre-filter omissions for debugging.

## Multi-day policy
Do **not** treat every multi-day event as a trip. Multi-day is only a signal.
Use `trip` when the event has destination/away/travel cues: flights, hotels,
Airbnb, airport codes, vacation, camping, road trip, conference in another city,
or "in <place>". Generic multi-day OOO/PTO/busy blocks should usually be omitted.
Holiday/wedding/anniversary/deadline cues beat trip when that is the event's
primary meaning.

## Add or change a skill
1. Add or edit `life_skills/<name>.md` with allowed kinds, guidance, and examples.
2. Add/update the category + kinds `(days_before, points)` in `life_smart.LEAD`.
3. Add/update cues in `router.md`.
4. Add/update runtime compact prompt compilation if it does not read these files.
5. Add worked examples covering the new edge cases.

The code is authoritative for allowed kinds. If a kind is missing from `LEAD`, the
engine should drop it and log the mismatch.
