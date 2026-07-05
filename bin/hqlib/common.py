"""Shared plumbing: subprocess helpers, config loading, output capping.

Output contract (small-model friendly):
- every command prints JSON to stdout; errors are {"error": "..."} + exit 1
- list results use the envelope {"count": N, "shown": M, "truncated": bool, "results": [...]}
- a final size guard drops tail items rather than ever flooding the caller
"""
import json
import os
import re
import subprocess
import sys
from datetime import date
from pathlib import Path

MAX_OUTPUT_BYTES = 10_000


def fail(msg):
    print(json.dumps({"error": msg}))
    sys.exit(1)


def run(cmd, input_text=None, env=None, allow_fail=False):
    """allow_fail=True raises RuntimeError instead of exiting — for callers
    that must report partial success (e.g. issue created, project-add failed)."""
    try:
        proc = subprocess.run(cmd, input=input_text, capture_output=True, text=True, env=env)
    except FileNotFoundError as e:
        if allow_fail:
            raise RuntimeError(f"command not found: {e.filename}")
        fail(f"command not found: {e.filename}")
    if proc.returncode != 0:
        stderr = proc.stderr.strip() or proc.stdout.strip()
        msg = f"`{' '.join(cmd)}` failed: {stderr[:800]}"
        if allow_fail:
            raise RuntimeError(msg)
        fail(msg)
    return proc.stdout


def run_json(cmd, input_text=None, env=None, allow_fail=False):
    out = run(cmd, input_text, env, allow_fail=allow_fail)
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        msg = f"`{' '.join(cmd)}` did not return valid JSON: {out[:500]}"
        if allow_fail:
            raise RuntimeError(msg)
        fail(msg)


# --------------------------------------------------------------------------
# Paths and config
# --------------------------------------------------------------------------

def repo_root():
    """HQ repo root: parent of bin/. Override with $HQ_ROOT."""
    override = os.environ.get("HQ_ROOT")
    if override:
        return Path(override).expanduser()
    return Path(__file__).resolve().parents[2]


def config_path():
    override = os.environ.get("HQ_ENV")
    if override:
        return Path(override).expanduser()
    return repo_root() / "config" / "hq.yaml"


def _parse_yaml(path):
    """PyYAML if available, else yq — covers hosts that have only one of them."""
    try:
        import yaml
        return yaml.safe_load(path.read_text())
    except ImportError:
        pass
    raw = run(["yq", "-o=json", "eval", ".", str(path)])
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        fail(f"could not parse {path} as YAML")


_CONFIG_CACHE = None


def load_config():
    global _CONFIG_CACHE
    if _CONFIG_CACHE is not None:
        return _CONFIG_CACHE
    path = config_path()
    if not path.exists():
        fail(f"config not found at {path} (set $HQ_ENV to override, or run the setup skill)")
    cfg = _parse_yaml(path)
    if not isinstance(cfg, dict):
        fail(f"{path} did not parse to a mapping")
    _CONFIG_CACHE = cfg
    return cfg


def owner(cfg):
    """Forgejo/repo owner login. Override: $HQ_OWNER."""
    v = os.environ.get("HQ_OWNER") or (cfg.get("user") or {}).get("username")
    if not v:
        fail("config is missing user.username (or $HQ_OWNER)")
    return v


def repo(cfg):
    """Full 'owner/repo' slug — legacy display/URL accessor, unrelated to the
    bare name Forgejo API paths need (see repo_name())."""
    v = (cfg.get("repo") or {}).get("name")
    if not v:
        fail("config is missing repo.name")
    return v


def repo_name(cfg):
    """Bare repo name (no owner prefix) for Forgejo API path building, e.g.
    'HQ' or 'HQ-rehearsal'. Override: $HQ_REPO_NAME."""
    override = os.environ.get("HQ_REPO_NAME")
    if override:
        return override
    return repo(cfg).rsplit("/", 1)[-1]


def forgejo_url(cfg):
    """Base URL of the Forgejo instance, e.g. http://forgejo:3000.
    Override: $FORGEJO_URL."""
    v = os.environ.get("FORGEJO_URL") or cfg.get("forgejo_url")
    if not v:
        fail("config is missing forgejo_url (or $FORGEJO_URL)")
    return v.rstrip("/")


def client(cfg):
    """Shared ForgejoClient factory — the single construction point for the
    REST client, used by core (task/queue/dossier) and the mail/discord
    plugins so the (url, owner, repo) wiring lives in exactly one place.
    Imported lazily to keep this module free of a hard forgejo dependency."""
    from .forgejo import ForgejoClient
    return ForgejoClient(forgejo_url(cfg), owner(cfg), repo_name(cfg))


# --------------------------------------------------------------------------
# Output shaping
# --------------------------------------------------------------------------

# --------------------------------------------------------------------------
# gws (Google Workspace CLI) account resolution — shared by any plugin that
# talks to gws (mail, drive, ...), and by core's queue.py for thread-update
# bookkeeping. Lives here (not in the mail plugin) so plugins never import
# each other.
# --------------------------------------------------------------------------

def resolve_account(cfg, name):
    """Returns (account_key, config_dir_or_None, mail_url_index).

    Account values are either a bare config-dir string (legacy) or a mapping
    {config_dir, mail_url_index}. An empty config_dir means "use gws's own
    default" (single-account host).
    """
    gt = cfg.get("google") or {}
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


def excerpt(text, chars):
    """Whitespace-normalized, hard-truncated excerpt."""
    flat = " ".join((text or "").split())
    return flat[:chars] + ("…" if len(flat) > chars else "")


def envelope(results, total=None):
    return {
        "count": total if total is not None else len(results),
        "shown": len(results),
        "truncated": (total or len(results)) > len(results),
        "results": results,
    }


def print_json(obj):
    """Print with a final size guard: shrink envelope results rather than flood."""
    out = json.dumps(obj, indent=2, ensure_ascii=False)
    if len(out) > MAX_OUTPUT_BYTES and isinstance(obj, dict) and isinstance(obj.get("results"), list):
        results = obj["results"]
        while len(results) > 1 and len(json.dumps(obj, indent=2, ensure_ascii=False)) > MAX_OUTPUT_BYTES:
            results.pop()
        obj["shown"] = len(results)
        obj["truncated"] = True
        out = json.dumps(obj, indent=2, ensure_ascii=False)
    print(out)


def read_params(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError) as e:
        fail(f"could not read params file {path}: {e}")


def is_valid_date(s):
    try:
        date.fromisoformat(s)
        return True
    except (ValueError, TypeError):
        return False


# --------------------------------------------------------------------------
# hq-meta body block (see PLAN.md section 1) — Defer/Scheduled/Duration/Booked
# live in a single trailing HTML-comment JSON block, since Forgejo has no
# custom project fields to hold them.
# --------------------------------------------------------------------------

META_KEYS = ("defer", "scheduled", "duration", "booked")
_META_RE = re.compile(r"^<!-- hq-meta (\{.*\}) -->[ \t]*$", re.MULTILINE)


def read_meta(body):
    """Parse the trailing `<!-- hq-meta {...} -->` line, if any. Always
    returns all four keys (None when absent/null)."""
    meta = {k: None for k in META_KEYS}
    match = None
    for match in _META_RE.finditer(body or ""):
        pass  # last match wins; the block is always meant to be the last line
    if match:
        try:
            data = json.loads(match.group(1))
        except json.JSONDecodeError:
            data = {}
        for k in META_KEYS:
            if k in data:
                meta[k] = data[k]
    return meta


def write_meta(body, meta):
    """Strip any existing hq-meta block from body and append a fresh one
    reflecting `meta`. Null/absent keys are omitted from the JSON; the block
    itself is omitted entirely when all four keys are empty."""
    prose = _META_RE.sub("", body or "").rstrip("\n").rstrip()
    payload = {k: meta.get(k) for k in META_KEYS if meta.get(k) not in (None, "")}
    if not payload:
        return prose + "\n" if prose else ""
    line = f"<!-- hq-meta {json.dumps(payload)} -->"
    return f"{prose}\n\n{line}\n" if prose else f"{line}\n"
