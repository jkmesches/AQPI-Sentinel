#!/usr/bin/env bash
# Sentinel database backup — pg_dump, verified, pruned, and observable.
#
#   bash ops/backup.sh              # normal run
#   bash ops/backup.sh --check      # validate config and exit, changing nothing
#
# Install via cron on the DOCKER HOST (not inside a container):
#   0 8 * * *  /srv/sentinel/ops/backup.sh >> /var/log/sentinel-backup.log 2>&1
#
# Runs an hour before the in-process retention sweep
# (SENTINEL_RETENTION_HOUR_UTC, default 9) so a backup always precedes the
# offload that deletes rows. If a sweep ever goes wrong, the night's dump
# predates it.
#
# ── Configuration ────────────────────────────────────────────────────────────
# Every knob is an environment variable with a default. Set them in the cron
# line, or export them from a file the cron line sources.
#
#   SENTINEL_BACKUP_DIR           where dumps are written
#                                 default: /var/backups/sentinel
#   SENTINEL_BACKUP_KEEP          how many dumps to retain (>= 1)
#                                 default: 14
#   SENTINEL_PG_CONTAINER         Postgres container name
#                                 default: sentinel-postgres
#   POSTGRES_USER / POSTGRES_DB   database identity
#                                 default: sentinel / sentinel
#   SENTINEL_BACKUP_REQUIRE_MOUNT 1 = refuse to run unless the dumps land on a
#                                 mounted volume rather than the root disk.
#                                 default: 1 when SENTINEL_BACKUP_DIR is set
#                                 (you pointed us at storage — we verify it is
#                                 attached), 0 when it is not (the local
#                                 default IS a choice to use local disk).
#   SENTINEL_BACKUP_MOUNT_ROOT    the mountpoint to require
#                                 default: the filesystem SENTINEL_BACKUP_DIR
#                                 lands on. NOT the directory itself — a share
#                                 is mounted at a root and dumps live in a
#                                 subdirectory, so the dump directory is almost
#                                 never a mountpoint of its own.
#   SENTINEL_BACKUP_MIN_FREE_MB   abort if the destination has less free space
#                                 than this. default: 2048
#   SENTINEL_BACKUP_MIN_BYTES     crude floor for a zero-length/header-only
#                                 file. Validity is decided by a schema
#                                 CONTENT check, not by size — a fresh install
#                                 dumps to ~6 kB and is perfectly valid.
#                                 default: 512
#   SENTINEL_BACKUP_STATUS_FILE   machine-readable outcome of the last run
#                                 default: $SENTINEL_BACKUP_DIR/status.json
#
# ── Why the status file matters ──────────────────────────────────────────────
# A cron backup that fails writes to a log nobody reads. This script records
# the outcome of EVERY run — success or failure — to status.json, and
# Sentinel's `layer0.self.backup` check reads it and alarms when backups go
# stale or start failing. Without that, the first sign of a broken backup is
# needing one. See docs/MAINTENANCE.md.

set -euo pipefail

DEST="${SENTINEL_BACKUP_DIR:-/var/backups/sentinel}"
KEEP="${SENTINEL_BACKUP_KEEP:-14}"
CONTAINER="${SENTINEL_PG_CONTAINER:-sentinel-postgres}"
PGUSER="${POSTGRES_USER:-sentinel}"
PGDB="${POSTGRES_DB:-sentinel}"
# Require a real mount only when the operator pointed us somewhere. Taking
# the built-in local default IS the choice to keep dumps on local disk, so
# demanding a mountpoint there makes a fresh install abort every night with
# nothing configured wrong. Set the variable explicitly to override either way.
if [ -n "${SENTINEL_BACKUP_DIR:-}" ]; then
    REQUIRE_MOUNT="${SENTINEL_BACKUP_REQUIRE_MOUNT:-1}"
else
    REQUIRE_MOUNT="${SENTINEL_BACKUP_REQUIRE_MOUNT:-0}"
fi

# The mountpoint to verify. Defaulting this to $DEST was wrong: shares are
# mounted at a root and dumps live in a subdirectory beneath it, so the
# directory you name is almost never itself a mountpoint. Walk up to the
# filesystem $DEST actually lands on. $DEST may not exist yet — the guard runs
# before mkdir — so start from its deepest existing ancestor.
enclosing_mountpoint() {
    local p="$1"
    while [ ! -e "$p" ] && [ "$p" != "/" ]; do p="$(dirname "$p")"; done
    df -P "$p" 2>/dev/null | awk 'NR==2{print $6}'
}
MOUNT_ROOT="${SENTINEL_BACKUP_MOUNT_ROOT:-$(enclosing_mountpoint "$DEST")}"
MIN_FREE_MB="${SENTINEL_BACKUP_MIN_FREE_MB:-2048}"
MIN_BYTES="${SENTINEL_BACKUP_MIN_BYTES:-512}"
STATUS_FILE="${SENTINEL_BACKUP_STATUS_FILE:-$DEST/status.json}"

# === Load-bearing: why this is not a plain `gzip -dc | grep -q` ===
#
# `grep -q` exits at the first match, which closes the pipe and kills gzip
# with SIGPIPE (141). Under `set -o pipefail` the pipeline then reports 141 —
# a FAILURE — even though the pattern was found. It only happens when the file
# is big enough that gzip is still writing when grep quits, so it passes on a
# small test database and fails on a real one. Measured: a 4,000-row dump
# returns 141, a toy dump returns 0.
#
# That would have failed every nightly backup on the production database while
# looking correct in every test.
dump_has_schema() {
    local rc
    set +o pipefail
    gzip -dc "$1" | grep -qE "CREATE TABLE (IF NOT EXISTS )?(public\.)?check_runs"
    rc=$?
    set -o pipefail
    return $rc
}

log()  { echo "[$(date -uIs)] $*"; }
err()  { echo "[$(date -uIs)] $*" >&2; }

# Record the outcome of this run no matter how it ends — including an
# unexpected abort under `set -e`, which is precisely the case a hand-placed
# "write success" line at the bottom would miss.
STATUS="failed"; DETAIL="aborted before completion"; BYTES=0; ARTIFACT=""
# status.json records BACKUP ATTEMPTS, and nothing else. `--check` sets this to
# 0 so a config check cannot masquerade as a dump.
#
# It did, until 2026-09-05. --check exits through the same EXIT trap, so it
# wrote status=ok with a fresh finished_at and an empty artifact — and
# layer0.self.backup, which keys on status + age, reported "last backup 0.0h
# ago". Running the documented config-validation command silently reset the
# staleness clock, so an operator checking their config while real backups
# were failing would be told everything was fine indefinitely.
# Decided here, before the EXIT trap is armed, so it also holds when a
# --check ABORTS on bad config: that is still not a backup attempt, and
# recording it as a failed dump would misreport what happened.
if [ "${1:-}" = "--check" ]; then WRITE_STATUS=0; else WRITE_STATUS=1; fi
write_status() {
    local rc=$?
    [ "$WRITE_STATUS" = "1" ] || return 0
    [ "$rc" -eq 0 ] && [ "$STATUS" = "failed" ] && { STATUS="ok"; DETAIL="completed"; }
    mkdir -p "$(dirname "$STATUS_FILE")" 2>/dev/null || true
    cat > "$STATUS_FILE.part" 2>/dev/null <<JSON || return 0
{
  "status": "$STATUS",
  "detail": "$DETAIL",
  "finished_at": "$(date -uIs)",
  "exit_code": $rc,
  "artifact": "$ARTIFACT",
  "bytes": $BYTES,
  "keep": $KEEP,
  "dest": "$DEST",
  "database": "$PGDB"
}
JSON
    mv -f "$STATUS_FILE.part" "$STATUS_FILE" 2>/dev/null || true
}
trap write_status EXIT

die() { STATUS="failed"; DETAIL="$1"; err "ABORT: $1"; shift; for m in "$@"; do err "  $m"; done; exit 1; }

# ── Validate configuration before touching anything ──────────────────────────

case "$KEEP" in
    ''|*[!0-9]*) die "SENTINEL_BACKUP_KEEP must be a whole number, got '$KEEP'" ;;
esac
# KEEP=0 with the old pruning logic deleted every backup including the one
# just written. Refuse rather than interpret it.
[ "$KEEP" -lt 1 ] && die "SENTINEL_BACKUP_KEEP must be at least 1 (got $KEEP)" \
    "A value of 0 would delete every backup, including the one just taken."

command -v docker >/dev/null 2>&1 || die "docker not found on PATH" \
    "This script runs on the docker HOST, not inside a container."

docker inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null | grep -q true \
    || die "container '$CONTAINER' is not running" \
        "Set SENTINEL_PG_CONTAINER if your container has another name."

# "/" counts as a mountpoint, so testing that alone would let a detached
# share pass silently — the whole failure this guards against. Landing on the
# root filesystem is exactly the thing we refuse.
if [ "$REQUIRE_MOUNT" = "1" ]; then
    if [ "$MOUNT_ROOT" = "/" ]; then
        why="$DEST is on the root filesystem, not a mounted volume"
    elif ! mountpoint -q "$MOUNT_ROOT" 2>/dev/null; then
        why="$MOUNT_ROOT is not a mountpoint"
    else
        why=""
    fi
    [ -z "$why" ] || die "$why — refusing to write backups there" \
        "If the share failed to attach, the path is still a writable local" \
        "directory and nightly dumps would quietly fill the disk this exists" \
        "to protect. Mount it, or set SENTINEL_BACKUP_REQUIRE_MOUNT=0 to" \
        "deliberately keep backups on local disk."
fi

mkdir -p "$DEST" || die "cannot create $DEST"
[ -w "$DEST" ] || die "$DEST is not writable by $(id -un)"

free_mb=$(df -Pm "$DEST" 2>/dev/null | awk 'NR==2{print $4}')
if [ -n "$free_mb" ] && [ "$free_mb" -lt "$MIN_FREE_MB" ]; then
    die "only ${free_mb} MB free at $DEST (need ${MIN_FREE_MB} MB)" \
        "Prune old dumps, lower SENTINEL_BACKUP_KEEP, or point" \
        "SENTINEL_BACKUP_DIR at a larger disk."
fi

if [ "${1:-}" = "--check" ]; then
    log "config OK: dest=$DEST keep=$KEEP container=$CONTAINER db=$PGDB free=${free_mb}MB"
    log "--check specified; not taking a backup."
    exit 0
fi

# ── Dump ─────────────────────────────────────────────────────────────────────

stamp="$(date -u +%Y%m%dT%H%M%SZ)"
out="$DEST/sentinel-${stamp}.sql.gz"
ARTIFACT="$out"

log "dumping $PGDB from $CONTAINER -> $out"
# pipefail makes a pg_dump failure fail the pipeline even though gzip succeeds.
if ! docker exec "$CONTAINER" pg_dump -U "$PGUSER" -d "$PGDB" 2>/tmp/sentinel-pgdump.err \
        | gzip -6 > "$out.part"; then
    rm -f "$out.part"
    die "pg_dump failed" "$(tail -3 /tmp/sentinel-pgdump.err 2>/dev/null || true)"
fi

# A valid gzip stream is the minimum bar — catches truncation from a full
# destination or a killed container.
gzip -t "$out.part" 2>/dev/null || { rm -f "$out.part"; die "dump failed gzip integrity check"; }

BYTES=$(stat -c%s "$out.part" 2>/dev/null || stat -f%z "$out.part" 2>/dev/null || echo 0)

# gzip -t proves the file decompresses, not that it is a usable dump. This is
# the real validity test: a dump that restores to an empty database is worse
# than no backup, because it looks like one.
#
# Deliberately a CONTENT check rather than a size threshold. The first version
# of this script rejected anything under 10 kB as "too small to be real" — a
# fresh install with 500 rows dumps to 6.2 kB, so a new deployment's very
# first backup failed. Size tells you nothing about a small-but-correct
# database; the schema being present tells you everything.
if ! dump_has_schema "$out.part"; then
    rm -f "$out.part"
    die "dump does not contain the check_runs schema" \
        "It decompressed cleanly but is not a usable Sentinel backup." \
        "Usually means pg_dump connected to the wrong database — check" \
        "POSTGRES_DB and POSTGRES_USER."
fi

# Crude floor, kept only to catch a zero-length or header-only file that
# somehow satisfied the grep. Not a judgment about database size.
[ "$BYTES" -lt "$MIN_BYTES" ] && { rm -f "$out.part"; die "dump is only ${BYTES} bytes"; }

mv "$out.part" "$out"
sync
log "wrote $(du -h "$out" | cut -f1) ($BYTES bytes)"

# ── Prune ────────────────────────────────────────────────────────────────────
# Only runs after a verified dump exists, so a bad night can never leave you
# with fewer good backups than you started with.

pruned=0
while IFS= read -r old; do
    [ -n "$old" ] || continue
    log "pruning $(basename "$old")"
    rm -f "$old" && pruned=$((pruned+1))
done < <(ls -1t "$DEST"/sentinel-*.sql.gz 2>/dev/null | tail -n +$((KEEP + 1)))

remaining=$(ls -1 "$DEST"/sentinel-*.sql.gz 2>/dev/null | wc -l | tr -d ' ')
STATUS="ok"; DETAIL="wrote $(basename "$out"); pruned $pruned; $remaining retained"
log "done — $remaining backups retained, $pruned pruned"
