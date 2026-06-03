# Router skill — classify each event into ONE category

Use the event title first, then location/calendar/description/all-day span when
available. Match case-insensitively. Pick the single best fit.

## Priority when multiple cues match
birthday → anniversary → wedding → holiday → deadline → trip → appointment → social → generic → ignore

Use the priority only when two categories are genuinely plausible. Otherwise use
the category that would create the most useful real-world prep task.

## Categories
- **birthday** — "birthday", "bday", "born", "🎂", "<Name>'s Birthday", or events
  from a Birthdays calendar. Do not confuse "birthday party" for someone else
  with `social` unless the birthday person's name/occasion is explicit; birthday
  is usually better.

- **anniversary** — "anniversary", "anniv", "years together", "wedding anniversary".
  The user's own/partner anniversary should be anniversary, not social.

- **wedding** — "wedding", "gets married", "ceremony", "reception", "rehearsal
  dinner", "bridal shower" when clearly wedding-related, or named couple + wedding
  context. Use `wedding` over `trip` if the event is a wedding, even when travel
  may be needed; the wedding skill can emit travel.

- **holiday** — culturally-prepped days: Thanksgiving, Christmas, Christmas Eve,
  Hanukkah, Eid, Diwali, Valentine's Day, Mother's Day, Father's Day, New Year's
  Eve, Easter, Passover, Halloween, July 4th/Independence Day. Use `holiday`, not
  `trip`, unless the title clearly describes travel such as "Flight to Chicago".

- **deadline** — time-bound obligations: "due", "deadline", "renew", "expires",
  "expiration", "registration", "payment", "pay", "file", "submit", "taxes",
  "application", "enrollment", "cancel by", "return by", "RSVP due". Use
  `deadline` over appointment/social when the main action is completing something
  by a date.

- **trip** — being away from home or travel logistics: "flight", "airport", "trip",
  "vacation", "getaway", "travel", "hotel", "Airbnb", "check-in", "check out",
  "road trip", "camping", "conference in <city>", airport codes/routes like
  "MSP → DEN", "✈", or a multi-day "in <city/place>" event. A multi-day event is
  NOT automatically a trip; ignore generic "OOO"/"PTO"/"busy" unless it includes
  a destination or travel reason.

- **appointment** — prep/paperwork events: doctor, dentist, therapy, vet,
  appointment, interview, exam, test, DMV, consultation, court, inspection,
  service appointment, repair appointment, estimate, fitting, passport, license,
  school conference. Routine work meetings are not appointments.

- **social** — gatherings worth light prep with a named person/occasion: "dinner
  with <name>", "<name>'s party", housewarming, baby shower, bridal shower,
  graduation party, BBQ, potluck, game night, concert with a named person, date
  night, hosting, family gathering. Plain "Lunch"/"Coffee" without a person or
  purpose is ignore.

- **generic** — clearly noteworthy and personal but fits nothing above and still
  benefits from a heads-up: recital, performance, move, race, tournament, photos,
  audition, big presentation, home project day, first day of school. Use sparingly.

## Otherwise: ignore / omit the event
Always omit routine or low-signal items: meetings, standups, 1:1s, syncs,
reviews, sprint ceremonies, focus/blocked/busy/hold, generic OOO/PTO, reminders
already phrased as tasks, commutes, workouts, chores, recurring work blocks,
automated calendar holds, birthdays for unknown contacts with no useful action,
and anything you're not confident is personal. When in doubt, omit.
