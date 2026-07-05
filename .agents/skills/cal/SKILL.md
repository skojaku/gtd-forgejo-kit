---
name: cal
description: Google Calendar via `hq cal` — agenda, free slots, time checks, and session proposals. Use for meetings, appointments, availability, scheduling. Trigger on "calendar", "meeting", "free", "schedule".
---

# Calendar

Use `hq cal --help` for exact usage; run commands directly. This CLI is read + propose only — it never writes to the calendar.

All policy (working hours, commute gap, max-bookable %, min free time) is computed by the CLI from `config/hq.yaml` — trust `free`/`check`/`propose` output; never recompute policy yourself.

- `hq cal agenda --days N` — upcoming events.
- `hq cal free --from D --to D` — bookable slots per day, policy-applied.
- `hq cal check --date D --start HH:MM --end HH:MM` — validate one time; surface any `warnings` to the user verbatim.
- `hq cal propose file.json` — propose work sessions from `{from, to, minutes, min_session?}`. Proposals are suggestions only: show them to the user, they book what they want.

To actually create/edit an event (book a session, rename, add attendee, delete), show the user what you intend and use `gws calendar events insert/patch/delete` only after they confirm — never on your own initiative.
