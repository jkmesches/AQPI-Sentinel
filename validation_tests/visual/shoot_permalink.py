#!/usr/bin/env python3
"""A link has to reopen the view it was copied from.

The encoding is asserted in ``validation_tests/js/test_map_permalink.mjs``,
which proves the string round-trips. What it cannot prove is that the
console writes a link describing what is on screen and then reads one back
into the same state. Those are two different pieces of wiring, and either
can be wrong while the encoder is right.

So this drives the deployed console. It sets a view, reads the address bar,
opens that URL in a fresh context, and checks the second console came up
the same. Then it opens a hand-written link naming a radar and a product
that do not exist, and checks the console drops them instead of asking the
backend for them.

Usage
-----
::

    python validation_tests/visual/shoot_permalink.py OUTDIR [--base URL]
"""
from __future__ import annotations

import os
import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

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
# settles. Same waits capture.py measured, plus the link debounce.
LOAD_SETTLE_MS = 6000
LINK_SETTLE_MS = 1200

VIEWPORT = {"width": 1600, "height": 1000}

# How far the reopened camera may sit from the one the link was taken at.
# The link rounds longitude and latitude to five places, about a meter, so
# anything past that rounding is a real drift.
CAMERA_EPS = 2e-5

# The view the first console is put into. Chosen so every field a link
# carries is non-default: a product with units, two radars, a moment that
# is not reflectivity, and overlays.
SET_VIEW = {
    "activeRadars": ["XEBY", "XSCR"],
    "moment": "Velocity",
    "composite": "qpe_1hr",
    "watersheds": True,
    "terrain": True
}

# What the console must read back out of its own link.
EXPECT = {
    "p": "qpe_1hr",
    "m": "Velocity",
    "r": "XEBY,XSCR",
    "u": "in",
    "o": "watersheds,terrain"
}

# A link naming things this deployment does not publish.
HOSTILE = "?p=nosuch&r=GONE,XEBY&m=Fictional&u=furlongs&c=999,999,99"


async def read_state(page: Page) -> dict[str, Any]:
    """Read what the console is showing, from its own controls.

    Args:
        page: The loaded console.

    Returns:
        The product, moment, radars and unit on screen.
    """
    js = """
      () => {
        // From data-view, which the console publishes for exactly this.
        // Scraping the panel's checkbox rows returned an empty list and
        // made every comparison pass by comparing nothing to nothing.
        const el = document.querySelector('[data-view]');
        const view = el ? JSON.parse(el.getAttribute('data-view')) : {};
        const warn = [...document.querySelectorAll('[data-unit-warning]')]
          .map((d) => d.textContent.trim());
        return { ...view, unitWarning: warn[0] ?? null,
                 url: location.search };
      }
    """
    got: dict[str, Any] = await page.evaluate(js)
    return got


async def open_with(browser: Browser, base: str, prefs: dict[str, Any] | None,
                    query: str = "") -> Page:
    """Open the console, optionally with prefs seeded or a query string.

    Args:
        browser: The running chromium.
        base: The deployment's origin.
        prefs: What to seed storage with, or None to leave it empty.
        query: A query string to open with, including the question mark.

    Returns:
        The loaded page.

    Raises:
        PlaywrightTimeout: If the console does not load.
    """
    ctx = await browser.new_context(viewport=VIEWPORT, device_scale_factor=2)
    page = await ctx.new_page()
    seed = "localStorage.setItem('sentinel-theme', 'dark');"
    if prefs is not None:
        seed += ("localStorage.setItem('sentinel-map-prefs', "
                 f"{json.dumps(json.dumps(prefs))});")
    await page.add_init_script(seed)
    await page.goto(base + query, wait_until="load", timeout=45_000)
    await page.wait_for_timeout(LOAD_SETTLE_MS + LINK_SETTLE_MS)
    return page


def judge_written(url: str) -> list[str]:
    """Check the console wrote a link describing the view it was put in.

    Args:
        url: The address bar's query string.

    Returns:
        One message per field that is missing or wrong.
    """
    problems: list[str] = []
    if not url:
        problems.append("the console wrote no link at all")
        return problems
    q = parse_qs(url.lstrip("?"))
    for key, want in EXPECT.items():
        got = (q.get(key) or [None])[0]
        if got != want:
            problems.append(f"the link's {key} is {got!r}, expected {want!r}")
    if "t" not in q:
        problems.append("the link carries no frame time")
    elif q["t"][0].isdigit():
        problems.append(
            f"the link's time is {q['t'][0]!r}, which looks like an index; "
            f"indices shift as the window rolls")
    if "c" not in q:
        problems.append("the link carries no camera")
    return problems


def judge_reopened(first: dict[str, Any], second: dict[str, Any]) -> list[str]:
    """Check the reopened console matches the one the link came from.

    Args:
        first: What the original console showed.
        second: What the reopened one showed.

    Returns:
        One message per field that did not survive.
    """
    problems: list[str] = []
    for field in ("product", "moment", "radars", "overlays", "unit"):
        if first[field] != second[field]:
            problems.append(
                f"{field} came back as {second[field]!r}, not "
                f"{first[field]!r}")
    # The view is the purpose of a link, and it was the one field this did
    # not compare. The camera was dropped on load for three days, because
    # applyLink runs before the map is constructed and quietly did nothing
    # when there was no map to jump.
    a, b = first.get("camera"), second.get("camera")
    if a is None or b is None:
        problems.append(
            f"a console published no camera, {a} and {b}; the view the link "
            f"was sent to show cannot be compared")
    else:
        names = ("longitude", "latitude", "zoom")
        for name, x, y in zip(names, a, b, strict=True):
            if abs(x - y) > CAMERA_EPS:
                problems.append(
                    f"{name} came back as {y}, not the {x} the link was "
                    f"taken at; the reader is looking somewhere else")
    return problems


def judge_hostile(state: dict[str, Any]) -> list[str]:
    """Check a link naming nothing real leaves the console on its defaults.

    Args:
        state: What the console showed after opening the hostile link.

    Returns:
        One message per value that got through.
    """
    problems: list[str] = []
    if "GONE" in state["radars"]:
        problems.append(
            f"a decommissioned radar survived into {state['radars']}")
    if state["product"] == "nosuch":
        problems.append(f"an unknown product survived: {state['product']!r}")
    if state["moment"] == "Fictional":
        problems.append(f"an unknown moment survived: {state['moment']!r}")
    if state["unit"] == "furlongs":
        problems.append("an unknown unit survived")
    return problems


async def run(browser: Browser, base: str, outdir: Path) -> dict[str, Any]:
    """Write a link, reopen it, then try a hostile one.

    Args:
        browser: The running chromium.
        base: The deployment's origin.
        outdir: Where the screenshots go.

    Returns:
        What each step showed, with a "problems" list.
    """
    report: dict[str, Any] = {"base": base}
    problems: list[str] = []

    first = await open_with(browser, base, SET_VIEW)
    # The drag puts the camera somewhere no fresh console opens at. A link
    # carrying the default view compares equal at the other end whether or
    # not the camera travels, and that is a check of nothing.
    box = await first.locator(".maplibregl-canvas").first.bounding_box()
    if box:
        await first.mouse.move(box["x"] + 260, box["y"] + 340)
        await first.mouse.down()
        await first.mouse.move(box["x"] + 430, box["y"] + 250, steps=12)
        await first.mouse.up()
        await first.wait_for_timeout(LINK_SETTLE_MS)
    state = await read_state(first)
    report["written"] = state
    await first.screenshot(path=str(outdir / "permalink-source.png"))
    problems += judge_written(state["url"])
    await first.context.close()

    if state["url"]:
        # A fresh context, so nothing but the link carries the view over.
        second = await open_with(browser, base, None, state["url"])
        reopened = await read_state(second)
        report["reopened"] = reopened
        await second.screenshot(path=str(outdir / "permalink-reopened.png"))
        problems += judge_reopened(state, reopened)
        await second.context.close()

    hostile = await open_with(browser, base, None, HOSTILE)
    report["hostile"] = await read_state(hostile)
    await hostile.screenshot(path=str(outdir / "permalink-hostile.png"))
    problems += judge_hostile(report["hostile"])
    await hostile.context.close()

    report["problems"] = problems
    return report


async def shoot(outdir: Path, base: str) -> int:
    """Check the permalink on the deployed console.

    Args:
        outdir: Where to write the screenshots and the report.
        base: The deployment's origin.

    Returns:
        Process exit status, non-zero when a link does not survive.
    """
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        try:
            report = await run(browser, base, outdir)
        except PlaywrightTimeout:
            report = {"problems": ["the console did not load"]}
        await browser.close()
    (outdir / "report.json").write_text(json.dumps(report, indent=1))

    if "written" in report:
        print(f"written:  {report['written']['url']}")
        print(f"          product={report['written']['product']!r} "
              f"radars={report['written']['radars']} "
              f"overlays={report['written']['overlays']}")
    if "reopened" in report:
        print(f"reopened: product={report['reopened']['product']!r} "
              f"radars={report['reopened']['radars']} "
              f"overlays={report['reopened']['overlays']}")
    if "hostile" in report:
        print(f"hostile:  product={report['hostile']['product']!r} "
              f"radars={report['hostile']['radars']}")

    if report["problems"]:
        print("\nFAIL:")
        for p in report["problems"]:
            print(f"  {p}")
        return 1
    print(f"\nok: the link describes the view, reopens it in a fresh "
          f"browser, and drops what this deployment does not publish; "
          f"images in {outdir}")
    return 0


def main() -> int:
    """Parse arguments and run.

    Returns:
        Process exit status.
    """
    ap = argparse.ArgumentParser(description="check the permalink")
    ap.add_argument("outdir", type=Path)
    ap.add_argument("--base", default=DEFAULT_BASE)
    args = ap.parse_args()
    args.outdir.mkdir(parents=True, exist_ok=True)
    _ = urlparse(args.base)
    return asyncio.run(shoot(args.outdir, args.base))


if __name__ == "__main__":
    sys.exit(main())
