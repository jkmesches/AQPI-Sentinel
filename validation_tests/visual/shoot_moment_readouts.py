#!/usr/bin/env python3
"""Every radar and moment draws a legend and answers the cursor.

Why this exists
---------------
Joseph reported that Differential Reflectivity, Differential Phase and
the Correlation Coefficient had no legend and no probe. Measuring the
ramps fixed the cause, and a ramp resolving is not the thing he asked
for: a legend has to be drawn and a number has to appear under the
cursor. Those are two further steps, through `activeRamp` and through
the held frame, and either can be broken while the registry is right.

So this walks the whole grid, each selectable radar against each
moment, and holds every pair to both. The pairs the coverage record
rejects are checked too, in the other direction: they must draw nothing
rather than a scale the frames were never measured against.

Each pair's frames are served flat, painted in a known stop of that
pair's own ramp, so the expected reading is exact and no run depends on
there being weather over a particular radar.

Usage
-----
::

    python validation_tests/visual/shoot_moment_readouts.py OUTDIR \
        [--base URL] [--radar XSCW]
"""
from __future__ import annotations

import os
import argparse
import asyncio
import json
import sys
from dataclasses import dataclass
from collections.abc import Awaitable, Callable
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

DEFAULT_BASE = os.environ.get("SENTINEL_VISUAL_BASE",
                              "http://localhost:5173")

VIEWPORT = {"width": 1500, "height": 950}
LOAD_SETTLE_MS = 15_000
SOLO_SETTLE_MS = 6000
MOMENT_SETTLE_MS = 6500

RAMP_DIR = VISUAL.parent.parent / "frontend/src/lib/map/ramps/data"
COVERAGE = RAMP_DIR / "coverage.json"

#: The moment tabs as the dock labels them, with the name `data-view`
#: reports for each. The tab carries the symbol and the view carries
#: the console's own word, and neither is the published prefix.
MOMENTS = (
    ("Z", "Reflectivity"),
    ("V", "Velocity"),
    ("Zdr", "Differential Reflectivity"),
    ("ΦDP", "PhiDP"),
    ("ρhv", "RhoHV"),
)

#: Which stop of each ramp to paint with, as a fraction along it.
#: Deliberately off both ends, where a reading can come back from a
#: clamp rather than from a match.
PAINT_AT = 0.4

#: Where to hover, as fractions across and down the pane. The radar is
#: soloed first, which frames the camera on its own box, so these sit
#: inside the served frame by construction.
HOVER = ((0.5, 0.5), (0.45, 0.55), (0.56, 0.46))


@dataclass(frozen=True)
class Grid:
    """What says which radar and moment pairs should read."""

    rows: list[dict[str, Any]]
    prefixes: dict[str, dict[str, str]]


async def read_grid(page: Page) -> Grid:
    """The coverage pairs, and how this deployment names each moment.

    The prefix table comes from the deployment rather than a copy here,
    because it is what the console itself resolves a ramp through and a
    second copy would drift.

    Args:
        page: The loaded console, for its API origin.

    Returns:
        The pairs and the prefix table.
    """
    rows: list[dict[str, Any]] = json.loads(COVERAGE.read_text())["pairs"]
    got: dict[str, dict[str, str]] = await page.evaluate(
        "async()=>{const r=await fetch('/api/version');"
        " return (await r.json()).moment_prefixes || {}; }")
    return Grid(rows=rows, prefixes=got)


def ramp_of(name: str) -> dict[str, Any]:
    """One ramp as shipped.

    Args:
        name: Its file's stem, as the coverage record names it.

    Returns:
        The ramp.

    Raises:
        RuntimeError: When the file is not checked in.
    """
    path = RAMP_DIR / f"{name}.json"
    if not path.exists():
        raise RuntimeError(f"coverage names {name}, which is not here")
    got: dict[str, Any] = json.loads(path.read_text())
    return got


def paint_stop(ramp: dict[str, Any]) -> dict[str, Any]:
    """The stop to paint a pair's frames in, and how it reads back.

    Args:
        ramp: The ramp as shipped.

    Returns:
        Its index and color.
    """
    stops = ramp["stops"]
    at = max(1, min(len(stops) - 2, int(len(stops) * PAINT_AT)))
    hexed = str(stops[at]["color"]).lstrip("#")
    return {"index": at,
            "rgb": tuple(int(hexed[k:k + 2], 16) for k in (0, 2, 4))}


def flat_png(rgb: tuple[int, int, int]) -> bytes:
    """A whole scan painted in one color.

    Args:
        rgb: The color to paint.

    Returns:
        The PNG bytes.
    """
    buf = BytesIO()
    Image.new("RGBA", (697, 693), (*rgb, 255)).save(buf, format="PNG")
    return buf.getvalue()


def serving(body: bytes) -> Callable[[Route], Awaitable[None]]:
    """A route handler that answers with these bytes.

    A closure rather than a default argument. Playwright hands a
    two-parameter handler the Request as its second positional
    argument, so a `payload: bytes = body` default is overwritten with
    a Request and the fulfil fails on serializing it.

    Args:
        body: The PNG to serve.

    Returns:
        A handler taking the route alone.
    """
    async def serve(route: Route) -> None:
        await route.fulfill(status=200, body=body,
                            headers={"content-type": "image/png",
                                     "cache-control": "no-store"})
    return serve


async def open_console(browser: Browser, base: str) -> Page:
    """Load the console with no product, so only the radar is readable.

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
    await page.add_init_script(
        "localStorage.clear();"
        "localStorage.setItem('sentinel-theme','dark');"
        "localStorage.setItem('sentinel-map-prefs', JSON.stringify("
        "  {activeRadars:[], moment:'Reflectivity',"
        "   composite:'none', nexrad:false}));")
    await page.goto(f"{base}/?diag=probe", wait_until="load", timeout=60_000)
    await page.wait_for_timeout(LOAD_SETTLE_MS)
    return page


async def solo(page: Page, radar: str) -> None:
    """Show one radar alone, and frame the camera on it.

    Args:
        page: The loaded console.
        radar: Its id.
    """
    row = page.locator("[data-dock-slot] li", has_text=radar).first
    # Scrolled first, then hovered. The solo button is revealed on
    # hover, and a click that scrolls the row into view moves it out
    # from under the pointer, which hides the button again and leaves
    # Playwright waiting on an element the hover would have shown.
    await row.scroll_into_view_if_needed()
    await page.wait_for_timeout(200)
    await row.hover()
    await page.wait_for_timeout(200)
    # Forced, because visibility here is driven by the hover this just
    # did and the actionability check races it.
    await row.locator("button", has_text="solo").first.click(force=True)
    await page.wait_for_timeout(SOLO_SETTLE_MS)


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


async def look(page: Page, tab: str, showing: str) -> dict[str, Any]:
    """Switch to one moment and report what the pane shows.

    Args:
        page: The loaded console.
        tab: The moment tab's label.
        showing: Which tab the pane is on already.

    Returns:
        The moment, the legend's title and the ramp, or a note that the
        tab is disabled.
    """
    button = page.locator("[data-dock-slot] button", has_text=tab).first
    if await button.is_disabled():
        return {"tab": "disabled"}
    # Away and back when the pane is already on this moment. Clicking
    # the tab it is on changes nothing and so refetches nothing, which
    # left the real frame on screen instead of the served one. XSCV is
    # quiet, its real reflectivity frame is nearly empty, and the
    # cursor then found nothing to read while the legend promised a
    # scale, which looks exactly like the fault this checks for.
    if showing == tab:
        other = next(t for t, _ in MOMENTS if t != tab)
        await page.locator(
            "[data-dock-slot] button", has_text=other).first.click()
        await page.wait_for_timeout(1200)
    await button.click()
    await page.wait_for_timeout(MOMENT_SETTLE_MS)
    view = page.locator("[data-view]").first
    raw = await view.get_attribute("data-view")
    return {
        "moment": json.loads(raw or "{}").get("moment"),
        "ramp": await view.get_attribute("data-readout-ramp"),
        "frame": await view.get_attribute("data-readout-frame"),
        "legend": await page.evaluate(
            "() => { const e = document.querySelector('[data-legend-title]');"
            " return e ? e.textContent.trim() : null; }"),
    }


def judge(radar: str, tab: str, seen: dict[str, Any],
          reads: list[str | None], *, covered: bool) -> list[str]:
    """What one radar and moment got wrong, if anything.

    Args:
        radar: The radar's id.
        tab: The moment tab's label.
        seen: What the pane showed.
        reads: What the readout said at each hovered point.
        covered: Whether the record covers this pair.

    Returns:
        One message per defect.
    """
    where = f"{radar} {tab}"
    if seen.get("tab") == "disabled":
        return [] if not covered else [
            f"{where} is covered but its tab is disabled"]
    answered = [r for r in reads if r]
    if not covered:
        out: list[str] = []
        if seen.get("legend"):
            out.append(f"{where} is not covered yet draws the legend "
                       f"{seen['legend']!r}, which is a scale its frames "
                       f"were never measured against")
        if answered:
            out.append(f"{where} is not covered yet reported {answered[:2]}")
        return out
    out = []
    if not seen.get("legend"):
        out.append(f"{where} draws no legend, so its colors say nothing")
    if not seen.get("ramp"):
        out.append(f"{where} resolves no ramp")
    if not answered:
        out.append(f"{where} reports nothing anywhere over its own frame, "
                   f"while the legend offers a scale to read it by")
    elif any(r == "not on this scale" for r in answered):
        out.append(f"{where} calls its own painted color off its scale")
    return out


async def check_radar(page: Page, radar: str, grid: Grid,
                      rep: dict[str, Any]) -> list[str]:
    """Walk one radar through every moment.

    Args:
        page: The loaded console.
        radar: Its id.
        grid: The coverage pairs and the deployment's prefix table.
        rep: Collects what was seen.

    Returns:
        One message per defect.
    """
    rows, prefixes = grid.rows, grid.prefixes
    # The solo leaves the pane on whatever moment it had, which is the
    # first of these.
    showing = MOMENTS[0][0]
    problems: list[str] = []
    await solo(page, radar)
    for tab, moment in MOMENTS:
        # Through the deployment's own prefix table, the way the console
        # resolves it. The published name differs by radar and by
        # moment: the X-band writes FilteredPhiDP where the C-band
        # writes PhiDP, and both call reflectivity CorrReflectivity.
        prefix = prefixes.get(radar, {}).get(moment)
        pair = next((p for p in rows if p["radar"] == radar
                     and p["moment"] == prefix), None) if prefix else None
        covered = bool(pair and pair.get("covered"))
        if covered and pair:
            stop = paint_stop(ramp_of(str(pair["ramp"])))
            await page.unroute("**/xband_scan.png*")
            await page.route("**/xband_scan.png*",
                             serving(flat_png(stop["rgb"])))
        seen = await look(page, tab, showing)
        showing = tab
        reads = ([await read_at(page, fx, fy) for fx, fy in HOVER]
                 if seen.get("tab") != "disabled" else [])
        rep.setdefault(radar, {})[tab] = {**seen, "covered": covered,
                                          "prefix": prefix, "read": reads}
        problems += judge(radar, tab, seen, reads, covered=covered)
    return problems


async def run(base: str, outdir: Path, only: str) -> dict[str, Any]:
    """Walk every radar the dock offers through every moment.

    Args:
        base: The deployment's origin.
        outdir: Where to write the screenshots.
        only: One radar to check, or empty for all of them.

    Returns:
        What the run showed, with a "problems" list.
    """
    rep: dict[str, Any] = {"base": base, "seen": {}}
    problems: list[str] = []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        try:
            page = await open_console(browser, base)
            grid = await read_grid(page)
            ids: list[str] = await page.evaluate(
                "()=>[...document.querySelectorAll('[data-dock-slot] li')]"
                ".map(li=>li.querySelector('.num')?.textContent.trim())"
                ".filter(Boolean)")
            rep["radars"] = ids
            for radar in ids:
                if only and radar != only:
                    continue
                problems += await check_radar(page, radar, grid, rep["seen"])
                await page.screenshot(path=str(outdir / f"{radar}.png"))
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
    """Check every radar and moment's legend and cursor readout.

    Returns:
        0 when every pair held, 1 otherwise.
    """
    ap = argparse.ArgumentParser(description=__doc__)
    _ = ap.add_argument("outdir", type=Path)
    _ = ap.add_argument("--base", default=DEFAULT_BASE)
    _ = ap.add_argument("--radar", default="",
                        help="check one radar rather than all of them")
    args = ap.parse_args()
    outdir: Path = args.outdir
    outdir.mkdir(parents=True, exist_ok=True)

    rep = asyncio.run(run(args.base, outdir, args.radar))
    _ = (outdir / "report.json").write_text(json.dumps(rep, indent=2))

    for radar, moments in rep.get("seen", {}).items():
        for tab, got in moments.items():
            if got.get("tab") == "disabled":
                print(f"  --   {radar:6s} {tab:4s} tab disabled")
                continue
            first = next((r for r in got["read"] if r), None)
            mark = "ok  " if (got["covered"] == bool(got["legend"])) else "FAIL"
            print(f"  {mark} {radar:6s} {tab:4s} "
                  f"legend={got['legend']!s:16s} reads={first}")

    problems: list[str] = rep.get("problems", [])
    if problems:
        print("\nFAIL:")
        for line in problems:
            print(f"  {line}")
        return 1
    n = sum(len(m) for m in rep.get("seen", {}).values())
    print(f"\nok: all {n} radar and moment pairs either read their own "
          f"painted value under a legend, or draw neither; images in {outdir}")
    return 0


raise SystemExit(main())
