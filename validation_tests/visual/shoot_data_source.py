#!/usr/bin/env python3
"""Check what the header credits as this deployment's data source.

Why this exists
---------------
``DATA_SOURCE`` was the literal string "radarca.engr.colostate.edu" on the
AQPI profile. The AQPI deployment sets ``SENTINEL_BACKEND_ROOT``, so fs_products serves
every product manifest, every product frame and every X-band moment frame off
the main archive and radarca is left with the stream gauges. The navbar and the mobile
more-page credited radarca for all of it.

The backend now names the mount first when it reads one. This walks the three
places the string reaches a reader, with ``/api/version`` stubbed, because the
deployment under test has no mount and cannot produce the two-origin form on
its own. Holding each case to its own stub rather than to whichever deployment
happens to answer is the point: the condition is routed into existence.

Usage
-----
::

    python validation_tests/visual/shoot_data_source.py OUT_DIR [--base URL]
"""
from __future__ import annotations

import os
import argparse
import asyncio
import json
import sys
from dataclasses import dataclass
from pathlib import Path

try:
    from playwright.async_api import Page, Route, async_playwright
except ImportError:  # pragma: no cover - environment check, not a code path
    print("playwright is not installed: pip install playwright "
          "&& playwright install chromium", file=sys.stderr)
    raise SystemExit(2) from None

DEFAULT_BASE = os.environ.get("SENTINEL_VISUAL_BASE",
                              "http://localhost:5173")

# How long to let the page settle before reading the header. The source line
# arrives from /api/version, which the layout fetches on mount.
SETTLE_MS = 4000


@dataclass(frozen=True)
class Case:
    """One deployment shape and what its header must say."""

    name: str
    # None removes the key, standing in for a backend older than the field.
    source: str | None
    site: str
    # The exact text the navbar must carry, empty when it must say nothing.
    want: str


CASES = (
    Case("mount and http origin", "the main archive · radarca.engr.colostate.edu",
         "AQPI Sentinel", "the main archive · radarca.engr.colostate.edu"),
    Case("http origin only", "radarca.engr.colostate.edu",
         "AQPI Sentinel", "radarca.engr.colostate.edu"),
    Case("mount only", "the secondary archive · /projects/xqpi",
         "XQPI Sentinel", "the secondary archive · /projects/xqpi"),
    Case("backend predating the field", None, "AQPI Sentinel", ""),
)


async def stub_version(page: Page, case: Case) -> None:
    """Serve /api/version with this case's source, leaving the rest alone.

    The real payload carries the product table, the map extents and the
    overlay list, so it is fetched and amended rather than replaced. A
    hand-written payload would change what the page draws and the header
    would then be read off a console in a state no deployment produces.

    Args:
        page: The page to install the route on.
        case: Which deployment shape to serve.
    """
    async def handler(route: Route) -> None:
        upstream = await route.fetch()
        body = await upstream.json()
        body["site_name"] = case.site
        if case.source is None:
            body.pop("data_source", None)
        else:
            body["data_source"] = case.source
        await route.fulfill(status=200, content_type="application/json",
                            body=json.dumps(body))

    await page.route("**/api/version", handler)


async def header_text(page: Page, base: str, case: Case) -> dict[str, str]:
    """Load the desktop console and the mobile more-page, and read both lines.

    Args:
        page: A fresh page.
        base: The deployment's origin.
        case: Which deployment shape to stub.

    Returns:
        The navbar's source line and the more-page's about line.

    Raises:
        RuntimeError: When either element never appears.
    """
    await stub_version(page, case)
    await page.goto(f"{base}/", wait_until="domcontentloaded", timeout=30000)
    await page.wait_for_timeout(SETTLE_MS)
    # Absent when there is no source, because the separator goes with it, so
    # a missing element is the expected reading rather than a broken probe.
    nav = page.locator("[data-data-source]")
    desktop = ((await nav.inner_text()).strip() if await nav.count() else "")

    await page.goto(f"{base}/m/more", wait_until="domcontentloaded",
                    timeout=30000)
    await page.wait_for_timeout(SETTLE_MS)
    about = page.locator("[data-site-line]")
    if await about.count() == 0:
        raise RuntimeError("no about line on the more-page")
    return {"navbar": desktop, "more": (await about.inner_text()).strip()}


async def run(base: str) -> list[dict[str, object]]:
    """Walk every case and record what the header said.

    Args:
        base: The deployment's origin.

    Returns:
        One reading per case, each carrying both lines and its verdict.
    """
    readings: list[dict[str, object]] = []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        ctx = await browser.new_context(viewport={"width": 1400, "height": 900},
                                        ignore_https_errors=True)
        for case in CASES:
            page = await ctx.new_page()
            try:
                got = await header_text(page, base, case)
            finally:
                await page.close()
            ok_nav = got["navbar"] == case.want
            # The more-page joins the name and the source, and drops the
            # separator when there is no source to separate.
            want_more = (f"{case.site} · {case.want}" if case.want
                         else case.site)
            ok_more = got["more"] == want_more
            readings.append({"case": case.name, "want": case.want,
                             "navbar": got["navbar"], "more": got["more"],
                             "ok": ok_nav and ok_more})
            mark = "ok  " if ok_nav and ok_more else "FAIL"
            print(f"  {mark}  {case.name}")
            print(f"          navbar {got['navbar']!r}  (want {case.want!r})")
            print(f"          more   {got['more']!r}  (want {want_more!r})")
        await browser.close()
    return readings


def main() -> int:
    """Parse the arguments, run the cases and write the readings.

    Returns:
        The process exit code.
    """
    ap = argparse.ArgumentParser(description=__doc__)
    _ = ap.add_argument("out", type=Path, help="where to write the readings")
    _ = ap.add_argument("--base", default=DEFAULT_BASE, help="deployment origin")
    args = ap.parse_args()
    readings = asyncio.run(run(args.base))
    args.out.mkdir(parents=True, exist_ok=True)
    _ = (args.out / "data_source.json").write_text(
        json.dumps(readings, indent=2) + "\n")
    failures = [str(r["case"]) for r in readings if not r["ok"]]
    if failures:
        print(f"\n{len(failures)} FAILED: {', '.join(failures)}")
        return 1
    print(f"\nall {len(CASES)} data-source cases passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
