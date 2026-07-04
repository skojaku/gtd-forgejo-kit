#!/usr/bin/env bash
# Mac runner (launchd, every 5 min while awake): claim heavy jobs from the
# entry-point host's queue over ssh and process them with the local mtplx
# model. Triage jobs are handled on the entry-point host itself.
set -uo pipefail
HQ="${HQ_ROOT:-$HOME/HQ}"
REMOTE="${HQ_QUEUE_REMOTE:-aster}"

git -C "$HQ" pull --ff-only --quiet || echo "warning: git pull failed, continuing with current checkout"

"$HQ/bin/hq" queue work --types collect --remote "$REMOTE" --max 3
