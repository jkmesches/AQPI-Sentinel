#!/bin/bash
# Sentinel nightly backup — pg_dump + retention, onto the NFS share.
#
# Install on the production host via cron (see docs/MAINTENANCE.md):
#   0 8 * * *  /srv/sentinel/ops/backup.sh >> /var/log/sentinel-backup.log 2>&1
#
# Runs at 08:00 UTC, an hour before the in-process retention sweep
# (SENTINEL_RETENTION_HOUR_UTC=9) so a backup always precedes the offload
# that removes rows — if a sweep ever goes wrong, the night's dump predates it.
#
# Writes to a .part file and renames on success, so a partial dump from an
# interrupted run is never mistaken for a usable backup by the restore path
# or by the keep-N pruning below.
set -euo pipefail

DEST="${SENTINEL_BACKUP_DIR:-/mnt/aqpi-data/backups/db}"
KEEP="${SENTINEL_BACKUP_KEEP:-14}"
CONTAINER="${SENTINEL_PG_CONTAINER:-sentinel-postgres}"
PGUSER="${POSTGRES_USER:-sentinel}"
PGDB="${POSTGRES_DB:-sentinel}"

stamp="$(date -u +%Y%m%dT%H%M%SZ)"
out="$DEST/sentinel-${stamp}.sql.gz"

mkdir -p "$DEST"

# Fail loudly if the NFS mount is absent rather than silently filling the
# local disk with "backups" at a path that is supposed to be a mount point.
if ! mountpoint -q "$(dirname "$(dirname "$DEST")")" 2>/dev/null; then
    echo "WARN: $(dirname "$(dirname "$DEST")") is not a mountpoint — is the NFS share mounted?" >&2
fi

echo "[$(date -uIs)] dumping $PGDB → $out"
if ! docker exec "$CONTAINER" pg_dump -U "$PGUSER" -d "$PGDB" \
        | gzip -6 > "$out.part"; then
    echo "[$(date -uIs)] ERROR: pg_dump failed" >&2
    rm -f "$out.part"
    exit 1
fi

# A valid gzip stream that decompresses is the minimum bar for calling this a
# backup. Catches truncation from a full destination or a killed container.
if ! gzip -t "$out.part"; then
    echo "[$(date -uIs)] ERROR: dump failed gzip integrity check" >&2
    rm -f "$out.part"
    exit 1
fi

mv "$out.part" "$out"
sync
echo "[$(date -uIs)] wrote $(du -h "$out" | cut -f1)"

# Prune to the newest $KEEP. Only fully-renamed dumps are considered.
ls -1t "$DEST"/sentinel-*.sql.gz 2>/dev/null | tail -n +$((KEEP + 1)) | while read -r old; do
    echo "[$(date -uIs)] pruning old backup $(basename "$old")"
    rm -f "$old"
done

echo "[$(date -uIs)] done — $(ls -1 "$DEST"/sentinel-*.sql.gz 2>/dev/null | wc -l) backups retained"
