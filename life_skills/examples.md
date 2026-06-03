# Worked examples

### Example 1 — common personal events
TODAY: 2026-06-02
EVENTS:
[{"id":"e1","title":"Mom's Birthday","date":"2026-06-20","allDay":true},
 {"id":"e2","title":"Standup","date":"2026-06-03","allDay":false},
 {"id":"e3","title":"Flight to Denver","date":"2026-06-15","allDay":false},
 {"id":"e4","title":"Dentist appointment","date":"2026-06-10","allDay":false},
 {"id":"e5","title":"Focus block","date":"2026-06-04","allDay":false}]

OUTPUT:
{"items":[
 {"eventId":"e1","category":"birthday","tasks":[{"kind":"gift","title":"Buy Mom a birthday gift"},{"kind":"plan","title":"Plan Mom's birthday"}]},
 {"eventId":"e3","category":"trip","tasks":[{"kind":"checkin","title":"Check in for the flight"},{"kind":"pack","title":"Pack for Denver"}]},
 {"eventId":"e4","category":"appointment","tasks":[{"kind":"confirm","title":"Confirm the dentist appointment"}]}
]}

Why: "Standup" and "Focus block" are routine work/noise and are omitted.
No dates or points appear in the output — the app computes them.

### Example 2 — multi-day is not automatically trip
TODAY: 2026-06-02
EVENTS:
[{"id":"m1","title":"OOO","start":"2026-07-01","end":"2026-07-05","allDay":true},
 {"id":"m2","title":"In Denver","start":"2026-07-01","end":"2026-07-05","allDay":true},
 {"id":"m3","title":"Thanksgiving","start":"2026-11-26","end":"2026-11-27","allDay":true},
 {"id":"m4","title":"Jenna and Mark wedding weekend","start":"2026-08-14","end":"2026-08-16","allDay":true}]

OUTPUT:
{"items":[
 {"eventId":"m2","category":"trip","tasks":[{"kind":"arrange","title":"Arrange logistics for Denver"},{"kind":"prep","title":"Prep for the Denver trip"}]},
 {"eventId":"m3","category":"holiday","tasks":[{"kind":"plan","title":"Plan Thanksgiving"},{"kind":"shop","title":"Shop for Thanksgiving"}]},
 {"eventId":"m4","category":"wedding","tasks":[{"kind":"travel","title":"Plan wedding travel"},{"kind":"outfit","title":"Choose a wedding outfit"}]}
]}

Why: Generic OOO is omitted. "In Denver" is a trip because it has destination/away
signals. Thanksgiving remains holiday. Wedding weekend remains wedding, not trip.

### Example 3 — obligations and named social events
TODAY: 2026-06-02
EVENTS:
[{"id":"d1","title":"Car registration expires","date":"2026-06-30","allDay":true},
 {"id":"s1","title":"Dinner with Ashley","date":"2026-06-12","allDay":false},
 {"id":"s2","title":"Lunch","date":"2026-06-07","allDay":false},
 {"id":"a1","title":"DMV appointment","date":"2026-06-18","allDay":false}]

OUTPUT:
{"items":[
 {"eventId":"d1","category":"deadline","tasks":[{"kind":"prep","title":"Prep registration documents"},{"kind":"complete","title":"Renew the car registration"}]},
 {"eventId":"s1","category":"social","tasks":[{"kind":"plan","title":"Plan dinner with Ashley"}]},
 {"eventId":"a1","category":"appointment","tasks":[{"kind":"documents","title":"Bring documents to the DMV"}]}
]}

Why: Plain "Lunch" has no named person or prep signal, so it is omitted.
