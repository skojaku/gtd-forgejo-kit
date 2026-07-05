#!/usr/bin/env bash
# HQ container entrypoint — prepares env/state then execs the given command.
set -euo pipefail

# gws uses the file-keyring backend in the container; the encryption key
# lives in the bind-mounted ~/.config/gws/.encryption_key.
export GOOGLE_WORKSPACE_CLI_KEYRING_BACKEND=file

# Teach git to authenticate to the Forgejo host via FORGEJO_TOKEN for the
# cron `git pull`. Uses git's credential-store helper (a plain host->token
# mapping file) rather than embedding the token in an `insteadOf` URL rewrite,
# so the token never shows up in `git remote -v` or any rewritten-URL output.
# Writes to the container-local HOME (not bind-mounted). The git username is a
# generic literal ("hq") — Forgejo authenticates on the token, not the name.
if [[ -n "${FORGEJO_TOKEN:-}" ]]; then
    FORGEJO_HOST="$(python3 -c '
import sys
sys.path.insert(0, "/hq/bin")
from urllib.parse import urlparse
from hqlib.common import load_config, forgejo_url
print(urlparse(forgejo_url(load_config())).netloc)
')"
    git config --global credential.helper "store --file=${HOME}/.git-credentials"
    echo "http://hq:${FORGEJO_TOKEN}@${FORGEJO_HOST}" > "${HOME}/.git-credentials"
    chmod 600 "${HOME}/.git-credentials"

    # The committed `origin` uses the host's external Forgejo URL (e.g. a tailnet
    # name), which is not routable from the compose bridge. Inside the container,
    # transparently rewrite that URL to the in-network service DNS (FORGEJO_URL,
    # e.g. http://forgejo:3000) so the cron `git pull origin main` works. This is
    # container-local (global git config in the container HOME); the host repo,
    # which can reach the external URL, is untouched.
    CONFIG_FORGEJO_URL="$(python3 -c '
import sys
sys.path.insert(0, "/hq/bin")
from hqlib.common import load_config
print((load_config().get("forgejo_url") or "").rstrip("/"))
' 2>/dev/null || true)"
    if [[ -n "$CONFIG_FORGEJO_URL" && "$CONFIG_FORGEJO_URL" != "${FORGEJO_URL%/}" ]]; then
        git config --global "url.${FORGEJO_URL%/}/.insteadOf" "${CONFIG_FORGEJO_URL}/"
    fi
fi

# Ensure state/log locations exist (no-op if already bind-mounted).
mkdir -p "${HOME}/.email-triage"
: >> /tmp/hq-agent.log
: >> /tmp/hq-pull.log
: >> /tmp/task-emails.log

cd /hq

# Debug hook: `HQ_DRY_RUN=1 docker compose up hq-cron` prints the crontab and exits.
if [[ "${HQ_DRY_RUN:-0}" == "1" ]]; then
    echo "[hq-entrypoint] HQ_DRY_RUN=1 — crontab contents:"
    cat /hq/docker/crontab
    exit 0
fi

# The Hermes Discord gateway is NOT launched here — it runs as its own opt-in
# compose service (hq-discord, profile "discord") from the same image, so it
# can be enabled/disabled independently and a gateway crash can't disrupt the
# dispatch cron. Container behavior is set by each service's command/crontab,
# not by this entrypoint.

exec "$@"
