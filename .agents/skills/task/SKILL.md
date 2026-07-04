---
name: task
description: GTD tasks on the Forgejo repo via `hq task`. Use for adding, listing, updating, deferring, or completing tasks, and for task briefs/dossiers. Trigger on "task", "todo", "inbox", "what's on my plate".
---

# Tasks

All task state lives on Forgejo issues (status/context as scoped labels, due date as the native field, everything else in an `hq-meta` body block — see `ARCHITECTURE.md`). Use `hq task --help` for exact usage; run commands directly.

Status meanings:
- **Inbox** = captured, NOT yet approved by the user. You may create Inbox tasks freely (`hq task add`).
- **Next** = user-approved and scheduled. Moving anything to Next requires the user's explicit say-so in this conversation (and duration + booked set — the CLI enforces that part).
- **Waiting / Someday / Deferred / Done** as usual in GTD.

Hard boundaries:
- Never move a task out of Inbox, mark it Done, or close an issue unless the user explicitly asked for that task.
- New tasks: verb-first title, land in Inbox. Include a source link in `notes` when the task came from an email or document.

Conventions:
- Commands taking several values use a JSON params file (`hq task add params.json`).
- `hq task list` is capped and shows a short projection; add `--full` only when you need every field.
- `hq task brief <n>` / `hq task dossier <n> file.json` are the collect-loop bookends — see the `collect` skill.
