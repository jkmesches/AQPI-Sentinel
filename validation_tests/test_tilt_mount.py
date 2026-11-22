"""Reading tilts off the mount, and the stream-gauge list Sentinel ships.

Run directly (no DB, no network):

    python validation_tests/test_tilt_mount.py

radardisplay.engr.colostate.edu serves its document root verbatim, so
`/<RADAR>/images/el_<n>/<moment>_<frame>.png` is the path on disk under
`htdocs/`. Two frames fetched over HTTP on 2026-11-21 matched the files on the main archive
byte for byte. The tilt routes therefore read a file where they used to open
a connection.

That swap is safe only while three things hold.

  1. THE ROOT RESOLVES THE WAY EACH DEPLOYMENT NEEDS. AQPI mounts the
     web-files root that holds htdocs, so the path derives from
     backend_root. XQPI mounts only the secondary archive, and FLOW's tilts live under the
     AQPI root on the main archive, so deriving one there would name a directory that does
     not exist. An absent tree has to read as "use HTTP" rather than as an
     error on every tilt request.

  2. THE PARAMETERS ARE CONFINED. `radar`, `el` and `moment` arrive from a
     query string and are joined into a path. The product path is confined
     with realpath because its source is free-form; these are confined by
     vocabulary, and a test is the only thing that says the vocabulary is
     actually enforced rather than assumed.

  3. A MISSING FRAME READS AS A 404. The rolling window is seven deep
     and shifts every ~140 s, so a reader asking for a frame that has just
     aged out is ordinary, and the HTTP path answered it with a 404.

The gauge list is here because it is the same question with a different
answer: the two per-COMID series are rows of files on the main archive, and this one is on
no share, so it ships with Sentinel instead.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

FAILURES: list[str] = []

PNG = b"\x89PNG\r\n\x1a\n" + b"rest of a frame"

# What radar-display puts beside each frame, trimmed to the keys the console
# reads. The timestamp is its naive Mountain-time spelling.
PLOT_JSON: dict[str, Any] = {
    "RadarID": "aqpi.scvw",
    "Latitude": {"Units": "deg", "Value": 37.398878},
    "Longitude": {"Units": "deg", "Value": -121.833363},
    "MaxRange": {"Units": "km", "Value": 40.50920685892669},
    "Time": {"Zone": "LT", "Value": "Sat, 21 Nov 2026 20:56:34"},
    "Scan": {"Type": "PPI", "Angle": {"Units": "deg", "Value": "2.0"}},
}


def check(label: str, cond: bool, detail: str = "") -> None:
    """Record one assertion.

    Args:
        label: What was checked.
        cond: Whether it held.
        detail: What was seen, shown when it did not.
    """
    print(f"  {'ok  ' if cond else 'FAIL'}  {label}"
          f"{'  — ' + detail if detail else ''}")
    if not cond:
        FAILURES.append(label)


def build_tilt_tree(root: str) -> None:
    """One radar's four elevations under a stand-in htdocs root.

    Args:
        root: Directory standing in for SENTINEL_BACKEND_ROOT. The tree is
            written at the same offset the real mount uses, so the test
            exercises the derived path rather than a hand-made one.
    """
    for el in (1, 2, 3, 4):
        images = Path(root, "htdocs", "XSCV", "images", f"el_{el}")
        meta = Path(root, "htdocs", "XSCV", "json", f"el_{el}")
        images.mkdir(parents=True, exist_ok=True)
        meta.mkdir(parents=True, exist_ok=True)
        for frame in range(7):
            for moment in ("reflectivity", "velocity", "copolarcorrelation"):
                _ = Path(images, f"{moment}_{frame}.png").write_bytes(PNG)
            doc = dict(PLOT_JSON)
            doc["Scan"] = {"Type": "PPI",
                           "Angle": {"Units": "deg", "Value": f"{el + 1}.0"}}
            _ = Path(meta, f"radar_plot_{frame}.json").write_text(json.dumps(doc))


def load(profile: str, backend_root: str | None,
         tilt_root: str | None) -> Any:
    """Import fs_products fresh under one deployment's environment.

    Args:
        profile: SENTINEL_PROFILE value.
        backend_root: SENTINEL_BACKEND_ROOT, or None to leave it unset.
        tilt_root: SENTINEL_TILT_ROOT, or None to leave it unset.

    Returns:
        The reimported module.
    """
    for name in [m for m in sys.modules if m.startswith("backend")]:
        del sys.modules[name]
    os.environ["SENTINEL_PROFILE"] = profile
    for var, value in (("SENTINEL_BACKEND_ROOT", backend_root),
                       ("SENTINEL_TILT_ROOT", tilt_root)):
        if value is None:
            _ = os.environ.pop(var, None)
        else:
            os.environ[var] = value
    import backend.fs_products as fs
    return fs


def run_resolution(root: str) -> None:
    """Which deployments find a tilt tree and which fall back to HTTP.

    Args:
        root: A directory holding an htdocs tree.
    """
    print("where the tilt root comes from:")
    fs = load("aqpi", root, None)
    check("aqpi derives it from the mount", fs.tilt_available())
    check("...at TILT_TREE under backend_root",
          fs.tilt_root() == os.path.join(root, "htdocs"), str(fs.tilt_root()))

    fs = load("aqpi", None, None)
    check("a deployment with no mount falls back to HTTP",
          not fs.tilt_available(), str(fs.tilt_root()))

    # XQPI mounts only the secondary archive. FLOW's tilts are under the AQPI root on the main archive, so
    # a path derived from its own mount would be a decoy: it would resolve,
    # read nothing, and report every frame missing instead of using HTTP.
    fs = load("xqpi", root, None)
    check("xqpi does not derive one from its own mount",
          not fs.tilt_available(), str(fs.tilt_root()))

    fs = load("xqpi", root, os.path.join(root, "htdocs"))
    check("...and takes SENTINEL_TILT_ROOT when it is given one",
          fs.tilt_available())
    check("...which wins over any derived path",
          fs.tilt_root() == os.path.join(root, "htdocs"), str(fs.tilt_root()))

    fs = load("aqpi", root, "/somewhere/else")
    check("the override wins on aqpi too",
          fs.tilt_root() == "/somewhere/else", str(fs.tilt_root()))


def run_reads(root: str) -> None:
    """Reading frames and metadata out of the tree.

    Args:
        root: A directory holding an htdocs tree.
    """
    print("\nreading one tilt:")
    fs = load("aqpi", root, None)
    resp = asyncio.run(fs.read_tilt_image("XSCV", 2, "reflectivity", 0))
    check("the newest frame comes back", resp.status_code == 200,
          str(resp.status_code))
    check("...as the bytes on disk", resp.content == PNG)
    check("...typed as a PNG",
          resp.headers.get("content-type") == "image/png",
          str(resp.headers))

    doc = asyncio.run(fs.read_tilt_json("XSCV", 2, 0))
    check("the metadata parses", isinstance(doc, dict))
    check("...and carries the scan angle",
          (doc or {}).get("Scan", {}).get("Angle", {}).get("Value") == "3.0",
          json.dumps((doc or {}).get("Scan")))
    check("...and the stamp the archive keys on",
          (doc or {}).get("Time", {}).get("Value") == PLOT_JSON["Time"]["Value"])

    print("\na frame that is not there is a 404:")
    gone = asyncio.run(fs.read_tilt_image("XSCV", 2, "reflectivity", 9))
    check("a frame past the window", gone.status_code == 404,
          str(gone.status_code))
    missing = asyncio.run(fs.read_tilt_image("XEBY", 2, "reflectivity", 0))
    check("a radar with no directory", missing.status_code == 404,
          str(missing.status_code))
    check("and its metadata reads as absent",
          asyncio.run(fs.read_tilt_json("XEBY", 2, 0)) is None)

    # A half-written frame must not be cached and archived as a valid PNG.
    torn = Path(root, "htdocs", "XSCV", "images", "el_2", "velocity_6.png")
    _ = torn.write_bytes(b"<html>404</html>")
    bad = asyncio.run(fs.read_tilt_image("XSCV", 2, "velocity", 6))
    check("bytes that are not a PNG are refused", bad.status_code == 502,
          str(bad.status_code))
    _ = torn.write_bytes(PNG)


def run_confinement(root: str) -> None:
    """What the path builder refuses.

    Args:
        root: A directory holding an htdocs tree.
    """
    print("\nthe query parameters cannot leave the tree:")
    fs = load("aqpi", root, None)
    for label, radar, el, moment in (
        ("a traversal in the radar", "../../etc", 2, "reflectivity"),
        ("a slash in the radar", "XSCV/../..", 2, "reflectivity"),
        ("an absolute radar", "/etc", 2, "reflectivity"),
        ("an empty radar", "", 2, "reflectivity"),
        ("a traversal in the moment", "XSCV", 2, "../../../etc/passwd"),
        ("a slash in the moment", "XSCV", 2, "a/b"),
    ):
        try:
            _ = asyncio.run(fs.read_tilt_image(radar, el, moment, 0))
        except ValueError:
            check(label + " is refused", True)
        else:
            check(label + " is refused", False, "it was accepted")

    for el in (0, 5, -1, 99):
        try:
            _ = asyncio.run(fs.read_tilt_image("XSCV", el, "reflectivity", 0))
        except ValueError:
            check(f"elevation {el} is refused", True)
        else:
            check(f"elevation {el} is refused", False, "it was accepted")

    # The probe has to return a known value, or every refusal above would
    # pass against a reader that refuses everything.
    good = asyncio.run(fs.read_tilt_image("XSCV", 3, "reflectivity", 1))
    check("while a real frame still reads", good.status_code == 200,
          str(good.status_code))


def run_sites() -> None:
    """The stream-gauge site list that ships with Sentinel."""
    print("\nthe vendored stream-gauge list:")
    from backend.api.routes.upstream import _parse_sites, _SITES_FILE

    check("the file is in the repo", _SITES_FILE.exists(), str(_SITES_FILE))
    if not _SITES_FILE.exists():
        return
    sites = _parse_sites(_SITES_FILE.read_text(encoding="utf-8"))
    # A floor rather than the exact count, so adding a gauge upstream does not
    # fail a test about the list being usable.
    check("it holds the whole network", len(sites) > 300, str(len(sites)))
    check("every entry has a COMID",
          all(str(s["comid"]).isdigit() for s in sites))
    check("every coordinate is in Northern California",
          all(30.0 <= float(s["lat"]) <= 45.0        # type: ignore[arg-type]
              and -126.0 <= float(s["lon"]) <= -114.0 for s in sites))  # type: ignore[arg-type]
    statuses = {s["status"] for s in sites}
    check("status is only B or R", statuses <= {"B", "R"}, str(statuses))
    check("and some sites are real-time",
          any(s["status"] == "R" for s in sites))

    # 29 COMIDs appear twice, at coordinates a few hundred meters apart: two
    # gauge sites on one forecast reach. Collapsing them would move a marker
    # off its own gauge, so the parser keeps both.
    comids = [s["comid"] for s in sites]
    check("a repeated COMID keeps both of its sites",
          len(comids) > len(set(comids)),
          f"{len(comids)} rows, {len(set(comids))} reaches")

    print("\na list that is not the real one is dropped row by row:")
    check("a row with no coordinate is dropped",
          _parse_sites("COMID,LatSite,LonSite,Status\n1,,,,B\n") == [])
    check("a row with text where a number goes is dropped",
          _parse_sites("COMID,LatSite,LonSite,Status\n1,north,west,B\n") == [])
    check("a file with the wrong columns yields nothing",
          _parse_sites("a,b\n1,2\n") == [])
    check("an empty body yields nothing", _parse_sites("") == [])
    kept = _parse_sites(
        "COMID,LatSite,LonSite,Status\n1,north,west,B\n2,38.5,-122.8,R\n")
    check("...while a good row beside a bad one survives",
          len(kept) == 1 and kept[0]["comid"] == "2", str(kept))


def run_provenance() -> None:
    """What x-sentinel-cache says about where the bytes came from.

    A fetcher alone used to mean "mount". The tilt routes pass one for
    radar-display over HTTP as well, and have since they were written, so
    every tilt frame crossing the network was labeled as a local read. The
    header is what tells an operator whether a deployment is still reaching
    out, which makes a wrong label worse than no label.

    `origin_url` is what separates them: a caller reading a file names no
    origin. Driven through `_serve_source` with no store, so the archive step
    is skipped and the fetcher is reached on every call.
    """
    print("\nwhat the cache header credits:")
    import backend.api.routes.upstream as up
    from backend.fs_products import FsResponse

    class _State:
        store = None

    class _App:
        state = _State()

    async def fetch_ok() -> FsResponse:
        return FsResponse(200, PNG, "image/png")

    cases = (
        ("a file read says mount", None, "mount"),
        ("another HTTP origin says upstream",
         "https://radardisplay.engr.colostate.edu/XSCV/images/el_2/x.png",
         "upstream"),
    )
    for label, origin, want in cases:
        # A fresh key each time, or the LRU answers and nothing is exercised.
        key = f"test/{label.replace(' ', '-')}.png"
        _, _, prov = asyncio.run(up._serve_source(
            _App(), None, key, fetcher=fetch_ok, origin_url=origin))
        check(label, prov == want, prov)
        # Second call must come from the LRU, which proves the key was novel
        # on the first and that the fetcher ran rather than a cache.
        _, _, again = asyncio.run(up._serve_source(
            _App(), None, key, fetcher=fetch_ok, origin_url=origin))
        check("...and the repeat is served from the LRU", again == "lru",
              again)


def main() -> int:
    """Run every group against a temporary tree.

    Returns:
        0 when every assertion held, 1 otherwise.
    """
    with tempfile.TemporaryDirectory() as root:
        build_tilt_tree(root)
        run_resolution(root)
        run_reads(root)
        run_confinement(root)
        run_provenance()
    run_sites()
    if FAILURES:
        print(f"\n{len(FAILURES)} FAILED: {', '.join(FAILURES)}")
        return 1
    print("\nall tilt-mount and gauge-list assertions passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
