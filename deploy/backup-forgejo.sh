#!/usr/bin/env bash
# Nightly forgejo dump backup (PLAN.md Phase 4 step 16).
# Runs `forgejo dump` inside the container, copies the archive to a second
# disk, and prunes anything older than 14 days.
set -euo pipefail

DEST=/data/backups/forgejo
STAMP=$(date +%Y%m%d-%H%M%S)
ARCHIVE="forgejo-dump-${STAMP}.zip"

mkdir -p "$DEST"

docker exec -u 1000 forgejo forgejo dump -c /data/gitea/conf/app.ini -t /tmp -f "/tmp/${ARCHIVE}"
docker cp "forgejo:/tmp/${ARCHIVE}" "$DEST/${ARCHIVE}"
docker exec -u 1000 forgejo rm -f "/tmp/${ARCHIVE}"

find "$DEST" -name 'forgejo-dump-*.zip' -mtime +14 -delete

echo "backed up to $DEST/${ARCHIVE}"
