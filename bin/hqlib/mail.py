"""hq mail — Gmail via `gws` (ported from the gmail CLI, minus send).

Drafts only by design: there is no `hq mail send` and replies are always
created as drafts. The never-send boundary is enforced here, not by prompts.

Search results carry a clickable `url` built from the account's
`mail_url_index` config so the model never constructs Gmail links.
"""
import json
import os
from pathlib import Path

from .common import fail, run, run_json, load_config, excerpt, envelope, print_json


def resolve_account(cfg, name):
    """Returns (account_key, config_dir_or_None, mail_url_index).

    Account values are either a bare config-dir string (legacy) or a mapping
    {config_dir, mail_url_index}. An empty config_dir means "use gws's own
    default" (single-account host).
    """
    gt = cfg.get("gmail_triage") or {}
    accounts = gt.get("accounts", {})
    key = name or gt.get("default_account")
    if not key or key not in accounts:
        fail(f"unknown account '{key}'. Known accounts: {sorted(accounts)}")
    raw = accounts[key]
    if isinstance(raw, dict):
        path = raw.get("config_dir") or ""
        url_index = raw.get("mail_url_index", 0)
    else:
        path = raw or ""
        url_index = 0
    config_dir = str(Path(path).expanduser()) if path else None
    return key, config_dir, url_index


def gws_env(config_dir):
    env = os.environ.copy()
    if config_dir:
        env["GOOGLE_WORKSPACE_CLI_CONFIG_DIR"] = config_dir
    return env


def mail_url(url_index, message_id):
    return f"https://mail.google.com/mail/u/{url_index}/#all/{message_id}"


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

def cmd_accounts(args):
    cfg = load_config()
    gt = cfg.get("gmail_triage") or {}
    print_json({
        "default_account": gt.get("default_account"),
        "accounts": gt.get("accounts", {}),
        "inbox_label": gt.get("inbox_label"),
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

    s = s2.add_parser("read", help="read a message by ID (body capped; --full for everything)")
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
