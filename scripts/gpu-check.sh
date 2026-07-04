#!/usr/bin/env bash
# gpu-check.sh — GPU availability gate for local-model agent runs.
#
# Exit codes:
#   0  at least one local GPU has enough free VRAM and low enough utilization to
#      host a local LLM without disrupting other GPU jobs  → OK to launch
#   1  all GPUs are too busy  → hold
#   2  availability could not be determined  → caller decides (we fail-open)
#
# How it queries GPUs: the runner container has no nvidia-smi and there is no
# nvidia container runtime, so we run nvidia-smi inside a throwaway container via
# device + driver-lib bind-mount (DooD). The -v/--device paths are resolved by
# the HOST docker daemon, so they refer to the HOST's files even though this
# script runs inside the runner container. No nvidia container toolkit required.
#
# Tunables (env):
#   GPU_MIN_FREE_MB  free VRAM (MiB) a GPU must have to count as available [20000]
#   GPU_MAX_UTIL     max compute util% a GPU may show to count available    [30]
#   GPU_DEVICES      host device nodes to expose (space-separated). Default is
#                    this 4-GPU host; override if the GPU count changes.
#   GPU_IMAGE        image used to run nvidia-smi                  [hq-container]
set -uo pipefail

GPU_MIN_FREE_MB="${GPU_MIN_FREE_MB:-20000}"
GPU_MAX_UTIL="${GPU_MAX_UTIL:-30}"
GPU_IMAGE="${GPU_IMAGE:-hq-container}"
GPU_DEVICES="${GPU_DEVICES:-/dev/nvidiactl /dev/nvidia-uvm /dev/nvidia0 /dev/nvidia1 /dev/nvidia2 /dev/nvidia3}"

dev_args=()
for d in $GPU_DEVICES; do dev_args+=(--device "$d"); done

out=$(timeout 60 docker run --rm "${dev_args[@]}" \
  -v /usr/bin/nvidia-smi:/usr/bin/nvidia-smi:ro \
  -v /usr/lib/x86_64-linux-gnu/libnvidia-ml.so.1:/usr/lib/x86_64-linux-gnu/libnvidia-ml.so.1:ro \
  --entrypoint nvidia-smi "$GPU_IMAGE" \
  --query-gpu=utilization.gpu,memory.used,memory.total \
  --format=csv,noheader,nounits 2>/dev/null)
rc=$?
if [[ $rc -ne 0 || -z "$out" ]]; then
  echo "gpu-check: could not query GPUs (rc=$rc)" >&2
  exit 2
fi

# Available if ANY GPU clears both the free-VRAM floor and the util ceiling.
echo "$out" | awk -F', *' -v minf="$GPU_MIN_FREE_MB" -v maxu="$GPU_MAX_UTIL" '
  { util=$1+0; used=$2+0; total=$3+0; free=total-used;
    if (free>=minf && util<=maxu) ok=1 }
  END { exit (ok?0:1) }'
