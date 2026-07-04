"""forgejo.py — stdlib-only REST client for the self-hosted Forgejo instance.

Mirrors the house style of the Discord class in scripts/discord-issue-sync.py:
plain `urllib.request`, a thin `_req` wrapper that builds the request, adds
auth, retries transient failures, and raises a clear `RuntimeError` with the
HTTP status + response body on hard failures.

Token resolution (in order):
  1. $FORGEJO_TOKEN env var
  2. ~/.config/hq/forgejo-token file (mode 600) — this is the fallback the
     Hermes sandbox relies on, since it scrubs env tokens from its tool
     sandbox the same way ~/.config/gh-agent worked for `gh`.

All paths are under /api/v1. See PLAN.md section 2 for the full method list.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

TOKEN_FILE = Path(os.path.expanduser("~/.config/hq/forgejo-token"))
MAX_ATTEMPTS = 3
BACKOFF_BASE = 1.5  # seconds; attempt N sleeps BACKOFF_BASE * N


def load_token() -> str:
    """$FORGEJO_TOKEN wins; else the mounted token file. Raises if neither is set."""
    tok = os.environ.get("FORGEJO_TOKEN")
    if tok:
        return tok.strip()
    if TOKEN_FILE.exists():
        tok = TOKEN_FILE.read_text().strip()
        if tok:
            return tok
    raise RuntimeError(
        f"no Forgejo token: set $FORGEJO_TOKEN or create {TOKEN_FILE} (mode 600)"
    )


class ForgejoClient:
    """Thin REST client for one Forgejo repo (owner/repo fixed at construction)."""

    def __init__(self, url: str, owner: str, repo: str, token: str | None = None):
        self.base = url.rstrip("/")
        self.owner = owner
        self.repo = repo
        self.token = token or load_token()

    # -- transport ------------------------------------------------------
    def _req(self, method: str, path: str, payload=None, params: dict | None = None):
        """path is relative to /api/v1, e.g. '/repos/{o}/{r}/issues'."""
        url = f"{self.base}/api/v1{path}"
        if params:
            query = urllib.parse.urlencode(
                {k: v for k, v in params.items() if v is not None}
            )
            if query:
                url = f"{url}?{query}"
        body = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(url, data=body, method=method)
        req.add_header("Authorization", f"token {self.token}")
        req.add_header("Content-Type", "application/json")
        req.add_header("User-Agent", "hq-forgejo-client (see README, 1.0)")

        last_err = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                with urllib.request.urlopen(req) as r:
                    raw = r.read().decode()
                    return json.loads(raw) if raw else {}
            except urllib.error.HTTPError as e:
                detail = e.read().decode()
                if 500 <= e.code < 600 and attempt < MAX_ATTEMPTS:
                    last_err = RuntimeError(
                        f"forgejo {method} {path} -> {e.code}: {detail[:500]}"
                    )
                    time.sleep(BACKOFF_BASE * attempt)
                    continue
                raise RuntimeError(
                    f"forgejo {method} {path} -> {e.code}: {detail[:500]}"
                ) from None
            except urllib.error.URLError as e:
                if attempt < MAX_ATTEMPTS:
                    last_err = RuntimeError(f"forgejo {method} {path}: {e}")
                    time.sleep(BACKOFF_BASE * attempt)
                    continue
                raise RuntimeError(f"forgejo {method} {path}: {e}") from None
        raise last_err or RuntimeError(f"forgejo {method} {path}: gave up")

    def _repo_path(self, suffix: str = "") -> str:
        return f"/repos/{self.owner}/{self.repo}{suffix}"

    # -- issues -----------------------------------------------------------
    def list_issues(self, state: str = "all", labels: str | None = None, since: str | None = None) -> list[dict]:
        """Paginated walk of all issues (type=issues excludes PRs).

        since: RFC3339 timestamp — only issues updated after it (any field,
        not just comments — a superset is fine for a poll-and-recheck scan).
        """
        items, page = [], 1
        while True:
            params = {
                "type": "issues",
                "state": state,
                "labels": labels,
                "since": since,
                "limit": 50,
                "page": page,
            }
            batch = self._req("GET", self._repo_path("/issues"), params=params)
            if not batch:
                break
            items.extend(batch)
            if len(batch) < 50:
                break
            page += 1
        return items

    def get_issue(self, number: int) -> dict:
        return self._req("GET", self._repo_path(f"/issues/{number}"))

    def create_issue(self, title: str, body: str = "", labels: list[str] | None = None) -> dict:
        payload = {"title": title, "body": body}
        if labels:
            label_ids = self._label_ids(labels)
            if label_ids:
                payload["labels"] = label_ids
        return self._req("POST", self._repo_path("/issues"), payload=payload)

    def edit_issue(self, number: int, **fields) -> dict:
        """fields: any of title/body/due_date/state (open|closed)/..."""
        return self._req("PATCH", self._repo_path(f"/issues/{number}"), payload=fields)

    # -- labels -------------------------------------------------------------
    def list_labels(self) -> list[dict]:
        return self._req("GET", self._repo_path("/labels"))

    def create_label(self, name: str, color: str, exclusive: bool = False) -> dict:
        payload = {"name": name, "color": color, "exclusive": exclusive}
        return self._req("POST", self._repo_path("/labels"), payload=payload)

    def _label_ids(self, names: list[str]) -> list[int]:
        by_name = {l["name"]: l["id"] for l in self.list_labels()}
        return [by_name[n] for n in names if n in by_name]

    def add_labels(self, number: int, names: list[str]) -> dict:
        label_ids = self._label_ids(names)
        return self._req(
            "POST", self._repo_path(f"/issues/{number}/labels"), payload={"labels": label_ids}
        )

    def remove_label(self, number: int, name: str) -> None:
        by_name = {l["name"]: l["id"] for l in self.list_labels()}
        label_id = by_name.get(name)
        if label_id is None:
            return  # already absent — idempotent
        self._req("DELETE", self._repo_path(f"/issues/{number}/labels/{label_id}"))

    def replace_scoped(self, number: int, scope: str, value: str) -> dict:
        """Add `scope/value` — Forgejo's exclusive-label enforcement auto-removes
        any sibling label in the same scope, so no explicit remove is needed."""
        return self.add_labels(number, [f"{scope}/{value}"])

    # -- comments -------------------------------------------------------------
    def list_comments(self, number: int) -> list[dict]:
        return self._req("GET", self._repo_path(f"/issues/{number}/comments"))

    def create_comment(self, number: int, body: str) -> dict:
        return self._req(
            "POST", self._repo_path(f"/issues/{number}/comments"), payload={"body": body}
        )
