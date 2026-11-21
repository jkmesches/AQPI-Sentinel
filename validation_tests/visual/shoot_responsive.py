#!/usr/bin/env python3
"""The console has to stay usable at phone width.

The thresholds are asserted in
``validation_tests/js/test_map_responsive.mjs``. What that cannot see is
whether the page honors them, and whether what is left at 390 px is a
console or a panel with a sliver of map behind it.

Four things are checked at each width, and the first is the one that is
easy to get wrong in a way nobody notices on a laptop: the page must not
scroll sideways. A single element wider than the viewport does that, and it
makes every drag on the map pan the page instead.

Usage
-----
::

    python validation_tests/visual/shoot_responsive.py OUTDIR [--base URL]
"""
from __future__ import annotations

import argparse
import asyncio
import json
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

DEFAULT_BASE = "https://the test deployment"

LOAD_SETTLE_MS = 7000
ACTION_SETTLE_MS = 2500
PANE_SETTLE_MS = 6000

# A phone, a tablet and a laptop. The thresholds in $lib/map/responsive sit
# at 560, 720 and 900, so these three land on either side of all of them.
SIZES = [
    ("phone", {"width": 390, "height": 844}),
    ("tablet", {"width": 820, "height": 1180}),
    ("laptop", {"width": 1600, "height": 950}),
]

# The map must keep at least this share of the pane's width. Below it the
# panel is the page and the map is a stripe behind it.
MIN_MAP_SHARE = 0.55


async def page_metrics(page: Page) -> dict[str, Any]:
    """What the layout came out as.

    Args:
        page: The loaded console.

    Returns:
        The scroll widths, the panel state and the pane layout.
    """
    js = """
      () => {
        const doc = document.documentElement;
        const panel = document.querySelector('[data-panel]');
        const canvas = document.querySelector('.maplibregl-canvas');
        const strip = document.querySelector('[data-time-controls]');
        const layout = document.querySelector('[data-pane-layout]');
        return {
          innerWidth: window.innerWidth,
          scrollWidth: doc.scrollWidth,
          clientWidth: doc.clientWidth,
          panel: panel ? panel.getAttribute('data-panel') : null,
          panelWidth: panel ? panel.getBoundingClientRect().width : 0,
          canvasWidth: canvas ? canvas.clientWidth : 0,
          canvasHeight: canvas ? canvas.clientHeight : 0,
          stripLayout: strip ? strip.getAttribute('data-strip-layout') : null,
          paneLayout: layout ? layout.getAttribute('data-pane-layout') : null,
          panes: document.querySelectorAll('[data-view]').length,
          strips: document.querySelectorAll('[data-time-controls]').length
        };
      }
    """
    got: dict[str, Any] = await page.evaluate(js)
    return got


async def controls_visible(page: Page) -> dict[str, bool]:
    """Whether each transport control is inside the viewport.

    A control pushed off the edge is still in the DOM and still passes a
    count, so this asks where each one actually is.

    Args:
        page: The loaded console.

    Returns:
        One entry per control.
    """
    js = """
      () => {
        const inside = (el) => {
          if (!el) return false;
          const r = el.getBoundingClientRect();
          return r.width > 0 && r.height > 0 && r.left >= -1
            && r.right <= window.innerWidth + 1;
        };
        const strip = document.querySelector('[data-time-controls]');
        if (!strip) return {};
        const by = (label) => strip.querySelector(`[aria-label="${label}"]`);
        return {
          play: inside(by('play')) || inside(by('pause')),
          next: inside(by('step forward')),
          prev: inside(by('step back')),
          latest: inside(by('latest')),
          speed: inside(strip.querySelector('[data-speed]')),
          help: inside(strip.querySelector('[data-help-toggle]')),
          axis: inside(strip.querySelector('[data-time-axis]'))
        };
      }
    """
    got: dict[str, bool] = await page.evaluate(js)
    return got


async def open_at(browser: Browser, base: str,
                  viewport: dict[str, int]) -> Page:
    """Load the console at a viewport size, as a first visit.

    Args:
        browser: The running chromium.
        base: The deployment's origin.
        viewport: The width and height.

    Returns:
        The loaded page.

    Raises:
        PlaywrightTimeout: If the console does not load.
    """
    ctx = await browser.new_context(viewport=viewport, device_scale_factor=1)
    page = await ctx.new_page()
    # The theme only, so the panel's first-visit rule is exercised. Seeding
    # the map prefs would make every load look like a return visit, and the
    # auto-close would never run.
    await page.add_init_script(
        "localStorage.setItem('sentinel-theme', 'dark');")
    await page.goto(base, wait_until="load", timeout=45_000)
    await page.wait_for_timeout(LOAD_SETTLE_MS)
    return page


def judge_size(name: str, m: dict[str, Any],
               controls: dict[str, bool]) -> list[str]:
    """Check one viewport's layout.

    Args:
        name: The size's name, for the messages.
        m: What page_metrics found.
        controls: What controls_visible found.

    Returns:
        One message per defect.
    """
    problems: list[str] = []
    if m["scrollWidth"] > m["clientWidth"] + 1:
        problems.append(
            f"{name}: the page scrolls sideways, {m['scrollWidth']} wide in "
            f"{m['clientWidth']}; every drag on the map pans the page")
    if m["canvasWidth"] <= 0 or m["canvasHeight"] <= 0:
        problems.append(f"{name}: the map canvas is "
                        f"{m['canvasWidth']}x{m['canvasHeight']}")
        return problems
    if m["panel"] == "open" and m["innerWidth"] > 0:
        # The map's share of the window. The dock takes its width from the
        # map rather than covering it, so the old form subtracted the dock
        # from a canvas the dock had already shrunk and reported 39% of a
        # pane that had 61% of the screen.
        share = m["canvasWidth"] / m["innerWidth"]
        if share < MIN_MAP_SHARE:
            problems.append(
                f"{name}: the open dock leaves {share:.0%} of the window to "
                f"the map, under {MIN_MAP_SHARE:.0%}")
    for control, seen in controls.items():
        if not seen:
            problems.append(
                f"{name}: the {control} control is outside the viewport")
    return problems


async def check_compare(page: Page, name: str, width: int,
                        problems: list[str]) -> tuple[dict[str, Any], bool]:
    """Open the comparison pane and check it fits.

    Two panes are the layout most likely to overflow, since they put two
    maps and two sets of map overlays where there was one of each.

    Args:
        page: The loaded console.
        name: The size's name, for the messages.
        width: The viewport's width.
        problems: Collects failure messages.

    Returns:
        The metrics after opening it, and whether a control was found.
    """
    # The console counts panes rather than toggling a comparison, so
    # opening the second one is the add control. It lives on the toolbar,
    # which is console chrome and is there at every width.
    toggle = page.locator("[data-pane-add]")
    if await toggle.count() == 0:
        problems.append(f"{name}: no control to open a second pane")
        return {}, False

    await toggle.first.click()
    await page.wait_for_timeout(PANE_SETTLE_MS)
    cm = await page_metrics(page)
    want = "stacked" if width < 900 else "side-by-side"
    if cm["paneLayout"] != want:
        problems.append(f"{name}: compare laid out {cm['paneLayout']!r}, "
                        f"expected {want!r}")
    if cm["panes"] != 2:
        problems.append(f"{name}: {cm['panes']} panes with compare on")
    if cm["strips"] != 1:
        problems.append(f"{name}: {cm['strips']} time strips")
    if cm["scrollWidth"] > cm["clientWidth"] + 1:
        problems.append(
            f"{name}: compare mode scrolls sideways, {cm['scrollWidth']} "
            f"in {cm['clientWidth']}")
    return cm, True


async def run(browser: Browser, base: str, outdir: Path) -> dict[str, Any]:
    """Load the console at each size and measure the layout.

    Args:
        browser: The running chromium.
        base: The deployment's origin.
        outdir: Where the screenshots go.

    Returns:
        What each size showed, with a "problems" list.
    """
    report: dict[str, Any] = {"base": base, "sizes": {}}
    problems: list[str] = []

    for name, viewport in SIZES:
        page = await open_at(browser, base, viewport)
        m = await page_metrics(page)
        controls = await controls_visible(page)
        report["sizes"][name] = {"metrics": m, "controls": controls}
        problems += judge_size(name, m, controls)

        # The panel's first-visit rule, and the strip's wrap, both read
        # off the thresholds in $lib/map/responsive.
        if viewport["width"] < 560 and m["panel"] != "closed":
            problems.append(
                f"{name}: the panel is {m['panel']!r} on a "
                f"{viewport['width']} px screen, where it covers the map")
        if viewport["width"] >= 900 and m["panel"] != "open":
            problems.append(
                f"{name}: the panel is {m['panel']!r} with room for it")
        if viewport["width"] < 720 and m["stripLayout"] != "wrapped":
            problems.append(
                f"{name}: the time strip is {m['stripLayout']!r}, so its "
                f"axis is sharing a row it has no width for")
        if viewport["width"] >= 900 and m["stripLayout"] != "one-row":
            problems.append(
                f"{name}: the time strip wrapped with room to spare")

        await page.screenshot(path=str(outdir / f"responsive-{name}.png"),
                              full_page=False)
        compare, found = await check_compare(page, name, viewport["width"],
                                             problems)
        if found:
            report["sizes"][name]["compare"] = compare
            await page.screenshot(
                path=str(outdir / f"responsive-{name}-compare.png"))
        await page.context.close()

    report["problems"] = problems
    return report


async def shoot(outdir: Path, base: str) -> int:
    """Check the console at phone, tablet and laptop widths.

    Args:
        outdir: Where to write the screenshots and the report.
        base: The deployment's origin.

    Returns:
        Process exit status, non-zero when a layout is wrong.
    """
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        try:
            report = await run(browser, base, outdir)
        except PlaywrightTimeout as e:
            report = {"problems": [f"the console stopped answering: {e}"]}
        await browser.close()
    (outdir / "report.json").write_text(json.dumps(report, indent=1))

    for name, got in report.get("sizes", {}).items():
        m = got["metrics"]
        print(f"{name:7} panel={m['panel']} strip={m['stripLayout']} "
              f"map={m['canvasWidth']}x{m['canvasHeight']} "
              f"compare={got.get('compare', {}).get('paneLayout')}")
    if report["problems"]:
        print("\nFAIL:")
        for p in report["problems"]:
            print(f"  {p}")
        return 1
    print(f"\nok: no sideways scroll, the panel yields on a phone, the strip "
          f"takes its own row and the panes stack; images in {outdir}")
    return 0


def main() -> int:
    """Parse arguments and run.

    Returns:
        Process exit status.
    """
    ap = argparse.ArgumentParser(description="check the responsive layout")
    ap.add_argument("outdir", type=Path)
    ap.add_argument("--base", default=DEFAULT_BASE)
    args = ap.parse_args()
    args.outdir.mkdir(parents=True, exist_ok=True)
    return asyncio.run(shoot(args.outdir, args.base))


if __name__ == "__main__":
    sys.exit(main())
