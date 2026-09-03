#!/usr/bin/env bash
# Import a Sentinel data bundle produced by ops/export-data.sh.
#
#   bash ops/import-data.sh <bundle-dir> [--force] [--yes]
#
# Restores database.sql.gz into the target deployment, then re-counts every
# table and compares against manifest.json. A transfer that lost rows is
# caught here rather than quietly becoming a smaller dataset that looks fine.
#
# ── Safety ───────────────────────────────────────────────────────────────────
# Refuses by default if the target database already holds data. Restoring a
# dump over a populated database is not a merge — it is a mess of primary-key
# collisions and half-applied tables, and the state afterwards is neither the
# old data nor the new. --force drops and recreates the schema first, which is
# destructive and says so.
#
# ── Configuration ────────────────────────────────────────────────────────────
#   SENTINEL_PG_CONTAINER   Postgres container   default: sentinel-postgres
#   POSTGRES_USER           database user        default: sentinel
#   POSTGRES_DB             database name        default: sentinel

set -euo pipefail

BUNDLE="${1:-}"; shift || true
FORCE=0; ASSUME_YES=0
while [ $# -gt 0 ]; do
    case "$1" in
        --force) FORCE=1; shift ;;
        --yes|-y) ASSUME_YES=1; shift ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done

CONTAINER="${SENTINEL_PG_CONTAINER:-sentinel-postgres}"
PGUSER="${POSTGRES_USER:-sentinel}"
PGDB="${POSTGRES_DB:-sentinel}"

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
die()  { echo "[$(date -uIs)] ABORT: $*" >&2; exit 1; }

[ -n "$BUNDLE" ] || die "usage: bash ops/import-data.sh <bundle-dir> [--force]"
[ -d "$BUNDLE" ] || die "$BUNDLE is not a directory"
DUMP="$BUNDLE/database.sql.gz"
MANIFEST="$BUNDLE/manifest.json"
[ -f "$DUMP" ]     || die "$DUMP not found — is this an export bundle?"
[ -f "$MANIFEST" ] || die "$MANIFEST not found — refusing to import unverifiable data"

command -v docker >/dev/null 2>&1 || die "docker not found — run this on the docker host"
docker inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null | grep -q true \
    || die "container '$CONTAINER' is not running (set SENTINEL_PG_CONTAINER)"

# client_min_messages=warning suppresses the "drop cascades to table ..."
# NOTICE flood from DROP SCHEMA CASCADE, which otherwise buries the progress
# output under 21 lines of noise at exactly the moment the operator is
# watching for a problem.
# === Load-bearing: no -i on the query helper ===
#
# `docker exec -i` attaches the caller's stdin. Inside the `while read` loop
# that verifies row counts, that means the FIRST psql call consumes the rest
# of the loop's input — so verification silently checked one table and
# declared success. The manifest exists precisely to catch a short transfer;
# a verifier that only looks at one table is worse than none, because it
# reports "verified".
#
# Queries therefore run WITHOUT -i. Only the restore, which genuinely pipes a
# dump in, uses -i (inline below).
#
# client_min_messages=warning suppresses the "drop cascades to ..." NOTICE
# flood from DROP SCHEMA CASCADE, which otherwise buries the progress output.
psql() { docker exec "$CONTAINER" psql -U "$PGUSER" -d "$PGDB" -tAq -c "SET client_min_messages=warning;" "$@" </dev/null; }

# ── Verify the bundle before touching the database ───────────────────────────
log "verifying bundle"
EXPECT_SHA=$(python3 -c "import json;print(json.load(open('$MANIFEST'))['database']['sha256'])")
ACTUAL_SHA=$(sha256sum "$DUMP" | cut -d' ' -f1)
# Written as an explicit if rather than `|| die ... && log ...`: that form
# chains the && off die's status and only behaves because die exits.
if [ "$EXPECT_SHA" != "$ACTUAL_SHA" ]; then
    die "checksum mismatch — the dump was corrupted in transit" \
        "expected $EXPECT_SHA" "got      $ACTUAL_SHA"
fi
log "  checksum matches manifest"
gzip -t "$DUMP" || die "dump is not a valid gzip stream"
dump_has_schema "$DUMP" || die "dump does not contain the Sentinel schema"
log "  dump is a valid Sentinel backup"
python3 -c "
import json;d=json.load(open('$MANIFEST'))
print('  exported %s from Sentinel %s (postgres %s)' % (d['exported_at'], d['sentinel_version'], d['postgres_version']))
print('  %d rows across %d tables' % (sum(d['row_counts'].values()), len(d['row_counts'])))
r=d.get('check_runs_range') or {}
print('  check_runs span: %s .. %s' % (r.get('oldest','?'), r.get('newest','?')))"

# ── Decide what is in the way ────────────────────────────────────────────────
#
# Three distinct states, and conflating them is what makes an importer either
# dangerous or useless:
#
#   no schema      first boot has not happened. Restore straight in.
#   schema, 0 rows THE NORMAL CASE. A freshly deployed Sentinel creates its
#                  tables on first startup, so the target always has empty
#                  tables by the time anyone runs this. pg_dump emits
#                  CREATE TABLE, which collides — so the schema must be
#                  dropped first. Nothing is lost, so this needs no --force.
#   schema + rows  real data. Destroying it requires --force and a typed
#                  confirmation.
#
# The first version only counted check_runs and alarms, so the normal case
# fell through to the restore and failed with `relation "ack_tokens" already
# exists` — the importer did not work for the situation it exists for.

N_TABLES=$(psql -c "SELECT count(*) FROM pg_tables WHERE schemaname='public';" 2>/dev/null | tr -d '[:space:]' || echo 0)
N_TABLES="${N_TABLES:-0}"

EXISTING=0
if [ "$N_TABLES" != "0" ]; then
    # Sum rows across every public table, not a hand-picked pair.
    EXISTING=$(psql -c "
        SELECT coalesce(sum(n),0) FROM (
          SELECT (xpath('/row/c/text()',
                  query_to_xml(format('SELECT count(*) AS c FROM %I.%I', schemaname, tablename),
                               false, true, '')))[1]::text::bigint AS n
          FROM pg_tables WHERE schemaname='public'
        ) t;" 2>/dev/null | tr -d '[:space:]' || echo 0)
    EXISTING="${EXISTING:-0}"
fi

if [ "$EXISTING" != "0" ]; then
    if [ "$FORCE" != "1" ]; then
        die "target database already holds $EXISTING rows across $N_TABLES tables." \
            "Restoring over populated tables is not a merge — it collides on" \
            "primary keys and leaves neither the old data nor the new." \
            "Re-run with --force to DROP the public schema and replace it."
    fi
    log "WARNING: --force will DROP SCHEMA public CASCADE — $EXISTING rows destroyed"
    if [ "$ASSUME_YES" != "1" ]; then
        printf "  Type the database name (%s) to confirm: " "$PGDB"
        read -r reply
        [ "$reply" = "$PGDB" ] || die "confirmation did not match — nothing changed"
    fi
    log "dropping populated schema"
    psql -c "DROP SCHEMA public CASCADE; CREATE SCHEMA public;" >/dev/null
elif [ "$N_TABLES" != "0" ]; then
    log "target has $N_TABLES empty tables from first boot — clearing them to restore"
    psql -c "DROP SCHEMA public CASCADE; CREATE SCHEMA public;" >/dev/null
else
    log "target database is empty"
fi

# ── Restore ──────────────────────────────────────────────────────────────────
log "restoring — the backend may log errors until this finishes"
if ! gzip -dc "$DUMP" | docker exec -i "$CONTAINER" psql -U "$PGUSER" -d "$PGDB" -v ON_ERROR_STOP=1 -q >/tmp/sentinel-import.log 2>&1; then
    die "restore failed" "$(tail -5 /tmp/sentinel-import.log 2>/dev/null || true)"
fi
log "  restore completed"

# ── Verify what actually landed ──────────────────────────────────────────────
# The whole reason the manifest exists. Sentinel keeps writing during an
# export, so the restore may hold slightly MORE than the manifest recorded;
# that is fine. Fewer rows means the transfer or restore lost data.
log "verifying row counts against the manifest"
MISMATCH=0
while IFS='|' read -r table expected; do
    [ -n "$table" ] || continue
    actual=$(psql -c "SELECT count(*) FROM \"$table\";" 2>/dev/null | tr -d '[:space:]')
    actual="${actual:-MISSING}"
    if [ "$actual" = "MISSING" ]; then
        echo "  MISSING  $table (expected $expected)"; MISMATCH=$((MISMATCH+1))
    elif [ "$actual" -lt "$expected" ]; then
        echo "  SHORT    $table: $actual < $expected expected"; MISMATCH=$((MISMATCH+1))
    else
        extra=""; [ "$actual" -gt "$expected" ] && extra=" (+$((actual-expected)) written since export)"
        printf "  ok       %-22s %s%s\n" "$table" "$actual" "$extra"
    fi
done < <(python3 -c "
import json
for t,n in sorted(json.load(open('$MANIFEST'))['row_counts'].items()): print('%s|%d' % (t,n))")

if [ "$MISMATCH" -gt 0 ]; then
    die "$MISMATCH table(s) have fewer rows than the manifest records." \
        "The data did not arrive intact. Do not treat this deployment as" \
        "carrying the exported history."
fi

log "import verified — all tables match or exceed the manifest"
echo
echo "  Restart the backend so it picks up the restored data:"
echo "    docker compose -f ops/docker-compose.deploy.yml --env-file ops/.env restart backend"
echo
echo "  The image archive is transferred separately — see README.txt in the bundle."
