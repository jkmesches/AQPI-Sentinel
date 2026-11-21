#!/usr/bin/env python3
"""Only a published frame may set a backend product's freshness.

LB1's `newest_mtime` basis takes the newest entry in a product's images
directory. Every entry carrying a fresh mtime therefore sets the product's
age, so a lock file, an editor backup or a half-written temp file reports a
stale product as current. That is the dangerous direction: a stale FAIL gets
investigated, a stale PASS is believed, and this tier exists to notice when a
publisher stops.

The filter is positive rather than a list of junk suffixes, so an entry a
future tool invents is ignored instead of counted. The frame names below are
the ones the live tree publishes, read from the main archive on 2026-11-21, including
`qpe_15m_step_COMP_20261121_033600_rainfall.nc.png`, which a filter that cut
at the first dot would reject.

Run:  python3 validation_tests/test_lb1_frame_filter.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from backend.checks.layer1_backend_product import (  # noqa: E402
    FRAME_SUFFIXES, _is_frame, _scan)

# How much older the real frames are than the junk entry, in seconds. Three
# hours, the staleness seen on the mount on 2026-11-21.
STALE_AGE_S = 178 * 60

# Entries measured in the live product tree that are not frames. The .lock and
# .tmp leftovers sit beside the manifests one level above the images
# directories, so they are what would appear if a publisher ever wrote one
# alongside the frames.
NOT_FRAMES = ("details_in.json", "details_in.json.lock",
              "details_in.json.868453.tmp", "20261121_1600.png.tmp",
              "20261121_1600.png~", "20261121_1600.png.1", ".hidden",
              "images.tar.gz")

# Frame names the live tree publishes, plus an uppercase extension.
FRAMES = ("20261121_1600.png", "C_hrrr_accum_step17.png",
          "qpe_15m_step_COMP_20261121_033600_rainfall.nc.png",
          "qpe_pr_step_COMP_20261121_033600_rainfall.nc.PNG")


def write(path: Path, name: str, age_s: float) -> None:
    """Create a file with an mtime in the past.

    Args:
        path: The directory.
        name: The file's name.
        age_s: How long ago it was written.
    """
    f = path / name
    f.write_bytes(b"x")
    when = time.time() - age_s
    os.utime(f, (when, when))


def check_names(fails: list[str]) -> None:
    """The filter admits real frame names and nothing else.

    Args:
        fails: Collects failure messages.
    """
    for name in FRAMES:
        if not _is_frame(name):
            fails.append(f"{name!r} is a published frame and was rejected")
    for name in NOT_FRAMES:
        if _is_frame(name):
            fails.append(f"{name!r} is not a frame and was admitted; a fresh "
                         f"mtime on it would report a stale product as current")
    if ".png" not in FRAME_SUFFIXES:
        fails.append("the extension every live product publishes is not listed")


def check_junk_does_not_refresh(fails: list[str]) -> None:
    """A fresh non-frame entry leaves the product's age where it was.

    Args:
        fails: Collects failure messages.
    """
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        write(d, "20261121_1300.png", STALE_AGE_S)
        write(d, "20261121_1400.png", STALE_AGE_S)
        quiet = _scan(str(d))
        if quiet["newest_name"] != "20261121_1400.png":
            fails.append(f"with frames only: {quiet['newest_name']!r}")
        quiet_age = time.time() - quiet["newest_mtime"]

        for junk in ("20261121_1600.png.tmp", "details_in.json.lock",
                     "20261121_1400.png~"):
            write(d, junk, 0)
        got = _scan(str(d))
        if got["newest_name"] != "20261121_1400.png":
            fails.append(
                f"{got['newest_name']!r} set the freshness; a stale product "
                f"now reads current")
        age = time.time() - got["newest_mtime"]
        if age < STALE_AGE_S - 60:
            fails.append(
                f"freshness fell to {age / 60:.1f} minutes, from "
                f"{quiet_age / 60:.1f}; a non-frame entry was counted")
        if got.get("non_frames") != 3:
            fails.append(f"non_frames is {got.get('non_frames')!r}, expected "
                         f"3; the metric has to say they were there")
        if got["count"] != 2:
            fails.append(f"count is {got['count']}, expected 2; only frames "
                         f"are published frames")


def check_rename_lands(fails: list[str]) -> None:
    """A frame counts as soon as it has a frame's name.

    Args:
        fails: Collects failure messages.
    """
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        write(d, "20261121_1400.png", STALE_AGE_S)
        write(d, "20261121_1600.png.tmp", 0)
        (d / "20261121_1600.png.tmp").rename(d / "20261121_1600.png")
        got = _scan(str(d))
        if got["newest_name"] != "20261121_1600.png":
            fails.append(f"after the rename the newest is "
                         f"{got['newest_name']!r}; a finished frame must count")
        if got.get("non_frames") != 0:
            fails.append(f"non_frames is {got.get('non_frames')!r} after the "
                         f"rename, expected 0")


def check_only_junk(fails: list[str]) -> None:
    """A directory holding no frame reports no freshness at all.

    Args:
        fails: Collects failure messages.
    """
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        write(d, "20261121_1600.png.tmp", 0)
        write(d, "details_in.json", 0)
        got = _scan(str(d))
        if got["newest_mtime"] != 0.0:
            fails.append(
                "a directory with no frame reported a freshness; it has "
                "published nothing, which A_present must fail on")
        if got["count"] != 0:
            fails.append(f"count is {got['count']}, expected 0")
        if got.get("non_frames") != 2:
            fails.append(f"non_frames is {got.get('non_frames')!r}, expected 2")


def main() -> int:
    """Run the checks and report.

    Returns:
        Process exit status.
    """
    fails: list[str] = []
    check_names(fails)
    check_junk_does_not_refresh(fails)
    check_rename_lands(fails)
    check_only_junk(fails)
    if fails:
        print("FAILED:")
        for f in fails:
            print(f"  {f}")
        return 1
    print("ok: only a published frame sets freshness, the live tree's frame "
          "names all pass, and a directory of junk reports nothing published")
    return 0


if __name__ == "__main__":
    sys.exit(main())
