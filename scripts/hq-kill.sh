#!/usr/bin/env zsh
# hq-kill.sh — Kill HQ tmux sessions and their Docker containers
#
# Usage:
#   ./scripts/hq-kill.sh          # kill all (hq + hq-mobile)
#   ./scripts/hq-kill.sh hq       # kill only hq
#   ./scripts/hq-kill.sh mobile   # kill only hq-mobile

HQ_DIR="$HOME/service/hq-stack"
TARGETS=("${@:-all}")

kill_session() {
  local session="$1"
  if tmux has-session -t "$session" 2>/dev/null; then
    tmux kill-session -t "$session"
    echo "Killed tmux session: $session"
  else
    echo "No tmux session: $session"
  fi
}

for target in "${TARGETS[@]}"; do
  case "$target" in
    hq)      kill_session "hq" ;;
    mobile)  kill_session "hq-mobile" ;;
    all)     kill_session "hq"; kill_session "hq-mobile" ;;
    *)       echo "Unknown target: $target (use hq, mobile, or all)"; exit 1 ;;
  esac
done

# Stop all Docker containers from the compose file
cd "$HQ_DIR" && docker compose down 2>/dev/null && echo "Docker containers stopped." || true
