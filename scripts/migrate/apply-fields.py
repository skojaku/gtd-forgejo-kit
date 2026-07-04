#!/usr/bin/env python3
"""apply-fields.py — replay the field snapshot captured by snapshot-fields.sh
onto a migrated Forgejo repo (PLAN.md sections 1 and 8):

  - status/*  and context/* exclusive scoped labels
  - due_date  (native Forgejo issue field)
  - hq-meta body block for defer/scheduled/duration/booked

Idempotent: label swap relies on Forgejo's exclusive-label enforcement
(adding status/next auto-removes any sibling status/* label), and the
hq-meta block is replaced — not appended — each run via common.write_meta()'s
find/replace semantics. Re-running after a partial/failed pass is safe.

Usage:
  scripts/migrate/apply-fields.py fields.json [--dry-run]
      [--owner O] [--repo R] [--forgejo-url U]

Defaults come from config/project.yaml via hqlib.common, with the usual
HQ_OWNER / HQ_REPO_NAME / FORGEJO_URL env overrides for targeting the
rehearsal instance, e.g.:

  FORGEJO_URL=http://localhost:3000 HQ_OWNER=youruser HQ_REPO_NAME=HQ-rehearsal \\
    scripts/migrate/apply-fields.py fields.json --dry-run
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "bin"))

from hqlib.common import (  # noqa: E402
    META_KEYS,
    forgejo_url as cfg_forgejo_url,
    load_config,
    owner as cfg_owner,
    repo_name as cfg_repo_name,
    write_meta,
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


def status_label(value: str | None) -> str | None:
    return value.strip().lower() if value else None


def context_label(value: str | None) -> str | None:
    """'@computer' -> 'computer' (context labels drop the leading '@')."""
    if not value:
        return None
    v = value.strip()
    return (v[1:] if v.startswith("@") else v).lower()


def due_date_value(due: str | None) -> str | None:
    return f"{due}T00:00:00Z" if due else None


def apply_one(fg: ForgejoClient, entry: dict, dry: bool) -> list[str]:
    """Apply one fields.json entry to its Forgejo issue. Returns a list of
    human-readable action descriptions (taken, or that would be taken)."""
    number = entry["issue"]
    actions: list[str] = []

    status = status_label(entry.get("status"))
    if status:
        actions.append(f"#{number}: label status/{status}")
        if not dry:
            fg.replace_scoped(number, "status", status)

    context = context_label(entry.get("context"))
    if context:
        actions.append(f"#{number}: label context/{context}")
        if not dry:
            fg.replace_scoped(number, "context", context)

    patch: dict = {}

    due_date = due_date_value(entry.get("due"))
    if due_date:
        patch["due_date"] = due_date
        actions.append(f"#{number}: due_date={due_date}")

    meta = {k: entry.get(k) for k in META_KEYS}
    if any(v not in (None, "") for v in meta.values()):
        current_body = fg.get_issue(number).get("body") or ""
        new_body = write_meta(current_body, meta)
        if new_body != current_body:
            patch["body"] = new_body
            payload = {k: v for k, v in meta.items() if v not in (None, "")}
            actions.append(f"#{number}: hq-meta {json.dumps(payload)}")

    if patch and not dry:
        fg.edit_issue(number, **patch)

    return actions


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("fields_json", help="path to fields.json produced by snapshot-fields.sh")
    ap.add_argument("--dry-run", action="store_true", help="log intended actions, mutate nothing")
    add_common_args(ap)
    args = ap.parse_args()

    path = Path(args.fields_json)
    if not path.exists():
        print(f"error: {path} not found", file=sys.stderr)
        sys.exit(1)
    entries = json.loads(path.read_text())

    fg = build_client(args)

    total_actions = 0
    touched_issues = 0
    for entry in entries:
        if not entry.get("issue"):
            continue
        actions = apply_one(fg, entry, args.dry_run)
        if actions:
            touched_issues += 1
        for a in actions:
            print(("[dry-run] " if args.dry_run else "") + a)
        total_actions += len(actions)

    verb = "would apply" if args.dry_run else "applied"
    print(f"{verb} {total_actions} field change(s) across {touched_issues}/{len(entries)} issue(s)")


if __name__ == "__main__":
    main()
