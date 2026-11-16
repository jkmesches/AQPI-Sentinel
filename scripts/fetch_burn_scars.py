#!/usr/bin/env python3
"""Build the per-profile burn-scar overlay from CAL FIRE's perimeter service.

Writes `frontend/static/data/burn-scars-<profile>.json`, which the map's
burn-scar layer fetches. Run from the repo root:

    python scripts/fetch_burn_scars.py --profile aqpi
    python scripts/fetch_burn_scars.py --profile xqpi

Re-run after CAL FIRE publishes a season, which happens each spring for the
year before. The service carries nothing from the season in progress, so a
file built today ends at the last published year; the panel shows that date
rather than letting an empty recent window read as "no fires".

Source is the FRAP historic layer, not the view named
`California_Fire_Perimeters_last_5_years`. That view sounds right and is
wrong: it stops at 2023, so it omits Eaton, Palisades, Hughes and Bridge,
which are the scars the Southern California deployment exists to watch.
"""

from __future__ import annotations

import argparse
import gzip
import json
import sys
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

import requests

SERVICE_URL = (
    "https://services1.arcgis.com/jUJYIo9tSA7EHvfZ/ArcGIS/rest/services"
    "/California_Historic_Fire_Perimeters/FeatureServer/0/query"
)

# The service caps a response at 2000 records. Paging below that leaves room
# for the cap to be lowered without silently truncating the output.
PAGE_SIZE = 1000

# Power of Ten rule 2: the paging loop needs a bound that does not depend on
# what the server reports. 50 pages is 50k perimeters, against 23,334 in the
# whole state, so hitting this means the loop is not advancing.
MAX_PAGES = 50

# Vertex simplification, in degrees of the output projection (EPSG:4326).
# 0.0005 is about 55 m at these latitudes, sub-pixel below zoom 12. Halving it
# roughly doubles the file for detail the map never draws; doubling it starts
# to flatten the narrow canyon arms that matter for debris flow.
SIMPLIFY_DEG = 0.0005

# How far back the file reaches. The age slider tops out at 5 years, so a
# shorter window would leave the slider pointing at scars the file does not
# carry.
YEARS_BACK = 5

REQUEST_TIMEOUT_S = 120

# Fields the layer draws from. ALARM_DATE is the ignition date in epoch
# milliseconds and drives both the age filter and the age ramp; YEAR_ alone
# would bucket a January fire with the December one eleven months after it.
OUT_FIELDS = "YEAR_,FIRE_NAME,GIS_ACRES,ALARM_DATE"

# A regenerated file that balloons is the failure mode worth catching, since
# nothing downstream errors on a 5 MB overlay, it just stalls the map.
MAX_RAW_BYTES = 1_500_000


@dataclass(frozen=True)
class BoundingBox:
    """A clip region in degrees, west/south/east/north."""

    west: float
    south: float
    east: float
    north: float

    def as_envelope(self) -> str:
        """Return the box in the comma-separated form the service expects."""
        return f"{self.west},{self.south},{self.east},{self.north}"

    def overlaps(self, other: BoundingBox) -> bool:
        """Report whether two boxes share any area.

        Touching edges count as overlapping. That matches the service's
        INTERSECTS filter, so a perimeter grazing the clip boundary survives
        the validation that calls this.

        Args:
            other: The box to test against.

        Returns:
            True if the boxes share any area or edge.
        """
        return (
            self.west <= other.east
            and other.west <= self.east
            and self.south <= other.north
            and other.south <= self.north
        )


# Each box is its profile's composite extent padded by 0.15 degrees, roughly
# 17 km, so a perimeter just outside the radar's product still draws when the
# user pans off the edge.
#
# The two numbers come from different files because the extents do. AQPI's
# live in MapView.svelte as EXTENT_LARGE, since backend COMP_EXTENT is empty
# for that profile and the frontend keeps its own per-product table. XQPI's
# come from backend/profiles/xqpi.py COMP_EXTENT["composite_ref"].
PROFILE_BOXES: dict[str, BoundingBox] = {
    "aqpi": BoundingBox(west=-124.155, south=36.35, east=-121.045, north=39.655),
    "xqpi": BoundingBox(west=-119.714, south=33.138, east=-116.85, north=35.177),
}


def _require_mapping(value: object, what: str) -> dict[str, object]:
    """Narrow a decoded JSON value to an object.

    Args:
        value: Whatever `json` produced.
        what: Name used in the error, e.g. "response".

    Returns:
        The value as a string-keyed dict.

    Raises:
        TypeError: If the value is not a JSON object.
    """
    if not isinstance(value, dict):
        raise TypeError(f"{what} is {type(value).__name__}, expected an object")
    return value


def _require_list(value: object, what: str) -> list[object]:
    """Narrow a decoded JSON value to an array.

    Args:
        value: Whatever `json` produced.
        what: Name used in the error.

    Returns:
        The value as a list.

    Raises:
        TypeError: If the value is not a JSON array.
    """
    if not isinstance(value, list):
        raise TypeError(f"{what} is {type(value).__name__}, expected an array")
    return value


def fetch_page(box: BoundingBox, since_year: int, offset: int) -> dict[str, object]:
    """Fetch one page of perimeters.

    Args:
        box: Clip region.
        since_year: Lowest YEAR_ to include.
        offset: Row offset, for paging.

    Returns:
        The decoded FeatureCollection for this page.

    Raises:
        requests.HTTPError: If the service returns a non-2xx status.
        ValueError: If the body is not a JSON object, or carries an
            ArcGIS error payload instead of features.
    """
    params = {
        "where": f"YEAR_ >= {since_year}",
        "geometry": box.as_envelope(),
        "geometryType": "esriGeometryEnvelope",
        "inSR": "4326",
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": OUT_FIELDS,
        "outSR": "4326",
        "maxAllowableOffset": str(SIMPLIFY_DEG),
        # Paging without an order is undefined, and the service is free to
        # return the same row on two pages or skip one entirely.
        "orderByFields": "OBJECTID",
        "resultOffset": str(offset),
        "resultRecordCount": str(PAGE_SIZE),
        "f": "geojson",
    }
    response = requests.get(SERVICE_URL, params=params, timeout=REQUEST_TIMEOUT_S)
    response.raise_for_status()
    body = _require_mapping(response.json(), "response")
    # ArcGIS answers a rejected query with HTTP 200 and an error object, so
    # status alone does not say the request worked.
    if "error" in body:
        raise ValueError(f"service rejected the query: {body['error']!r}")
    return body


def fetch_features(box: BoundingBox, since_year: int) -> list[object]:
    """Fetch every perimeter in the box, paging until the service stops.

    Args:
        box: Clip region.
        since_year: Lowest YEAR_ to include.

    Returns:
        The features, in service order.

    Raises:
        RuntimeError: If paging runs past MAX_PAGES, which means the loop is
            not advancing rather than that the region is large.
    """
    features: list[object] = []
    for page in range(MAX_PAGES):
        body = fetch_page(box, since_year, offset=page * PAGE_SIZE)
        batch = _require_list(body.get("features"), "features")
        features.extend(batch)
        # Absent once the last page has been served. Checking the flag rather
        # than a short page keeps this correct if the server caps below
        # PAGE_SIZE.
        if body.get("exceededTransferLimit") is not True:
            return features
    raise RuntimeError(f"still paging after {MAX_PAGES} pages, {len(features)} features")


def _feature_box(geometry: dict[str, object]) -> BoundingBox:
    """Compute a polygon's bounding box.

    Args:
        geometry: A GeoJSON Polygon or MultiPolygon geometry object.

    Returns:
        The box enclosing every vertex.

    Raises:
        ValueError: If the geometry carries no usable coordinate pairs.
    """
    lons: list[float] = []
    lats: list[float] = []
    stack: list[object] = [geometry.get("coordinates")]
    while stack:
        node = stack.pop()
        if not isinstance(node, list):
            continue
        if len(node) == 2 and all(isinstance(n, (int, float)) for n in node):
            lon, lat = node
            if isinstance(lon, (int, float)) and isinstance(lat, (int, float)):
                lons.append(float(lon))
                lats.append(float(lat))
            continue
        stack.extend(node)
    if not lons:
        raise ValueError("geometry has no coordinate pairs")
    return BoundingBox(west=min(lons), south=min(lats), east=max(lons), north=max(lats))


def validate_features(features: list[object], box: BoundingBox) -> None:
    """Check every feature before it is written.

    The map has no way to report a bad row: a feature with no ALARM_DATE is
    silently dropped by the age filter, and one outside the box draws where
    nobody looks. Both are caught here instead.

    Args:
        features: Decoded GeoJSON features.
        box: The region they were requested for.

    Raises:
        ValueError: On the first feature whose values fail a check, named so
            the problem can be looked up in the service.
        TypeError: If a feature, its properties or its geometry is not a JSON
            object.
    """
    if not features:
        raise ValueError("service returned no perimeters for this box and year")
    for index, raw in enumerate(features):
        feature = _require_mapping(raw, f"feature {index}")
        props = _require_mapping(feature.get("properties"), f"feature {index} properties")
        name = props.get("FIRE_NAME")
        alarm = props.get("ALARM_DATE")
        if not isinstance(name, str) or not name:
            raise ValueError(f"feature {index} has no FIRE_NAME")
        if not isinstance(alarm, (int, float)) or alarm <= 0:
            raise ValueError(f"{name}: ALARM_DATE is {alarm!r}, expected epoch ms")
        geometry = feature.get("geometry")
        if not isinstance(geometry, dict):
            raise TypeError(f"{name}: geometry is {type(geometry).__name__}")
        if not _feature_box(geometry).overlaps(box):
            raise ValueError(f"{name}: perimeter falls outside the requested box")


def newest_alarm(features: list[object]) -> date:
    """Return the most recent ignition date in the set.

    Printed so the operator can see which season they just baked in, which is
    the one thing a regenerated file should be checked against.

    Args:
        features: Features that have already passed validate_features.

    Returns:
        The latest ALARM_DATE as a calendar date.

    Raises:
        ValueError: If no feature carries a usable ALARM_DATE.
    """
    stamps: list[float] = []
    for raw in features:
        props = _require_mapping(_require_mapping(raw, "feature").get("properties"), "properties")
        alarm = props.get("ALARM_DATE")
        if isinstance(alarm, (int, float)):
            stamps.append(float(alarm))
    if not stamps:
        raise ValueError("no feature carries an ALARM_DATE")
    return datetime.fromtimestamp(max(stamps) / 1000.0, tz=UTC).date()


def write_collection(features: list[object], out_path: Path) -> int:
    """Write the features as a FeatureCollection and report its size.

    The `crs` member the service emits is dropped: RFC 7946 removed it,
    MapLibre ignores it, and leaving it in invites a reader to think the file
    is in something other than WGS 84.

    Args:
        features: Validated features.
        out_path: Destination file.

    Returns:
        Bytes written.

    Raises:
        ValueError: If the result exceeds MAX_RAW_BYTES.
    """
    collection = {"type": "FeatureCollection", "features": features}
    # separators: the default ", " and ": " add about 8% across 30k vertices.
    text = json.dumps(collection, separators=(",", ":"))
    raw = text.encode("utf-8")
    if len(raw) > MAX_RAW_BYTES:
        raise ValueError(f"{len(raw)} bytes exceeds the {MAX_RAW_BYTES} ceiling")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    _ = out_path.write_bytes(raw)
    return len(raw)


def build(profile: str, since_year: int, out_path: Path) -> None:
    """Fetch, validate and write one profile's overlay.

    Args:
        profile: Key into PROFILE_BOXES.
        since_year: Lowest YEAR_ to include.
        out_path: Destination file.

    Raises:
        KeyError: If the profile has no bounding box.
        ValueError: If the fetched set fails validation or exceeds the size
            ceiling.
        TypeError: If the service returns a shape that is not GeoJSON.
    """
    box = PROFILE_BOXES[profile]
    features = fetch_features(box, since_year)
    validate_features(features, box)
    written = write_collection(features, out_path)
    # gzip here matches what adapter-node pre-compresses at build time, which
    # is what the browser actually downloads.
    on_wire = len(gzip.compress(out_path.read_bytes()))
    print(
        f"{profile}: {len(features)} perimeters since {since_year}, "
        f"newest ignition {newest_alarm(features)}"
    )
    print(f"  {out_path} — {written / 1024:.0f} KB raw, {on_wire / 1024:.0f} KB gzipped")


def parse_args(argv: list[str]) -> argparse.Namespace:
    """Parse the command line.

    Args:
        argv: Arguments without the program name.

    Returns:
        The parsed namespace.
    """
    repo_root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description=__doc__ or "")
    _ = parser.add_argument(
        "--profile",
        required=True,
        choices=sorted(PROFILE_BOXES),
        help="which deployment's extent to clip to",
    )
    _ = parser.add_argument(
        "--since-year",
        type=int,
        default=datetime.now(tz=UTC).year - YEARS_BACK,
        help="lowest fire year to include (default: this year minus 5)",
    )
    _ = parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="output path (default: frontend/static/data/burn-scars-<profile>.json)",
    )
    args = parser.parse_args(argv)
    # A later year than this silently produces a file shorter than the age
    # slider's range, so the map would offer years it has no perimeters for
    # and the empty result would read as a region with no fires.
    newest_allowed = datetime.now(tz=UTC).year - YEARS_BACK
    if args.since_year > newest_allowed:
        parser.error(
            f"--since-year {args.since_year} leaves the {YEARS_BACK}-year age "
            f"slider short; use {newest_allowed} or earlier"
        )
    if args.out is None:
        args.out = repo_root / "frontend" / "static" / "data" / f"burn-scars-{args.profile}.json"
    return args


def main(argv: list[str]) -> int:
    """Run the fetch for one profile.

    Args:
        argv: Arguments without the program name.

    Returns:
        0 on success, 1 if the fetch or validation failed.
    """
    args = parse_args(argv)
    try:
        build(args.profile, args.since_year, args.out)
    except (requests.RequestException, ValueError, TypeError, RuntimeError) as err:
        print(f"burn-scar fetch failed: {err}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
