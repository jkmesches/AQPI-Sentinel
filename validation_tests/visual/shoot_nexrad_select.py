#!/usr/bin/env python3
"""Selecting the NEXRAD sites on the map, and where their frames land.

Why this exists
---------------
Joseph asked on 2026-11-22 for the NEXRAD imagery to be selectable, with
presets named for the profile, NEXRAD, all and clear. The frames are on the
AQPI mount only, and the test deployment has none, so this probe serves them
itself: it marks the three sites as having frames in /api/radars/meta and
answers their frame and step requests.

Four things can go wrong without any of them showing in a screenshot. A
400 km frame placed by latitude sits 2.5 km north of the radar. A NEXRAD
layer added after an X-band one covers it. The readout can report the frame
underneath rather than the one on top. And a NEXRAD site left on under
Velocity asks for a moment its plotter never draws.

The X-band and NEXRAD frames are flat, in two different reflectivity
colors, so a reading names which frame answered.

Usage
-----
::

    python validation_tests/visual/shoot_nexrad_select.py OUTDIR [--base URL]
        [--frames DIR]

``--frames`` is a directory holding ``<SITE>/Reflectivity/*.png`` copied off
the mount. With it, the last step serves the newest real frame per site and
saves a screenshot of the three, for reading by eye.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import sys
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

VISUAL = Path(__file__).resolve().parent
sys.path.insert(0, str(VISUAL))

try:
    from PIL import Image
    from playwright.async_api import Page, Route, async_playwright
except ImportError:  # pragma: no cover - environment check
    print("pillow and playwright are needed: pip install pillow playwright "
          "&& playwright install chromium", file=sys.stderr)
    raise SystemExit(2) from None

from shoot_radar_readout import read_at, solo, turn_on  # noqa: E402  sibling module, loaded by path
from shoot_tilt_readout import expected, ramp, rgb_of  # noqa: E402  sibling module, loaded by path

DEFAULT_BASE = os.environ.get("SENTINEL_VISUAL_BASE", "http://localhost:5173")
VIEWPORT = {"width": 1500, "height": 950}
LOAD_SETTLE_MS = 12_000
STEP_SETTLE_MS = 6_000
NEXRAD = ("KBBX", "KDAX", "KMUX")
NETWORK = ("XSCV", "XSCW", "XSCR", "XEBY", "XSWR", "CBAND")
#: Reflectivity stops painted on each kind of frame. Different colors, so a
#: reading says which frame answered.
XBAND_STOP = 12
NEXRAD_STOP = 6
FRAME_PX = 693
#: Earth's mean radius in km, as radarGeo.ts takes it.
EARTH_KM = 6371.0


def flat_png(rgb: tuple[int, int, int]) -> bytes:
    """A whole square frame in one opaque color."""
    buf = BytesIO()
    Image.new("RGBA", (FRAME_PX, FRAME_PX), (*rgb, 255)).save(buf, format="PNG")
    return buf.getvalue()


def lat2y(lat: float) -> float:
    """Latitude to Web Mercator y, in radians of the unit sphere."""
    return math.log(math.tan(math.pi / 4 + math.radians(lat) / 2))


def y2lat(y: float) -> float:
    """Web Mercator y back to latitude in degrees."""
    return math.degrees(2 * math.atan(math.exp(y)) - math.pi / 2)


def placement(coords: list[list[float]], lat: float, lon: float) -> dict[str, float]:
    """Where a placed frame's middle pixel lands, and how wide the frame is.

    MapLibre interpolates an image linearly in Mercator, so the middle pixel
    is at the Mercator midpoint of the corners.

    Args:
        coords: The source's corners, clockwise from the north-west.
        lat: The radar's latitude.
        lon: The radar's longitude.

    Returns:
        The middle pixel's offset from the radar in meters (north and east),
        and the frame's width and height in km at the radar's scale.
    """
    (west, north), (east, _), (_, south), _ = coords
    mid_lat = y2lat((lat2y(north) + lat2y(south)) / 2)
    km_per_rad = EARTH_KM * math.cos(math.radians(lat))
    return {
        "north_m": (mid_lat - lat) * 111_195.0,
        "east_m": ((west + east) / 2 - lon) * 111_195.0 * math.cos(math.radians(lat)),
        "width_km": math.radians(east - west) * km_per_rad,
        "height_km": (lat2y(north) - lat2y(south)) * km_per_rad,
    }


async def snapshot(page: Page) -> dict[str, Any]:
    """The map's structural probe for the first pane."""
    got: dict[str, Any] = await page.evaluate("() => window.__sentinelProbe?.() ?? {}")
    return got


def layer_index(snap: dict[str, Any], radar: str) -> int:
    """A radar layer's place in the paint order, or -1 when it is absent."""
    order: list[str] = snap.get("layerOrder", [])
    name = f"radar-{radar}-layer"
    return order.index(name) if name in order else -1


async def click_preset(page: Page, name: str) -> None:
    """Press one preset in the radar dock and let the frames land."""
    await page.locator(f"[data-preset='{name}']").first.click()
    await page.wait_for_timeout(STEP_SETTLE_MS)


async def pick_moment(page: Page, short: str) -> None:
    """Press one moment tab by its short label and let the overlays settle."""
    await page.get_by_role("button", name=short, exact=True).first.click()
    await page.wait_for_timeout(STEP_SETTLE_MS)


def make_router(paint: dict[str, bytes], requests: list[str],
                ) -> dict[str, Callable[[Route], Awaitable[None]]]:
    """The handlers that stand in for a mount on the test deployment.

    Args:
        paint: Frame bytes by kind, "xband" and "nexrad", or a site id for a
            real frame. Read at request time, so a step can repaint.
        requests: Collects every frame URL the page asked for.

    Returns:
        Handlers by URL glob.
    """
    async def frame(route: Route) -> None:
        url = route.request.url
        requests.append(url)
        radar = parse_qs(urlparse(url).query).get("radar", [""])[0]
        body = paint.get(radar) or paint["nexrad" if radar in NEXRAD else "xband"]
        await route.fulfill(status=200, body=body,
                            headers={"content-type": "image/png",
                                     "cache-control": "no-store"})

    async def meta(route: Route) -> None:
        resp = await route.fetch()
        rows: list[dict[str, Any]] = await resp.json()
        for r in rows:
            if r["id"] in NEXRAD:
                r["scan"] = True
        await route.fulfill(response=resp, json=rows)

    async def steps(route: Route) -> None:
        radar = parse_qs(urlparse(route.request.url).query).get("radar", [""])[0]
        if radar not in NEXRAD:
            await route.continue_()
            return
        now = datetime.now(UTC).replace(microsecond=0)
        ts = [(now - timedelta(minutes=5 * k)).isoformat() for k in range(6, -1, -1)]
        await route.fulfill(json={"steps": [{"i": i, "ts": t} for i, t in enumerate(ts)]})

    return {"**/xband_scan.png*": frame, "**/api/radars/meta": meta,
            "**/api/upstream/radar_steps*": steps}


async def check_presets(page: Page, out: list[dict[str, Any]]) -> None:
    """The dock offers the four presets, named for this profile.

    Args:
        page: The loaded console.
        out: Collects each step's reading.
    """
    names: list[str] = await page.evaluate(
        "() => [...document.querySelectorAll('[data-preset]')]"
        ".map(e => e.dataset.preset + '=' + e.textContent.trim())")
    first_pane = names[:4]
    out.append({"step": "preset row", "want": ["network=AQPI", "nexrad=nexrad",
                                               "all=all", "clear=clear"],
                "got": first_pane})
    await click_preset(page, "nexrad")
    snap = await snapshot(page)
    out.append({"step": "nexrad preset selects the three sites",
                "want": list(NEXRAD), "got": snap.get("state", {}).get("activeRadars")})
    src = snap.get("sources", {}).get("radar-KDAX-src", {})
    meta: list[dict[str, Any]] = await page.evaluate(
        "() => fetch('/api/radars/meta').then(r => r.json())")
    kdax = next(r for r in meta if r["id"] == "KDAX")
    got = placement(src.get("coordinates"), kdax["lat"], kdax["lon"])
    out.append({"step": "KDAX middle pixel on the radar (m north, m east)",
                "want": [0.0, 0.0],
                "got": [round(got["north_m"], 1), round(got["east_m"], 1)]})
    out.append({"step": "KDAX frame size (km across, km down)",
                "want": [400.0, 400.0],
                "got": [round(got["width_km"], 3), round(got["height_km"], 3)]})


async def check_stacking(page: Page, refl: dict[str, Any],
                         out: list[dict[str, Any]]) -> None:
    """A NEXRAD frame added after an X-band one still draws, and reads, under it.

    Args:
        page: The loaded console.
        refl: The reflectivity ramp both kinds of frame are painted from.
        out: Collects each step's reading.
    """
    await solo(page, "KDAX")
    out.append({"step": "KDAX alone reads its own frame",
                "want": expected(refl, NEXRAD_STOP), "got": await read_at(page, 0.5, 0.5)})
    await solo(page, "XEBY")
    await turn_on(page, "KDAX")
    snap = await snapshot(page)
    out.append({"step": "KDAX added after XEBY draws under it",
                "want": True,
                "got": 0 <= layer_index(snap, "KDAX") < layer_index(snap, "XEBY")})
    out.append({"step": "over XEBY the reading is XEBY's",
                "want": expected(refl, XBAND_STOP), "got": await read_at(page, 0.5, 0.5)})
    await pick_moment(page, "V")
    snap = await snapshot(page)
    out.append({"step": "under Velocity KDAX stays on but undrawn",
                "want": [True, -1, True],
                "got": ["KDAX" in snap.get("state", {}).get("activeRadars", []),
                        layer_index(snap, "KDAX"), layer_index(snap, "XEBY") >= 0]})
    await pick_moment(page, "Z")
    snap = await snapshot(page)
    out.append({"step": "back on reflectivity KDAX returns under XEBY",
                "want": True,
                "got": 0 <= layer_index(snap, "KDAX") < layer_index(snap, "XEBY")})


async def check_all_and_network(page: Page, out: list[dict[str, Any]]) -> None:
    """ALL keeps every NEXRAD layer under every network layer, and AQPI drops them.

    Args:
        page: The loaded console.
        out: Collects each step's reading.
    """
    await click_preset(page, "all")
    snap = await snapshot(page)
    nex = [layer_index(snap, r) for r in NEXRAD]
    own = [layer_index(snap, r) for r in NETWORK]
    out.append({"step": "all: every NEXRAD layer under every network layer",
                "want": True,
                "got": min(nex) >= 0 and min(own) >= 0 and max(nex) < min(own)})
    await click_preset(page, "network")
    snap = await snapshot(page)
    out.append({"step": "network preset selects the network alone",
                "want": sorted(NETWORK),
                "got": sorted(snap.get("state", {}).get("activeRadars", []))})


async def shoot_real(page: Page, paint: dict[str, bytes], frames: Path,
                     outdir: Path) -> None:
    """Serve the newest real frame per site and save the three for reading by eye.

    Args:
        page: The loaded console.
        paint: The served frames, which this adds the real ones to.
        frames: Holds <SITE>/Reflectivity/*.png.
        outdir: Where the screenshot goes.
    """
    for site in NEXRAD:
        names = sorted(p for p in (frames / site / "Reflectivity").glob(f"{site}_*.png")
                       if "colorbar" not in p.name and ".tmp." not in p.name)
        if names:
            paint[site] = names[-1].read_bytes()
    await click_preset(page, "clear")
    await click_preset(page, "nexrad")
    _ = await page.screenshot(path=str(outdir / "nexrad_real.png"))


async def run(base: str, outdir: Path, frames: Path | None) -> dict[str, Any]:
    """Every step, in one browser.

    Args:
        base: The deployment's origin.
        outdir: Where the screenshots go.
        frames: Real frames to finish on, or None.

    Returns:
        The report.
    """
    refl = ramp("aqpi-xband-reflectivity")
    paint = {"xband": flat_png(rgb_of(refl["stops"][XBAND_STOP])),
             "nexrad": flat_png(rgb_of(refl["stops"][NEXRAD_STOP]))}
    requests: list[str] = []
    steps: list[dict[str, Any]] = []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        try:
            ctx = await browser.new_context(viewport=VIEWPORT, device_scale_factor=1)
            page = await ctx.new_page()
            await page.add_init_script(
                "window.__errs=[];"
                "window.addEventListener('error', e => window.__errs.push(String(e.message)));"
                "localStorage.clear(); localStorage.setItem('sentinel-theme','dark');"
                "localStorage.setItem('sentinel-map-prefs', JSON.stringify({activeRadars: [],"
                " moment: 'Reflectivity', composite: 'none', nexrad: false}));")
            for glob, handler in make_router(paint, requests).items():
                await page.route(glob, handler)
            _ = await page.goto(f"{base}/?diag=probe", wait_until="load", timeout=60_000)
            await page.wait_for_timeout(LOAD_SETTLE_MS)
            await check_presets(page, steps)
            await check_stacking(page, refl, steps)
            await check_all_and_network(page, steps)
            if frames is not None:
                await shoot_real(page, paint, frames, outdir)
            errs: list[str] = await page.evaluate("() => window.__errs || []")
        finally:
            await browser.close()
    wrong = [u for u in requests
             if parse_qs(urlparse(u).query).get("radar", [""])[0] in NEXRAD
             and parse_qs(urlparse(u).query).get("moment", ["Reflectivity"])[0]
             != "Reflectivity"]
    steps.append({"step": "no NEXRAD frame asked for in another moment",
                  "want": [], "got": wrong})
    steps.append({"step": "page errors", "want": [], "got": errs})
    return {"base": base, "steps": steps, "frame_requests": len(requests)}


def main() -> int:
    """Run the probe and report.

    Returns:
        0 when every step read what it should, 1 otherwise.
    """
    ap = argparse.ArgumentParser(description="NEXRAD selection probe")
    ap.add_argument("outdir", type=Path)
    ap.add_argument("--base", default=DEFAULT_BASE)
    ap.add_argument("--frames", type=Path, default=None)
    args = ap.parse_args()
    outdir: Path = args.outdir
    outdir.mkdir(parents=True, exist_ok=True)
    report = asyncio.run(run(args.base, outdir, args.frames))
    _ = (outdir / "nexrad_select.json").write_text(json.dumps(report, indent=1))
    bad = 0
    for s in report["steps"]:
        ok = s["got"] == s["want"]
        bad += 0 if ok else 1
        print(f"  {'ok  ' if ok else 'FAIL'}  {s['step']}: {s['got']!r}"
              + ("" if ok else f" (want {s['want']!r})"))
    print(f"{report['frame_requests']} frame requests served")
    print("all NEXRAD selection steps passed" if bad == 0 else f"{bad} FAILED")
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
