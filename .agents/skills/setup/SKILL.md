---
name: setup
description: Interactive setup wizard for `config/hq.yaml`. Use when the user says "setup", "configure", "set up env", or when `config/hq.yaml` is missing.
---

# Setup Wizard

You are a thin wrapper around the built-in `hq setup` command. It does the real work
(writes config, detects UID/GID/TZ, checks reachability, and on Tier 2 bootstraps
Forgejo). Your job is to ask the human the questions it needs and run it for them.

`config/hq.yaml` is the gitignored, host-local config every `hq` command reads. The
tracked template is `config/hq.example.yaml`. There is no separate `project.yaml` and no
GitHub anymore — the backend is Forgejo, and `forgejo_url` is personal, so it lives in
`hq.yaml` too.

## Pick the tier

1. **CLI only** — a laptop that just talks to an existing Forgejo over the tailnet.
2. **Server stack** — the always-on host running docker compose (forgejo + ollama +
   hq-cron). This tier also bootstraps Forgejo (admin, repo, API token, GTD labels).
3. **Extra worker** — another machine that claims collect jobs from the hub's Forgejo.

## Run it

Interactive: `./bin/hq setup` (prompts on stdin, prints the compose/ollama commands to
run afterwards). Ask the human for the values it needs and pass them as flags when you
already know them:

- `--tier {1,2,3}`
- `--username`, `--repo` (Forgejo `owner/name` slug), `--timezone` (must contain `/`)
- `--forgejo-url` (base URL of the Forgejo instance)
- Tier 2 only: `--bind-ip`, `--domain`, `--admin-user`, `--admin-password`, and either
  `--forgejo-token` (reuse an existing token) or `--token-name` (mint one).

`hq setup` never overwrites an existing `config/hq.yaml`/`.env` unless you pass `--force`
— confirm with the user before forcing.

## After setup

- Fill any placeholders the wizard reported in `config/hq.yaml` (e.g. per-account gws
  `config_dir` and `mail_url_index` under `google.accounts`, working-hour blocks).
- Run `./bin/hq doctor` — it checks binaries, config keys, token validity, Forgejo/ollama
  reachability, and container/cron freshness, and prints a FIX line per failure. Walk the
  user through anything red.
- Per-plugin scaffolding without the full wizard: `./bin/hq install <mail|cal|drive|discord|core|all>`.

## Docker / other hosts

Bind-mount the config: `docker run -v /path/to/config/hq.yaml:/hq/config/hq.yaml:ro …`.
Each host keeps its own `hq.yaml` (accounts and runners differ per host).
