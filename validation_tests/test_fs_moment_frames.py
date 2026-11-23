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
  NEXRAD (AQPI only) PRODUCTS/Chivo/nexrad/%Y/%m/%d/<site>/<moment>/
        <site>_<moment>_0.5_YYYYMMDD-HHMMSS.png

All three carry one colorbar per directory, and the AQPI tree holds five
X-band folders without RhoHV plus sscb with it. The NEXRAD plotter also
leaves <frame>.png.<pid>.tmp.png while it renders.
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


def build_nexrad_tree(root: str, day: datetime) -> list[str]:
    """Three KDAX frames in the NEXRAD plotter's layout, plus its debris.

    The layout is the XQPI plotter's, under PRODUCTS/Chivo/nexrad, as read
    on the AQPI deployment's mount on 2026-11-23: a moment directory, the
    K-prefixed site, the elevation in the name and seconds in the stamp. A
    render in progress is named <frame>.png.<pid>.tmp.png, which still ends
    in .png.

    Args:
        root: Directory standing in for SENTINEL_BACKEND_ROOT.
        day: The UTC instant the frames are stamped from.

    Returns:
        The three frame names, oldest first.
    """
    d = os.path.join(root, "PRODUCTS", "Chivo", "nexrad", day.strftime("%Y/%m/%d"),
                     "KDAX", "Reflectivity")
    os.makedirs(d, exist_ok=True)
    Path(d, "KDAX_Reflectivity_colorbar.png").write_bytes(b"legend")
    names: list[str] = []
    for second in (0, 320, 640):
        ts = (day + timedelta(seconds=second)).strftime("%Y%m%d-%H%M%S")
        names.append(f"KDAX_Reflectivity_0.5_{ts}.png")
        Path(d, names[-1]).write_bytes(b"\x89PNG\r\n\x1a\n")
    Path(d, f"{names[-1]}.4242.tmp.png").write_bytes(b"\x89PNG half")
    # The July frames under Chivo/plots, which the old root job wrote with
    # another name, must not be read as this site's.
    old = os.path.join(root, "PRODUCTS", "Chivo", "plots", "DAX", day.strftime("%Y/%m/%d"))
    os.makedirs(old, exist_ok=True)
    Path(old, f"DAX_reflectivity_{day.strftime('%Y%m%d-%H%M')}.png").write_bytes(b"\x89PNG")
    return names


def run_nexrad(root: str) -> None:
    """The AQPI profile reads NEXRAD frames from their own tree.

    Args:
        root: Empty directory to build the tree under.
    """
    for name in [m for m in sys.modules if m.startswith("backend")]:
        del sys.modules[name]
    os.environ["SENTINEL_PROFILE"] = "aqpi"
    os.environ["SENTINEL_BACKEND_ROOT"] = root
    import backend.fs_products as fs
    from backend import config

    day = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(minutes=15)
    names = build_nexrad_tree(root, day)
    check("nexrad: KDAX is a scan radar", fs.is_scan_radar("KDAX"))
    check("nexrad: ...filed under its own id", fs.plot_folder("KDAX") == "KDAX")
    check("nexrad: ...and kept out of RADAR_FOLDER, which the checks iterate",
          "KDAX" not in config.RADAR_FOLDER)
    frames = asyncio.run(fs.list_moment_frames("KDAX", "Reflectivity"))
    got = [n for _, n in frames]
    check("nexrad: the three frames are found, oldest first", got == names, str(got))
    check("nexrad: a half-written .tmp.png is not a frame",
          all(".tmp" not in n for n in got))
    check("nexrad: the stamp keeps its seconds",
          frames[1][0] - frames[0][0] == timedelta(seconds=320),
          str(frames[1][0] - frames[0][0]))
    resp = asyncio.run(fs.read_moment_image("KDAX", "Reflectivity", names[-1], frames[-1][0]))
    check("nexrad: the newest frame reads back", resp.status_code == 200,
          f"HTTP {resp.status_code}")
    for bad in (f"{names[-1]}.4242.tmp.png", "KDAX_Reflectivity_colorbar.png",
                f"DAX_reflectivity_{day.strftime('%Y%m%d-%H%M')}.png"):
        r = asyncio.run(fs.read_moment_image("KDAX", "Reflectivity", bad, frames[-1][0]))
        check(f"nexrad: {bad!r} is refused", r.status_code == 404, f"HTTP {r.status_code}")
    try:
        _ = asyncio.run(fs.list_moment_frames("KDAX", "Velocity"))
        check("nexrad: a moment the plotter does not draw is refused", False, "no error")
    except KeyError:
        check("nexrad: a moment the plotter does not draw is refused", True)
    check("nexrad: an unknown id is not a scan radar", not fs.is_scan_radar("KXYZ"))


def main() -> int:
    for profile in ("aqpi", "xqpi"):
        print(f"--- {profile} ---")
        with tempfile.TemporaryDirectory() as root:
            run_profile(profile, root)
    print("--- nexrad ---")
    with tempfile.TemporaryDirectory() as root:
        run_nexrad(root)
    print(f"\n{len(FAILURES)} FAILED: {', '.join(FAILURES)}" if FAILURES
          else "\nall fs moment-frame assertions passed")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    raise SystemExit(main())
