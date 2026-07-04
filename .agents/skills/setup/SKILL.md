---
name: setup
description: Interactive setup wizard for `config/env.yaml`. Use when the user says "setup", "configure", "set up env", or when `config/env.yaml` is missing.
---

# Setup Wizard

Guide the user through creating or updating `config/env.yaml` — the gitignored, host-local config every `hq` command reads. Public identifiers (owner/repo/forgejo_url) live in the committed `config/project.yaml` and rarely change.

## Steps

Start from the template: `cp config/env.example.yaml config/env.yaml` (never overwrite an existing env.yaml without asking). Then walk through the sections, asking only what the template can't default:

1. **Identity** — full name, Forgejo username, IANA timezone (validate it contains `/`). `repo.name` = `<username>/<repo>`.
2. **Calendar defaults** — Zoom URL, office location, default duration.
3. **Working hours** — per-day blocks as `HH:MM-HH:MM` lists (validate the pattern); optional commute gap; `max_bookable_pct` (default 80) and `min_free_min` (default 30).
4. **Forgejo repo** — confirm `config/project.yaml` values (owner/repo/forgejo_url); verify access with `hq task config`.
5. **Gmail accounts** — for each account: name (e.g. `work`), `config_dir` (the `GOOGLE_WORKSPACE_CLI_CONFIG_DIR`; blank if this host has a single gws account at the default location), and `mail_url_index` (the account's position in the browser's Gmail switcher, for building message links).
6. **Collect + queue** — defaults are fine for most users; on the entry-point host set `queue.runners.triage.pi_args` to a small local model, on the processing host set `collect`/`full-sweep` runners and `warm_url`.

Finish by running `hq task config` to confirm the project resolves, and show the user a summary.

## Docker / other hosts

Bind-mount the file: `docker run -v /path/to/config/env.yaml:/app/config/env.yaml:ro …`. Each host keeps its own env.yaml (accounts and runners differ per host); only `config/project.yaml` is shared through git.
