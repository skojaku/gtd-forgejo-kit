---
name: triage
description: Email triage — turn unread mail into Inbox task cards and draft replies. Use when asked to "triage", "process email/inbox", or during a triage job.
---

# Triage

Goal: convert new email into (a) Inbox task cards and (b) draft replies. You never send, never archive, never mark anything done.

1. `hq mail brief --max 15` (or the message ids given in the job).
2. For each message, classify:
   - **Task** (needs real work, a deadline, or a deliverable — or carries the `tasks/inbox` label, which always means task): write `{"title": "<verb-first summary>", "notes": "From: <sender>\nEmail: <url from the brief output>", "due": "<date only if stated>"}` to a params file, then `hq task add <file>`. It lands in **Inbox** — the user approves it later; that is the whole point.
   - **Quick reply** (answerable in a few sentences): `hq mail draft-reply <message_id> "<reply text>"` — tone per `scripts/prompts/email-style.md`. This creates a draft; the user sends it.
   - **Noise** (newsletters, notifications): skip silently.
3. Finish with a short report: tasks filed (issue numbers), drafts created, items skipped.

Never: `hq task update` to any status other than Inbox, labeling or deleting email, replying without `draft-reply`.
