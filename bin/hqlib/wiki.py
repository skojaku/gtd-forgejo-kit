"""hq wiki — ripgrep search over the local wiki/, with Forgejo blob URLs.

Read-only. Editing rules live in the wiki skill; this is just the search
entry point that returns dossier-ready links.
"""
import json
import subprocess
from urllib.parse import quote

from .common import fail, load_config, owner, repo_name, forgejo_url, repo_root, excerpt, envelope, print_json

_BRANCH_CACHE = None


def default_branch():
    global _BRANCH_CACHE
    if _BRANCH_CACHE:
        return _BRANCH_CACHE
    proc = subprocess.run(
        ["git", "-C", str(repo_root()), "symbolic-ref", "--short", "HEAD"],
        capture_output=True, text=True,
    )
    _BRANCH_CACHE = proc.stdout.strip() if proc.returncode == 0 else "main"
    return _BRANCH_CACHE


def blob_url(cfg, relpath):
    return f"{forgejo_url(cfg)}/{owner(cfg)}/{repo_name(cfg)}/src/branch/{default_branch()}/{quote(relpath)}"


def cmd_find(args):
    cfg = load_config()
    root = repo_root()
    search_dir = root / args.dir
    if not search_dir.is_dir():
        fail(f"no directory {search_dir}")

    proc = subprocess.run(
        ["rg", "-i", "--json", "--max-count", "3", args.terms, str(search_dir)],
        capture_output=True, text=True,
    )
    if proc.returncode not in (0, 1):  # 1 = no matches
        fail(f"rg failed: {proc.stderr.strip()[:300]}")

    by_file = {}
    for line in proc.stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") != "match":
            continue
        data = event["data"]
        path = data["path"]["text"]
        matched = data["lines"]["text"]
        entry = by_file.setdefault(path, {"lines": [], "n": 0})
        entry["n"] += 1
        if len(entry["lines"]) < 2:
            entry["lines"].append(excerpt(matched, 200))

    results = []
    for path, entry in sorted(by_file.items(), key=lambda kv: -kv[1]["n"]):
        rel = str(path).replace(str(root) + "/", "", 1)
        results.append({
            "path": rel,
            "matches": entry["n"],
            "lines": entry["lines"],
            "url": blob_url(cfg, rel),
        })

    total = len(results)
    print_json(envelope(results[: args.max], total))


def register(sub):
    p = sub.add_parser("wiki", help="search local notes (read-only)")
    s2 = p.add_subparsers(dest="wiki_cmd", required=True)

    s = s2.add_parser("find", help="ripgrep the wiki; returns paths, matching lines, Forgejo links")
    s.add_argument("terms", help="search pattern (case-insensitive regex)")
    s.add_argument("--dir", default="wiki", help="directory relative to repo root (default: wiki)")
    s.add_argument("--max", type=int, default=10)
    s.set_defaults(func=cmd_find)
