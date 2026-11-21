#!/usr/bin/env python3
"""A unit picker must not serve a subtree nobody writes any more.

Six of AQPI's products publish a second unit, as ``details_<unit>.json``
beside the canonical one and ``images/<unit>/`` beside the canonical frames.
Offering a toggle over those is slice 7 of ``docs/map-console-plan.md``.

Reading the mount on 2026-11-21 turned up two reasons a plain toggle would
lie. Three of the six stopped writing their millimeter subtree on
2026-09-30 while still writing inches every two minutes, so switching units
on those would show rainfall seven weeks old with nothing saying so. And
temperature's second subtree is not a second unit at all: all twelve of its
``Deg`` frames were byte-identical to its ``F`` frames, so a toggle there
would move nothing while implying it had.

So the vocabulary is per product, temperature has none, and every pair
carries the state the mount was in when it was read. These assertions pin
that, plus the resolution itself and its refusals.

Run:  python3 validation_tests/test_product_units.py
"""
from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

# The products the mount showed a real second unit for, with the suffix
# each label resolves to. Anything added here needs the mount read again.
EXPECTED_AQPI: dict[str, dict[str, str]] = {
    "qpe_15min":             {"in": "in", "mm": "mm"},
    "qpe_1hr":               {"in": "in", "mm": "mm"},
    "precip_rate_radar":     {"in/h": "in", "mm/h": "mm"},
    "fcst_total_precip":     {"in": "in", "mm": "mm"},
    "fcst_total_precip_cum": {"in": "in", "mm": "mm"},
    "fcst_precip_rate":      {"in/h": "in", "mm/h": "mm"},
}

# Products that look like they have units and do not.
EXPECTED_NO_UNITS = ("comp_ref", "fcst_temp", "water_level")


def load(profile: str) -> Any:
    """Import backend.config under one profile.

    Args:
        profile: The SENTINEL_PROFILE value.

    Returns:
        The config module.
    """
    os.environ["SENTINEL_PROFILE"] = profile
    for name in [m for m in sys.modules if m.startswith("backend")]:
        del sys.modules[name]
    return importlib.import_module("backend.config")


def check_vocabulary(cfg: Any, fails: list[str]) -> None:
    """Every product's units are the ones read off the mount.

    Args:
        cfg: The loaded config module.
        fails: Collects failure messages.
    """
    for pid, want in EXPECTED_AQPI.items():
        got = cfg.product_units(pid)
        if sorted(got) != sorted(want):
            fails.append(f"{pid}: units {got}, expected {sorted(want)}")
            continue
        canonical = str(cfg.PRODUCTS[pid]["unit"])
        if got[0] != canonical:
            fails.append(
                f"{pid}: {got[0]!r} is listed first, not the canonical "
                f"{canonical!r}; the L1 checks read the canonical unit and a "
                f"picker that defaults elsewhere moves what they measure")
        for label, suffix in want.items():
            if cfg.unit_suffix(pid, label) != suffix:
                fails.append(
                    f"{pid}/{label}: resolves to "
                    f"{cfg.unit_suffix(pid, label)!r}, expected {suffix!r}")
    for pid in EXPECTED_NO_UNITS:
        if pid not in cfg.PRODUCTS:
            continue
        if cfg.product_units(pid):
            fails.append(
                f"{pid}: now offers {cfg.product_units(pid)}; if the mount "
                f"really grew a second unit, read it before trusting it")


def check_paths(cfg: Any, fails: list[str]) -> None:
    """A unit reaches both the manifest and the image subdirectory.

    Resolving one and not the other is the failure that reads a millimeter
    manifest and then fetches inch frames by the names it found.

    Args:
        cfg: The loaded config module.
        fails: Collects failure messages.
    """
    for pid, want in EXPECTED_AQPI.items():
        for label, suffix in want.items():
            man = cfg.manifest_rel(pid, label)
            img = cfg.image_path(pid, "frame.png", label)
            if f"details_{suffix}.json" not in man:
                fails.append(f"{pid}/{label}: manifest is {man!r}")
            if f"/{suffix}/frame.png" not in img:
                fails.append(f"{pid}/{label}: image is {img!r}")
        # The canonical unit must resolve the same way whether it is named
        # or left out, or the cache would hold the same frames twice.
        canonical = cfg.product_units(pid)[0]
        if cfg.manifest_rel(pid, None) != cfg.manifest_rel(pid, canonical):
            fails.append(
                f"{pid}: the default manifest differs from the canonical "
                f"one, {cfg.manifest_rel(pid, None)!r} against "
                f"{cfg.manifest_rel(pid, canonical)!r}")
        if (cfg.image_path(pid, "f.png", None)
                != cfg.image_path(pid, "f.png", canonical)):
            fails.append(f"{pid}: the default image path differs")


def check_refusals(cfg: Any, fails: list[str]) -> None:
    """An unknown unit is refused rather than silently ignored.

    Args:
        cfg: The loaded config module.
        fails: Collects failure messages.
    """
    cases = [
        ("qpe_1hr", "cm"),
        ("qpe_1hr", "in/h"),       # the rate label on a depth product
        ("precip_rate_radar", "mm"),  # the depth label on a rate product
        ("qpe_1hr", ""),
        ("comp_ref", "mm"),        # a product with no units at all
    ]
    for pid, unit in cases:
        if pid not in cfg.PRODUCTS:
            continue
        for fn, name in ((cfg.unit_suffix, "unit_suffix"),
                         (cfg.manifest_rel, "manifest_rel")):
            try:
                got = fn(pid, unit)
            except ValueError:
                continue
            fails.append(f"{name}({pid!r}, {unit!r}) returned {got!r} "
                         f"instead of refusing")
        try:
            got = cfg.image_path(pid, "f.png", unit)
        except ValueError:
            continue
        fails.append(f"image_path({pid!r}, {unit!r}) returned {got!r} "
                     f"instead of refusing")


def check_state(cfg: Any, fails: list[str]) -> None:
    """Every offered unit carries what the mount held when it was read.

    A unit with no recorded state is one nobody checked, and the picker
    cannot warn about a staleness it was never told about.

    Args:
        cfg: The loaded config module.
        fails: Collects failure messages.
    """
    stale = 0
    for pid in EXPECTED_AQPI:
        state = cfg.PRODUCT_UNIT_STATE.get(pid)
        if not state:
            fails.append(f"{pid}: offers units with no recorded state")
            continue
        for label in cfg.product_units(pid):
            value = state.get(label)
            if not value:
                fails.append(f"{pid}/{label}: no recorded state")
            elif value == "live":
                pass
            elif value.startswith("stale:") and len(value) == len("stale:") + 10:
                stale += 1
            else:
                fails.append(f"{pid}/{label}: state {value!r} is neither "
                             f"'live' nor 'stale:YYYY-MM-DD'")
        canonical = cfg.product_units(pid)[0]
        if state.get(canonical) != "live":
            fails.append(
                f"{pid}: the canonical unit {canonical!r} is recorded as "
                f"{state.get(canonical)!r}; the L1 thresholds are "
                f"characterized against it")
    if stale == 0:
        fails.append(
            "no unit is recorded stale, though three were on 2026-11-21; "
            "if the mount caught up, reread it and update the dates rather "
            "than deleting the check")


def main() -> int:
    """Check both profiles and report.

    Returns:
        Process exit status.
    """
    fails: list[str] = []
    aqpi = load("aqpi")
    for fn in (check_vocabulary, check_paths, check_refusals, check_state):
        fn(aqpi, fails)
    offered = {p: aqpi.product_units(p) for p in aqpi.PRODUCTS
               if aqpi.product_units(p)}
    print(f"aqpi: {len(offered)} products with units")
    for pid, units in offered.items():
        state = aqpi.PRODUCT_UNIT_STATE.get(pid, {})
        print(f"   {pid:24} {units}  {[state.get(u) for u in units]}")

    xqpi = load("xqpi")
    x_offered = [p for p in xqpi.PRODUCTS if xqpi.product_units(p)]
    print(f"xqpi: {len(x_offered)} products with units")
    if x_offered:
        fails.append(
            f"xqpi now offers units for {x_offered}; its tree was read on "
            f"2026-11-21 and had no per-unit subdirectories")

    if fails:
        print("\nFAILED:")
        for f in fails:
            print(f"  {f}")
        return 1
    print("\nok: the vocabulary, the paths, the refusals and the recorded "
          "state all match what the mount held")
    return 0


if __name__ == "__main__":
    sys.exit(main())
