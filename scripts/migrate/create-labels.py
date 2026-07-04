#!/usr/bin/env python3
"""create-labels.py — create the 12 exclusive scoped status/context labels
(PLAN.md section 1) on a target Forgejo repo, via hqlib.forgejo.ForgejoClient.

Idempotent: tolerates "already exists" from the API, and also skips any
label name already present, so it's safe to re-run.

Usage:
  scripts/migrate/create-labels.py [--owner O] [--repo R] [--forgejo-url U]

Defaults come from config/project.yaml via hqlib.common (owner/repo_name/
forgejo_url), which already respect the HQ_OWNER / HQ_REPO_NAME / FORGEJO_URL
env overrides — e.g. to target the rehearsal instance:

  FORGEJO_URL=http://localhost:3000 HQ_OWNER=youruser HQ_REPO_NAME=HQ-rehearsal \\
    scripts/migrate/create-labels.py
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "bin"))

from hqlib.common import (  # noqa: E402
    forgejo_url as cfg_forgejo_url,
    load_config,
    owner as cfg_owner,
    repo_name as cfg_repo_name,
)
from hqlib.forgejo import ForgejoClient  # noqa: E402

# name, color, exclusive — exact values from PLAN.md section 1.
LABELS = [
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


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    add_common_args(ap)
    args = ap.parse_args()

    fg = build_client(args)
    existing = {l["name"] for l in fg.list_labels()}

    created, skipped = [], []
    for name, color, exclusive in LABELS:
        if name in existing:
            skipped.append(name)
            continue
        try:
            fg.create_label(name, color, exclusive=exclusive)
            created.append(name)
        except RuntimeError as e:
            if "already exists" in str(e).lower():
                skipped.append(name)
            else:
                raise

    print(f"created {len(created)}: {created}")
    print(f"skipped (already existed) {len(skipped)}: {skipped}")
    if len(created) + len(skipped) != len(LABELS):
        print("warning: not all labels accounted for", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
