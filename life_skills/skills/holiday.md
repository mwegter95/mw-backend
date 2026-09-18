# Skill: holiday

Allowed kinds: `plan`, `shop`, `gift`.
- **plan** — plan the gathering, travel, meal, reservations, schedule, or family
  logistics.
- **shop** — groceries, supplies, decorations, cards, or hosting needs.
- **gift** — presents or gestures for gift-centered holidays.

Guidance:
- Pick the 1–2 most relevant to the holiday.
- Christmas/Hanukkah/Eid/Diwali: usually `gift` + `plan` or `shop`.
- Thanksgiving/Easter/Passover/July 4th/Halloween: usually `plan` + `shop`.
- Valentine's/Mother's/Father's Day: usually `gift` + `plan` for close people;
  just `gift` if the calendar event itself is enough.
- New Year's Eve: `plan` unless hosting, then consider `shop`.
- Do not classify a holiday as trip unless the event itself is travel.

Titles: "Plan Thanksgiving", "Shop for Christmas gifts", "Get a Valentine's gift", "Buy Mother's Day flowers".

## Allowlist (enforced in code)
Only these count as holidays. Anything else — awareness days, observances, local
festivals, minor federal days — is omitted, whatever the model decides:

Thanksgiving · Christmas · Christmas Eve · Hanukkah · New Year's Eve · New
Year's Day · Easter · Passover · Halloween · Valentine's Day · Mother's Day ·
Father's Day · Independence Day (July 4th) · Eid · Diwali · Lunar New Year ·
Rosh Hashanah · Yom Kippur

`life_smart._MAJOR_HOLIDAY_RE` is the source of truth; add to it and to this
list together.
