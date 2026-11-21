#!/usr/bin/env python3
r"""Does a ramp read a colorbar's own printed numbers back correctly?

Why this exists
---------------
``extract_ramp.py`` is told the value at each end of the bar and the shape
of the axis between them, and it believes all three. A wrong bound or a
linear reading of a logarithmic axis produces a ramp that inverts every
color to a plausible, wrong number, and nothing downstream can tell.

radarca's accumulation bars are logarithmic and were nearly read as
linear, which would have reported 10 inches of rain where the bar means
0.41. This is the check that catches that class of error: the bar prints
its own scale beside itself, so the ramp is asked to reproduce those
numbers from the colors at those exact rows.

How it works
------------
The printed labels are found as bands of dark ink in the gutter right of
the bar, and matplotlib centers each label on its tick. The bar's color
at each of those rows goes through the shipped ``invert.ts``, and the
value that comes back is compared with the number that is printed there.

Usage
-----
::

    python validation_tests/visual/check_ramp_ticks.py BAR.png RAMP.json \\
        --labels 75,70,65,...,-25 [--tolerance 0.02]
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any

from bar_geometry import bar_columns, bar_rows, label_rows
from ramp_coverage import invert_all

try:
    from PIL import Image
except ImportError:  # pragma: no cover - environment check
    print("pillow is not installed: pip install pillow", file=sys.stderr)
    raise SystemExit(2) from None

# How far a reading may sit from its printed number, as a fraction of the
# bar's full span. One part in fifty is wider than the error from landing
# a tick one pixel off a 640-row bar and far tighter than the error a
# wrong bound or a wrong axis shape produces.
DEFAULT_TOLERANCE = 0.02

# How many rows of outline matplotlib draws around a colorbar, which
# `extract_ramp.py` takes off each end for the same reason.
OUTLINE_ROWS = 2


def finite(text: str) -> float:
    """Parse a command-line number, refusing nan and inf.

    Args:
        text: The argument as typed.

    Returns:
        The value.

    Raises:
        argparse.ArgumentTypeError: If it is not a finite number.
    """
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not a number") from None
    if not math.isfinite(value):
        raise argparse.ArgumentTypeError(f"{text!r} is not finite")
    return value


def sample_bar(im: Image.Image, left: int, right: int,
               rows: list[int]) -> list[list[int]]:
    """The bar's color at each of these rows.

    A tick at either end of the bar is level with the outline that
    matplotlib draws around it, and the ramp holds no stop for an
    outline, so a sample taken there matches nothing. Samples are pulled
    inside the painted area by the width of that outline.

    Args:
        im: The colorbar, RGBA.
        left: The bar's first column.
        right: The bar's last column.
        rows: Which rows to sample.

    Returns:
        One [r, g, b, a] per row, taken from the middle of the bar.
    """
    px = im.load()
    mid = (left + right) // 2
    top, bottom = bar_rows(im, left, right)
    lo = top + OUTLINE_ROWS
    hi = bottom - OUTLINE_ROWS
    return [list(px[mid, min(max(y, lo), hi)]) for y in rows]


def slack(ramp: dict[str, Any], want: float, tolerance: float) -> float:
    """How far from a printed number a reading may sit.

    Measured in the axis the bar is drawn in. A fraction of the range
    is right on a linear bar and meaningless on a log one, where the
    same fraction is a rounding error at the bottom and a factor of two
    at the top: a fiftieth of the 0.25 to 500 mm bar is 10 mm, which
    passes anything above 400.

    Args:
        ramp: The ramp, for its bounds and axis.
        want: The printed number this slack is for.
        tolerance: The allowance as a fraction of the axis.

    Returns:
        A distance in the value's own units.
    """
    lo, hi = float(ramp["min"]), float(ramp["max"])
    if ramp.get("scale") != "log":
        return abs(hi - lo) * tolerance
    decades = abs(math.log10(hi) - math.log10(lo))
    return abs(want) * (10 ** (decades * tolerance) - 1)


def report(labels: list[float], readings: list[dict[str, float] | None],
           ramp: dict[str, Any], tolerance: float) -> list[str]:
    """Compare what the bar prints with what the ramp reads.

    Args:
        labels: The printed numbers, top to bottom.
        readings: What invert returned for each label's color.
        ramp: The ramp, for its bounds and axis.
        tolerance: Allowed error as a fraction of the axis.

    Returns:
        One message per label the ramp does not reproduce.
    """
    out: list[str] = []
    for want, got in zip(labels, readings, strict=True):
        allowed = slack(ramp, want, tolerance)
        if got is None:
            out.append(f"the color at the {want} tick matched no stop")
            continue
        # The band rather than its middle. A tick marks a boundary
        # between two blocks, so on a discrete bar the color at that row
        # belongs to one of them and its midpoint sits half a block away
        # by construction. Containment asks the question that has an
        # answer on both bar kinds.
        lo, hi = got.get("lo"), got.get("hi")
        if lo is None or hi is None:
            out.append(f"the reading at the {want} tick carries no band")
        elif not (lo - allowed <= want <= hi + allowed):
            out.append(
                f"the bar prints {want} and the ramp puts that color at "
                f"{lo:.4g} to {hi:.4g}, outside it by "
                f"{min(abs(want - lo), abs(want - hi)):.4g} against the "
                f"{allowed:.4g} allowed")
    return out


def main() -> int:
    """Check one ramp against its bar's printed ticks.

    Returns:
        Process exit status, non-zero when a tick does not read back.
    """
    ap = argparse.ArgumentParser(
        description="check a ramp against its colorbar's printed numbers")
    ap.add_argument("image", type=Path)
    ap.add_argument("ramp", type=Path)
    ap.add_argument("--labels", required=True,
                    help="the printed numbers, top to bottom, comma separated")
    ap.add_argument("--tolerance", type=finite, default=DEFAULT_TOLERANCE,
                    help="allowed error as a fraction of the bar's axis")
    args = ap.parse_args()

    labels = [float(v) for v in args.labels.split(",")]
    im = Image.open(args.image).convert("RGBA")
    left, right = bar_columns(im)
    rows = label_rows(im, right, bar_rows(im, left, right))
    if len(rows) != len(labels):
        print(f"{len(labels)} labels given and {len(rows)} found beside the "
              f"bar at rows {rows}; they have to pair up", file=sys.stderr)
        return 2
    ramp: dict[str, Any] = json.loads(args.ramp.read_text())
    readings = invert_all(ramp, sample_bar(im, left, right, rows))
    problems = report(labels, readings, ramp, args.tolerance)

    print(f"{args.ramp.name}: {ramp.get('scale', 'linear')} axis, "
          f"{ramp['min']} to {ramp['max']} {ramp['unit']}, "
          f"{len(labels)} ticks checked")
    for want, got in zip(labels, readings, strict=True):
        have = got.get("value") if got else None
        print(f"  prints {want:>8} reads {have}")
    if problems:
        print("\nFAIL:")
        for p in problems:
            print(f"  {p}")
        return 1
    print(f"\nok: every printed number reads back within "
          f"{args.tolerance:.1%} of the bar's axis")
    return 0


if __name__ == "__main__":
    sys.exit(main())
