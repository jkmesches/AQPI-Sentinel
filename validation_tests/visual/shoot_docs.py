#!/usr/bin/env python3
"""Capture the documentation screenshots with stand-in identities.

The screenshots under ``docs/images`` are published to GitHub Pages, so
whatever the deployment was showing when they were taken is public. The
dashboard shot taken at v0.1.2 carried a real address twice in the alarm
band, because an acknowledged alarm names whoever acknowledged it.

This replaces every address and display name in the payloads with a stand-in
before the page paints, so the capture cannot show one. The rest of the
screen is the deployment's own live state.

Usage
-----
::

    python validation_tests/visual/shoot_docs.py docs/images \\
        [--base URL] [--only live-dashboard]
"""
from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
from pathlib import Path
from typing import Any

try:
    from playwright.async_api import Page, Route, async_playwright
except ImportError:  # pragma: no cover - environment check, not a code path
    print("playwright is not installed: pip install playwright "
          "&& playwright install chromium", file=sys.stderr)
    raise SystemExit(2) from None

DEFAULT_BASE = os.environ.get("SENTINEL_VISUAL_BASE", "http://localhost:5173")

# Matching the shots already in docs/images, which were taken at 1440x900 on
# a 2x display and are 2880x1800 on disk.
VIEWPORT = {"width": 1440, "height": 900}
SCALE = 2

# Long enough for the REST snapshot, the check catalog and the first metric
# batch to land over the socket.
SETTLE_MS = 12000

EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")

# Stand-ins. example.org is reserved by RFC 2606 and can never be a real
# mailbox, so a reader cannot mistake one of these for an address to write to.
STAND_IN_EMAIL = "oncall@example.org"
STAND_IN_NAME = "On-Call"

# Display names that would otherwise identify the operator. Addresses are
# caught by the pattern above; these are the bare names beside them.
REAL_NAMES = ("Mesches", "mesches")

SHOTS: dict[str, str] = {
    "live-dashboard": "/dashboard",
    "timeline": "/timeline",
}

# Controls to click before a shot, so the capture shows a populated panel
# rather than a collapsed one. The alarm band hides acknowledged rows by
# default, and those rows are the ones that name an operator, so the
# dashboard shot is the one that has to prove a stand-in is what appears.
REVEAL: dict[str, str] = {
    "live-dashboard": "text=SHOW ACKNOWLEDGED",
}


def launder(text: str) -> str:
    """Swap every address and operator name in a payload for a stand-in.

    Args:
        text: The raw JSON body the deployment returned.

    Returns:
        The same JSON with identities replaced.
    """
    text = EMAIL_RE.sub(STAND_IN_EMAIL, text)
    for name in REAL_NAMES:
        text = text.replace(name, STAND_IN_NAME)
    return text


async def install_routes(page: Page) -> None:
    """Rewrite every API response before the page sees it.

    Every endpoint is covered rather than the one known to carry an address,
    because a new field naming an operator would otherwise reach a published
    screenshot without anyone noticing.

    Args:
        page: The page to install the handler on.
    """
    async def handler(route: Route) -> None:
        response = await route.fetch()
        body = await response.text()
        await route.fulfill(status=response.status,
                            headers={"content-type": "application/json"},
                            body=launder(body))

    await page.route("**/api/**", handler)


async def capture(page: Page, out: Path, name: str, path: str) -> dict[str, Any]:
    """Take one screenshot and report what it contains.

    Args:
        page: A page with the laundering routes installed.
        out: Directory the PNG goes in.
        name: File stem, without the extension.
        path: Route to visit on the deployment.

    Returns:
        The file written, its size, and whether any address survived.
    """
    # Relative, because the context carries base_url.
    await page.goto(path, wait_until="domcontentloaded", timeout=45000)
    await page.wait_for_timeout(SETTLE_MS)
    selector = REVEAL.get(name)
    if selector:
        control = page.locator(selector).first
        if await control.count():
            await control.click()
            await page.wait_for_timeout(1500)
    shot = out / f"{name}.png"
    await page.screenshot(path=str(shot))
    # Compared without case, because the alarm band uppercases the address it
    # renders and a case-sensitive test calls the stand-in itself a leak.
    rendered = await page.inner_text("body")
    leaked = sorted({e for e in EMAIL_RE.findall(rendered)
                     if e.lower() != STAND_IN_EMAIL})
    return {"file": str(shot), "bytes": shot.stat().st_size, "leaked": leaked}


async def run(out: Path, base: str, wanted: list[str]) -> list[dict[str, Any]]:
    """Capture each requested screenshot.

    Args:
        out: Directory the PNGs go in.
        base: The deployment to render.
        wanted: Which entries of SHOTS to take.

    Returns:
        One reading per screenshot.
    """
    found: list[dict[str, Any]] = []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        ctx = await browser.new_context(viewport=VIEWPORT,
                                        device_scale_factor=SCALE,
                                        base_url=base,
                                        ignore_https_errors=True)
        page = await ctx.new_page()
        await install_routes(page)
        for name in wanted:
            found.append(await capture(page, out, name, SHOTS[name]))
        # Routes still in flight log a noisy failure at teardown otherwise.
        await page.unroute_all(behavior="ignoreErrors")
        await ctx.close()
        await browser.close()
    return found


def main() -> int:
    """Capture the documentation screenshots and report each one.

    Returns:
        0 when no real address reached any screenshot.
    """
    ap = argparse.ArgumentParser(description=__doc__)
    _ = ap.add_argument("out", type=Path)
    _ = ap.add_argument("--base", default=DEFAULT_BASE)
    _ = ap.add_argument("--only", action="append", choices=sorted(SHOTS))
    args = ap.parse_args()

    wanted: list[str] = args.only or sorted(SHOTS)
    args.out.mkdir(parents=True, exist_ok=True)
    found = asyncio.run(run(args.out, args.base, wanted))

    bad = 0
    for f in found:
        ok = not f["leaked"]
        print(f"  {'ok  ' if ok else 'LEAK'}  {f['file']}  "
              f"{f['bytes'] // 1024} KB")
        if f["leaked"]:
            print(f"        addresses still on screen: {f['leaked']}")
            bad += 1
    print(f"\n{bad} screenshot(s) still show an address" if bad else
          "\nno real address reached any screenshot")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
