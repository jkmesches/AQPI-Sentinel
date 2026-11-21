#!/usr/bin/env python3
"""Photograph the legend in both themes, and measure its labels.

Why this exists
---------------
The page is the only thing that can catch a bad character measurement.
``fitTicks`` decides the ticking from a width the component reads in the
browser, and the assertions in ``validation_tests/js/test_map_legend.mjs``
check the geometry against that number rather than against the font. So this
reads the drawn bounding box of every tick label back out of the rendered
SVG.

It also crops the legend out of the full page at a readable scale, since a
legend that is right in its numbers and unreadable on screen is still wrong.

Usage
-----
::

    python validation_tests/visual/shoot_legend.py OUTDIR [--base URL]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from itertools import pairwise
from pathlib import Path
from typing import Any

try:
    from playwright.async_api import Browser, async_playwright
    from playwright.async_api import TimeoutError as PlaywrightTimeout
except ImportError:  # pragma: no cover - environment check
    print("playwright is not installed: pip install playwright "
          "&& playwright install chromium", file=sys.stderr)
    raise SystemExit(2) from None

DEFAULT_BASE = "https://the test deployment"

# The console polls every 5 s and holds a websocket, so networkidle never
# settles. These are the same waits capture.py measured.
LOAD_SETTLE_MS = 4000
THEME_SETTLE_MS = 600

# How many pixels two label boxes may overlap before it is a defect. Zero: a
# label that touches its neighbor is the failure fitTicks exists to prevent.
OVERLAP_SLACK_PX = 0

VIEWPORT = {"width": 1600, "height": 1000}

# Read the drawn geometry back out of the SVG rather than recomputing it.
MEASURE_JS = """
() => {
  const host = document.querySelector("[data-legend]");
  if (!host) return { present: false };
  const svg = host.querySelector('svg');
  if (!svg) return { present: true, drawn: false };
  const texts = [...svg.querySelectorAll('text')]
    .filter((t) => t.getAttribute('x') !== '-9999');
  const boxes = texts.map((t) => {
    const b = t.getBBox();
    return { label: t.textContent, x: b.x, w: b.width, y: b.y, h: b.height,
             anchor: t.getAttribute('text-anchor') };
  });
  const rects = [...svg.querySelectorAll('rect')].length;
  const grads = [...svg.querySelectorAll('linearGradient stop')].length;
  const vb = svg.getAttribute('viewBox');
  return { present: true, drawn: true, boxes, rects, grads, viewBox: vb,
           title: host.querySelector('[data-legend-title]')?.textContent?.trim() ?? '' };
}
"""


def overlaps(boxes: list[dict[str, Any]]) -> list[str]:
    """Find label pairs whose drawn boxes touch.

    Args:
        boxes: One entry per drawn label, with x and w in SVG units.

    Returns:
        One message per overlapping pair.
    """
    out: list[str] = []
    ordered = sorted(boxes, key=lambda b: b["x"])
    for a, b in pairwise(ordered):
        gap = b["x"] - (a["x"] + a["w"])
        if gap < -OVERLAP_SLACK_PX:
            out.append(f"{a['label']!r} and {b['label']!r} overlap by "
                       f"{-gap:.2f} px")
    return out


def off_bar(boxes: list[dict[str, Any]], width: float) -> list[str]:
    """Find labels drawn outside the bar's own width.

    Args:
        boxes: The drawn labels.
        width: The bar's width in SVG units.

    Returns:
        One message per label that hangs off either end.
    """
    out: list[str] = []
    for b in boxes:
        if b["x"] < -0.5:
            out.append(f"{b['label']!r} starts at {b['x']:.2f}, left of the bar")
        if b["x"] + b["w"] > width + 0.5:
            out.append(f"{b['label']!r} ends at {b['x'] + b['w']:.2f}, "
                       f"past the bar's {width:.2f}")
    return out


async def shoot_theme(browser: Browser, theme: str, base: str,
                      outdir: Path, moment: str = "Reflectivity"
                      ) -> dict[str, Any]:
    """Load the console in one theme and read the legend back.

    Args:
        browser: The running chromium.
        theme: "dark" or "light".
        base: The deployment's origin.
        outdir: Where the images go.
        moment: Which moment to select, to reach a discrete or a continuous
            ramp.

    Returns:
        What the page reported, with a "problems" list.
    """
    ctx = await browser.new_context(
        viewport=VIEWPORT, device_scale_factor=2,
        color_scheme="dark" if theme == "dark" else "light")
    page = await ctx.new_page()
    # One radar and reflectivity, which coverage.json records as measured, so
    # the legend has a ramp to draw.
    await page.add_init_script(
        "localStorage.setItem('sentinel-map-prefs', JSON.stringify("
        "{activeRadars:['XEBY'], moment:'" + moment + "',"        " composite:'none'}));"
        f"localStorage.setItem('sentinel-theme', '{theme}');")
    try:
        await page.goto(base, wait_until="load", timeout=45_000)
    except PlaywrightTimeout:
        await ctx.close()
        return {"problems": ["the page did not load"]}
    await page.wait_for_timeout(LOAD_SETTLE_MS + THEME_SETTLE_MS)

    measured: dict[str, Any] = await page.evaluate(MEASURE_JS)
    measured["problems"] = inspect(measured)
    await page.screenshot(path=str(outdir / f"page-{slug(moment)}-{theme}.png"))
    host = page.locator("[data-legend]")
    if await host.count() > 0:
        await host.first.screenshot(path=str(outdir / f"legend-{slug(moment)}-{theme}.png"))
    await ctx.close()
    return measured


def inspect(measured: dict[str, Any]) -> list[str]:
    """Judge one theme's measurements.

    Args:
        measured: What the page reported.

    Returns:
        One message per defect, empty when the legend is sound.
    """
    if not measured.get("present"):
        return ["no legend on the page"]
    if not measured.get("drawn"):
        return ["the legend drew no svg"]
    boxes = measured["boxes"]
    found: list[str] = []
    if len(boxes) < 2:
        found.append(f"{len(boxes)} labels, where both ends is the minimum")
    width = float(measured["viewBox"].split()[2])
    found += overlaps(boxes)
    found += off_bar(boxes, width)
    return found


def slug(moment: str) -> str:
    """A filename-safe form of a moment label.

    Args:
        moment: The label.

    Returns:
        Lowercase, with spaces replaced.
    """
    return moment.lower().replace(" ", "-")


async def shoot(outdir: Path, base: str, moments: list[str]) -> int:
    """Measure and photograph the legend in both themes.

    Args:
        outdir: Where to write the images and the report.
        base: The deployment's origin.
        moments: Which moments to select, one per legend to photograph.

    Returns:
        Process exit status, non-zero when a measurement fails.
    """
    report: dict[str, Any] = {"base": base, "themes": {}}
    problems: list[str] = []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        for moment in moments:
            for theme in ("dark", "light"):
                measured = await shoot_theme(
                    browser, theme, base, outdir, moment)
                report["themes"][f"{slug(moment)}/{theme}"] = measured
                problems += [f"{moment}/{theme}: {m}"
                             for m in measured["problems"]]
        await browser.close()

    report["problems"] = problems
    (outdir / "report.json").write_text(json.dumps(report, indent=1))
    for theme, m in report["themes"].items():
        if m.get("drawn"):
            labels = " ".join(b["label"] for b in m["boxes"])
            print(f"{theme:26} {len(m['boxes'])} labels, {m['rects']} rects, "
                  f"{m['grads']} gradient stops: {labels}")
    if problems:
        print("\nFAIL:")
        for p in problems:
            print(f"  {p}")
        return 1
    print(f"\nok: the legend draws and its labels clear each other in both "
          f"themes; images in {outdir}")
    return 0


def main() -> int:
    """Parse arguments and run.

    Returns:
        Process exit status.
    """
    ap = argparse.ArgumentParser(description="photograph and measure the legend")
    ap.add_argument("outdir", type=Path)
    ap.add_argument("--base", default=DEFAULT_BASE)
    ap.add_argument("--moments", nargs="+",
                    default=["Reflectivity", "Velocity"],
                    help="one discrete and one continuous ramp by default")
    args = ap.parse_args()
    args.outdir.mkdir(parents=True, exist_ok=True)
    return asyncio.run(shoot(args.outdir, args.base, args.moments))


if __name__ == "__main__":
    sys.exit(main())
