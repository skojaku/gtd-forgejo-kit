"""hq mail — Gmail via `gws` (ported from the gmail CLI, minus send).

Drafts only by design: there is no `hq mail send` and replies are always
created as drafts. The never-send boundary is enforced here, not by prompts.

Search results carry a clickable `url` built from the account's
`mail_url_index` config so the model never constructs Gmail links.

Also the queue's mail event-detection plugin: `scan()` finds new unread mail
(called by `hq queue scan`), `handle_job()` processes "thread_update" jobs
(a reply/forward on a thread already linked to an issue — logs it and forces
a dossier refresh). Both hooks are optional parts of the plugin contract,
see hqlib/plugins/__init__.py.
"""
import json
import os
import re
from pathlib import Path

from ...common import (
    fail, run, run_json, load_config, excerpt, envelope, print_json,
    resolve_account, gws_env, client as _client,
)

NAME = "mail"

# The email-thread ↔ issue marker convention lives here (this plugin is the
# only thing that writes it — the triage prompt embeds it in the task body — so
# core no longer hardcodes the regex). A task issue whose body carries
# `<!-- email-thread:<gmail-thread-id> -->` claims that Gmail thread, so a later
# message on the thread logs onto the issue instead of spawning a duplicate card.
_EMAIL_THREAD_RE = re.compile(r"<!--\s*email-thread:([A-Za-z0-9_-]+)\s*-->")


def thread_marker(thread_id):
    """The body marker the triage step writes to bind a thread to its issue."""
    return f"<!-- email-thread:{thread_id} -->"

BIN_DEPS = ["gws"]

CONFIG_STUB = {
    "google": {
        "default_account": "",
        "accounts": {},  # add one entry per account, e.g. work: {config_dir: "", mail_url_index: 0}
    },
}


def mail_url(url_index, message_id):
    return f"https://mail.google.com/mail/u/{url_index}/#all/{message_id}"


def _gws_json(cmd, env):
    """Run a gws command and parse JSON, tolerating the 'Using keyring
    backend: ...' status line gws prints before JSON on the raw API verbs."""
    out = run(cmd, env=env)
    for i, ch in enumerate(out):
        if ch in "{[":
            return json.loads(out[i:])
    fail(f"`{' '.join(cmd)}` returned no JSON: {out[:300]}")


def _list_unread_threads(cfg):
    """[{'id','threadId'}] for recent unread mail. The raw messages.list API
    carries threadId (the +triage helper strips it), so one call links every
    unread message to the thread it belongs to."""
    _, config_dir, _ = resolve_account(cfg, None)
    params = json.dumps({"userId": "me", "q": "is:unread newer_than:2d", "maxResults": 25})
    data = _gws_json(
        ["gws", "gmail", "users", "messages", "list", "--params", params, "--format", "json"],
        env=gws_env(config_dir),
    )
    return [
        {"id": m.get("id"), "threadId": m.get("threadId")}
        for m in (data.get("messages") or []) if m.get("id")
    ]


def read_message(cfg, message_id, account=None, max_chars=4000):
    """Full message fetch for the triage runner: {from,subject,date,body,url,
    thread_id}. The body is capped so a giant email can't blow the small model's
    context. This is the deterministic email-fetch half of triage — the model
    only ever sees the text this returns, never a mailbox tool."""
    _, config_dir, url_index = resolve_account(cfg, account)
    out = run_json(
        ["gws", "gmail", "+read", "--id", message_id, "--format", "json"],
        env=gws_env(config_dir),
    )
    body = out.get("body_text") or ""
    if len(body) > max_chars:
        body = body[:max_chars] + "…"
    return {
        "from": _fmt_from(out.get("from")) or "unknown",
        "subject": out.get("subject") or "(no subject)",
        "date": out.get("date") or "",
        "body": body,
        "url": mail_url(url_index, message_id),
        "thread_id": out.get("thread_id"),
    }


def _message_meta(cfg, mid):
    """{'from','subject','date','snippet','url'} for one message — used to log a
    thread evolution onto the issue."""
    _, config_dir, url_index = resolve_account(cfg, None)
    params = json.dumps({"userId": "me", "id": mid, "format": "metadata",
                         "metadataHeaders": ["From", "Subject", "Date"]})
    data = _gws_json(
        ["gws", "gmail", "users", "messages", "get", "--params", params, "--format", "json"],
        env=gws_env(config_dir),
    )
    headers = {h.get("name", "").lower(): h.get("value", "")
               for h in (data.get("payload") or {}).get("headers", [])}
    return {
        "from": headers.get("from", "unknown"),
        "subject": headers.get("subject", "(no subject)"),
        "date": headers.get("date", ""),
        "snippet": (data.get("snippet") or "").strip(),
        "url": mail_url(url_index, mid),
    }


# --------------------------------------------------------------------------
# Queue plugin hooks — scan() / handle_job(), see hqlib/plugins/__init__.py
# --------------------------------------------------------------------------

def _thread_index(cfg):
    """{gmail_thread_id: issue_number} built live from Forgejo by scanning open
    issue bodies for the thread marker. Replaces the old queue state.json
    `email_threads` map — the mapping is derivable from Forgejo itself, so no
    local state (and no core→plugin coupling) is needed."""
    client = _client(cfg)
    index = {}
    for issue in client.list_issues(state="open"):
        m = _EMAIL_THREAD_RE.search(issue.get("body") or "")
        if m:
            index[m.group(1)] = issue["number"]
    return index


def scan(cfg, state, q, taken):
    """New-unread-mail event detection. `q` is the QueueClient handed in by
    core's queue.py; `taken` is its dedup set of already-queued targets."""
    msgs = _list_unread_threads(cfg)
    seen = set(state.get("seen_message_ids", []))
    new = [m for m in msgs if m["id"] not in seen]
    if not new:
        return []
    index = _thread_index(cfg)
    ids = state.setdefault("seen_message_ids", [])
    # One job per message: a single read-classify-act decision per LLM
    # invocation is the granularity small models handle reliably.
    created = []
    for m in new[:10]:
        mid, tid = m["id"], m.get("threadId")
        ids.append(mid)
        issue = index.get(tid) if tid else None
        if issue:
            # This thread already has an issue → the new message is a
            # reply/forward that evolved it. Log + refresh, don't re-triage.
            if ("thread_update", mid) not in taken:
                created.append(q.enqueue("thread_update", mid,
                                         {"issue": issue, "message_id": mid, "thread_id": tid}))
                taken.add(("thread_update", mid))
        elif ("triage", mid) not in taken:
            created.append(q.enqueue("triage", mid, {"message_id": mid, "thread_id": tid}))
            taken.add(("triage", mid))
    # Bound the seen list so state.json can't grow without limit.
    if len(ids) > 2000:
        state["seen_message_ids"] = ids[-2000:]
    return created


def handle_job(job_type, cfg, payload, log, job_name):
    """Process a "thread_update" job: log a Gmail thread evolution
    (reply/forward) onto the linked issue and queue a forced dossier
    refresh. Deterministic — no LLM. Returns None for any other job_type
    (declines, falls through to the generic prompt-driven runner)."""
    if job_type != "thread_update":
        return None
    from ...queue import QueueClient

    issue = payload.get("issue")
    mid = payload.get("message_id")
    if not issue or not mid:
        return False, "thread_update missing issue/message_id"
    client = _client(cfg)
    issue_data = client.get_issue(issue)
    if (issue_data.get("state") or "").lower() == "closed":
        log.append({"job": job_name, "note": f"issue #{issue} closed — thread update skipped"})
        return True, None
    meta = _message_meta(cfg, mid)
    body = (
        f"📩 **Email thread update** — {meta['from']}"
        + (f" · {meta['date']}" if meta["date"] else "")
        + f"\n\n> {meta['subject']}\n\n{meta['snippet']}\n\n[Open in Gmail]({meta['url']})"
    )
    client.create_comment(issue, body)
    # Refresh the dossier so it reflects the evolved thread. force=True because a
    # new email is not a "user comment", so is_fresh() would otherwise skip it —
    # a forced collect is filed as a standalone hq-job ticket (it must carry the
    # force flag, which an in-place collect label can't).
    q = QueueClient(cfg)
    if ("collect", str(issue)) not in q.existing_targets():
        q.enqueue("collect", str(issue), {"issue": issue, "force": True})
    log.append({"job": job_name, "issue": issue, "logged": meta["subject"]})
    return True, None


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

def cmd_accounts(args):
    cfg = load_config()
    gt = cfg.get("google") or {}
    print_json({
        "default_account": gt.get("default_account"),
        "accounts": gt.get("accounts", {}),
    })


def _q(value):
    return f'"{value}"' if any(c.isspace() for c in value) else value


def build_query(args):
    parts = []
    if args.from_:
        parts.append(f"from:{_q(args.from_)}")
    if args.subject:
        parts.append(f"subject:{_q(args.subject)}")
    if args.label:
        parts.append(f"label:{_q(args.label)}")
    if args.newer_than:
        parts.append(f"newer_than:{args.newer_than}")
    if args.older_than:
        parts.append(f"older_than:{args.older_than}")
    if args.after:
        parts.append(f"after:{args.after}")
    if args.before:
        parts.append(f"before:{args.before}")
    if args.has_attachment:
        parts.append("has:attachment")
    if args.unread:
        parts.append("is:unread")
    if args.text:
        parts.append(args.text)
    return " ".join(parts)


def _triage(query, max_n, env):
    return run_json([
        "gws", "gmail", "+triage",
        "--query", query,
        "--max", str(max_n),
        "--format", "json",
    ], env=env)


def cmd_find(args):
    cfg = load_config()
    account, config_dir, url_index = resolve_account(cfg, args.account)
    query = build_query(args)
    if not query:
        fail("find needs at least one filter (--from/--subject/--text/…)")
    raw = _triage(query, args.max, gws_env(config_dir))
    results = [
        {
            "date": m.get("date"), "from": m.get("from"),
            "subject": m.get("subject"), "id": m.get("id"),
            "url": mail_url(url_index, m.get("id")),
        }
        for m in raw.get("messages", [])
    ]
    out = envelope(results)
    out["account"] = account
    out["query"] = query
    print_json(out)


def _fmt_from(frm):
    if not isinstance(frm, dict):
        return frm
    name, email = frm.get("name"), frm.get("email")
    return f"{name} <{email}>" if name and email else (email or name)


def cmd_brief(args):
    """One-shot triage retrieval: scan + read every hit with a body preview."""
    cfg = load_config()
    account, config_dir, url_index = resolve_account(cfg, args.account)
    env = gws_env(config_dir)
    scan = _triage(args.query or "is:unread", args.max, env)
    messages = []
    for m in scan.get("messages", []):
        r = run_json(["gws", "gmail", "+read", "--id", m["id"], "--format", "json"], env=env)
        messages.append({
            "id": m["id"],
            "thread_id": r.get("thread_id"),
            "from": _fmt_from(r.get("from")) or m.get("from"),
            "subject": r.get("subject") or m.get("subject"),
            "date": r.get("date") or m.get("date"),
            "url": mail_url(url_index, m["id"]),
            "preview": excerpt(r.get("body_text"), args.preview_chars),
        })
    out = envelope(messages)
    out["account"] = account
    out["query"] = args.query or "is:unread"
    print_json(out)


def cmd_read(args):
    cfg = load_config()
    account, config_dir, url_index = resolve_account(cfg, args.account)
    out = run_json([
        "gws", "gmail", "+read", "--id", args.message_id, "--format", "json",
    ], env=gws_env(config_dir))
    out.pop("body_html", None)
    body = out.get("body_text") or ""
    if not args.full and len(body) > args.max_chars:
        out["body_text"] = body[: args.max_chars] + "…"
        out["body_truncated"] = True
    out["account"] = account
    out["url"] = mail_url(url_index, args.message_id)
    print_json(out)


def get_label_map(config_dir):
    data = run_json([
        "gws", "gmail", "users", "labels", "list", "--params", json.dumps({"userId": "me"}),
    ], env=gws_env(config_dir))
    return {l["name"].lower(): l["id"] for l in data.get("labels", [])}


def resolve_label_id(label_map, name):
    lid = label_map.get(name.lower())
    if not lid:
        fail(f"no Gmail label named '{name}'. Known labels: {sorted(label_map)}")
    return lid


def cmd_label(args):
    cfg = load_config()
    account, config_dir, _ = resolve_account(cfg, args.account)
    label_map = get_label_map(config_dir)
    add_id = resolve_label_id(label_map, args.label)
    remove_ids = [resolve_label_id(label_map, n) for n in (args.remove.split(",") if args.remove else [])]

    run([
        "gws", "gmail", "users", "threads", "modify",
        "--params", json.dumps({"userId": "me", "id": args.thread_id}),
        "--json", json.dumps({"addLabelIds": [add_id], "removeLabelIds": remove_ids}),
    ], env=gws_env(config_dir))
    print_json({
        "thread_id": args.thread_id, "account": account,
        "added": args.label, "removed": args.remove.split(",") if args.remove else [],
    })


def _body_from_args(args):
    if args.body_file:
        try:
            return Path(args.body_file).read_text()
        except OSError as e:
            fail(f"could not read body file {args.body_file}: {e}")
    if args.body:
        return args.body
    fail("provide the body as an argument or via --body-file")


def cmd_draft_reply(args):
    cfg = load_config()
    account, config_dir, _ = resolve_account(cfg, args.account)
    body = _body_from_args(args)
    cmd = ["gws", "gmail", "+reply", "--message-id", args.message_id,
           "--body", body, "--draft", "--format", "json"]
    if args.cc:
        cmd += ["--cc", args.cc]
    out = run_json(cmd, env=gws_env(config_dir))
    out["account"] = account
    out["draft"] = True
    print_json(out)


def cmd_draft(args):
    cfg = load_config()
    account, config_dir, _ = resolve_account(cfg, args.account)
    body = _body_from_args(args)
    cmd = ["gws", "gmail", "+send", "--to", args.to, "--subject", args.subject,
           "--body", body, "--draft", "--format", "json"]
    if args.cc:
        cmd += ["--cc", args.cc]
    out = run_json(cmd, env=gws_env(config_dir))
    out["account"] = account
    out["draft"] = True
    print_json(out)


def register(sub):
    p = sub.add_parser("mail", help="Gmail (drafts only — sending is not possible from this CLI)")
    s2 = p.add_subparsers(dest="mail_cmd", required=True)

    s2.add_parser("accounts", help="list configured accounts").set_defaults(func=cmd_accounts)

    s = s2.add_parser("find", help="search with structured filters (builds the Gmail query for you)")
    s.add_argument("--account")
    s.add_argument("--from", dest="from_", help="sender (from:)")
    s.add_argument("--subject", help="words in subject (subject:)")
    s.add_argument("--text", help="free-text terms matched anywhere")
    s.add_argument("--label", help="Gmail label (label:)")
    s.add_argument("--newer-than", dest="newer_than", help="e.g. 7d, 2m, 1y")
    s.add_argument("--older-than", dest="older_than", help="e.g. 30d, 1y")
    s.add_argument("--after", help="YYYY/MM/DD")
    s.add_argument("--before", help="YYYY/MM/DD")
    s.add_argument("--has-attachment", dest="has_attachment", action="store_true")
    s.add_argument("--unread", action="store_true")
    s.add_argument("--max", type=int, default=10)
    s.set_defaults(func=cmd_find)

    s = s2.add_parser("brief", help="scan + hydrate each hit with a body preview (one-shot triage input)")
    s.add_argument("--account")
    s.add_argument("--query", help="Gmail query (default is:unread)")
    s.add_argument("--max", type=int, default=10)
    s.add_argument("--preview-chars", dest="preview_chars", type=int, default=400)
    s.set_defaults(func=cmd_brief)

    s = s2.add_parser("show", help="show a message by ID (body capped; --full for everything)")
    s.add_argument("message_id")
    s.add_argument("--account")
    s.add_argument("--max-chars", dest="max_chars", type=int, default=2000)
    s.add_argument("--full", action="store_true")
    s.set_defaults(func=cmd_read)

    s = s2.add_parser("label", help="add/remove labels on a thread, resolved by name")
    s.add_argument("thread_id")
    s.add_argument("label")
    s.add_argument("--account")
    s.add_argument("--remove", help="comma-separated label names to remove")
    s.set_defaults(func=cmd_label)

    s = s2.add_parser("draft-reply", help="create a draft reply (never sends)")
    s.add_argument("message_id")
    s.add_argument("body", nargs="?", help="reply text (or use --body-file)")
    s.add_argument("--body-file", dest="body_file")
    s.add_argument("--account")
    s.add_argument("--cc")
    s.set_defaults(func=cmd_draft_reply)

    s = s2.add_parser("draft", help="create a draft email (never sends)")
    s.add_argument("--to", required=True)
    s.add_argument("--subject", required=True)
    s.add_argument("--body")
    s.add_argument("--body-file", dest="body_file")
    s.add_argument("--account")
    s.add_argument("--cc")
    s.set_defaults(func=cmd_draft)
