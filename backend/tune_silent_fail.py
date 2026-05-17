"""Sample each radar's inter-scan gaps and suggest new RADAR_SILENT_FAIL_S
thresholds.

Cadences drift — radars get reconfigured, replaced, taken offline for
maintenance, etc. The GHOST_UP thresholds in `config.RADAR_SILENT_FAIL_S`
should be revisited periodically (monthly is a reasonable cadence) so
that "currently silent" detection stays tight without false-firing on
normal operation.

Usage:
    python -m backend.tune_silent_fail              # human-readable table
    python -m backend.tune_silent_fail --emit       # also print a paste-ready
                                                    # RADAR_SILENT_FAIL_S dict

Methodology: take 1.5× each radar's observed worst-case inter-scan gap,
round up to the nearest minute, and floor at 4 min (so a tightly-paced
radar's threshold doesn't fire on a single missed scan). Adjust the
multiplier in BUFFER_MULT below if the fleet's typical jitter changes.

Output is informational only — this script never writes to config.py.
Copy the suggested dict into backend/config.py manually after reviewing.
"""
from __future__ import annotations
import argparse
import math
import re
import ssl
import sys
import urllib.request
import urllib.error
import json
from datetime import datetime, timezone
from pathlib import Path

# Load .env first so SETTINGS picks up the right base URL.
from dotenv import load_dotenv
_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_ROOT / ".env")

from backend.config import RADAR_FOLDER, RADAR_SILENT_FAIL_S, SETTINGS  # noqa: E402

# Buffer factor applied to each radar's observed max gap. 1.5 means
# "tolerate 50% jitter beyond worst observed." Raise to reduce false
# alarms; lower to be more aware than upstream.
BUFFER_MULT = 1.5

# Minimum threshold floor. A radar with a perfect 2-min cadence has
# max_gap=120s and would otherwise get a 180s threshold — too tight, a
# single missed scan would fire.
MIN_THRESHOLD_S = 240

# Filename → timestamp patterns mirrored from backend/checks/helpers.py.
_PATTERNS = (
    re.compile(r"_(\d{8}-\d{4})\.png$"),     # X-band + CBAND
    re.compile(r"^(\d{8}_\d{4})\.png$"),     # mosaic-style fallback
)


_ctx = ssl.create_default_context()
_ctx.check_hostname = False
_ctx.verify_mode = ssl.CERT_NONE


def parse_ts(name: str) -> datetime | None:
    for rx in _PATTERNS:
        m = rx.search(name)
        if m:
            fmt = "%Y%m%d-%H%M" if "-" in m.group(1) else "%Y%m%d_%H%M"
            try:
                return datetime.strptime(m.group(1), fmt).replace(tzinfo=timezone.utc)
            except ValueError:
                continue
    return None


def fetch(folder: str) -> list[str]:
    url = (f"{SETTINGS.base}/api/xbandRadarImages/"
           f"?radarFolder={folder}&productPrefix=CorrReflectivity")
    req = urllib.request.Request(url, headers={"accept": "application/json"})
    with urllib.request.urlopen(req, context=_ctx, timeout=10) as r:
        return json.load(r).get("images", []) or []


def recommend(max_gap_s: float) -> int:
    raw = max_gap_s * BUFFER_MULT
    raw = max(raw, MIN_THRESHOLD_S)
    return int(math.ceil(raw / 60.0) * 60)


def main(emit: bool) -> None:
    now = datetime.now(timezone.utc)
    print(f"\nRadar cadence sample @ {now.isoformat(timespec='seconds')}")
    print(f"Source: {SETTINGS.base}/api/xbandRadarImages/")
    print(f"Buffer: {BUFFER_MULT}x max observed gap, floor {MIN_THRESHOLD_S}s\n")

    hdr = f"{'radar':6s}  {'n':>3s}  {'min':>5s}  {'p50':>5s}  {'p90':>5s}  {'max':>5s}  {'newest_age':>10s}  {'now':>5s}  {'rec':>5s}"
    print(hdr)
    print("-" * len(hdr))

    suggestions: dict[str, int] = {}
    for radar_id, folder in RADAR_FOLDER.items():
        try:
            imgs = fetch(folder)
        except (urllib.error.URLError, urllib.error.HTTPError) as e:
            print(f"{radar_id:6s}  ERR  {e}")
            continue
        ts_list = sorted(t for t in (parse_ts(n) for n in imgs) if t is not None)
        if len(ts_list) < 2:
            current = RADAR_SILENT_FAIL_S.get(radar_id, 600)
            print(f"{radar_id:6s}  {len(ts_list):>3d}  (no scans — current threshold {current}s kept)")
            suggestions[radar_id] = current
            continue
        gaps = sorted((ts_list[i + 1] - ts_list[i]).total_seconds()
                      for i in range(len(ts_list) - 1))
        n = len(gaps)
        mn = gaps[0]
        p50 = gaps[n // 2]
        p90 = gaps[min(n - 1, int(n * 0.90))]
        mx = gaps[-1]
        age = (now - ts_list[-1]).total_seconds()
        current = RADAR_SILENT_FAIL_S.get(radar_id, 600)
        rec = recommend(mx)
        diff = ""
        if rec != current:
            diff = f"  (was {current}s)"
        print(f"{radar_id:6s}  {len(ts_list):>3d}  "
              f"{int(mn):>5d}  {int(p50):>5d}  {int(p90):>5d}  {int(mx):>5d}  "
              f"{int(age):>10d}  {current:>5d}  {rec:>5d}{diff}")
        suggestions[radar_id] = rec

    if emit:
        print("\n# Paste into backend/config.py:")
        print("RADAR_SILENT_FAIL_S = {")
        for k, v in suggestions.items():
            print(f"    {k!r:8s}: {v},")
        print("}")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--emit", action="store_true",
                   help="also print a paste-ready RADAR_SILENT_FAIL_S dict")
    args = p.parse_args()
    try:
        main(args.emit)
    except KeyboardInterrupt:
        sys.exit(130)
