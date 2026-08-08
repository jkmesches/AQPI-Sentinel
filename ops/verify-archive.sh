#!/bin/bash
# Verify a copy of the image archive before trusting or deleting the original.
#
# Used for the 2026-08-08 migration of the 18GB archive onto the NFS share,
# and reusable for any future relocation or restore-from-backup.
#
#   SRC=/var/lib/docker/volumes/sentinel_archive/_data \
#   DST=/mnt/aqpi-data/archive ops/verify-archive.sh
#
# Scope: real archive blobs only. *.tmp files are EXCLUDED. Those are orphaned
# temp files from writes that died mid-flight — the migration found 10,726 of
# them, all but one zero-byte, dating from the disk-full window; the single
# non-zero one was a 32KB truncated write stamped 2026-08-01 23:26, half an
# hour before collection stopped. They are not archive data: save_image()
# writes its image_archive row only after os.replace() promotes the tmp to its
# final name. Including them fails gate 3 against sha256("") by construction.
#
# Three gates, increasing in strength. Exits non-zero on any failure; do not
# delete the source unless this exits 0.
#
#   1. COMPLETENESS — sorted filename SETS, not counts. A copy that lost one
#      file and gained another cannot pass.
#   2. SIZE — total bytes. Catches truncated transfers that still produced a
#      correctly-named file.
#   3. INTEGRITY — a random sample on the destination is re-hashed and checked
#      against its own filename. The store is content-addressed
#      (<sha[:2]>/<sha>.png), so filename == sha256(contents) is self-proving:
#      it needs no reference to the source at all.
#
# Arithmetic note: sum bytes with awk '%.0f', NOT '%d'. A 17.8GB total
# overflows 32-bit and clamps to 2147483647, which silently makes gate 2 pass
# by comparing INT_MAX to itself. Doubles carry 53 bits of mantissa, which is
# exact well past any plausible archive size. This bit us during the migration.
set -uo pipefail

SRC="${SRC:-/var/lib/docker/volumes/sentinel_archive/_data}"
DST="${DST:-/mnt/aqpi-data/archive}"
SAMPLE="${SAMPLE:-500}"

for d in "$SRC" "$DST"; do
    [ -d "$d" ] || { echo "ABORT: $d is not a directory"; exit 2; }
done

WORK=$(mktemp -d); trap 'rm -rf "$WORK"' EXIT
fail=0

_blobs() { ( cd "$1" && find . -type f ! -name '*.tmp' -printf '%P\n' | sort ); }
_bytes() { ( cd "$1" && find . -type f ! -name '*.tmp' -printf '%s\n' \
             | awk '{t+=$1} END{printf "%.0f", t+0}' ); }

echo "=== 1. completeness ==="
_blobs "$SRC" > "$WORK/src.txt"
_blobs "$DST" > "$WORK/dst.txt"
echo "  src : $(wc -l < "$WORK/src.txt") blobs"
echo "  dst : $(wc -l < "$WORK/dst.txt") blobs"
missing=$(comm -23 "$WORK/src.txt" "$WORK/dst.txt" | wc -l)
echo "  missing at dst : $missing"
echo "  extra at dst   : $(comm -13 "$WORK/src.txt" "$WORK/dst.txt" | wc -l)  (expected >0 if the app is live and writing to dst)"
if [ "$missing" -ne 0 ]; then
    echo "  FAIL — absent from dst:"; comm -23 "$WORK/src.txt" "$WORK/dst.txt" | head -10 | sed 's/^/    /'
    fail=1
else
    echo "  PASS — every source blob exists at dst"
fi

echo "=== 2. total size ==="
s=$(_bytes "$SRC"); d=$(_bytes "$DST")
echo "  src : $s bytes"
echo "  dst : $d bytes"
if awk -v a="$s" -v b="$d" 'BEGIN{exit !(b+0 >= a+0)}'; then
    awk -v a="$s" -v b="$d" 'BEGIN{printf "  PASS — dst >= src (dst larger by %.0f bytes)\n", b-a}'
else
    awk -v a="$s" -v b="$d" 'BEGIN{printf "  FAIL — dst SHORT by %.0f bytes\n", a-b}'; fail=1
fi

echo "=== 3. integrity: re-hash $SAMPLE random blobs at dst ==="
bad=0; checked=0
while read -r rel; do
    expect="${rel##*/}"; expect="${expect%%.*}"
    actual=$(sha256sum "$DST/$rel" 2>/dev/null | cut -d' ' -f1)
    checked=$((checked+1))
    [ "$expect" = "$actual" ] || { echo "  MISMATCH $rel"; echo "    name: $expect"; echo "    hash: $actual"; bad=$((bad+1)); }
done < <(shuf -n "$SAMPLE" "$WORK/dst.txt")
echo "  checked $checked, $bad mismatches"
if [ "$bad" -ne 0 ]; then echo "  FAIL — content does not match content-addressed name"; fail=1
else echo "  PASS — every sampled blob hashes to its own name"; fi

echo
[ "$fail" -eq 0 ] && echo "VERIFY OK — source may be removed" || echo "VERIFY FAILED — remove nothing"
exit $fail
