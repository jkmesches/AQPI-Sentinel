"""Rendered moment frames read off the published tree.

Run directly (no DB, no network, no mount):

    python validation_tests/test_fs_moment_frames.py

Why this exists: the map fetched every radar frame from radarca, for bytes
already sitting on a mount Sentinel reads for every other check. Serving them
locally means resolving a path, and a path resolved wrongly fails in the two
ways that are hardest to see. A pattern that is too loose serves the
colorbar each directory carries as though it were a frame, which paints a
legend over the radar. A pattern that is too tight silently returns nothing,
which is indistinguishable from a radar that has stopped.

So the tree below is built to the shapes measured on the real mounts on
2026-11-20 rather than to a convenient fiction:

  AQPI  PRODUCTS/Chivo/plots/<folder>/%Y/%m/%d/<folder>_<prefix>_YYYYMMDD-HHMM.png
  XQPI  PRODUCT_IMAGES/%Y/%m/%d/<radar>/<moment>/<radar>_<moment>_3.5_
        YYYYMMDD-HHMMSS.png

Both carry one <prefix>_colorbar.png per directory, and the AQPI tree holds
five X-band folders without RhoHV plus sscb with it.
"""
from __future__ import annotations
import asyncio
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

FAILURES: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if cond else 'FAIL'}  {label}{'  — ' + detail if detail else ''}")
    if not cond:
        FAILURES.append(label)


def build_aqpi_tree(root: str, day: datetime) -> None:
    """One UTC day of ebay and sscb frames, with their colorbars.

    Args:
        root: Directory standing in for SENTINEL_BACKEND_ROOT.
        day: The UTC day to write under; the time of day is used for the
            frame timestamps.
    """
    for folder, prefixes in (("ebay", ("CorrReflectivity", "Velocity")),
                             ("sscb", ("CorrReflectivity", "PhiDP", "RhoHV"))):
        d = os.path.join(root, "PRODUCTS", "Chivo", "plots", folder,
                         day.strftime("%Y/%m/%d"))
        os.makedirs(d, exist_ok=True)
        for prefix in prefixes:
            Path(d, f"{folder}_{prefix}_colorbar.png").write_bytes(b"legend")
            for minute in (0, 3, 6):
                ts = (day + timedelta(minutes=minute)).strftime("%Y%m%d-%H%M")
                Path(d, f"{folder}_{prefix}_{ts}.png").write_bytes(b"\x89PNG\r\n\x1a\n")


def build_xqpi_tree(root: str, day: datetime) -> None:
    """One UTC day of FLOW frames under per-moment directories.

    Args:
        root: Directory standing in for SENTINEL_BACKEND_ROOT.
        day: The UTC day to write under; the time of day is used for the
            frame timestamps.
    """
    for moment in ("Reflectivity", "DifferentialPhase"):
        d = os.path.join(root, "PRODUCT_IMAGES", day.strftime("%Y/%m/%d"),
                         "FLOW", moment)
        os.makedirs(d, exist_ok=True)
        Path(d, f"FLOW_{moment}_colorbar.png").write_bytes(b"legend")
        for second in (0, 150, 300):
            ts = (day + timedelta(seconds=second)).strftime("%Y%m%d-%H%M%S")
            Path(d, f"FLOW_{moment}_3.5_{ts}.png").write_bytes(b"\x89PNG\r\n\x1a\n")


def run_profile(profile: str, root: str) -> None:
    """Import the backend fresh under one profile and exercise the reader.

    Args:
        profile: SENTINEL_PROFILE value, "aqpi" or "xqpi".
        root: Empty directory to build that profile's tree under.
    """
    for name in [m for m in sys.modules if m.startswith("backend")]:
        del sys.modules[name]
    os.environ["SENTINEL_PROFILE"] = profile
    os.environ["SENTINEL_BACKEND_ROOT"] = root
    import backend.fs_products as fs

    # Near now, not a fixed hour: the listing keeps only the last hour, so
    # frames written at 12:00 would be filtered out at any other time of day.
    day = datetime.now(timezone.utc).replace(second=0, microsecond=0) - timedelta(minutes=10)
    radar, moment = ("XEBY", "Reflectivity") if profile == "aqpi" else ("FLOW", "Reflectivity")
    if profile == "aqpi":
        build_aqpi_tree(root, day)
    else:
        build_xqpi_tree(root, day)

    frames = asyncio.run(fs.list_moment_frames(radar, moment))
    check(f"{profile}: three frames found", len(frames) == 3, str(len(frames)))
    check(f"{profile}: the colorbar is not a frame",
          all("colorbar" not in n for _, n in frames),
          ", ".join(n for _, n in frames))
    check(f"{profile}: frames come back oldest first",
          [t for t, _ in frames] == sorted(t for t, _ in frames))
    check(f"{profile}: timestamps carry a zone",
          all(t.tzinfo is not None for t, _ in frames))

    newest_ts, newest = frames[-1]
    resp = asyncio.run(fs.read_moment_image(radar, moment, newest, newest_ts))
    check(f"{profile}: the newest frame reads back", resp.status_code == 200,
          f"HTTP {resp.status_code}")

    # A name that is not a frame must not reach the filesystem at all.
    for bad in ("../../../etc/passwd", "ebay_CorrReflectivity_colorbar.png",
                "FLOW_Reflectivity_colorbar.png", "", "nonsense.png"):
        bad_resp = asyncio.run(fs.read_moment_image(radar, moment, bad, newest_ts))
        check(f"{profile}: {bad!r} is refused", bad_resp.status_code == 404,
              f"HTTP {bad_resp.status_code}")

    # A day with no directory is normal rather than an error.
    empty = asyncio.run(fs.list_moment_frames(radar, moment, days=1))
    check(f"{profile}: a scan that finds a directory still works", len(empty) == 3)

    try:
        asyncio.run(fs.list_moment_frames(radar, moment, days=0))
        check(f"{profile}: days=0 is refused", False, "no error raised")
    except ValueError:
        check(f"{profile}: days=0 is refused", True)

    # The tree holds whole days where radarca served a rolling hour. Handing
    # the map everything on disk stretched its scrubber across two days.
    old = asyncio.run(fs.list_moment_frames(radar, moment, window_s=60))
    check(f"{profile}: the window drops frames older than it",
          len(old) < len(frames), f"{len(old)} of {len(frames)}")
    everything = asyncio.run(fs.list_moment_frames(radar, moment, window_s=0))
    check(f"{profile}: window_s=0 keeps everything the scan found",
          len(everything) == len(frames), f"{len(everything)} of {len(frames)}")


def main() -> int:
    for profile in ("aqpi", "xqpi"):
        print(f"--- {profile} ---")
        with tempfile.TemporaryDirectory() as root:
            run_profile(profile, root)
    print(f"\n{len(FAILURES)} FAILED: {', '.join(FAILURES)}" if FAILURES
          else "\nall fs moment-frame assertions passed")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    raise SystemExit(main())
