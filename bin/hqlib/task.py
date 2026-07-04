"""hq task — GTD task management on the self-hosted Forgejo repo.

State that used to live in GitHub Projects V2 custom fields now lives in:
  - exclusive scoped labels `status/<value>` and `context/<value>`
  - the native issue `due_date`
  - a trailing `<!-- hq-meta {...} -->` body block for defer/scheduled/
    duration/booked (see PLAN.md section 1 and hqlib/common.py read_meta/
    write_meta)
  - `proj:<name>` labels, unchanged, for project grouping

The issue number is the only identifier now (no more Projects V2 item id).
Callers (triage prompt, Hermes agent, dossier.py, queue.py) see the same
subcommands and the same JSON shapes as the old gh/GraphQL implementation.
"""
from datetime import date

from .common import (
    fail, load_config, owner, repo_name, forgejo_url,
    read_meta, write_meta, envelope, print_json, read_params, is_valid_date,
    config_path,
)
from .forgejo import ForgejoClient

STATUS_VALUES = ("inbox", "next", "review", "done", "deferred", "waiting", "someday")
CONTEXT_VALUES = ("computer", "writing", "errands", "calls", "reading")


def _client(cfg):
    return ForgejoClient(forgejo_url(cfg), owner(cfg), repo_name(cfg))


def _norm_status(value):
    return value.strip().lower() if value else None


def _norm_context(value):
    return value.strip().lstrip("@").lower() if value else None


def _status_display(value):
    return value.capitalize() if value else None


def _context_display(value):
    return f"@{value}" if value else None


# --------------------------------------------------------------------------
# Task fetch — derive the flat task shape from one issue's labels/due/body
# --------------------------------------------------------------------------

def _task_from_issue(issue_data):
    labels = [l["name"] for l in (issue_data.get("labels") or [])]
    status = context = project = None
    for lbl in labels:
        if lbl.startswith("status/"):
            status = _status_display(lbl.split("/", 1)[1])
        elif lbl.startswith("context/"):
            context = _context_display(lbl.split("/", 1)[1])
        elif lbl.startswith("proj:"):
            project = lbl[len("proj:"):]

    meta = read_meta(issue_data.get("body"))
    due = issue_data.get("due_date")
    due = due[:10] if due else None

    return {
        "issue": issue_data.get("number"),
        "title": issue_data.get("title"),
        "url": issue_data.get("html_url"),
        "state": (issue_data.get("state") or "").upper(),
        "labels": labels,
        "project": project,
        "status": status,
        "due": due,
        "context": context,
        "defer": meta.get("defer"),
        "scheduled": meta.get("scheduled"),
        "duration": meta.get("duration"),
        "booked": meta.get("booked"),
    }


def fetch_tasks(cfg):
    client = _client(cfg)
    return [_task_from_issue(i) for i in client.list_issues(state="all")]


def find_task(cfg, issue):
    """Single-issue lookup (no more full-project scan needed with REST)."""
    client = _client(cfg)
    try:
        issue_data = client.get_issue(issue)
    except RuntimeError:
        return None
    return _task_from_issue(issue_data)


def _require_issue(cfg, issue):
    """Like find_task, but returns the raw issue payload too (for body edits)
    and fail()s with a clear message if the issue doesn't exist."""
    client = _client(cfg)
    try:
        issue_data = client.get_issue(issue)
    except RuntimeError as e:
        fail(f"issue #{issue} not found: {e}")
    return issue_data, _task_from_issue(issue_data)


# --------------------------------------------------------------------------
# Label helpers
# --------------------------------------------------------------------------

def ensure_label(cfg, name, color="0075ca", exclusive=False):
    """Idempotent label create — used for dynamic `proj:` labels and by
    dossier.py for the needs-decision label. Tolerates "already exists"."""
    client = _client(cfg)
    try:
        client.create_label(name, color, exclusive=exclusive)
    except RuntimeError as e:
        if "already exists" not in str(e).lower():
            fail(f"could not create label {name}: {e}")


def ensure_project_label(cfg, name):
    ensure_label(cfg, f"proj:{name}")


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

LIST_FIELDS = ("issue", "title", "status", "due", "project")


def cmd_config(args):
    cfg = load_config()
    print_json({
        "config_path": str(config_path()),
        "owner": owner(cfg),
        "repo": repo_name(cfg),
        "forgejo_url": forgejo_url(cfg),
        "status_values": list(STATUS_VALUES),
        "context_values": list(CONTEXT_VALUES),
        "working_hours": cfg.get("working_hours", {}),
        "timezone": cfg.get("user", {}).get("timezone"),
    })


def cmd_list(args):
    cfg = load_config()
    tasks = fetch_tasks(cfg)
    if args.status:
        tasks = [t for t in tasks if (t["status"] or "").lower() == args.status.lower()]
    if args.project:
        tasks = [t for t in tasks if (t["project"] or "").lower() == args.project.lower()]
    if args.context:
        want_context = args.context.lower().lstrip("@")
        tasks = [t for t in tasks if (t["context"] or "").lower().lstrip("@") == want_context]
    if args.overdue:
        today = date.today().isoformat()
        tasks = [t for t in tasks if t["due"] and t["due"] < today and t["status"] != "Done"]
    total = len(tasks)
    shown = tasks[: args.max]
    if not args.full:
        shown = [{k: t[k] for k in LIST_FIELDS} for t in shown]
    print_json(envelope(shown, total))


def cmd_show(args):
    cfg = load_config()
    t = find_task(cfg, args.issue)
    if not t:
        fail(f"issue #{args.issue} not found")
    print_json(t)


def cmd_add(args):
    cfg = load_config()
    params = read_params(args.file)

    title = (params.get("title") or "").strip()
    if not title:
        fail("params file must have a non-empty 'title'")
    duration = params.get("duration")
    if duration is not None and (not isinstance(duration, int) or duration <= 0):
        fail("'duration' must be a positive integer (minutes) or omitted")
    due = params.get("due")
    if due is not None and not is_valid_date(due):
        fail(f"'due' must be YYYY-MM-DD, got: {due}")
    project = params.get("project")
    notes = params.get("notes") or ""

    client = _client(cfg)

    if project:
        ensure_project_label(cfg, project)

    labels = ["status/inbox"]
    if project:
        labels.append(f"proj:{project}")

    issue = client.create_issue(title, body=notes, labels=labels)
    issue_number = issue.get("number")
    url = issue.get("html_url")

    # The issue exists from here on — never hide its number behind a later
    # failure, or retries will file duplicates.
    result = {
        "issue": issue_number, "url": url, "title": title,
        "duration": duration, "due": due, "project": project,
        "status": "Inbox",
    }
    try:
        fields = {}
        if due:
            fields["due_date"] = f"{due}T00:00:00Z"
        if duration:
            fields["body"] = write_meta(notes, {"duration": duration})
        if fields:
            client.edit_issue(issue_number, **fields)
    except RuntimeError as e:
        result["warning"] = f"issue created but due/duration not fully applied: {e}"
    print_json(result)


def cmd_update(args):
    cfg = load_config()
    params = read_params(args.file)
    client = _client(cfg)
    issue_data, current = _require_issue(cfg, args.issue)

    # Booking gate: only "next" requires Duration + Booked already known (or being set now).
    if "status" in params and _norm_status(params["status"]) == "next":
        final_duration = params.get("duration", current["duration"])
        final_booked = params.get("booked", current["booked"])
        if not final_duration or not final_booked:
            fail(
                "cannot move to Next: Duration and Booked must both be set. "
                f"duration={final_duration!r} booked={final_booked!r}. "
                "Set 'duration' and 'booked' in this same update, or run 'hq cal book' first."
            )

    for key in ("due", "defer", "scheduled", "booked"):
        if key in params:
            v = params[key]
            if v is not None and not is_valid_date(v):
                fail(f"'{key}' must be YYYY-MM-DD or null, got: {v}")

    if "duration" in params:
        v = params["duration"]
        if v is not None and (not isinstance(v, int) or v <= 0):
            fail("'duration' must be a positive integer or null")

    fields = {}

    if "due" in params:
        due = params["due"]
        if due:
            fields["due_date"] = f"{due}T00:00:00Z"
        else:
            # Forgejo ignores due_date: null on PATCH /issues/{n}; clearing
            # requires the dedicated unset_due_date flag.
            fields["unset_due_date"] = True

    meta_changed = any(k in params for k in ("defer", "scheduled", "duration", "booked"))
    notes_changed = "notes" in params
    if meta_changed or notes_changed:
        prose = params["notes"] if notes_changed else (issue_data.get("body") or "")
        merged_meta = {
            "defer": params.get("defer", current["defer"]),
            "scheduled": params.get("scheduled", current["scheduled"]),
            "duration": params.get("duration", current["duration"]),
            "booked": params.get("booked", current["booked"]),
        }
        fields["body"] = write_meta(prose, merged_meta)

    if "title" in params and params["title"]:
        fields["title"] = params["title"]

    done_requested = "status" in params and _norm_status(params["status"]) == "done"
    if done_requested:
        fields["state"] = "closed"

    if fields:
        client.edit_issue(args.issue, **fields)

    if "status" in params:
        sv = _norm_status(params["status"])
        if sv:
            client.replace_scoped(args.issue, "status", sv)
        else:
            existing = next((l for l in current["labels"] if l.startswith("status/")), None)
            if existing:
                client.remove_label(args.issue, existing)

    if "context" in params:
        cv = _norm_context(params["context"])
        if cv:
            client.replace_scoped(args.issue, "context", cv)
        else:
            existing = next((l for l in current["labels"] if l.startswith("context/")), None)
            if existing:
                client.remove_label(args.issue, existing)

    if "project" in params and params["project"]:
        ensure_project_label(cfg, params["project"])
        client.add_labels(args.issue, [f"proj:{params['project']}"])

    # Echo the merged result instead of re-fetching the whole issue.
    updated = dict(current)
    for key in ("due", "defer", "scheduled", "booked", "duration"):
        if key in params:
            updated[key] = params[key]
    if "status" in params:
        updated["status"] = _status_display(_norm_status(params["status"]))
        if done_requested:
            updated["state"] = "CLOSED"
    if "context" in params:
        updated["context"] = _context_display(_norm_context(params["context"]))
    if "title" in params and params["title"]:
        updated["title"] = params["title"]
    if "project" in params and params["project"]:
        updated["project"] = params["project"]
        label = f"proj:{params['project']}"
        if label not in (updated.get("labels") or []):
            updated["labels"] = (updated.get("labels") or []) + [label]
    print_json(updated)


def cmd_done(args):
    cfg = load_config()
    client = _client(cfg)
    _require_issue(cfg, args.issue)
    client.replace_scoped(args.issue, "status", "done")
    client.edit_issue(args.issue, state="closed")
    print_json({"issue": args.issue, "status": "Done", "closed": True})


def cmd_defer(args):
    cfg = load_config()
    if not is_valid_date(args.until):
        fail(f"'until' must be YYYY-MM-DD, got: {args.until}")
    client = _client(cfg)
    issue_data, current = _require_issue(cfg, args.issue)
    merged_meta = {
        "defer": args.until,
        "scheduled": current["scheduled"],
        "duration": current["duration"],
        "booked": current["booked"],
    }
    client.edit_issue(args.issue, body=write_meta(issue_data.get("body") or "", merged_meta))
    client.replace_scoped(args.issue, "status", "deferred")
    print_json({"issue": args.issue, "status": "Deferred", "defer": args.until})


def cmd_comment(args):
    cfg = load_config()
    client = _client(cfg)
    client.create_comment(args.issue, args.text)
    print_json({"issue": args.issue, "commented": True})


def cmd_projects_list(args):
    cfg = load_config()
    client = _client(cfg)
    labels = client.list_labels()
    projects = sorted(l["name"][len("proj:"):] for l in labels if l["name"].startswith("proj:"))
    print_json(projects)


def cmd_projects_add(args):
    cfg = load_config()
    ensure_project_label(cfg, args.name)
    print_json({"project": args.name, "created": True})


def cmd_summary(args):
    cfg = load_config()
    tasks = fetch_tasks(cfg)
    counts = {}
    for t in tasks:
        key = t["status"] or "No Status"
        counts[key] = counts.get(key, 0) + 1
    print_json({"counts": counts, "total": len(tasks)})


# --------------------------------------------------------------------------
# Cron commands (ports of the old gtd-daily.yml / gtd-cleanup.yml workflows)
# --------------------------------------------------------------------------

def _today(args):
    today = args.today or date.today().isoformat()
    if not is_valid_date(today):
        fail(f"--today must be YYYY-MM-DD, got: {today}")
    return today


def cmd_cron_daily(args):
    cfg = load_config()
    today = _today(args)
    client = _client(cfg)
    tasks = fetch_tasks(cfg)
    actions = []

    for t in tasks:
        issue = t["issue"]
        status = (t["status"] or "").lower()
        booked_ok = bool(t["duration"] and t["booked"])

        # 1. Un-defer: status/deferred AND defer <= today.
        if status == "deferred" and t["defer"] and t["defer"] <= today:
            if booked_ok:
                actions.append({"issue": issue, "action": "undefer", "to": "inbox"})
                if not args.dry_run:
                    issue_data = client.get_issue(issue)
                    new_meta = {
                        "defer": None,
                        "scheduled": t["scheduled"],
                        "duration": t["duration"],
                        "booked": t["booked"],
                    }
                    client.edit_issue(issue, body=write_meta(issue_data.get("body") or "", new_meta))
                    client.replace_scoped(issue, "status", "inbox")
            else:
                actions.append({"issue": issue, "action": "skip", "reason": "not booked (undefer)"})
            continue

        # 2. Schedule: not done/deferred AND scheduled == today.
        if status not in ("done", "deferred") and t["scheduled"] == today:
            if booked_ok:
                actions.append({"issue": issue, "action": "schedule", "to": "next"})
                if not args.dry_run:
                    client.replace_scoped(issue, "status", "next")
            else:
                actions.append({"issue": issue, "action": "skip", "reason": "not booked (schedule)"})

    print_json({"today": today, "dry_run": args.dry_run, "actions": actions})


def cmd_cron_cleanup(args):
    cfg = load_config()
    today = _today(args)
    client = _client(cfg)
    tasks = fetch_tasks(cfg)
    actions = []

    for t in tasks:
        issue = t["issue"]
        status = (t["status"] or "").lower()

        if t["state"] == "OPEN" and status == "done":
            actions.append({"issue": issue, "action": "close"})
            if not args.dry_run:
                client.edit_issue(issue, state="closed")
        elif t["state"] == "CLOSED" and status != "done":
            actions.append({"issue": issue, "action": "label-done"})
            if not args.dry_run:
                client.replace_scoped(issue, "status", "done")

    print_json({"today": today, "dry_run": args.dry_run, "actions": actions})


def register(sub):
    p = sub.add_parser("task", help="GTD tasks on the Forgejo repo")
    s2 = p.add_subparsers(dest="task_cmd", required=True)

    s2.add_parser("config", help="show resolved config").set_defaults(func=cmd_config)
    s2.add_parser("summary", help="status counts across all tasks").set_defaults(func=cmd_summary)

    s = s2.add_parser("list", help="list tasks (capped; --full for all fields)")
    s.add_argument("--status")
    s.add_argument("--project")
    s.add_argument("--context")
    s.add_argument("--overdue", action="store_true")
    s.add_argument("--max", type=int, default=10)
    s.add_argument("--full", action="store_true", help="all fields instead of the short projection")
    s.set_defaults(func=cmd_list)

    s = s2.add_parser("show", help="show one task by issue number")
    s.add_argument("issue", type=int)
    s.set_defaults(func=cmd_show)

    s = s2.add_parser("add", help="create a task from a JSON params file: {title, notes?, due?, project?, duration?} — lands in Inbox")
    s.add_argument("file")
    s.set_defaults(func=cmd_add)

    s = s2.add_parser("update", help="update fields from a JSON params file")
    s.add_argument("issue", type=int)
    s.add_argument("file")
    s.set_defaults(func=cmd_update)

    s = s2.add_parser("done", help="mark done and close the issue")
    s.add_argument("issue", type=int)
    s.set_defaults(func=cmd_done)

    s = s2.add_parser("defer", help="defer until a date")
    s.add_argument("issue", type=int)
    s.add_argument("until", help="YYYY-MM-DD")
    s.set_defaults(func=cmd_defer)

    s = s2.add_parser("comment", help="add a plain comment to the issue")
    s.add_argument("issue", type=int)
    s.add_argument("text")
    s.set_defaults(func=cmd_comment)

    proj = s2.add_parser("projects", help="manage proj: labels")
    proj_sub = proj.add_subparsers(dest="projects_cmd", required=True)
    proj_sub.add_parser("list").set_defaults(func=cmd_projects_list)
    pa = proj_sub.add_parser("add")
    pa.add_argument("name")
    pa.set_defaults(func=cmd_projects_add)

    s = s2.add_parser("cron-daily", help="daily un-defer / schedule sweep (cron)")
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--today", help="override today's date YYYY-MM-DD (testing)")
    s.set_defaults(func=cmd_cron_daily)

    s = s2.add_parser("cron-cleanup", help="close status/done issues; label closed issues done (cron)")
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--today", help="override today's date YYYY-MM-DD (testing)")
    s.set_defaults(func=cmd_cron_cleanup)

    # brief/dossier live in dossier.py but hang off `hq task`
    from . import dossier
    dossier.register(s2)
