#!/usr/bin/env python3
"""Measure a color ramp against the frames it has to read.

Why this exists
---------------
A ramp that is wrong gives a legend that lies and a readout that lies
confidently, and both look correct. A number beside a cursor carries more
authority than the picture under it, and nobody checks a number against a
color by eye.

So each ramp is measured before the readout is enabled for its moment. This
samples every distinct opaque color in a set of real frames and asks the
shipped `invert` whether each one maps to a stop. Full coverage means the
ramp is the table the producer painted with. Anything short means the ramp
is wrong or incomplete, and the readout stays off for that moment.

The matching runs through `frontend/src/lib/map/ramps/invert.ts` rather than
a copy of its rule here. Two implementations of one rule is the bug this
project keeps finding, most recently a status reducer that existed twice
with only one copy fixed.

Usage
-----
::

    python validation_tests/visual/ramp_coverage.py RAMP.json FRAME.png...
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any, TypeAlias

try:
    from PIL import Image
except ImportError:  # pragma: no cover - environment check
    print("pillow is not installed: pip install pillow", file=sys.stderr)
    raise SystemExit(2) from None

# Both child processes are local and bounded work: a transpile of one file
# and a lookup over a few hundred colors. A hang means something is wrong
# with the toolchain rather than slow, so the wait is short and the failure
# is loud.
CHILD_TIMEOUT_S = 60

Json: TypeAlias = "str | int | float | bool | list[Json] | dict[str, Json] | None"

REPO = Path(__file__).resolve().parents[2]
INVERT_TS = REPO / "frontend/src/lib/map/ramps/invert.ts"
ESBUILD = REPO / "frontend/node_modules/.bin/esbuild"

# Pixels at or below this alpha are outside the radar's coverage. Matches
# TRANSPARENT_ALPHA in invert.ts, and the runner asserts they agree.
TRANSPARENT_ALPHA = 8

# A color appearing fewer times than this across every frame is an
# anti-aliased edge between two bands rather than a painted value. Reported
# separately instead of being dropped, so a ramp that misses a whole band
# cannot hide behind the word "rare".
RARE_PIXELS = 4


def distinct_colors(paths: list[Path]) -> Counter[tuple[int, int, int]]:
    """Count every opaque color across a set of frames.

    Args:
        paths: The frames to read.

    Returns:
        Pixel counts by RGB.

    Raises:
        ValueError: If a frame has no opaque pixel, which means it is blank
            and measures nothing.
    """
    seen: Counter[tuple[int, int, int]] = Counter()
    for p in paths:
        with Image.open(p) as img:
            rgba = img.convert("RGBA")
            opaque = [px for px in rgba.get_flattened_data()
                      if px[3] > TRANSPARENT_ALPHA]
        if not opaque:
            raise ValueError(f"{p.name} has no opaque pixel")
        seen.update(px[:3] for px in opaque)
    return seen


def invert_all(
    ramp: dict[str, Json], colors: list[list[int]]
) -> list[dict[str, float] | None]:
    """Ask the shipped module what each color reads as.

    Args:
        ramp: The ramp, as checked in.
        colors: The colors to look up.

    Returns:
        One reading per color, with None where nothing matched.

    Raises:
        RuntimeError: If the module cannot be built or run.
        subprocess.TimeoutExpired: If either child outlives CHILD_TIMEOUT_S.
    """
    with tempfile.TemporaryDirectory() as tmp:
        bundle = Path(tmp) / "invert.mjs"
        build = subprocess.run(
            [str(ESBUILD), str(INVERT_TS), "--format=esm", f"--outfile={bundle}"],
            capture_output=True, text=True, check=False,
            timeout=CHILD_TIMEOUT_S)
        if build.returncode != 0:
            raise RuntimeError(f"esbuild failed: {build.stderr.strip()}")
        script = Path(tmp) / "run.mjs"
        # Through a file rather than argv. A composite frame carries tens
        # of thousands of distinct colors, and the whole list as one
        # argument passes the kernel's limit and fails with E2BIG, which
        # reads as the ramp being broken rather than the call being too
        # long.
        payload = Path(tmp) / "in.json"
        payload.write_text(json.dumps([ramp, colors]))
        script.write_text(
            f"import {{ invert, TRANSPARENT_ALPHA }} from {str(bundle)!r};\n"
            "import { readFileSync } from 'node:fs';\n"
            "const [ramp, colors] = JSON.parse("
            "readFileSync(process.argv[2], 'utf8'));\n"
            "console.log(JSON.stringify({\n"
            "  alpha: TRANSPARENT_ALPHA,\n"
            "  readings: colors.map((c) => invert(ramp, c))\n"
            "}));\n")
        run = subprocess.run(
            ["node", str(script), str(payload)],
            capture_output=True, text=True, check=False,
            timeout=CHILD_TIMEOUT_S)
        if run.returncode != 0:
            raise RuntimeError(f"invert failed: {run.stderr.strip()}")
        out: dict[str, Any] = json.loads(run.stdout)
    if out["alpha"] != TRANSPARENT_ALPHA:
        raise RuntimeError(
            f"this script treats alpha<= {TRANSPARENT_ALPHA} as absent and the "
            f"module uses {out['alpha']}; they have to agree")
    readings: list[dict[str, float] | None] = out["readings"]
    return readings


def main() -> int:
    """Measure one ramp and report its coverage.

    Returns:
        Process exit status, non-zero when any color fails to map.
    """
    ap = argparse.ArgumentParser(description="measure a ramp against real frames")
    ap.add_argument("ramp", type=Path)
    ap.add_argument("frames", type=Path, nargs="+")
    ap.add_argument("--max-unmatched", type=int, default=0,
                    help="how many distinct colors may fail to map (default 0)")
    args = ap.parse_args()

    ramp: dict[str, Json] = json.loads(args.ramp.read_text())
    stops = ramp.get("stops")
    if not isinstance(stops, list) or not stops:
        print(f"{args.ramp} carries no stops", file=sys.stderr)
        return 2
    counts = distinct_colors(args.frames)
    colors = sorted(counts, key=lambda c: -counts[c])
    readings = invert_all(ramp, [list(c) for c in colors])

    matched = [c for c, r in zip(colors, readings, strict=True) if r is not None]
    unmatched = [c for c, r in zip(colors, readings, strict=True) if r is None]
    pixels = sum(counts.values())
    matched_px = sum(counts[c] for c in matched)

    print(f"ramp     {ramp['moment']} ({ramp['unit']}), {len(stops)} stops")
    print(f"frames   {len(args.frames)}, {pixels:,} opaque pixels")
    print(f"colors   {len(matched)}/{len(colors)} distinct map to a stop")
    print(f"pixels   {matched_px / pixels * 100:.3f}% of painted area reads back")
    for c in unmatched:
        n = counts[c]
        kind = "rare" if n < RARE_PIXELS else "COMMON"
        print(f"  unmatched  #{c[0]:02x}{c[1]:02x}{c[2]:02x}  {n:>7,} px  {kind}")

    if len(unmatched) > args.max_unmatched:
        print(f"\nFAIL: {len(unmatched)} unmatched, "
              f"{args.max_unmatched} allowed; the readout stays off for "
              f"{ramp['moment']}")
        return 1
    print(f"\nok: every distinct color in these frames reads back as "
          f"{ramp['unit']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
