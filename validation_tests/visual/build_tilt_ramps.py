#!/usr/bin/env python3
"""Write the three tilt ramps from the renderer's own colormap and limits.

Why this exists
---------------
The tilt frames come from radar-display's renderer, not from Chivo, and
paint a different field from a different table over a different range. The
Chivo ramps cannot read them.

The renderer's published colorbar is no source for the range either. It is
drawn from ``reflectivity_limits = [-35.0, 80.0]`` in colorscales.py while
the frames are painted with ``ZE,-30,85`` from the command line, so the bar
reads 5 dBZ below every frame. The same 23 colors at the same 5 dBZ width
fit both ranges, so a color match against real frames cannot tell them
apart. The values here come from the plotting call, and the colors from the
colorscales lists it passes to ``ListedColormap``.

Neither was read by this tool. The AQPI Backend session read the plotting
script and colorscales.py on the X-band producer host on 2026-11-22 and
recorded them in the AQPI wiki's data-flow page, under its tilt frame
renderer section. Every color below then matched 100% of the opaque pixels
in nine sampled frames, which proves the palette and says nothing about the
range.

How the renderer maps a value
-----------------------------
``pcolormesh(vmin, vmax, cmap=ListedColormap(colors))`` with the default
linear ``Normalize``. A value v lands in bin ``floor((v - vmin) / (vmax -
vmin) * N)``, so bin k covers ``[vmin + k*w, vmin + (k+1)*w)`` for a width
``w = (vmax - vmin) / N``. A value below vmin paints the first color and one
at or above vmax the last, so the two end stops are open on their outer
side. Masked gates are transparent.

Usage
-----
::

    python validation_tests/visual/build_tilt_ramps.py [OUT_DIR]

OUT_DIR defaults to ``frontend/src/lib/map/ramps/data``.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DEFAULT_OUT = REPO / "frontend/src/lib/map/ramps/data"
READ_ON = "2026-11-22"

SOURCE = (
    "radar-display tilt renderer: pcolormesh over a ListedColormap of the "
    "colorscales.py list, vmin and vmax from the six plotting scripts' "
    "command line (ZE,-30,85 VE,-35,35 RHV,0.5,1.0). Read from the code by the "
    "AQPI Backend session, recorded in the AQPI wiki data-flow page "
    "(tilt frame renderer)")

HEX_RE = re.compile(r"^#[0-9a-f]{6}$")


@dataclass(frozen=True)
class TiltScale:
    """One moment's colormap, as the renderer passes it to pcolormesh."""

    name: str
    moment: str
    unit: str
    label: str
    vmin: float
    vmax: float
    colors: tuple[str, ...]
    notes: tuple[str, ...]


SCALES: tuple[TiltScale, ...] = (
    TiltScale(
        name="tilt-reflectivity",
        moment="reflectivity",
        unit="dBZ",
        label="Z, uncorrected [dBZ]",
        vmin=-30.0,
        vmax=85.0,
        colors=(
            "#646464", "#ccffff", "#cc99cc", "#996699", "#663366", "#cccc99",
            "#999966", "#646464", "#04e9e7", "#019ff4", "#0300f4", "#02fd02",
            "#01c501", "#008e00", "#fdf802", "#e5bc00", "#fd9500", "#fd0000",
            "#d40000", "#bc0000", "#f800fd", "#9854c6", "#fdfdfd",
        ),
        notes=(
            "The field is the radar's uncorrected Reflectivity, not the "
            "attenuation-corrected CorrReflectivity the Chivo frames show.",
            "#646464 paints two bands, below -25 and 5 to 10, so a gray pixel "
            "reads as both.",
            "The renderer's published colorbar is labeled -35 to 80 and reads "
            "5 dBZ below these frames. If the plotting scripts move to "
            "ZE,-35,80, this ramp must move with them the same day.",
        ),
    ),
    TiltScale(
        name="tilt-velocity",
        moment="velocity",
        unit="m/s",
        label="V [m/s]",
        vmin=-35.0,
        vmax=35.0,
        colors=(
            "#0000ff", "#3232ff", "#4b4bff", "#6464ff", "#7d7dff", "#9696ff",
            "#afafff", "#f0e1f0", "#ffafaf", "#ff9696", "#ff7d7d", "#ff6464",
            "#ff4b4b", "#ff3232", "#ff0000",
        ),
        notes=("The colorbar and the plotting call agree on -35 to 35.",),
    ),
    TiltScale(
        name="tilt-copolarcorrelation",
        moment="copolarcorrelation",
        unit="",
        label="RhoHV (CrossPolCorrelation)",
        vmin=0.5,
        vmax=1.0,
        colors=(
            "#d2d2d2", "#22c8fc", "#1a9df2", "#0f3cf1", "#28f32c", "#1cbc20",
            "#118b15", "#08590a", "#fef937", "#e5b329", "#fd9527", "#fc291c",
            "#b21a10", "#e44a8c", "#772f9e", "#000000",
        ),
        notes=(
            "The renderer's CrossPolCorrelation field, which the Chivo tree "
            "does not publish for the X-band radars.",
            "The colorbar and the plotting call agree on 0.5 to 1.0.",
        ),
    ),
)


def build(scale: TiltScale) -> dict[str, object]:
    """Lay one colormap out as the ramp JSON the frontend reads.

    Args:
        scale: The moment's colors and limits.

    Returns:
        The ramp, ready for ``json.dumps``.

    Raises:
        ValueError: If the limits are not an increasing pair or a color is
            not lowercase ``#rrggbb``.
    """
    if not scale.vmax > scale.vmin:
        raise ValueError(f"{scale.name}: vmax {scale.vmax} <= vmin {scale.vmin}")
    bad = [c for c in scale.colors if HEX_RE.match(c) is None]
    if bad:
        raise ValueError(f"{scale.name}: not lowercase #rrggbb: {bad}")
    count = len(scale.colors)
    width = (scale.vmax - scale.vmin) / count
    stops: list[dict[str, object]] = []
    for k, color in enumerate(scale.colors):
        lo = scale.vmin + k * width
        hi = scale.vmin + (k + 1) * width
        stop: dict[str, object] = {
            "color": color,
            "value": round((lo + hi) / 2, 4),
            "lo": round(lo, 4),
            "hi": round(hi, 4),
        }
        # Normalize clips rather than masks, so the end colors also stand
        # for everything past their limit.
        if k == 0:
            stop["open"] = "below"
        elif k == count - 1:
            stop["open"] = "above"
        stops.append(stop)
    ramp: dict[str, object] = {
        "_comment": list(scale.notes),
        "moment": scale.moment,
        "unit": scale.unit,
        "label": scale.label,
        "min": scale.vmin,
        "max": scale.vmax,
        "source": SOURCE,
        "read_on": READ_ON,
        # Lets the table be rebuilt without the renderer's host, and selects
        # the per-stop match rule that keeps a reused color exact.
        "built_from": {
            "kind": "ListedColormap",
            "anchors": list(scale.colors),
            "levels": count,
            "vmin": scale.vmin,
            "vmax": scale.vmax,
        },
        "stops": stops,
    }
    if not scale.unit:
        ramp["dimensionless"] = True
    return ramp


def main() -> int:
    """Write every tilt ramp.

    Returns:
        0 when every file was written.
    """
    ap = argparse.ArgumentParser(description="write the tilt ramps")
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
