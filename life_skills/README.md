# Life Dashboard — smart-reminder skills

A multi-file, model-orchestrated process that turns Google Calendar events into
dated reminders. Tuned for **GPT-5.4 mini**.

## These files are the spec — not the runtime prompt
`gpt-5-mini` caps requests at ~4,000 input tokens, so shipping all of these
(~2,100 tokens) plus a real calendar overflows. So at **runtime** `life_smart.py`
compiles a **compact (~350-token) prompt** from the `LEAD` taxonomy (categories →
allowed `kind`s) plus terse rules + one example — it does NOT concatenate these
files. These `.md` files remain the human-readable source of truth for each
skill; keep them and the `LEAD` table in sync.

Pipeline:
1. **Collapse recurring series** to the soonest instance (60 standups → 1; keeps
   the next birthday).
2. **Batch** events (`BATCH_SIZE`) into token-bounded calls and merge.
3. Per batch the model returns ONLY `{eventId, category, tasks:[{kind, title}]}`.
4. The engine **deterministically** computes the reminder DATE (lead-time table
   in `life_smart.LEAD`) and POINTS, validates enums, drops anything off-contract,
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
