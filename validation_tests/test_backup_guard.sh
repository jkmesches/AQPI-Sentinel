#!/usr/bin/env bash
# ops/backup.sh mount guard + status-file discipline.
#
#   bash validation_tests/test_backup_guard.sh
#
# Runs only `--check`, which validates configuration and exits without dumping.
# Needs Docker (backup.sh verifies the Postgres container before it reaches the
# guard, so a throwaway container stands in) and one unprivileged mountpoint.
# Skips loudly rather than faking either — a stubbed `mountpoint` would test the
# stub, not the guard.
#
# This exists because the guard has caused two silent failures, both
# config-shaped rather than code-shaped:
#
#   1. Hardening moved SENTINEL_BACKUP_DIR's default off the NFS share and
#      MOUNT_ROOT's default onto $DEST. The cron line carried no environment,
#      so from the moment it deployed the guard refused to write every night.
#      Two days of backups were lost and nothing said so.
#
#   2. `--check` exited through the same EXIT trap that records outcomes, so it
#      wrote status=ok with a fresh timestamp. layer0.self.backup keys on
#      status + age, so it reported "last backup 0.0h ago". Running the
#      documented validation command reset the staleness clock.
#
# Invariants: naming a directory means "verify my storage is attached", taking
# the default means "local disk is fine"; and status.json records BACKUP
# ATTEMPTS only.
#
# Every assertion matches the SPECIFIC abort reason. An earlier draft matched
# only "did it abort", and passed while the run was aborting on the container
# check without ever reaching the guard.
set -uo pipefail
cd "$(dirname "$0")/.."
SCRIPT=ops/backup.sh
fails=0

MNT=/dev/shm
mountpoint -q "$MNT" 2>/dev/null || { echo "SKIP — no unprivileged mountpoint at $MNT"; exit 0; }
command -v docker >/dev/null 2>&1 || { echo "SKIP — docker not available"; exit 0; }

PG=sentinel-guard-test-pg
docker rm -f "$PG" >/dev/null 2>&1
docker run -d --name "$PG" alpine sleep 600 >/dev/null 2>&1 \
    || { echo "SKIP — cannot start a throwaway container"; exit 0; }

TMP="$(mktemp -d)"
SHARE="$MNT/sentinel-guard-test"; mkdir -p "$SHARE/db"
cleanup() { docker rm -f "$PG" >/dev/null 2>&1; rm -rf "$TMP" "$SHARE"; }
trap cleanup EXIT

run() { env SENTINEL_PG_CONTAINER="$PG" "$@" bash "$SCRIPT" --check 2>&1; }

expect() {  # expect <label> <regex the output must match> [env...]
    local label="$1" want="$2"; shift 2
    local out
    out="$(run "$@")"
    if grep -qE "$want" <<<"$out"; then
        printf '  ok    %s\n' "$label"
    else
        printf '  FAIL  %s\n         wanted /%s/, got:\n' "$label" "$want"
        sed 's/^/         /' <<<"$out" | tail -3
        fails=$((fails + 1))
    fi
}

refute() {  # refute <label> <regex the output must NOT match> [env...]
    local label="$1" bad="$2"; shift 2
    local out
    out="$(run "$@")"
    if grep -qE "$bad" <<<"$out"; then
        printf '  FAIL  %s\n         output matched /%s/:\n' "$label" "$bad"
        sed 's/^/         /' <<<"$out" | tail -3
        fails=$((fails + 1))
    else
        printf '  ok    %s\n' "$label"
    fi
}

GUARD='root filesystem|not a mountpoint'

echo "mount guard:"
# Taking the built-in default IS the choice to use local disk, so the guard
# must stay off. Asserted negatively because /var/backups/sentinel is not
# writable unprivileged — the run still fails, just never at the guard.
refute "unset DIR does not arm the guard" "$GUARD" \
    SENTINEL_BACKUP_STATUS_FILE="$TMP/s.json"
# Naming a directory means "this is my storage" — verify it is really there.
expect "named DIR on the root filesystem aborts" "is on the root filesystem" \
    SENTINEL_BACKUP_DIR="$TMP/dumps"
# The case production hit: a share is mounted at a root and dumps live in a
# subdirectory, so the dump directory is almost never a mountpoint itself.
expect "named DIR NESTED under a mount is accepted" "config OK" \
    SENTINEL_BACKUP_DIR="$SHARE/db"
expect "a not-yet-created dir under a mount is accepted" "config OK" \
    SENTINEL_BACKUP_DIR="$SHARE/db/deeper/still"
expect "REQUIRE_MOUNT=0 permits the root filesystem" "config OK" \
    SENTINEL_BACKUP_DIR="$TMP/dumps" SENTINEL_BACKUP_REQUIRE_MOUNT=0
expect "REQUIRE_MOUNT=1 arms the guard on the default" "is on the root filesystem" \
    SENTINEL_BACKUP_REQUIRE_MOUNT=1 SENTINEL_BACKUP_STATUS_FILE="$TMP/s.json"
expect "an explicit MOUNT_ROOT is still honoured" "config OK" \
    SENTINEL_BACKUP_DIR="$SHARE/db" SENTINEL_BACKUP_MOUNT_ROOT="$MNT"
expect "an explicit MOUNT_ROOT that is not mounted aborts" "is not a mountpoint" \
    SENTINEL_BACKUP_DIR="$SHARE/db" SENTINEL_BACKUP_MOUNT_ROOT=/nonexistent-mount

echo "status file discipline:"
STATUS="$SHARE/db/status.json"
printf '{"status":"ok","finished_at":"2020-01-01T00:00:00+00:00"}' > "$STATUS"
before="$(cat "$STATUS")"

run SENTINEL_BACKUP_DIR="$SHARE/db" >/dev/null 2>&1
if [ "$(cat "$STATUS")" = "$before" ]; then
    echo "  ok    a passing --check leaves status.json untouched"
else
    echo "  FAIL  a passing --check rewrote status.json"; fails=$((fails + 1))
fi

# An aborting --check must not record a failed attempt either — it did not
# attempt one. Only setting the flag before the EXIT trap is armed makes this
# true on the abort path.
run SENTINEL_BACKUP_DIR="$TMP/dumps" SENTINEL_BACKUP_STATUS_FILE="$STATUS" >/dev/null 2>&1
if [ "$(cat "$STATUS")" = "$before" ]; then
    echo "  ok    an aborting --check leaves status.json untouched"
else
    echo "  FAIL  an aborting --check rewrote status.json"; fails=$((fails + 1))
fi

[ "$fails" -eq 0 ] && { echo; echo "all backup-guard assertions passed"; exit 0; }
echo; echo "$fails FAILED"; exit 1
