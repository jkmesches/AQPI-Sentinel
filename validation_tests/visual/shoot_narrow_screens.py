#!/usr/bin/env python3
"""The login card on short screens and the desktop routes at 320 px.

Why this exists
---------------
The standards session's ui check of 2026-11-24 found two layout bugs on
small screens:

1. The login card was centered in the layout's overflow-hidden main. On a
   phone held sideways (734 x 343) its title was cut off at the top and the
   Sign In button sat under the footer and past the bottom edge, with no
   way to scroll to it. Phones reach /login from /m/alarms and /m/more.
2. The footer's link group did not wrap. It ran to 376 px and scrolled
   every desktop route sideways in a 320 px window.

The probe checks, on the deployment it is pointed at:

1. On each short screen, once the Sign In button is scrolled into view, it
   is clear of the footer and inside the viewport, and the card's sides
   are inside the viewport.
2. At 320 px no desktop route is wider than the viewport.

Usage
-----
::

    python validation_tests/visual/shoot_narrow_screens.py OUTDIR [--base URL]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

try:
    from playwright.async_api import Browser, async_playwright
except ImportError:  # pragma: no cover - environment check
    print("playwright is not installed: pip install playwright "
          "&& playwright install chromium", file=sys.stderr)
    raise SystemExit(2) from None

DEFAULT_BASE = os.environ.get("SENTINEL_VISUAL_BASE", "http://localhost:5173")
SETTLE_MS = 2500
#: Width x height in CSS px. The first two are the screens the bug showed
#: on; the laptop is there to catch a fix that breaks the common case.
LOGIN_SCREENS = ((734, 343), (320, 568), (1366, 768))
NARROW = (320, 568)
#: The desktop routes. Headless Chromium's user agent is not a phone's, so
#: the root URL is not sent to /m.
ROUTES = ("/", "/dashboard", "/timeline", "/history", "/faq", "/login")
#: Subpixel layout lets a box end a fraction past the edge it is aligned to.
EDGE_PX = 0.5

#: Far enough to reach the bottom of any login card.
WHEEL_PX = 2000
#: The top and bottom of main's part inside the viewport, in CSS px.
MAIN_JS = """() => {
  const r = document.querySelector('main').getBoundingClientRect();
  return [Math.max(r.top, 0), Math.min(r.bottom, innerHeight)];
}"""
LOGIN_JS = """() => {
  const btn = document.querySelector('form button[type=submit]');
  const form = document.querySelector('form');
  const foot = document.querySelector('footer');
  if (!btn || !form || !foot) return null;
  const b = btn.getBoundingClientRect(), f = form.getBoundingClientRect();
  return {vw: document.documentElement.clientWidth, vh: innerHeight,
          button: [b.top, b.bottom], form: [f.left, f.right], formTop: f.top,
          footerTop: foot.getBoundingClientRect().top};
}"""


def login_problems(size: tuple[int, int], top: dict[str, Any] | None,
                   m: dict[str, Any] | None) -> list[str]:
    """List what is wrong with one login screen.

    Args:
        size: The screen, width and height in CSS px.
        top: What LOGIN_JS returned on load, before any scroll.
        m: What LOGIN_JS returned after scrolling down with the wheel.

    Returns:
        One line per problem, empty when the card is reachable and inside.
    """
    tag = f"/login {size[0]}x{size[1]}"
    if top is None or m is None:
        return [f"{tag}: no form, submit button or footer on the page"]
    out: list[str] = []
    if top["formTop"] < -EDGE_PX:
        out.append(f"{tag}: card top is cut off at {top['formTop']:.1f} on load")
    btn_top, btn_bottom = m["button"]
    if btn_bottom > m["footerTop"] + EDGE_PX:
        out.append(f"{tag}: button bottom {btn_bottom:.1f} is under the footer "
                   f"at {m['footerTop']:.1f}")
    if btn_top < -EDGE_PX or btn_bottom > m["vh"] + EDGE_PX:
        out.append(f"{tag}: button {btn_top:.1f}-{btn_bottom:.1f} is outside the "
                   f"{m['vh']} px viewport")
    left, right = m["form"]
    if left < -EDGE_PX or right > m["vw"] + EDGE_PX:
        out.append(f"{tag}: card {left:.1f}-{right:.1f} is outside the "
                   f"{m['vw']} px viewport")
    return out


async def check_login(browser: Browser, base: str, outdir: Path
                      ) -> tuple[list[dict[str, Any]], list[str]]:
    """Measure the login card on each short screen.

    Args:
        browser: A launched browser.
        base: The deployment's origin.
        outdir: Where the screenshots go.

    Returns:
        The measurements and the problems found.
    """
    rows: list[dict[str, Any]] = []
    problems: list[str] = []
    for w, h in LOGIN_SCREENS:
        ctx = await browser.new_context(viewport={"width": w, "height": h})
        try:
            page = await ctx.new_page()
            _ = await page.goto(f"{base}/login", wait_until="load", timeout=60_000)
            await page.wait_for_timeout(SETTLE_MS)
            top: dict[str, Any] | None = await page.evaluate(LOGIN_JS)
            # The wheel, as a reader scrolls. Playwright's scroll-into-view
            # also scrolls an overflow-hidden box, which a reader cannot,
            # and it passed this probe on the broken page.
            # Over the visible part of main: at 320 px the header wraps to
            # about 330 px tall, and a wheel turned over it scrolls nothing.
            box = await page.evaluate(MAIN_JS)
            await page.mouse.move(w / 2, (box[0] + box[1]) / 2)
            await page.mouse.wheel(0, WHEEL_PX)
            await page.wait_for_timeout(300)
            m: dict[str, Any] | None = await page.evaluate(LOGIN_JS)
            _ = await page.screenshot(path=str(outdir / f"login_{w}x{h}.png"))
        finally:
            await ctx.close()
        rows.append({"size": [w, h], "on_load": top, "scrolled": m})
        problems += login_problems((w, h), top, m)
    return rows, problems


async def check_widths(browser: Browser, base: str
                       ) -> tuple[dict[str, int], list[str]]:
    """Measure each desktop route's width at 320 px.

    Args:
        browser: A launched browser.
        base: The deployment's origin.

    Returns:
        Each route's scroll width, and the problems found.
    """
    widths: dict[str, int] = {}
    problems: list[str] = []
    ctx = await browser.new_context(viewport={"width": NARROW[0], "height": NARROW[1]})
    try:
        page = await ctx.new_page()
        for path in ROUTES:
            _ = await page.goto(base + path, wait_until="load", timeout=60_000)
            await page.wait_for_timeout(SETTLE_MS)
            width: int = await page.evaluate("document.documentElement.scrollWidth")
            widths[path] = width
            if width > NARROW[0]:
                problems.append(f"{path}: {width} px wide in a {NARROW[0]} px window")
    finally:
        await ctx.close()
    return widths, problems


async def run(base: str, outdir: Path) -> dict[str, Any]:
    """Run both checks.

    Args:
        base: The deployment's origin.
        outdir: Where the screenshots go.

    Returns:
        The report, with a `problems` list.
    """
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        try:
            login, login_bad = await check_login(browser, base, outdir)
            widths, width_bad = await check_widths(browser, base)
        finally:
            await browser.close()
    return {"base": base, "login": login, "widths": widths,
            "problems": login_bad + width_bad}


def main() -> int:
    """Run the probe and report.

    Returns:
        0 when nothing was wrong, 1 otherwise.
    """
    ap = argparse.ArgumentParser(description="narrow and short screen probe")
    ap.add_argument("outdir", type=Path)
    ap.add_argument("--base", default=DEFAULT_BASE)
    args = ap.parse_args()
    outdir: Path = args.outdir
    outdir.mkdir(parents=True, exist_ok=True)
    report = asyncio.run(run(args.base, outdir))
    _ = (outdir / "narrow_screens.json").write_text(json.dumps(report, indent=1))
    print(json.dumps(report["widths"], indent=1))
    for p in report["problems"]:
        print(f"  FAIL  {p}")
    print("all narrow screen checks passed" if not report["problems"]
          else f"{len(report['problems'])} FAILED")
    return 0 if not report["problems"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
