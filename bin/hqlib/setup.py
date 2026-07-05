"""hq setup — the tier-aware install wizard.

Three install tiers (see PLAN.md sections 4 and 7):

  Tier 1 (CLI only)  — write config/hq.yaml from prompts, check gws auth, and
                       verify the Forgejo instance is reachable with a valid token.
  Tier 2 (server)    — additionally write the compose .env (UID/GID/TZ auto-
                       detected), print the `docker compose ... up -d` command
                       (never run it), bootstrap the Forgejo repo + API token +
                       the 12 scoped GTD labels via the API, and print the
                       `ollama pull` commands to seed local models.
  Tier 3 (worker)    — write a worker .env pointing at the hub's forgejo_url and
                       print the `docker compose --profile worker up -d` command.

This is a human-facing wizard: it prompts on stdin and prints readable guidance
(the /setup skill is a thin AI wrapper around it). Every value can also be
supplied by a flag so the whole thing runs non-interactively (--non-interactive
uses flags + detected defaults only, never blocks on a prompt). It never runs
`docker compose` or `ollama pull` itself — those are PRINTED for the user.

All Forgejo/repo mutation is idempotent and reuses forgejo.py + common.py.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from .common import repo_root, config_path, forgejo_url, owner, repo_name, load_config, fail
from . import forgejo as fj

# The 12 exclusive scoped GTD labels (7 status + 5 context). Exact name/color/
# exclusive values carried over from the retired scripts/migrate/create-labels.py
# so a greenfield repo gets the identical GTD model the task/queue code expects.
GTD_LABELS = [
    ("status/inbox", "0075ca", True),
    ("status/next", "0e8a16", True),
    ("status/review", "fbca04", True),
    ("status/done", "6f42c1", True),
    ("status/deferred", "d93f0b", True),
    ("status/waiting", "c5def5", True),
    ("status/someday", "bfdadc", True),
    ("context/computer", "1d76db", True),
    ("context/writing", "5319e7", True),
    ("context/errands", "e99695", True),
    ("context/calls", "f9d0c4", True),
    ("context/reading", "c2e0c6", True),
]

# ruamel is imported lazily so the core CLI stays stdlib+PyYAML only; only
# `hq setup` (an interactive host command) needs it. See hqlib/install.py.
_yaml = None


def _get_yaml():
    global _yaml
    if _yaml is None:
        try:
            from ruamel.yaml import YAML
        except ModuleNotFoundError:
            fail("`hq setup` needs the ruamel.yaml package: pip install ruamel.yaml")
        _yaml = YAML()
        _yaml.preserve_quotes = True
        _yaml.indent(mapping=2, sequence=2, offset=0)
    return _yaml


# --------------------------------------------------------------------------
# Small I/O helpers — all human-facing text goes to stderr so a caller can
# still capture a clean stdout if it wants; prompts read from stdin.
# --------------------------------------------------------------------------

def _say(msg=""):
    print(msg, file=sys.stderr)


def _step(title):
    _say()
    _say(f"== {title} ==")


def _ok(msg):
    _say(f"  [ok] {msg}")


def _warn(msg):
    _say(f"  [!!] {msg}")


def _cmd(cmd):
    """Print a shell command for the user to run (setup never runs these)."""
    _say(f"    $ {cmd}")


def _ask(args, prompt, default=None):
    """Prompt on stdin. In --non-interactive mode never blocks: returns default.
    An empty answer falls back to default."""
    if getattr(args, "non_interactive", False) or not sys.stdin.isatty():
        return default
    suffix = f" [{default}]" if default else ""
    try:
        ans = input(f"  {prompt}{suffix}: ").strip()
    except EOFError:
        return default
    return ans or default


# --------------------------------------------------------------------------
# Detection helpers
# --------------------------------------------------------------------------

def _detect_uid_gid():
    try:
        uid = subprocess.run(["id", "-u"], capture_output=True, text=True).stdout.strip()
        gid = subprocess.run(["id", "-g"], capture_output=True, text=True).stdout.strip()
    except Exception:
        uid, gid = "1000", "1000"
    return uid or "1000", gid or "1000"


def _detect_tz():
    p = Path("/etc/timezone")
    if p.exists():
        tz = p.read_text().strip()
        if tz:
            return tz
    link = Path("/etc/localtime")
    if link.is_symlink():
        target = os.readlink(link)
        if "zoneinfo/" in target:
            return target.split("zoneinfo/", 1)[1]
    return os.environ.get("TZ") or "America/New_York"


def _gws_auth_ok():
    """Best-effort read-only gws auth probe. Returns (ok, detail). gws is only
    needed by the mail/cal/drive plugins, so a miss here is a warning, not fatal."""
    import shutil
    if shutil.which("gws") is None:
        return False, "gws not on PATH"
    try:
        r = subprocess.run(
            ["gws", "auth", "status"],
            capture_output=True, text=True, timeout=15,
        )
    except Exception as e:
        return False, str(e)
    if r.returncode == 0:
        return True, (r.stdout.strip()[:200] or "authenticated")
    return False, (r.stderr.strip() or r.stdout.strip() or "gws returned non-zero")


# --------------------------------------------------------------------------
# Config writers
# --------------------------------------------------------------------------

def _write_hq_yaml(args, answers):
    """Start from config/hq.example.yaml and overlay the answered top-level
    identity keys, preserving all the template's comments/structure."""
    dst = config_path()
    if dst.exists() and not args.force:
        _warn(f"{dst} already exists — leaving it untouched (pass --force to overwrite)")
        return dst, False
    example = repo_root() / "config" / "hq.example.yaml"
    if not example.exists():
        _warn(f"template {example} missing — cannot scaffold hq.yaml")
        return dst, False
    data = _get_yaml().load(example.read_text())
    data.setdefault("user", {})
    data["user"]["username"] = answers["username"]
    data["user"]["timezone"] = answers["timezone"]
    data.setdefault("repo", {})
    data["repo"]["name"] = answers["repo"]
    data["forgejo_url"] = answers["forgejo_url"]
    dst.parent.mkdir(parents=True, exist_ok=True)
    with dst.open("w") as f:
        _get_yaml().dump(data, f)
    _ok(f"wrote {dst}")
    return dst, True


def _write_env(args, values, path=None):
    """Rewrite root .env from .env.example, substituting KEY=value for every key
    in `values`. Preserves comments and any keys we don't override."""
    dst = path or (repo_root() / ".env")
    if dst.exists() and not args.force:
        _warn(f"{dst} already exists — leaving it untouched (pass --force to overwrite)")
        return dst, False
    example = repo_root() / ".env.example"
    lines = example.read_text().splitlines() if example.exists() else [
        f"{k}=" for k in values
    ]
    out, seen = [], set()
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in values:
                out.append(f"{key}={values[key]}")
                seen.add(key)
                continue
        out.append(line)
    for key, val in values.items():
        if key not in seen:
            out.append(f"{key}={val}")
    dst.write_text("\n".join(out) + "\n")
    _ok(f"wrote {dst}")
    return dst, True


# --------------------------------------------------------------------------
# Shared checks (Tier 1 tail)
# --------------------------------------------------------------------------

def _check_forgejo(args, url):
    _step("Forgejo reachability + token")
    try:
        ver = fj.reachable(url)
        _ok(f"reachable at {url} (forgejo {ver.get('version', '?')})")
    except RuntimeError as e:
        _warn(f"NOT reachable at {url}: {e}")
        _say("    FIX: check forgejo_url and that the forgejo container is up "
             "(hq doctor).")
        return
    try:
        cfg = load_config()
        who = fj.ForgejoClient(url, owner(cfg), repo_name(cfg)).whoami()
        _ok(f"token valid — authenticated as {who.get('login')}")
    except RuntimeError as e:
        _warn(f"token check failed: {e}")
        _say("    FIX: set $FORGEJO_TOKEN or create ~/.config/hq/forgejo-token "
             "(mode 600) with a valid token.")


def _check_gws():
    _step("Google Workspace (gws) auth")
    ok, detail = _gws_auth_ok()
    if ok:
        _ok(f"gws authenticated ({detail})")
    else:
        _warn(f"gws not ready: {detail}")
        _say("    FIX: install gws and run `gws auth` (only needed for the "
             "mail / cal / drive plugins).")


# --------------------------------------------------------------------------
# Tier flows
# --------------------------------------------------------------------------

def _collect_identity(args):
    _step("Core identity")
    username = args.username or _ask(args, "Forgejo username / repo owner", None)
    if not username:
        _say('{"error": "username required (prompt or --username)"}')
        sys.exit(1)
    default_repo = f"{username}/HQ"
    repo = args.repo or _ask(args, "Forgejo repo slug (owner/name)", default_repo) or default_repo
    tz = args.timezone or _ask(args, "IANA timezone", _detect_tz()) or _detect_tz()
    fu = args.forgejo_url or _ask(args, "Forgejo base URL", None)
    if not fu:
        _say('{"error": "forgejo_url required (prompt or --forgejo-url)"}')
        sys.exit(1)
    return {
        "username": username,
        "repo": repo,
        "timezone": tz,
        "forgejo_url": fu.rstrip("/"),
    }


def _bootstrap_forgejo(args, answers):
    """Tier-2 Forgejo greenfield: create the repo, mint a working token, and
    create the 12 GTD labels. Reads admin credentials from flags/prompts.

    Returns the list of label names created (for the caller to surface for human
    confirmation). Idempotent and best-effort: any step that can't run prints a
    manual FIX and the wizard continues."""
    _step("Forgejo bootstrap (repo + token + labels)")
    url = answers["forgejo_url"]
    slug_owner = answers["repo"].split("/")[0]
    bare_repo = answers["repo"].split("/")[-1]

    admin_user = args.admin_user or _ask(args, "Forgejo admin username", slug_owner) or slug_owner
    admin_pass = args.admin_password or _ask(args, "Forgejo admin password (leave blank to skip API bootstrap)", None)

    token = args.forgejo_token or os.environ.get("FORGEJO_TOKEN")

    if not token and admin_pass:
        try:
            token = fj.create_token(url, admin_user, admin_pass,
                                    name=args.token_name)
            _ok(f"minted API token '{args.token_name}'")
        except RuntimeError as e:
            _warn(f"could not mint token: {e}")

    if not token:
        _warn("no token available — skipping repo/label bootstrap")
        _say("    FIX: create an admin in the Forgejo web installer, generate a "
             "token (Settings > Applications), then re-run with --forgejo-token, "
             "or run these manually once the token is in the environment:")
        _cmd(f"FORGEJO_URL={url} hq setup --tier 2 --forgejo-token <TOKEN>")
        return []

    # Persist the token so subsequent commands (and this same run) find it.
    saved = fj.save_token(token)
    os.environ["FORGEJO_TOKEN"] = token
    _ok(f"stored token at {saved}")

    client = fj.ForgejoClient(url, slug_owner, bare_repo, token=token)
    try:
        r = client.ensure_repo(private=True, description="HQ — personal GTD system")
        _ok("repo exists" if not r["created"] else f"created repo {r['repo']}")
    except RuntimeError as e:
        _warn(f"repo bootstrap failed: {e}")
        _say("    FIX: create the repo in the Forgejo UI, then re-run.")
        return []

    try:
        res = client.ensure_labels(GTD_LABELS)
        _ok(f"labels: {len(res['created'])} created, {len(res['skipped'])} already present")
        if res["created"]:
            _say("    created: " + ", ".join(res["created"]))
        return res["created"]
    except RuntimeError as e:
        _warn(f"label bootstrap failed: {e}")
        _say("    FIX: re-run `hq setup --tier 2` once the token has write:issue scope.")
        return []


def _print_compose(profile, label):
    _step(f"Bring up the {label} stack (run this yourself — setup will not)")
    prof = f" --profile {profile}" if profile else ""
    _cmd(f"docker compose -f deploy/compose.yaml{prof} up -d")


def _print_ollama_pull(cfg):
    _step("Seed local ollama models (run these yourself)")
    runners = ((cfg.get("queue") or {}).get("runners") or {})
    models = []
    for r in runners.values():
        m = (r or {}).get("model")
        if m and m not in models:
            models.append(m)
    if not models:
        _warn("no models found under queue.runners in hq.yaml")
        return
    for m in models:
        _cmd(f"ollama pull {m}")


def _tier1(args):
    answers = _collect_identity(args)
    _write_hq_yaml(args, answers)
    _check_gws()
    _check_forgejo(args, answers["forgejo_url"])
    return answers


def _tier2(args):
    answers = _tier1(args)

    _step("Compose environment (.env)")
    uid, gid = _detect_uid_gid()
    values = {
        "UID": uid,
        "GID": gid,
        "TZ": answers["timezone"],
        "HQ_BIND_IP": args.bind_ip or _ask(args, "Address Forgejo binds to", "127.0.0.1") or "127.0.0.1",
        "HQ_DOMAIN": args.domain or _ask(args, "Public hostname of this hub", "") or "",
    }
    if args.forgejo_token or os.environ.get("FORGEJO_TOKEN"):
        values["FORGEJO_TOKEN"] = args.forgejo_token or os.environ["FORGEJO_TOKEN"]
    _ok(f"detected UID={uid} GID={gid} TZ={answers['timezone']}")
    _write_env(args, values)

    _print_compose("", "server (forgejo + ollama + hq-cron)")

    created = _bootstrap_forgejo(args, answers)

    try:
        cfg = load_config()
    except SystemExit:
        cfg = {"queue": {"runners": {}}}
    _print_ollama_pull(cfg)

    _step("Done")
    _say("  Next: run the two commands above, then `hq doctor` to verify the stack.")
    return created


def _tier3(args):
    _step("Worker identity")
    hub_url = args.forgejo_url or _ask(args, "Hub Forgejo base URL", None)
    if not hub_url:
        _say('{"error": "hub forgejo_url required (prompt or --forgejo-url)"}')
        sys.exit(1)
    hub_url = hub_url.rstrip("/")
    token = args.forgejo_token or os.environ.get("FORGEJO_TOKEN")

    uid, gid = _detect_uid_gid()
    values = {
        "UID": uid,
        "GID": gid,
        "TZ": args.timezone or _detect_tz(),
        "FORGEJO_URL": hub_url,
    }
    if token:
        values["FORGEJO_TOKEN"] = token
    else:
        _warn("no FORGEJO_TOKEN provided — the worker needs one to claim jobs")
        _say("    FIX: re-run with --forgejo-token, or set FORGEJO_TOKEN in the "
             "worker .env before bringing the service up.")
    _write_env(args, values)

    _step("gws on the worker")
    _say("  Run `gws auth` once on this machine so the collect worker can reach "
         "mail/drive.")

    _print_compose("worker", "worker")
    _step("Done")
    _say("  Verify with `hq doctor` once the worker container is up.")


def cmd_setup(args):
    tier = args.tier
    if tier is None:
        ans = _ask(args, "Install tier — 1 (CLI) / 2 (server) / 3 (worker)", "1")
        tier = int(ans or "1")
    if tier == 1:
        _tier1(args)
    elif tier == 2:
        _tier2(args)
    elif tier == 3:
        _tier3(args)
    else:
        _say('{"error": "tier must be 1, 2, or 3"}')
        sys.exit(1)


def register(sub):
    p = sub.add_parser(
        "setup",
        help="tier-aware install wizard (1=CLI, 2=server, 3=worker); prompts on stdin, PRINTS compose/ollama commands to run",
    )
    p.add_argument("--tier", type=int, choices=[1, 2, 3], help="install tier (skips the tier prompt)")
    p.add_argument("--non-interactive", action="store_true",
                   help="never block on a prompt — use flags + detected defaults only")
    p.add_argument("--force", action="store_true", help="overwrite existing config/hq.yaml and .env")
    # identity
    p.add_argument("--username")
    p.add_argument("--repo", help="Forgejo repo slug owner/name")
    p.add_argument("--timezone")
    p.add_argument("--forgejo-url", dest="forgejo_url")
    # tier 2/3 compose + bootstrap
    p.add_argument("--bind-ip", dest="bind_ip", help="HQ_BIND_IP for the compose .env")
    p.add_argument("--domain", help="HQ_DOMAIN for the compose .env")
    p.add_argument("--admin-user", dest="admin_user", help="Forgejo admin username for API bootstrap")
    p.add_argument("--admin-password", dest="admin_password", help="Forgejo admin password for API bootstrap")
    p.add_argument("--forgejo-token", dest="forgejo_token", help="existing token (skips minting one)")
    p.add_argument("--token-name", dest="token_name", default="hq-setup", help="name for the minted token")
    p.set_defaults(func=cmd_setup)
