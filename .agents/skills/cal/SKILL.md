---
name: cal
description: Google Calendar via `hq cal` — agenda, free slots, time checks, session proposals, booking. Use for meetings, appointments, availability, scheduling. Trigger on "calendar", "meeting", "free", "schedule", "book".
---

# Calendar

Use `hq cal --help` for exact usage; run commands directly.

All policy (working hours, commute gap, max-bookable %, min free time) is computed by the CLI from `config/env.yaml` — trust `free`/`check`/`propose` output; never recompute policy yourself.

- `hq cal agenda --days N` — upcoming events.
- `hq cal free --from D --to D` — bookable slots per day, policy-applied.
- `hq cal check --date D --start HH:MM --end HH:MM` — validate one time; surface any `warnings` to the user verbatim.
- `hq cal propose file.json` — proposed work sessions for a task/project. Proposals are suggestions: show them to the user.
- `hq cal book file.json` — creates real events. **Only after the user explicitly approves the exact sessions**; the params file must then include `"approved": true`. Never book, move, or delete events on your own initiative.

For ad-hoc event edits (rename, add attendee, delete), show the user what you intend and use `gws calendar events patch/delete` only after they confirm.
