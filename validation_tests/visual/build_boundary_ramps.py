#!/usr/bin/env python3
"""Write the ramps whose frames are painted through a matplotlib BoundaryNorm.

Why this exists
---------------
The AQPI X-band reflectivity ramp shipped in v0.15.0 was read off the
published colorbar, 22 equal bins over -20 to 80 dBZ. The frames are not
painted that way. The plotting script passes ``norm_dbz = BoundaryNorm([-25,
-20, ..., 75], 22, extend="both")`` to the frame call and no norm to the
colorbar call, so Py-ART draws the bar under a linear Normalize(-20, 80) and
the frames under the BoundaryNorm. The readout was wrong by about 9.5 dBZ at
the bottom of the scale and about 1 dBZ at the top, on every X-band radar and
on the C-band, whose own script uses the same norm.

A color match against real frames cannot see that, because both readings
use the same 22 colors. Two other things settled it. The AQPI Backend
session read the norm and the plot calls on the producer host on 2026-11-23,
and a gate-to-pixel check against two DROPS volumes put each color's gate
values inside the BoundaryNorm bins (e.g., #0300fc over gates with medians of
16.3 and 16.6 dBZ, 86% inside 15 to 20 and none inside the bar's 20.9 to
25.5).

How a BoundaryNorm maps a value
-------------------------------
With boundaries b0 < b1 < ... < bM and extend "both", color 0 takes
everything below b0, color i takes [b(i-1), bi) for i from 1 to M, and the
last color takes bM and above. "min" and "max" add only one end, and
"neither" with clip True folds values past the ends into the end bins. The
open stops are given a band one boundary step wide past their limit, so the
legend can draw them, and are marked open so the readout names them as such.

Usage
-----
::

    python validation_tests/visual/build_boundary_ramps.py [OUT_DIR]

OUT_DIR defaults to ``frontend/src/lib/map/ramps/data``.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DEFAULT_OUT = REPO / "frontend/src/lib/map/ramps/data"
HEX_RE = re.compile(r"^#[0-9a-f]{6}$")
EXTENDS = ("neither", "min", "max", "both")


@dataclass(frozen=True)
class BoundaryScale:
    """One moment's colormap as the renderer builds it."""

    name: str
    moment: str
    unit: str
    label: str
    boundaries: tuple[float, ...]
    colors: tuple[str, ...]
    extend: str
    clip: bool
    source: str
    read_on: str
    notes: tuple[str, ...]


SCALES: tuple[BoundaryScale, ...] = (
    BoundaryScale(
        name="aqpi-xband-reflectivity",
        moment="CorrReflectivity",
        unit="dBZ",
        label="Z [dBZ]",
        boundaries=tuple(float(b) for b in range(-25, 80, 5)),
        colors=(
            "#ccefff", "#cabfcc", "#987f98", "#643266", "#cccbb3", "#988f66",
            "#636363", "#04e8e6", "#018ffc", "#0300fc", "#02fc02", "#01c501",
            "#008d00", "#fcf902", "#e5bb00", "#fd9400", "#fd0000", "#d30000",
            "#bb0000", "#f700fc", "#9853c5", "#fcfcfc",
        ),
        extend="both",
        clip=False,
        source=(
            "aqpi_xband_realtime_volume_plotting.py:87 norm_dbz = "
            "BoundaryNorm(dbz_levels, cmap_dbz_custom.N, extend=\"both\"), "
            "dbz_levels -25 to 75 by 5 at line 82 and the 22 colors at line 75, "
            "passed to the frame's plot_ppi at lines 259 to 267. The C-band's "
            "c_band_plotting.py:227 builds the same norm. Read on the producer "
            "host by the AQPI Backend session"),
        read_on="2026-11-23",
        notes=(
            "Not the published colorbar, which is drawn without the norm under "
            "a linear -20 to 80 and reads up to 9.5 dBZ above the frames.",
            "Shared by the five X-band radars and the C-band.",
        ),
    ),
)


def regions(scale: BoundaryScale) -> list[tuple[float, float, str | None]]:
    """Each color's value range, in color order.

    Args:
        scale: The colormap and norm.

    Returns:
        One (lo, hi, open) per color, open being "below", "above" or None.

    Raises:
        ValueError: If the boundaries do not increase, the extend is unknown,
            or the color count does not match the number of regions.
    """
    b = scale.boundaries
    if len(b) < 2 or any(hi <= lo for lo, hi in pairwise(b)):
        raise ValueError(f"{scale.name}: boundaries must increase: {b}")
    if scale.extend not in EXTENDS:
        raise ValueError(f"{scale.name}: extend {scale.extend!r} not in {EXTENDS}")
    out: list[tuple[float, float, str | None]] = [
        (lo, hi, None) for lo, hi in pairwise(b)]
    if scale.extend in ("min", "both"):
        out.insert(0, (b[0] - (b[1] - b[0]), b[0], "below"))
    elif scale.clip:
        out[0] = (out[0][0], out[0][1], "below")
    if scale.extend in ("max", "both"):
        out.append((b[-1], b[-1] + (b[-1] - b[-2]), "above"))
    elif scale.clip:
        out[-1] = (out[-1][0], out[-1][1], "above")
    if len(out) != len(scale.colors):
        raise ValueError(f"{scale.name}: {len(scale.colors)} colors for "
                         f"{len(out)} regions")
    return out


def build(scale: BoundaryScale) -> dict[str, object]:
    """Lay one scale out as the ramp JSON the frontend reads.

    Args:
        scale: The colormap and norm.

    Returns:
        The ramp, ready for ``json.dumps``.

    Raises:
        ValueError: If a color is not lowercase ``#rrggbb`` or the regions
            do not fit the colors.
    """
    bad = [c for c in scale.colors if HEX_RE.match(c) is None]
    if bad:
        raise ValueError(f"{scale.name}: not lowercase #rrggbb: {bad}")
    stops: list[dict[str, object]] = []
    for color, (lo, hi, side) in zip(scale.colors, regions(scale), strict=True):
        stop: dict[str, object] = {"color": color, "value": (lo + hi) / 2,
                                   "lo": lo, "hi": hi}
        if side is not None:
            stop["open"] = side
        stops.append(stop)
    return {
        "_comment": list(scale.notes),
        "moment": scale.moment,
        "unit": scale.unit,
        "label": scale.label,
        "min": stops[0]["lo"],
        "max": stops[-1]["hi"],
        "source": scale.source,
        "read_on": scale.read_on,
        "built_from": {
            "kind": f"BoundaryNorm(extend={scale.extend}, clip={scale.clip})",
            "anchors": list(scale.colors),
            "levels": len(scale.colors),
            "vmin": scale.boundaries[0],
            "vmax": scale.boundaries[-1],
        },
        "stops": stops,
    }


def main() -> int:
    """Write every BoundaryNorm ramp.

    Returns:
        0 when every file was written, 2 when the output is not a directory.
    """
    ap = argparse.ArgumentParser(description="write the BoundaryNorm ramps")
    ap.add_argument("out", type=Path, nargs="?", default=DEFAULT_OUT)
    args = ap.parse_args()
    out: Path = args.out
    if not out.is_dir():
        print(f"not a directory: {out}", file=sys.stderr)
        return 2
    for scale in SCALES:
        path = out / f"{scale.name}.json"
        _ = path.write_text(json.dumps(build(scale), indent=1) + "\n", encoding="utf-8")
        print(f"wrote {path.relative_to(REPO) if path.is_relative_to(REPO) else path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
