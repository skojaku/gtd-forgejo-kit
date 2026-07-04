"""hq task brief / dossier — the collect-loop bookends.

`brief` gives the model everything needed to start a collect run in one call:
issue content, project fields, the last dossier, and the user comments posted
since it (those comments are the user's standing instructions).

`dossier` turns a small flat JSON file into one correctly-marked, correctly-
formatted issue comment, so the model never composes the format itself.

Marker (first line of every agent comment, invisible in the GitHub UI):
    <!-- hq-dossier v1 2026-07-03T14:05Z -->
"""
from datetime import datetime, timezone

from .common import (
    fail, run, run_json, load_config, repo, owner,
    excerpt, print_json, read_params,
)

MARKER_PREFIX = "<!-- hq-dossier v1"
VALID_SOURCES = ("gmail", "drive", "wiki", "calendar")
NEEDS_DECISION_LABEL = "needs-decision"


def _fetch_issue_and_comments(cfg, issue):
    r = repo(cfg)
    data = run_json(["gh", "api", f"repos/{r}/issues/{issue}"])
    comments = run_json(["gh", "api", f"repos/{r}/issues/{issue}/comments", "--paginate"])
    if isinstance(comments, dict):
        comments = [comments]
    return data, comments


def _last_dossier(comments):
    for c in reversed(comments):
        if (c.get("body") or "").startswith(MARKER_PREFIX):
            return c
    return None


def brief_data(cfg, issue):
    collect_cfg = cfg.get("collect") or {}
    chars = collect_cfg.get("excerpt_chars", 400)
    issue_data, comments = _fetch_issue_and_comments(cfg, issue)

    last = _last_dossier(comments)
    last_info = None
    since = None
    if last:
        since = last.get("created_at")
        body_lines = [
            l for l in (last.get("body") or "").splitlines()
            if l and not l.startswith(("<!--", "#", ">", "**", "-"))
        ]
        last_info = {
            "date": since,
            "summary": excerpt(" ".join(body_lines[:3]), chars),
        }

    user_login = owner(cfg)
    user_comments = []
    for c in comments:
        if (c.get("body") or "").startswith(MARKER_PREFIX):
            continue
        if c.get("user", {}).get("login") != user_login:
            continue
        if since and c.get("created_at") <= since:
            continue
        user_comments.append({
            "date": c.get("created_at"),
            "text": excerpt(c.get("body"), chars * 2),
        })

    from .task import find_task
    task = find_task(cfg, issue) or {}

    return {
        "issue": issue,
        "title": issue_data.get("title"),
        "url": issue_data.get("html_url"),
        "state": issue_data.get("state"),
        "body_excerpt": excerpt(issue_data.get("body"), chars * 2),
        "labels": [l["name"] for l in issue_data.get("labels", [])],
        "fields": {k: task.get(k) for k in ("status", "due", "project", "context", "duration", "scheduled")},
        "last_dossier": last_info,
        "user_comments_since": user_comments,
    }


def is_fresh(brief, max_age_days=3):
    """True when the last dossier is recent and no new user comments arrived —
    the deterministic 'skip this collect run' rule."""
    last = brief.get("last_dossier")
    if not last or brief.get("user_comments_since"):
        return False
    from datetime import datetime, timezone, timedelta
    try:
        posted = datetime.strptime(last["date"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (ValueError, KeyError, TypeError):
        return False
    return datetime.now(timezone.utc) - posted < timedelta(days=max_age_days)


def cmd_brief(args):
    cfg = load_config()
    print_json(brief_data(cfg, args.issue))


def _validate_entries(params, cfg):
    collect_cfg = cfg.get("collect") or {}
    max_entries = collect_cfg.get("max_entries", 12)
    max_chars = collect_cfg.get("excerpt_chars", 400)

    summary = (params.get("summary") or "").strip()
    if not summary:
        fail("params must have a non-empty 'summary' (2-3 sentences)")
    entries = params.get("entries")
    if not isinstance(entries, list):
        fail("params must have 'entries' as a list (may be empty)")
    if len(entries) > max_entries:
        fail(f"too many entries ({len(entries)}); cap is {max_entries} — keep only the most relevant")
    for i, e in enumerate(entries):
        if e.get("source") not in VALID_SOURCES:
            fail(f"entries[{i}].source must be one of {VALID_SOURCES}, got: {e.get('source')!r}")
        if not (e.get("url") or "").strip():
            fail(f"entries[{i}].url is required (the clickable source link)")
        if not (e.get("title") or "").strip():
            fail(f"entries[{i}].title is required")
        if not (e.get("excerpt") or "").strip():
            fail(f"entries[{i}].excerpt is required (2-3 sentences)")
        if len(e["excerpt"]) > max_chars:
            e["excerpt"] = excerpt(e["excerpt"], max_chars)
    decision = params.get("decision_needed")
    if decision is not None and not str(decision).strip():
        fail("'decision_needed' must be a non-empty question or omitted")
    return summary, entries, (str(decision).strip() if decision else None)


SOURCE_ICONS = {"gmail": "📧", "drive": "📄", "wiki": "📝", "calendar": "📅"}


def _render(summary, entries, decision, now_utc):
    lines = [f"{MARKER_PREFIX} {now_utc} -->", f"### Dossier — {now_utc[:10]}", ""]
    if decision:
        lines += [f"> **Decision needed:** {decision}", ""]
    lines += [summary, ""]
    by_source = {}
    for e in entries:
        by_source.setdefault(e["source"], []).append(e)
    for source in VALID_SOURCES:
        group = by_source.get(source)
        if not group:
            continue
        lines.append(f"**{SOURCE_ICONS[source]} {source.capitalize()}**")
        for e in group:
            lines.append(f"- [{e['title']}]({e['url']}) — {e['excerpt']}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def post_dossier(cfg, issue, params):
    """Validate a dossier params dict and post it as one marked comment.
    Shared by the CLI verb and the queue runner (which posts on the model's
    behalf — small models reliably produce the JSON but not the final call)."""
    summary, entries, decision = _validate_entries(params, cfg)

    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
    body = _render(summary, entries, decision, now_utc)

    r = repo(cfg)
    run(["gh", "issue", "comment", str(issue), "--repo", r, "--body", body])
    if decision:
        from .task import ensure_label
        ensure_label(cfg, NEEDS_DECISION_LABEL, color="d93f0b")
        run(["gh", "issue", "edit", str(issue), "--repo", r, "--add-label", NEEDS_DECISION_LABEL])

    return {
        "issue": issue,
        "posted": True,
        "entries": len(entries),
        "decision_needed": decision,
        "marker": now_utc,
    }


def cmd_dossier(args):
    cfg = load_config()
    params = read_params(args.file)
    print_json(post_dossier(cfg, args.issue, params))


def register(sub):
    s = sub.add_parser("brief", help="everything needed to start a collect run: issue + fields + last dossier + new user comments")
    s.add_argument("issue", type=int)
    s.set_defaults(func=cmd_brief)

    s = sub.add_parser(
        "dossier",
        help="post one dossier comment from a JSON file: "
             '{summary, decision_needed?, entries: [{source, url, title, excerpt}]}',
    )
    s.add_argument("issue", type=int)
    s.add_argument("file")
    s.set_defaults(func=cmd_dossier)
