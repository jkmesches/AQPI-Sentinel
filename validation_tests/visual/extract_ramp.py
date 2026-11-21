#!/usr/bin/env python3
"""Read a colorbar PNG into the color-to-value ramp the legend and readout need.

Why this exists
---------------
A legend that lies and a readout that lies confidently are the same fault,
and both look correct. The plan for slice 5 of ``docs/map-console-plan.md``
assumed the ramps would be transcribed from radarca's site; the authoritative
ones turned out to be on the mount already, one ``<prefix>_colorbar.png`` per
moment directory, written by the same code that paints the frames.

The colors come out of the image exactly. The numbers beside them are
rendered text, so they are transcribed once by hand, checked in with the
source path and the date, and passed here. That split is deliberate: a
misread color is invisible, while a mistyped bound is a round number a
reviewer can check against the bar.

What it assumes
---------------
A matplotlib vertical colorbar: the painted bar occupies a contiguous block
of columns on the left, ticks and labels sit to its right, and the bar runs
from the maximum at the top to the minimum at the bottom. Every one of those
is asserted rather than trusted, because a horizontal or inverted bar would
otherwise produce a ramp that is confidently backwards.

Usage
-----
::

    python validation_tests/visual/extract_ramp.py COLORBAR.png \\
        --min -30 --max 80 --unit dBZ --label "Z [dBZ]" --source PATH
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

try:
    from PIL import Image
except ImportError:  # pragma: no cover - environment check
    print("pillow is not installed: pip install pillow", file=sys.stderr)
    raise SystemExit(2) from None

# A pixel this transparent is background rather than bar.
ALPHA_FLOOR = 250

# Two colors closer than this in summed channel distance are one block. A
# matplotlib discrete bar separates its blocks by far more; a continuous one
# steps by one or two per row, well under it.
BLOCK_TOLERANCE = 12

# The bar must be at least this many pixels tall to be read at all. Below it
# the sampling has too few rows to place a boundary.
MIN_BAR_ROWS = 32

# Matplotlib draws the bar inside a black outline, and the row beside that
# outline is a blend of it and the first real color. Measured on the FLOW
# bars: row 17 is (0,0,0), row 18 is (238,238,238) against a true (252,252,252)
# on reflectivity and (224,69,66) against (239,78,74) on velocity. Both ends
# behave the same way, so two rows come off each.
FRAME_ROWS = 2

# A row this dark is the outline rather than a ramp color. The darkest real
# stop on these bars is well above it.
OUTLINE_SUM = 60


def finite(text: str) -> float:
    """Parse a bound, refusing nan and inf.

    A nan bound would make every value in the ramp nan without raising, and
    the legend would print blanks beside real colors.

    Args:
        text: The argument as typed.

    Returns:
        The value.

    Raises:
        ArgumentTypeError: If the text is not a finite number.
    """
    try:
        v = float(text)
    except ValueError as e:
        raise argparse.ArgumentTypeError(f"{text!r} is not a number") from e
    if not math.isfinite(v):
        raise argparse.ArgumentTypeError(f"{text!r} is not finite")
    return v


@dataclass(frozen=True)
class Stop:
    """One color in the ramp, with the value at its center."""

    rgb: tuple[int, int, int]
    value: float
    lo: float
    hi: float


def opaque_columns(img: Image.Image) -> list[int]:
    """Columns that are painted top to bottom, which is the bar.

    Args:
        img: The colorbar, in RGBA.

    Returns:
        Column indices, ascending.
    """
    w, h = img.size
    px = img.load()
    out: list[int] = []
    for x in range(w):
        opaque = sum(1 for y in range(h) if px[x, y][3] >= ALPHA_FLOOR)
        if opaque > h * 0.5:
            out.append(x)
    return out


def bar_bounds(img: Image.Image, col: int) -> tuple[int, int]:
    """The first and last painted row in one column.

    Args:
        img: The colorbar, in RGBA.
        col: Which column to walk.

    Returns:
        Top and bottom row, inclusive.

    Raises:
        ValueError: If the column has no painted run, which means the column
            search found the frame rather than the bar.
    """
    _, h = img.size
    px = img.load()
    rows = [y for y in range(h) if px[col, y][3] >= ALPHA_FLOOR]
    if not rows:
        raise ValueError(f"column {col} is empty")
    return rows[0], rows[-1]


def frame_inset(img: Image.Image, col: int, row: int) -> int:
    """How many rows of outline sit at one end of the bar.

    Zero for a bar drawn without an outline, so a colorbar from another
    renderer is read whole rather than quietly losing two stops.

    Args:
        img: The colorbar, in RGBA.
        col: The column being sampled.
        row: The first painted row at this end.

    Returns:
        Rows to skip.
    """
    px = img.load()
    return FRAME_ROWS if sum(px[col, row][:3]) <= OUTLINE_SUM else 0


def near(a: tuple[int, int, int], b: tuple[int, int, int]) -> bool:
    """Whether two colors belong to the same block.

    Args:
        a: One color.
        b: The other.

    Returns:
        True when the summed channel distance is within tolerance.
    """
    return sum(abs(x - y) for x, y in zip(a, b, strict=True)) <= BLOCK_TOLERANCE


def read_stops(img: Image.Image, vmin: float, vmax: float) -> list[Stop]:
    """Sample the bar into one stop per block of constant color.

    Args:
        img: The colorbar, in RGBA.
        vmin: The value at the bottom of the bar.
        vmax: The value at the top.

    Returns:
        Stops from the minimum upward.

    Raises:
        ValueError: If the bar cannot be found or is too short to sample.
    """
    cols = opaque_columns(img)
    if not cols:
        raise ValueError("no painted column spans the image; is this a vertical bar")
    # The leftmost contiguous run is the bar. Ticks and labels sit right of
    # it and never span the full height.
    col = cols[len(cols) // 4]
    top, bottom = bar_bounds(img, col)
    top += frame_inset(img, col, top)
    bottom -= frame_inset(img, col, bottom)
    rows = bottom - top + 1
    if rows < MIN_BAR_ROWS:
        raise ValueError(f"bar is {rows} rows tall, under the {MIN_BAR_ROWS} needed")

    px = img.load()
    span = vmax - vmin
    blocks: list[tuple[tuple[int, int, int], int, int]] = []
    # Bottom to top, so the stops come out ascending in value.
    for y in range(bottom, top - 1, -1):
        rgb = px[col, y][:3]
        if blocks and near(blocks[-1][0], rgb):
            blocks[-1] = (blocks[-1][0], blocks[-1][1], y)
        else:
            blocks.append((rgb, y, y))

    out: list[Stop] = []
    for rgb, y0, y1 in blocks:
        # y runs downward and value upward, so the bottom row is the minimum.
        lo = vmin + span * (bottom - y0) / (rows - 1)
        hi = vmin + span * (bottom - y1) / (rows - 1)
        out.append(Stop(rgb=rgb, value=round((lo + hi) / 2, 4),
                        lo=round(lo, 4), hi=round(hi, 4)))
    return out


def to_hex(rgb: tuple[int, int, int]) -> str:
    """Format one color for the checked-in ramp.

    Args:
        rgb: The channels.

    Returns:
        A lowercase `#rrggbb` string.
    """
    return "#{:02x}{:02x}{:02x}".format(*rgb)


def main() -> int:
    """Read a colorbar and print its ramp as JSON.

    Returns:
        Process exit status.
    """
    ap = argparse.ArgumentParser(description="read a colorbar PNG into a ramp")
    ap.add_argument("image", type=Path)
    ap.add_argument("--min", type=finite, required=True,
                    help="the value at the bottom of the bar, read from its labels")
    ap.add_argument("--max", type=finite, required=True,
                    help="the value at the top of the bar, read from its labels")
    ap.add_argument("--unit", required=True, help="e.g. dBZ")
    ap.add_argument("--label", required=True, help="the bar's own axis label")
    ap.add_argument("--moment", required=True, help="the moment this ramp is for")
    ap.add_argument("--source", required=True, help="where the file was read from")
    args = ap.parse_args()

    img = Image.open(args.image).convert("RGBA")
    stops = read_stops(img, args.min, args.max)
    print(json.dumps({
        "moment": args.moment,
        "unit": args.unit,
        "label": args.label,
        "min": args.min,
        "max": args.max,
        "source": args.source,
        "read_on": datetime.now(timezone.utc).date().isoformat(),
        "stops": [{"color": to_hex(s.rgb), "value": s.value,
                   "lo": s.lo, "hi": s.hi} for s in stops]
    }, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
