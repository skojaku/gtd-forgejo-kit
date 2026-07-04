#!/usr/bin/env bash
# HQ container entrypoint — prepares env/state then execs the given command.
set -euo pipefail

# gws uses the file-keyring backend in the container; the encryption key
# lives in the bind-mounted ~/.config/gws/.encryption_key.
export GOOGLE_WORKSPACE_CLI_KEYRING_BACKEND=file

# Teach git to authenticate to the Forgejo host via FORGEJO_TOKEN for the
# cron `git pull`. Uses git's credential-store helper (a plain
# host->token mapping file) rather than embedding the token in an
# `insteadOf` URL rewrite, so the token never shows up in `git remote -v`
# or any rewritten-URL output. Writes to the container-local
# /home/youruser/.gitconfig and /home/youruser/.git-credentials (HOME is not
# bind-mounted).
if [[ -n "${FORGEJO_TOKEN:-}" ]]; then
    FORGEJO_HOST="$(python3 -c '
import sys
sys.path.insert(0, "/home/youruser/HQ/bin")
from urllib.parse import urlparse
from hqlib.common import load_config, forgejo_url
print(urlparse(forgejo_url(load_config())).netloc)
')"
    git config --global credential.helper "store --file=/home/youruser/.git-credentials"
    echo "http://youruser:${FORGEJO_TOKEN}@${FORGEJO_HOST}" > /home/youruser/.git-credentials
    chmod 600 /home/youruser/.git-credentials
fi

# Keep the unified `hq` CLI resolvable, and drop stale symlinks from the
# pre-`hq` layout — the old gtd/gmail wrappers were removed from the repo, so
# their dangling symlinks (baked into older images) would otherwise mislead
# the hermes agent. Self-heals on every start.
ln -sf /home/youruser/HQ/bin/hq /home/youruser/.npm-global/bin/hq 2>/dev/null || true
rm -f /home/youruser/.npm-global/bin/gtd /home/youruser/.npm-global/bin/gmail 2>/dev/null || true

# Ensure state/log locations exist (no-op if already bind-mounted).
mkdir -p /home/youruser/.email-triage
: >> /tmp/hq-agent.log
: >> /tmp/hq-pull.log
: >> /tmp/task-emails.log

cd /home/youruser/HQ

# Debug hook: `HQ_DRY_RUN=1 docker compose up hq` prints the crontab and exits.
if [[ "${HQ_DRY_RUN:-0}" == "1" ]]; then
    echo "[hq-entrypoint] HQ_DRY_RUN=1 — crontab contents:"
    cat /home/youruser/HQ/docker/crontab
    exit 0
fi

# Merged container: run the Hermes Discord gateway in the background (with
# an auto-restart loop, since it's not supervised by anything else) and the
# supercronic cron daemon in the foreground as PID 1's exec target.
( while true; do
    hermes -p hq-github gateway run 2>&1 | sed 's/^/[hermes] /'
    echo "[hermes] exited ($?), restarting in 5s"; sleep 5
  done ) &

exec "$@"
