# Architecture

A personal GTD/knowledge system built on a self-hosted Forgejo instance
instead of GitHub. Originated as a migration off GitHub Projects V2 (which
has no self-hostable equivalent — Forgejo has no GraphQL API and no custom
project fields), so the core design problem this repo solves is: **how do
you replicate GitHub Projects V2's custom fields (Status, Context, Due,
Defer, Scheduled, Booked, Duration, ...) using only what a plain Forgejo
issue gives you?**

## The field-mapping pattern

Forgejo issues natively support: labels, one `due_date`, and free-text
Markdown bodies/comments. That's the entire surface. This repo maps GTD
fields onto it as follows — reuse this pattern for your own field set:

### Single-select fields -> exclusive scoped labels

Forgejo supports *exclusive scoped labels*: a label named `scope/value` with
`exclusive: true` — adding one automatically removes any sibling in the same
scope (enforced server-side, both UI and API). This is a clean 1:1
replacement for a GitHub Projects single-select field.

```
status/inbox   status/next   status/review   status/done
status/deferred   status/waiting   status/someday

context/computer   context/writing   context/errands
context/calls   context/reading
```

Create via `POST /api/v1/repos/{owner}/{repo}/labels` with
`"exclusive": true`. See `scripts/migrate/create-labels.py` for a template.

### One date field -> native `due_date`

Forgejo issues have exactly one native date field. Use it for whichever of
your fields matters most for at-a-glance UI visibility (this repo uses it
for "Due"). `PATCH /repos/{owner}/{repo}/issues/{n}` with
`{"due_date": "2026-07-10T00:00:00Z"}`. Shows in the Forgejo UI with overdue
highlighting — everything else does not.

### Every other field -> a machine-readable body block

Every other date/number field goes into a single-line HTML comment appended
to the issue body (invisible when rendered):

```
<!-- hq-meta {"defer": "2026-07-10", "scheduled": null, "duration": 30, "booked": "2026-07-08"} -->
```

Rules: null/absent keys omitted from the JSON; the whole block omitted if
every tracked key is empty; always the last line of the body; found/replaced
with the regex `^<!-- hq-meta (\{.*\}) -->$` (multiline). See
`bin/hqlib/common.py`'s `read_meta()`/`write_meta()`.

**Trade-off you're accepting**: these fields are invisible in the Forgejo
web UI (visible only via the CLI or the raw body). If you need UI
visibility for more than one field, you only get to pick one (the
`due_date`) — plan your field set accordingly.

## Components

```
bin/hq                       Unified CLI entry point (hq <domain> --help)
bin/hqlib/
  forgejo.py                 Stdlib-only REST client (urllib) for Forgejo's API
  task.py                    GTD command surface: add/list/update/done/defer/
                              comment/cron-daily/cron-cleanup — all built on
                              forgejo.py + the label/due_date/meta pattern above
  common.py                  Config loading + the read_meta/write_meta helpers
  mail.py / cal.py / drive.py  Gmail / Calendar / Drive integration (via `gws`)
  queue.py                   Filesystem job queue for async agent work
  wiki.py                    Local Markdown note search + Forgejo blob links
  dossier.py                 Posts agent-collected context as issue comments
docker/
  entrypoint.sh               Sets up Forgejo git credentials, launches an
                               AI agent gateway in a background restart loop,
                               execs supercronic in the foreground
  crontab                     Replaces GitHub Actions cron: queue scanning,
                               the Discord mirror, cron-daily/cron-cleanup
  hermes-hq-soul.md            System prompt template for a Hermes Agent
                               gateway acting as your "information collector"
scripts/
  discord-issue-sync.py       Mirrors "Next"-labeled issues to Discord threads
  migrate/                    One-shot scripts for migrating an existing
                               GitHub Projects V2 board onto Forgejo (below)
  hermes-setup.sh              Provisions isolated Hermes profiles/brains
  hq-runner.sh / hq-kill.sh    Example remote-worker + teardown scripts
deploy/
  compose.example.yaml         Forgejo + app container, Tailscale-friendly
                               port bindings (loopback + private network only)
  backup-forgejo.sh             Nightly `forgejo dump` to a second disk
config/
  project.example.yaml         Committed owner/repo/forgejo_url (copy to
                               project.yaml)
  env.example.yaml             Gitignored personal config (copy to env.yaml)
  launchd/ , systemd/           Scheduler units for a remote worker machine
.agents/skills/                Short task recipes for an AI agent (routed by
                               AGENTS.md) — task, mail, cal, drive, collect,
                               triage, wiki, setup
```

## Container model

One container runs both the cron scheduler (`supercronic`) and an AI agent
gateway, as a background process with an auto-restart loop, so a crashed
agent doesn't take down the cron jobs:

```bash
( while true; do
    your-agent-gateway-command 2>&1 | sed 's/^/[agent] /'
    echo "[agent] exited ($?), restarting in 5s"; sleep 5
  done ) &
exec supercronic /path/to/docker/crontab
```

Forgejo runs as a second container. Bind its ports to loopback + a private
network only (Tailscale, WireGuard, ...) — never `0.0.0.0` — since it now
holds all your task/note data. See `deploy/compose.example.yaml`.

## Migrating an existing GitHub Projects V2 board

`scripts/migrate/` is a 4-step, idempotent, `--dry-run`-capable pipeline:

1. **`snapshot-fields.sh`** — while GitHub is still live, dump every
   project item's field values to `fields.json` via the Projects V2 GraphQL
   API. Read-only.
2. **`create-labels.py`** — create your exclusive scoped labels on the
   target Forgejo repo.
3. **`apply-fields.py`** — for each `fields.json` entry: swap in the right
   scoped labels, set `due_date`, write the meta body block.
4. **`verify-migration.py`** — asserts open/closed issue counts match
   between GitHub and Forgejo, every snapshotted issue exists with a
   matching title, and status-labeled counts add up; prints a few random
   issues for a manual spot-check.

Run the actual GitHub -> Forgejo repo migration itself via Forgejo's
built-in GitHub importer (`POST /repos/migrate` or the web UI's "New
Migration" — imports issues, comments, labels, milestones, PRs, and
preserves issue numbers) *before* step 3, so `apply-fields.py` has real
issues to attach labels to.

## Cron replacement

GitHub Actions workflows become `hq task cron-daily` / `hq task
cron-cleanup` subcommands run by `supercronic` inside the container, on the
container's local timezone (no more UTC-vs-local drift math). Both support
`--dry-run` and `--today YYYY-MM-DD` for testing.

## What you don't get

- No kanban board UI (closest substitute: a bookmarked, label-filtered issue
  list URL).
- Every field except one is invisible in the web UI.
- If you were relying on GitHub's rate limits shaping your architecture
  (e.g. "one GraphQL call per run"), you can drop that constraint entirely
  — a self-hosted instance has no meaningful rate limit.
