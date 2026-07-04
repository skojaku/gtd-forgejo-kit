# gtd-forgejo-kit

Personal GTD (Getting Things Done) and knowledge-collector system. Built
entirely on a self-hosted [Forgejo](https://forgejo.org/) instance. Issues
are tasks. Labels are GTD state. An AI agent gateway does the legwork. See
[`ARCHITECTURE.md`](ARCHITECTURE.md) for the field-mapping design.

## Core philosophy

Everything stays local. Forgejo, the CLI, the agent gateway, the job queue.
All run on your own machine or private network. Nothing leaves your
control.

The AI agent is an information collector, not a decision maker. It gathers,
links, summarizes, and drafts. You make every judgment call. Promote a
task. Book a meeting. Send an email. The CLI enforces this. There is no
send-email verb. Booking needs an explicit approval flag. The agent can
only file new work into an Inbox state.

## What's in here

- **`bin/hq`**. One CLI for everything. Tasks (`hq task`). Email (`hq
  mail`, Gmail via [`gws`](https://github.com/GAM-team/GYB)-style tooling).
  Calendar (`hq cal`). Drive (`hq drive`, read-only). Local wiki search
  (`hq wiki`). A filesystem job queue (`hq queue`) for async agent work.
- **`bin/hqlib/forgejo.py`**. A ~150-line stdlib-only (`urllib`) REST
  client for Forgejo. No `gh`, no `tea`, no extra dependencies.
- **`bin/hqlib/plugins/`**. Core (task/forgejo/queue/wiki) never imports a
  plugin; `mail`/`cal`/`drive` are plugins — each its own folder, discovered
  by name, never importing each other. Add your own the same way. See
  ARCHITECTURE.md's "Plugin model".
- **`docker/`** and **`deploy/compose.example.yaml`**. A two-container
  stack. Forgejo plus one app container running both a cron scheduler and
  an AI agent gateway. Tailscale-friendly, loopback-only port bindings.
- **`.agents/skills/`**. Short task recipes for driving an AI agent.
  Triage incoming email into task cards. Collect context into a dossier
  comment. Written for small, local models.

## Quick start

1. **Stand up Forgejo.** Copy `deploy/compose.example.yaml`. Fill in your
   domain or Tailscale IP. Run `docker compose up -d forgejo`. Create an
   admin user. Generate an API token.
2. **Configure.**
   ```bash
   cp config/project.example.yaml config/project.yaml   # owner/repo/forgejo_url
   cp config/env.example.yaml config/env.yaml            # your name, working hours, ...
   mkdir -p ~/.config/hq && echo -n "<your token>" > ~/.config/hq/forgejo-token
   chmod 600 ~/.config/hq/forgejo-token
   ```
3. **Set up `gws`** (Gmail, Calendar, Drive access). Install it. Authorize
   it. Point `env.yaml` at the resulting profile.
   ```bash
   npm install -g @googleworkspace/cli
   export GOOGLE_WORKSPACE_CLI_CONFIG_DIR=~/.config/gws-work   # one dir per account
   gws auth login                                              # opens a browser
   gws auth status                                              # confirm it worked
   ```
   Repeat with a different `GOOGLE_WORKSPACE_CLI_CONFIG_DIR` for each extra
   account (work, personal, ...). List each profile's dir under
   `gmail_triage.accounts.<name>.config_dir` in `config/env.yaml`.
4. **Build and run the app container.**
   ```bash
   docker compose -f deploy/compose.example.yaml up -d --build hq
   docker exec hq bin/hq task summary
   ```
5. **Wire up an AI agent gateway** (optional). See `scripts/hermes-setup.sh`
   for one way to do this with [Hermes Agent](https://hermes-agent.nousresearch.com).
   Adapt `docker/hermes-hq-soul.md` (the agent's system prompt) and
   `docker/entrypoint.sh` for whatever gateway you use.
6. **Nightly backups.** `deploy/backup-forgejo.sh` runs `forgejo dump` and
   copies the archive off-container. Schedule it with cron or systemd on
   the host, not inside the container, so it survives a container rebuild.

## Design notes worth reading before you customize

- Every command's JSON output is small and capped on purpose. Built to be
  driven by small, local LLMs, not just humans.
- The label, due-date, body-metadata field-mapping pattern in
  `ARCHITECTURE.md` generalizes to any custom-field system on Forgejo, or
  any issue tracker with only labels, one date field, and a body.
- `config/project.yaml` is committed. Owner, repo, forgejo_url. No secrets.
  `config/env.yaml` is gitignored. Personal working hours, accounts.
  Tokens never go here either. Tokens live in `~/.config/hq/forgejo-token`
  or the `FORGEJO_TOKEN` env var.

## License

MIT. See [`LICENSE`](LICENSE).
