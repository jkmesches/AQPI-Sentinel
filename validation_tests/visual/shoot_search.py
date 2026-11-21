#!/usr/bin/env python3
"""The search box has to find real places and move the camera to them.

The matcher is asserted in ``validation_tests/js/test_map_search.mjs``
against a fixture. What that cannot see is whether the box is fed this
deployment's actual datasets, whether a result moves the map, and whether a
watershed is fitted rather than centered at some fixed zoom.

The dataset check is the reason this exists. The matcher is happy with an
empty index, so a box wired to nothing returns "nothing by that name" for
every query and looks like a working search over a thin world.

Usage
-----
::

    python validation_tests/visual/shoot_search.py OUTDIR [--base URL]
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
TYPE_SETTLE_MS = 1200
FLY_SETTLE_MS = 2500

VIEWPORT = {"width": 1500, "height": 950}

# A query per dataset, so a box wired to only one of them fails here. The
# names come from the files the console serves: a radar in
# /api/radars/meta, a dam in /data/reservoirs-norcal.json and a HUC-8 in
# /data/watersheds-huc8-norcal.geojson.
QUERIES = [
    ("xscr", "radar"),
    ("shasta", "reservoir"),
    ("putah", "watershed"),
]

# How far the camera must move for a result to count as having been flown
# to, in degrees. The default view is in the Bay Area and every query above
# is a long way from the others.
MIN_MOVE_DEG = 0.05


async def camera(page: Page) -> list[float] | None:
    """The pane's published camera.

    Args:
        page: The loaded console.

    Returns:
        Longitude, latitude and zoom, or None before one is published.
    """
    js = """
      () => {
        const el = document.querySelector('[data-view]');
        return el ? JSON.parse(el.getAttribute('data-view')).camera : null;
      }
    """
    got: list[float] | None = await page.evaluate(js)
    return got


async def results(page: Page) -> list[dict[str, str]]:
    """What the result list is showing.

    Args:
        page: The loaded console.

    Returns:
        One entry per row, with its text and its kind.
    """
    js = """
      () => {
        const box = document.querySelector('[data-search-results]');
        if (!box) return [];
        return [...box.querySelectorAll('button')].map((b) => ({
          text: b.innerText.trim(),
          kind: (b.lastElementChild || {}).innerText || ''
        }));
      }
    """
    got: list[dict[str, str]] = await page.evaluate(js)
    return got


async def open_console(browser: Browser, base: str) -> Page:
    """Load the console with the layer panel open.

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
    await page.goto(base, wait_until="load", timeout=45_000)
    await page.wait_for_timeout(LOAD_SETTLE_MS)
    return page


async def type_query(page: Page, text: str) -> None:
    """Clear the box and type a query into it.

    Args:
        page: The loaded console.
        text: What to type.
    """
    box = page.locator("[data-place-search]")
    await box.first.click()
    await box.first.fill("")
    await box.first.type(text, delay=20)
    await page.wait_for_timeout(TYPE_SETTLE_MS)


async def check_datasets(page: Page, problems: list[str]) -> None:
    """Every dataset the box claims to search returns something.

    Args:
        page: The loaded console.
        problems: Collects failure messages.
    """
    for query, kind in QUERIES:
        await type_query(page, query)
        rows = await results(page)
        if not rows:
            problems.append(
                f"{query!r} found nothing; the {kind} dataset is not in the "
                f"index, so the box searches a thinner world than it says")
            continue
        kinds = {r["kind"].strip().lower() for r in rows}
        if kind not in kinds:
            problems.append(
                f"{query!r} returned {sorted(kinds)}, with no {kind}")


async def check_fly(page: Page, problems: list[str]) -> None:
    """Choosing a result moves the camera to it.

    Args:
        page: The loaded console.
        problems: Collects failure messages.
    """
    before = await camera(page)
    await type_query(page, "shasta")
    rows = await results(page)
    if not rows:
        problems.append("no result to choose for the camera check")
        return
    await page.locator("[data-search-results] button").first.click()
    await page.wait_for_timeout(FLY_SETTLE_MS)
    after = await camera(page)
    if before is None or after is None:
        problems.append(f"a camera was not published, {before} and {after}")
        return
    moved = max(abs(after[0] - before[0]), abs(after[1] - before[1]))
    if moved < MIN_MOVE_DEG:
        problems.append(
            f"the camera moved {moved:.4f} degrees, under {MIN_MOVE_DEG}; "
            f"choosing a result did not fly anywhere")
    # Shasta Dam is at about 40.72 north, well north of the Bay Area.
    if after[1] < 39.5:
        problems.append(
            f"the camera landed at {after[1]:.3f} north, which is not "
            f"Shasta; the wrong coordinates were used")
    if await page.locator("[data-search-results]").count() != 0:
        problems.append("the result list stayed open after a choice")


async def check_watershed_fit(page: Page, problems: list[str]) -> None:
    """A watershed is fitted rather than centered at a fixed zoom.

    Args:
        page: The loaded console.
        problems: Collects failure messages.
    """
    await type_query(page, "putah")
    rows = await results(page)
    if not rows:
        problems.append("no watershed result to fit")
        return
    await page.locator("[data-search-results] button").first.click()
    await page.wait_for_timeout(FLY_SETTLE_MS)
    after = await camera(page)
    if after is None:
        problems.append("no camera after choosing a watershed")
        return
    # A HUC-8 is tens of kilometers across, so fitting one lands between
    # about zoom 8 and 11. A fixed point zoom would land on its own value
    # whatever the shape's size.
    if not 7.0 <= after[2] <= 12.0:
        problems.append(
            f"the watershed left the camera at zoom {after[2]}, outside the "
            f"7 to 12 a HUC-8 fits in; it was not fitted to the shape")


async def check_miss(page: Page, problems: list[str]) -> None:
    """A query matching nothing says what was searched.

    Args:
        page: The loaded console.
        problems: Collects failure messages.
    """
    await type_query(page, "zzzznowhere")
    box = page.locator("[data-search-results]")
    if await box.count() == 0:
        problems.append("a miss shows nothing at all, not even an answer")
        return
    text = (await box.first.inner_text()).lower()
    for word in ("radars", "reservoirs", "watersheds"):
        if word not in text:
            problems.append(
                f"a miss does not say {word} were searched: {text[:90]!r}")


async def run(browser: Browser, base: str, outdir: Path) -> dict[str, Any]:
    """Drive the search box on the deployed console.

    Args:
        browser: The running chromium.
        base: The deployment's origin.
        outdir: Where the screenshots go.

    Returns:
        What the run showed, with a "problems" list.
    """
    report: dict[str, Any] = {"base": base}
    problems: list[str] = []
    page = await open_console(browser, base)

    if await page.locator("[data-place-search]").count() != 1:
        problems.append("there is no search box in the panel")
        await page.context.close()
        report["problems"] = problems
        return report

    await check_datasets(page, problems)
    report["results"] = await results(page)
    await page.screenshot(path=str(outdir / "search-results.png"))
    await check_fly(page, problems)
    await page.screenshot(path=str(outdir / "search-flown.png"))
    await check_watershed_fit(page, problems)
    await page.screenshot(path=str(outdir / "search-watershed.png"))
    await check_miss(page, problems)
    await page.screenshot(path=str(outdir / "search-miss.png"))

    report["camera"] = await camera(page)
    await page.context.close()
    report["problems"] = problems
    return report


async def shoot(outdir: Path, base: str) -> int:
    """Check the place search on the deployed console.

    Args:
        outdir: Where to write the screenshots and the report.
        base: The deployment's origin.

    Returns:
        Process exit status, non-zero when the search is wrong.
    """
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        try:
            report = await run(browser, base, outdir)
        except PlaywrightTimeout as e:
            report = {"problems": [f"the console stopped answering: {e}"]}
        await browser.close()
    (outdir / "report.json").write_text(json.dumps(report, indent=1))

    for row in report.get("results", []):
        print(f"  {row['text']}")
    if report["problems"]:
        print("\nFAIL:")
        for p in report["problems"]:
            print(f"  {p}")
        return 1
    print(f"\nok: every dataset answers, a result flies the camera to it, "
          f"and a watershed is fitted; images in {outdir}")
    return 0


def main() -> int:
    """Parse arguments and run.

    Returns:
        Process exit status.
    """
    ap = argparse.ArgumentParser(description="check the place search")
    ap.add_argument("outdir", type=Path)
    ap.add_argument("--base", default=DEFAULT_BASE)
    args = ap.parse_args()
    args.outdir.mkdir(parents=True, exist_ok=True)
    return asyncio.run(shoot(args.outdir, args.base))


if __name__ == "__main__":
    sys.exit(main())
