---
name: mail
description: Gmail via `hq mail` — search, read, label, and draft (never send). Use for anything involving email. Trigger on "email", "inbox", "reply", "message from".
---

# Mail

Use `hq mail --help` for exact usage; run commands directly. Default account is `work`.

Drafts only: this CLI **cannot send email** — `hq mail draft-reply` and `hq mail draft` always create Gmail drafts the user reviews and sends themselves. Tell the user where the draft is; never claim something was sent.

Guidance:
- Prefer `hq mail find` with structured flags (`--from`, `--subject`, `--text`, `--newer-than`) over broad scans — one precise query beats many wide ones.
- `hq mail brief` is the one-shot triage input: unread messages hydrated with previews.
- `hq mail read <id>` caps the body; add `--full` only when the capped body is genuinely not enough.
- Search results include a clickable `url` per message — use it whenever you cite an email.
- Draft tone: follow `scripts/prompts/email-style.md`.
