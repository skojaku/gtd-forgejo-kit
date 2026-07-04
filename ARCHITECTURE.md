# Architecture

A personal GTD/knowledge system built entirely on a self-hosted Forgejo
instance. The core design problem: Forgejo has no GraphQL API and no custom
project fields (nothing like GitHub Projects V2's Status/Context/Due/Defer/
Scheduled/Booked/Duration). So: **how do you get a rich, multi-field GTD
state machine using only what a plain Forgejo issue gives you — labels, one
due date, and a Markdown body?**

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
`"exclusive": true`.

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
bin/hqlib/                   CORE — never imports a plugin
  forgejo.py                 Stdlib-only REST client (urllib) for Forgejo's API
  task.py                    GTD command surface: add/list/update/done/defer/
                              comment/cron-daily/cron-cleanup — all built on
                              forgejo.py + the label/due_date/meta pattern above
  common.py                  Config loading + the read_meta/write_meta helpers
  queue.py                   Filesystem job queue for async agent work; core
                              handles "collect" jobs directly, offers every
                              other job type to each plugin's handle_job()
                              hook before falling back to the generic runner
  wiki.py                    Local Markdown note search + Forgejo blob links
  dossier.py                 Posts agent-collected context as issue comments
  plugins/                   Plugin loader (see "Plugin model" below)
    mail/ cal/ drive/        Gmail / Calendar / Drive connectors (via `gws`) —
                              each its own folder, each may import core, never
                              another plugin
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

## Plugin model

Core (`task.py`, `forgejo.py`, `common.py`, `dossier.py`, `queue.py`,
`wiki.py`) never imports a plugin. Everything source-specific — Gmail,
Calendar, Drive, or whatever you add — is a plugin: a folder under
`bin/hqlib/plugins/<name>/`, discovered by folder name, contributing its own
`hq <name> ...` subcommand. This localizes a broken/half-finished connector
to itself; it can't take down the CLI or another plugin.

Contract (`bin/hqlib/plugins/__init__.py` has the full docstring):
- `NAME`: must match the folder name.
- `register(subparsers)`: required — wires the `hq <name> ...` subcommand.
- `scan(cfg, state, qdir, taken) -> list[str]`: optional — called by
  `hq queue scan` for plugins that detect their own events (new mail, etc).
- `handle_job(job_type, cfg, payload, log, job_name) -> (ok, err) | None`:
  optional — called by `hq queue work` for job types this plugin created via
  its own `scan()`; return `None` to decline (falls through to the generic
  prompt-driven runner — this is why "triage" needs no plugin-specific code
  at all, just a prompt template).

A plugin may import from `hqlib.common` / `hqlib.forgejo` / `hqlib.queue`
(core) — never from another plugin. If two plugins need to share something
(the `mail` and `drive` plugins both need gws account resolution), that
shared piece belongs in `common.py`, not in one plugin importing the other.

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

## Worker model

Heavy jobs (dossier collection against a big local model) are pulled from a
filesystem job queue on the master by whichever worker claims them first —
not pushed by the master to a fixed target. Any number of worker machines
can run `hq queue work --remote <master-ssh-alias>` on their own schedule
(cron/launchd/systemd); each just needs the `hq` CLI, a `FORGEJO_TOKEN` (or
token file), its own local model, and ssh reachability to the master. The
master doesn't need to know how many workers exist — adding one is zero
master-side change.

Priority is just claim order/frequency, not a strict failover chain: a
preferred worker polls often; a lower-priority one (e.g. the master's own
GPU, `scripts/master-local-worker.sh` + `scripts/gpu-check.sh`) only claims
a job once it's aged past a threshold *and* that worker's own GPUs are
free — it stays out of the way otherwise. Give every lower-priority worker
the *same* flat threshold rather than chaining thresholds per worker, so
worst-case wait stays bounded as you add more workers instead of compounding.

Optionally, split the master's own local worker into its own container
(`hq-worker` in `deploy/compose.example.yaml`, same image, `HQ_ROLE=worker`
skips the agent gateway, runs `docker/crontab.worker` instead of
`docker/crontab`) — symmetric with how a remote worker operates, just local
instead of over ssh.

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
