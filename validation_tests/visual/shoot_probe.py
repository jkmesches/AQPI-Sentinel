#!/usr/bin/env python3
"""The value probe pins a point, and every pane reads its own at it.

The cursor readout answers for wherever the pointer is, so a reader
comparing two places holds the first number in their head while moving
to the second, and a reader scrubbing time cannot hold a place at all.
A pin fixes the place so the value under it is what moves.

Two claims are checked here, and both of them failed silently before
there was a harness for them.

A pin is ground. Each pane projects that ground into its own screen
and reads its own product there, so a pin dropped in one pane has to
appear in all of them, and in a stacked layout the same pixel is a
different place in each. A pin drawn at the pointer's screen point
would pass a count and still point at the wrong ground.

The reading is taken again on each frame. A pin whose value is frozen
at the moment it was dropped looks exactly like one that updates, and
scrubbing time at a fixed point is what the pin is for.

The frames are this harness's own. A sweep of 63 points across the
pane found three that read anything on 2026-11-21 and none an hour
later, so pinning a guessed point measured the weather: the same code
passed and failed within the hour. Instead the composite route is
served a frame painted entirely in one color taken from the shipped
ramp, which makes the expected reading exact and the run the same in
any weather. Changing the color and stepping time is then what proves
the pin re-reads rather than keeping the number it was dropped on.

The shared cursor is checked in the same run, because it rests on the
same projection: hovering one pane marks the ground on the others.

Usage
-----
::

    python validation_tests/visual/shoot_probe.py OUTDIR [--base URL]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from io import BytesIO
from pathlib import Path
from typing import Any

try:
    from PIL import Image
except ImportError:  # pragma: no cover - environment check
    print("pillow is not installed: pip install pillow", file=sys.stderr)
    raise SystemExit(2) from None

VISUAL = Path(__file__).resolve().parent
sys.path.insert(0, str(VISUAL))

try:
    from playwright.async_api import Browser, Page, Route, async_playwright
    from playwright.async_api import TimeoutError as PlaywrightTimeout
except ImportError:  # pragma: no cover - environment check
    print("playwright is not installed: pip install playwright "
          "&& playwright install chromium", file=sys.stderr)
    raise SystemExit(2) from None

DEFAULT_BASE = "https://the test deployment"

LOAD_SETTLE_MS = 9000
ACTION_SETTLE_MS = 1200
PANE_SETTLE_MS = 4000
STEP_SETTLE_MS = 3000

VIEWPORT = {"width": 1700, "height": 1000}

# How far apart two panes may put one pin, as a share of a pane's width.
# Not zero: the panes are the same size and locked to one camera, so the
# offsets inside each should agree to the pixel, but a half-pixel of
# rounding in two separate projections is not a fault.
MAX_OFFSET_SHARE = 0.01

# Two exact stops of ramps/data/aqpi-product-reflectivity.json, with the
# band each one reads back as. Taken from the shipped file rather than
# retyped from the colorbar, so a ramp that is re-measured moves this
# check with it instead of leaving it asserting last year's numbers.
RAMP_FILE = (VISUAL.parent.parent / "frontend/src/lib/map/ramps/data"
             / "aqpi-product-reflectivity.json")
#: Which stops to paint with, by index into that file.
PAINT_STOPS = (8, 10)

#: The size of a real comp_ref frame. Any size reads the same, since the
#: overlay is placed by extent and sampled by fraction, but matching the
#: real one keeps the sampler's own size reporting honest.
FRAME_SIZE = (843, 1108)

PINS = """
  () => [...document.querySelectorAll('[data-probe-pin]')].map((el) => {
    const r = el.getBoundingClientRect();
    const pane = el.closest('[data-view]');
    const box = pane ? pane.getBoundingClientRect() : null;
    return {
      id: el.getAttribute('data-probe-pin'),
      value: el.getAttribute('data-probe-value'),
      // Relative to the pane, since the panes sit at different offsets
      // across the page and the same ground is a different page pixel
      // in each.
      x: box ? Math.round(r.left + r.width / 2 - box.left) : null,
      y: box ? Math.round(r.top + r.height / 2 - box.top) : null,
      pane: pane ? JSON.parse(pane.getAttribute('data-view')).product : null
    };
  })
"""

CURSORS = """
  () => [...document.querySelectorAll('[data-cursor-follow]')].map((el) => {
    const pane = el.closest('[data-view]');
    const box = pane ? pane.getBoundingClientRect() : null;
    const r = el.getBoundingClientRect();
    const chip = el.querySelector('[data-cursor-follow-value]');
    return {
      x: box ? Math.round(r.left + r.width / 2 - box.left) : null,
      y: box ? Math.round(r.top + r.height / 2 - box.top) : null,
      value: chip ? chip.textContent.trim() : null
    };
  })
"""


async def open_console(browser: Browser, base: str) -> Page:
    """Load the console with the probe's diagnostics on.

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
        "if (!localStorage.getItem('sentinel-map-prefs')) {"
        "  localStorage.setItem('sentinel-theme', 'dark');"
        "  localStorage.setItem('sentinel-map-prefs', JSON.stringify("
        "    {activeRadars:['XSCV'], moment:'Reflectivity',"
        "     composite:'comp_ref', nexrad:false}));"
        "}")
    await page.goto(f"{base}/?diag=probe", wait_until="load", timeout=60_000)
    await page.wait_for_timeout(LOAD_SETTLE_MS)
    return page


async def pane_box(page: Page, index: int) -> dict[str, float]:
    """Where one pane's map sits on the page.

    Args:
        page: The loaded console.
        index: Which pane, left to right from zero.

    Returns:
        Its bounding box.

    Raises:
        RuntimeError: When that pane is not on screen.
    """
    box = await page.locator("[data-view]").nth(index).bounding_box()
    if not box:
        raise RuntimeError(f"pane {index} has no box")
    return {k: float(box[k]) for k in ("x", "y", "width", "height")}


async def click_in_pane(page: Page, index: int,
                        fx: float, fy: float) -> dict[str, float]:
    """Click one pane at a fraction of its own size.

    Args:
        page: The loaded console.
        index: Which pane.
        fx: Across, from 0 at its left edge to 1 at its right.
        fy: Down, from 0 at its top.

    Returns:
        The page point that was clicked.
    """
    box = await pane_box(page, index)
    at = {"x": box["x"] + box["width"] * fx,
          "y": box["y"] + box["height"] * fy}
    await page.mouse.click(at["x"], at["y"])
    await page.wait_for_timeout(ACTION_SETTLE_MS)
    return at


def judge_shared(pins: list[dict[str, Any]], panes: int,
                 width: float) -> list[str]:
    """One pin has to land on the same ground in every pane.

    Args:
        pins: Every pin marker on the page, with its pane-relative place.
        panes: How many panes are open.
        width: A pane's width in CSS pixels.

    Returns:
        One message per defect.
    """
    problems: list[str] = []
    if len(pins) != panes:
        problems.append(
            f"one pin drew {len(pins)} markers across {panes} panes; a pin "
            f"is a place and every pane shows the same ground")
        return problems
    xs = [p["x"] for p in pins if p["x"] is not None]
    ys = [p["y"] for p in pins if p["y"] is not None]
    if len(xs) != panes or len(ys) != panes:
        problems.append("a pin marker is not inside a pane")
        return problems
    slack = max(2.0, width * MAX_OFFSET_SHARE)
    if max(xs) - min(xs) > slack or max(ys) - min(ys) > slack:
        problems.append(
            f"the panes put one pin at {list(zip(xs, ys, strict=True))}, more than "
            f"{slack:.0f} px apart inside their own panes; they are marking "
            f"different ground, so the numbers beside them compare nothing")
    return problems


def judge_readings(pins: list[dict[str, Any]], want: str) -> list[str]:
    """Every pane has to read the band the served color belongs to.

    Exact, because the frame was painted in one stop of the shipped
    ramp. A looser check passes on a readout that is wired up and
    reporting the wrong number, which is the failure this whole ramp
    chain exists to prevent.

    Args:
        pins: Every pin marker on the page.
        want: The band the painted color reads back as.

    Returns:
        One message per defect.
    """
    if not pins:
        return ["no pin to read"]
    wrong = [p for p in pins if p["value"] != want]
    if not wrong:
        return []
    return [f"the frame is painted entirely {want!r} and "
            f"{len(wrong)} of {len(pins)} pins read "
            f"{sorted({str(p['value']) for p in wrong})}"]


def paint_stops() -> list[dict[str, Any]]:
    """The stops this run paints its frames with, from the shipped ramp.

    Returns:
        One entry per stop in PAINT_STOPS, with its rgb and the band it
        reads back as.

    Raises:
        RuntimeError: When the ramp file no longer has those stops.
    """
    ramp = json.loads(RAMP_FILE.read_text())
    stops = ramp["stops"]
    out: list[dict[str, Any]] = []
    for i in PAINT_STOPS:
        if i >= len(stops):
            raise RuntimeError(
                f"{RAMP_FILE.name} has {len(stops)} stops, so index {i} is "
                f"gone; pick two that still exist")
        stop = stops[i]
        hexed = str(stop["color"]).lstrip("#")
        rgb = tuple(int(hexed[k:k + 2], 16) for k in (0, 2, 4))
        out.append({
            "rgb": rgb,
            # The console prints the band, since a block ramp names a
            # range and not a value. One decimal, as readout.ts rounds.
            "reads": f"{stop['lo']:.1f} to {stop['hi']:.1f} {ramp['unit']}"
        })
    return out


def flat_png(rgb: tuple[int, int, int]) -> bytes:
    """A whole frame painted in one color.

    Args:
        rgb: The color to paint.

    Returns:
        The PNG bytes.
    """
    img = Image.new("RGBA", FRAME_SIZE, (*rgb, 255))
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


async def serve_flat_frames(page: Page, rgb: tuple[int, int, int]) -> None:
    """Answer every composite frame request with one flat color.

    The weather is not this check's subject. A frame painted end to end
    in a known stop makes the expected reading exact and the same in any
    weather, where a guessed point on real imagery passed at one hour
    and failed at the next.

    Args:
        page: The loaded console.
        rgb: The color to paint every frame.
    """
    body = flat_png(rgb)

    async def handler(route: Route) -> None:
        await route.fulfill(status=200, body=body,
                            headers={"content-type": "image/png",
                                     "cache-control": "no-store"})

    # Replaces any route installed by an earlier call, so switching
    # colors is one more call rather than an unroute dance.
    await page.unroute("**/product_image.png*")
    await page.route("**/product_image.png*", handler)


async def force_refetch(page: Page) -> None:
    """Step time and back, so each pane fetches its frame again.

    Args:
        page: The loaded console.
    """
    await page.locator("body").press("ArrowLeft")
    await page.wait_for_timeout(STEP_SETTLE_MS)
    await page.locator("body").press("ArrowRight")
    await page.wait_for_timeout(STEP_SETTLE_MS)


async def check_pin_is_a_place(page: Page, outdir: Path,
                               report: dict[str, Any]) -> list[str]:
    """Pin a point in one pane and check the others mark the same ground.

    Args:
        page: The loaded console.
        outdir: Where to write the screenshots.
        report: Collects what was seen.

    Returns:
        One message per defect.
    """
    problems: list[str] = []
    await page.locator("[data-probe-toggle]").click()
    await page.wait_for_timeout(ACTION_SETTLE_MS)
    if await page.locator("[data-probe-toggle]").get_attribute(
            "aria-pressed") != "true":
        problems.append("the probe control does not report itself on")

    # Away from the middle on purpose. A pin at the center of a pane
    # lands at the center of every pane whether or not the projection is
    # right, so a centered pin cannot tell a shared place from a shared
    # pixel.
    _ = await click_in_pane(page, 0, 0.38, 0.42)
    pins = await page.evaluate(PINS)
    report["pinned"] = pins
    panes = await page.locator("[data-view]").count()
    box = await pane_box(page, 0)
    problems += judge_shared(pins, panes, box["width"])
    problems += judge_readings(pins, report["painted"][0]["reads"])
    await page.screenshot(path=str(outdir / "probe-pinned.png"))
    return problems


async def check_reading_follows_frame(page: Page,
                                      report: dict[str, Any]) -> list[str]:
    """Repaint the frame and check the pin reports the new imagery.

    The pin stays where it is and the imagery under it changes, which
    is the whole reason to pin a point. A pin that keeps the number it
    was dropped on looks identical to one that re-reads, and only a
    change the harness caused tells them apart.

    Args:
        page: The loaded console.
        report: Collects what was seen.

    Returns:
        One message per defect.
    """
    problems: list[str] = []
    second = report["painted"][1]
    await serve_flat_frames(page, second["rgb"])
    await force_refetch(page)
    after = await page.evaluate(PINS)
    report["after_repaint"] = after
    if not after:
        problems.append("the pin went away when the frame changed")
        return problems
    problems += judge_readings(after, second["reads"])
    if problems:
        problems.append(
            f"so the pin is holding the reading it was dropped on "
            f"({report['painted'][0]['reads']!r}) rather than reading the "
            f"frame under it")
    return problems


async def check_cursor_is_shared(page: Page, outdir: Path,
                                 report: dict[str, Any]) -> list[str]:
    """Hover one pane and check the others mark the same ground.

    Args:
        page: The loaded console.
        outdir: Where to write the screenshots.
        report: Collects what was seen.

    Returns:
        One message per defect.
    """
    problems: list[str] = []
    panes = await page.locator("[data-view]").count()
    box = await pane_box(page, 0)
    await page.mouse.move(box["x"] + box["width"] * 0.55,
                          box["y"] + box["height"] * 0.35)
    await page.wait_for_timeout(ACTION_SETTLE_MS)
    marks = await page.evaluate(CURSORS)
    report["cursor"] = marks
    # Every pane but the hovered one: the pointer is already the mark in
    # that one, and a second crosshair under it would be noise.
    if len(marks) != panes - 1:
        problems.append(
            f"hovering one pane drew {len(marks)} crosshairs across {panes} "
            f"panes; the other panes are not following the cursor")
        return problems
    xs = [m["x"] for m in marks if m["x"] is not None]
    ys = [m["y"] for m in marks if m["y"] is not None]
    slack = max(2.0, box["width"] * MAX_OFFSET_SHARE)
    if xs and (max(xs) - min(xs) > slack or max(ys) - min(ys) > slack):
        problems.append(
            f"the following panes put the cursor at "
            f"{list(zip(xs, ys, strict=True))}, "
            f"more than {slack:.0f} px apart inside their own panes")
    await page.screenshot(path=str(outdir / "probe-cursor.png"))
    # Leaving the map has to take the mark with it, or a crosshair left
    # behind is a reading of a place nobody is pointing at.
    await page.mouse.move(box["x"] + box["width"] / 2, box["y"] - 40)
    await page.wait_for_timeout(ACTION_SETTLE_MS)
    left = await page.evaluate(CURSORS)
    if left:
        problems.append(
            f"{len(left)} crosshairs are still drawn with the pointer off "
            f"the maps")
    return problems


async def check_removal(page: Page, outdir: Path,
                        report: dict[str, Any]) -> list[str]:
    """A pin comes off when it is clicked, and clear takes them all.

    Args:
        page: The loaded console.
        outdir: Where to write the screenshots.
        report: Collects what was seen.

    Returns:
        One message per defect.
    """
    problems: list[str] = []
    panes = await page.locator("[data-view]").count()
    _ = await click_in_pane(page, 0, 0.6, 0.6)
    after_two = await page.evaluate(PINS)
    if len(after_two) != 2 * panes:
        problems.append(
            f"a second pin gave {len(after_two)} markers, not "
            f"{2 * panes}")
    first = after_two[0]["id"] if after_two else None
    if first:
        await page.locator(f'[data-probe-pin="{first}"]').first.click()
        await page.wait_for_timeout(ACTION_SETTLE_MS)
        left = await page.evaluate(PINS)
        report["after_remove"] = left
        if any(p["id"] == first for p in left):
            problems.append("clicking a pin did not take it off")
        if len(left) != panes:
            problems.append(
                f"removing one pin left {len(left)} markers, not {panes}; "
                f"it came off some panes and not others")
    clear = page.locator("[data-probe-clear]")
    if await clear.count() == 0:
        problems.append("no control to clear the pins")
    else:
        await clear.click()
        await page.wait_for_timeout(ACTION_SETTLE_MS)
        if await page.evaluate("() => "
                               "document.querySelectorAll('[data-probe-pin]')"
                               ".length"):
            problems.append("pins are still on the map after clear")
    await page.screenshot(path=str(outdir / "probe-cleared.png"))
    return problems


async def run(base: str, outdir: Path) -> dict[str, Any]:
    """Drive the probe and the shared cursor across two panes.

    Args:
        base: The deployment's origin.
        outdir: Where to write the screenshots.

    Returns:
        What the run showed, with a "problems" list.
    """
    report: dict[str, Any] = {"base": base}
    problems: list[str] = []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        try:
            page = await open_console(browser, base)
            painted = paint_stops()
            report["painted"] = painted
            await serve_flat_frames(page, painted[0]["rgb"])
            await force_refetch(page)
            await page.locator("[data-pane-add]").click()
            await page.wait_for_timeout(PANE_SETTLE_MS)
            report["panes"] = await page.locator("[data-view]").count()
            if report["panes"] != 2:
                problems.append(f"{report['panes']} panes, expected 2")

            problems += await check_pin_is_a_place(page, outdir, report)
            problems += await check_reading_follows_frame(page, report)
            problems += await check_cursor_is_shared(page, outdir, report)
            problems += await check_removal(page, outdir, report)
        except (PlaywrightTimeout, RuntimeError) as exc:
            problems.append(f"the console stopped answering: {exc}")
        finally:
            await browser.close()
    report["problems"] = problems
    return report


def main() -> int:
    """Run the probe checks and report.

    Returns:
        0 when every claim held, 1 otherwise.
    """
    ap = argparse.ArgumentParser(description=__doc__)
    _ = ap.add_argument("outdir", type=Path)
    _ = ap.add_argument("--base", default=DEFAULT_BASE)
    args = ap.parse_args()
    outdir: Path = args.outdir
    outdir.mkdir(parents=True, exist_ok=True)

    report = asyncio.run(run(args.base, outdir))
    _ = (outdir / "report.json").write_text(json.dumps(report, indent=2))

    print(f"pinned:  {report.get('pinned')}")
    print(f"painted: {report.get('painted')}")
    print(f"repaint: {report.get('after_repaint')}")
    print(f"cursor:  {report.get('cursor')}")
    problems: list[str] = report["problems"]
    if problems:
        print("\nFAIL:")
        for p in problems:
            print(f"  {p}")
        return 1
    print(f"\nok: a pin marks one ground point in every pane, every pane "
          f"reads the exact band the served frame is painted in, repainting "
          f"moves the reading, the cursor is shared and a pin comes off when "
          f"clicked; images in {outdir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
