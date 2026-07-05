#!/usr/bin/env bash
# Nightly Forgejo dump backup.
#
# RUN THIS ON THE HOST (a host crontab entry), not inside the hq-cron container:
# it calls `docker exec` on the forgejo container, which needs the docker
# socket that the containers deliberately do not mount. Example host crontab:
#   30 3 * * * /path/to/HQ/deploy/backup-forgejo.sh
#
# Env overrides:
#   BACKUP_DEST   where archives are written   [/data/backups/forgejo]
#   FORGEJO_CTR   forgejo container name        [forgejo]
#                 (compose names it <project>-forgejo-1; `docker ps` to confirm)
#   KEEP_DAYS     prune archives older than     [14]
set -euo pipefail

DEST="${BACKUP_DEST:-/data/backups/forgejo}"
CTR="${FORGEJO_CTR:-forgejo}"
KEEP_DAYS="${KEEP_DAYS:-14}"
STAMP=$(date +%Y%m%d-%H%M%S)
ARCHIVE="forgejo-dump-${STAMP}.zip"

mkdir -p "$DEST"

docker exec -u 1000 "$CTR" forgejo dump -c /data/gitea/conf/app.ini -t /tmp -f "/tmp/${ARCHIVE}"
docker cp "${CTR}:/tmp/${ARCHIVE}" "$DEST/${ARCHIVE}"
docker exec -u 1000 "$CTR" rm -f "/tmp/${ARCHIVE}"

find "$DEST" -name 'forgejo-dump-*.zip' -mtime +"${KEEP_DAYS}" -delete

echo "backed up to $DEST/${ARCHIVE}"
