#!/usr/bin/env python3
"""Values read off radar scans, and the cursor during a drag.

Why this exists
---------------
Two faults Joseph found on 2026-11-21, both of which every existing
harness passed over.

The readout went quiet with radars on and no composite, while the
legend went on drawing their scale. The legend asks whether a ramp
resolves and the readout asks whether a frame is held, and those two
questions had drifted apart: a frame was held only when exactly one
radar was on, so two radars drew a scale that nothing could be read
against. A legend promising a number that never comes is worse than no
legend, because the reader blames their pointing.

The crosshair froze or vanished during a drag. It followed MapLibre's
own mousemove, which stops while MapLibre is running a gesture, so the
mark stayed where the drag began as the real cursor walked away from
it, and a mouseout mid-drag took it off screen.

Both are checked here against frames this harness serves, painted in a
known stop of the shipped ramp, so the expected reading is exact and
the run does not depend on there being rain over the Bay.

Usage
-----
::

    python validation_tests/visual/shoot_radar_readout.py OUTDIR [--base URL]
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
    from playwright.async_api import (
        Browser, Locator, Page, Route, async_playwright)
    from playwright.async_api import TimeoutError as PlaywrightTimeout
except ImportError:  # pragma: no cover - environment check
    print("playwright is not installed: pip install playwright "
          "&& playwright install chromium", file=sys.stderr)
    raise SystemExit(2) from None

DEFAULT_BASE = "https://the test deployment"

LOAD_SETTLE_MS = 12_000
RADAR_SETTLE_MS = 6000
ACTION_SETTLE_MS = 900

VIEWPORT = {"width": 1500, "height": 950}

#: The X-band reflectivity table, and which stop to paint the scans in.
RAMP_FILE = (VISUAL.parent.parent / "frontend/src/lib/map/ramps/data"
             / "aqpi-xband-reflectivity.json")
PAINT_STOP = 9

#: A real X-band scan's size.
SCAN_SIZE = (697, 693)

#: How far the drawn crosshair may sit from the pointer, in pixels.
#: It is drawn at the pointer's own position, so this covers rounding
#: and the border of the mark. Lag would show up as a much larger gap.
CURSOR_SLACK_PX = 3

#: How long the crosshair gets to reach a new pointer position, in ms.
#: The measured figure is 0 ms for every move but the first of a drag,
#: which MapLibre spends starting the gesture and which settled between
#: 200 and 500 ms in testing. The fault this replaces was a mark that
#: never arrived at all, so the check reports the time each move took
#: and this only bounds the wait.
CURSOR_CATCHUP_MS = 1200
CURSOR_POLL_MS = 40


def painted_stop() -> dict[str, Any]:
    """The stop the scans are painted in, and what it reads back as.

    Returns:
        Its color and the text the console prints for it.

    Raises:
        RuntimeError: When the ramp no longer has that stop.
    """
    ramp = json.loads(RAMP_FILE.read_text())
    if PAINT_STOP >= len(ramp["stops"]):
        raise RuntimeError(f"{RAMP_FILE.name} has {len(ramp['stops'])} stops")
    stop = ramp["stops"][PAINT_STOP]
    hexed = str(stop["color"]).lstrip("#")
    return {
        "rgb": tuple(int(hexed[k:k + 2], 16) for k in (0, 2, 4)),
        "reads": f"{stop['lo']:.1f} to {stop['hi']:.1f} {ramp['unit']}"
    }


def flat_png(rgb: tuple[int, int, int]) -> bytes:
    """A whole scan painted in one color.

    Args:
        rgb: The color to paint.

    Returns:
        The PNG bytes.
    """
    buf = BytesIO()
    Image.new("RGBA", SCAN_SIZE, (*rgb, 255)).save(buf, format="PNG")
    return buf.getvalue()


async def open_console(browser: Browser, base: str,
                       rgb: tuple[int, int, int]) -> Page:
    """Load the console with every radar scan painted one color.

    Args:
        browser: The running chromium.
        base: The deployment's origin.
        rgb: The color every scan is served in.

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
    # No composite, which is the state the readout went quiet in.
    await page.add_init_script(
        "localStorage.clear();"
        "localStorage.setItem('sentinel-theme','dark');"
        "localStorage.setItem('sentinel-map-prefs', JSON.stringify("
        "  {activeRadars:['XSCV'], moment:'Reflectivity',"
        "   composite:'none', nexrad:false}));")
    body = flat_png(rgb)

    async def serve(route: Route) -> None:
        await route.fulfill(status=200, body=body,
                            headers={"content-type": "image/png",
                                     "cache-control": "no-store"})

    await page.route("**/xband_scan.png*", serve)
    await page.goto(f"{base}/?diag=probe", wait_until="load", timeout=60_000)
    await page.wait_for_timeout(LOAD_SETTLE_MS)
    return page


async def read_at(page: Page, fx: float, fy: float) -> str | None:
    """Hover one point of the first pane and read the chip.

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
    await page.wait_for_timeout(260)
    got: str | None = await page.evaluate(
        "() => { const e = document.querySelector('[data-readout]');"
        " return e ? e.textContent.trim() : null; }")
    return got


def row(page: Page, radar: str) -> Locator:
    """One radar's row in the dock.

    Args:
        page: The loaded console.
        radar: Its id.

    Returns:
        The row's locator.
    """
    return page.locator(
        '[data-dock-slot] li', has_text=radar).first


async def solo(page: Page, radar: str) -> None:
    """Show one radar alone, and frame the camera on it.

    The readings below are taken at the middle of the pane, so the
    middle of the pane has to be inside that radar's own box. Soloing
    fits the camera to the selection, which puts it there by
    construction. Hovering a fixed fraction of whatever view the
    console happened to open on was testing where six range circles
    land, not whether a frame can be read.

    Args:
        page: The loaded console.
        radar: Its id.
    """
    target = row(page, radar)
    # The solo button is revealed on hover, so the row has to be under
    # the pointer before the button is clickable.
    await target.hover()
    await target.locator('button', has_text='solo').first.click()
    await page.wait_for_timeout(RADAR_SETTLE_MS)


async def turn_on(page: Page, radar: str) -> None:
    """Switch one more radar on from the dock, leaving the camera alone.

    Args:
        page: The loaded console.
        radar: Its id.
    """
    await row(page, radar).click()
    await page.wait_for_timeout(RADAR_SETTLE_MS)


def judge_reading(seen: list[str | None], want: str,
                  how: str) -> list[str]:
    """Every point over a painted scan has to report the painted band.

    Args:
        seen: What the readout said at each point.
        want: The band the scans are painted in.
        how: What was on screen, for the message.

    Returns:
        One message per defect.
    """
    answered = [s for s in seen if s]
    if not answered:
        return [f"with {how} the readout said nothing anywhere over the "
                f"scans, while the legend drew their scale; a legend that "
                f"promises a number the readout will not give sends the "
                f"reader looking for a pointing mistake"]
    wrong = [s for s in answered if s != want]
    if wrong:
        return [f"with {how} the readout said {sorted(set(wrong))}, "
                f"and every scan is painted {want!r}"]
    return []


async def check_one_radar(page: Page, rep: dict[str, Any],
                          want: str) -> list[str]:
    """One radar on, no composite, reads its own scan.

    Args:
        page: The loaded console.
        rep: Collects what was seen.
        want: The band the scans are painted in.

    Returns:
        One message per defect.
    """
    await solo(page, "XSCV")
    legend = await page.evaluate(
        "() => { const e = document.querySelector('[data-legend-title]');"
        " return e ? e.textContent.trim() : null; }")
    rep["one_radar_legend"] = legend
    seen = [await read_at(page, fx, fy)
            for fx, fy in ((0.5, 0.5), (0.46, 0.54), (0.54, 0.47))]
    rep["one_radar"] = seen
    # Which frame the console is holding, so a silent readout says
    # whether the pixels never arrived or arrived and read as nothing.
    held = await page.locator("[data-view]").first.get_attribute(
        "data-readout-frame")
    rep["one_radar_held"] = held
    problems: list[str] = []
    if not legend:
        problems.append("one radar and no composite draws no legend")
    if not held:
        problems.append("one radar and no composite holds no frame, so the "
                        "readout has nothing to read")
    problems += judge_reading(seen, want, "one radar and no composite")
    return problems


async def check_two_radars(page: Page, rep: dict[str, Any],
                           want: str) -> list[str]:
    """A second radar on must not silence the readout.

    Args:
        page: The loaded console.
        rep: Collects what was seen.
        want: The band the scans are painted in.

    Returns:
        One message per defect.
    """
    await turn_on(page, "XSCW")
    state = await page.evaluate("""
      () => {
        const v = document.querySelector('[data-view]');
        const t = document.querySelector('[data-legend-title]');
        return { radars: JSON.parse(v.getAttribute('data-view')).radars,
                 legend: t ? t.textContent.trim() : null };
      }
    """)
    rep["two_radars_state"] = state
    problems: list[str] = []
    if len(state["radars"]) != 2:
        problems.append(f"{state['radars']} radars on, expected 2")
    if not state["legend"]:
        problems.append("two radars sharing one table draw no legend")
    seen = [await read_at(page, fx, fy)
            for fx, fy in ((0.5, 0.5), (0.44, 0.52), (0.56, 0.46),
                           (0.5, 0.42))]
    rep["two_radars"] = seen
    problems += judge_reading(seen, want, "two radars and no composite")
    return problems


async def settle_at(page: Page, where: str, px: float,
                    py: float) -> tuple[int, dict[str, Any] | None]:
    """Wait for the crosshair to reach a pointer position, and time it.

    A fixed wait cannot tell a mark that is slow from one that never
    comes, and the fault being checked for is the second. At 200 ms the
    first move of a drag read as a mark frozen where the drag began,
    which is the exact wording of the fault, and at 500 ms it was
    already exact.

    Args:
        page: The loaded console.
        where: The expression measuring the mark against a point.
        px: Where the pointer is, across the viewport.
        py: Where the pointer is, down the viewport.

    Returns:
        How long it took in ms, and the last offset read. The time is
        the budget when the mark never arrived.
    """
    waited = 0
    off: dict[str, Any] | None = await page.evaluate(where, {"x": px, "y": py})
    # Bounded by the budget, so a mark that never arrives still returns.
    while waited < CURSOR_CATCHUP_MS:
        if off and abs(off["dx"]) <= CURSOR_SLACK_PX \
                and abs(off["dy"]) <= CURSOR_SLACK_PX:
            return waited, off
        await page.wait_for_timeout(CURSOR_POLL_MS)
        waited += CURSOR_POLL_MS
        off = await page.evaluate(where, {"x": px, "y": py})
    return waited, off


async def check_cursor_through_a_drag(page: Page, rep: dict[str, Any],
                                      want: str) -> list[str]:
    """The crosshair and its value both survive a drag.

    Args:
        page: The loaded console.
        rep: Collects what was seen.
        want: The band the scans are painted in.

    Returns:
        One message per defect.
    """
    problems: list[str] = []
    box = await page.locator("[data-view]").first.bounding_box()
    if not box:
        return ["no pane to drag"]
    # Measure the drawn svg against the pointer in viewport pixels, both
    # in one call. The div holding the svg is placed at the pointer and
    # the svg carries the -50% transform that centers it there, so the
    # div's own box starts at the pointer and reads 14 px off. Taking the
    # pane's rectangle separately was worse: the staleness banner above
    # the map came and went during the run, and the 26 px it occupies
    # read as the crosshair sitting 25 px high at every step.
    where = """
      (want) => {
        const el = document.querySelector('[data-cursor-self] svg');
        if (!el) return null;
        const r = el.getBoundingClientRect();
        return { dx: Math.round(r.left + r.width / 2 - want.x),
                 dy: Math.round(r.top + r.height / 2 - want.y) };
      }
    """
    start = (box["x"] + box["width"] * 0.5, box["y"] + box["height"] * 0.5)
    await page.mouse.move(*start)
    await page.wait_for_timeout(ACTION_SETTLE_MS)
    await page.mouse.down()
    seen: list[dict[str, Any]] = []
    for step in range(1, 5):
        px = start[0] - 40 * step
        py = start[1] - 26 * step
        # In steps, as a hand drags. A single jump of the whole leg loses
        # its one move: Chromium delivered no pointermove at all for the
        # first move after a button went down, so the mark sat at the
        # drag's start for as long as the harness waited.
        await page.mouse.move(px, py, steps=4)
        took, off = await settle_at(page, where, px, py)
        seen.append({
            "pointer": [round(px), round(py)], "ms": took, "off": off,
            # The value has to survive the gesture. `move` used to blank
            # it, so panning across the map took the number away for the
            # whole drag and gave it back on release.
            "reads": await page.evaluate(
                "() => { const e = document.querySelector('[data-readout]');"
                " return e ? e.textContent.trim() : null; }")
        })
    await page.mouse.up()
    await page.wait_for_timeout(ACTION_SETTLE_MS)
    rep["through_drag"] = seen

    missing = [s for s in seen if s["off"] is None]
    if missing:
        problems.append(
            f"the crosshair went away during the drag at "
            f"{[s['pointer'] for s in missing]}, and a mouseout mid-gesture "
            f"used to take it off screen")
        return problems
    quiet = [s["pointer"] for s in seen if s["reads"] != want]
    if quiet:
        problems.append(
            f"the readout stopped saying {want!r} during the drag, at "
            f"{quiet}, over scans painted that band end to end. A pan "
            f"moves the world under a still cursor, and blanking the "
            f"value on every move took the number away for the whole "
            f"gesture")
    adrift = [s for s in seen
              if abs(s["off"]["dx"]) > CURSOR_SLACK_PX
              or abs(s["off"]["dy"]) > CURSOR_SLACK_PX]
    if adrift:
        problems.append(
            f"the crosshair never reached the pointer during the drag, "
            f"inside {CURSOR_CATCHUP_MS} ms: {adrift[:2]}. MapLibre stops "
            f"emitting mousemove while it runs a gesture, so a mark that "
            f"follows that event freezes where the drag began")
    # And it has to come back to following an ordinary move afterwards.
    at = (box["x"] + box["width"] * 0.62, box["y"] + box["height"] * 0.38)
    await page.mouse.move(*at)
    took, after = await settle_at(page, where, at[0], at[1])
    rep["after_drag"] = {"ms": took, "off": after}
    if not after or abs(after["dx"]) > CURSOR_SLACK_PX \
            or abs(after["dy"]) > CURSOR_SLACK_PX:
        problems.append(
            f"after the drag the crosshair sat at {after} rather than "
            f"under the pointer")
    return problems


async def run(base: str, outdir: Path) -> dict[str, Any]:
    """Drive the readout and the cursor.

    Args:
        base: The deployment's origin.
        outdir: Where to write the screenshots.

    Returns:
        What the run showed, with a "problems" list.
    """
    rep: dict[str, Any] = {"base": base}
    problems: list[str] = []
    stop = painted_stop()
    rep["painted"] = {"color": stop["rgb"], "reads": stop["reads"]}
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        try:
            page = await open_console(browser, base, stop["rgb"])
            problems += await check_one_radar(page, rep, stop["reads"])
            await page.screenshot(path=str(outdir / "radar-one.png"))
            problems += await check_two_radars(page, rep, stop["reads"])
            await page.screenshot(path=str(outdir / "radar-two.png"))
            problems += await check_cursor_through_a_drag(
                page, rep, stop["reads"])
            await page.screenshot(path=str(outdir / "radar-drag.png"))
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
    """Check the radar readout and the dragged cursor.

    Returns:
        0 when both held, 1 otherwise.
    """
    ap = argparse.ArgumentParser(description=__doc__)
    _ = ap.add_argument("outdir", type=Path)
    _ = ap.add_argument("--base", default=DEFAULT_BASE)
    args = ap.parse_args()
    outdir: Path = args.outdir
    outdir.mkdir(parents=True, exist_ok=True)

    rep = asyncio.run(run(args.base, outdir))
    _ = (outdir / "report.json").write_text(json.dumps(rep, indent=2))

    print(f"painted:    {rep.get('painted')}")
    print(f"one radar:  {rep.get('one_radar')}")
    print(f"two radars: {rep.get('two_radars')}")
    print(f"drag:       {rep.get('through_drag')}")
    problems: list[str] = rep["problems"]
    if problems:
        print("\nFAIL:")
        for p in problems:
            print(f"  {p}")
        return 1
    print(f"\nok: a radar scan reads its painted band with one radar on and "
          f"with two, and the crosshair stays under the pointer through a "
          f"drag; images in {outdir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
