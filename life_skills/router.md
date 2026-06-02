# Router skill — classify each event into ONE category

Match on the event title (case-insensitive). Pick the single best fit.

- **birthday** — "birthday", "bday", "🎂", "<Name>'s Birthday", or events from a Birthdays calendar.
- **anniversary** — "anniversary".
- **wedding** — "wedding", "gets married", "ceremony"/"reception".
- **trip** — being away from home: "flight", "trip", "vacation", "getaway", airport codes (e.g. SFO → JFK), "✈", hotel / Airbnb check-in, a multi-day "in <city>".
- **appointment** — needs prep or paperwork: "doctor", "dentist", "appointment", "interview", "exam", "DMV", "consultation", "court", "inspection".
- **holiday** — culturally-prepped days: Thanksgiving, Christmas, Hanukkah, Eid, Diwali, Valentine's Day, Mother's/Father's Day, New Year's Eve. (Use `holiday`, not `trip`, unless it's clearly travel.)
- **social** — gatherings worth light prep with a NAMED person/occasion: "dinner with <name>", "<name>'s party", "housewarming", "baby/bridal shower", "game night".
- **deadline** — time-bound obligations: "due", "deadline", "renew", "expires", "registration", "payment", "file", "submit", "taxes".
- **generic** — clearly noteworthy and personal but fits nothing above. Use sparingly.

## Otherwise: ignore (omit the event)
Routine/work and low-signal items always get omitted: meetings, standups, 1:1s,
syncs, reviews, "focus" / "blocked" / "busy" / "hold", OOO markers, recurring
work blocks, commutes, plain "Lunch" / "Coffee" with no named person, and
anything you're not confident is personal. **When in doubt, omit.**
