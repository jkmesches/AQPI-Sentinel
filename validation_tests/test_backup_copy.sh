#!/usr/bin/env bash
# ops/backup.sh copy to a second folder (SENTINEL_BACKUP_COPY_DIR), on full runs.
#
#   bash validation_tests/test_backup_copy.sh
#
# A throwaway alpine container with a fake pg_dump stands in for Postgres, and
# /dev/shm, a tmpfs mountpoint, stands in for the share. Skips loudly without
# Docker or /dev/shm, as test_backup_guard.sh does. The invariants are that a
# local dump is pruned only once the copy folder holds a checked copy, that a
# share that is down or stalls costs local disk and never fails the dump, and
# that status.json says which.
set -uo pipefail  # no errexit: keeps going after a failed assertion, to report them all
cd "$(dirname "$0")/.." || exit 1
# Overridable so a planted fault in a scratch copy can be run against these tests.
readonly SCRIPT=${BACKUP_SCRIPT:-ops/backup.sh}
readonly MNT=/dev/shm
readonly PG=sentinel-copy-test-pg
fails=0

ok()   { printf '  ok    %s\n' "$1"; }
bad()  { printf '  FAIL  %s  (%s)\n' "$1" "$2"; fails=$((fails + 1)); }
want() { if [[ $2 == "$3" ]]; then ok "$1"; else bad "$1" "got '$2', wanted '$3'"; fi; }
field() {
    python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get(sys.argv[2], ""))' \
        "$1" "$2"
}
count() { find "$1" -maxdepth 1 -type f -name 'sentinel-*.sql.gz' | wc -l; }

# seed DIR NAME: an old valid dump called NAME in DIR, modified at the time in its
# name, since backup.sh prunes local dumps by modification time.
seed() {
    local when
    when=$(sed -E 's/^sentinel-(....)(..)(..)T(..)(..)(..)Z.*/\1-\2-\3 \4:\5:\6Z/' <<<"$2")
    printf 'CREATE TABLE public.check_runs (id integer);\n-- %s\n' "$2" | gzip > "$1/$2"
    touch -d "$when" "$1/$2"
}

# run_backup LOCAL [ENV...]: one full backup.sh run into LOCAL. Prints its output.
run_backup() {
    local local_dir=$1
    shift
    env SENTINEL_PG_CONTAINER="$PG" SENTINEL_BACKUP_DIR="$local_dir" \
        SENTINEL_BACKUP_REQUIRE_MOUNT=0 SENTINEL_BACKUP_MIN_FREE_MB=1 "$@" bash "$SCRIPT" 2>&1
}

start_container() {
    docker rm -f "$PG" >/dev/null 2>&1
    docker run -d --name "$PG" alpine sleep 900 >/dev/null 2>&1 || return 1
    # Random filler, so each dump differs and clears backup.sh's 512-byte floor.
    docker exec "$PG" sh -c 'printf "%s\n" "#!/bin/sh" \
        "echo \"CREATE TABLE public.check_runs (id integer);\"" \
        "head -c 2048 /dev/urandom | od -An -tx1" > /usr/local/bin/pg_dump \
        && chmod +x /usr/local/bin/pg_dump'
}

copy_off() {
    local d="$TMP/off"
    mkdir -p "$d"
    seed "$d" sentinel-20260101T000000Z.sql.gz
    seed "$d" sentinel-20260102T000000Z.sql.gz
    run_backup "$d" SENTINEL_BACKUP_KEEP=2 >/dev/null
    want "no copy folder: status ok" "$(field "$d/status.json" status)" ok
    want "...copy_status off" "$(field "$d/status.json" copy_status)" off
    want "...prunes to KEEP as before" "$(count "$d")" 2
}

copy_on() {
    local d="$TMP/on" c="$SHARE/on" new
    mkdir -p "$d" "$c"
    seed "$d" sentinel-20260101T000000Z.sql.gz
    seed "$d" sentinel-20260102T000000Z.sql.gz
    seed "$d" sentinel-20260103T000000Z.sql.gz
    printf 'not the dump\n' > "$c/sentinel-20260103T000000Z.sql.gz"
    seed "$c" sentinel-20251201T000000Z.sql.gz
    seed "$c" sentinel-20251202T000000Z.sql.gz
    run_backup "$d" SENTINEL_BACKUP_KEEP=2 SENTINEL_BACKUP_COPY_DIR="$c" \
        SENTINEL_BACKUP_COPY_KEEP=3 >/dev/null
    new=$(basename "$(field "$d/status.json" artifact)")
    want "mounted copy: status ok" "$(field "$d/status.json" status)" ok
    want "...copy_status ok" "$(field "$d/status.json" copy_status)" ok
    want "...copy_newest is this run's dump" "$(field "$d/status.json" copy_newest)" "$new"
    want "...copy_newest_at is its time" "$(field "$d/status.json" copy_newest_at)" \
        "$(sed -E 's/^sentinel-(....)(..)(..)T(..)(..)(..)Z.*/\1-\2-\3T\4:\5:\6+00:00/' <<<"$new")"
    want "...the copy folder keeps COPY_KEEP" "$(count "$c")" 3
    want "...the newest three are kept" "$(cd "$c" && ls sentinel-* | sort | head -1)" \
        sentinel-20260102T000000Z.sql.gz
    want "...this run's copy matches" "$(sha256sum < "$c/$new")" "$(sha256sum < "$d/$new")"
    want "...a corrupted copy is copied again" \
        "$(sha256sum < "$c/sentinel-20260103T000000Z.sql.gz")" \
        "$(sha256sum < "$d/sentinel-20260103T000000Z.sql.gz")"
    want "...copies are 600" "$(stat -c %a "$c/$new")" 600
    want "...no .part is left" "$(find "$c" -name '*.part' | wc -l)" 0
    want "...local prunes to KEEP once copied" "$(count "$d")" 2
}

copy_unmounted() {
    local d="$TMP/down"
    mkdir -p "$d"
    seed "$d" sentinel-20260101T000000Z.sql.gz
    seed "$d" sentinel-20260102T000000Z.sql.gz
    run_backup "$d" SENTINEL_BACKUP_KEEP=1 \
        SENTINEL_BACKUP_COPY_DIR=/nonexistent-sentinel-copy-test/backups >/dev/null
    want "share not mounted: the dump is still ok" "$(field "$d/status.json" status)" ok
    want "...copy_status failed" "$(field "$d/status.json" copy_status)" failed
    grep -q "root filesystem" <<<"$(field "$d/status.json" copy_detail)" \
        && ok "...and says why" || bad "...and says why" "$(field "$d/status.json" copy_detail)"
    want "...no local dump is pruned without a copy" "$(count "$d")" 3
    want "...and nothing was created under /" \
        "$(test -e /nonexistent-sentinel-copy-test && echo yes || echo no)" no
}

copy_stalls() {
    local d="$TMP/stall" c="$SHARE/stall" t0 took
    mkdir -p "$d" "$c"
    seed "$d" sentinel-20260101T000000Z.sql.gz
    mkfifo "$c/sentinel-20260101T000000Z.sql.gz.part"
    t0=$(date +%s)
    run_backup "$d" SENTINEL_BACKUP_KEEP=1 SENTINEL_BACKUP_COPY_DIR="$c" \
        SENTINEL_BACKUP_COPY_TIMEOUT_S=2 >/dev/null
    took=$(($(date +%s) - t0))
    want "a stalled copy: the dump is still ok" "$(field "$d/status.json" status)" ok
    want "...copy_status failed" "$(field "$d/status.json" copy_status)" failed
    ((took < 60)) && ok "...the limit ended it (${took}s)" || bad "...the limit ended it" "${took}s"
    want "...the newer dump's copy still counts" \
        "$(field "$d/status.json" copy_newest)" "$(basename "$(field "$d/status.json" artifact)")"
    want "...the uncopied dump is held" "$(count "$d")" 2
}

yes_no() { if [[ -e $1 ]]; then echo yes; else echo no; fi; }
says() { if grep -q -- "$3" <<<"$2"; then ok "$1"; else bad "$1" "$2"; fi; }

# The clock stepped back: this run's dump is named and dated before all others.
clock_back() {
    local d="$TMP/skew" c="$SHARE/skew" new
    mkdir -p "$d" "$c"
    seed "$d" sentinel-20990101T000000Z.sql.gz
    seed "$d" sentinel-20990102T000000Z.sql.gz
    seed "$c" sentinel-20990103T000000Z.sql.gz
    run_backup "$d" SENTINEL_BACKUP_KEEP=2 SENTINEL_BACKUP_COPY_DIR="$c" \
        SENTINEL_BACKUP_COPY_KEEP=2 >/dev/null
    new=$(basename "$(field "$d/status.json" artifact)")
    want "clock stepped back: this run's dump stays local" "$(yes_no "$d/$new")" yes
    want "...and on the share" "$(yes_no "$c/$new")" yes
}

same_filesystem() {
    local d="$SHARE/samefs-local" c="$SHARE/samefs-copy"
    mkdir -p "$d" "$c"
    seed "$d" sentinel-20260101T000000Z.sql.gz
    run_backup "$d" SENTINEL_BACKUP_KEEP=1 SENTINEL_BACKUP_COPY_DIR="$c" >/dev/null
    want "copy folder on the dumps' filesystem: copy failed" \
        "$(field "$d/status.json" copy_status)" failed
    says "...and says why" "$(field "$d/status.json" copy_detail)" "same filesystem"
    want "...nothing is pruned on the strength of it" "$(count "$d")" 2
}

hand_named() {
    local d="$TMP/hand" c="$SHARE/hand" odd=sentinel-pre-migration.sql.gz
    mkdir -p "$d" "$c"
    printf 'CREATE TABLE public.check_runs (id integer);\n' | gzip > "$d/$odd"
    touch -d '2026-01-01 00:00:00Z' "$d/$odd"
    run_backup "$d" SENTINEL_BACKUP_KEEP=1 SENTINEL_BACKUP_COPY_DIR="$c" >/dev/null
    want "a hand-named dump: the copy is ok" "$(field "$d/status.json" copy_status)" ok
    want "...it is not copied" "$(yes_no "$c/$odd")" no
    want "...the newest mark stays this run's dump" "$(field "$d/status.json" copy_newest)" \
        "$(basename "$(field "$d/status.json" artifact)")"
    want "...and it stays local" "$(yes_no "$d/$odd")" yes
    if grep -q "held" <<<"$(field "$d/status.json" detail)"; then
        bad "...and is not held for a copy it never gets" "$(field "$d/status.json" detail)"
    else
        ok "...and is not held for a copy it never gets"
    fi
}

# No copy folder: a hand-made name past KEEP is pruned, as before the copy step.
hand_named_off() {
    local d="$TMP/hand-off" odd=sentinel-pre-migration.sql.gz
    mkdir -p "$d"
    printf 'CREATE TABLE public.check_runs (id integer);\n' | gzip > "$d/$odd"
    touch -d '2026-01-01 00:00:00Z' "$d/$odd"
    run_backup "$d" SENTINEL_BACKUP_KEEP=1 >/dev/null
    want "no copy folder: a hand-named dump past KEEP is pruned as before" "$(yes_no "$d/$odd")" no
}

share_mode_kept() {
    local d="$TMP/mode" c="$SHARE/mode"
    mkdir -p "$d" "$c"
    chmod 755 "$c"
    run_backup "$d" SENTINEL_BACKUP_COPY_DIR="$c" >/dev/null
    want "an existing share folder keeps its mode" "$(stat -c %a "$c")" 755
}

locked() {
    local d="$TMP/lock" before out holder
    mkdir -p "$d"
    printf '{"status":"ok","finished_at":"2020-01-01T00:00:00+00:00"}' > "$d/status.json"
    before=$(<"$d/status.json")
    setsid flock "$d/.backup.lock" sleep 20 &
    holder=$!
    for _ in {1..50}; do flock -n "$d/.backup.lock" true || break; sleep 0.1; done
    out=$(run_backup "$d")
    kill -- -"$holder" 2>/dev/null
    wait "$holder" 2>/dev/null
    says "a held lock stops the run" "$out" "another backup.sh run holds"
    want "...status.json is left alone" "$(<"$d/status.json")" "$before"
    want "...and no dump is taken" "$(count "$d")" 0
}

odd_path() {
    local d="$TMP/quote" c="$SHARE/q\"x\\y"
    mkdir -p "$d" "$c"
    run_backup "$d" SENTINEL_BACKUP_COPY_DIR="$c" >/dev/null
    want "a quote in the share's path: status.json still parses" \
        "$(field "$d/status.json" copy_status)" ok
    want "...and records the path as it is" "$(field "$d/status.json" copy_dir)" "$c"
}

orphan_part() {
    local d="$TMP/orphan" c="$SHARE/orphan" stale=sentinel-20260101T000000Z.sql.gz.part
    mkdir -p "$d" "$c"
    printf 'partial' > "$c/$stale"
    touch -d '2 days ago' "$c/$stale"
    run_backup "$d" SENTINEL_BACKUP_COPY_DIR="$c" >/dev/null
    want "a .part left two days ago is swept" "$(yes_no "$c/$stale")" no
}

bad_local() {
    local d="$TMP/rot" c="$SHARE/rot" name=sentinel-20260101T000000Z.sql.gz good
    mkdir -p "$d" "$c"
    seed "$c" "$name"
    good=$(sha256sum < "$c/$name")
    printf 'bit rot' > "$d/$name"
    touch -d '2026-01-01 00:00:00Z' "$d/$name"
    run_backup "$d" SENTINEL_BACKUP_KEEP=1 SENTINEL_BACKUP_COPY_DIR="$c" >/dev/null
    want "a local dump failing gzip -t: the share's copy is kept" \
        "$(sha256sum < "$c/$name")" "$good"
    says "...the copy fails and says why" "$(field "$d/status.json" copy_detail)" "gzip -t"
    want "...and the local file is held" "$(yes_no "$d/$name")" yes
}

other_mark() {
    local d="$TMP/mark"
    mkdir -p "$d"
    printf 'sentinel-20260101T000000Z.sql.gz\n/some/other/share\n' > "$d/.copy-verified"
    run_backup "$d" SENTINEL_BACKUP_COPY_DIR=/nonexistent-sentinel-copy-test/b >/dev/null
    want "a mark left by another copy folder does not count" \
        "$(field "$d/status.json" copy_newest)" ""
}

check_mode() {
    local base=(env SENTINEL_PG_CONTAINER="$PG" SENTINEL_BACKUP_DIR="$TMP/chk"
                SENTINEL_BACKUP_REQUIRE_MOUNT=0 SENTINEL_BACKUP_MIN_FREE_MB=1)
    says "--check refuses an unmounted copy folder" \
        "$("${base[@]}" SENTINEL_BACKUP_COPY_DIR=/nonexistent-x/b bash "$SCRIPT" --check 2>&1)" \
        "copy folder not ready"
    says "--check refuses COPY_KEEP=0" \
        "$("${base[@]}" SENTINEL_BACKUP_COPY_DIR="$SHARE/chk" SENTINEL_BACKUP_COPY_KEEP=0 \
            bash "$SCRIPT" --check 2>&1)" "COPY_KEEP must be"
    says "--check refuses COPY_KEEP under KEEP" \
        "$("${base[@]}" SENTINEL_BACKUP_COPY_DIR="$SHARE/chk" SENTINEL_BACKUP_KEEP=3 \
            SENTINEL_BACKUP_COPY_KEEP=2 bash "$SCRIPT" --check 2>&1)" "must be at least"
    says "a stray COPY_KEEP without a copy folder changes nothing" \
        "$("${base[@]}" SENTINEL_BACKUP_COPY_KEEP=abc bash "$SCRIPT" --check 2>&1)" "config OK"
    says "--check accepts a mounted copy folder" \
        "$("${base[@]}" SENTINEL_BACKUP_COPY_DIR="$SHARE/chk" bash "$SCRIPT" --check 2>&1)" \
        "copy OK"
}

main() {
    mountpoint -q "$MNT" 2>/dev/null || { echo "SKIP: no unprivileged mountpoint at $MNT"; exit 0; }
    command -v docker >/dev/null 2>&1 || { echo "SKIP: docker not available"; exit 0; }
    start_container || { echo "SKIP: cannot start a throwaway container"; exit 0; }
    TMP=$(mktemp -d)
    SHARE="$MNT/sentinel-copy-test-$$"
    mkdir -p "$SHARE"
    trap 'docker rm -f "$PG" >/dev/null 2>&1; rm -rf -- "${TMP:?}" "${SHARE:?}"' EXIT
    copy_off
    copy_on
    copy_unmounted
    copy_stalls
    clock_back
    same_filesystem
    hand_named
    hand_named_off
    share_mode_kept
    locked
    odd_path
    orphan_part
    bad_local
    other_mark
    check_mode
    if ((fails == 0)); then echo; echo "all backup-copy assertions passed"; exit 0; fi
    echo; echo "$fails FAILED"; exit 1
}

main "$@"
