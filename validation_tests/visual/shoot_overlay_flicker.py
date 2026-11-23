#!/usr/bin/env python3
"""Whether the radar and NEXRAD overlays blink during playback or an opacity drag.

Why this exists
---------------
Joseph saw radar imagery flicker between frames while playing every radar
on reflectivity with no composite, and radar and NEXRAD imagery flicker
while dragging an opacity slider, both on 2026-11-23.

Two faults can each draw a frame with the overlay missing. MapLibre fades
a raster in from transparent after every image update unless its layer
sets ``raster-fade-duration`` to 0, so each step blinks. And an effect
that rebuilds a source whenever it re-runs blinks every time something it
reads incidentally changes, which for NEXRAD was every opacity input and
every playback step.

The probe serves every radar and NEXRAD frame painted in one flat color,
so the overlay covers the ground it is placed on and a blink is a drop in
painted area rather than a judgment about weather. It samples the canvas
on every animation frame and reports the deepest drop below the steady
level, and how many frames sat in it.

Usage
-----
::

    python validation_tests/visual/shoot_overlay_flicker.py OUTDIR [--base URL]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from io import BytesIO
from pathlib import Path
from typing import Any

try:
    from PIL import Image
except ImportError:  # pragma: no cover - environment check
    print("pillow is not installed: pip install pillow", file=sys.stderr)
    raise SystemExit(2) from None

try:
    from playwright.async_api import Page, Route, async_playwright
except ImportError:  # pragma: no cover - environment check
    print("playwright is not installed: pip install playwright "
          "&& playwright install chromium", file=sys.stderr)
    raise SystemExit(2) from None

DEFAULT_BASE = os.environ.get("SENTINEL_VISUAL_BASE", "http://localhost:5173")
VIEWPORT = {"width": 1500, "height": 950}
LOAD_SETTLE_MS = 14_000
SAMPLE_MS = 6000
RADARS = ["XEBY", "XSCR", "XSCV", "XSCW", "XSWR", "CBAND"]
#: Radars in magenta and NEXRAD in cyan, far from the basemap and from each
#: other, so a radar that blinks over NEXRAD is not counted as still there.
#: Magenta at 70% over cyan still leads green by 100 in red and blue.
RADAR_PAINT = (255, 0, 255)
NEXRAD_PAINT = (0, 255, 255)
#: A drop to below this share of the steady painted area counts as a blink.
#: The overlays sit at 70% to 100% opacity, so a steady frame keeps well
#: over half its painted pixels magenta-dominant, and a fade from
#: transparent passes through nearly none.
BLINK_SHARE = 0.5
#: How long each served frame takes. The Iowa State WMS answers in a few
#: hundred milliseconds from here, and the radar frames over the LAN in tens.
SERVE_DELAY_S = 0.4

# Runs in the page. Reads the map canvas through a 2D copy on every animation
# frame and counts pixels whose red and blue both clear green by a margin,
# which only the painted overlays produce on the dark basemap.
SAMPLER_JS = """
(ms) => new Promise((resolve) => {
  const canvas = document.querySelector('[data-view] canvas.maplibregl-canvas');
  if (!canvas) { resolve([]); return; }
  const w = 240, h = 160;
  const off = document.createElement('canvas');
  off.width = w; off.height = h;
  const ctx = off.getContext('2d', { willReadFrequently: true });
  const out = [];
  const t0 = performance.now();
  const tick = () => {
    ctx.drawImage(canvas, 0, 0, w, h);
    const d = ctx.getImageData(0, 0, w, h).data;
    let radar = 0, nexrad = 0;
    for (let i = 0; i < d.length; i += 4) {
      if (d[i] - d[i + 1] > 60 && d[i + 2] - d[i + 1] > 60) radar++;
      else if (d[i + 1] - d[i] > 60 && d[i + 2] - d[i] > 60) nexrad++;
    }
    out.push([radar, nexrad]);
    if (performance.now() - t0 < ms) requestAnimationFrame(tick);
    else resolve(out);
  };
  requestAnimationFrame(tick);
})
"""


def flat_png(size: tuple[int, int], rgb: tuple[int, int, int]) -> bytes:
    """A frame painted in one color at full alpha."""
    buf = BytesIO()
    Image.new("RGBA", size, (*rgb, 255)).save(buf, format="PNG")
    return buf.getvalue()


STEP_JS = """
() => {
  const panes = window.__sentinelPanes;
  if (!panes) return null;
  const s = panes[Object.keys(panes).sort()[0]]();
  return s && s.state ? s.state.stepIdx : null;
}
"""


async def open_console(base: str, page: Page) -> None:
    """Load every radar and NEXRAD on reflectivity with no composite.

    Args:
        base: The deployment's origin.
        page: A fresh page.
    """
    await page.add_init_script(
        "localStorage.clear();"
        "localStorage.setItem('sentinel-theme','dark');"
        "localStorage.setItem('sentinel-map-prefs', JSON.stringify("
        f"  {{activeRadars:{json.dumps(RADARS)}, moment:'Reflectivity',"
        "   composite:'none', nexrad:true}));")
    scan = flat_png((697, 693), RADAR_PAINT)
    wms = flat_png((1024, 1100), NEXRAD_PAINT)

    # Delayed like a real fetch. Answered at once, a rebuild that leaves a
    # layer with no image until its request returns refills within a frame
    # and never shows.
    async def serve_scan(route: Route) -> None:
        await asyncio.sleep(SERVE_DELAY_S)
        await route.fulfill(status=200, body=scan, headers={
            "content-type": "image/png", "cache-control": "no-store",
            "access-control-allow-origin": "*"})

    async def serve_wms(route: Route) -> None:
        await asyncio.sleep(SERVE_DELAY_S)
        await route.fulfill(status=200, body=wms, headers={
            "content-type": "image/png", "cache-control": "no-store",
            "access-control-allow-origin": "*"})

    await page.route("**/xband_scan.png*", serve_scan)
    await page.route("**/n0q-t.cgi*", serve_wms)
    # The canvas has to be readable for the sampler, which MapLibre allows
    # only with the drawing buffer kept between frames.
    await page.add_init_script("""
      const orig = HTMLCanvasElement.prototype.getContext;
      HTMLCanvasElement.prototype.getContext = function (type, attrs) {
        if (type === 'webgl' || type === 'webgl2') {
          attrs = Object.assign({}, attrs, { preserveDrawingBuffer: true });
        }
        return orig.call(this, type, attrs);
      };""")
    await page.goto(f"{base}/?diag=probe", wait_until="load", timeout=60_000)
    await page.wait_for_timeout(LOAD_SETTLE_MS)


def judge(samples: list[list[int]], what: str) -> dict[str, Any]:
    """Summarize one sampling run for radars and NEXRAD apart.

    Args:
        samples: [radar, nexrad] painted pixel counts, one per animation frame.
        what: Which action was running, for the report.

    Returns:
        Per layer, the steady level, the deepest drop, and how many frames
        blinked.
    """
    if not samples:
        return {"what": what, "frames": 0, "error": "no canvas"}
    out: dict[str, Any] = {"what": what, "frames": len(samples)}
    for i, layer in enumerate(("radar", "nexrad")):
        series = [s[i] for s in samples]
        steady = sorted(series)[len(series) // 2]
        floor = min(series)
        out[layer] = {
            "steady": steady, "lowest": floor,
            "blink_frames": sum(1 for s in series if steady and s < steady * BLINK_SHARE)}
    return out


async def run(base: str) -> dict[str, Any]:
    """Sample a still map, a playback and an opacity drag.

    Args:
        base: The deployment's origin.

    Returns:
        One summary per action.
    """
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(args=["--use-gl=angle", "--use-angle=swiftshader"])
        try:
            ctx = await browser.new_context(viewport=VIEWPORT, device_scale_factor=1)
            page = await ctx.new_page()
            await open_console(base, page)
            still = judge(await page.evaluate(SAMPLER_JS, 3000), "still")
            step_before = await page.evaluate(STEP_JS)
            await page.get_by_role("button", name="play").first.click()
            playing = judge(await page.evaluate(SAMPLER_JS, SAMPLE_MS), "playing")
            playing["steps"] = [step_before, await page.evaluate(STEP_JS)]
            await page.get_by_role("button", name="pause").first.click()
            await page.wait_for_timeout(1500)
            await page.locator("[data-settings-toggle]").click()
            sampler = asyncio.ensure_future(page.evaluate(SAMPLER_JS, 4000))
            for level in (90, 80, 70, 60, 70, 80, 90, 100, 90, 80):
                await page.locator("[data-opacity=radars]").fill(str(level))
                await page.locator("[data-opacity=nexrad]").fill(str(level))
                await page.wait_for_timeout(250)
            dragging = judge(await sampler, "opacity drag")
        finally:
            await browser.close()
    return {"base": base, "runs": [still, playing, dragging]}


def main() -> int:
    """Run the probe and report.

    Returns:
        0 when no action blinked, 1 otherwise.
    """
    ap = argparse.ArgumentParser(description="overlay flicker probe")
    ap.add_argument("outdir", type=Path)
    ap.add_argument("--base", default=DEFAULT_BASE)
    args = ap.parse_args()
    outdir: Path = args.outdir
    outdir.mkdir(parents=True, exist_ok=True)
    report = asyncio.run(run(args.base))
    _ = (outdir / "overlay_flicker.json").write_text(json.dumps(report, indent=1))
    bad = 0
    for r in report["runs"]:
        ok = (r.get("frames", 0) > 0
              and all(r[layer]["steady"] > 0 and r[layer]["blink_frames"] == 0
                      for layer in ("radar", "nexrad")))
        steps = r.get("steps")
        if steps is not None and steps[0] == steps[1]:
            ok = False
            r["error"] = "playback did not move, so it tested nothing"
        bad += 0 if ok else 1
        print(f"  {'ok  ' if ok else 'FAIL'}  {r}")
    print("no overlay blinked" if bad == 0 else f"{bad} FAILED")
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
