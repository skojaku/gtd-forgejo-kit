#!/usr/bin/env bash
# hermes-setup.sh — ensure GitHub HQ's isolated Hermes profiles exist.
# Idempotent; safe to re-run.
#
# IMPORTANT: this script does NOT touch ~/.hermes/config.yaml. That file belongs
# to the always-on host gateway (hermes-gateway.service) and defines the DEFAULT
# profile + brain, which is now Discord-ONLY. GitHub HQ runs are FULLY SEPARATED
# onto their own profiles, each with its own brain:
#   * hq-github — non-protected GitHub issues. LOCAL qwen3.6:27b, own brain.
#   * protected — protected-data issues. LOCAL qwen, own brain; data never reaches
#                 a cloud endpoint and never lands in any other brain.
# Separating these means changing the gateway's model/config/brain never affects
# GitHub runs, and vice versa.
#
# It drives the `hermes` binary inside hq-container, writing into the host's
# bind-mounted ~/.hermes with the correct ownership.
#
# Prereq: Hermes must already be set up on the host (config.yaml present), which
# it is once the gateway has been configured. Usage:  ./scripts/hermes-setup.sh
set -euo pipefail

HERMES_DIR="${HOME}/.hermes"
IMAGE="hq-container"
OLLAMA_URL="http://localhost:11434/v1"   # local ollama — serves local + cloud models
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SOUL_TEMPLATE="$REPO_ROOT/docker/hermes-hq-soul.md"   # committed collector contract

if [[ ! -f "$HERMES_DIR/config.yaml" ]]; then
  echo "ERROR: $HERMES_DIR/config.yaml not found." >&2
  echo "Set up Hermes / the gateway on the host first; this script only adds the" >&2
  echo "GitHub HQ profiles and refuses to create a default config from scratch." >&2
  exit 1
fi

run_hermes() {
  docker run --rm --network host \
    --user "$(id -u):$(id -g)" \
    -e HOME=$HOME \
    -v "$HERMES_DIR:$HOME/.hermes" \
    --entrypoint hermes "$IMAGE" "$@"
}

# ensure_profile <name> <local-model> [discord] — create the profile (if
# missing) and pin it to a LOCAL model with its own brain, isolated from the
# gateway's default. Pass "discord" as the 3rd arg to also write a Discord
# adapter block (bot token/access policy still come from the profile's own
# .env — never written by this script).
ensure_profile() {
  local name="$1" model="$2" want_discord="${3:-}"
  echo "→ Ensuring isolated '$name' profile exists..."
  if ! run_hermes profile list 2>/dev/null | grep -qw "$name"; then
    run_hermes profile create "$name"
  else
    echo "  (already exists)"
  fi

  echo "→ Pinning '$name' profile to LOCAL model '$model' (never a :cloud endpoint)..."
  mkdir -p "$HERMES_DIR/profiles/$name/memories"
  {
    cat <<EOF
# Managed by scripts/hermes-setup.sh — '$name' brain. LOCAL-ONLY:
# GitHub HQ data must never be routed to a cloud endpoint.
model:
  default: "$model"
  provider: "ollama"
  base_url: "$OLLAMA_URL"
  api_key: "ollama"
# Skill sources beyond the builtins — the global cross-project skills
# (find-skills, gws-calendar, gws-gmail, ...) plus this repo's own skills
# (task, mail, cal, drive, collect, triage, wiki via .claude/skills symlinks).
# Both are bind-mounted into the container by docker-compose; this just makes
# Hermes look there. NOT $HOME/HQ/skills (removed) or the whole repo.
skills:
  external_dirs:
    - $HOME/.agents/skills
    - $HOME/HQ/.claude/skills
# Tool shells run the hq CLI, which must start in the repo and reach the host's
# Google Workspace creds. Hermes sandboxes tool env, so without this a
# profile-scoped HOME makes ~/HQ resolve to a bogus path. Pin cwd to the repo
# and pass through the non-secret vars. NOTE: FORGEJO_TOKEN cannot be passed here —
# hermes hard-blocks env secrets passed to sandboxed tool shells (see
# GHSA-rhgp-j443-p4rf for the GH_TOKEN case this also applies to); the agent's
# `hq` CLI authenticates via the ~/.config/hq/forgejo-token file instead
# (mounted read-only by the compose file — see deploy/compose.example.yaml).
terminal:
  cwd: $HOME/HQ
  env_passthrough:
    - HOME
    - PATH
    - TZ
    - GH_CONFIG_DIR
    - GOOGLE_WORKSPACE_CLI_KEYRING_BACKEND
    - GOOGLE_WORKSPACE_CLI_CONFIG_DIR
EOF
    if [[ "$want_discord" == "discord" ]]; then
      cat <<'EOF'
# Discord adapter — bot token + access policy (GATEWAY_ALLOW_ALL_USERS or
# DISCORD_ALLOWED_USERS) live in this profile's .env, not here.
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
    fi
  } > "$HERMES_DIR/profiles/$name/config.yaml"

  # Seed the system prompt (SOUL.md) from the committed collector contract so
  # the gateway drives `hq` and respects the collector boundaries (Inbox+drafts
  # only; never send/book/close). Overwritten on every run — the authoritative
  # copy lives in git, not the host brain.
  if [[ -f "$SOUL_TEMPLATE" ]]; then
    echo "→ Installing HQ collector contract into '$name' SOUL.md..."
    cp "$SOUL_TEMPLATE" "$HERMES_DIR/profiles/$name/SOUL.md"
  else
    echo "  ⚠ SOUL template not found at $SOUL_TEMPLATE — skipping"
  fi

  echo "→ Smoke test ('$name' brain, local model)..."
  if run_hermes -p "$name" -z "Reply with exactly: ${name}-ready" 2>/dev/null | grep -q "${name}-ready"; then
    echo "  ✓ $name brain OK"
  else
    echo "  ⚠ ${name}-brain smoke test failed — check the local model '$model' in ollama"
  fi
}

ensure_profile hq-github qwen3.6:27b discord
ensure_profile protected qwen3.6:35b-a3b

cat <<EOF

GitHub HQ profiles ready (fully separated from the Discord gateway).
  Discord gateway brain (untouched) : $HERMES_DIR/memories/MEMORY.md   (DEFAULT profile — gateway only)
  GitHub HQ brain (non-protected)   : $HERMES_DIR/profiles/hq-github/memories/MEMORY.md   (local qwen3.6:27b)
  GitHub protected brain            : $HERMES_DIR/profiles/protected/memories/MEMORY.md   (local qwen, isolated)

Discord: 'hq-github' is fronted by the 'hq-agent' docker-compose service
(hermes -p hq-github gateway run). Before 'docker compose up -d hq-agent':
  1. Create a bot at https://discord.com/developers/applications, invite it
     to your server, enable MESSAGE CONTENT intent.
  2. echo "DISCORD_BOT_TOKEN=<token>" >> $HERMES_DIR/profiles/hq-github/.env
  3. Access policy — either:
       echo "GATEWAY_ALLOW_ALL_USERS=true" >> $HERMES_DIR/profiles/hq-github/.env
     or
       echo "DISCORD_ALLOWED_USERS=id1,id2" >> $HERMES_DIR/profiles/hq-github/.env
EOF
