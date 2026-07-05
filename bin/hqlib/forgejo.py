"""forgejo.py — stdlib-only REST client for the self-hosted Forgejo instance.

Mirrors the house style of the _Discord class in hqlib/plugins/discord/:
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

# Default scopes for a token minted by `hq setup` — enough to drive the whole
# GTD workflow (repos, issues, labels, comments) and read the owning user, but
# no admin/sudo. Forgejo's coarse read/write scope model, newest naming.
SETUP_TOKEN_SCOPES = [
    "write:repository",
    "write:issue",
    "write:user",
]


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


def save_token(token: str) -> Path:
    """Persist a token to ~/.config/hq/forgejo-token (mode 600). Returns the path.
    Used by `hq setup` after minting a token so interactive hosts pick it up via
    load_token() without an env var."""
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.write_text(token.strip() + "\n")
    os.chmod(TOKEN_FILE, 0o600)
    return TOKEN_FILE


# --------------------------------------------------------------------------
# Greenfield bootstrap helpers (used by `hq setup` Tier 2). These talk to a
# fresh Forgejo with HTTP Basic auth (admin username/password) rather than a
# token, since on a greenfield instance no token exists yet.
# --------------------------------------------------------------------------

def _basic_auth_header(user: str, password: str) -> str:
    import base64
    raw = f"{user}:{password}".encode()
    return "Basic " + base64.b64encode(raw).decode()


def _plain_req(url: str, method: str = "GET", payload=None, headers=None,
               timeout: int = 15):
    body = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=body, method=method)
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", "hq-forgejo-client (HQ setup, 1.0)")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        detail = e.read().decode()
        raise RuntimeError(f"forgejo {method} {url} -> {e.code}: {detail[:500]}") from None
    except urllib.error.URLError as e:
        raise RuntimeError(f"forgejo {method} {url}: {e}") from None


def reachable(base_url: str, timeout: int = 5) -> dict:
    """Unauthenticated GET /api/v1/version — confirms the instance is up and
    speaking the API, independent of any token. Returns the version dict or
    raises RuntimeError. Used by `hq doctor`."""
    return _plain_req(
        f"{base_url.rstrip('/')}/api/v1/version", timeout=timeout
    )


def create_token(base_url: str, username: str, password: str, name: str,
                 scopes: list | None = None) -> str:
    """Mint a personal access token via Basic auth (POST /users/{u}/tokens).
    Returns the raw sha1 token string. Raises if the name already exists."""
    data = _plain_req(
        f"{base_url.rstrip('/')}/api/v1/users/{username}/tokens",
        method="POST",
        payload={"name": name, "scopes": scopes or SETUP_TOKEN_SCOPES},
        headers={"Authorization": _basic_auth_header(username, password)},
    )
    tok = data.get("sha1") or data.get("token")
    if not tok:
        raise RuntimeError(f"token created but no sha1 in response: {data}")
    return tok


def create_admin_user(base_url: str, admin_user: str, admin_password: str,
                      username: str, password: str, email: str,
                      admin: bool = True) -> dict:
    """Create a user via the admin API (POST /admin/users) using an existing
    admin's Basic-auth credentials. On a greenfield instance the first admin is
    made by the Forgejo web installer or `forgejo admin user create`; this is
    for scripting additional (or the working) accounts once one admin exists."""
    return _plain_req(
        f"{base_url.rstrip('/')}/api/v1/admin/users",
        method="POST",
        payload={
            "username": username,
            "password": password,
            "email": email,
            "must_change_password": False,
            "admin": admin,
        },
        headers={"Authorization": _basic_auth_header(admin_user, admin_password)},
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
        req.add_header("User-Agent", "hq-forgejo-client/1.0")

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

    # -- identity / bootstrap ---------------------------------------------
    def whoami(self) -> dict:
        """GET /user — resolves the token to its owning account. Doubles as the
        token-validity probe for `hq doctor` (raises on a bad/expired token)."""
        return self._req("GET", "/user")

    def repo_exists(self) -> bool:
        try:
            self._req("GET", self._repo_path())
            return True
        except RuntimeError as e:
            if "-> 404" in str(e):
                return False
            raise

    def ensure_repo(self, private: bool = True, description: str = "",
                    auto_init: bool = True) -> dict:
        """Create owner/repo if it doesn't exist yet (POST /user/repos, which
        creates under the token's own account). Idempotent: returns
        {'created': bool, 'repo': <name>}."""
        if self.repo_exists():
            return {"created": False, "repo": f"{self.owner}/{self.repo}"}
        self._req(
            "POST", "/user/repos",
            payload={
                "name": self.repo,
                "private": private,
                "description": description,
                "auto_init": auto_init,
            },
        )
        return {"created": True, "repo": f"{self.owner}/{self.repo}"}

    def ensure_labels(self, labels: list) -> dict:
        """Idempotent bulk label create. `labels` is a list of
        (name, color, exclusive) tuples. Returns {'created': [...], 'skipped': [...]}."""
        existing = {l["name"] for l in self.list_labels()}
        created, skipped = [], []
        for name, color, exclusive in labels:
            if name in existing:
                skipped.append(name)
                continue
            try:
                self.create_label(name, color, exclusive=exclusive)
                created.append(name)
            except RuntimeError as e:
                if "already exists" in str(e).lower():
                    skipped.append(name)
                else:
                    raise
        return {"created": created, "skipped": skipped}

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

    def issue_labels(self, number: int) -> list[str]:
        """Current label names on an issue — used by the queue's atomic claim to
        re-read who actually holds a job after an exclusive-label swap."""
        data = self.get_issue(number)
        return [l["name"] for l in (data.get("labels") or [])]

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
