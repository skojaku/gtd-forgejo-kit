#!/usr/bin/env python3
"""verify-migration.py — post-migration sanity checks comparing the GitHub
source repo against the migrated Forgejo repo (PLAN.md sections 8 and 10).

Checks:
  1. open + closed issue counts match between GitHub and Forgejo.
  2. every issue in fields.json exists on Forgejo, with a matching title.
  3. the number of Forgejo issues carrying a status/* label equals the
     number of entries in fields.json.
  4. prints N (default 5) random Forgejo issues for manual spot-check.

Prints a report to stdout and exits 1 if any assertion fails.

Usage:
  scripts/migrate/verify-migration.py fields.json
      [--github-repo owner/repo] [--owner O] [--repo R] [--forgejo-url U]
      [--sample N]

Note: fields.json's schema per PLAN.md section 8 is
{issue, status, context, due, defer, scheduled, duration, booked} — this
script additionally expects a "title" key (added by this repo's
snapshot-fields.sh) so check #2 can compare titles; if you supply a
fields.json without "title", that half of check #2 is skipped.

Requires `gh` authenticated against GitHub (same GH_TOKEN as the rest of the
codebase) to read the GitHub-side counts.
"""
from __future__ import annotations

import argparse
import json
import random
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "bin"))

from hqlib.common import (  # noqa: E402
    forgejo_url as cfg_forgejo_url,
    load_config,
    owner as cfg_owner,
    repo as cfg_repo,
    repo_name as cfg_repo_name,
)
from hqlib.forgejo import ForgejoClient  # noqa: E402


def add_common_args(ap: argparse.ArgumentParser) -> None:
    ap.add_argument("--owner", help="Forgejo owner (default: config / $HQ_OWNER)")
    ap.add_argument(
        "--repo",
        help="bare Forgejo repo name, e.g. HQ or HQ-rehearsal (default: config / $HQ_REPO_NAME)",
    )
    ap.add_argument("--forgejo-url", help="Forgejo base URL (default: config / $FORGEJO_URL)")


def build_client(args: argparse.Namespace) -> ForgejoClient:
    cfg = load_config()
    return ForgejoClient(
        args.forgejo_url or cfg_forgejo_url(cfg),
        args.owner or cfg_owner(cfg),
        args.repo or cfg_repo_name(cfg),
    )


def gh_issue_count(github_repo: str, state: str) -> int:
    proc = subprocess.run(
        [
            "gh", "issue", "list", "--repo", github_repo, "--state", state,
            "--limit", "1000", "--json", "number",
        ],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        print(f"error: gh issue list --state {state} failed: {proc.stderr.strip()}", file=sys.stderr)
        sys.exit(1)
    return len(json.loads(proc.stdout))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("fields_json", help="path to fields.json produced by snapshot-fields.sh")
    ap.add_argument("--github-repo", help="GitHub owner/repo slug (default: config repo.name)")
    add_common_args(ap)
    ap.add_argument("--sample", type=int, default=5, help="number of random issues to print (default 5)")
    args = ap.parse_args()

    path = Path(args.fields_json)
    if not path.exists():
        print(f"error: {path} not found", file=sys.stderr)
        sys.exit(1)
    entries = json.loads(path.read_text())
    entries_by_num = {e["issue"]: e for e in entries if e.get("issue")}

    cfg = load_config()
    github_repo = args.github_repo or cfg_repo(cfg)
    fg = build_client(args)

    failures: list[str] = []

    # 1. open/closed counts
    gh_open = gh_issue_count(github_repo, "open")
    gh_closed = gh_issue_count(github_repo, "closed")
    fg_issues = fg.list_issues(state="all")
    fg_open = sum(1 for i in fg_issues if i.get("state") == "open")
    fg_closed = sum(1 for i in fg_issues if i.get("state") == "closed")

    print(f"GitHub  ({github_repo}): open={gh_open} closed={gh_closed} total={gh_open + gh_closed}")
    print(f"Forgejo ({fg.owner}/{fg.repo}): open={fg_open} closed={fg_closed} total={fg_open + fg_closed}")
    if gh_open != fg_open or gh_closed != fg_closed:
        failures.append(
            f"issue counts differ: GitHub open={gh_open}/closed={gh_closed} "
            f"vs Forgejo open={fg_open}/closed={fg_closed}"
        )

    # 2. every fields.json issue exists on Forgejo, with a matching title
    fg_by_num = {i["number"]: i for i in fg_issues}
    missing, title_mismatch = [], []
    for num, entry in entries_by_num.items():
        fi = fg_by_num.get(num)
        if fi is None:
            missing.append(num)
            continue
        want_title = entry.get("title")
        if want_title and fi.get("title") != want_title:
            title_mismatch.append((num, want_title, fi.get("title")))
    if missing:
        failures.append(f"{len(missing)} fields.json issue(s) missing on Forgejo: {sorted(missing)[:20]}")
    if title_mismatch:
        failures.append(f"{len(title_mismatch)} title mismatch(es): {title_mismatch[:5]}")
    print(f"fields.json coverage: {len(entries_by_num) - len(missing)}/{len(entries_by_num)} found on Forgejo")

    # 3. status-labeled Forgejo issue count == fields.json entry count
    status_labeled = sum(
        1 for i in fg_issues
        if any(l["name"].startswith("status/") for l in (i.get("labels") or []))
    )
    print(f"status-labeled Forgejo issues: {status_labeled}, fields.json entries: {len(entries_by_num)}")
    if status_labeled != len(entries_by_num):
        failures.append(
            f"status-labeled count ({status_labeled}) != fields.json entries ({len(entries_by_num)})"
        )

    # 4. spot-check sample
    sample = random.sample(fg_issues, min(args.sample, len(fg_issues)))
    print(f"\nspot-check ({len(sample)} random issue(s)):")
    for i in sample:
        print(f"  #{i['number']} [{i['state']}] {i['title']}")

    if failures:
        print("\nFAILURES:")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)

    print("\nOK — all checks passed")


if __name__ == "__main__":
    main()
