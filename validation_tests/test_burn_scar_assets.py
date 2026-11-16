#!/usr/bin/env python3
"""Every profile that offers the burn-scar layer ships a file the map can draw.

Run directly (no DB, no network):

    python validation_tests/test_burn_scar_assets.py

Three things can go wrong here and none of them raises at run time.

A profile can list `burn_scars` in MAP_OVERLAYS with no matching asset. The
map then offers a toggle that fetches a 404, swallows it and draws nothing,
which looks like a region with no fires.

A regenerated file can lose the field the layer reads. Age drives both the
filter and the color ramp, and a feature with no ALARM_DATE is dropped by the
filter without a word. A whole file of them is an empty overlay that still
reports as on.

A regenerated file can balloon. Nothing downstream errors on a 5 MB overlay,
it just stalls the map on a phone. The size ceiling here is the only thing
watching, since the fetch script's own ceiling does not protect a file
somebody wrote by hand.
"""

from __future__ import annotations

import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "frontend" / "static" / "data"
CONFIG = ROOT / "backend" / "config.py"
XQPI = ROOT / "backend" / "profiles" / "xqpi.py"
SITE_TS = ROOT / "frontend" / "src" / "lib" / "site.ts"

OVERLAY = "burn_scars"

# Matches the generator's ceiling. Raw bytes, not gzipped: the browser gets
# the compressed form, but a raw blowup is what signals a bad regeneration.
MAX_RAW_BYTES = 1_500_000

# The age slider tops out here, so a file reaching back less than this leaves
# the slider pointing at years it has no data for.
SLIDER_MAX_YEARS = 5

failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    """Record one assertion and print its result.

    Args:
        label: What was checked.
        condition: Whether it held.
        detail: Extra context, shown on both pass and fail.
    """
    print(f"  {'ok  ' if condition else 'FAIL'}  {label}{f'  - {detail}' if detail else ''}")
    if not condition:
        failures.append(label)


def overlays_in(path: Path) -> list[str]:
    """Read a profile's MAP_OVERLAYS tuple out of its source.

    This parses source rather than importing, so it runs with no venv and no
    SENTINEL_* environment set. The rest of validation_tests assumes that.

    Args:
        path: A config or profile module.

    Returns:
        The overlay names, or an empty list if the file declares none.

    Raises:
        ValueError: If the file has no MAP_OVERLAYS assignment at all, which
            means this test is reading the wrong file.
    """
    text = path.read_text(encoding="utf-8")
    match = re.search(r"^MAP_OVERLAYS:[^=]*=\s*\(([^)]*)\)", text, re.MULTILINE)
    if match is None:
        raise ValueError(f"{path.name} has no MAP_OVERLAYS assignment")
    return re.findall(r"[\"']([a-z_]+)[\"']", match.group(1))


def load_collection(path: Path) -> list[dict[str, object]]:
    """Read one overlay file and return its features.

    Args:
        path: The GeoJSON asset.

    Returns:
        The feature list.

    Raises:
        TypeError: If the file is not a GeoJSON FeatureCollection.
    """
    body = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(body, dict) or body.get("type") != "FeatureCollection":
        raise TypeError(f"{path.name} is not a FeatureCollection")
    features = body.get("features")
    if not isinstance(features, list):
        raise TypeError(f"{path.name} has no feature list")
    return [f for f in features if isinstance(f, dict)]


def check_features(name: str, features: list[dict[str, object]]) -> None:
    """Check that every feature carries what the layer reads.

    Args:
        name: File name, for the assertion labels.
        features: Features from the asset.
    """
    missing_date = 0
    missing_name = 0
    bad_geometry = 0
    for feature in features:
        props = feature.get("properties")
        props = props if isinstance(props, dict) else {}
        alarm = props.get("ALARM_DATE")
        if not isinstance(alarm, (int, float)) or isinstance(alarm, bool) or alarm <= 0:
            missing_date += 1
        if not isinstance(props.get("FIRE_NAME"), str) or not props.get("FIRE_NAME"):
            missing_name += 1
        geometry = feature.get("geometry")
        if not isinstance(geometry, dict) or geometry.get("type") not in (
            "Polygon",
            "MultiPolygon",
        ):
            bad_geometry += 1
    check(
        f"{name}: every feature has an ALARM_DATE", missing_date == 0, f"{missing_date} without one"
    )
    check(
        f"{name}: every feature has a FIRE_NAME", missing_name == 0, f"{missing_name} without one"
    )
    check(f"{name}: every geometry is a polygon", bad_geometry == 0, f"{bad_geometry} other")


def check_coverage(name: str, features: list[dict[str, object]]) -> None:
    """Check the file reaches back as far as the slider goes.

    Args:
        name: File name, for the assertion labels.
        features: Features from the asset.
    """
    stamps = [
        float(p["ALARM_DATE"])
        for f in features
        if isinstance(p := f.get("properties"), dict)
        and isinstance(p.get("ALARM_DATE"), (int, float))
    ]
    if not stamps:
        check(f"{name}: has a usable date range", False, "no dates at all")
        return
    now = datetime.now(tz=UTC)
    oldest = datetime.fromtimestamp(min(stamps) / 1000.0, tz=UTC)
    newest = datetime.fromtimestamp(max(stamps) / 1000.0, tz=UTC)
    span_years = (now - oldest).days / 365.25
    check(
        f"{name}: reaches back at least {SLIDER_MAX_YEARS} years",
        span_years >= SLIDER_MAX_YEARS,
        f"{span_years:.1f} yr to {oldest.date()}",
    )
    # Not an assertion. CAL FIRE publishes a season the spring after it ends,
    # so a gap here is expected and the panel shows this date rather than
    # letting an empty recent window read as "no fires".
    print(f"        newest ignition {newest.date()}")


def main() -> int:
    """Check each profile's declared overlays against the shipped files.

    Returns:
        0 if every assertion held, 1 otherwise.
    """
    print("the overlay name is known to both sides:")
    site_text = SITE_TS.read_text(encoding="utf-8")
    check("site.ts ALL_OVERLAYS lists burn_scars", f"'{OVERLAY}'" in site_text)

    profiles = {"aqpi": CONFIG, "xqpi": XQPI}
    for profile, path in sorted(profiles.items()):
        declared = overlays_in(path)
        if OVERLAY not in declared:
            print(f"\n{profile}: does not offer burn scars, nothing to ship")
            continue
        print(f"\n{profile}: offers burn scars, so the asset must exist:")
        asset = DATA_DIR / f"burn-scars-{profile}.json"
        if not asset.is_file():
            check(f"{asset.name} exists", False, "missing")
            continue
        size = asset.stat().st_size
        check(f"{asset.name} exists", True, f"{size / 1024:.0f} KB")
        check(
            f"{asset.name} is under the size ceiling",
            size <= MAX_RAW_BYTES,
            f"{size} vs {MAX_RAW_BYTES}",
        )
        features = load_collection(asset)
        check(f"{asset.name} has perimeters", len(features) > 0, f"{len(features)}")
        check_features(asset.name, features)
        check_coverage(asset.name, features)

    print(
        f"\n{len(failures)} FAILED: {', '.join(failures)}"
        if failures
        else "\nall burn-scar asset assertions passed"
    )
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
