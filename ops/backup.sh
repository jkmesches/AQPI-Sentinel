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
#   SENTINEL_BACKUP_COPY_DIR      a second folder for the dumps, normally on a
#                                 share (e.g., /mnt/share/sentinel/backups).
#                                 Each run copies every local dump the
#                                 folder lacks and checks the copy's SHA-256. A
#                                 local dump past SENTINEL_BACKUP_KEEP is pruned
#                                 only once the folder holds a checked copy.
#                                 The dump itself is still written locally first:
#                                 written straight to a stalled share, pg_dump
#                                 would hold its snapshot open for the stall.
#                                 default: unset, no copy
#   SENTINEL_BACKUP_COPY_KEEP     how many dumps the copy folder keeps (>= 1)
#                                 default: 14
#   SENTINEL_BACKUP_COPY_TIMEOUT_S  limit in seconds on each copy or checksum in
#                                 the copy folder. A stalled `hard` NFS mount
#                                 never returns an error, so without a limit the
#                                 run would wait forever. default: 600
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
COPY_DIR="${SENTINEL_BACKUP_COPY_DIR:-}"
COPY_KEEP="${SENTINEL_BACKUP_COPY_KEEP:-14}"
COPY_TIMEOUT_S="${SENTINEL_BACKUP_COPY_TIMEOUT_S:-600}"
# Listing, mkdir, rm and the mount test take well under a second on a healthy
# share, so a short limit keeps a stalled one from holding the run.
readonly META_TIMEOUT_S=30
# The names this script gives its dumps, which sort by time. The copy step
# copies, prunes and counts only these, so a hand-made file in either folder
# (e.g., sentinel-pre-migration.sql.gz) is never pruned on the share and never
# taken for the newest dump.
d4='[0-9][0-9][0-9][0-9]'
readonly DUMP_GLOB="sentinel-${d4}${d4}T${d4}[0-9][0-9]Z.sql.gz"
# The newest dump with a checked copy, and the COPY_DIR it was checked in, one
# per line. Kept beside the dumps rather than read back from the share, so
# status.json can report it while the share gives no answer.
VERIFIED_MARK="$DEST/.copy-verified"
# Dump names this run found or made a checked copy of in COPY_DIR.
declare -A VERIFIED=()

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

# dump_time NAME: the UTC time in a dump's name (sentinel-20261125T150002Z.sql.gz)
# as ISO 8601, or nothing for a name of another shape.
dump_time() {
    local name=$1
    local re='^sentinel-([0-9]{4})([0-9]{2})([0-9]{2})T([0-9]{2})([0-9]{2})([0-9]{2})Z\.sql\.gz$'
    [[ $name =~ $re ]] || return 0
    printf '%s-%s-%sT%s:%s:%s+00:00\n' "${BASH_REMATCH[@]:1:6}"
}

# Every call that touches COPY_DIR runs under a limit, because a stalled `hard`
# NFS mount blocks a call instead of failing it.
nfs_meta() { timeout -k 5 "$META_TIMEOUT_S" "$@"; }
nfs_copy() { timeout -k 10 "$COPY_TIMEOUT_S" "$@"; }

# json_str TEXT: TEXT escaped for the inside of a JSON string. A quote or a
# backslash in a path or a reason would otherwise make status.json unreadable,
# and layer0.self.backup reads an unreadable file as "not configured".
json_str() {
    local s=$1
    s=${s//\\/\\\\}
    s=${s//\"/\\\"}
    s=${s//$'\n'/\\n}
    s=${s//$'\t'/\\t}
    s=${s//[[:cntrl:]]/ }
    printf '%s' "$s"
}

# copy_dir_ready: succeed when COPY_DIR lands on a mounted filesystem that is
# neither the root one nor DEST's, and set COPY_REASON when it does not. A share
# that failed to attach leaves its mount point as an empty folder on the root
# disk, and a copy there would fill the disk the copy exists to spare. A copy on
# DEST's own filesystem (the same folder, one inside it, a bind mount of it)
# checks against itself, and the prune would then delete the only copy. A share
# whose mount point sits on a separate local partition, and fails to attach, is
# not caught here.
copy_dir_ready() {
    local p=$COPY_DIR
    local rc root copy_dev dest_dev
    while [[ $p != / ]]; do
        rc=0
        nfs_meta test -e "$p" || rc=$?
        if ((rc == 0)); then break; fi
        if ((rc >= 124)); then
            COPY_REASON="$p gave no answer in ${META_TIMEOUT_S}s"
            return 1
        fi
        p=$(dirname "$p")
    done
    if ! root=$(nfs_meta df --output=target "$p" | tail -n 1); then
        COPY_REASON="df on $p gave no answer"
        return 1
    fi
    if [[ -z $root || $root == / ]]; then
        COPY_REASON="$COPY_DIR is on the root filesystem, not a mounted share"
        return 1
    fi
    if ! nfs_meta mountpoint -q "$root"; then
        COPY_REASON="$root is not a mountpoint"
        return 1
    fi
    copy_dev=$(nfs_meta stat -c %d -- "$p") || { COPY_REASON="stat on $p failed"; return 1; }
    dest_dev=$(stat -c %d -- "$DEST") || { COPY_REASON="stat on $DEST failed"; return 1; }
    if [[ $copy_dev == "$dest_dev" ]]; then
        COPY_REASON="$COPY_DIR is on the same filesystem as $DEST, so it is no second copy"
        return 1
    fi
}

# marked_newest: the newest dump name recorded in VERIFIED_MARK for this
# COPY_DIR, or nothing. A mark left by another COPY_DIR does not count.
marked_newest() {
    local name="" dir=""
    if [[ -f $VERIFIED_MARK ]]; then
        { IFS= read -r name; IFS= read -r dir; } < "$VERIFIED_MARK" || true
    fi
    if [[ $dir == "$COPY_DIR" ]]; then printf '%s' "$name"; fi
}

# mark_verified NAME: count NAME as checked in COPY_DIR, and record it as the
# newest such dump unless a newer one is recorded. Dump names sort by time.
mark_verified() {
    local name=$1
    local current
    VERIFIED[$name]=1
    current=$(marked_newest)
    if [[ $name > $current ]]; then
        printf '%s\n%s\n' "$name" "$COPY_DIR" > "$VERIFIED_MARK.part" \
            && mv -f -- "$VERIFIED_MARK.part" "$VERIFIED_MARK"
    fi
}

# copy_one DUMP: make COPY_DIR hold DUMP with the same SHA-256, copying it when
# the copy is missing or differs. A local dump that fails `gzip -t` is never
# copied over the share's copy, since the share's may be the good one.
copy_one() {
    local src=$1
    local name=${1##*/}
    local dst="$COPY_DIR/${1##*/}"
    local want have
    # Hashed from a pipe: sha256sum prefixes its line with a backslash when the
    # file's name holds one, and the read from the share stays under its limit.
    want=$(sha256sum < "$src" | cut -d' ' -f1) || return 1
    have=$(nfs_copy cat -- "$dst" 2>/dev/null | sha256sum | cut -d' ' -f1) || have=""
    if [[ -n $have && $have == "$want" ]]; then
        mark_verified "$name"
        return 0
    fi
    if ! gzip -t -- "$src" 2>/dev/null; then
        COPY_REASON="the local $name fails gzip -t, so it was not copied"
        return 1
    fi
    # umask 077 makes the .part 600 from its creation, as the finished copy is.
    if ! (umask 077 && nfs_copy cp --preserve=timestamps -- "$src" "$dst.part"); then
        nfs_meta rm -f -- "$dst.part" || true
        COPY_REASON="copying $name gave no answer or failed"
        return 1
    fi
    have=$(nfs_copy cat -- "$dst.part" | sha256sum | cut -d' ' -f1) || have=""
    if [[ $have != "$want" ]]; then
        nfs_meta rm -f -- "$dst.part" || true
        COPY_REASON="the copy of $name did not match its SHA-256"
        return 1
    fi
    if ! nfs_meta chmod 600 -- "$dst.part" || ! nfs_meta mv -f -- "$dst.part" "$dst"; then
        COPY_REASON="could not put $name in place"
        return 1
    fi
    mark_verified "$name"
    log "copied $name to $COPY_DIR, SHA-256 verified"
}

# prune_copies KEEP_NAME: remove the oldest dumps in COPY_DIR past COPY_KEEP,
# never KEEP_NAME (this run's dump, which is the oldest name when the clock has
# stepped back). A dump removed here is past both limits, since COPY_KEEP is at
# least KEEP, so the local prune may remove it too. Also removes a .part a
# killed run left more than a day ago.
prune_copies() {
    local keep_name=$1
    local listing old name excess
    local copies=()
    listing=$(nfs_meta find "$COPY_DIR" -maxdepth 1 -type f -name "$DUMP_GLOB" | sort) \
        || return 1
    if [[ -n $listing ]]; then mapfile -t copies <<<"$listing"; fi
    excess=$((${#copies[@]} - COPY_KEEP))
    for old in "${copies[@]}"; do
        ((excess > 0)) || break
        name=${old##*/}
        if [[ $name == "$keep_name" ]]; then continue; fi
        log "pruning copy $name"
        nfs_meta rm -f -- "$old" || return 1
        excess=$((excess - 1))
    done
    nfs_meta find "$COPY_DIR" -maxdepth 1 -type f -name "$DUMP_GLOB.part" -mmin +1440 -delete
}

# copy_dumps KEEP_NAME: copy every local dump COPY_DIR lacks, newest first, then
# prune COPY_DIR, never KEEP_NAME. Sets COPY_STATUS and COPY_DETAIL and never
# exits, because a failed copy must not turn a good dump into a failed run.
copy_dumps() {
    local keep_name=$1
    local dumps=()
    local dump
    COPY_STATUS="failed"
    if ! copy_dir_ready; then COPY_DETAIL=$COPY_REASON; return 1; fi
    # Created only when missing: `install -d -m` on an existing folder would
    # change its mode, and fails outright on a share folder owned by another user.
    if ! nfs_meta test -d "$COPY_DIR" && ! nfs_meta install -d -m 700 -- "$COPY_DIR"; then
        COPY_DETAIL="cannot create $COPY_DIR"
        return 1
    fi
    mapfile -t dumps < <(find "$DEST" -maxdepth 1 -type f -name "$DUMP_GLOB" | sort -r)
    for dump in "${dumps[@]}"; do
        # The first failure ends the run's copying: on a stalled share each
        # further dump would wait out its own limit.
        if ! copy_one "$dump"; then COPY_DETAIL=$COPY_REASON; return 1; fi
    done
    if ! prune_copies "$keep_name"; then
        COPY_DETAIL="copied ${#dumps[@]} dumps, but pruning $COPY_DIR failed"
        return 1
    fi
    COPY_STATUS="ok"
    COPY_DETAIL="${#dumps[@]} dumps verified in $COPY_DIR"
}

# Record the outcome of this run no matter how it ends — including an
# unexpected abort under `set -e`, which is precisely the case a hand-placed
# "write success" line at the bottom would miss.
STATUS="failed"; DETAIL="aborted before completion"; BYTES=0; ARTIFACT=""
# The copy's own outcome, apart from the dump's: a good dump stays good when the
# share is down, and layer0.self.backup grades the copy separately.
if [[ -n $COPY_DIR ]]; then COPY_STATUS="not_run"; else COPY_STATUS="off"; fi
COPY_DETAIL=""; COPY_REASON=""
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
    local newest="" newest_at="" keep_json=null
    if [[ -n $COPY_DIR ]]; then
        newest=$(marked_newest)
        newest_at=$(dump_time "$newest")
    fi
    # A KEEP that failed validation is recorded as null, which keeps the JSON valid.
    if [[ $KEEP =~ ^[0-9]+$ ]]; then keep_json=$KEEP; fi
    mkdir -p "$(dirname "$STATUS_FILE")" 2>/dev/null || true
    cat > "$STATUS_FILE.part" 2>/dev/null <<JSON || return 0
{
  "status": "$(json_str "$STATUS")",
  "detail": "$(json_str "$DETAIL")",
  "finished_at": "$(date -uIs)",
  "exit_code": $rc,
  "artifact": "$(json_str "$ARTIFACT")",
  "bytes": $BYTES,
  "keep": $keep_json,
  "dest": "$(json_str "$DEST")",
  "database": "$(json_str "$PGDB")",
  "copy_dir": "$(json_str "$COPY_DIR")",
  "copy_status": "$(json_str "$COPY_STATUS")",
  "copy_detail": "$(json_str "$COPY_DETAIL")",
  "copy_newest": "$(json_str "$newest")",
  "copy_newest_at": "$newest_at"
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
# The copy settings mean nothing without a copy folder, so a stray value cannot
# stop a deployment that never set one.
if [[ -n $COPY_DIR ]]; then
    if [[ ! $COPY_KEEP =~ ^[1-9][0-9]*$ ]]; then
        die "SENTINEL_BACKUP_COPY_KEEP must be a whole number of at least 1, got '$COPY_KEEP'"
    fi
    # Fewer on the share than on the host would prune copies of dumps the host
    # still keeps, then copy them back the next night.
    if ((COPY_KEEP < KEEP)); then
        die "SENTINEL_BACKUP_COPY_KEEP ($COPY_KEEP) must be at least SENTINEL_BACKUP_KEEP ($KEEP)"
    fi
    if [[ ! $COPY_TIMEOUT_S =~ ^[1-9][0-9]*$ ]]; then
        die "SENTINEL_BACKUP_COPY_TIMEOUT_S must be whole seconds, got '$COPY_TIMEOUT_S'"
    fi
fi

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
    # A real run copies after the dump and records a copy failure without
    # failing the dump. --check takes no dump, so it can refuse outright.
    if [[ -n $COPY_DIR ]] && ! copy_dir_ready; then
        die "copy folder not ready: $COPY_REASON"
    fi
    log "config OK: dest=$DEST keep=$KEEP container=$CONTAINER db=$PGDB free=${free_mb}MB"
    if [[ -n $COPY_DIR ]]; then log "copy OK: dir=$COPY_DIR keep=$COPY_KEEP"; fi
    log "--check specified; not taking a backup."
    exit 0
fi

# One run at a time: two would share the .part files, the mark and status.json.
# A run that finds the lock held leaves status.json alone and exits, so a hung
# run shows as a stale backup in layer0.self.backup rather than as an ok one.
command -v flock >/dev/null 2>&1 || die "flock not found on PATH (util-linux)"
exec 9>"$DEST/.backup.lock"
if ! flock -n 9; then
    WRITE_STATUS=0
    err "ABORT: another backup.sh run holds $DEST/.backup.lock"
    exit 1
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
# Only the dump's own filesystem: a bare `sync` flushes every mount, and one
# stalled share would hold it past every limit in this script.
sync -f -- "$out"
log "wrote $(du -h "$out" | cut -f1) ($BYTES bytes)"

# ── Copy ─────────────────────────────────────────────────────────────────────
# After the dump and before the prune, so the prune knows which local dumps
# the copy folder holds.

if [[ -n $COPY_DIR ]]; then
    if copy_dumps "$(basename "$out")"; then
        log "copy: $COPY_DETAIL"
    else
        err "copy FAILED: $COPY_DETAIL"
    fi
fi

# ── Prune ────────────────────────────────────────────────────────────────────
# Only runs after a verified dump exists, so a bad night can never leave you
# with fewer good backups than you started with. With a copy folder, a dump
# also stays until the folder holds a checked copy of it, so a share that is
# down costs local disk (one dump a day) and never the only copy.

pruned=0; held=0
# With a copy folder, the prune takes only the names the copy step handles, so
# a hand-made file (e.g., sentinel-pre-migration.sql.gz) stays put and is not
# held every night for a copy it never gets. Without one, every
# sentinel-*.sql.gz counts, as before.
if [[ -n $COPY_DIR ]]; then prune_glob=$DUMP_GLOB; else prune_glob='sentinel-*.sql.gz'; fi
# shellcheck disable=SC2086 # $prune_glob is a pattern and must expand
while IFS= read -r old; do
    [ -n "$old" ] || continue
    # This run's dump is never pruned. Its mtime is the oldest when the clock
    # has stepped back since the last run, and the prune goes by mtime.
    [[ $old == "$out" ]] && continue
    if [[ -n $COPY_DIR && -z ${VERIFIED[$(basename "$old")]:-} ]]; then
        log "keeping $(basename "$old") until $COPY_DIR holds a checked copy"
        held=$((held + 1))
        continue
    fi
    log "pruning $(basename "$old")"
    rm -f "$old" && pruned=$((pruned+1))
done < <(ls -1t "$DEST"/$prune_glob 2>/dev/null | tail -n +$((KEEP + 1)))

remaining=$(ls -1 "$DEST"/sentinel-*.sql.gz 2>/dev/null | wc -l | tr -d ' ')
STATUS="ok"; DETAIL="wrote $(basename "$out"); pruned $pruned; $remaining retained"
if ((held)); then DETAIL="$DETAIL, $held held for the copy"; fi
log "done — $remaining backups retained, $pruned pruned, $held held for the copy"
