#!/usr/bin/env python3
"""Refresh the stream-gauge site list Sentinel ships.

Writes `backend/reference/stream_sites.csv`, which `/api/upstream/stream_gauges`
serves. Run from the repo root:

    python scripts/fetch_stream_sites.py

The two per-COMID series the gauge popup plots are rows of files on the main archive, under
PRODUCTS/National_Water_Model and PRODUCTS/StreamGauge_Obs. This list is not:
a search for its LatSite column across the main archive root and
the QPE staging root on 2026-11-21 found nothing, so radarca is the
only place it exists. It is 16.7 KB and its Last-Modified read 2026-01-09,
so Sentinel carries a copy rather than fetching it on every cold start.

Re-run when a site is added, which is roughly a yearly event. The script
refuses to overwrite a good file with a worse one, because the failure that
matters here is a truncated or error-page response being written over 468
working rows and nobody noticing until the markers disappear.
"""

from __future__ import annotations

import argparse
import csv
import io
import sys
from dataclasses import dataclass
from pathlib import Path

import requests

SOURCE_URL = "https://radarca.engr.colostate.edu/data/stream_data.csv"

# Where the backend reads it from. Kept out of any directory named `data`:
# .gitignore carries an unanchored `data/`, so a file under one is dropped at
# any depth and the image ships without it.
OUT_PATH = Path(__file__).resolve().parents[1] / "backend" / "reference" / "stream_sites.csv"

COLUMNS = ("COMID", "LatSite", "LonSite", "Status")

# What a usable list looks like. 468 rows on 2026-11-21. A response under
# this floor is truncated, since the gauge network does not shrink by a third.
MIN_ROWS = 300

# Northern California, with room to spare. A coordinate outside this box
# means the columns were read in the wrong order or the body is not the CSV.
LAT_RANGE = (30.0, 45.0)
LON_RANGE = (-126.0, -114.0)

TIMEOUT_S = 60


@dataclass(frozen=True)
class Checked:
    """What a candidate list was found to contain."""

    rows: int
    statuses: dict[str, int]
    lat_span: tuple[float, float]
    lon_span: tuple[float, float]


def inspect(text: str) -> Checked:
    """Read a candidate list and report what it holds.

    Args:
        text: The CSV as fetched.

    Returns:
        Its row count, status tally and coordinate spans.

    Raises:
        ValueError: If a column is missing, a coordinate is unreadable or out
            of range, or the list is too short to be the real one.
    """
    reader = csv.DictReader(io.StringIO(text))
    missing = [c for c in COLUMNS if c not in (reader.fieldnames or [])]
    if missing:
        raise ValueError(f"missing columns: {missing}; got {reader.fieldnames}")
    lats: list[float] = []
    lons: list[float] = []
    statuses: dict[str, int] = {}
    rows = 0
    for row in reader:
        rows += 1
        lat, lon = float(row["LatSite"]), float(row["LonSite"])
        if not LAT_RANGE[0] <= lat <= LAT_RANGE[1]:
            raise ValueError(f"latitude {lat} outside {LAT_RANGE} (COMID {row['COMID']})")
        if not LON_RANGE[0] <= lon <= LON_RANGE[1]:
            raise ValueError(f"longitude {lon} outside {LON_RANGE} (COMID {row['COMID']})")
        lats.append(lat)
        lons.append(lon)
        statuses[row["Status"]] = statuses.get(row["Status"], 0) + 1
    if rows < MIN_ROWS:
        raise ValueError(f"only {rows} rows, under the {MIN_ROWS} floor")
    return Checked(rows, statuses, (min(lats), max(lats)), (min(lons), max(lons)))


def fetch() -> tuple[str, str]:
    """Download the list.

    Returns:
        The body, and the Last-Modified header or an empty string.

    Raises:
        RuntimeError: If the request fails or answers with anything but 200.
    """
    try:
        r = requests.get(SOURCE_URL, timeout=TIMEOUT_S)
    except requests.RequestException as e:
        raise RuntimeError(f"{SOURCE_URL} unreachable: {e}") from e
    if r.status_code != 200:
        raise RuntimeError(f"{SOURCE_URL} answered HTTP {r.status_code}")
    return r.text, r.headers.get("Last-Modified", "")


def describe(label: str, c: Checked) -> None:
    """Print one list's contents on two lines.

    Args:
        label: Which list this is.
        c: What it was found to hold.
    """
    tally = ", ".join(f"{k}={v}" for k, v in sorted(c.statuses.items()))
    print(f"{label}: {c.rows} rows ({tally})")
    print(f"  lat {c.lat_span[0]:.4f}..{c.lat_span[1]:.4f}  "
          f"lon {c.lon_span[0]:.4f}..{c.lon_span[1]:.4f}")


def main() -> int:
    """Fetch, check and write the list.

    Returns:
        0 on success, 1 when the fetched list is unusable.
    """
    ap = argparse.ArgumentParser(description=__doc__)
    _ = ap.add_argument("--out", type=Path, default=OUT_PATH,
                        help="where to write it")
    _ = ap.add_argument("--force", action="store_true",
                        help="write even when the new list has fewer rows")
    args = ap.parse_args()

    try:
        text, modified = fetch()
        fresh = inspect(text)
    except (RuntimeError, ValueError) as e:
        print(f"refusing to write: {e}", file=sys.stderr)
        return 1
    describe("fetched", fresh)
    if modified:
        print(f"  Last-Modified: {modified}")

    out = Path(args.out)
    if out.exists():
        try:
            held = inspect(out.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            print(f"the file on disk does not read: {e}")
        else:
            describe("on disk", held)
            if fresh.rows < held.rows and not args.force:
                print(f"refusing to write: {fresh.rows} rows would replace "
                      f"{held.rows}; pass --force if that is intended",
                      file=sys.stderr)
                return 1

    out.parent.mkdir(parents=True, exist_ok=True)
    _ = out.write_text(text, encoding="utf-8")
    print(f"wrote {out} ({len(text)} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
