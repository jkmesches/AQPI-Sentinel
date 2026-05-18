"""Canonical mapping from internal stage IDs to user-facing descriptors.

Mirrors frontend/src/lib/format.ts. Internal IDs (L0…L4-T1T2) flow through
the data layer; anything rendered to a human goes through stage_descriptor().
The technical code stays discoverable via stage_tech() (used in email
footers + push payload data for click routing).

Approved 2026-05-18.
"""
from __future__ import annotations


_STAGE_LABELS = {
    "L0":      "Connectivity",
    "L1":      "Product Freshness",
    "L2":      "Radar Scans",
    "L3":      "Map Overlays",
    "L4-T1T2": "Image Quality",
}
_STAGE_TECH_CODES = {
    "L0":      "L0",
    "L1":      "L1",
    "L2":      "L2",
    "L3":      "L3",
    "L4-T1T2": "L4",
}


def stage_descriptor(s: str) -> str:
    """e.g. 'L2' -> 'Radar Scans'. Unknown IDs fall through unchanged."""
    return _STAGE_LABELS.get(s, s)


def stage_tech(s: str) -> str:
    """e.g. 'L4-T1T2' -> 'L4'. Unknown IDs fall through unchanged."""
    return _STAGE_TECH_CODES.get(s, s)
