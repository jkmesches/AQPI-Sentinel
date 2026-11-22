#!/usr/bin/env python3
"""Render the dashboard against a mounted deployment's payload.

Why this exists
---------------
The test deployment has no backend mount, so three of the dashboard's layouts
never run on it: the paired product rail with its the main archive and RC columns, radar
cards carrying several checks instead of one, and the composite-input and
fleet rows that only exist when LB1/LB2/LB3 are registered. the AQPI deployment has all
of them, and it runs 84 checks across eight stages where the test deployment
runs 45 across five.

Deploying to find out is the wrong order. This serves the AQPI deployment's own
``/api/status``, ``/api/checks`` and ``/api/version`` to the test
deployment's frontend, so the rendering code meets production-shaped data
before production meets the rendering code.

The payloads are captured separately and passed in, because this host cannot
reach the AQPI deployment without the VPN helper and a test that needs a tunnel is a
test nobody runs.

Usage
-----
::

    python validation_tests/visual/shoot_dashboard_paired.py OUT_DIR \\
        --status prod_status.json --checks prod_checks.json \\
        --version prod_version.json [--base URL]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from collections.abc import Awaitable, Callable
from typing import Any

try:
    from playwright.async_api import Page, Route, async_playwright
except ImportError:  # pragma: no cover - environment check, not a code path
    print("playwright is not installed: pip install playwright "
          "&& playwright install chromium", file=sys.stderr)
    raise SystemExit(2) from None

DEFAULT_BASE = "https://the test deployment"

# The dashboard paints from the REST snapshot and then takes live updates over
# the socket. Long enough for the snapshot, the check catalog and the first
# metric batch to land.
SETTLE_MS = 12000


async def serve_captures(page: Page, payloads: dict[str, Any]) -> None:
    """Answer the dashboard's three snapshot calls from the captured files.

    The metric routes are not intercepted, so the sparklines and readouts
    draw the test deployment's own real series. A row whose check exists only
    on the mounted deployment then has no series at all, and drawing that
    empty case is part of what this is for.

    Args:
        page: The page to install the routes on.
        payloads: Keyed by route name, the JSON to return.
    """
    def answer(name: str) -> Callable[[Route], Awaitable[None]]:
        async def handler(route: Route) -> None:
            await route.fulfill(status=200, content_type="application/json",
                                body=json.dumps(payloads[name]))
        return handler

    await page.route("**/api/status", answer("status"))
    await page.route("**/api/checks", answer("checks"))
    await page.route("**/api/version", answer("version"))


def summarize(payload: dict[str, Any]) -> str:
    """One line naming what the captured snapshot holds.

    Args:
        payload: The captured /api/status body.

    Returns:
        A stage-by-stage row count.
    """
    stages = payload.get("stages", {})
    parts = [f"{k}={len(v)}" for k, v in sorted(stages.items())]
    return f"{sum(len(v) for v in stages.values())} checks · " + " ".join(parts)


async def run(out: Path, base: str, payloads: dict[str, Any],
              widths: tuple[int, ...]) -> list[dict[str, object]]:
    """Render the dashboard at each width and report what it drew.

    Args:
        out: Where the screenshots go.
        base: The deployment serving the frontend.
        payloads: The captured snapshots.
        widths: Viewport widths to render at.

    Returns:
        One reading per width.
    """
    findings: list[dict[str, object]] = []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        for width in widths:
            ctx = await browser.new_context(
                viewport={"width": width, "height": 1080},
                ignore_https_errors=True)
            page = await ctx.new_page()
            errors: list[str] = []
            # Bound as a default, not captured: the handler outlives this
            # iteration and would otherwise append to whichever list the last
            # loop pass happened to leave behind.
            page.on("pageerror",
                    lambda e, sink=errors: sink.append(str(e)[:160]))
            await serve_captures(page, payloads)
            await page.goto(f"{base}/dashboard", wait_until="domcontentloaded",
                            timeout=45000)
            await page.wait_for_timeout(SETTLE_MS)
            shot = out / f"paired_{width}.png"
            await page.screenshot(path=str(shot))
            findings.append({
                "width": width,
                # The paired rail is the layout under test, and its column
                # header is the one element that exists only in that branch.
                "paired_header": await page.get_by_text("the main archive / the secondary archive").count(),
                "radar_cards": await page.locator("button[aria-expanded]").count(),
                "dash_cells": await page.get_by_text("—", exact=True).count(),
                "overflow": await page.evaluate(
                    "document.documentElement.scrollWidth") > width,
                "errors": errors,
                "shot": str(shot),
            })
            await ctx.close()
        await browser.close()
    return findings


def main() -> int:
    """Load the captures, render, and print what each width drew.

    Returns:
        0 when every width rendered the paired layout without errors.
    """
    ap = argparse.ArgumentParser(description=__doc__)
    _ = ap.add_argument("out", type=Path)
    _ = ap.add_argument("--status", type=Path, required=True)
    _ = ap.add_argument("--checks", type=Path, required=True)
    _ = ap.add_argument("--version", type=Path, required=True)
    _ = ap.add_argument("--base", default=DEFAULT_BASE)
    _ = ap.add_argument("--widths", default="1920,1366")
    args = ap.parse_args()

    payloads = {
        "status": json.loads(args.status.read_text()),
        "checks": json.loads(args.checks.read_text()),
        "version": json.loads(args.version.read_text()),
    }
    print(f"serving: {summarize(payloads['status'])}")
    widths = tuple(int(w) for w in str(args.widths).split(","))
    args.out.mkdir(parents=True, exist_ok=True)
    found = asyncio.run(run(args.out, args.base, payloads, widths))

    bad = 0
    for f in found:
        ok = bool(f["paired_header"]) and not f["errors"] and not f["overflow"]
        print(f"  {'ok  ' if ok else 'FAIL'}  {f['width']}px: "
              f"paired rail={bool(f['paired_header'])} "
              f"radar cards={f['radar_cards']} "
              f"empty cells={f['dash_cells']} "
              f"overflow={f['overflow']}")
        if f["errors"]:
            print(f"        page errors: {f['errors']}")
        print(f"        {f['shot']}")
        if not ok:
            bad += 1
    print(f"\n{bad} width(s) failed" if bad else
          "\nthe paired layout rendered at every width")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
