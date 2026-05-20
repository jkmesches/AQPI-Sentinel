"""Radar geography. Static; baked from the upstream JS bundle. Used by the
frontend MapView component."""
from __future__ import annotations
from fastapi import APIRouter

from ...config import RADAR_FOLDER

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
RADAR_META = {
    "XSCV":  {"lat": 37.3989, "lon": -121.8334, "range_m":  40_000, "kind": "xband", "name": "Coyote Valley", "elevations": [2.0, 3.0, 4.0, 5.0]},
    "XSCW":  {"lat": 38.5216, "lon": -122.8022, "range_m":  40_000, "kind": "xband", "name": "Sonoma", "elevations": [1.5, 2.5, 3.5, 4.5]},
    "XSCR":  {"lat": 36.9847, "lon": -121.9786, "range_m":  40_000, "kind": "xband", "name": "Santa Cruz", "elevations": [2.0, 3.0, 4.0, 5.0]},
    "XEBY":  {"lat": 37.8156, "lon": -122.0620, "range_m":  40_000, "kind": "xband", "name": "East Bay", "elevations": [0.0, 1.0, 2.0, 3.0]},
    "XSWR":  {"lat": 37.5742867, "lon": -122.4229867, "range_m":  40_000, "kind": "xband", "name": "Sawyer Ridge", "elevations": [2.5, 3.5, 4.5, 5.5]},
    "CBAND": {"lat": 38.0277, "lon": -122.7161, "range_m": 100_000, "kind": "cband", "name": "Mt Barnabe", "elevations": None},
    "KBBX":  {"lat": 39.4956, "lon": -121.6316, "range_m": 100_000, "kind": "nexrad", "name": "KBBX", "elevations": None},
    "KDAX":  {"lat": 38.5010, "lon": -121.6770, "range_m": 100_000, "kind": "nexrad", "name": "KDAX", "elevations": None},
    "KMUX":  {"lat": 37.1553, "lon": -121.8980, "range_m": 100_000, "kind": "nexrad", "name": "KMUX", "elevations": None},
}


@router.get("/meta")
async def list_radar_meta():
    return [
        {"id": rid, **meta, "folder": RADAR_FOLDER.get(rid)}
        for rid, meta in RADAR_META.items()
    ]
