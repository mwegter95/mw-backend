# Life Dashboard — smart-reminder skills

A multi-file, model-orchestrated process that turns Google Calendar events into
dated reminders. Tuned for **GPT-5.4 mini**.

## How it works
`life_smart.py` composes these files (in order) into one system prompt:
1. `core.md` — the contract: flow, hard rules, exact JSON output, self-check.
2. `router.md` — the triage skill: classify each event into one category (or ignore/omit).
3. `skills/<category>.md` — one skill per category. For each event the model
   "orchestrates" by routing to the matching skill and using only that skill's
   allowed `kind`s.
4. `examples.md` — worked input→output examples.

The model returns ONLY `{eventId, category, tasks:[{kind, title}]}`. The engine
then **deterministically** computes the reminder DATE (lead-time table in
`life_smart.LEAD`) and POINTS, validates enums, drops anything off-contract,
dedups, and caps (≤2/event, ≤25 total). The model never does date math or sets
points — that's what makes the output predictable and robust.

## Robustness layers
- Closed enums for `category` and `kind`; unknown values are dropped in code.
- One automatic repair retry if the reply isn't valid JSON.
- GPT-5-family transport handling in `gh_models.py` (uses `max_completion_tokens`,
  omits forced `temperature`/`top_p`, with a 400-parameter fallback).
- A built-in fallback prompt if the skill files are ever missing on disk.

## Add or change a skill
1. Add `skills/<name>.md` (allowed kinds + when-to-use + title examples).
2. Add the category + its kinds `(days_before, points)` to `LEAD` in `life_smart.py`.
3. Add a cue in `router.md`, and add the file path to `SKILL_FILES`.
Keep each file's kinds in sync with `LEAD` — the code is authoritative and
silently drops kinds it doesn't recognize.
