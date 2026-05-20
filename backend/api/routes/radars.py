"""Radar geography. Static; baked from the upstream JS bundle. Used by the
frontend MapView component."""
from __future__ import annotations
from fastapi import APIRouter

from ...config import RADAR_FOLDER

router = APIRouter(prefix="/api/radars")


# Site coordinates compiled from a mix of sources: the original
# X-band positions (XSCV, XSCW, XSCR, XEBY) came from radarca's
# JS bundle and the characterization doc. XSWR (Sawyer Ridge, San
# Mateo County, hosted by SFPUC) and CBAND (Mt Barnabe, Marin
# County, FCC-licensed Mar 2026) were not in any radarca-exposed
# coord table; values below are the GNIS topo centroids for those
# named landforms — accurate within ~100-200 m of the actual
# antenna, well below MapLibre's per-pixel resolution at our
# typical operational zoom. Range on CBAND is from Marin County's
# press coverage ("detects out to 62 miles" → ~100 km).
RADAR_META = {
    "XSCV":  {"lat": 37.3989, "lon": -121.8334, "range_m":  40_000, "kind": "xband", "name": "Coyote Valley"},
    "XSCW":  {"lat": 38.5216, "lon": -122.8022, "range_m":  40_000, "kind": "xband", "name": "Sonoma"},
    "XSCR":  {"lat": 36.9847, "lon": -121.9786, "range_m":  40_000, "kind": "xband", "name": "Santa Cruz"},
    "XEBY":  {"lat": 37.8156, "lon": -122.0620, "range_m":  40_000, "kind": "xband", "name": "East Bay"},
    "XSWR":  {"lat": 37.5564, "lon": -122.4083, "range_m":  40_000, "kind": "xband", "name": "Sawyer Ridge"},
    "CBAND": {"lat": 38.0277, "lon": -122.7161, "range_m": 100_000, "kind": "cband", "name": "Mt Barnabe"},
    "KBBX":  {"lat": 39.4956, "lon": -121.6316, "range_m": 100_000, "kind": "nexrad", "name": "KBBX"},
    "KDAX":  {"lat": 38.5010, "lon": -121.6770, "range_m": 100_000, "kind": "nexrad", "name": "KDAX"},
    "KMUX":  {"lat": 37.1553, "lon": -121.8980, "range_m": 100_000, "kind": "nexrad", "name": "KMUX"},
}


@router.get("/meta")
async def list_radar_meta():
    return [
        {"id": rid, **meta, "folder": RADAR_FOLDER.get(rid)}
        for rid, meta in RADAR_META.items()
    ]
