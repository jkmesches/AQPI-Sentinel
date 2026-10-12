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
    # Read from K2/trinity directly rather than through radarca. Separate stages so
    # alerts.yaml can route them as authoritative while the radarca-derived stages
    # stay informational.
    "LB1":     "Backend Products",
    "LB2":     "Backend Radar Arrival",
    # The processing between arrival (LB2) and a published product (LB1/L1):
    # composite participation, and the DROPS producer. Nothing watched this
    # band until 2026-10-05, when a dead producer read as five radar outages
    # and a radar left the composite unremarked in the same twelve hours.
    "LB3":     "Backend Processing",
}
_STAGE_TECH_CODES = {
    "L0":      "L0",
    "L1":      "L1",
    "L2":      "L2",
    "L3":      "L3",
    "L4-T1T2": "L4",
    "LB1":     "LB1",
    "LB2":     "LB2",
    "LB3":     "LB3",
}


# Which of the two OBSERVATION TIERS a stage belongs to.
#
# The `L*` stages ask the radarca display tier what it believes. The `LB*`
# stages read the published files off K2/Trinity and owe the web tier nothing.
# The distinction matters to an operator because the two can disagree, and
# where they do, the disagreement itself is the signal — so anything that
# switches on "which half of the stack is this" switches on this map rather
# than on a prefix test or a list of check-id families.
#
# Stated per stage, not derived from the "LB" spelling, so a new stage has to
# answer the question. The parity test asserts this covers _STAGE_LABELS
# exactly: a stage added above without a tier here fails that assertion
# instead of silently landing in whichever half the string test picks.
TIERS = ("frontend", "backend")

_STAGE_TIERS = {
    "L0":      "frontend",
    "L1":      "frontend",
    "L2":      "frontend",
    "L3":      "frontend",
    "L4-T1T2": "frontend",
    "LB1":     "backend",
    "LB2":     "backend",
    "LB3":     "backend",
}


def stage_tier(s: str) -> str:
    """'LB2' -> 'backend', 'L2' -> 'frontend'.

    An unknown stage reads as 'frontend'. That is the FAIL-OPEN direction for
    every current caller: the tier is used to gate NOTIFICATION, and an
    unrecognised stage should keep paging rather than go quiet because of a
    taxonomy gap.
    """
    return _STAGE_TIERS.get(s, "frontend")


def stage_descriptor(s: str) -> str:
    """e.g. 'L2' -> 'Radar Scans'. Unknown IDs fall through unchanged."""
    return _STAGE_LABELS.get(s, s)


def stage_tech(s: str) -> str:
    """e.g. 'L4-T1T2' -> 'L4'. Unknown IDs fall through unchanged."""
    return _STAGE_TECH_CODES.get(s, s)
