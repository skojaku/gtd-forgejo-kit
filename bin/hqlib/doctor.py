"""hq doctor — read-only health check for an HQ install.

Prints PASS/FAIL for each check; every failure carries a FIX line. Doctor NEVER
mutates anything: it reads config, probes Forgejo/ollama over HTTP, and inspects
containers with a read-only `docker ps`. It is the companion to `hq setup` — run
it after setup and any time the automation looks stuck.

Checks:
  1. required binaries on PATH
  2. config/hq.yaml present with the keys the CLI needs
  3. Forgejo reachability (unauthenticated /version)
  4. Forgejo token validity (/user)
  5. ollama reachability (/api/tags)
  6. container status (docker ps, read-only)
  7. last cron-run freshness (queue/state.json)
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from .common import (
    repo_root, config_path, load_config, forgejo_url, owner, repo_name,
)
from . import forgejo as fj
from . import ollama

# Binaries and where to get them. Some are only needed at Tier 2+, flagged
# optional so a Tier-1 laptop doesn't fail on docker/hermes/ollama.
REQUIRED_BINS = [
    ("python3", "your OS package manager", False),
    ("git", "brew install git / apt install git", False),
    ("rg", "brew install ripgrep / apt install ripgrep", False),
    ("gws", "https://github.com/google/googleworkspace-cli", True),
    ("hermes", "install the Hermes agent (Tier 2 collect/discord only)", True),
    ("ollama", "https://ollama.com/download (Tier 2 only; runs in a container in compose)", True),
    ("docker", "https://docs.docker.com/engine/install/ (Tier 2/3 only)", True),
]

# hq.yaml keys the CLI reads. Dotted paths.
REQUIRED_KEYS = [
    "user.username",
    "user.timezone",
    "repo.name",
    "forgejo_url",
    "queue.runners",
]

EXPECTED_CONTAINERS = ["forgejo", "ollama", "hq-cron"]

# A cron sweep runs every 5 min; anything older than this means the loop is
# wedged (or the container is down).
CRON_STALE_SEC = 30 * 60


class Report:
    def __init__(self):
        self.failed = 0

    def check(self, name, ok, detail="", fix=None, warn_only=False):
        if ok:
            tag = "PASS"
        elif warn_only:
            tag = "WARN"
        else:
            tag = "FAIL"
            self.failed += 1
        line = f"[{tag}] {name}"
        if detail:
            line += f" — {detail}"
        print(line)
        if not ok and fix:
            print(f"       FIX: {fix}")


def _dig(cfg, dotted):
    cur = cfg
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def _check_bins(rep):
    for name, hint, optional in REQUIRED_BINS:
        present = shutil.which(name) is not None
        rep.check(
            f"binary: {name}", present,
            "on PATH" if present else "missing",
            fix=f"install it ({hint})",
            warn_only=optional,
        )


def _load_cfg_or_none(rep):
    path = config_path()
    if not path.exists():
        rep.check("config: hq.yaml present", False, str(path),
                  fix="run `hq setup` (Tier 1) to write config/hq.yaml")
        return None
    rep.check("config: hq.yaml present", True, str(path))
    try:
        cfg = load_config()
    except SystemExit:
        rep.check("config: parses", False, "could not parse YAML",
                  fix="fix the YAML syntax in config/hq.yaml")
        return None
    missing = [k for k in REQUIRED_KEYS if _dig(cfg, k) in (None, "")]
    rep.check(
        "config: required keys", not missing,
        "all present" if not missing else f"missing: {', '.join(missing)}",
        fix="add the missing keys (see config/hq.example.yaml) or re-run `hq setup`",
    )
    return cfg


def _check_forgejo(rep, cfg):
    try:
        url = forgejo_url(cfg)
    except SystemExit:
        rep.check("forgejo: url configured", False, "no forgejo_url",
                  fix="set forgejo_url in hq.yaml or $FORGEJO_URL")
        return
    try:
        ver = fj.reachable(url)
        rep.check("forgejo: reachable", True, f"{url} (v{ver.get('version', '?')})")
    except RuntimeError as e:
        rep.check("forgejo: reachable", False, str(e)[:160],
                  fix=f"confirm {url} is up (docker ps) and reachable over the tailnet")
        return
    try:
        who = fj.ForgejoClient(url, owner(cfg), repo_name(cfg)).whoami()
        rep.check("forgejo: token valid", True, f"authenticated as {who.get('login')}")
    except RuntimeError as e:
        rep.check("forgejo: token valid", False, str(e)[:160],
                  fix="set $FORGEJO_TOKEN or write ~/.config/hq/forgejo-token (mode 600)")


def _check_ollama(rep, cfg):
    url = ollama.base_url(cfg)
    try:
        models = ollama.list_models(cfg)
        rep.check("ollama: reachable", True, f"{url} ({len(models)} models)")
    except RuntimeError as e:
        rep.check("ollama: reachable", False, str(e)[:160],
                  fix=f"confirm the ollama container is up (docker ps) and OLLAMA_URL/{url} is correct",
                  warn_only=True)


def _check_containers(rep):
    if shutil.which("docker") is None:
        rep.check("containers: status", True, "docker not installed (Tier 1 — skipped)",
                  warn_only=True)
        return
    try:
        out = subprocess.run(
            ["docker", "ps", "--format", "{{.Names}}"],
            capture_output=True, text=True, timeout=15,
        )
    except Exception as e:
        rep.check("containers: status", False, str(e),
                  fix="check the docker daemon is running", warn_only=True)
        return
    if out.returncode != 0:
        rep.check("containers: status", False, out.stderr.strip()[:160],
                  fix="check the docker daemon is running", warn_only=True)
        return
    running = set(out.stdout.split())
    for name in EXPECTED_CONTAINERS:
        up = name in running
        rep.check(
            f"container: {name}", up, "running" if up else "not running",
            fix="docker compose -f deploy/compose.yaml up -d",
            warn_only=True,
        )


def _check_cron_freshness(rep, cfg):
    d = repo_root() / ((cfg.get("queue") or {}).get("dir", "queue")) if cfg else repo_root() / "queue"
    state = d / "state.json"
    if not state.exists():
        rep.check("cron: last run", False, f"no {state}",
                  fix="start the hq-cron container; it writes queue/state.json each sweep",
                  warn_only=True)
        return
    age = time.time() - state.stat().st_mtime
    fresh = age < CRON_STALE_SEC
    mins = int(age // 60)
    rep.check(
        "cron: last run", fresh,
        f"queue/state.json updated {mins} min ago",
        fix="check the hq-cron container logs (docker logs hq-cron) — the sweep loop may be wedged",
        warn_only=True,
    )


def cmd_doctor(args):
    rep = Report()
    print("hq doctor — read-only health check\n")
    _check_bins(rep)
    print()
    cfg = _load_cfg_or_none(rep)
    print()
    if cfg is not None:
        _check_forgejo(rep, cfg)
        print()
        _check_ollama(rep, cfg)
        print()
    _check_containers(rep)
    print()
    _check_cron_freshness(rep, cfg)
    print()
    if rep.failed:
        print(f"{rep.failed} check(s) FAILED — see FIX lines above.")
        sys.exit(1)
    print("all required checks passed.")


def register(sub):
    p = sub.add_parser(
        "doctor",
        help="read-only health check: binaries, config, Forgejo/ollama, containers, cron freshness (prints a FIX per failure)",
    )
    p.set_defaults(func=cmd_doctor)
