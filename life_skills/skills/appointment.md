# Skill: appointment

Allowed kinds: `prep`, `documents`, `confirm`.
- **prep** — prepare questions, notes, symptoms, goals, study material, or talking
  points.
- **documents** — gather IDs, insurance, forms, records, paperwork, permits, or
  payment info.
- **confirm** — confirm the slot, address, instructions, or reschedule if needed.

Guidance:
- Usually emit one task.
- Medical/dental/therapy/vet: `prep` or `confirm`; add `documents` for new
  patient, specialist, insurance, forms, or records.
- DMV/court/passport/license/immigration/school/financial appointments: strongly
  consider `documents`.
- Interview/exam/test/consultation: prefer `prep`.
- Repair/inspection/service appointments: prefer `confirm`.
- Routine work meetings, 1:1s, standups, reviews, and syncs are ignore, not appointment.

Titles: "Prep questions for the doctor", "Bring documents to the DMV", "Confirm the dentist appointment".
