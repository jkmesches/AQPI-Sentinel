r"""Where the bar and its printed numbers sit in a colorbar PNG.

Shared by `extract_ramp.py`, which reads the colors into a ramp, and
`check_ramp_ticks.py`, which asks the ramp to reproduce the numbers
printed beside those colors. Both have to agree about which columns are
the bar and which rows the labels sit on, so the geometry lives here
rather than in each of them.

Assumes a matplotlib vertical colorbar: the painted bar is a contiguous
block of columns, its labels are black numerals in the gutter to the
right, and each label is centered on the tick it names.
"""
from __future__ import annotations

import sys

try:
    from PIL import Image
except ImportError:  # pragma: no cover - environment check
    print("pillow is not installed: pip install pillow", file=sys.stderr)
    raise SystemExit(2) from None

# A pixel this transparent is background rather than bar.
ALPHA_FLOOR = 250

# A channel under this counts as label ink. Applied only in the gutter
# right of the bar, never on the bar, which reaches very dark colors.
INK = 120

# How far right of the bar the gutter starts. The tick marks sit in the
# first few columns and are not numerals to be grouped.
GUTTER_PAD = 8

# A band of ink this short is a tick mark or an artifact. The numerals in
# these bars are twenty rows and more.
MIN_LABEL_ROWS = 4

# How far outside the bar a label may still belong to it. A tick at an
# end sits level with that end, give or take the inset taken off for the
# outline. The unit title sits further above than this: on the
# millimeter accumulation bar it is 52 rows clear, and it is wide enough
# to reach the gutter, so without this it reads as a ninth tick.
LABEL_MARGIN_ROWS = 12

# A column counts as part of the bar when it is opaque over this much of
# the image. The title above the bar and the labels beside it never are.
COLUMN_COVERAGE = 0.5


def bar_columns(im: Image.Image) -> tuple[int, int]:
    """The widest run of columns the painted bar occupies.

    Args:
        im: The colorbar, RGBA.

    Returns:
        The first and last column of the bar.

    Raises:
        ValueError: If no column is opaque over most of the image.
    """
    w, h = im.size
    px = im.load()
    on = [sum(1 for y in range(h) if px[x, y][3] >= ALPHA_FLOOR)
          > h * COLUMN_COVERAGE for x in range(w)]
    best = (0, 0, 0)
    run = 0
    for x, flag in enumerate(on):
        run = run + 1 if flag else 0
        if run > best[0]:
            best = (run, x - run + 1, x)
    if best[0] == 0:
        raise ValueError("no column is opaque over half the image; "
                         "is this a vertical colorbar")
    return best[1], best[2]


def bar_rows(im: Image.Image, left: int, right: int) -> tuple[int, int]:
    """The first and last row the painted bar covers.

    Args:
        im: The colorbar, RGBA.
        left: The bar's first column.
        right: The bar's last column.

    Returns:
        The top and bottom row of the bar.

    Raises:
        ValueError: If the bar's middle column is empty.
    """
    px = im.load()
    mid = (left + right) // 2
    # The longest unbroken run, because the unit title is centered above
    # the bar in the same columns and its glyphs are painted too.
    best = (0, 0, 0)
    run = 0
    for y in range(im.size[1]):
        if px[mid, y][3] >= ALPHA_FLOOR:
            run += 1
            if run > best[0]:
                best = (run, y - run + 1, y)
        else:
            run = 0
    if best[0] == 0:
        raise ValueError(f"column {mid} between {left} and {right} is empty")
    return best[1], best[2]


def label_rows(im: Image.Image, right: int,
               extent: tuple[int, int] | None = None) -> list[int]:
    """The center row of each number printed beside the bar.

    Args:
        im: The colorbar, RGBA.
        right: The bar's last column.
        extent: The bar's top and bottom row. Given, ink outside it is
            the unit title rather than a tick label.

    Returns:
        One row per label, top to bottom.
    """
    w, h = im.size
    px = im.load()
    x0 = right + GUTTER_PAD
    if x0 >= w:
        return []
    bands: list[list[int]] = []
    for y in range(h):
        inked = any(
            px[x, y][3] >= ALPHA_FLOOR and max(px[x, y][:3]) < INK
            for x in range(x0, w))
        if not inked:
            continue
        if bands and y - bands[-1][-1] <= 2:
            bands[-1].append(y)
        else:
            bands.append([y])
    rows = [(b[0] + b[-1]) // 2 for b in bands if len(b) >= MIN_LABEL_ROWS]
    if extent is None:
        return rows
    top, bottom = extent
    return [y for y in rows
            if top - LABEL_MARGIN_ROWS <= y <= bottom + LABEL_MARGIN_ROWS]


def tick_fractions(im: Image.Image) -> list[float]:
    """Where each printed label sits along the bar, 0 at the bottom.

    Args:
        im: The colorbar, RGBA.

    Returns:
        One fraction per label, top to bottom.

    Raises:
        ValueError: If the bar cannot be located or has no height.
    """
    left, right = bar_columns(im)
    top, bottom = bar_rows(im, left, right)
    span = bottom - top
    if span <= 0:
        raise ValueError(f"the bar spans {span} rows")
    return [(bottom - y) / span
            for y in label_rows(im, right, (top, bottom))]
