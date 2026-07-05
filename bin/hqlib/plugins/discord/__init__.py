"""hq discord — mirror Forgejo "Next" issues to Discord threads.

One-way, issue-driven sync driven by the `hq-local` Hermes bot token:

  * issue enters status/next -> open a Discord thread in the target channel,
                                 seeded with the issue title/body/link
  * issue is closed          -> archive (+lock) that thread

The threads live in the same Discord server the Hermes `hq-local` gateway
watches, so replying in one still reaches the agent — this just gives every
actionable issue its own conversation surface.

Entirely optional: a fresh clone has no discord.* config filled in, no
hq-discord container running, and `hq queue scan`/task/mail/cal/drive/wiki
all work with zero Discord awareness. `hq install discord` scaffolds the
config; `docker compose --profile discord up -d hq-discord` is the runtime
on/off switch (see compose.yaml) — this plugin's CLI verbs never start or
stop that container, only report on it and drive the sync itself.

Config (config/hq.yaml):
  discord:
    guild_id:          ""    # Discord server (guild) ID
    next_channel_id:   ""    # parent text channel ID that hosts the threads
    bot_token_env:     "~/.hermes/profiles/hq-local/.env"   # reads DISCORD_BOT_TOKEN

State: ~/.hermes/hq-discord-threads.json (issue<->thread map, host-local).
"""
import json
import os
import shutil
import time
import urllib.error
import urllib.request
from pathlib import Path

from ...common import client as _client, fail, load_config, print_json, run

NAME = "discord"

BIN_DEPS = ["hermes"]

CONFIG_STUB = {
    "discord": {
        "guild_id": "",
        "next_channel_id": "",
        "bot_token_env": "~/.hermes/profiles/hq-local/.env",
    },
}

STATE = Path(os.path.expanduser("~/.hermes/hq-discord-threads.json"))
API = "https://discord.com/api/v10"
DISCORD_MAX = 2000  # message content hard limit
THREAD_NAME_MAX = 100  # Discord channel/thread name limit
AUTO_ARCHIVE_MIN = 10080  # 7 days of inactivity before Discord auto-archives


# ── config + token ───────────────────────────────────────────────────────
def _load_token(cfg):
    # Profile file wins: this plugin is pinned to the hq-local bot, and a
    # global DISCORD_BOT_TOKEN (e.g. the gateway default profile's, exported
    # from ~/.hermes/.env) must not hijack it. Env is only a fallback.
    env_path = (cfg.get("discord") or {}).get("bot_token_env")
    if env_path:
        p = Path(os.path.expanduser(env_path))
        if p.exists():
            for line in p.read_text().splitlines():
                line = line.strip()
                if line.startswith("DISCORD_BOT_TOKEN="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    tok = os.environ.get("DISCORD_BOT_TOKEN")
    if tok:
        return tok.strip()
    fail("no DISCORD_BOT_TOKEN (discord.bot_token_env file or $DISCORD_BOT_TOKEN)")


def _require_channel(cfg):
    channel = (cfg.get("discord") or {}).get("next_channel_id")
    if not channel:
        fail("config discord.next_channel_id is missing — run `hq install discord` then fill config/hq.yaml")
    return str(channel)


# ── state (issue <-> thread map, host-local, no Forgejo API cost) ───────────
def _load_state():
    if STATE.exists():
        try:
            return json.loads(STATE.read_text())
        except json.JSONDecodeError:
            return {"threads": {}}
    return {"threads": {}}  # {issue_number(str): {"id": str, "archived": bool}}


def _save_state(state):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(STATE)


# ── Forgejo: REST client for this repo (shared factory from common) ─────────
def _to_item(fg, issue, status):
    """Normalize a Forgejo issue dict (+ its comments) into the shape the
    sync/diff logic below consumes: state upper-cased to OPEN/CLOSED, author
    normalized to {"login": ...}, createdAt kept as the ISO8601 string the
    watermark logic sorts on."""
    number = issue["number"]
    comments = []
    for c in fg.list_comments(number):
        u = c.get("user") or {}
        login = u.get("username") or u.get("login") or "unknown"
        comments.append({
            "author": {"login": login},
            "body": c.get("body") or "",
            "createdAt": c.get("created_at") or "",
        })
    return {
        "number": number,
        "title": issue["title"],
        "state": (issue.get("state") or "").upper(),
        "url": issue.get("html_url") or issue.get("url") or "",
        "body": issue.get("body") or "",
        "status": status,
        "comments": comments,
    }


def _is_not_found(err):
    """True only for a genuine 404 (issue deleted/gone) — vs. a transient
    network/5xx failure, which must NOT be treated as 'issue disappeared'."""
    return " -> 404:" in str(err)


def _fetch_items(fg, tracked_numbers):
    """Return ([{number,title,state,url,body,status,comments}], unreachable)
    covering every open issue labeled status/next (candidates for a new
    thread) plus every already-tracked issue (to detect closure / new
    comments). `unreachable` holds tracked issue numbers that failed to fetch
    for a transient reason (not a genuine 404) — the caller must leave those
    threads alone this round rather than archiving them."""
    items, seen, unreachable = [], set(), set()
    for issue in fg.list_issues(state="open", labels="status/next"):
        items.append(_to_item(fg, issue, status="Next"))
        seen.add(str(issue["number"]))
    for num in tracked_numbers:
        if num in seen:
            continue
        try:
            issue = fg.get_issue(int(num))
        except RuntimeError as e:
            if not _is_not_found(e):
                unreachable.add(num)
            continue
        items.append(_to_item(fg, issue, status=None))
    return items, unreachable


# ── Discord REST (Hermes bot token) ─────────────────────────────────────────
class _Discord:
    def __init__(self, token, dry):
        self.token = token
        self.dry = dry

    def _req(self, method, path, payload=None):
        url = f"{API}{path}"
        body = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(url, data=body, method=method)
        req.add_header("Authorization", f"Bot {self.token}")
        req.add_header("Content-Type", "application/json")
        req.add_header("User-Agent", "hq-discord/1.0")
        for _ in range(5):
            try:
                with urllib.request.urlopen(req) as r:
                    raw = r.read().decode()
                    return json.loads(raw) if raw else {}
            except urllib.error.HTTPError as e:
                if e.code == 429:  # honor Discord rate limit
                    retry = float(e.headers.get("Retry-After", "1"))
                    time.sleep(retry + 0.5)
                    continue
                detail = e.read().decode()
                raise RuntimeError(f"discord {method} {path} -> {e.code}: {detail}")
        raise RuntimeError(f"discord {method} {path}: gave up after rate-limit retries")

    def create_thread(self, channel_id, name):
        name = name[:THREAD_NAME_MAX]
        if self.dry:
            return "dry-thread-id"
        # type 11 = public thread not attached to a message (text channel)
        res = self._req("POST", f"/channels/{channel_id}/threads", {
            "name": name, "type": 11, "auto_archive_duration": AUTO_ARCHIVE_MIN,
        })
        return res["id"]

    def post(self, thread_id, content):
        content = content[:DISCORD_MAX]
        if self.dry:
            return
        self._req("POST", f"/channels/{thread_id}/messages", {"content": content})

    def archive(self, thread_id):
        if self.dry:
            return
        self._req("PATCH", f"/channels/{thread_id}", {"archived": True, "locked": True})


# ── sync logic ──────────────────────────────────────────────────────────────
def _seed_message(item):
    body = (item["body"] or "").strip()
    if len(body) > 1500:
        body = body[:1500].rstrip() + "\n…(truncated — see issue)"
    parts = [f"**#{item['number']} · {item['title']}**", item["url"]]
    if body:
        parts.append("")
        parts.append(body)
    return "\n".join(parts)


def _post_comment(dc, tid, c):
    """Post one issue comment as a Discord message (chunked to the limit)."""
    author = ((c.get("author") or {}).get("login")) or "unknown"
    date = (c.get("createdAt") or "")[:10]
    body = (c.get("body") or "").strip()
    header = f"**@{author}** · {date}\n"
    room = DISCORD_MAX - len(header)
    chunk = body if len(body) <= room else body[:room - 20].rstrip() + "\n…(truncated)"
    dc.post(tid, header + chunk)


def _watermark(comments):
    """Newest createdAt among comments (ISO8601 sorts lexicographically), or ''."""
    return max((c.get("createdAt") or "" for c in comments), default="")


def _run_sync(cfg, token, dry):
    channel = _require_channel(cfg)
    fg = _client(cfg)
    state = _load_state()
    threads = state["threads"]
    changed = False
    created, archived_now, comments_posted = [], [], 0

    items, unreachable = _fetch_items(fg, list(threads.keys()))
    by_num = {str(i["number"]): i for i in items}
    dc = _Discord(token, dry)

    # 1. new Next+open issues without a thread -> open one, seed with the
    #    issue body + its recent comments (all from the bulk query above)
    for i in items:
        num = str(i["number"])
        if i["status"] == "Next" and i["state"] == "OPEN" and num not in threads:
            tid = dc.create_thread(channel, f"#{num} {i['title']}")
            dc.post(tid, _seed_message(i))
            comments = i["comments"]
            if comments:
                dc.post(tid, f"— context: {len(comments)} comment(s) from the issue —")
                for c in comments:
                    _post_comment(dc, tid, c)
                comments_posted += len(comments)
            threads[num] = {"id": tid, "archived": False, "last_comment_at": _watermark(comments)}
            created.append(num)
            changed = True

    # 1b. ongoing: mirror new comments on already-tracked, still-open issues
    for num, rec in threads.items():
        if rec.get("archived"):
            continue
        it = by_num.get(num)
        if not it or it["state"] != "OPEN":
            continue
        seen = rec.get("last_comment_at", "")
        fresh = [c for c in it["comments"] if (c.get("createdAt") or "") > seen]
        if fresh:
            for c in fresh:
                _post_comment(dc, rec["id"], c)
            comments_posted += len(fresh)
            rec["last_comment_at"] = _watermark(it["comments"])
            changed = True

    # 2. tracked issues now closed -> archive their thread (once)
    for num, rec in list(threads.items()):
        if rec.get("archived") or num in unreachable:
            continue
        it = by_num.get(num)
        # CLOSED, or genuinely gone (404) — see _fetch_items()/_is_not_found()
        if it is None or it["state"] == "CLOSED":
            dc.archive(rec["id"])
            rec["archived"] = True
            archived_now.append(num)
            changed = True

    if changed and not dry:
        _save_state(state)

    return {
        "dry_run": dry,
        "created": created,
        "archived": archived_now,
        "comments_posted": comments_posted,
        "threads_tracked": len(threads),
        "threads_active": sum(1 for r in threads.values() if not r["archived"]),
    }


# ── status checks ────────────────────────────────────────────────────────
def _gateway_status():
    """docker ps for the hq-gateway container. Not an error if docker/socket
    is unreachable from wherever `hq` runs — just reported as unknown."""
    if shutil.which("docker") is None:
        return {"checked": False, "note": "docker not on PATH"}
    try:
        out = run(["docker", "ps", "--filter", "name=^hq-gateway$", "--format", "{{.Status}}"],
                   allow_fail=True)
    except RuntimeError as e:
        return {"checked": False, "note": str(e)[:200]}
    status = out.strip()
    return {"checked": True, "running": bool(status), "status": status or None}


def cmd_status(args):
    cfg = load_config()
    d = cfg.get("discord") or {}
    token_path = d.get("bot_token_env")
    token_file_found = False
    if token_path:
        p = Path(os.path.expanduser(token_path))
        token_file_found = p.exists() and "DISCORD_BOT_TOKEN=" in p.read_text()

    state = _load_state()
    threads = state.get("threads", {})
    active = sum(1 for r in threads.values() if not r.get("archived"))

    print_json({
        "config": {
            "guild_id_set": bool(d.get("guild_id")),
            "next_channel_id_set": bool(d.get("next_channel_id")),
            "bot_token_file_found": token_file_found,
            "hermes_on_path": shutil.which("hermes") is not None,
        },
        "threads": {"tracked": len(threads), "active": active, "archived": len(threads) - active},
        "gateway": _gateway_status(),
    })


def cmd_sync(args):
    cfg = load_config()
    token = _load_token(cfg)
    print_json(_run_sync(cfg, token, dry=args.dry_run))


def cmd_test_post(args):
    cfg = load_config()
    channel = _require_channel(cfg)
    token = _load_token(cfg)
    message = args.message or "hq discord test-post — connectivity check"
    dc = _Discord(token, dry=args.dry_run)
    dc.post(channel, message)
    print_json({"posted_to": channel, "dry_run": args.dry_run, "message": message})


def register(sub):
    p = sub.add_parser("discord", help="Forgejo 'Next' issues <-> Discord thread mirror (optional plugin)")
    s2 = p.add_subparsers(dest="discord_cmd", required=True)

    s = s2.add_parser("status", help="config completeness, thread state, hq-gateway container status")
    s.set_defaults(func=cmd_status)

    s = s2.add_parser("sync", help="one sync pass: open/archive threads, mirror new comments")
    s.add_argument("--dry-run", action="store_true", help="log intended actions, mutate nothing")
    s.set_defaults(func=cmd_sync)

    s = s2.add_parser("test-post", help="post a test message to discord.next_channel_id (verifies token/perms)")
    s.add_argument("--message", help="custom message text")
    s.add_argument("--dry-run", action="store_true")
    s.set_defaults(func=cmd_test_post)
