#!/usr/bin/env python3
"""The console's controls have to reach the map, and come back after a reload.

The pure modules are asserted in ``validation_tests/js``: the opacity
vocabulary in ``test_map_opacity.mjs``, the speed in ``test_map_playback.mjs``
and the axis geometry in ``test_map_time_axis.mjs``. None of them can see
whether a slider is wired to the layer it names, whether a setting survives
the round trip through storage, or whether the axis the strip draws is the
measured one. Each of those has been the real defect in a slice of this
console, so this drives the deployed page.

Two checks are the reason it exists.

A slider must move the layer it is labeled with and no other. Three raster
layers stack on the same ground, and the single slider this replaces moved
the radars while the composite and NEXRAD sat at constants, so a slider wired
to the wrong one would look exactly like the old behavior.

The paint value is read back off MapLibre through ``?diag=probe`` rather than
off the control, because a slider that updates its own readout and nothing
else is the failure this is looking for.

Usage
-----
::

    python validation_tests/visual/shoot_controls.py OUTDIR [--base URL]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

VISUAL = Path(__file__).resolve().parent
sys.path.insert(0, str(VISUAL))

try:
    from playwright.async_api import Browser, Page, async_playwright
    from playwright.async_api import TimeoutError as PlaywrightTimeout
except ImportError:  # pragma: no cover - environment check
    print("playwright is not installed: pip install playwright "
          "&& playwright install chromium", file=sys.stderr)
    raise SystemExit(2) from None

from capture import (  # noqa: E402  sibling module, loaded by path
    open_dock, open_settings)

DEFAULT_BASE = "https://the test deployment"

LOAD_SETTLE_MS = 7000
ACTION_SETTLE_MS = 1500

VIEWPORT = {"width": 1600, "height": 950}

# What each slider opens at, and which MapLibre layer it has to move. These
# are the values that shipped before the sliders existed, so a default that
# drifted would repaint every console on upgrade.
LAYER_PAINT = {
    "composite": ("comp-overlay-layer", 70),
    "radars": (None, 80),
    "nexrad": ("nexrad-layer", 60),
}

# Where each slider is driven to, as a percent.
MOVED_TO = 25


async def probe(page: Page) -> dict[str, Any]:
    """Read the map's own layer definitions.

    Args:
        page: The console, loaded with ?diag=probe.

    Returns:
        The probe snapshot, or an empty dict when it is not published.
    """
    got: dict[str, Any] | None = await page.evaluate(
        "() => window.__sentinelProbe ? window.__sentinelProbe() : null")
    return got or {}


async def paint_of(page: Page, layer: str) -> float | None:
    """One layer's raster opacity, as MapLibre holds it.

    Args:
        page: The loaded console.
        layer: The MapLibre layer id.

    Returns:
        The opacity, or None when the layer is not on the map.
    """
    snap = await probe(page)
    entry = (snap.get("layers") or {}).get(layer)
    if not entry:
        return None
    value = (entry.get("paint") or {}).get("raster-opacity")
    return None if value is None else float(value)


async def sliders(page: Page) -> dict[str, int]:
    """Each opacity slider's percent, by layer.

    Args:
        page: The loaded console.

    Returns:
        One entry per slider found.
    """
    js = """
      () => Object.fromEntries(
        [...document.querySelectorAll('[data-opacity]')]
          .map((el) => [el.getAttribute('data-opacity'), Number(el.value)]))
    """
    got: dict[str, int] = await page.evaluate(js)
    return got


async def strip(page: Page) -> dict[str, Any]:
    """The time strip's published state.

    Args:
        page: The loaded console.

    Returns:
        The strip state, or an empty dict.
    """
    js = """
      () => {
        const el = document.querySelector('[data-time-strip]');
        return el ? JSON.parse(el.getAttribute('data-time-strip')) : null;
      }
    """
    got: dict[str, Any] | None = await page.evaluate(js)
    return got or {}


async def axis(page: Page) -> dict[str, Any]:
    """What the time axis reports about itself.

    Args:
        page: The loaded console.

    Returns:
        The axis summary, or an empty dict.
    """
    js = """
      () => {
        const el = document.querySelector('[data-time-axis]');
        return el ? JSON.parse(el.getAttribute('data-time-axis')) : null;
      }
    """
    got: dict[str, Any] | None = await page.evaluate(js)
    return got or {}


async def open_console(browser: Browser, base: str) -> Page:
    """Load the console with the panel open and the probe published.

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
    # Seeded only when nothing is stored. An init script runs on every
    # navigation, so seeding unconditionally overwrites what the console
    # just saved and makes the reload check pass against its own fixture.
    await page.add_init_script(
        "if (!localStorage.getItem('sentinel-map-prefs')) {"
        "  localStorage.setItem('sentinel-theme', 'dark');"
        "  localStorage.setItem('sentinel-map-prefs', JSON.stringify("
        "    {activeRadars:['XSCV'], moment:'Reflectivity',"
        "     composite:'comp_ref', nexrad:true, panelOpen:true}));"
        "}")
    await page.goto(f"{base}/?diag=probe", wait_until="load", timeout=45_000)
    await page.wait_for_timeout(LOAD_SETTLE_MS)
    await open_dock(page)
    # The opacity sliders moved out of the dock and into the toolbar's
    # settings menu, so reaching them is two openings rather than one.
    await open_settings(page)
    return page


async def set_slider(page: Page, layer: str, percent: int) -> None:
    """Drive one opacity slider to a value.

    Args:
        page: The loaded console.
        layer: The slider's data-opacity key.
        percent: Where to put it.
    """
    await page.locator(f'[data-opacity="{layer}"]').fill(str(percent))
    await page.locator(f'[data-opacity="{layer}"]').dispatch_event("input")
    await page.wait_for_timeout(ACTION_SETTLE_MS)


async def check_defaults(page: Page, problems: list[str]) -> None:
    """Three sliders, opening where the constants used to sit.

    Args:
        page: The loaded console.
        problems: Collects failure messages.
    """
    found = await sliders(page)
    if set(found) != set(LAYER_PAINT):
        problems.append(
            f"the panel offers {sorted(found)}, expected {sorted(LAYER_PAINT)}")
        return
    for layer, (_, want) in LAYER_PAINT.items():
        if found[layer] != want:
            problems.append(
                f"{layer} opens at {found[layer]}%, not the {want}% it "
                f"shipped at; upgrading would repaint every console")
    for layer, (map_layer, want) in LAYER_PAINT.items():
        if map_layer is None:
            continue
        got = await paint_of(page, map_layer)
        if got is None:
            problems.append(f"{map_layer} is not on the map, so the {layer} "
                            f"slider cannot be checked against it")
        elif abs(got - want / 100) > 1e-6:
            problems.append(
                f"{map_layer} is painted at {got}, not the {want / 100} its "
                f"slider reports")


async def check_isolation(page: Page, problems: list[str]) -> None:
    """Moving one slider moves its layer and leaves the others alone.

    Args:
        page: The loaded console.
        problems: Collects failure messages.
    """
    before = {}
    for map_layer, _ in LAYER_PAINT.values():
        if map_layer:
            before[map_layer] = await paint_of(page, map_layer)

    await set_slider(page, "composite", MOVED_TO)
    got = await paint_of(page, "comp-overlay-layer")
    if got is None or abs(got - MOVED_TO / 100) > 1e-6:
        problems.append(
            f"the composite slider left the layer at {got}, expected "
            f"{MOVED_TO / 100}; the slider is not wired to the map")
    other = await paint_of(page, "nexrad-layer")
    was = before.get("nexrad-layer")
    if other is not None and was is not None and abs(other - was) > 1e-6:
        problems.append(
            f"moving the composite also moved NEXRAD, {was} to {other}; the "
            f"sliders are not independent")

    await set_slider(page, "nexrad", 0)
    zeroed = await paint_of(page, "nexrad-layer")
    if zeroed is None or zeroed != 0:
        problems.append(f"NEXRAD at 0% paints at {zeroed}")
    text = await page.locator("[data-opacity-panel]").inner_text()
    if "hidden" not in text.lower():
        problems.append(
            "a layer at 0% does not say it is hidden; it is still switched "
            "on and still fetching, so nothing explains the blank")


async def check_persistence(page: Page, base: str,
                            problems: list[str]) -> Page:
    """The settings come back after a reload.

    Args:
        page: The loaded console.
        base: The deployment's origin.
        problems: Collects failure messages.

    Returns:
        The reloaded page.
    """
    await page.select_option("[data-speed]", "2")
    await page.wait_for_timeout(ACTION_SETTLE_MS)
    if (await strip(page)).get("speed") != 2:
        problems.append(f"the speed control reads "
                        f"{(await strip(page)).get('speed')} after selecting 2x")

    await page.goto(f"{base}/?diag=probe", wait_until="load", timeout=45_000)
    await page.wait_for_timeout(LOAD_SETTLE_MS)
    # A narrow window or a stored preference can leave the dock closed
    # after a reload, and the settings menu always opens closed. The
    # opacities are the setting this checks came back.
    await open_dock(page)
    await open_settings(page)
    after = await sliders(page)
    if after.get("composite") != MOVED_TO:
        problems.append(
            f"the composite opacity came back as {after.get('composite')}, "
            f"not the {MOVED_TO} it was left at")
    if after.get("nexrad") != 0:
        problems.append(
            f"a layer left at 0 came back as {after.get('nexrad')}; zero is a "
            f"setting, not a missing value")
    if (await strip(page)).get("speed") != 2:
        problems.append(
            f"the speed came back as {(await strip(page)).get('speed')}")
    painted = await paint_of(page, "comp-overlay-layer")
    if painted is None or abs(painted - MOVED_TO / 100) > 1e-6:
        problems.append(
            f"the restored opacity reached the control but not the map, "
            f"painted at {painted}")
    return page


async def check_axis(page: Page, problems: list[str]) -> None:
    """The strip places frames by time, and a click lands on one.

    Args:
        page: The loaded console.
        problems: Collects failure messages.
    """
    report = await axis(page)
    if not report:
        problems.append("the strip published no axis")
        return
    if report.get("even") is not False:
        problems.append(
            f"the axis fell back to even spacing: {report}; the frames carry "
            f"timestamps, so it should be measured")
    if not report.get("cadenceMs"):
        problems.append(f"the axis found no cadence: {report}")

    total = (await strip(page)).get("totalSteps") or 0
    if total < 3:
        problems.append(f"{total} steps, too few to check a scrub")
        return
    track = page.locator("[data-time-axis] .cursor-pointer").first
    box = await track.bounding_box()
    if not box:
        problems.append("the activity track has no box to click")
        return
    await page.mouse.click(box["x"] + 2, box["y"] + box["height"] / 2)
    await page.wait_for_timeout(ACTION_SETTLE_MS)
    first = (await strip(page)).get("stepIdx")
    if first != 0:
        problems.append(f"clicking the left edge selected step {first}, not 0")
    await page.mouse.click(box["x"] + box["width"] - 2,
                           box["y"] + box["height"] / 2)
    await page.wait_for_timeout(ACTION_SETTLE_MS)
    last = (await strip(page)).get("stepIdx")
    if last != total - 1:
        problems.append(
            f"clicking the right edge selected step {last}, not {total - 1}")


async def run(browser: Browser, base: str, outdir: Path) -> dict[str, Any]:
    """Drive the console's controls.

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
    report["sliders"] = await sliders(page)
    report["axis"] = await axis(page)
    report["strip"] = await strip(page)

    await check_defaults(page, problems)
    await page.screenshot(path=str(outdir / "controls-defaults.png"))
    await check_isolation(page, problems)
    await page.screenshot(path=str(outdir / "controls-moved.png"))
    page = await check_persistence(page, base, problems)
    await page.screenshot(path=str(outdir / "controls-reloaded.png"))
    await check_axis(page, problems)
    await page.screenshot(path=str(outdir / "controls-axis.png"))

    report["after"] = await sliders(page)
    await page.context.close()
    report["problems"] = problems
    return report


async def shoot(outdir: Path, base: str) -> int:
    """Check the console's controls on the deployed page.

    Args:
        outdir: Where to write the screenshots and the report.
        base: The deployment's origin.

    Returns:
        Process exit status, non-zero when a control is wrong.
    """
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        try:
            report = await run(browser, base, outdir)
        except PlaywrightTimeout as e:
            report = {"problems": [f"the console stopped answering: {e}"]}
        await browser.close()
    (outdir / "report.json").write_text(json.dumps(report, indent=1))

    if "sliders" in report:
        print(f"sliders: {report['sliders']}")
        print(f"axis: {report['axis']}")
    if report["problems"]:
        print("\nFAIL:")
        for p in report["problems"]:
            print(f"  {p}")
        return 1
    print(f"\nok: each slider moves its own layer, the settings survive a "
          f"reload, and the strip places frames by time; images in {outdir}")
    return 0


def main() -> int:
    """Parse arguments and run.

    Returns:
        Process exit status.
    """
    ap = argparse.ArgumentParser(description="check the console controls")
    ap.add_argument("outdir", type=Path)
    ap.add_argument("--base", default=DEFAULT_BASE)
    args = ap.parse_args()
    args.outdir.mkdir(parents=True, exist_ok=True)
    return asyncio.run(shoot(args.outdir, args.base))


if __name__ == "__main__":
    sys.exit(main())
