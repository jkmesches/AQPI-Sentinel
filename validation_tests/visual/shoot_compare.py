#!/usr/bin/env python3
"""Two panes have to differ in one thing and agree on everything else.

A comparison is only worth anything if the panes are genuinely locked. If
they drift in time, the reader is comparing two minutes rather than two
products; if they drift in view, they are comparing two places. Either way
the picture looks like a comparison and is not one, which is the failure
this whole console keeps running into.

So this turns compare on, changes one pane's product, and checks that the
panes hold the same view and the same frame time while the products
differ. It also checks there is exactly one time strip and one compare
toggle, since a second pane writing the shared chrome fights the first
for it.

Usage
-----
::

    python validation_tests/visual/shoot_compare.py OUTDIR [--base URL]
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
# settles. Same waits capture.py measured, plus room for a second map to
# build its style.
LOAD_SETTLE_MS = 6000
PANE_SETTLE_MS = 6000
ACTION_SETTLE_MS = 2500

# Wide, because two panes side by side need the room and a narrow window
# would be testing the responsive pass instead.
VIEWPORT = {"width": 2200, "height": 1100}

# What the second pane is switched to, so the two differ in one thing.
OTHER_PRODUCT = "qpe_1hr"

# How far apart the panes' cameras may sit and still count as locked. The
# published values are rounded to five places of longitude, which is about
# one meter, so anything above that rounding is a real drift.
CAMERA_EPS = 2e-5


async def pane_states(page: Page) -> list[dict[str, Any]]:
    """Read every pane's published view.

    Args:
        page: The loaded console.

    Returns:
        One entry per pane, in document order.
    """
    js = """
      () => [...document.querySelectorAll('[data-view]')]
        .map((el) => JSON.parse(el.getAttribute('data-view')))
    """
    got: list[dict[str, Any]] = await page.evaluate(js)
    return got


async def chrome_counts(page: Page) -> dict[str, int]:
    """Count the pieces there must be exactly one of.

    Args:
        page: The loaded console.

    Returns:
        How many panes, time strips, toggles and labels are on the page.
    """
    js = """
      () => ({
        panes: document.querySelectorAll('[data-view]').length,
        strips: document.querySelectorAll('[data-time-controls]').length,
        toggles: document.querySelectorAll('[data-compare-toggle]').length,
        labels: document.querySelectorAll('[data-pane-label]').length
      })
    """
    got: dict[str, int] = await page.evaluate(js)
    return got


async def canvas_sizes(page: Page) -> list[dict[str, float]]:
    """How much room each pane's map got.

    Two panes of different widths cover different ground at the same zoom,
    so the comparison would be between unequal areas.

    Args:
        page: The loaded console.

    Returns:
        One width and height per map canvas.
    """
    js = """
      () => [...document.querySelectorAll('.maplibregl-canvas')]
        .map((c) => ({ w: c.clientWidth, h: c.clientHeight }))
    """
    got: list[dict[str, float]] = await page.evaluate(js)
    return got


async def open_console(browser: Browser, base: str) -> Page:
    """Load the console with a known view and no stored prefs.

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
        "{activeRadars:[], moment:'Reflectivity', composite:'comp_ref'}));")
    await page.goto(base, wait_until="load", timeout=45_000)
    await page.wait_for_timeout(LOAD_SETTLE_MS)
    return page


def judge_single(counts: dict[str, int]) -> list[str]:
    """Check the console before compare is turned on.

    Args:
        counts: What chrome_counts found.

    Returns:
        One message per defect.
    """
    problems: list[str] = []
    if counts["panes"] != 1:
        problems.append(f"{counts['panes']} panes before compare is on")
    if counts["toggles"] != 1:
        problems.append(f"{counts['toggles']} compare toggles, expected 1")
    if counts["labels"] != 0:
        problems.append(
            f"{counts['labels']} pane labels with one pane; naming a pane "
            f"A when there is no B says there is a B")
    return problems


def judge_compare(counts: dict[str, int], panes: list[dict[str, Any]],
                  canvases: list[dict[str, float]]) -> list[str]:
    """Check the console with both panes open.

    Args:
        counts: What chrome_counts found.
        panes: Each pane's published view.
        canvases: Each map canvas's size.

    Returns:
        One message per defect.
    """
    problems: list[str] = []
    if counts["panes"] != 2:
        problems.append(f"{counts['panes']} panes with compare on")
        return problems
    if counts["strips"] != 1:
        problems.append(
            f"{counts['strips']} time strips; two would disagree about the "
            f"time the panes share")
    if counts["toggles"] != 1:
        problems.append(
            f"{counts['toggles']} compare toggles; the second pane must not "
            f"offer one, since turning compare off would remove the control "
            f"that did it")
    if counts["labels"] != 2:
        problems.append(
            f"{counts['labels']} pane labels; two maps of the same ground "
            f"are otherwise indistinguishable in a screenshot")
    if len(canvases) != 2:
        problems.append(f"{len(canvases)} map canvases, expected 2")
    elif abs(canvases[0]["w"] - canvases[1]["w"]) > 4:
        problems.append(
            f"the panes are {canvases[0]['w']} and {canvases[1]['w']} wide; "
            f"a comparison between unequal areas is not one")
    elif abs(canvases[0]["h"] - canvases[1]["h"]) > 4:
        # The first version of this checked only the width, and the time
        # strip sat inside the primary pane, so that pane was 80 px
        # shorter and the two covered different ground north to south.
        problems.append(
            f"the panes are {canvases[0]['h']} and {canvases[1]['h']} tall; "
            f"at one zoom they cover different ground")
    if panes[0] == panes[1]:
        problems.append(
            "both panes show the same view, so there is nothing to compare")
    return problems


def judge_locked(panes: list[dict[str, Any]]) -> list[str]:
    """Check the panes differ in the product and agree on the rest.

    Args:
        panes: Each pane's published view.

    Returns:
        One message per field that drifted.
    """
    if len(panes) != 2:
        return ["there are not two panes to compare"]
    problems: list[str] = []
    a, b = panes
    if a["product"] == b["product"]:
        problems.append(
            f"both panes are on {a['product']!r}, so the one thing being "
            f"compared is not different")
    if a["moment"] != b["moment"]:
        problems.append(
            f"moment drifted: {a['moment']!r} against {b['moment']!r}")
    return problems


def judge_cameras(panes: list[dict[str, Any]]) -> list[str]:
    """Check both panes settled on the same camera after one was dragged.

    Args:
        panes: Each pane's published view.

    Returns:
        One message per way they disagree.
    """
    if len(panes) != 2:
        return ["there are not two panes to compare"]
    a, b = (p.get("camera") for p in panes)
    if a is None or b is None:
        return [f"a pane published no camera: {a} and {b}; the lock cannot "
                f"be checked"]
    names = ("longitude", "latitude", "zoom")
    out: list[str] = []
    for name, x, y in zip(names, a, b, strict=True):
        if abs(x - y) > CAMERA_EPS:
            out.append(
                f"{name} drifted between the panes: {x} against {y}; they "
                f"are showing different places")
    return out


async def run(browser: Browser, base: str, outdir: Path) -> dict[str, Any]:
    """Open the console, turn compare on, and check the panes.

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

    report["single"] = await chrome_counts(page)
    problems += judge_single(report["single"])
    await page.screenshot(path=str(outdir / "compare-off.png"))

    await page.locator("[data-compare-toggle]").first.click()
    await page.wait_for_timeout(PANE_SETTLE_MS)
    report["counts"] = await chrome_counts(page)

    # Put the second pane on another product, so the panes differ in the
    # one thing a comparison is for.
    selects = page.locator("select")
    if await selects.count() >= 2:
        await selects.nth(1).select_option(OTHER_PRODUCT)
        await page.wait_for_timeout(ACTION_SETTLE_MS)

    # Drag one pane and check the other follows. This is the claim the
    # whole mode rests on: panes that drift in view are comparing two
    # places, and the picture looks like a comparison either way.
    box = await page.locator(".maplibregl-canvas").first.bounding_box()
    if box:
        await page.mouse.move(box["x"] + 200, box["y"] + 300)
        await page.mouse.down()
        await page.mouse.move(box["x"] + 340, box["y"] + 380, steps=10)
        await page.mouse.up()
        await page.wait_for_timeout(ACTION_SETTLE_MS)

    report["panes"] = await pane_states(page)
    report["canvases"] = await canvas_sizes(page)
    problems += judge_cameras(report["panes"])
    problems += judge_compare(report["counts"], report["panes"],
                              report["canvases"])
    problems += judge_locked(report["panes"])
    await page.screenshot(path=str(outdir / "compare-on.png"))

    # And off again, which has to leave one working pane behind.
    await page.locator("[data-compare-toggle]").first.click()
    await page.wait_for_timeout(ACTION_SETTLE_MS)
    report["after"] = await chrome_counts(page)
    if report["after"]["panes"] != 1:
        problems.append(
            f"{report['after']['panes']} panes after turning compare off")
    await page.screenshot(path=str(outdir / "compare-closed.png"))

    await page.context.close()
    report["problems"] = problems
    return report


async def shoot(outdir: Path, base: str) -> int:
    """Check compare mode on the deployed console.

    Args:
        outdir: Where to write the screenshots and the report.
        base: The deployment's origin.

    Returns:
        Process exit status, non-zero when the panes are not locked.
    """
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        try:
            report = await run(browser, base, outdir)
        except PlaywrightTimeout as e:
            report = {"problems": [f"the console stopped answering: {e}"]}
        await browser.close()
    (outdir / "report.json").write_text(json.dumps(report, indent=1))

    for pane in report.get("panes", []):
        print(f"pane: product={pane['product']!r} moment={pane['moment']!r} "
              f"radars={pane['radars']} camera={pane.get('camera')}")
    if "counts" in report:
        print(f"chrome: {report['counts']}")

    if report["problems"]:
        print("\nFAIL:")
        for p in report["problems"]:
            print(f"  {p}")
        return 1
    print(f"\nok: two panes, one time strip, different products and the "
          f"same everything else; images in {outdir}")
    return 0


def main() -> int:
    """Parse arguments and run.

    Returns:
        Process exit status.
    """
    ap = argparse.ArgumentParser(description="check compare mode")
    ap.add_argument("outdir", type=Path)
    ap.add_argument("--base", default=DEFAULT_BASE)
    args = ap.parse_args()
    args.outdir.mkdir(parents=True, exist_ok=True)
    return asyncio.run(shoot(args.outdir, args.base))


if __name__ == "__main__":
    sys.exit(main())
