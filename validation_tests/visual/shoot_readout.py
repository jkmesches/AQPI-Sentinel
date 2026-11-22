#!/usr/bin/env python3
"""Hover the deployed overlay and check what the readout says.

Why this exists
---------------
The geometry is asserted in ``validation_tests/js/test_map_readout.mjs`` and
the ramps are measured in ``ramp_coverage.py``, but neither runs a real
canvas. Between them sits the part that can go wrong silently: the frame the
sampler holds has to be the frame on screen, drawn at the size the geometry
assumes, with its alpha intact.

So this hovers a grid of points over the overlay on the live console, reads
whatever chip appears, and checks three things the pure tests cannot.

Every value it reports must lie inside the ramp's own range, since a value
outside it means the pixel was read from somewhere other than the overlay.
Hovering well away from any radar must report nothing, since the map
background is not a reading. And at least some points must report, or the
sampler is holding nothing and the honest-blank path is hiding a dead
feature.

Usage
-----
::

    python validation_tests/visual/shoot_readout.py OUTDIR [--base URL]
"""
from __future__ import annotations

import os
import argparse
import asyncio
import json
import re
import sys
from pathlib import Path
from typing import Any

try:
    from playwright.async_api import Browser, Page, async_playwright
    from playwright.async_api import TimeoutError as PlaywrightTimeout
except ImportError:  # pragma: no cover - environment check
    print("playwright is not installed: pip install playwright "
          "&& playwright install chromium", file=sys.stderr)
    raise SystemExit(2) from None

DEFAULT_BASE = os.environ.get("SENTINEL_VISUAL_BASE",
                              "http://localhost:5173")

# The console polls every 5 s and holds a websocket, so networkidle never
# settles. Measured the same way capture.py's waits were.
LOAD_SETTLE_MS = 6000
HOVER_SETTLE_MS = 120

VIEWPORT = {"width": 1600, "height": 1000}

# A grid over the map area, inset from the edges so no point lands on the
# layers panel (right) or the timeline (bottom). Dense, because weather is
# sparse: the C-band's frame is about 2% opaque on a quiet day and the
# X-bands' about 0.3%, so a coarse grid lands on nothing and proves nothing.
GRID_COLS = 29
GRID_ROWS = 19
MAP_LEFT, MAP_TOP = 60, 180
MAP_RIGHT, MAP_BOTTOM = 1500, 900

# The C-band, whose reflectivity coverage.json records as measured at 21/21
# over 1,316,297 pixels. It paints the widest area and the most of it, so it
# gives the grid something to find where an X-band would not.
RADAR = "CBAND"
MOMENT = "Reflectivity"
# Any composite this deployment publishes. Used only to check the readout
# goes quiet while one is on.
COMPOSITE = "comp_ref"
RAMP_MIN, RAMP_MAX = -20.0, 80.0
RAMP_UNIT = "dBZ"

# Below this share of opaque pixels, a grid landing on nothing says the sky
# is clear rather than that the readout is broken. Measured on 2026-11-21:
# a quiet C-band frame is 2.0% opaque and a quiet X-band one 0.27%, and the
# grid covers 551 of roughly 1.4 million map pixels.
SPARSE_FRAME_FRACTION = 0.10

# The top-left of the map area. On the default camera that is open ocean
# north-west of Fort Bragg, far outside every radar's range ring, and the
# readout must say nothing there. A screen point rather than a longitude and
# latitude, because the probe deliberately publishes a snapshot instead of
# the live map, so there is nothing here to project with.
OFF_OVERLAY_POINT = (MAP_LEFT, MAP_TOP)

READING = re.compile(r"^(-?\d+(?:\.\d+)?) to (-?\d+(?:\.\d+)?) (\S+)$")


def parse_reading(text: str) -> tuple[float, float, str] | None:
    """Pull the band out of a readout chip.

    Args:
        text: What the chip said.

    Returns:
        Low bound, high bound and unit, or None when it does not parse.
    """
    m = READING.match(text.strip())
    if not m:
        return None
    return float(m.group(1)), float(m.group(2)), m.group(3)


async def hover_grid(page: Page) -> list[dict[str, Any]]:
    """Hover a grid of points and collect whatever the readout says.

    Args:
        page: The loaded console.

    Returns:
        One entry per point that reported something.
    """
    found: list[dict[str, Any]] = []
    for r in range(GRID_ROWS):
        for c in range(GRID_COLS):
            x = MAP_LEFT + (MAP_RIGHT - MAP_LEFT) * c / (GRID_COLS - 1)
            y = MAP_TOP + (MAP_BOTTOM - MAP_TOP) * r / (GRID_ROWS - 1)
            await page.mouse.move(x, y)
            await page.wait_for_timeout(HOVER_SETTLE_MS)
            chip = page.locator("[data-readout]")
            if await chip.count() == 0:
                continue
            text = (await chip.first.inner_text()).strip()
            if text:
                found.append({"x": x, "y": y, "text": text})
    return found


def judge_silence(found: list[dict[str, Any]], held: str | None,
                  opaque: float | None) -> list[str]:
    """Decide whether a grid that reported nothing is a fault.

    A clear sky and a dead sampler look identical from the grid alone. The
    held frame rules out the second, and the opaque share says whether there
    was anything to find.

    Args:
        found: What the grid reported.
        held: The data-readout-frame attribute, or None when absent.
        opaque: The share of the live frame that is painted, or None.

    Returns:
        One message per defect.
    """
    if not held:
        return ["the console is holding no frame for the readout, so no "
                "hover could have reported anything"]
    if found:
        return []
    if opaque is None:
        return [f"no point reported, the frame {held!r} is held, and the "
                f"live frame could not be fetched to say whether it has any "
                f"data; rerun when the upstream answers"]
    if opaque >= SPARSE_FRAME_FRACTION:
        return [f"no point reported although the frame {held!r} is "
                f"{opaque * 100:.2f}% painted, which the grid should have "
                f"landed on"]
    return []


def judge(found: list[dict[str, Any]], off_text: str | None,
          held: str | None, opaque: float | None) -> list[str]:
    """Check the readings against the ramp they claim to come from.

    Args:
        found: What the grid reported.
        off_text: What the off-overlay corner reported, if anything.
        held: The data-readout-frame attribute, or None when absent.
        opaque: The share of the live frame that is painted, or None when it
            could not be fetched.

    Returns:
        One message per defect.
    """
    problems = judge_silence(found, held, opaque)
    if len(found) == GRID_COLS * GRID_ROWS:
        problems.append(
            "every point on the grid reported, including ones far outside "
            "any range ring; the readout is answering where it should not")
    if off_text:
        problems.append(
            f"a point far off every overlay reported {off_text!r}; the map "
            f"background is not a reading")
    for f in found:
        parsed = parse_reading(f["text"])
        if not parsed:
            problems.append(f"{f['text']!r} is not a band")
            continue
        lo, hi, unit = parsed
        if unit != RAMP_UNIT:
            problems.append(f"{f['text']!r} is not in {RAMP_UNIT}")
        if hi <= lo:
            problems.append(f"{f['text']!r} runs backwards")
        if lo < RAMP_MIN - 0.01 or hi > RAMP_MAX + 0.01:
            problems.append(
                f"{f['text']!r} is outside the ramp's {RAMP_MIN}..{RAMP_MAX}, "
                f"so that pixel came from somewhere other than the overlay")
    return problems


async def opaque_share(page: Page, base: str) -> float | None:
    """What share of the live frame carries any paint.

    Read in the page, so it goes through the same proxy the overlay does and
    needs no second credential path.

    Args:
        page: The loaded console.
        base: The deployment's origin.

    Returns:
        A fraction from 0 to 1, or None when the frame could not be read.
    """
    js = """
      async (url) => {
        const img = new Image();
        img.crossOrigin = 'anonymous';
        img.src = url;
        try { await img.decode(); } catch { return null; }
        const c = document.createElement('canvas');
        c.width = img.naturalWidth; c.height = img.naturalHeight;
        const ctx = c.getContext('2d');
        if (!ctx) return null;
        ctx.drawImage(img, 0, 0);
        let d;
        try { d = ctx.getImageData(0, 0, c.width, c.height).data; }
        catch { return null; }
        let n = 0;
        for (let i = 3; i < d.length; i += 4) if (d[i] > 8) n++;
        return n / (d.length / 4);
      }
    """
    url = f"{base}/api/upstream/xband_scan.png?radar={RADAR}&moment={MOMENT}"
    got: float | None = await page.evaluate(js, url)
    return got


async def composite_reads_itself(browser: Browser, base: str,
                                 outdir: Path) -> list[str]:
    """Check the readout reports the composite the cursor is over.

    A composite sits in the same raster band as the radar overlays and
    covers far more ground, so where the two overlap the reader is looking
    at the composite and that is the value owed. Until comp_ref's ramp was
    measured on 2026-11-21 no product had one and this had to stay quiet
    instead; reporting the radar's dBZ for a pixel the composite painted
    is the failure either way.

    Args:
        browser: The running chromium.
        base: The deployment's origin.
        outdir: Where the screenshot goes.

    Returns:
        One message per way the reading is wrong or absent.
    """
    ctx = await browser.new_context(viewport=VIEWPORT)
    page = await ctx.new_page()
    await page.add_init_script(
        "localStorage.setItem('sentinel-map-prefs', JSON.stringify("
        f"{{activeRadars:['{RADAR}'], moment:'{MOMENT}',"
        f" composite:'{COMPOSITE}'}}));"
        "localStorage.setItem('sentinel-theme', 'dark');")
    try:
        await page.goto(base, wait_until="load", timeout=45_000)
    except PlaywrightTimeout:
        await ctx.close()
        return ["the composite page did not load"]
    await page.wait_for_timeout(LOAD_SETTLE_MS)
    spoke = await hover_grid(page)
    await page.screenshot(path=str(outdir / "readout-composite.png"))
    held = await page.evaluate(
        "() => document.querySelector('[data-readout-frame]')"
        "?.getAttribute('data-readout-frame') ?? null")
    await ctx.close()
    if not spoke:
        return [f"with the {COMPOSITE} composite on, no grid point reported; "
                f"its ramp is measured, so the composite's own value is owed"]
    # The composite covers the whole Bay while one radar covers a corner
    # of it, so a reading that only appears where the radar is painting
    # is the radar's. The grid is wider than any single radar's range.
    xs = {round(p["x"]) for p in spoke}
    ys = {round(p["y"]) for p in spoke}
    if len(xs) < 3 or len(ys) < 3:
        return [f"with the {COMPOSITE} composite on, {len(spoke)} points "
                f"reported and they sit in {len(xs)} columns and {len(ys)} "
                f"rows; that is one radar's footprint rather than the "
                f"composite's, so the reading is coming from underneath "
                f"(frame held: {held!r})"]
    return []


async def run(browser: Browser, base: str, outdir: Path) -> dict[str, Any]:
    """Load the console, hover the grid, and save a picture.

    Args:
        browser: The running chromium.
        base: The deployment's origin.
        outdir: Where the screenshot goes.

    Returns:
        What was found, with a "problems" list.
    """
    ctx = await browser.new_context(viewport=VIEWPORT, device_scale_factor=2)
    page = await ctx.new_page()
    await page.add_init_script(
        "localStorage.setItem('sentinel-map-prefs', JSON.stringify("
        f"{{activeRadars:['{RADAR}'], moment:'{MOMENT}',"
        " composite:'none'}));"
        "localStorage.setItem('sentinel-theme', 'dark');")
    try:
        await page.goto(base, wait_until="load", timeout=45_000)
    except PlaywrightTimeout:
        await ctx.close()
        return {"problems": ["the page did not load"], "found": []}
    await page.wait_for_timeout(LOAD_SETTLE_MS)

    found = await hover_grid(page)

    await page.mouse.move(*OFF_OVERLAY_POINT)
    await page.wait_for_timeout(HOVER_SETTLE_MS)
    chip = page.locator("[data-readout]")
    off_text: str | None = None
    if await chip.count() > 0:
        off_text = (await chip.first.inner_text()).strip() or None

    holder = page.locator("[data-readout-frame]")
    held: str | None = None
    if await holder.count() > 0:
        held = await holder.first.get_attribute("data-readout-frame")

    # Park the pointer on the densest reading for the screenshot.
    if found:
        best = found[len(found) // 2]
        await page.mouse.move(best["x"], best["y"])
        await page.wait_for_timeout(HOVER_SETTLE_MS)
    await page.screenshot(path=str(outdir / "readout.png"))
    opaque = await opaque_share(page, base)
    await ctx.close()
    return {"found": found, "off_overlay": off_text, "held": held,
            "opaque_share": opaque,
            "problems": judge(found, off_text, held, opaque)}


async def shoot(outdir: Path, base: str) -> int:
    """Check the readout on the deployed console.

    Args:
        outdir: Where to write the screenshot and the report.
        base: The deployment's origin.

    Returns:
        Process exit status, non-zero when a reading is wrong.
    """
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        report = await run(browser, base, outdir)
        report["problems"] += await composite_reads_itself(browser, base, outdir)
        await browser.close()
    (outdir / "report.json").write_text(json.dumps(report, indent=1))

    found = report["found"]
    print(f"{len(found)} of {GRID_COLS * GRID_ROWS} grid points reported")
    for f in found[:8]:
        print(f"   ({f['x']:.0f},{f['y']:.0f})  {f['text']}")
    if len(found) > 8:
        print(f"   ... and {len(found) - 8} more")
    share = report.get("opaque_share")
    print(f"frame held: {report.get('held')!r}"
          + (f", {share * 100:.2f}% painted" if share is not None else ""))
    if report.get("off_overlay") is None:
        print("the off-overlay corner: silent, as it should be")
    print(f"with the {COMPOSITE} composite on: checked it reads its own value")

    if report["problems"]:
        print("\nFAIL:")
        for p in report["problems"]:
            print(f"  {p}")
        return 1
    if not found:
        print(f"\nok: the frame is held and only "
              f"{(share or 0) * 100:.2f}% of it is painted, so the grid "
              f"landing on nothing is a clear sky; image in {outdir}")
        return 0
    print(f"\nok: every reading is a {RAMP_UNIT} band inside the ramp, and "
          f"the background reports nothing; image in {outdir}")
    return 0


def main() -> int:
    """Parse arguments and run.

    Returns:
        Process exit status.
    """
    ap = argparse.ArgumentParser(description="check the cursor readout")
    ap.add_argument("outdir", type=Path)
    ap.add_argument("--base", default=DEFAULT_BASE)
    args = ap.parse_args()
    args.outdir.mkdir(parents=True, exist_ok=True)
    return asyncio.run(shoot(args.outdir, args.base))


if __name__ == "__main__":
    sys.exit(main())
