"""Desktop and mobile must encode a timeline cell the same way.

Run directly (no DB, no network):

    python validation_tests/test_timeline_surface_parity.py

Both surfaces draw the same thing: a bucket of check runs, coloured by the
worst status in it and filled in proportion to how much of it was at that
status. They are separate Svelte routes, so nothing stops one from being
updated and the other left behind — which is exactly what happened. The
proportional-fill encoding shipped on desktop while mobile kept a flat colour
and its own private STATUS_BG, so for a while the same bucket looked like a
total outage on a phone and a thin sliver on a laptop.

That failure is silent. Neither page errors, neither test fails, and the only
symptom is two people looking at the same incident and disagreeing about how
bad it was.

So this asserts the mechanism rather than the appearance: both surfaces must
import the shared encoding, and neither may define a competing colour map or
inline the fill logic itself. Appearance is checked by the unit tests over
$lib/timelineFill; this checks that both pages actually use it.
"""
from __future__ import annotations
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LIB = ROOT / "frontend/src/lib/timelineFill.ts"
SURFACES = {
    "desktop": ROOT / "frontend/src/routes/timeline/+page.svelte",
    "mobile":  ROOT / "frontend/src/routes/m/uptime/+page.svelte",
}

failures: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if cond else 'FAIL'}  {label}" + (f"  — {detail}" if detail else ""))
    if not cond:
        failures.append(label)


def main() -> int:
    check("the shared encoding module exists", LIB.exists(), str(LIB))
    if not LIB.exists():
        return 1
    lib_src = LIB.read_text()
    for fn in ("cellFill", "STATUS_BG", "cellRatioText", "badFraction"):
        check(f"$lib/timelineFill exports {fn}",
              re.search(rf"export (?:const|function) {fn}\b", lib_src) is not None)

    for name, path in SURFACES.items():
        print(f"\n  [{name}] {path.relative_to(ROOT)}")
        if not path.exists():
            check(f"{name} surface exists", False, str(path))
            continue
        src = path.read_text()

        check(f"{name} imports the shared encoding",
              "from '$lib/timelineFill'" in src)
        check(f"{name} draws cells with cellFill()",
              re.search(r"\bcellFill\s*\(", src) is not None)

        # A private colour table is how the two drifted apart the first time:
        # mobile had its own, missing `unknown`, and updating one did nothing
        # to the other.
        check(f"{name} defines no competing STATUS_BG",
              re.search(r"const\s+STATUS_BG\s*(?::|=)", src) is None,
              "found a local status colour map")

        # Re-implementing the fill inline would pass the import check above
        # while still drifting, so look for the giveaway.
        check(f"{name} does not inline its own gradient fill",
              "linear-gradient(to top" not in src,
              "found an inline cell gradient")

        # The opacity encoding this replaced: dimming red on a dark canvas
        # reads as "dark red", not "less red". It must not come back on either
        # surface.
        cell_opacity = re.search(r"opacity:\s*\{?\s*cell(Density|Fill|Ratio)", src)
        check(f"{name} does not encode amount as opacity", cell_opacity is None,
              cell_opacity.group(0) if cell_opacity else "")

        # The ratio must be reachable. Grepping for the identifier anywhere is
        # too weak — it passes if the call sits in a tooltip a touch device can
        # never show. On mobile the accessible label is the only place the
        # numbers can actually live, so check the placement, not the presence.
        check(f"{name} surfaces the underlying ratio to the reader",
              "cellRatioText" in src)
        if name == "mobile":
            aria = re.search(r'aria-label="[^"]*"', src)
            check("mobile puts the ratio in the accessible label, not only a tooltip",
                  aria is not None and "cellRatioText" in aria.group(0),
                  (aria.group(0)[:80] + "…") if aria else "no aria-label found")

    print(f"\n{len(failures)} FAILED: {', '.join(failures)}" if failures
          else "\nPASS — both surfaces share one timeline-cell encoding")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
