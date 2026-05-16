"""Radar geography. Static; baked from the upstream JS bundle. Used by the
frontend MapView component."""
from __future__ import annotations
from fastapi import APIRouter

from ...config import RADAR_FOLDER

router = APIRouter(prefix="/api/radars")


# Per radarca's JS bundle (radarCoordinates) — see the characterization doc.
RADAR_META = {
    "XSCV":  {"lat": 37.3989, "lon": -121.8334, "range_m":  40_000, "kind": "xband", "name": "Coyote Valley"},
    "XSCW":  {"lat": 38.5216, "lon": -122.8022, "range_m":  40_000, "kind": "xband", "name": "Sonoma"},
    "XSCR":  {"lat": 36.9847, "lon": -121.9786, "range_m":  40_000, "kind": "xband", "name": "Santa Cruz"},
    "XEBY":  {"lat": 37.8156, "lon": -122.0620, "range_m":  40_000, "kind": "xband", "name": "East Bay"},
    "XSWR":  {"lat": 37.7500, "lon": -122.2000, "range_m":  40_000, "kind": "xband", "name": "SWR"},
    "CBAND": {"lat": 37.9500, "lon": -122.4000, "range_m":  80_000, "kind": "cband", "name": "C-band"},
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
