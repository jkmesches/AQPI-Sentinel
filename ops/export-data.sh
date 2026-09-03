#!/usr/bin/env bash
# Export a Sentinel deployment's data as a portable, verifiable bundle.
#
#   bash ops/export-data.sh [-o OUTDIR] [--with-archive]
#
# Produces  <outdir>/sentinel-export-<stamp>/
#     database.sql.gz    full pg_dump
#     manifest.json      row counts, checksums, date range, versions
#     README.txt         how to import it
#     archive.tar        only with --with-archive (see the warning below)
#
# The manifest is the point. A dump alone cannot tell the receiving end
# whether what arrived is what left — import-data.sh re-counts every table
# after restoring and compares, so a truncated transfer is caught rather than
# silently becoming a smaller dataset that looks fine.
#
# ── Configuration ────────────────────────────────────────────────────────────
#   SENTINEL_PG_CONTAINER   Postgres container      default: sentinel-postgres
#   POSTGRES_USER           database user           default: sentinel
#   POSTGRES_DB             database name           default: sentinel
#   SENTINEL_ARCHIVE_DIR    host path to the image archive, for --with-archive
#                           default: read from the backend container's mount
#
# ── About the image archive ──────────────────────────────────────────────────
# The L4 image archive is large (~22 GB on the reference deployment) and
# content-addressed, so it is a poor fit for a tarball and an excellent fit for
# rsync: transfers resume, and identical files are skipped. --with-archive is
# offered for completeness, but for anything over a few GB use the rsync
# command this script prints instead.

set -euo pipefail

OUTDIR="."
WITH_ARCHIVE=0
while [ $# -gt 0 ]; do
    case "$1" in
        -o|--out) OUTDIR="$2"; shift 2 ;;
        --with-archive) WITH_ARCHIVE=1; shift ;;
        -h|--help) sed -n '2,30p' "$0"; exit 0 ;;
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

log() { echo "[$(date -uIs)] $*"; }
die() { echo "[$(date -uIs)] ABORT: $*" >&2; exit 1; }

command -v docker >/dev/null 2>&1 || die "docker not found — run this on the docker host"
docker inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null | grep -q true \
    || die "container '$CONTAINER' is not running (set SENTINEL_PG_CONTAINER)"

psql() { docker exec -i "$CONTAINER" psql -U "$PGUSER" -d "$PGDB" -tAq "$@"; }

stamp="$(date -u +%Y%m%dT%H%M%SZ)"
BUNDLE="$OUTDIR/sentinel-export-$stamp"
mkdir -p "$BUNDLE" || die "cannot create $BUNDLE"
log "bundle: $BUNDLE"

# ── Row counts BEFORE the dump ───────────────────────────────────────────────
# Taken first so the manifest describes at least what existed when the dump
# started. Sentinel keeps writing during the export, so the restored database
# may hold slightly MORE than the manifest says — import verification treats
# that as fine and a shortfall as an error.
log "counting rows"
TABLES=$(psql -c "SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename;")
counts="{}"
for t in $TABLES; do
    n=$(psql -c "SELECT count(*) FROM \"$t\";" | tr -d '[:space:]')
    counts=$(printf '%s' "$counts" | python3 -c "
import json,sys
d=json.load(sys.stdin); d['$t']=int('$n'); print(json.dumps(d))")
done

RANGE=$(psql -c "SELECT coalesce(min(finished_at)::text,'') || '|' || coalesce(max(finished_at)::text,'') FROM check_runs;" | tr -d '\n')
OLDEST="${RANGE%%|*}"; NEWEST="${RANGE##*|}"
PGVER=$(psql -c "SHOW server_version;" | tr -d '[:space:]')

# ── Dump ─────────────────────────────────────────────────────────────────────
log "dumping $PGDB"
docker exec "$CONTAINER" pg_dump -U "$PGUSER" -d "$PGDB" | gzip -6 > "$BUNDLE/database.sql.gz.part" \
    || { rm -f "$BUNDLE/database.sql.gz.part"; die "pg_dump failed"; }
gzip -t "$BUNDLE/database.sql.gz.part" || { rm -f "$BUNDLE/database.sql.gz.part"; die "dump failed integrity check"; }
dump_has_schema "$BUNDLE/database.sql.gz.part" \
    || { rm -f "$BUNDLE/database.sql.gz.part"; die "dump does not contain the Sentinel schema"; }
mv "$BUNDLE/database.sql.gz.part" "$BUNDLE/database.sql.gz"

SHA=$(sha256sum "$BUNDLE/database.sql.gz" | cut -d' ' -f1)
BYTES=$(stat -c%s "$BUNDLE/database.sql.gz" 2>/dev/null || stat -f%z "$BUNDLE/database.sql.gz")
log "database.sql.gz  $(du -h "$BUNDLE/database.sql.gz" | cut -f1)"

# ── Archive ──────────────────────────────────────────────────────────────────
ARCHIVE_NOTE="not included"
if [ "$WITH_ARCHIVE" = "1" ]; then
    ADIR="${SENTINEL_ARCHIVE_DIR:-}"
    if [ -z "$ADIR" ]; then
        ADIR=$(docker inspect sentinel-backend \
            --format '{{range .Mounts}}{{if eq .Destination "/data/archive"}}{{.Source}}{{end}}{{end}}' 2>/dev/null || true)
    fi
    [ -n "$ADIR" ] && [ -d "$ADIR" ] || die "cannot locate the image archive (set SENTINEL_ARCHIVE_DIR)"
    sz=$(du -sh "$ADIR" 2>/dev/null | cut -f1)
    log "archiving $ADIR ($sz) — for anything over a few GB, prefer the rsync command in README.txt"
    tar -cf "$BUNDLE/archive.tar" -C "$ADIR" . || die "archive tar failed"
    ARCHIVE_NOTE="archive.tar ($sz uncompressed; already-compressed PNGs, so not gzipped)"
fi

# ── Manifest ─────────────────────────────────────────────────────────────────
SENTINEL_EXPORT_COUNTS="$counts" python3 - "$BUNDLE" "$SHA" "$BYTES" "$OLDEST" "$NEWEST" "$PGVER" "$ARCHIVE_NOTE" <<'PY'
import json, sys, subprocess, datetime, os
bundle, sha, size, oldest, newest, pgver, archive = sys.argv[1:8]
counts = json.loads(os.environ["SENTINEL_EXPORT_COUNTS"])
try:
    ver = subprocess.check_output(["git", "describe", "--tags", "--always"],
                                  stderr=subprocess.DEVNULL, text=True).strip()
except Exception:
    ver = "unknown"
json.dump({
    "exported_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    "sentinel_version": ver,
    "postgres_version": pgver,
    "database": {"sha256": sha, "bytes": int(size), "file": "database.sql.gz"},
    "row_counts": counts,
    "check_runs_range": {"oldest": oldest, "newest": newest},
    "image_archive": archive,
}, open(os.path.join(bundle, "manifest.json"), "w"), indent=2)
PY

cat > "$BUNDLE/README.txt" <<README
Sentinel data export — $stamp

Import into a fresh deployment:

    bash ops/import-data.sh $(basename "$BUNDLE")

import-data.sh refuses to overwrite a database that already has data unless
you pass --force, restores the dump, then re-counts every table and compares
against manifest.json. A transfer that lost rows is caught there rather than
becoming a smaller dataset that looks fine.

Verify the transfer arrived intact before importing:

    sha256sum -c <<< "$SHA  database.sql.gz"

Image archive: $ARCHIVE_NOTE

The archive is content-addressed (files are named by the sha256 of their own
bytes), so rsync is the right tool: transfers resume, and files already
present are skipped. From the source host:

    rsync -av --partial --info=progress2 <source-archive-dir>/ \\
        user@newhost:/path/to/archive/

It is also optional. Sentinel runs without it; you lose the ability to view
images behind past verdicts, not any current monitoring.
README

log "manifest: $(python3 -c "import json;d=json.load(open('$BUNDLE/manifest.json'));print(sum(d['row_counts'].values()),'rows across',len(d['row_counts']),'tables')")"
log "done — $BUNDLE"
echo
echo "  Transfer it:  rsync -av --partial $BUNDLE/ user@newhost:/tmp/$(basename "$BUNDLE")/"
echo "  Then import:  bash ops/import-data.sh /tmp/$(basename "$BUNDLE")"
