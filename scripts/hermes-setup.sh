#!/usr/bin/env bash
# hermes-setup.sh — create/refresh HQ's isolated Hermes profile ("hq-local").
# Idempotent; safe to re-run.
#
# HQ runs Hermes for its agentic jobs (collect dossiers, the Discord gateway) on
# ONE dedicated profile, "hq-local", pinned to a LOCAL ollama model. This keeps
# GTD data on the tailnet — it never reaches a cloud endpoint. The profile is
# fully separated from any other Hermes config on the host: changing another
# profile's model/brain never affects HQ runs, and vice versa.
#
# This does NOT touch ~/.hermes/config.yaml (the host default profile). It only
# writes ~/.hermes/profiles/hq-local/. It drives the `hermes` binary inside the
# hq-container image so the files land with the right ownership in the host's
# bind-mounted ~/.hermes.
#
# Prereq: Hermes set up on the host (~/.hermes/config.yaml present) and the
# hq-container image built. Usage:  ./scripts/hermes-setup.sh
set -euo pipefail

HERMES_DIR="${HOME}/.hermes"
IMAGE="hq-container"
PROFILE="hq-local"
MODEL="qwen3.6:27b"                          # collect/discord local model (27B)
# ollama's compose service DNS; the OpenAI-compatible endpoint hermes talks to.
OLLAMA_URL="${OLLAMA_URL:-http://ollama:11434}/v1"
# Docker network for the container that drives hermes. The profile-writing calls
# don't need ollama, so they default to the host network; the smoke test needs
# to reach ollama, so run this script with HQ_NETWORK set to your compose
# network (e.g. HQ_NETWORK=hq_hq ./scripts/hermes-setup.sh) to exercise it.
HQ_NETWORK="${HQ_NETWORK:-host}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SOUL_TEMPLATE="$REPO_ROOT/docker/hermes-hq-soul.md"   # committed collector contract

if [[ ! -f "$HERMES_DIR/config.yaml" ]]; then
  echo "ERROR: $HERMES_DIR/config.yaml not found." >&2
  echo "Set up Hermes on the host first (hermes setup); this script only adds the" >&2
  echo "hq-local profile and refuses to create a default config from scratch." >&2
  exit 1
fi

# Drive hermes inside the image, HOME=/home/hq and the repo at /hq (matching
# deploy/compose.yaml), writing into the host-bind-mounted ~/.hermes.
run_hermes() {
  docker run --rm --network "$HQ_NETWORK" \
    --user "$(id -u):$(id -g)" \
    -e HOME=/home/hq \
    -v "$HERMES_DIR:/home/hq/.hermes" \
    -v "$REPO_ROOT:/hq" \
    --entrypoint hermes "$IMAGE" "$@"
}

echo "→ Ensuring isolated '$PROFILE' profile exists..."
if ! run_hermes profile list 2>/dev/null | grep -qw "$PROFILE"; then
  run_hermes profile create "$PROFILE"
else
  echo "  (already exists)"
fi

echo "→ Pinning '$PROFILE' to LOCAL model '$MODEL' (never a cloud endpoint)..."
mkdir -p "$HERMES_DIR/profiles/$PROFILE/memories"
cat > "$HERMES_DIR/profiles/$PROFILE/config.yaml" <<EOF
# Managed by scripts/hermes-setup.sh — the 'hq-local' brain. LOCAL-ONLY:
# HQ/GTD data must never be routed to a cloud endpoint.
model:
  default: "$MODEL"
  provider: "ollama"
  base_url: "$OLLAMA_URL"
  api_key: "ollama"
# Skill sources beyond the builtins: the global cross-project skills under
# ~/.agents/skills (mounted at /home/hq/.agents) plus this repo's own skills
# (task, mail, cal, drive, collect, triage, wiki) under /hq/.claude/skills. Both
# are bind-mounted by deploy/compose.yaml; this just points hermes at them.
skills:
  external_dirs:
    - /home/hq/.agents/skills
    - /hq/.claude/skills
# Tool shells run the hq CLI, which must start in the repo (/hq) and reach the
# host's Google Workspace creds and the Forgejo token. Hermes sandboxes tool
# env, so pin cwd to the repo and pass through the non-secret vars. The Forgejo
# token comes from FORGEJO_TOKEN (from .env) or ~/.config/hq/forgejo-token
# (mounted read-only) — the same two ways forgejo.py resolves it. No gh: HQ
# talks to Forgejo over its API, not the GitHub CLI.
terminal:
  cwd: /hq
  env_passthrough:
    - HOME
    - PATH
    - TZ
    - FORGEJO_URL
    - FORGEJO_TOKEN
    - GOOGLE_WORKSPACE_CLI_KEYRING_BACKEND
    - GOOGLE_WORKSPACE_CLI_CONFIG_DIR
# Discord adapter — the bot token + access policy (GATEWAY_ALLOW_ALL_USERS or
# DISCORD_ALLOWED_USERS) live in this profile's .env, not here. Consumed by the
# hq-discord compose service (hermes -p hq-local gateway run).
discord:
  require_mention: false
  free_response_channels: ''
  allowed_channels: ''
  auto_thread: true
  thread_require_mention: false
  history_backfill: true
  history_backfill_limit: 50
  reactions: true
EOF

# Seed SOUL.md from the committed collector contract so the agent drives `hq`
# and respects the collector boundaries (Inbox + drafts only; never send/book/
# close). Overwritten every run — the authoritative copy lives in git.
if [[ -f "$SOUL_TEMPLATE" ]]; then
  echo "→ Installing HQ collector contract into '$PROFILE' SOUL.md..."
  cp "$SOUL_TEMPLATE" "$HERMES_DIR/profiles/$PROFILE/SOUL.md"
else
  echo "  ⚠ SOUL template not found at $SOUL_TEMPLATE — skipping"
fi

echo "→ Smoke test ('$PROFILE' brain, local model)..."
if run_hermes -p "$PROFILE" -z "Reply with exactly: ${PROFILE}-ready" 2>/dev/null | grep -q "${PROFILE}-ready"; then
  echo "  ✓ $PROFILE brain OK"
else
  echo "  ⚠ ${PROFILE}-brain smoke test failed. If HQ_NETWORK is 'host', hermes"
  echo "    can't reach ollama:11434 — re-run with HQ_NETWORK=<compose-network>"
  echo "    (e.g. HQ_NETWORK=hq_hq), and confirm model '$MODEL' is pulled in ollama."
fi

cat <<EOF

Hermes 'hq-local' profile ready (isolated, LOCAL model '$MODEL').
  Brain (memory) : $HERMES_DIR/profiles/$PROFILE/memories/MEMORY.md
  Contract       : $HERMES_DIR/profiles/$PROFILE/SOUL.md
  Model endpoint : $OLLAMA_URL  (ollama)

Used by:
  * collect jobs  — 'hq queue work --types collect' shells 'hermes -z -p $PROFILE'.
  * Discord       — the opt-in 'hq-discord' compose service
                    ('docker compose --profile discord up -d').

Before starting hq-discord:
  1. Create a bot at https://discord.com/developers/applications, invite it to
     your server, and enable the MESSAGE CONTENT intent.
  2. echo "DISCORD_BOT_TOKEN=<token>" >> $HERMES_DIR/profiles/$PROFILE/.env
  3. Access policy — either:
       echo "GATEWAY_ALLOW_ALL_USERS=true" >> $HERMES_DIR/profiles/$PROFILE/.env
     or
       echo "DISCORD_ALLOWED_USERS=id1,id2" >> $HERMES_DIR/profiles/$PROFILE/.env
EOF
