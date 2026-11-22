#!/usr/bin/env python3
"""The measure tool has to measure the map, and not act on what it clicks.

The geometry is asserted in ``validation_tests/js/test_map_measure.mjs``
against Vincenty on WGS84. What that cannot see is whether the tool is wired
to the map at all, whether a click while measuring also toggles the radar
underneath it, and whether the distance on screen matches the ground the two
clicks actually landed on.

The last one is the check this exists for. Every other way of getting it
wrong produces a plausible number: the pixels are converted to coordinates
by MapLibre, so a tool reading the wrong pair, or reading them in the wrong
order, still reports a distance that looks like a distance.

So this clicks two points whose screen separation is known, asks the page
what coordinates those pixels are, computes the great-circle distance here,
and compares it with what the readout says.

Usage
-----
::

    python validation_tests/visual/shoot_measure.py OUTDIR [--base URL]
"""
from __future__ import annotations

import os
import argparse
import asyncio
import json
import math
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

LOAD_SETTLE_MS = 7000
ACTION_SETTLE_MS = 900

VIEWPORT = {"width": 1600, "height": 950}

# Mean Earth radius in kilometers, the same sphere the console uses.
EARTH_RADIUS_KM = 6371.0088

# Longitude, latitude, zoom, bearing and pitch, through the permalink. North
# up and flat, so the unprojection below needs no rotation.
CAMERA_QUERY = "c=-122.6,37.95,7.0,0,0"

# Beyond half a unit of whatever precision the readout printed. The readout
# drops decimals above 100 km on purpose, since one screen pixel at zoom 7
# is about 600 m, so a fixed percentage would either fail on that rounding
# or be loose enough to miss a real error on a short leg.
DISTANCE_SLACK_KM = 0.001


def haversine_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Great-circle distance between two points.

    Args:
        a: Longitude and latitude of the first point, in degrees.
        b: Longitude and latitude of the second point, in degrees.

    Returns:
        The distance in kilometers.
    """
    lon1, lat1 = math.radians(a[0]), math.radians(a[1])
    lon2, lat2 = math.radians(b[0]), math.radians(b[1])
    h = (math.sin((lat2 - lat1) / 2) ** 2
         + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2)
    return 2 * EARTH_RADIUS_KM * math.asin(min(1.0, math.sqrt(h)))


async def unproject(page: Page, x: float, y: float) -> tuple[float, float]:
    """What coordinates a pixel of the map is over.

    Args:
        page: The loaded console.
        x: Pixel offset from the canvas's left edge.
        y: Pixel offset from its top edge.

    Returns:
        Longitude and latitude.

    Raises:
        RuntimeError: If the probe reports no camera.
    """
    js = """
      ([x, y]) => {
        const p = window.__sentinelProbe ? window.__sentinelProbe() : null;
        const c = p && p.camera;
        if (!c) return null;
        // Web Mercator, from the camera the probe reports. The console's
        // own readout is derived the same way, so this is independent of
        // the measure tool and not of the projection.
        const canvas = document.querySelector('.maplibregl-canvas');
        const w = canvas.clientWidth, h = canvas.clientHeight;
        const scale = 512 * Math.pow(2, c.zoom);
        const cx = (c.center[0] + 180) / 360 * scale;
        const sin = Math.sin(c.center[1] * Math.PI / 180);
        const cy = (0.5 - Math.log((1 + sin) / (1 - sin)) / (4 * Math.PI))
          * scale;
        const px = cx + (x - w / 2);
        const py = cy + (y - h / 2);
        const lng = px / scale * 360 - 180;
        const n = Math.PI - 2 * Math.PI * py / scale;
        const lat = 180 / Math.PI * Math.atan(0.5 * (Math.exp(n)
          - Math.exp(-n)));
        return [lng, lat];
      }
    """
    got: list[float] | None = await page.evaluate(js, [x, y])
    if got is None:
        raise RuntimeError("the probe reported no camera")
    return (got[0], got[1])


async def readout(page: Page) -> str:
    """The measure panel's text, or '' when it is not shown.

    Args:
        page: The loaded console.

    Returns:
        The panel's text.
    """
    panel = page.locator("[data-measure-readout]")
    if await panel.count() == 0:
        return ""
    text: str = await panel.first.inner_text()
    return text


async def active_radars(page: Page) -> list[str]:
    """Which radars the pane has switched on.

    Args:
        page: The loaded console.

    Returns:
        The radar ids, sorted.
    """
    js = """
      () => {
        const el = document.querySelector('[data-view]');
        return el ? JSON.parse(el.getAttribute('data-view')).radars : [];
      }
    """
    got: list[str] = await page.evaluate(js)
    return got


async def open_console(browser: Browser, base: str) -> Page:
    """Load the console with the probe published and no radars on.

    Args:
        browser: The running chromium.
        base: The deployment's origin.

    Returns:
        The loaded page.

    Raises:
        PlaywrightTimeout: If the console does not load.
    """
    ctx = await browser.new_context(viewport=VIEWPORT, device_scale_factor=1)
    page = await ctx.new_page()
    await page.add_init_script(
        "localStorage.setItem('sentinel-theme', 'dark');"
        "localStorage.setItem('sentinel-map-prefs', JSON.stringify("
        "{activeRadars:[], moment:'Reflectivity', composite:'comp_ref',"
        " panelOpen:true}));")
    # The camera is pinned north-up and flat through the permalink, because
    # the unprojection below is plain Web Mercator. The console's default
    # bearing is about 22 degrees, taken from the radar layout, and
    # measuring against a rotated map without rotating the math put the
    # reference 0.8 km out on a 216 km leg.
    await page.goto(f"{base}/?diag=probe&{CAMERA_QUERY}", wait_until="load",
                    timeout=45_000)
    await page.wait_for_timeout(LOAD_SETTLE_MS)
    return page


def parse_km(text: str) -> tuple[float, float] | None:
    """The first distance in a readout, and the precision it was printed at.

    Args:
        text: The panel's text.

    Returns:
        Kilometers and the width of its last digit, or None when there is
        no distance in the text.
    """
    m = re.search(r"([\d.]+)\s*km", text)
    if not m:
        return None
    shown = m.group(1)
    places = len(shown.split(".")[1]) if "." in shown else 0
    return float(shown), 10.0 ** -places


async def check_toggle(page: Page, problems: list[str]) -> None:
    """The tool turns on from its control and off from Escape.

    Args:
        page: The loaded console.
        problems: Collects failure messages.
    """
    if await readout(page) != "":
        problems.append("the measure panel is shown before the tool is on")
    toggle = page.locator("[data-measure-toggle]")
    if await toggle.count() == 0:
        problems.append("no control turns the measure tool on")
        return
    await toggle.first.click()
    await page.wait_for_timeout(ACTION_SETTLE_MS)
    if "no points yet" not in (await readout(page)).lower():
        problems.append(
            f"the panel does not say what to do with no points yet: "
            f"{(await readout(page))[:80]!r}")


async def check_measurement(page: Page, problems: list[str]) -> None:
    """Two clicks report the distance between the ground they landed on.

    Args:
        page: The loaded console.
        problems: Collects failure messages.
    """
    box = await page.locator(".maplibregl-canvas").first.bounding_box()
    if not box:
        problems.append("the map canvas has no box to click")
        return
    # Checked rather than assumed. A rotated or pitched camera makes the
    # plain Mercator unprojection below wrong, and the comparison would
    # then fail against the harness rather than against the tool.
    camera = await page.evaluate(
        "() => window.__sentinelProbe ? window.__sentinelProbe().camera : null")
    if not camera or abs(camera["bearing"]) > 1e-6 \
            or abs(camera["pitch"]) > 1e-6:
        problems.append(
            f"the camera is not north up and flat: {camera}; the reference "
            f"distance would be computed against the wrong geometry")
        return
    # Two points well apart on screen, away from the layer panel.
    ax, ay = 300.0, 300.0
    bx, by = 700.0, 620.0
    a = await unproject(page, ax, ay)
    b = await unproject(page, bx, by)
    want = haversine_km(a, b)

    await page.mouse.click(box["x"] + ax, box["y"] + ay)
    await page.wait_for_timeout(ACTION_SETTLE_MS)
    one = await readout(page)
    if "one point" not in one.lower():
        problems.append(f"after one click the panel says {one[:80]!r}")

    await page.mouse.click(box["x"] + bx, box["y"] + by)
    await page.wait_for_timeout(ACTION_SETTLE_MS)
    two = await readout(page)
    parsed = parse_km(two)
    if parsed is None:
        problems.append(f"no distance in the panel after two clicks: "
                        f"{two[:120]!r}")
        return
    got, step = parsed
    allowed = step / 2 + DISTANCE_SLACK_KM
    if abs(got - want) > allowed:
        problems.append(
            f"the panel reports {got} km where those two pixels are "
            f"{want:.3f} km apart, over the {allowed:.3f} km its own "
            f"precision allows; the tool is measuring something else")
    if "true" not in two:
        problems.append(
            f"the bearing does not say it is true: {two[:120]!r}")
    if "°" not in two:
        problems.append("no bearing beside the distance")


async def check_no_side_effect(page: Page, problems: list[str]) -> None:
    """A click while measuring does not also act on what is under it.

    Args:
        page: The loaded console.
        problems: Collects failure messages.
    """
    before = await active_radars(page)
    if await page.locator("[data-view]").count() == 0:
        problems.append("no pane to read the radar selection from")
        return
    box = await page.locator(".maplibregl-canvas").first.bounding_box()
    if not box:
        return
    # Near the middle of the default view, where the X-band pins sit. The
    # click adds a measure point either way, and the radar set must not
    # change whether or not a pin happened to be under it.
    await page.mouse.click(box["x"] + 820, box["y"] + 520)
    await page.wait_for_timeout(ACTION_SETTLE_MS)
    after = await active_radars(page)
    if after != before:
        problems.append(
            f"a click while measuring changed the radar selection, "
            f"{before} to {after}; the click both measured and acted")


async def check_undo_and_escape(page: Page, problems: list[str]) -> None:
    """Backspace drops a point and Escape leaves the tool.

    Args:
        page: The loaded console.
        problems: Collects failure messages.
    """
    legs_before = (await readout(page)).count("km")
    await page.locator("body").press("Backspace")
    await page.wait_for_timeout(ACTION_SETTLE_MS)
    legs_after = (await readout(page)).count("km")
    if legs_after >= legs_before:
        problems.append(
            f"Backspace did not drop a point, {legs_before} distances "
            f"before and {legs_after} after")

    await page.locator("body").press("Escape")
    await page.wait_for_timeout(ACTION_SETTLE_MS)
    if await readout(page) != "":
        problems.append("Escape did not stop the measure tool")

    # And the keyboard turns it back on, since the control and the key have
    # to reach the same place.
    await page.locator("body").press("m")
    await page.wait_for_timeout(ACTION_SETTLE_MS)
    if await readout(page) == "":
        problems.append("m did not start the measure tool")
    await page.locator("body").press("Escape")
    await page.wait_for_timeout(ACTION_SETTLE_MS)
    if await readout(page) != "":
        problems.append("Escape did not stop it the second time")


async def run(browser: Browser, base: str, outdir: Path) -> dict[str, Any]:
    """Drive the measure tool on the deployed console.

    Args:
        browser: The running chromium.
        base: The deployment's origin.
        outdir: Where the screenshots go.

    Returns:
        What each step showed, with a "problems" list.
    """
    report: dict[str, Any] = {"base": base}
    problems: list[str] = []
    page = await open_console(browser, base)

    await check_toggle(page, problems)
    await check_measurement(page, problems)
    report["readout"] = await readout(page)
    await page.screenshot(path=str(outdir / "measure-two-points.png"))
    await check_no_side_effect(page, problems)
    await page.screenshot(path=str(outdir / "measure-three-points.png"))
    await check_undo_and_escape(page, problems)
    await page.screenshot(path=str(outdir / "measure-off.png"))

    await page.context.close()
    report["problems"] = problems
    return report


async def shoot(outdir: Path, base: str) -> int:
    """Check the measure tool on the deployed console.

    Args:
        outdir: Where to write the screenshots and the report.
        base: The deployment's origin.

    Returns:
        Process exit status, non-zero when the tool is wrong.
    """
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        try:
            report = await run(browser, base, outdir)
        except PlaywrightTimeout as e:
            report = {"problems": [f"the console stopped answering: {e}"]}
        await browser.close()
    (outdir / "report.json").write_text(json.dumps(report, indent=1))

    if report.get("readout"):
        print("readout after two clicks:")
        for line in report["readout"].splitlines():
            print(f"  {line}")
    if report["problems"]:
        print("\nFAIL:")
        for p in report["problems"]:
            print(f"  {p}")
        return 1
    print(f"\nok: the tool measures the ground its clicks landed on, acts on "
          f"nothing else, and stops when told; images in {outdir}")
    return 0


def main() -> int:
    """Parse arguments and run.

    Returns:
        Process exit status.
    """
    ap = argparse.ArgumentParser(description="check the measure tool")
    ap.add_argument("outdir", type=Path)
    ap.add_argument("--base", default=DEFAULT_BASE)
    args = ap.parse_args()
    args.outdir.mkdir(parents=True, exist_ok=True)
    return asyncio.run(shoot(args.outdir, args.base))


if __name__ == "__main__":
    sys.exit(main())
