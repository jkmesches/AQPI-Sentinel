#!/usr/bin/env python3
"""The cursor readout with a composite and a radar both on.

Why this exists
---------------
Joseph, 2026-11-23: with a composite turned on, the probe measured nothing
over a radar's own imagery. The readout read only the composite whenever one
was on, through the composite's ramp, while a radar selected after it drew on
top. Wherever the composite was transparent the chip said nothing, and where
it was painted the chip named the composite's value under the radar's color.

The readout now walks the overlays in the map's paint order, each through its
own ramp. This serves the composite and every X-band scan in known colors and
checks three loads whose answers do not depend on guessing that order:

1. Composite transparent, radar painted: the radar's band, whatever the order.
2. Composite painted, radar transparent: the composite's band, read through
   the radar's empty pixels.
3. Both painted, radar turned on after the composite: the radar's band, since
   a later overlay draws on top.
4. Forecast rain under a reflectivity scan: the legend draws the rain scale
   until the radar goes on over it, then the radar's (Joseph, 2026-11-23).

Usage
-----
::

    python validation_tests/visual/shoot_composite_radar_readout.py OUTDIR [--base URL]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Awaitable, Callable
from io import BytesIO
from pathlib import Path
from typing import Any

VISUAL = Path(__file__).resolve().parent
sys.path.insert(0, str(VISUAL))

try:
    from PIL import Image
    from playwright.async_api import Browser, Page, Route, async_playwright
    from playwright.async_api import TimeoutError as PlaywrightTimeout
except ImportError:  # pragma: no cover - environment check
    print("needs pillow and playwright: pip install pillow playwright "
          "&& playwright install chromium", file=sys.stderr)
    raise SystemExit(2) from None

from shoot_radar_readout import painted_stop, read_at, solo  # noqa: E402

DEFAULT_BASE = os.environ.get("SENTINEL_VISUAL_BASE", "http://localhost:5173")
LOAD_SETTLE_MS = 12_000
VIEWPORT = {"width": 1500, "height": 950}
RAMPS = VISUAL.parent.parent / "frontend/src/lib/map/ramps/data"
#: The composite the console opens on, its ramp, and a stop far from the
#: radar's painted one so the two readings cannot be confused.
COMPOSITE = "comp_ref"
COMPOSITE_RAMP = RAMPS / "aqpi-product-reflectivity.json"
COMPOSITE_STOP = 16
#: A composite whose scale has a different title from the radars'.
LEGEND_PRODUCT = "fcst_total_precip"
LEGEND_PRODUCT_TITLE = json.loads(
    (RAMPS / "aqpi-product-forecast-accum-in.json").read_text())["label"]
SCAN_SIZE = (697, 693)
COMPOSITE_SIZE = (565, 605)
POINTS = ((0.5, 0.5), (0.46, 0.54), (0.54, 0.47))


def composite_stop() -> dict[str, Any]:
    """The stop the composite is painted in, and what it reads back as.

    Returns:
        Its color and the text the console prints for it.

    Raises:
        RuntimeError: When the ramp no longer has that stop.
    """
    ramp = json.loads(COMPOSITE_RAMP.read_text())
    if COMPOSITE_STOP >= len(ramp["stops"]) - 1:
        raise RuntimeError(f"{COMPOSITE_RAMP.name} has {len(ramp['stops'])} stops")
    stop = ramp["stops"][COMPOSITE_STOP]
    hexed = str(stop["color"]).lstrip("#")
    return {"rgb": tuple(int(hexed[k:k + 2], 16) for k in (0, 2, 4)),
            "reads": f"{stop['lo']:.1f} to {stop['hi']:.1f} {ramp['unit']}"}


def radar_title() -> str:
    """The title the legend gives the X-band reflectivity scale."""
    ramp = json.loads((RAMPS / "aqpi-xband-reflectivity.json").read_text())
    return str(ramp["label"])


def png(size: tuple[int, int], rgb: tuple[int, int, int] | None) -> bytes:
    """A frame painted in one color, or fully transparent for None.

    Args:
        size: Width and height in pixels.
        rgb: The color, or None for an empty frame.

    Returns:
        The PNG bytes.
    """
    buf = BytesIO()
    fill = (0, 0, 0, 0) if rgb is None else (*rgb, 255)
    Image.new("RGBA", size, fill).save(buf, format="PNG")
    return buf.getvalue()


async def load(browser: Browser, base: str, spec: dict[str, Any]) -> Page:
    """Open the console on the composite with no radar on, frames served.

    Args:
        browser: The running chromium.
        base: The deployment's origin.
        spec: The case, with its composite product and the frames to serve.

    Returns:
        The loaded page.
    """
    comp, scan = spec["comp"], spec["scan"]
    product = spec.get("product", COMPOSITE)
    page = await (await browser.new_context(viewport=VIEWPORT,
                                            device_scale_factor=1)).new_page()
    await page.add_init_script(
        "window.__errs=[];window.addEventListener('error',"
        "e=>window.__errs.push(String(e.message)));"
        "localStorage.clear();localStorage.setItem('sentinel-theme','dark');"
        "localStorage.setItem('sentinel-map-prefs', JSON.stringify("
        f"{{activeRadars:[], moment:'Reflectivity', composite:'{product}',"
        " nexrad:false}));")

    def serving(body: bytes) -> Callable[[Route], Awaitable[None]]:
        async def serve(route: Route) -> None:
            await route.fulfill(status=200, body=body, headers={
                "content-type": "image/png", "cache-control": "no-store"})
        return serve

    await page.route("**/product_image.png*", serving(comp))
    await page.route("**/product_latest.png*", serving(comp))
    await page.route("**/xband_scan.png*", serving(scan))
    _ = await page.goto(f"{base}/?diag=probe", wait_until="load", timeout=60_000)
    await page.wait_for_timeout(LOAD_SETTLE_MS)
    return page


async def case(browser: Browser, base: str, spec: dict[str, Any],
               outdir: Path) -> dict[str, Any]:
    """Run one load: serve the frames, turn XSCV on, hover, read.

    Args:
        browser: The running chromium.
        base: The deployment's origin.
        spec: The case's name, frames and expected reading.
        outdir: Where its screenshot goes.

    Returns:
        What was read, and whether every point read what it should.
    """
    page = await load(browser, base, spec)
    try:
        held = await page.locator("[data-view]").first.get_attribute(
            "data-readout-composite")
        legend = [await legend_title(page)]
        await solo(page, "XSCV")
        legend.append(await legend_title(page))
        seen = [await read_at(page, fx, fy) for fx, fy in POINTS]
        _ = await page.screenshot(path=str(outdir / f"{spec['name']}.png"))
        errs: list[str] = await page.evaluate("()=>window.__errs||[]")
    finally:
        await page.context.close()
    legend_ok = "legend" not in spec or legend == spec["legend"]
    return {"case": spec["name"], "want": spec["want"], "seen": seen,
            "legend": legend, "want_legend": spec.get("legend"),
            "composite_held": held, "errors": errs,
            "ok": bool(held) and not errs and legend_ok
            and all(s == spec["want"] for s in seen)}


async def legend_title(page: Page) -> str | None:
    """The legend's title, or None when no legend is drawn."""
    got: str | None = await page.evaluate(
        "() => document.querySelector('[data-legend-title]')?.textContent?.trim() ?? null")
    return got


async def run(base: str, outdir: Path) -> list[dict[str, Any]]:
    """Every case, in one browser.

    Args:
        base: The deployment's origin.
        outdir: Where the screenshots go.

    Returns:
        One result per case.
    """
    radar, comp = painted_stop(), composite_stop()
    painted_scan, empty_scan = png(SCAN_SIZE, radar["rgb"]), png(SCAN_SIZE, None)
    painted_comp, empty_comp = png(COMPOSITE_SIZE, comp["rgb"]), png(COMPOSITE_SIZE, None)
    specs = [
        {"name": "radar-over-empty-composite", "comp": empty_comp,
         "scan": painted_scan, "want": radar["reads"]},
        {"name": "composite-through-empty-radar", "comp": painted_comp,
         "scan": empty_scan, "want": comp["reads"]},
        {"name": "radar-over-painted-composite", "comp": painted_comp,
         "scan": painted_scan, "want": radar["reads"]},
        # Forecast rain under a reflectivity scan, so the two scales have
        # different titles. The legend draws the composite's until the radar
        # goes on over it, then the radar's.
        {"name": "legend-follows-radar-on-top", "product": LEGEND_PRODUCT,
         "comp": empty_comp, "scan": painted_scan, "want": radar["reads"],
         "legend": [LEGEND_PRODUCT_TITLE, radar_title()]},
    ]
    out: list[dict[str, Any]] = []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        try:
            for spec in specs:
                try:
                    out.append(await case(browser, base, spec, outdir))
                except PlaywrightTimeout as exc:
                    out.append({"case": spec["name"], "ok": False,
                                "error": f"the console stopped answering: {exc}"})
        finally:
            await browser.close()
    return out


def main() -> int:
    """Run the cases and report.

    Returns:
        0 when every case read what it should, 1 otherwise.
    """
    ap = argparse.ArgumentParser(description="composite and radar readout probe")
    _ = ap.add_argument("outdir", type=Path)
    _ = ap.add_argument("--base", default=DEFAULT_BASE)
    args = ap.parse_args()
    outdir: Path = args.outdir
    outdir.mkdir(parents=True, exist_ok=True)
    results = asyncio.run(run(args.base, outdir))
    _ = (outdir / "report.json").write_text(json.dumps(results, indent=2))
    for r in results:
        print(f"  {'ok  ' if r['ok'] else 'FAIL'}  {r['case']}: saw {r.get('seen')!r}, "
              f"legend {r.get('legend')!r}"
              + ("" if r["ok"] else f" (want {r.get('want')!r}, legend "
                 f"{r.get('want_legend')!r}, composite held {r.get('composite_held')!r}, "
                 f"errors {r.get('errors') or r.get('error')})"))
    bad = sum(1 for r in results if not r["ok"])
    print("all composite and radar readout cases passed" if bad == 0 else f"{bad} FAILED")
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
