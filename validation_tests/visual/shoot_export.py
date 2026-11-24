#!/usr/bin/env python3
"""The exported image has to carry the map, the time and the colors.

The layout is asserted in ``validation_tests/js/test_map_export.mjs``. What
that cannot see is whether the file the button produces contains anything.

A blank export is the failure to watch for, and it is not hypothetical: a
WebGL drawing buffer is cleared after each frame unless the map is built
with ``preserveDrawingBuffer``, and reading it afterwards then returns a
transparent image. It fails intermittently rather than always, so a check
that only opens the file and reads its size would pass on most runs.

So this downloads the PNG and measures its pixels: the map band has to hold
many colors, the caption band has to hold ink, and the color bar has to run
through a range rather than sit on one color.

Usage
-----
::

    python validation_tests/visual/shoot_export.py OUTDIR [--base URL]
"""
from __future__ import annotations

import os
import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

try:
    from PIL import Image
except ImportError:  # pragma: no cover - environment check
    print("pillow is not installed: pip install pillow", file=sys.stderr)
    raise SystemExit(2) from None

try:
    from playwright.async_api import Browser, Page, async_playwright
    from playwright.async_api import TimeoutError as PlaywrightTimeout
except ImportError:  # pragma: no cover - environment check
    print("playwright is not installed: pip install playwright "
          "&& playwright install chromium", file=sys.stderr)
    raise SystemExit(2) from None

DEFAULT_BASE = os.environ.get("SENTINEL_VISUAL_BASE",
                              "http://localhost:5173")

LOAD_SETTLE_MS = 8000
DOWNLOAD_TIMEOUT_MS = 30_000

VIEWPORT = {"width": 1400, "height": 900}

# The export's own geometry, from $lib/map/exportImage.
PADDING = 16
FOOTER_HEIGHT = 64

# How many distinct colors a band must hold to count as drawn. A blank
# export has one; a rendered basemap has thousands.
MIN_MAP_COLORS = 200
MIN_LEGEND_COLORS = 8

# What share of the caption band's pixels must differ from its background
# for the text to count as present. Three short lines of type over a
# 1400 px band is a small share, so this is deliberately low, and a blank
# band sits at exactly zero.
MIN_INK_FRACTION = 0.001


def band_colors(im: Image.Image, box: tuple[int, int, int, int]) -> int:
    """How many distinct colors a rectangle holds.

    Args:
        im: The image.
        box: Left, upper, right and lower edges.

    Returns:
        The count of distinct RGB values.
    """
    crop = im.crop(box).convert("RGB")
    return len(crop.getcolors(maxcolors=1 << 20) or [])


def ink_fraction(im: Image.Image, box: tuple[int, int, int, int]) -> float:
    """What share of a rectangle differs from its most common color.

    Args:
        im: The image.
        box: Left, upper, right and lower edges.

    Returns:
        A fraction in [0, 1].
    """
    crop = im.crop(box).convert("RGB")
    counts = crop.getcolors(maxcolors=1 << 20) or []
    if not counts:
        return 0.0
    total = sum(n for n, _ in counts)
    background = max(n for n, _ in counts)
    return (total - background) / total if total else 0.0


async def open_console(browser: Browser, base: str) -> Page:
    """Load the console with a product and a radar that has a legend.

    Args:
        browser: The running chromium.
        base: The deployment's origin.

    Returns:
        The loaded page.

    Raises:
        PlaywrightTimeout: If the console does not load.
    """
    ctx = await browser.new_context(viewport=VIEWPORT, device_scale_factor=1,
                                    accept_downloads=True)
    page = await ctx.new_page()
    # XEBY and Reflectivity, because ramps/data/coverage.json lists that
    # pair as measured and the legend only draws for a measured one. With
    # XSCV, which is not in that file, the color-bar check below was
    # skipped and the run passed without testing it.
    await page.add_init_script(
        "localStorage.setItem('sentinel-theme', 'dark');"
        "localStorage.setItem('sentinel-map-prefs', JSON.stringify("
        "{activeRadars:['XEBY'], moment:'Reflectivity',"
        " composite:'comp_ref'}));")
    await page.goto(base, wait_until="load", timeout=45_000)
    await page.wait_for_timeout(LOAD_SETTLE_MS)
    return page


async def canvas_size(page: Page) -> tuple[int, int]:
    """The map canvas's backing-store size.

    Args:
        page: The loaded console.

    Returns:
        Width and height in pixels.
    """
    js = """
      () => {
        const c = document.querySelector('.maplibregl-canvas');
        return c ? [c.width, c.height] : [0, 0];
      }
    """
    got: list[int] = await page.evaluate(js)
    return (got[0], got[1])


def judge_image(im: Image.Image, expect: tuple[int, int], *,
                has_legend: bool) -> list[str]:
    """Check the downloaded image holds what it is supposed to.

    Args:
        im: The exported image.
        expect: The map canvas's width and height.
        has_legend: Whether a color bar was expected.

    Returns:
        One message per defect.
    """
    problems: list[str] = []
    want = (expect[0] + PADDING * 2, expect[1] + PADDING * 2 + FOOTER_HEIGHT)
    if im.size != want:
        problems.append(
            f"the image is {im.size}, expected {want} for a "
            f"{expect[0]}x{expect[1]} canvas")
        return problems

    map_box = (PADDING, PADDING, PADDING + expect[0], PADDING + expect[1])
    colors = band_colors(im, map_box)
    if colors < MIN_MAP_COLORS:
        problems.append(
            f"the map band holds {colors} colors, under {MIN_MAP_COLORS}; "
            f"the drawing buffer was cleared before it was read")

    footer_top = PADDING + expect[1]
    footer = (PADDING, footer_top, PADDING + expect[0],
              footer_top + FOOTER_HEIGHT)
    ink = ink_fraction(im, footer)
    if ink < MIN_INK_FRACTION:
        problems.append(
            f"the caption band is {ink:.5f} ink, under {MIN_INK_FRACTION}; "
            f"a picture with no time on it is what this exists to prevent")

    if has_legend:
        # The bar is at the right of the caption band. Measuring the right
        # third is enough to tell a gradient from a flat fill.
        bar = (PADDING + (expect[0] * 2) // 3, footer_top + 8,
               PADDING + expect[0], footer_top + 34)
        bar_colors = band_colors(im, bar)
        if bar_colors < MIN_LEGEND_COLORS:
            problems.append(
                f"the color bar holds {bar_colors} colors, under "
                f"{MIN_LEGEND_COLORS}; it is not a ramp")
    return problems


async def run(browser: Browser, base: str, outdir: Path) -> dict[str, Any]:
    """Press the export button and measure what comes out.

    Args:
        browser: The running chromium.
        base: The deployment's origin.
        outdir: Where the downloaded image goes.

    Returns:
        What the run showed, with a "problems" list.
    """
    report: dict[str, Any] = {"base": base}
    problems: list[str] = []
    page = await open_console(browser, base)

    button = page.locator("[data-export]")
    if await button.count() != 1:
        problems.append(f"{await button.count()} export controls, expected 1")
        await page.context.close()
        report["problems"] = problems
        return report

    size = await canvas_size(page)
    report["canvas"] = list(size)
    has_legend = await page.locator("[data-legend]").count() > 0
    report["legend_on_screen"] = has_legend
    if not has_legend:
        # Reported rather than skipped. The legend is half of what the
        # export exists to carry, and a run that quietly leaves it
        # unchecked is a run that proves nothing about it.
        problems.append(
            "no legend on screen, so the color bar in the export cannot be "
            "checked; this needs a radar and moment that coverage.json "
            "lists as measured")

    async with page.expect_download(
            timeout=DOWNLOAD_TIMEOUT_MS) as download_info:
        await button.first.click()
    download = await download_info.value
    name = download.suggested_filename
    report["file"] = name
    saved = outdir / name
    await download.save_as(saved)

    if not name.endswith(".png"):
        problems.append(f"the file is named {name!r}, which is not a PNG")
    if "sentinel" not in name:
        problems.append(f"the file name does not say what made it: {name!r}")

    with Image.open(saved) as im:
        report["size"] = list(im.size)
        problems += judge_image(im, size, has_legend=has_legend)

    err = page.locator("[data-export-error]")
    if await err.count() > 0:
        problems.append(f"the panel reports {(await err.first.inner_text())!r}")

    await page.screenshot(path=str(outdir / "export-console.png"))
    await page.context.close()
    report["problems"] = problems
    return report


async def shoot(outdir: Path, base: str) -> int:
    """Check the export on the deployed console.

    Args:
        outdir: Where to write the image and the report.
        base: The deployment's origin.

    Returns:
        Process exit status, non-zero when the export is wrong.
    """
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        try:
            report = await run(browser, base, outdir)
        except PlaywrightTimeout as e:
            report = {"problems": [f"the export never arrived: {e}"]}
        await browser.close()
    (outdir / "report.json").write_text(json.dumps(report, indent=1))

    if "file" in report:
        print(f"wrote {report['file']} at {report.get('size')} "
              f"from a {report.get('canvas')} canvas")
    if report["problems"]:
        print("\nFAIL:")
        for p in report["problems"]:
            print(f"  {p}")
        return 1
    print(f"\nok: the export carries a rendered map, a caption and a color "
          f"bar; image in {outdir}")
    return 0


def main() -> int:
    """Parse arguments and run.

    Returns:
        Process exit status.
    """
    ap = argparse.ArgumentParser(description="check the image export")
    ap.add_argument("outdir", type=Path)
    ap.add_argument("--base", default=DEFAULT_BASE)
    args = ap.parse_args()
    args.outdir.mkdir(parents=True, exist_ok=True)
    return asyncio.run(shoot(args.outdir, args.base))


if __name__ == "__main__":
    sys.exit(main())
