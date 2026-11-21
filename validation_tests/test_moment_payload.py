#!/usr/bin/env python3
"""The moment vocabulary the API sends must be the one the backend resolves by.

The console used to carry AQPI's five moment labels as a TypeScript constant.
XQPI names two of them differently ("DifferentialReflectivity" against
"Differential Reflectivity") and publishes no RhoHV, so three of the five
buttons on the XQPI deployment sent a label ``moment_to_prefix()`` does not
know, and ``/api/upstream/xband_scan.png`` answered 400. Nothing on screen
said why.

The picker now reads ``moment_prefixes`` from ``/api/version``, which the
backend resolves with its own table. That removes the second copy of the rule
and the whole class of mismatch with it.

Two things still have to hold, and neither is visible from one side alone.
Every label the payload offers must resolve for the radar it is offered on,
otherwise the picker is back to offering a button that answers 400. And the
payloads pasted into ``validation_tests/js/test_map_moments.mjs`` must still
be what the profiles produce, since those assertions are only as good as
their inputs.

Run:  python3 validation_tests/test_moment_payload.py
"""
from __future__ import annotations

import importlib
import json
import os
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

# The payloads validation_tests/js/test_map_moments.mjs asserts against. A
# change here means that file needs the same change.
JS_TEST = REPO / "validation_tests/js/test_map_moments.mjs"

PROFILES = ("aqpi", "xqpi")


def payload_for(profile: str) -> dict[str, dict[str, str]]:
    """Build one profile's moment_prefixes exactly as the API builds it.

    Args:
        profile: The SENTINEL_PROFILE value.

    Returns:
        Radar id to moment label to published prefix.
    """
    os.environ["SENTINEL_PROFILE"] = profile
    for name in [m for m in sys.modules if m.startswith("backend")]:
        del sys.modules[name]
    config = importlib.import_module("backend.config")
    return {rid: {m: config.moment_to_prefix(rid, m) for m in config.X_MOMENTS}
            for rid in config.RADAR_FOLDER}


def check_resolvable(profile: str, payload: dict[str, dict[str, str]],
                     fails: list[str]) -> None:
    """Every offered label must resolve on the radar it is offered on.

    Args:
        profile: Which profile produced the payload.
        payload: The payload.
        fails: Collects failure messages.
    """
    if not payload:
        fails.append(f"{profile}: the payload is empty")
        return
    for radar, moments in payload.items():
        if not moments:
            fails.append(f"{profile}/{radar}: offers no moment")
        for label, prefix in moments.items():
            if not prefix:
                fails.append(f"{profile}/{radar}/{label}: empty prefix")
            if not isinstance(prefix, str):
                fails.append(f"{profile}/{radar}/{label}: prefix is not a string")


def check_js_matches(payloads: dict[str, dict[str, Any]],
                     fails: list[str]) -> None:
    """The payloads pasted into the JS assertions must still be current.

    Args:
        payloads: Each profile's live payload.
        fails: Collects failure messages.
    """
    text = JS_TEST.read_text()
    for profile, want in payloads.items():
        for radar, moments in want.items():
            if f"{radar}: {{" not in text and f"  {radar}:" not in text:
                continue
            for label, prefix in moments.items():
                quoted = f"'{label}': '{prefix}'"
                if quoted not in text:
                    fails.append(
                        f"{profile}/{radar}: {JS_TEST.name} does not carry "
                        f"{quoted}")


def main() -> int:
    """Check both profiles and report.

    Returns:
        Process exit status.
    """
    fails: list[str] = []
    payloads: dict[str, dict[str, Any]] = {}
    for profile in PROFILES:
        payload = payload_for(profile)
        payloads[profile] = payload
        print(f"{profile}: {len(payload)} radars, "
              f"{sorted({m for v in payload.values() for m in v})}")
        check_resolvable(profile, payload, fails)

    # The AQPI C-band is the exception that makes the per-radar shape
    # necessary. If it ever collapses to one table, the payload can too.
    aqpi = payloads["aqpi"]
    if "CBAND" in aqpi and "XEBY" in aqpi:
        if aqpi["CBAND"].get("PhiDP") == aqpi["XEBY"].get("PhiDP"):
            fails.append(
                "the C-band and X-band PhiDP prefixes now agree; the payload "
                "no longer needs to be per-radar")
    else:
        fails.append("aqpi: CBAND or XEBY is missing from RADAR_FOLDER")

    check_js_matches(payloads, fails)

    if fails:
        print("\nFAILED:")
        for f in fails:
            print(f"  {f}")
        return 1
    print("\nok: both profiles resolve every label they offer, and the "
          f"payloads in {JS_TEST.name} are current")
    print(json.dumps(payloads["xqpi"], indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
