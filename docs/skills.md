# HQ Skills

Canonical skills live in `.agents/skills/` (`.claude/skills/` symlinks to them). Routing and hard boundaries are in [AGENTS.md](../AGENTS.md). All mechanics go through the unified CLI — run `hq <domain> --help`.

## Core skills

| Skill | What it covers |
|---|---|
| [task](../.agents/skills/task/SKILL.md) | GTD tasks on the Forgejo repo: add/list/update, context/dossier bookends. Inbox = unapproved. |
| [mail](../.agents/skills/mail/SKILL.md) | Gmail search/show/label + drafts. The CLI cannot send email. |
| [cal](../.agents/skills/cal/SKILL.md) | Calendar agenda/free/check/propose. Read + propose only; the CLI never writes to the calendar. |
| [drive](../.agents/skills/drive/SKILL.md) | Read-only Drive search + Google Doc excerpts (`drive show`). |
| [collect](../.agents/skills/collect/SKILL.md) | The dossier recipe: context → search sources → post one marked comment with links + excerpts. |
| [triage](../.agents/skills/triage/SKILL.md) | Email → Inbox task cards + draft replies. Never sends, never promotes. |
| [wiki](../.agents/skills/wiki/SKILL.md) | Zettelkasten editing rules (inbox/pieces/hubs/literature, tags). Search via `hq wiki find`. |
| [setup](../.agents/skills/setup/SKILL.md) | Wizard for the host-local `config/hq.yaml` (thin wrapper around `hq setup`). |

## Folder-scoped skills

These stay next to the content they manage and are picked up when working under those directories:

- `logs/.claude/skills/` — checkin, meeting, weekly-review
- `people/.claude/skills/` — new-person
- `roadmap/.claude/skills/` — checkpoint
- `wiki/.claude/skills/` — promote, new-hub, new-literature

## Automation prompts

Headless jobs run by `hq queue work` use the templates in `scripts/prompts/`. Two mechanisms, matched to model capability: **triage** (`triage.md`, one message per job) is a single direct ollama structured-output call on a small model — no agent; **collect** (`collect.md`, one issue per job) runs a hermes agent loop on a large local model, which writes a JSON file that the runner validates and posts as the dossier deterministically. The daily backstop expands into per-issue collect jobs at scan time.
