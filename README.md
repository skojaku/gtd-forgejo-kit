# gtd-forgejo-kit

A personal GTD (Getting Things Done) / knowledge-collector system, built
entirely on a self-hosted [Forgejo](https://forgejo.org/) instance: issues
are tasks, labels are GTD state, and an AI agent gateway does the legwork.
See [`ARCHITECTURE.md`](ARCHITECTURE.md) for how the field-mapping design
works (labels + one date field + a body comment block, replacing what a
tool like GitHub Projects would give you natively).

**Core philosophy: the AI agent is an information collector, not a decision
maker.** It gathers, links, summarizes, and drafts; you make every judgment
call (promote a task, book a meeting, send an email). The CLI enforces this
— there is no "send email" verb, booking requires an explicit approval
flag, and the agent can only file new work into an Inbox state.

## What's in here

- **`bin/hq`** — one CLI for everything: tasks (`hq task`), email
  (`hq mail`, Gmail via [`gws`](https://github.com/GAM-team/GYB)-style
  tooling), calendar (`hq cal`), Drive (`hq drive`, read-only), local wiki
  search (`hq wiki`), and a filesystem job queue (`hq queue`) for async
  agent work.
- **`bin/hqlib/forgejo.py`** — a ~150-line stdlib-only (`urllib`) REST
  client for Forgejo. No `gh`, no `tea`, no extra dependencies.
- **`scripts/migrate/`** — a 4-script, idempotent, `--dry-run`-capable
  pipeline for migrating an existing GitHub Projects V2 board's field
  values onto Forgejo labels/due-dates/body-metadata.
- **`docker/`** + **`deploy/compose.example.yaml`** — a two-container stack
  (Forgejo + one app container running both a cron scheduler and an AI
  agent gateway) with Tailscale-friendly, loopback-only port bindings.
- **`.agents/skills/`** — short task recipes for driving an AI agent
  (triage incoming email into task cards, collect context into a dossier
  comment, etc.), written for small/local models.

## Quick start

1. **Stand up Forgejo.** Copy `deploy/compose.example.yaml`, fill in your
   domain/Tailscale IP, `docker compose up -d forgejo`, create an admin
   user, generate an API token.
2. **Configure.**
   ```bash
   cp config/project.example.yaml config/project.yaml   # owner/repo/forgejo_url
   cp config/env.example.yaml config/env.yaml            # your name, working hours, ...
   mkdir -p ~/.config/hq && echo -n "<your token>" > ~/.config/hq/forgejo-token
   chmod 600 ~/.config/hq/forgejo-token
   ```
3. **(Optional) Migrate an existing GitHub Projects V2 board** — see
   `ARCHITECTURE.md`'s migration section and `scripts/migrate/`.
4. **Build and run the app container.**
   ```bash
   docker compose -f deploy/compose.example.yaml up -d --build hq
   docker exec hq bin/hq task summary
   ```
5. **Wire up an AI agent gateway** (optional) — see
   `scripts/hermes-setup.sh` for one way to do this with
   [Hermes Agent](https://hermes-agent.nousresearch.com); adapt
   `docker/hermes-hq-soul.md` (the agent's system prompt) and
   `docker/entrypoint.sh` for whatever gateway you use.
6. **Nightly backups.** `deploy/backup-forgejo.sh` runs `forgejo dump` and
   copies the archive off-container; schedule it with cron/systemd on the
   host (not inside the container, so it survives a container rebuild).

## Design notes worth reading before you customize

- Every command's JSON output is deliberately small and capped — this is
  built to be driven by small/local LLMs, not just humans.
- The label/due-date/body-metadata field-mapping pattern in
  `ARCHITECTURE.md` generalizes to any custom-field system you're trying to
  replicate on Forgejo (or any issue tracker with only labels + one date
  field + a body).
- `config/project.yaml` is committed (owner/repo/forgejo_url — no secrets);
  `config/env.yaml` is gitignored (personal working hours, accounts, tokens
  never go here either — tokens live in `~/.config/hq/forgejo-token` or the
  `FORGEJO_TOKEN` env var).

## License

MIT — see [`LICENSE`](LICENSE).
