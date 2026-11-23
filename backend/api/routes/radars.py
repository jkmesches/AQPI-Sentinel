"""Radar geography. Static; baked from the upstream JS bundle. Used by the
frontend MapView component."""
from __future__ import annotations
from typing import Any

from fastapi import APIRouter

from ... import fs_products as _fs  # noqa: TID252  the package imports relatively throughout
from ...config import (  # noqa: TID252  the package imports relatively throughout
    NEXRAD_SITES, RADAR_FOLDER, RADAR_META_OVERRIDE)

router = APIRouter(prefix="/api/radars")


# Site coordinates + per-radar scan elevations.
#
# The 5 X-band positions (XSCV, XSCW, XSCR, XEBY, XSWR) and their
# elevation lists are authoritative — pulled straight from each
# radar's own metadata JSON exposed by the CSU Web Radar Display
# (https://radardisplay.engr.colostate.edu/<ID>/json/el_1/radar_plot_0.json,
# fields RadarID / Latitude / Longitude / MaxRange, and the
# <elevation-select> options in each radar's index.html). The four
# original X-bands matched radarca's bundle to 4 decimals; XSWR
# (Sawyer Ridge, San Mateo County, SFPUC) came only from this source.
#
# CBAND (Mt Barnabe, Marin County, FCC-licensed Mar 2026) is NOT in
# the radar-display directory — its coords are the GNIS topo centroid
# for the named peak (~100-200 m of the antenna) and its 100 km range
# is from Marin County press coverage ("detects out to 62 miles").
# elevations=None marks the entries we couldn't source authoritatively.
#
# The NEXRAD positions are the ones in each site's Level II volume header,
# which is the point the NEXRAD plotter centers its frames on. The rounded
# values these replaced sat 5 to 73 m off it. `frame_half_m` is half the
# side of that plotter's square frame (693 px over 400 km, north up). Its
# `range_m` is the circle inscribed in that frame, where the plotter stops
# drawing: echo in 39 frames from all three sites reached 200.4 km and no
# further. The 100 km these sites carried before is the ring radarca draws,
# from the radarCoordinates table in its bundle, and says nothing about
# these frames.
RADAR_META = {
    "XSCV":  {"lat": 37.3989, "lon": -121.8334, "range_m":  40_000, "kind": "xband", "name": "Coyote Valley", "elevations": [2.0, 3.0, 4.0, 5.0]},
    "XSCW":  {"lat": 38.5216, "lon": -122.8022, "range_m":  40_000, "kind": "xband", "name": "Sonoma", "elevations": [1.5, 2.5, 3.5, 4.5]},
    "XSCR":  {"lat": 36.9847, "lon": -121.9786, "range_m":  40_000, "kind": "xband", "name": "Santa Cruz", "elevations": [2.0, 3.0, 4.0, 5.0]},
    "XEBY":  {"lat": 37.8156, "lon": -122.0620, "range_m":  40_000, "kind": "xband", "name": "East Bay", "elevations": [0.0, 1.0, 2.0, 3.0]},
    "XSWR":  {"lat": 37.5742867, "lon": -122.4229867, "range_m":  40_000, "kind": "xband", "name": "Sawyer Ridge", "elevations": [2.5, 3.5, 4.5, 5.5]},
    "CBAND": {"lat": 38.0277, "lon": -122.7161, "range_m": 100_000, "kind": "cband", "name": "Mt Barnabe", "elevations": None},
    "KBBX":  {"lat": 39.49564, "lon": -121.63161, "range_m": 200_000,
              "frame_half_m": 200_000, "kind": "nexrad", "name": "KBBX", "elevations": None},
    "KDAX":  {"lat": 38.50111, "lon": -121.67783, "range_m": 200_000,
              "frame_half_m": 200_000, "kind": "nexrad", "name": "KDAX", "elevations": None},
    "KMUX":  {"lat": 37.15522, "lon": -121.89845, "range_m": 200_000,
              "frame_half_m": 200_000, "kind": "nexrad", "name": "KMUX", "elevations": None},
}


# The table above is AQPI's. A profile watching a different network supplies
# its own and REPLACES this one rather than adding to it — the nine radars
# below do not exist on another network, and returning them anyway is what put
# nine Bay Area radars with folder=None, and no FLOW, on the XQPI instance.
def _meta() -> dict[str, dict]:
    return RADAR_META_OVERRIDE or RADAR_META


def _has_frames(rid: str) -> bool:
    """Can the map draw this radar's frames on this deployment?

    The X-band and C-band frames come from radarca when there is no mount,
    so they always can. The NEXRAD frames exist only on the mount.
    """
    if rid in RADAR_FOLDER:
        return True
    return rid in NEXRAD_SITES and _fs.available()


@router.get("/meta")
async def list_radar_meta() -> list[dict[str, Any]]:
    """Every radar this profile shows, with where it is and what it draws."""
    return [
        {"id": rid, **meta, "folder": RADAR_FOLDER.get(rid), "scan": _has_frames(rid)}
        for rid, meta in _meta().items()
    ]
