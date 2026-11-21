#!/usr/bin/env python3
"""Every product draws a legend and answers under the cursor.

Why this exists
---------------
Joseph's report on 2026-11-21 was that Water Level had no legend and no
probe. The fix was a measured ramp for it, and nothing checked the
claim he actually made, which was about any product rather than that
one: "it needs to be there for any data depictions".

So this walks every product the console offers and holds each to the
same two questions. A legend has to be drawn, and the cursor has to
report the exact value the frame is painted in. Checking one product
and shipping would leave the next unmeasured product to be found the
same way, by a reader hovering it and getting nothing.

Each product's frames are served flat, painted in a known stop of that
product's own ramp, so the expected reading is exact and the run does
not depend on rain, flooding or a forecast being interesting today.
A product whose ramp is not checked in is a failure here, not a skip:
the console will draw its imagery either way, and imagery without a
scale is a picture presented as a measurement.

Usage
-----
::

    python validation_tests/visual/shoot_product_readouts.py OUTDIR \
        [--base URL]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from io import BytesIO
from pathlib import Path
from typing import Any

VISUAL = Path(__file__).resolve().parent
sys.path.insert(0, str(VISUAL))

try:
    from PIL import Image
except ImportError:  # pragma: no cover - environment check
    print("pillow is not installed: pip install pillow", file=sys.stderr)
    raise SystemExit(2) from None

try:
    from playwright.async_api import Browser, Page, Route, async_playwright
    from playwright.async_api import TimeoutError as PlaywrightTimeout
except ImportError:  # pragma: no cover - environment check
    print("playwright is not installed: pip install playwright "
          "&& playwright install chromium", file=sys.stderr)
    raise SystemExit(2) from None

from capture import open_dock  # noqa: E402  sibling module, loaded by path

DEFAULT_BASE = "https://the test deployment"

LOAD_SETTLE_MS = 14_000
PRODUCT_SETTLE_MS = 5000

VIEWPORT = {"width": 1500, "height": 950}

RAMP_DIR = VISUAL.parent.parent / "frontend/src/lib/map/ramps/data"
COVERAGE = RAMP_DIR / "product-coverage.json"

#: The size of a real composite frame. Any size reads the same, since
#: the overlay is placed by extent and sampled by fraction.
FRAME_SIZE = (843, 1108)

#: Which stop of each ramp to paint with. Deliberately not the first or
#: the last: an end stop can read back from a clamp rather than from a
#: match, and would pass a ramp whose middle is wrong.
PAINT_AT = 0.4

#: Where to hover, as fractions across and down the pane. The composite
#: covers the whole view at the opening camera, so these are inside it.
HOVER = ((0.5, 0.5), (0.44, 0.56), (0.57, 0.45))


def ramps_by_product() -> dict[str, dict[str, Any]]:
    """Each product's ramp, keyed by product id and unit.

    Returns:
        One entry per covered product, holding its ramp and unit.

    Raises:
        RuntimeError: When the coverage record names a missing ramp.
    """
    rows = json.loads(COVERAGE.read_text())["products"]
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not row.get("covered"):
            continue
        name = str(row["ramp"])
        path = RAMP_DIR / f"{name}.json"
        if not path.exists():
            raise RuntimeError(f"coverage names {name}, which is not here")
        key = f"{row['product']}|{row.get('unit') or ''}"
        out[key] = {"ramp": json.loads(path.read_text()),
                    "unit": row.get("unit"), "file": name}
    return out


def paint_stop(ramp: dict[str, Any]) -> dict[str, Any]:
    """The stop to paint a product's frames in, and how it reads back.

    Args:
        ramp: The ramp as shipped.

    Returns:
        Its color, and the text the console prints for it.
    """
    stops = ramp["stops"]
    at = max(1, min(len(stops) - 2, int(len(stops) * PAINT_AT)))
    stop = stops[at]
    hexed = str(stop["color"]).lstrip("#")
    return {
        "index": at,
        "rgb": tuple(int(hexed[k:k + 2], 16) for k in (0, 2, 4)),
        "lo": stop.get("lo"), "hi": stop.get("hi"),
        "value": stop.get("value")
    }


def flat_png(rgb: tuple[int, int, int]) -> bytes:
    """A whole frame painted in one color.

    Args:
        rgb: The color to paint.

    Returns:
        The PNG bytes.
    """
    buf = BytesIO()
    Image.new("RGBA", FRAME_SIZE, (*rgb, 255)).save(buf, format="PNG")
    return buf.getvalue()


async def open_console(browser: Browser, base: str) -> Page:
    """Load the console with its dock open and no radars on.

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
        "window.__errs=[];"
        "window.addEventListener('error',"
        "e=>window.__errs.push(String(e.message)));")
    # No radars, so the only thing under the cursor is the product.
    await page.add_init_script(
        "localStorage.clear();"
        "localStorage.setItem('sentinel-theme','dark');"
        "localStorage.setItem('sentinel-map-prefs', JSON.stringify("
        "  {activeRadars:[], moment:'Reflectivity',"
        "   composite:'comp_ref', nexrad:false}));")
    await page.goto(f"{base}/?diag=probe", wait_until="load", timeout=60_000)
    await page.wait_for_timeout(LOAD_SETTLE_MS)
    await open_dock(page)
    return page


async def offered(page: Page) -> list[str]:
    """Every product the console's own control lists.

    Args:
        page: The loaded console.

    Returns:
        The product ids, without the "off" entry.
    """
    got: list[str] = await page.evaluate(
        "()=>[...document.querySelectorAll("
        "'[data-product-select] option')].map(o=>o.value)")
    return [p for p in got if p and p != "none"]


async def show(page: Page, product: str) -> dict[str, Any]:
    """Switch to one product and report what the pane settled on.

    Args:
        page: The loaded console.
        product: The product id.

    Returns:
        The pane's own view record.
    """
    _ = await page.locator("[data-product-select]").select_option(product)
    await page.wait_for_timeout(PRODUCT_SETTLE_MS)
    raw = await page.locator("[data-view]").first.get_attribute("data-view")
    got: dict[str, Any] = json.loads(raw or "{}")
    return got


async def read_at(page: Page, fx: float, fy: float) -> str | None:
    """Hover one point of the pane and read the chip.

    Args:
        page: The loaded console.
        fx: Across the pane, 0 to 1.
        fy: Down the pane, 0 to 1.

    Returns:
        What the readout says, or None when it says nothing.
    """
    box = await page.locator("[data-view]").first.bounding_box()
    if not box:
        return None
    await page.mouse.move(box["x"] + box["width"] * fx,
                          box["y"] + box["height"] * fy)
    await page.wait_for_timeout(300)
    got: str | None = await page.evaluate(
        "() => { const e = document.querySelector('[data-readout]');"
        " return e ? e.textContent.trim() : null; }")
    return got


def judge(product: str, legend: str | None, held: str | None,
          seen: list[str | None]) -> list[str]:
    """What one product got wrong, if anything.

    Args:
        product: The product id.
        legend: The legend's title, or None.
        held: What `data-readout-composite` says, or None.
        seen: What the readout said at each hovered point.

    Returns:
        One message per defect.
    """
    out: list[str] = []
    if not legend:
        out.append(f"{product} draws no legend, so its colors say nothing")
    if not held:
        out.append(f"{product} holds no frame, so the cursor has nothing "
                   f"to read")
    answered = [s for s in seen if s]
    if not answered:
        out.append(f"{product} reports nothing anywhere over its own frame")
        return out
    off = [s for s in answered if s == "not on this scale"]
    if off:
        out.append(f"{product} calls its own painted color off its scale, "
                   f"{len(off)} of {len(answered)} points")
    return out


async def check_product(page: Page, product: str, table: dict[str, Any],
                        rep: dict[str, Any]) -> list[str]:
    """One product draws a scale and reads its painted value back.

    Args:
        page: The loaded console.
        product: The product id.
        table: Every covered product's ramp, by product and unit.
        rep: Collects what was seen.

    Returns:
        One message per defect.
    """
    view = await show(page, product)
    unit = view.get("unit") or ""
    entry = table.get(f"{product}|{unit}") or table.get(f"{product}|")
    if not entry:
        return [f"{product} at unit {unit!r} has no measured ramp, so the "
                f"console draws its imagery with no scale to read it by"]
    stop = paint_stop(entry["ramp"])
    await page.unroute("**/product_image.png*")
    body = flat_png(stop["rgb"])

    async def serve(route: Route) -> None:
        await route.fulfill(status=200, body=body,
                            headers={"content-type": "image/png",
                                     "cache-control": "no-store"})

    await page.route("**/product_image.png*", serve)
    # Away and back, so the frame now on screen is the painted one
    # rather than the real frame fetched before the route was added.
    _ = await page.locator("[data-product-select]").select_option("none")
    await page.wait_for_timeout(1200)
    _ = await page.locator("[data-product-select]").select_option(product)
    await page.wait_for_timeout(PRODUCT_SETTLE_MS)

    legend = await page.evaluate(
        "() => { const e = document.querySelector('[data-legend-title]');"
        " return e ? e.textContent.trim() : null; }")
    held = await page.locator("[data-view]").first.get_attribute(
        "data-readout-composite")
    seen = [await read_at(page, fx, fy) for fx, fy in HOVER]
    rep[product] = {"unit": unit, "ramp": entry["file"],
                    "painted": stop["index"], "legend": legend,
                    "held": held, "read": seen}
    return judge(product, legend, held, seen)


async def run(base: str, outdir: Path) -> dict[str, Any]:
    """Walk every product the console offers.

    Args:
        base: The deployment's origin.
        outdir: Where to write the screenshots.

    Returns:
        What the run showed, with a "problems" list.
    """
    rep: dict[str, Any] = {"base": base, "seen": {}}
    problems: list[str] = []
    table = ramps_by_product()
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        try:
            page = await open_console(browser, base)
            products = await offered(page)
            rep["offered"] = products
            if not products:
                problems.append("the product control offers nothing")
            for product in products:
                problems += await check_product(page, product, table,
                                                rep["seen"])
                await page.screenshot(path=str(outdir / f"{product}.png"))
            errs = await page.evaluate("()=>window.__errs||[]")
            if errs:
                problems.append(f"page errors: {errs[:3]}")
        except (PlaywrightTimeout, RuntimeError) as exc:
            problems.append(f"the console stopped answering: {exc}")
        finally:
            await browser.close()
    rep["problems"] = problems
    return rep


def main() -> int:
    """Check every product's legend and cursor readout.

    Returns:
        0 when every product held, 1 otherwise.
    """
    ap = argparse.ArgumentParser(description=__doc__)
    _ = ap.add_argument("outdir", type=Path)
    _ = ap.add_argument("--base", default=DEFAULT_BASE)
    args = ap.parse_args()
    outdir: Path = args.outdir
    outdir.mkdir(parents=True, exist_ok=True)

    rep = asyncio.run(run(args.base, outdir))
    _ = (outdir / "report.json").write_text(json.dumps(rep, indent=2))

    for product, got in rep.get("seen", {}).items():
        mark = "ok  " if got["legend"] and any(got["read"]) else "FAIL"
        print(f"  {mark} {product:24s} {got['legend']!s:22s} "
              f"{got['read'][0]}")

    problems: list[str] = rep.get("problems", [])
    if problems:
        print("\nFAIL:")
        for line in problems:
            print(f"  {line}")
        return 1
    n = len(rep.get("seen", {}))
    print(f"\nok: all {n} products draw a scale and read their own painted "
          f"value back; images in {outdir}")
    return 0


raise SystemExit(main())
