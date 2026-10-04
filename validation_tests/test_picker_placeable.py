#!/usr/bin/env python3
"""Every product the map offers must be one the map can place.

Run directly (no DB, no network):

    python validation_tests/test_picker_placeable.py

The composite picker used to be a fixed list of AQPI's thirteen products,
rendered whatever the deployment. On the XQPI instance that meant CoSMoS
water-level layers and Bay Area atmospheric forecasts it does not publish,
while two of its own four product ids were missing from the list entirely.

It is now derived, and filtered to products that have a geographic extent —
because a product with no extent can only draw nothing or draw it in the wrong
place. That filter is the point, and it is also the hazard: it is silent. A
product added to the picker without a matching COMP_EXTENT entry does not
error, it just stops appearing in the dropdown, and the next person wonders
where Reflectivity went.

Both tables live inside MapView.svelte, so this reads the source. Parsing a
component is ugly; shipping a picker that can quietly lose an option is worse.
"""
from __future__ import annotations
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MAPVIEW = ROOT / "frontend" / "src" / "lib" / "components" / "MapView.svelte"

failures: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if cond else 'FAIL'}  {label}" + (f"  — {detail}" if detail else ""))
    if not cond:
        failures.append(label)


def _block(src: str, start_marker: str, end_marker: str) -> str:
    i = src.index(start_marker)
    return src[i:src.index(end_marker, i)]


def main() -> int:
    src = MAPVIEW.read_text()

    picker_src = _block(src, "const COMPOSITE_GROUPS", "\n\t];")
    extent_src = _block(src, "const COMP_EXTENT: Record<Composite", "\n\t};")
    picker = set(re.findall(r"key: '([a-z_0-9]+)'", picker_src))
    extent = set(re.findall(r"^\s*([a-z_0-9]+):", extent_src, re.M))

    print("both tables parsed out of MapView.svelte:")
    # If a rename breaks the parse, every set comes back empty and every
    # assertion below passes vacuously — so assert the parse first.
    check("the picker table is non-empty", len(picker) >= 10, f"{len(picker)} options")
    check("the extent table is non-empty", len(extent) >= 10, f"{len(extent)} entries")

    print("\nevery offered product can be placed:")
    missing = sorted(picker - extent)
    check("no picker option lacks an extent", not missing,
          ", ".join(missing) + " would be filtered out of the dropdown silently"
          if missing else "")

    print("\nand the extent table carries nothing the picker cannot reach:")
    orphan = sorted(extent - picker)
    check("no extent entry is unreachable", not orphan, ", ".join(orphan))

    print("\n'none' is in both, because Off is a real choice:")
    check("'none' is an option", "none" in picker)
    check("'none' has an entry, so placeable() short-circuits consistently",
          "none" in extent)

    print(f"\n{len(failures)} FAILED: {', '.join(failures)}" if failures
          else "\nall picker-placeability assertions passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
