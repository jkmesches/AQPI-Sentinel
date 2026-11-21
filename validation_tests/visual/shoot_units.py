#!/usr/bin/env python3
"""Switching to a unit nobody publishes any more has to say so.

Three of AQPI's six multi-unit products stopped writing their millimeter
subtree on 2026-09-30 while still writing inches every two minutes. The
manifest for the stale unit parses, lists a full 31 steps and looks entirely
healthy; only its timestamps give it away. So a reader who switches units
gets seven-week-old rainfall that is pixel-for-pixel as convincing as this
hour's.

Two things guard that, and this checks both on the deployed console. The
unit control marks the stale option before it is chosen, and a banner over
the map says so after it is chosen.

The banner has to be its own, which is the part that is easy to get wrong.
The existing stale-composite banner reads the backend's L1 verdict for the
product, and the L1 checks read the canonical unit by design, so they stay
green while a stale alternate unit fills the screen. Relying on it here was
the first thing this script caught.

Usage
-----
::

    python validation_tests/visual/shoot_units.py OUTDIR [--base URL]
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

# The console polls every 5 s and holds a websocket, so networkidle never
# settles. Same waits capture.py measured.
LOAD_SETTLE_MS = 6000
SWITCH_SETTLE_MS = 3500

VIEWPORT = {"width": 1600, "height": 1000}

# A product whose second unit the mount last showed stale, and that unit.
PRODUCT = "qpe_1hr"
LIVE_UNIT = "in"
STALE_UNIT = "mm"
# The date the millimeter subtree stopped, which both the control and the
# step list should agree on.
STALE_SINCE = "2026-09-30"


async def open_console(browser: Browser, base: str, unit: str) -> Page:
    """Load the console with one product and unit already selected.

    Args:
        browser: The running chromium.
        base: The deployment's origin.
        unit: Which unit to start on.

    Returns:
        The loaded page.

    Raises:
        PlaywrightTimeout: If the console does not load.
    """
    ctx = await browser.new_context(viewport=VIEWPORT, device_scale_factor=2)
    page = await ctx.new_page()
    await page.add_init_script(
        "localStorage.setItem('sentinel-map-prefs', JSON.stringify("
        f"{{activeRadars:[], moment:'Reflectivity',"
        f" composite:'{PRODUCT}'}}));"
        "localStorage.setItem('sentinel-theme', 'dark');")
    await page.goto(base, wait_until="load", timeout=45_000)
    await page.wait_for_timeout(LOAD_SETTLE_MS)
    if unit != LIVE_UNIT:
        await page.get_by_role("button", name=unit, exact=False).first.click()
        await page.wait_for_timeout(SWITCH_SETTLE_MS)
    return page


async def inspect(page: Page) -> dict[str, Any]:
    """Read what the console is saying about units and staleness.

    Args:
        page: The loaded console.

    Returns:
        The control's buttons, any unit warning, and the stale banner text.
    """
    js = """
      () => {
        // By attribute, not by text. A div's textContent includes every
        // descendant's, so matching on text picks the outermost ancestor
        // that happens to contain the phrase, which here was the whole
        // page.
        const warn = [...document.querySelectorAll('[data-unit-warning]')]
          .map((d) => d.textContent.trim());
        const banner = [...document.querySelectorAll('[data-unit-banner]')]
          .map((d) => d.textContent.trim());
        const buttons = [...document.querySelectorAll('button')]
          .map((b) => b.textContent.trim())
          .filter((t) => /^(in|mm)(\\/h)?( \\u26a0)?$/.test(t));
        return { buttons, warn: warn[0] ?? null,
                 banner: banner.length ? banner[0].slice(0, 160) : null };
      }
    """
    got: dict[str, Any] = await page.evaluate(js)
    return got


def judge(live: dict[str, Any], stale: dict[str, Any]) -> list[str]:
    """Check that the console marks and then warns about the stale unit.

    Args:
        live: What it showed on the live unit.
        stale: What it showed after switching to the stale one.

    Returns:
        One message per defect.
    """
    problems: list[str] = []
    if len(live["buttons"]) < 2:
        problems.append(
            f"the unit control shows {live['buttons']}, so there is nothing "
            f"to switch between")
        return problems
    marked = [b for b in live["buttons"] if "\u26a0" in b]
    if not marked:
        problems.append(
            f"no unit is marked stale in {live['buttons']}, though "
            f"{STALE_UNIT} has not been published since {STALE_SINCE}")
    elif not any(STALE_UNIT in b for b in marked):
        problems.append(f"the marked unit is {marked}, expected {STALE_UNIT}")
    if live["warn"]:
        problems.append(
            f"the live unit warns anyway: {live['warn']!r}")
    if not stale["warn"]:
        problems.append(
            f"switching to {STALE_UNIT} raised no warning, so the reader is "
            f"shown {STALE_SINCE} rainfall with nothing saying so")
    elif STALE_SINCE not in stale["warn"]:
        problems.append(
            f"the warning {stale['warn']!r} does not name {STALE_SINCE}")
    if not stale["banner"]:
        problems.append(
            f"no banner appeared over the map on {STALE_UNIT}, so a reader "
            f"with the layers panel collapsed sees {STALE_SINCE} rainfall "
            f"with nothing saying so")
    elif STALE_SINCE not in stale["banner"]:
        problems.append(
            f"the banner {stale['banner']!r} does not name {STALE_SINCE}")
    if live["banner"]:
        problems.append(f"the live unit shows a banner too: "
                        f"{live['banner']!r}")
    return problems


async def shoot(outdir: Path, base: str) -> int:
    """Check both units on the deployed console and save the pictures.

    Args:
        outdir: Where to write the screenshots and the report.
        base: The deployment's origin.

    Returns:
        Process exit status, non-zero when the console fails to warn.
    """
    report: dict[str, Any] = {"base": base, "product": PRODUCT}
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        try:
            live_page = await open_console(browser, base, LIVE_UNIT)
            report["live"] = await inspect(live_page)
            await live_page.screenshot(path=str(outdir / "unit-live.png"))
            stale_page = await open_console(browser, base, STALE_UNIT)
            report["stale"] = await inspect(stale_page)
            await stale_page.screenshot(path=str(outdir / "unit-stale.png"))
        except PlaywrightTimeout:
            report["problems"] = ["the console did not load"]
            await browser.close()
            (outdir / "report.json").write_text(json.dumps(report, indent=1))
            print("FAIL: the console did not load")
            return 1
        await browser.close()

    report["problems"] = judge(report["live"], report["stale"])
    (outdir / "report.json").write_text(json.dumps(report, indent=1))
    print(f"units offered:   {report['live']['buttons']}")
    print(f"on {LIVE_UNIT}:          warn={report['live']['warn']!r} "
          f"banner={bool(report['live']['banner'])}")
    print(f"on {STALE_UNIT}:          warn={report['stale']['warn']!r}")
    print(f"                 banner={report['stale']['banner']!r}")
    if report["problems"]:
        print("\nFAIL:")
        for p in report["problems"]:
            print(f"  {p}")
        return 1
    print(f"\nok: the stale unit is marked before it is chosen and warned "
          f"about after; images in {outdir}")
    return 0


def main() -> int:
    """Parse arguments and run.

    Returns:
        Process exit status.
    """
    ap = argparse.ArgumentParser(description="check the unit control")
    ap.add_argument("outdir", type=Path)
    ap.add_argument("--base", default=DEFAULT_BASE)
    args = ap.parse_args()
    args.outdir.mkdir(parents=True, exist_ok=True)
    return asyncio.run(shoot(args.outdir, args.base))


if __name__ == "__main__":
    sys.exit(main())
