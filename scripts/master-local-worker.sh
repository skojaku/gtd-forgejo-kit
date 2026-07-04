#!/usr/bin/env bash
# master-local-worker.sh — let the master machine process dossier (collect)
# jobs itself when other workers are behind AND its own GPUs are idle enough.
#
# Collect jobs are normally claimed first by other workers (e.g.
# scripts/hq-runner.sh, pull-based over ssh) — they have claim priority. But a
# worker only runs while its host is awake/reachable, so jobs can sit in the
# queue indefinitely. This script is the master's own lower-priority worker.
#
# Two gates, BOTH must pass:
#   (1) Other-workers-idle: at least one collect job has been waiting in
#       pending for >= LOCAL_WORKER_IDLE_MIN minutes (default 20). If other
#       workers are keeping up, jobs never age that far and the master stays
#       out of the way.
#   (2) GPU-free: scripts/gpu-check.sh returns 0 (a GPU clears the VRAM floor
#       and util ceiling). If the GPUs are busy we HOLD — do nothing this
#       tick and let the next cron run retry, i.e. wait until they reopen.
#       GPU safety wins: we proceed ONLY on a clean rc=0, never on "busy" (1)
#       or "unknown" (2).
#
# When both pass, the master claims collect jobs from its LOCAL queue and
# runs them on its own local model via ollama, pinned through
# HQ_COLLECT_PI_ARGS so other workers' shared collect config is untouched.
#
# Idempotent + single-flight (flock). Safe to run on a tight cron.
set -uo pipefail

HQ="${HQ_ROOT:-/home/youruser/HQ}"
IDLE_MIN="${LOCAL_WORKER_IDLE_MIN:-20}"
MAX_JOBS="${LOCAL_WORKER_MAX:-2}"
MODEL="${LOCAL_WORKER_MODEL:-qwen3.6:27b}"   # 27B q4 build in ollama
LOCK="/tmp/master-local-worker.lock"

# Single-flight: skip silently if a previous run is still going (a 27B collect
# can outlast the cron interval).
exec 9>"$LOCK"
if ! flock -n 9; then
  echo "local-worker: previous run still active — skipping"
  exit 0
fi

PENDING="$HQ/queue/pending"
[[ -d "$PENDING" ]] || { echo "local-worker: no queue dir, nothing to do"; exit 0; }

# ── Gate 1: has any collect job been waiting too long? ──────────────────────
stale=$(find "$PENDING" -maxdepth 1 -name '*-collect-*.json' -mmin +"$IDLE_MIN" 2>/dev/null | head -1)
if [[ -z "$stale" ]]; then
  echo "local-worker: no collect job older than ${IDLE_MIN}m — other workers keeping up (or none pending)"
  exit 0
fi

# ── Gate 2: are the GPUs free enough? ───────────────────────────────────────
if ! "$HQ/scripts/gpu-check.sh"; then
  echo "local-worker: GPUs busy (or unqueryable) — holding, will retry next tick"
  exit 0
fi

# ── Both gates passed: process collect jobs locally ─────────────────────────
echo "local-worker: other workers behind (stale job present) + GPUs free -> processing up to ${MAX_JOBS} collect job(s) on ${MODEL}"
export HQ_COLLECT_PI_ARGS="--provider ollama --model ${MODEL}"
exec "$HQ/bin/hq" queue work --types collect --max "$MAX_JOBS"
