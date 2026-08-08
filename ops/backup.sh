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
# The mount guard below is the difference between "no backups" and "backups
# silently filling the disk we are trying to protect". Set to 0 only for a
# deployment that genuinely keeps backups on local disk.
REQUIRE_MOUNT="${SENTINEL_BACKUP_REQUIRE_MOUNT:-1}"
MOUNT_ROOT="${SENTINEL_BACKUP_MOUNT_ROOT:-/mnt/aqpi-data}"

stamp="$(date -u +%Y%m%dT%H%M%SZ)"
out="$DEST/sentinel-${stamp}.sql.gz"

# Refuse to run if the share isn't mounted. In production /mnt/aqpi-data is a
# Proxmox bind mount from the host, so it is normally always present — but if
# it ever fails to attach, the path is still a perfectly writable local
# directory, and a nightly 177MB dump would quietly land on the disk this
# whole exercise exists to protect. Aborting is the safe failure: a missing
# backup is visible in the log, a disk slowly filling with misplaced backups
# is not.
if [ "$REQUIRE_MOUNT" = "1" ] && ! mountpoint -q "$MOUNT_ROOT"; then
    echo "[$(date -uIs)] ABORT: $MOUNT_ROOT is not a mountpoint — refusing to write backups to local disk." >&2
    echo "  Mount the share, or set SENTINEL_BACKUP_REQUIRE_MOUNT=0 to allow local backups." >&2
    exit 1
fi

mkdir -p "$DEST"

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
