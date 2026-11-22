#!/usr/bin/env python3
"""A tilt is drawn on the surface its beam actually sweeps.

Why this exists
---------------
`beamVertices` puts each vertex of a tilt at its own beam height under
the 4/3 earth model, and the unit tests check that arithmetic. They
cannot check that the layer the console actually draws uses it. A
custom GL layer that silently fell back to a flat quad, or whose
altitudes never reached the shader, would pass every one of them and
look almost right on screen.

So this measures the rendered picture. Every tilt frame is served as
one opaque magenta square, which makes the painted region segment by
color alone, and the camera, the theme, the tilt and the layer opacity
are pinned identical across two runs. The only difference is the
surface: with 3D terrain off the frame is draped on the ground, with it
on the custom layer places each vertex at its own height.

What separates a beam from a flat sheet is that the beam answers to
the tilt angle. Its height is the distance from the dish times the
tangent of that angle, so walking the elevation up its four steps must
raise the surface each time, while a sheet at an altitude off a slider
would sit still through all four.

The claim is the ordering alone. Converting a beam
height into screen pixels needs MapLibre's projection at a given pitch
and latitude, and the conversion is not linear in height: a higher
surface sits nearer the camera at this pitch and gains pixels faster
than its meters would suggest. Measured lifts for the lowest and
highest tilt came out in a ratio of 3.9 at one zoom and 7.0 at another,
against the 2.84 their beam heights have, which is that nonlinearity
rather than a fault. A check asserting either number would have been
asserting the zoom. Four lifts in the right order are enough, and
nothing that ignores the elevation angle can produce them.

Two earlier versions got the geometry wrong, and each failure says
something about the shape. One compared the far and near edges of the quad, on the idea
that a beam lifts the far edge and leaves the near one alone; the
extent is a box centered on the radar, so its north and south edges are
both 40 km out and the beam lifts them alike, and that check passed a
flat 2 km sheet by 9 px against 0. The other predicted the ratio above.

Usage
-----
::

    python validation_tests/visual/shoot_beam_surface.py OUTDIR \
        [--base URL]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from itertools import pairwise
from dataclasses import dataclass
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
        Browser, BrowserContext, Route, async_playwright)
    from playwright.async_api import TimeoutError as PlaywrightTimeout
except ImportError:  # pragma: no cover - environment check
    print("playwright is not installed: pip install playwright "
          "&& playwright install chromium", file=sys.stderr)
    raise SystemExit(2) from None

DEFAULT_BASE = "https://the test deployment"

VIEWPORT = {"width": 1500, "height": 950}
LOAD_SETTLE_MS = 17_000
TILT_SETTLE_MS = 13_000

#: The served frame's color. Far from anything a basemap draws, so a
#: tolerant match cannot pick up terrain, water or a road.
PAINT = (255, 0, 255)

#: Pitched hard, because a beam height projects to nothing looking
#: straight down. Pinned through the permalink so both runs share it:
#: turning 3D terrain on moves the pitch by itself, and comparing two
#: pitches would measure that instead of the surface.
#:
#: Zoomed out far enough that the whole quad is inside the pane. At 8.9
#: its near edge ran off the bottom and so could not move, which made
#: the near lift read 0 whatever the surface was doing, and a flat sheet
#: at the default 2 km passed this check by 9 px against 0.
CAMERA = "-122.85,38.47,8.6,0,60"

#: How far the quad must stay from the pane's edges, in pixels. An edge
#: sitting on the boundary is clipped, and a clipped edge cannot lift.
EDGE_MARGIN_PX = 12

#: One X-band radar, its highest tilt, every layer fully opaque. At the
#: default 80% the painted pixels are a blend with whatever is under
#: them and no longer segment cleanly.
PREFS = json.dumps({
    "activeRadars": ["XSCW"], "moment": "Reflectivity",
    "composite": "none", "nexrad": False, "tiltEl": 4,
    "opacity": 1,
    "layerOpacity": {"composite": 1, "radars": 1, "nexrad": 1},
})

#: Every elevation the control offers, lowest first, as it indexes
#: them. All four, because four lifts in the right order is a far
#: stronger signature than two and costs one render each.
TILTS = ("1", "2", "3", "4")

#: The smallest step between one tilt's lift and the next, in pixels.
#: The centroid is a weighted mean over tens of thousands of pixels, so
#: it is steady to well under a pixel between runs, and the measured
#: steps at this camera run 5 to 12 px. Two is clear of the noise and
#: far under the smallest real step.
MIN_STEP_PX = 2


def flat_png() -> bytes:
    """One tilt frame, painted end to end in the match color.

    Returns:
        The PNG bytes.
    """
    buf = BytesIO()
    Image.new("RGBA", (700, 700), (*PAINT, 255)).save(buf, format="PNG")
    return buf.getvalue()


def painted_band(path: Path) -> dict[str, Any] | None:
    """Where the painted quad sits on screen.

    Args:
        path: The screenshot to read.

    Returns:
        Its top and bottom rows, its vertical centroid and how many
        pixels were sampled, or None when nothing was painted.
    """
    im = Image.open(path).convert("RGB")
    width, height = im.size
    px = im.load()
    # Every second column. The quad is hundreds of pixels wide, so this
    # halves the work and changes no row's verdict.
    counts = [
        sum(1 for x in range(0, width, 2)
            if px[x, y][0] > 190 and px[x, y][2] > 190 and px[x, y][1] < 90)
        for y in range(height)
    ]
    widest = max(counts)
    if widest == 0:
        return None
    # A row belongs to the quad when it is a real fraction of the widest
    # one. A fixed floor of five pixels let the legend in: the
    # reflectivity bar carries magenta stops, and five of its pixels at
    # rows 650 to 655 set the quad's bottom edge in every run, at both
    # zooms, which made the near lift read 0 whatever the surface did.
    floor = max(8, widest * 0.05)
    rows: list[int] = []
    total = 0
    weighted = 0
    for y, n in enumerate(counts):
        if n >= floor:
            rows.append(y)
            total += n
            weighted += y * n
    if not rows:
        return None
    return {"top": rows[0], "bottom": rows[-1],
            "centroid": round(weighted / total, 1), "sampled": total,
            "height": height}


@dataclass(frozen=True)
class Run:
    """What both renders share: where to go and what to serve."""

    base: str
    outdir: Path
    body: bytes


async def render(ctx: BrowserContext, run_with: Run, overlays: str,
                 name: str, tilt: str) -> dict[str, Any]:
    """Draw one tilt once and screenshot the pane.

    Args:
        ctx: The browser context.
        run_with: The origin, the output folder and the frame to serve.
        overlays: The permalink's overlay list, empty for none.
        name: What to call the screenshot.
        tilt: Which elevation to engage, as the control indexes them.

    Returns:
        What the console settled on, with any page errors.

    Raises:
        PlaywrightTimeout: If the console does not load.
    """
    base, outdir, body = run_with.base, run_with.outdir, run_with.body
    page = await ctx.new_page()
    errs: list[str] = []
    page.on("pageerror", lambda e: errs.append(str(e)[:150]))

    async def serve(route: Route) -> None:
        await route.fulfill(status=200, body=body,
                            headers={"content-type": "image/png",
                                     "cache-control": "no-store"})

    await page.route("**/tilt_image.png*", serve)
    await page.add_init_script(
        "localStorage.clear();"
        "localStorage.setItem('sentinel-theme','dark');"
        f"localStorage.setItem('sentinel-map-prefs', {PREFS!r});")
    url = f"{base}/?diag=probe&c={CAMERA}"
    if overlays:
        url += f"&o={overlays}"
    await page.goto(url, wait_until="load", timeout=60_000)
    await page.wait_for_timeout(LOAD_SETTLE_MS)
    _ = await page.locator("[data-tilt-select]").select_option(tilt)
    await page.wait_for_timeout(TILT_SETTLE_MS)
    state: dict[str, Any] = await page.evaluate(
        "()=>{const s=window.__sentinelProbe();"
        " return {cam:s.camera, t3:s.state.terrain3DOn,"
        " tilt:s.state.tiltEl, op:s.state.overlayOpacity}; }")
    # The pointer off the map, so the crosshair is not in the shot.
    await page.mouse.move(6, 6)
    await page.wait_for_timeout(900)
    await page.locator("[data-view]").first.screenshot(
        path=str(outdir / f"{name}.png"))
    await page.close()
    state["errors"] = errs
    return state


def check_setup(shots: dict[str, dict[str, Any]],
                quads: dict[str, dict[str, Any] | None]) -> list[str]:
    """Whether the renders are comparable at all.

    Args:
        shots: Each render's settled state, by name.
        quads: Where each render's quad landed, by name.

    Returns:
        One message per defect.
    """
    out: list[str] = []
    cams = {json.dumps(s["cam"], sort_keys=True) for s in shots.values()}
    if len(cams) != 1:
        out.append(f"the cameras differ across the runs, {sorted(cams)}, so "
                   f"nothing can be compared")
    if shots["draped"]["t3"]:
        out.append("the draped run has 3D terrain on, so it is not the "
                   "ground baseline")
    for t in TILTS:
        name = f"tilt{t}"
        if not shots[name]["t3"]:
            out.append(f"the {name} run has 3D terrain off, so it is draped "
                       f"rather than on a beam")
        if str(shots[name]["tilt"]) != t:
            out.append(f"the {name} run settled on elevation "
                       f"{shots[name]['tilt']} rather than {t}, so the "
                       f"control did not take")
    for name, q in quads.items():
        if not q:
            out.append(f"the {name} render painted nothing, so the served "
                       f"frame never reached the layer")
            continue
        if q["top"] < EDGE_MARGIN_PX or \
                q["bottom"] > q["height"] - EDGE_MARGIN_PX:
            out.append(
                f"the {name} quad runs to the pane's edge, rows {q['top']} "
                f"to {q['bottom']} of {q['height']}, so it is clipped and "
                f"cannot move. Zoom the pinned camera out")
    return out


def judge(quads: dict[str, dict[str, Any] | None]) -> tuple[list[str],
                                                           list[float]]:
    """Does the surface rise with every step of the elevation angle.

    Args:
        quads: Where each render's quad landed, by name.

    Returns:
        One message per defect, and the lift at each tilt in order.
    """
    ground = quads["draped"]
    if not ground:
        return ["the draped baseline painted nothing"], []
    lifts: list[float] = []
    for t in TILTS:
        q = quads[f"tilt{t}"]
        if not q:
            return [f"tilt {t} painted nothing"], lifts
        lifts.append(round(ground["centroid"] - q["centroid"], 1))
    out: list[str] = []
    if lifts[0] <= 0:
        out.append(
            f"the lowest tilt sits {-lifts[0]:.1f} px below the draped "
            f"baseline rather than above it, so the surface is not in the "
            f"air at all")
    steps = [b - a for a, b in pairwise(lifts)]
    flat = [i for i, s in enumerate(steps) if s < MIN_STEP_PX]
    if flat:
        out.append(
            f"the surface did not rise between tilts {[TILTS[i] for i in flat]}"
            f" and the next: lifts {lifts} give steps {steps}, against the "
            f"{MIN_STEP_PX} px each step needs. A beam's height is the "
            f"distance from the dish times the tangent of its elevation, so "
            f"a surface that holds still through the four is not one")
    return out, lifts


#: The renders: the ground baseline, then the same scan on its beam at
#: each elevation in turn.
SHOTS = (("draped", "", TILTS[0]),
         *((f"tilt{t}", "terrain-3d", t) for t in TILTS))


async def run(base: str, outdir: Path) -> dict[str, Any]:
    """Draw the scan draped and on two beams, and compare.

    Args:
        base: The deployment's origin.
        outdir: Where to write the screenshots.

    Returns:
        What the run showed, with a "problems" list.
    """
    rep: dict[str, Any] = {"base": base}
    problems: list[str] = []
    body = flat_png()
    async with async_playwright() as pw:
        browser: Browser = await pw.chromium.launch()
        try:
            ctx = await browser.new_context(
                viewport=VIEWPORT, device_scale_factor=1)
            shared = Run(base=base, outdir=outdir, body=body)
            shots: dict[str, dict[str, Any]] = {}
            quads: dict[str, dict[str, Any] | None] = {}
            for name, overlays, tilt in SHOTS:
                shots[name] = await render(
                    ctx, shared, overlays, f"beam-{name}", tilt)
                quads[name] = painted_band(outdir / f"beam-{name}.png")
                if shots[name]["errors"]:
                    problems.append(
                        f"{name}: page errors {shots[name]['errors'][:2]}")
            rep["shots"] = shots
            rep["quads"] = quads
            problems += check_setup(shots, quads)
            if not problems:
                found, lifts = judge(quads)
                problems += found
                rep["lifts_px"] = lifts
        except (PlaywrightTimeout, RuntimeError) as exc:
            problems.append(f"the console stopped answering: {exc}")
        finally:
            await browser.close()
    rep["problems"] = problems
    return rep


def main() -> int:
    """Check the tilt is drawn on its beam surface.

    Returns:
        0 when it is, 1 otherwise.
    """
    ap = argparse.ArgumentParser(description=__doc__)
    _ = ap.add_argument("outdir", type=Path)
    _ = ap.add_argument("--base", default=DEFAULT_BASE)
    args = ap.parse_args()
    outdir: Path = args.outdir
    outdir.mkdir(parents=True, exist_ok=True)

    rep = asyncio.run(run(args.base, outdir))
    _ = (outdir / "report.json").write_text(json.dumps(rep, indent=2))

    for name, q in (rep.get("quads") or {}).items():
        if q:
            print(f"  {name:7s} rows {q['top']}-{q['bottom']}, "
                  f"centroid {q['centroid']}, {q['sampled']} px")
    print(f"lift off the ground, by tilt: {rep.get('lifts_px')} px")

    problems: list[str] = rep.get("problems", [])
    if problems:
        print("\nFAIL:")
        for line in problems:
            print(f"  {line}")
        return 1
    print(f"\nok: the surface rises at every step of the elevation angle, "
          f"which nothing drawn at a fixed altitude does; images in {outdir}")
    return 0


raise SystemExit(main())
