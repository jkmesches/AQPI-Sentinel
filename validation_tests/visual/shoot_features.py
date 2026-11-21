#!/usr/bin/env python3
"""Every claim the console's chrome makes, checked in one run.

Why this exists
---------------
The other harnesses in this folder each drive one mechanism deeply:
shoot_probe.py the pinned readings, shoot_compare.py the pane lock,
shoot_controls.py the layer sliders. None of them answers the question
a reader actually asks after a release, which is whether the console
still does the things it says it does.

This one does. It is broad rather than deep: one assertion per claim,
each cheap, all in one session so a regression in one cannot hide
behind a green run of another. It is the check to run before calling a
batch of work done, and the list below is what that means.

What it holds the console to
----------------------------
The display controls are a dock beside the panes with every group
expanded, because a console where each setting is a click away is one
where nobody finds the settings. The place search is console chrome.
The compass sits top right, is 56 px at the default size, and faces
north when clicked. The legend is drawn for a measured ramp and its bar
is thick enough to judge a color against. A pin reads the exact band of
the color under it. One size control moves the legend and the compass
together. Three panes open, share one pin and one cursor, and track
each other while a drag is running rather than after it. Reset returns
the console to the state it opens in.

The frames are this harness's own, painted in a known stop of the
shipped ramp, so the expected reading is exact and the run is the same
in any weather. A sweep for real data passed at one hour and failed at
the next, which is a check that measures the sky.

Usage
-----
::

    python validation_tests/visual/shoot_features.py OUTDIR [--base URL]
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

DEFAULT_BASE = "https://the test deployment"

LOAD_SETTLE_MS = 11_000
ACTION_SETTLE_MS = 1200
PANE_SETTLE_MS = 3500
RESET_SETTLE_MS = 4000

VIEWPORT = {"width": 1700, "height": 1000}

#: The ramp the painted frames are taken from, and which stop to use.
RAMP_FILE = (VISUAL.parent.parent / "frontend/src/lib/map/ramps/data"
             / "aqpi-product-reflectivity.json")
PAINT_STOP = 10

#: A real comp_ref frame's size.
FRAME_SIZE = (843, 1108)

#: The dock's groups. Every one is open as soon as the console is.
WANT_GROUPS = ("Product", "Moment", "Radars", "Reference", "Geography",
               "Opacity")

#: Geometry the overlays are drawn at, at the default size and at 2x.
COMPASS_PX = 56
LEGEND_BAR_PX = 20
BIG = 2

#: How far apart two panes' live cameras may sit mid-drag, in degrees.
#: A pane that is tracking stays within a few hundred meters of its
#: partner; one that waits for the release is a whole drag away, which
#: at zoom 7 is degrees.
MID_DRAG_EPS = 0.01

#: Each pane's camera as MapLibre holds it, rather than the settled one
#: `data-view` publishes at moveend.
LIVE_CAMERAS = """
  () => {
    const panes = window.__sentinelPanes;
    if (!panes) return [];
    return Object.keys(panes).sort().map((k) => {
      const s = panes[k]();
      return s && s.camera
        ? [s.camera.center[0], s.camera.center[1], s.camera.zoom]
        : null;
    });
  }
"""

#: Every pin on the page, placed against its own pane rather than the
#: page, since the panes sit at different offsets across it.
PINS = """
  () => [...document.querySelectorAll('[data-probe-pin]')].map((el) => {
    const pane = el.closest('[data-view]').getBoundingClientRect();
    const r = el.getBoundingClientRect();
    return {
      x: Math.round(r.left + r.width / 2 - pane.left),
      y: Math.round(r.top + r.height / 2 - pane.top),
      value: el.getAttribute('data-probe-value')
    };
  })
"""


class Report:
    """What the run found, one line per claim."""

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}

    def check(self, claim: str, ok: object, detail: str = "") -> None:
        """Record one claim and print it.

        Args:
            claim: What was asserted, as a reader would say it.
            ok: Whether it held.
            detail: What was seen, for a failure to be actionable.
        """
        self.rows[claim] = {"ok": bool(ok), "detail": detail}
        mark = "ok  " if ok else "FAIL"
        print(f"  {mark}  {claim}" + (f"  — {detail}" if detail else ""))

    def failures(self) -> list[str]:
        """The claims that did not hold.

        Returns:
            Their names.
        """
        return [k for k, v in self.rows.items() if not v["ok"]]


def painted_stop() -> dict[str, Any]:
    """The ramp stop the frames are painted in, and what it reads as.

    Returns:
        Its rgb and the band the console prints for it.

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
    """A whole frame painted in one color.

    Args:
        rgb: The color to paint.

    Returns:
        The PNG bytes.
    """
    buf = BytesIO()
    Image.new("RGBA", FRAME_SIZE, (*rgb, 255)).save(buf, format="PNG")
    return buf.getvalue()


async def open_console(browser: Browser, base: str,
                       rgb: tuple[int, int, int]) -> Page:
    """Load the console with its composite frames painted one color.

    Args:
        browser: The running chromium.
        base: The deployment's origin.
        rgb: The color every composite frame is served in.

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
    # Cleared rather than seeded around, so the run starts from the
    # console as a first visit sees it plus one known product.
    await page.add_init_script(
        "localStorage.clear();"
        "localStorage.setItem('sentinel-theme','dark');"
        "localStorage.setItem('sentinel-map-prefs', JSON.stringify("
        "  {activeRadars:['XSCV'], moment:'Reflectivity',"
        "   composite:'comp_ref', nexrad:true}));")
    body = flat_png(rgb)

    async def serve(route: Route) -> None:
        await route.fulfill(status=200, body=body,
                            headers={"content-type": "image/png",
                                     "cache-control": "no-store"})

    await page.route("**/product_image.png*", serve)
    await page.goto(f"{base}/?diag=probe", wait_until="load", timeout=60_000)
    await page.wait_for_timeout(LOAD_SETTLE_MS)
    return page


async def check_dock(page: Page, rep: Report) -> None:
    """The display controls are a dock, open, with nothing behind a click.

    Args:
        page: The loaded console.
        rep: Collects the results.
    """
    groups = await page.evaluate(
        "()=>[...document.querySelectorAll('[data-dock-slot] .label')]"
        ".map(e=>e.textContent.trim())")
    rep.check("the dock carries every group, expanded",
              set(WANT_GROUPS) <= set(groups), " ".join(groups))
    rep.check("no dropdown menus are left on the map",
              await page.evaluate(
                  "()=>document.querySelectorAll('[data-menu]').length") == 0)
    reachable = await page.evaluate(
        "()=>document.querySelectorAll('[data-dock-slot] button,"
        "[data-dock-slot] input,[data-dock-slot] select').length")
    rep.check("its controls are reachable with no clicks", reachable >= 20,
              f"{reachable} controls")
    rep.check("the place search sits in the toolbar", await page.evaluate(
        "()=>!!document.querySelector("
        "'[data-map-toolbar] [data-place-search]')"))
    rep.check("and is gone from the dock", await page.evaluate(
        "()=>!document.querySelector('[data-dock-slot] [data-place-search]')"))


async def check_compass(page: Page, rep: Report) -> None:
    """The compass is top right, legible, and a way back to north.

    Args:
        page: The loaded console.
        rep: Collects the results.
    """
    seen = await page.evaluate("""
      () => {
        const c = document.querySelector('[data-compass]');
        if (!c) return null;
        const r = c.getBoundingClientRect();
        const p = c.closest('[data-view]').getBoundingClientRect();
        return { w: Math.round(r.width), right: Math.round(p.right - r.right),
                 top: Math.round(r.top - p.top),
                 north: c.getAttribute('data-compass-north') };
      }
    """)
    rep.check("the compass is in the top right corner",
              seen and seen["right"] < 40 and seen["top"] < 40,
              json.dumps(seen))
    rep.check(f"it is {COMPASS_PX} px at the default size",
              seen and seen["w"] == COMPASS_PX, str(seen and seen["w"]))
    rep.check("the console does not open north up", seen
              and seen["north"] == "no", str(seen and seen["north"]))
    await page.locator("[data-compass]").click()
    await page.wait_for_timeout(ACTION_SETTLE_MS + 600)
    rep.check("clicking it faces north", await page.evaluate(
        "()=>document.querySelector('[data-compass]')"
        ".getAttribute('data-compass-north')") == "yes")


async def check_legend_and_probe(page: Page, rep: Report,
                                 want: str) -> None:
    """The scale is drawn and a pin reads the exact band under it.

    Args:
        page: The loaded console.
        rep: Collects the results.
        want: The band the painted frames read back as.
    """
    legend = await page.evaluate("""
      () => {
        const t = document.querySelector('[data-legend-title]');
        const r = document.querySelector('[data-legend] rect');
        if (!t || !r) return null;
        return { title: t.textContent.trim(),
                 bar: Number(r.getAttribute('height')),
                 labels: document.querySelectorAll(
                   '[data-legend] text').length };
      }
    """)
    rep.check("a legend is drawn for the composite",
              legend and legend["title"], json.dumps(legend))
    rep.check(f"its bar is {LEGEND_BAR_PX} px rather than a 10 px band",
              legend and legend["bar"] == LEGEND_BAR_PX,
              str(legend and legend["bar"]))
    rep.check("it carries more than a couple of numbers",
              legend and legend["labels"] >= 4,
              str(legend and legend["labels"]))

    await page.locator("[data-probe-toggle]").click()
    await page.wait_for_timeout(600)
    box = await page.locator("[data-view]").first.bounding_box()
    if not box:
        rep.check("a pin reads the exact band under it", False, "no pane")
        return
    await page.mouse.click(box["x"] + box["width"] * 0.4,
                           box["y"] + box["height"] * 0.45)
    await page.wait_for_timeout(ACTION_SETTLE_MS)
    pins = await page.evaluate(PINS)
    rep.check("a pin reads the exact band under it",
              pins and all(p["value"] == want for p in pins),
              f"{[p['value'] for p in pins]} want {want!r}")


async def check_size_control(page: Page, rep: Report) -> None:
    """One setting sizes every overlay together.

    Args:
        page: The loaded console.
        rep: Collects the results.
    """
    read = """
      () => ({
        bar: Number(document.querySelector('[data-legend] rect')
                    .getAttribute('height')),
        compass: Math.round(document.querySelector('[data-compass]')
                            .getBoundingClientRect().width)
      })
    """
    await page.locator("[data-settings-toggle]").click()
    await page.wait_for_timeout(600)
    await page.locator(f'[data-ui-scale="{BIG}"]').click()
    await page.wait_for_timeout(ACTION_SETTLE_MS)
    big = await page.evaluate(read)
    rep.check(f"one size control takes the legend and compass to {BIG}x",
              big["bar"] == LEGEND_BAR_PX * BIG
              and big["compass"] == COMPASS_PX * BIG, json.dumps(big))
    await page.locator('[data-ui-scale="1"]').click()
    await page.wait_for_timeout(ACTION_SETTLE_MS)
    back = await page.evaluate(read)
    rep.check("and brings them back",
              back["bar"] == LEGEND_BAR_PX
              and back["compass"] == COMPASS_PX, json.dumps(back))
    await page.keyboard.press("Escape")


async def check_panes(page: Page, rep: Report, outdir: Path) -> int:
    """Three panes open, and share the pin and the cursor.

    Args:
        page: The loaded console.
        rep: Collects the results.
        outdir: Where to write the screenshot.

    Returns:
        How many panes ended up open.
    """
    for _ in range(2):
        await page.locator("[data-pane-add]").click()
        await page.wait_for_timeout(PANE_SETTLE_MS)
    await page.wait_for_timeout(PANE_SETTLE_MS)
    panes = await page.locator("[data-view]").count()
    rep.check("three panes open", panes == 3, str(panes))

    marks = await page.evaluate(PINS)
    places = {(m["x"], m["y"]) for m in marks}
    rep.check("one pin marks the same ground in every pane",
              len(marks) == panes and len(places) == 1, json.dumps(marks))

    box = await page.locator("[data-view]").first.bounding_box()
    if box:
        await page.mouse.move(box["x"] + box["width"] * 0.55,
                              box["y"] + box["height"] * 0.35)
        await page.wait_for_timeout(ACTION_SETTLE_MS)
    cursors = await page.evaluate("""
      () => ({
        following: document.querySelectorAll('[data-cursor-follow]').length,
        own: document.querySelectorAll('[data-cursor-self]').length
      })
    """)
    rep.check("the cursor is one shape in every pane",
              cursors["following"] == panes - 1 and cursors["own"] == 1,
              json.dumps(cursors))
    await page.screenshot(path=str(outdir / "features-3panes.png"))
    return panes


async def check_mid_drag(page: Page, rep: Report, panes: int) -> None:
    """The panes track each other while a drag is running.

    A follower that waits for the release is a before and an after
    rather than a comparison, and the picture looks the same either way
    once the mouse is up.

    Args:
        page: The loaded console.
        rep: Collects the results.
        panes: How many panes are open.
    """
    box = await page.locator("[data-view]").first.bounding_box()
    if not box:
        rep.check("every pane moves during the drag, not after it", False,
                  "no pane")
        return
    before = await page.evaluate(LIVE_CAMERAS)
    await page.mouse.move(box["x"] + 300, box["y"] + 300)
    await page.mouse.down()
    await page.mouse.move(box["x"] + 180, box["y"] + 230, steps=8)
    await page.wait_for_timeout(500)
    mid = await page.evaluate(LIVE_CAMERAS)
    await page.mouse.up()
    await page.wait_for_timeout(ACTION_SETTLE_MS)

    moved = [i for i, (a, b) in enumerate(zip(before, mid, strict=False))
             if a and b and abs(b[0] - a[0]) + abs(b[1] - a[1]) > 1e-6]
    rep.check("every pane moves during the drag, not after it",
              len(moved) == panes, f"{len(moved)} of {panes} moved mid-drag")
    if mid and all(mid):
        spread = max(abs(c[0] - mid[0][0]) + abs(c[1] - mid[0][1])
                     for c in mid)
        rep.check("and they agree with each other while it runs",
                  spread <= MID_DRAG_EPS, f"max spread {spread:.5f} deg")
    else:
        rep.check("and they agree with each other while it runs", False,
                  "a pane published no live camera; this needs ?diag=probe")


async def check_reset(page: Page, rep: Report) -> None:
    """Reset returns the console to the state it opens in.

    Args:
        page: The loaded console.
        rep: Collects the results.
    """
    await page.locator("[data-map-toolbar] button",
                       has_text="reset").first.click()
    await page.wait_for_timeout(RESET_SETTLE_MS)
    rep.check("reset returns to one pane",
              await page.locator("[data-view]").count() == 1)
    rep.check("reset clears the pinned probes", await page.evaluate(
        "()=>document.querySelectorAll('[data-probe-pin]').length") == 0)


async def run(base: str, outdir: Path) -> Report:
    """Drive the console through every claim.

    Args:
        base: The deployment's origin.
        outdir: Where to write the screenshots.

    Returns:
        What held and what did not.
    """
    rep = Report()
    stop = painted_stop()
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        try:
            page = await open_console(browser, base, stop["rgb"])
            await check_dock(page, rep)
            await check_compass(page, rep)
            await check_legend_and_probe(page, rep, stop["reads"])
            await check_size_control(page, rep)
            panes = await check_panes(page, rep, outdir)
            await check_mid_drag(page, rep, panes)
            await check_reset(page, rep)
            errs = await page.evaluate("()=>window.__errs||[]")
            rep.check("no page errors through any of it", len(errs) == 0,
                      json.dumps(errs[:3]))
        except (PlaywrightTimeout, RuntimeError) as exc:
            rep.check("the console answered throughout", False, str(exc))
        finally:
            await browser.close()
    return rep


def main() -> int:
    """Check every claim and report.

    Returns:
        0 when they all held, 1 otherwise.
    """
    ap = argparse.ArgumentParser(description=__doc__)
    _ = ap.add_argument("outdir", type=Path)
    _ = ap.add_argument("--base", default=DEFAULT_BASE)
    args = ap.parse_args()
    outdir: Path = args.outdir
    outdir.mkdir(parents=True, exist_ok=True)

    rep = asyncio.run(run(args.base, outdir))
    _ = (outdir / "report.json").write_text(json.dumps(rep.rows, indent=2))
    bad = rep.failures()
    print(f"\n{len(rep.rows) - len(bad)} of {len(rep.rows)} claims held")
    if bad:
        print("FAILED: " + ", ".join(bad))
        return 1
    print(f"images in {outdir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
