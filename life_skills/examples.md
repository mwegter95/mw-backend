# Worked examples

### Example
TODAY: 2026-06-02
EVENTS:
[{"id":"e1","title":"Mom's Birthday","date":"2026-06-20","allDay":true},
 {"id":"e2","title":"Standup","date":"2026-06-03","allDay":false},
 {"id":"e3","title":"Flight to Denver","date":"2026-06-15","allDay":false},
 {"id":"e4","title":"Dentist appointment","date":"2026-06-10","allDay":false},
 {"id":"e5","title":"Focus block","date":"2026-06-04","allDay":false}]

OUTPUT:
{"items":[
 {"eventId":"e1","category":"birthday","tasks":[{"kind":"gift","title":"Buy Mom a birthday gift"},{"kind":"plan","title":"Plan something for Mom's birthday"}]},
 {"eventId":"e3","category":"trip","tasks":[{"kind":"prep","title":"Prep for the Denver trip"},{"kind":"pack","title":"Pack for Denver"}]},
 {"eventId":"e4","category":"appointment","tasks":[{"kind":"confirm","title":"Confirm the dentist appointment"}]}
]}

Why: "Standup" (e2) and "Focus block" (e5) are routine work → omitted entirely.
No dates or points appear in the output — the app computes them.
