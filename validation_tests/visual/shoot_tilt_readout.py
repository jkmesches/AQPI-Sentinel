#!/usr/bin/env python3
"""Values read off a radar-display tilt, and the switch back to the scan.

Why this exists
---------------
Joseph found on 2026-11-22 that hovering a tilt reported nothing. The
tilt overlay never fed the readout, and the legend went on drawing the
Chivo scale for a frame painted from radar-display's own tables. Those
tables differ from Chivo's in field, range and colors, and the
reflectivity one paints #646464 for two bands.

Each check serves the frames itself, painted in a known stop, so the
expected text is exact and the run does not depend on the weather:

1. The processed scan reads through the Chivo ramp.
2. A tilt reads through the tilt ramp, and the legend names the tilt
   field.
3. The reused gray names both of its bands.
4. Going back to the processed scan reads the scan again, not the tilt
   frame the console held.

Usage
-----
::

    python validation_tests/visual/shoot_tilt_readout.py OUTDIR [--base URL]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Awaitable, Callable
from io import BytesIO
from itertools import pairwise
from pathlib import Path
from typing import Any

VISUAL = Path(__file__).resolve().parent
sys.path.insert(0, str(VISUAL))

try:
    from PIL import Image
except ImportError:  # pragma: no cover - environment check
    print("pillow is not installed: pip install pillow", file=sys.stderr)
    raise SystemExit(2) from None

try:
    from playwright.async_api import Browser, Page, Route, async_playwright
except ImportError:  # pragma: no cover - environment check
    print("playwright is not installed: pip install playwright "
          "&& playwright install chromium", file=sys.stderr)
    raise SystemExit(2) from None

from shoot_radar_readout import read_at, solo  # noqa: E402  sibling module, loaded by path

DEFAULT_BASE = os.environ.get("SENTINEL_VISUAL_BASE", "http://localhost:5173")
RAMPS = VISUAL.parent.parent / "frontend/src/lib/map/ramps/data"
VIEWPORT = {"width": 1500, "height": 950}
LOAD_SETTLE_MS = 12_000
TILT_SETTLE_MS = 6000
SCAN_SIZE = (697, 693)
RADAR = "XSCV"
#: DISCRETE_MEDIAN_STEP in legend.ts.
DISCRETE_MEDIAN_STEP = 20

#: The stops each case paints, by ramp file and index. The scan stops sit
#: 40 and 316 channel steps from every tilt color, so a scan pixel read
#: through a tilt ramp matches nothing rather than a neighbor. Reflectivity
#: tilt stop 11 is #02fd02 (25 to 30 dBZ) and 0 is the gray it shares with
#: stop 7. Velocity 7 is the near-zero band and 0 is open below.
CASES: tuple[dict[str, Any], ...] = (
    {"moment": "Reflectivity", "scan": ("aqpi-xband-reflectivity", 1),
     "tilt": "tilt-reflectivity", "stops": (11, 0)},
    {"moment": "Velocity", "scan": ("aqpi-xband-velocity", 0),
     "tilt": "tilt-velocity", "stops": (7, 0)},
)


def ramp(name: str) -> dict[str, Any]:
    """One checked-in ramp.

    Args:
        name: Its file name without the extension.

    Returns:
        The parsed ramp.
    """
    loaded: dict[str, Any] = json.loads((RAMPS / f"{name}.json").read_text())
    return loaded


def rgb_of(stop: dict[str, Any]) -> tuple[int, int, int]:
    """A stop's color as channels."""
    h = str(stop["color"]).lstrip("#")
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))


def band(stop: dict[str, Any], unit: str, d: int) -> str:
    """One band as readout.ts prints it.

    Args:
        stop: The ramp stop.
        unit: The ramp's unit, which may be empty.
        d: Decimals for the bounds.

    Returns:
        The text, e.g. "below -25.0 dBZ" or "5.0 to 10.0 dBZ".
    """
    def num(v: float) -> str:
        return f"{v:.{d}f} {unit}" if unit else f"{v:.{d}f}"
    if stop.get("open") == "below":
        return f"below {num(stop['hi'])}"
    if stop.get("open") == "above":
        return f"{num(stop['lo'])} or more"
    return f"{stop['lo']:.{d}f} to {num(stop['hi'])}"


def expected(r: dict[str, Any], index: int) -> str:
    """What the readout must print for a pixel painted in one stop.

    Every stop sharing that exact color is named, ascending, which is the
    rule invert.ts and readout.ts implement.

    Args:
        r: The ramp.
        index: The painted stop.

    Returns:
        The text.
    """
    stop = r["stops"][index]
    unit = str(r["unit"])
    if not discrete(r):
        step = abs(float(r["max"]) - float(r["min"])) / len(r["stops"])
        text = f"{float(stop['value']):.{decimals(step)}f}"
        return f"{text} {unit}" if unit else text
    same = sorted((s for s in r["stops"] if s["color"] == stop["color"]),
                  key=lambda s: float(s["lo"]))
    d = decimals(float(stop["hi"]) - float(stop["lo"]))
    return " or ".join(band(s, unit, d) for s in same)


def decimals(width: float) -> int:
    """bandDecimals in readout.ts: 0 at 10 and wider, 1 at 1, else 2."""
    return 0 if width >= 10 else 1 if width >= 1 else 2


def discrete(r: dict[str, Any]) -> bool:
    """Classify a ramp the way rampKind in legend.ts does.

    Args:
        r: The ramp.

    Returns:
        True when the median adjacent color step is 20 or more, so the
        readout prints bands rather than a value.
    """
    rgbs = [rgb_of(s) for s in r["stops"]]
    steps = sorted(sum(abs(a - b) for a, b in zip(p, q, strict=True))
                   for p, q in pairwise(rgbs))
    if not steps:
        return False
    mid = len(steps) // 2
    median = steps[mid] if len(steps) % 2 else (steps[mid - 1] + steps[mid]) / 2
    return median >= DISCRETE_MEDIAN_STEP


def flat_png(rgb: tuple[int, int, int]) -> bytes:
    """A whole frame in one color."""
    buf = BytesIO()
    Image.new("RGBA", SCAN_SIZE, (*rgb, 255)).save(buf, format="PNG")
    return buf.getvalue()


async def open_console(browser: Browser, base: str, moment: str,
                       paint: dict[str, bytes]) -> Page:
    """Load the console with the scan and tilt frames served by this run.

    Args:
        browser: The running chromium.
        base: The deployment's origin.
        moment: The moment to open on.
        paint: Frame bytes by kind, "scan" and "tilt". Read at request time,
            so a caller can repaint the tilt between elevations.

    Returns:
        The loaded page.
    """
    ctx = await browser.new_context(viewport=VIEWPORT, device_scale_factor=1)
    page = await ctx.new_page()
    await page.add_init_script(
        "window.__errs=[];"
        "window.addEventListener('error',"
        "e=>window.__errs.push(String(e.message)));")
    prefs = json.dumps({"activeRadars": [RADAR], "moment": moment,
                        "composite": "none", "nexrad": False})
    await page.add_init_script(
        "localStorage.clear();"
        "localStorage.setItem('sentinel-theme','dark');"
        f"localStorage.setItem('sentinel-map-prefs', {json.dumps(prefs)});")

    def server(kind: str) -> Callable[[Route], Awaitable[None]]:
        async def serve(route: Route) -> None:
            await route.fulfill(status=200, body=paint[kind],
                                headers={"content-type": "image/png",
                                         "cache-control": "no-store"})
        return serve

    await page.route("**/xband_scan.png*", server("scan"))
    await page.route("**/tilt_image.png*", server("tilt"))
    await page.goto(f"{base}/?diag=probe", wait_until="load", timeout=60_000)
    await page.wait_for_timeout(LOAD_SETTLE_MS)
    return page


async def legend_title(page: Page) -> str | None:
    """The legend's title text, or None when no legend is drawn."""
    got: str | None = await page.evaluate(
        "() => { const e = document.querySelector('[data-legend-title]');"
        " return e ? e.textContent.trim() : null; }")
    return got


async def pick_tilt(page: Page, value: int) -> None:
    """Choose an option in the tilt picker and let the frame land."""
    _ = await page.select_option("[data-tilt-select]", str(value))
    await page.wait_for_timeout(TILT_SETTLE_MS)


async def run_case(browser: Browser, base: str,
                   case: dict[str, Any]) -> dict[str, Any]:
    """Every step for one moment.

    Args:
        browser: The running chromium.
        base: The deployment's origin.
        case: One entry of CASES.

    Returns:
        What each step read and what it should have read.
    """
    scan_ramp = ramp(case["scan"][0])
    tilt_ramp = ramp(case["tilt"])
    first, second = case["stops"]
    paint = {"scan": flat_png(rgb_of(scan_ramp["stops"][case["scan"][1]])),
             "tilt": flat_png(rgb_of(tilt_ramp["stops"][first]))}
    page = await open_console(browser, base, case["moment"], paint)
    steps: list[dict[str, Any]] = []
    await solo(page, RADAR)
    steps.append({"step": "processed scan",
                  "want": expected(scan_ramp, case["scan"][1]),
                  "got": await read_at(page, 0.5, 0.5)})
    await pick_tilt(page, 1)
    steps.append({"step": "tilt el 1", "want": expected(tilt_ramp, first),
                  "got": await read_at(page, 0.5, 0.5)})
    steps.append({"step": "tilt legend", "want": tilt_ramp["label"],
                  "got": await legend_title(page)})
    # A new elevation is a new URL, so the repainted bytes are fetched.
    paint["tilt"] = flat_png(rgb_of(tilt_ramp["stops"][second]))
    await pick_tilt(page, 2)
    steps.append({"step": "tilt el 2", "want": expected(tilt_ramp, second),
                  "got": await read_at(page, 0.52, 0.48)})
    await pick_tilt(page, 0)
    steps.append({"step": "back to processed scan",
                  "want": expected(scan_ramp, case["scan"][1]),
                  "got": await read_at(page, 0.5, 0.5)})
    errs: list[str] = await page.evaluate("()=>window.__errs||[]")
    await page.context.close()
    return {"moment": case["moment"], "steps": steps, "errors": errs}


async def run(base: str) -> dict[str, Any]:
    """Run every case in one browser.

    Args:
        base: The deployment's origin.

    Returns:
        The report.
    """
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        try:
            cases = [await run_case(browser, base, c) for c in CASES]
        finally:
            await browser.close()
    return {"base": base, "cases": cases}


def main() -> int:
    """Run the probe and report.

    Returns:
        0 when every step read what it should, 1 otherwise.
    """
    ap = argparse.ArgumentParser(description="tilt readout probe")
    ap.add_argument("outdir", type=Path)
    ap.add_argument("--base", default=DEFAULT_BASE)
    args = ap.parse_args()
    outdir: Path = args.outdir
    outdir.mkdir(parents=True, exist_ok=True)
    report = asyncio.run(run(args.base))
    _ = (outdir / "tilt_readout.json").write_text(json.dumps(report, indent=1))
    bad = 0
    for case in report["cases"]:
        for s in case["steps"]:
            ok = s["got"] == s["want"]
            bad += 0 if ok else 1
            print(f"  {'ok  ' if ok else 'FAIL'}  {case['moment']} {s['step']}: "
                  f"{s['got']!r}" + ("" if ok else f" (want {s['want']!r})"))
        if case["errors"]:
            bad += 1
            print(f"  FAIL  {case['moment']} page errors: {case['errors']}")
    print("all tilt readout steps passed" if bad == 0 else f"{bad} FAILED")
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
