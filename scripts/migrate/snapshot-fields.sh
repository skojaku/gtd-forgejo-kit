#!/usr/bin/env bash
# snapshot-fields.sh — dump GitHub Projects V2 field values for every item in
# the live GTD project to fields.json, BEFORE cutover while GitHub is still
# the source of truth (PLAN.md section 8, step 1). Read-only.
#
# Reuses the TASKS_QUERY GraphQL shape from the pre-migration bin/hqlib/task.py
# (see `git show main:bin/hqlib/task.py`), trimmed to just the fields
# apply-fields.py needs to replay onto Forgejo.
#
# Output: a JSON array of
#   {issue, title, status, context, due, defer, scheduled, duration, booked}
# ("title" is an extra field beyond PLAN.md section 8's literal schema —
# added so verify-migration.py can assert title equality; harmless to ignore
# if you only need the six field values).
#
# Usage:
#   scripts/migrate/snapshot-fields.sh [output-path] [--owner LOGIN] [--project-number N]
#
# Env:
#   GH_TOKEN            GitHub token; read from .env (same as the rest of the
#                        codebase) if not already set.
#   HQ_OWNER             default for --owner (falls back to "youruser")
#   HQ_PROJECT_NUMBER    default for --project-number (falls back to 8 — the
#                        live GTD project's number, per config/project.yaml's
#                        pre-migration `project_number: 8`)
#
# Requires: gh (authenticated / GH_TOKEN set), jq.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

OWNER="${HQ_OWNER:-youruser}"
PROJECT_NUMBER="${HQ_PROJECT_NUMBER:-8}"
OUT="$REPO_ROOT/scripts/migrate/fields.json"

# Simple flag parsing so positional output path and flags can appear in any order.
while [ $# -gt 0 ]; do
  case "$1" in
    --owner)
      OWNER="$2"; shift 2 ;;
    --project-number)
      PROJECT_NUMBER="$2"; shift 2 ;;
    -h|--help)
      sed -n '2,30p' "$0"; exit 0 ;;
    *)
      OUT="$1"; shift ;;
  esac
done

for bin in gh jq; do
  command -v "$bin" >/dev/null 2>&1 || { echo "error: $bin not found on PATH" >&2; exit 1; }
done

# Load GH_TOKEN from .env the same way the rest of the codebase does, if not
# already set in the environment.
if [ -z "${GH_TOKEN:-}" ] && [ -f "$REPO_ROOT/.env" ]; then
  GH_TOKEN="$(grep -E '^GH_TOKEN=' "$REPO_ROOT/.env" | head -1 | cut -d= -f2-)"
  export GH_TOKEN
fi
if [ -z "${GH_TOKEN:-}" ]; then
  echo "error: GH_TOKEN not set and not found in $REPO_ROOT/.env" >&2
  exit 1
fi

QUERY='
query($login: String!, $number: Int!, $cursor: String) {
  user(login: $login) {
    projectV2(number: $number) {
      items(first: 100, after: $cursor) {
        pageInfo { hasNextPage endCursor }
        nodes {
          content {
            ... on Issue { number title }
          }
          fieldValues(first: 20) {
            nodes {
              ... on ProjectV2ItemFieldSingleSelectValue { name field { ... on ProjectV2SingleSelectField { name } } }
              ... on ProjectV2ItemFieldDateValue { date field { ... on ProjectV2Field { name } } }
              ... on ProjectV2ItemFieldNumberValue { number field { ... on ProjectV2Field { name } } }
            }
          }
        }
      }
    }
  }
}
'

JQ_MAP='
[
  .nodes[]
  | select(.content.number != null)
  | {
      issue: .content.number,
      title: .content.title,
      status:    ((.fieldValues.nodes[] | select(.field.name == "Status")    | .name) // null),
      context:   ((.fieldValues.nodes[] | select(.field.name == "Context")   | .name) // null),
      due:       ((.fieldValues.nodes[] | select(.field.name == "Due")       | .date) // null),
      defer:     ((.fieldValues.nodes[] | select(.field.name == "Defer")     | .date) // null),
      scheduled: ((.fieldValues.nodes[] | select(.field.name == "Scheduled") | .date) // null),
      duration:  ((.fieldValues.nodes[] | select(.field.name == "Duration")  | .number) // null),
      booked:    ((.fieldValues.nodes[] | select(.field.name == "Booked")    | .date) // null)
    }
]
'

acc='[]'
cursor=""

while true; do
  if [ -z "$cursor" ]; then
    page="$(gh api graphql -f query="$QUERY" -f login="$OWNER" -F number="$PROJECT_NUMBER" -F cursor=)"
  else
    page="$(gh api graphql -f query="$QUERY" -f login="$OWNER" -F number="$PROJECT_NUMBER" -F cursor="$cursor")"
  fi

  items="$(echo "$page" | jq '.data.user.projectV2.items')"
  batch="$(echo "$items" | jq "$JQ_MAP")"
  acc="$(jq -c -n --argjson a "$acc" --argjson b "$batch" '$a + $b')"

  has_next="$(echo "$items" | jq -r '.pageInfo.hasNextPage')"
  if [ "$has_next" != "true" ]; then
    break
  fi
  cursor="$(echo "$items" | jq -r '.pageInfo.endCursor')"
done

# GitHub's Number field is always a GraphQL Float (e.g. 90.0) — clean up to a
# plain int when it's whole, since Duration is documented as integer minutes.
echo "$acc" | jq 'map(
  if .duration != null and (.duration | floor) == .duration
  then .duration |= floor
  else . end
)' > "$OUT"
count="$(jq 'length' "$OUT")"
echo "wrote $count entries to $OUT" >&2
