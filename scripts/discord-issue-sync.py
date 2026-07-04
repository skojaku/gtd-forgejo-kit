#!/usr/bin/env python3
"""discord-issue-sync.py — mirror Forgejo "Next" issues to Discord threads.

One-way, issue-driven sync running inside the always-on hq container:

  * issue enters status/next   -> open a Discord thread in the target channel,
                                  seeded with the issue title/body/link
  * issue is closed            -> archive (+lock) that thread

The threads live in the same Discord server the Hermes `hq-github` gateway
watches, so replying in one still reaches the agent — this just gives every
actionable issue its own conversation surface.

Fetch strategy: one `GET /issues?labels=status/next&state=open` call finds
new candidates; each already-tracked thread is refreshed with one
`GET /issues/{n}` + one `GET /issues/{n}/comments` call. Call count is
~1 + 2×(active threads) per run — irrelevant against self-hosted Forgejo (the
"exactly one GraphQL call" constraint only ever existed for GitHub's rate
limit). The issue<->thread map is a host-local JSON file.

Config (config/env.yaml):
  discord:
    guild_id:          "<your-discord-guild-id>"
    next_channel_id:   "<your-channel-id>"   # parent text channel
    bot_token_env:     "~/.hermes/profiles/hq-github/.env"   # reads DISCORD_BOT_TOKEN

Forgejo connection: config/project.yaml (owner, repo, forgejo_url), same
accessors as the rest of hq (hqlib.common.owner/repo_name/forgejo_url), so
the usual $HQ_OWNER/$HQ_REPO_NAME/$FORGEJO_URL test overrides apply.

Env override: DISCORD_BOT_TOKEN wins over the profile .env file.
Flags: --dry-run (log intended actions, mutate nothing), --once (default).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
CONFIG = REPO / "config" / "env.yaml"
STATE = Path(os.path.expanduser("~/.hermes/hq-discord-threads.json"))
API = "https://discord.com/api/v10"
DISCORD_MAX = 2000  # message content hard limit
THREAD_NAME_MAX = 100  # Discord channel/thread name limit
AUTO_ARCHIVE_MIN = 10080  # 7 days of inactivity before Discord auto-archives

sys.path.insert(0, str(REPO / "bin"))
from hqlib.common import forgejo_url, load_config as hq_load_config, owner, repo_name  # noqa: E402
from hqlib.forgejo import ForgejoClient  # noqa: E402


def log(msg: str) -> None:
    print(f"[discord-sync] {msg}", flush=True)


def die(msg: str) -> None:
    log(f"ERROR: {msg}")
    sys.exit(1)


# ── config + token ────────────────────────────────────────────────────────
def load_config() -> dict:
    if not CONFIG.exists():
        die(f"{CONFIG} not found")
    cfg = yaml.safe_load(CONFIG.read_text()) or {}
    d = cfg.get("discord") or {}
    if not d.get("next_channel_id"):
        die("config discord.next_channel_id is missing")
    return cfg


def load_token(cfg: dict) -> str:
    # Profile file wins: this script is pinned to the hq-github bot, and a
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
    die("no DISCORD_BOT_TOKEN (discord.bot_token_env file or env)")


# ── state (issue <-> thread map, host-local, no GitHub cost) ────────────────
def load_state() -> dict:
    if STATE.exists():
        try:
            return json.loads(STATE.read_text())
        except json.JSONDecodeError:
            log(f"warning: {STATE} corrupt, starting fresh")
    return {"threads": {}}  # {issue_number(str): {"id": str, "archived": bool}}


def save_state(state: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(STATE)


# ── Forgejo: REST client for this repo ──────────────────────────────────────
def build_forgejo_client() -> ForgejoClient:
    hq_cfg = hq_load_config()
    return ForgejoClient(forgejo_url(hq_cfg), owner(hq_cfg), repo_name(hq_cfg))


def _to_item(fg: ForgejoClient, issue: dict, status: str | None) -> dict:
    """Normalize a Forgejo issue dict (+ its comments) into the same shape the
    sync/diff logic below has always consumed: state upper-cased to
    OPEN/CLOSED (matching the old GraphQL casing), author normalized to
    {"login": ...} (Forgejo's `user.username`, falling back to `user.login`),
    createdAt kept as the ISO8601 string the watermark logic sorts on."""
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


def _is_not_found(err: RuntimeError) -> bool:
    """True only for a genuine 404 (issue deleted/gone) — vs. a transient
    network/5xx failure, which must NOT be treated as 'issue disappeared'."""
    return " -> 404:" in str(err)


def fetch_items(fg: ForgejoClient, tracked_numbers: list[str]) -> tuple[list[dict], set[str]]:
    """Return ([{number,title,state,url,body,status,comments}], unreachable)
    covering every open issue labeled status/next (candidates for a new
    thread) plus every already-tracked issue (to detect closure / new
    comments). One list call for the former, one get+comments pair per
    tracked issue for the latter — see module docstring for the call-count
    rationale.

    `unreachable` holds tracked issue numbers that failed to fetch for a
    transient reason (not a genuine 404) — the caller must leave those
    threads alone this round rather than archiving them, so a network blip
    can't be mistaken for the issue disappearing."""
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
            if _is_not_found(e):
                log(f"tracked issue #{num} no longer exists (404)")
            else:
                log(f"warning: could not fetch tracked issue #{num}, leaving thread as-is: {e}")
                unreachable.add(num)
            continue
        items.append(_to_item(fg, issue, status=None))
    return items, unreachable


# ── Discord REST (Hermes bot token) ─────────────────────────────────────────
class Discord:
    def __init__(self, token: str, dry: bool):
        self.token = token
        self.dry = dry

    def _req(self, method: str, path: str, payload: dict | None = None) -> dict:
        url = f"{API}{path}"
        body = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(url, data=body, method=method)
        req.add_header("Authorization", f"Bot {self.token}")
        req.add_header("Content-Type", "application/json")
        req.add_header("User-Agent", "hq-discord-issue-sync (see README, 1.0)")
        for attempt in range(5):
            try:
                with urllib.request.urlopen(req) as r:
                    raw = r.read().decode()
                    return json.loads(raw) if raw else {}
            except urllib.error.HTTPError as e:
                if e.code == 429:  # honor Discord rate limit
                    retry = float(e.headers.get("Retry-After", "1"))
                    log(f"rate limited, sleeping {retry}s")
                    time.sleep(retry + 0.5)
                    continue
                detail = e.read().decode()
                raise RuntimeError(f"discord {method} {path} -> {e.code}: {detail}")
        raise RuntimeError(f"discord {method} {path}: gave up after rate-limit retries")

    def create_thread(self, channel_id: str, name: str) -> str:
        name = name[:THREAD_NAME_MAX]
        if self.dry:
            log(f"DRY create_thread in {channel_id}: {name!r}")
            return "dry-thread-id"
        # type 11 = public thread not attached to a message (text channel)
        res = self._req("POST", f"/channels/{channel_id}/threads", {
            "name": name, "type": 11, "auto_archive_duration": AUTO_ARCHIVE_MIN,
        })
        return res["id"]

    def post(self, thread_id: str, content: str) -> None:
        content = content[:DISCORD_MAX]
        if self.dry:
            log(f"DRY post to {thread_id}: {content[:80]!r}...")
            return
        self._req("POST", f"/channels/{thread_id}/messages", {"content": content})

    def archive(self, thread_id: str) -> None:
        if self.dry:
            log(f"DRY archive {thread_id}")
            return
        self._req("PATCH", f"/channels/{thread_id}", {"archived": True, "locked": True})


# ── sync logic ──────────────────────────────────────────────────────────────
def seed_message(item: dict) -> str:
    body = (item["body"] or "").strip()
    if len(body) > 1500:
        body = body[:1500].rstrip() + "\n…(truncated — see issue)"
    parts = [f"**#{item['number']} · {item['title']}**", item["url"]]
    if body:
        parts.append("")
        parts.append(body)
    return "\n".join(parts)


def post_comment(dc: "Discord", tid: str, c: dict) -> None:
    """Post one GitHub comment as a Discord message (chunked to the limit)."""
    author = ((c.get("author") or {}).get("login")) or "unknown"
    date = (c.get("createdAt") or "")[:10]
    body = (c.get("body") or "").strip()
    header = f"**@{author}** · {date}\n"
    room = DISCORD_MAX - len(header)
    chunk = body if len(body) <= room else body[:room - 20].rstrip() + "\n…(truncated)"
    dc.post(tid, header + chunk)


def watermark(comments: list[dict]) -> str:
    """Newest createdAt among comments (ISO8601 sorts lexicographically), or ''."""
    return max((c.get("createdAt") or "" for c in comments), default="")


def run(cfg: dict, token: str, dry: bool) -> None:
    channel = str(cfg["discord"]["next_channel_id"])

    fg = build_forgejo_client()
    state = load_state()
    threads = state["threads"]
    changed = False

    items, unreachable = fetch_items(fg, list(threads.keys()))
    by_num = {str(i["number"]): i for i in items}

    dc = Discord(token, dry)

    # 1. new Next+open issues without a thread -> open one, seed with the issue
    #    body + its recent comments (all from the single bulk query above)
    for i in items:
        num = str(i["number"])
        if i["status"] == "Next" and i["state"] == "OPEN" and num not in threads:
            log(f"issue #{num} entered Next -> creating thread")
            tid = dc.create_thread(channel, f"#{num} {i['title']}")
            dc.post(tid, seed_message(i))
            comments = i["comments"]
            if comments:
                dc.post(tid, f"— context: {len(comments)} comment(s) from the issue —")
                for c in comments:
                    post_comment(dc, tid, c)
            threads[num] = {"id": tid, "archived": False, "last_comment_at": watermark(comments)}
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
            log(f"issue #{num} has {len(fresh)} new comment(s) -> posting to thread")
            for c in fresh:
                post_comment(dc, rec["id"], c)
            rec["last_comment_at"] = watermark(it["comments"])
            changed = True

    # 2. tracked issues now closed -> archive their thread (once)
    for num, rec in list(threads.items()):
        if rec.get("archived") or num in unreachable:
            continue
        it = by_num.get(num)
        # CLOSED, or genuinely gone (404) — see fetch_items()/_is_not_found()
        if it is None or it["state"] == "CLOSED":
            log(f"issue #{num} closed -> archiving thread {rec['id']}")
            dc.archive(rec["id"])
            rec["archived"] = True
            changed = True

    if changed and not dry:
        save_state(state)
    log(f"done — {len(threads)} tracked, {sum(1 for r in threads.values() if not r['archived'])} active")


def main() -> None:
    ap = argparse.ArgumentParser(description="Sync Forgejo status/next issues to Discord threads")
    ap.add_argument("--dry-run", action="store_true", help="log actions, mutate nothing")
    args = ap.parse_args()
    cfg = load_config()
    token = load_token(cfg)
    run(cfg, token, args.dry_run)


if __name__ == "__main__":
    main()
