# Router skill — classify each event into ONE category

Use the event title first, then location/calendar/description/all-day span when
available. Match case-insensitively. Pick the single best fit.

## Priority when multiple cues match
birthday → anniversary → wedding → holiday → deadline → trip → social → ignore

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

- **holiday** — ONLY these, and nothing else: Thanksgiving, Christmas, Christmas
  Eve, Hanukkah, Eid, Diwali, Lunar New Year, Rosh Hashanah, Yom Kippur,
  Valentine's Day, Mother's Day, Father's Day, New Year's Eve/Day, Easter,
  Passover, Halloween, July 4th/Independence Day. This is an allowlist and the
  code enforces it. Awareness days, observances and local festivals — World
  Mental Health Day, Columbus Day, Fall Fest — are NOT holidays; omit them. Use
  `holiday`, not `trip`, unless the title clearly describes travel.

- **deadline** — a ONE-OFF obligation with a real consequence for missing it:
  "renew", "expires", "expiration", "registration", "taxes", "application",
  "enrollment", "cancel by", "return by", "RSVP due", "file by". NOT recurring
  bills ("pay mortgage", "rent due", a phone reimbursement) and NOT work admin
  ("submit hours", "timesheet", "pay period") — those recur forever and
  reminding about each one is noise.

- **trip** — being away from home or travel logistics: "flight", "airport", "trip",
  "vacation", "getaway", "travel", "hotel", "Airbnb", "check-in", "check out",
  "road trip", "camping", "conference in <city>", airport codes/routes like
  "MSP → DEN", "✈", or a multi-day "in <city/place>" event. A multi-day event is
  NOT automatically a trip; ignore generic "OOO"/"PTO"/"busy" unless it includes
  a destination or travel reason.


- **social** — a gathering that needs something BROUGHT, an RSVP, or genuine
  planning: "<name>'s party", housewarming, baby shower, graduation party, BBQ,
  potluck, game night, hosting, family gathering. A booked restaurant table or a
  ticketed show is NOT social — it is already handled, and the person simply
  attends. Meals of any kind (dinner, lunch, brunch, coffee, drinks) are always
  ignore, with or without a name attached.


## Otherwise: ignore / omit the event
Always omit routine or low-signal items: all medical and dental events, all
meals and restaurant bookings, all routine services (haircuts, cleaning, vehicle
drop-off), recurring bills and payments, work admin, meetings, standups, 1:1s, syncs,
reviews, sprint ceremonies, focus/blocked/busy/hold, generic OOO/PTO, reminders
already phrased as tasks, commutes, workouts, chores, recurring work blocks,
automated calendar holds, birthdays for unknown contacts with no useful action,
and anything you're not confident is personal. When in doubt, omit.
