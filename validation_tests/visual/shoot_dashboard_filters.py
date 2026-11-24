#!/usr/bin/env python3
"""The dashboard's "hide RadarCA" and "hide NEXRAD" filters.

Why this exists
---------------
Joseph asked on 2026-11-23 for both filters on the dashboard. Each hides a set
of checks the backend classifies (`reads_radarca`, `nexrad` on /api/checks)
from every rail and from the alarm band, and remembers the choice.

The test deployment has no backend mount, so no check there watches a NEXRAD
radar. This probe flags every check targeting one X-band radar as NEXRAD in
the catalog it serves the page, which exercises the same code path on rows
that exist. It reads rendered text, since the rails carry no check ids.

The alarm band keeps the true open count in its header, so a filtered band
must say how many alarms the filters hold back, and must not call a filtered
alarm acknowledged when it has no rows left. Which alarms are open live is
luck, so the probe serves the page its own list.

Usage
-----
::

    python validation_tests/visual/shoot_dashboard_filters.py OUTDIR [--base URL]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

try:
    from playwright.async_api import Page, Route, async_playwright
except ImportError:  # pragma: no cover - environment check
    print("playwright is not installed: pip install playwright "
          "&& playwright install chromium", file=sys.stderr)
    raise SystemExit(2) from None

DEFAULT_BASE = os.environ.get("SENTINEL_VISUAL_BASE", "http://localhost:5173")
VIEWPORT = {"width": 1500, "height": 950}
LOAD_SETTLE_MS = 8_000
STEP_SETTLE_MS = 1_200
#: The radar this probe pretends is NEXRAD.
STAND_IN = "XEBY"
#: A check on the stand-in radar, and one neither filter touches.
STAND_IN_CHECK = "layer2.radar.XEBY"
NEUTRAL_CHECK = "layer0.net.dns"

BAND_JS = """() => {
  const b = document.querySelector('[data-alarm-band]');
  const t = b?.innerText ?? '';
  const num = (re) => Number((t.match(re) || [0, 0])[1]);
  return {open: num(/(\\d+) open/), rows: b?.querySelectorAll('[data-alarm-row]').length ?? -1,
          overflow: num(/\\+(\\d+) more not shown/), acked: num(/show acknowledged \\((\\d+)\\)/),
          filtered: Number(b?.querySelector('[data-alarms-filtered]')
                             ?.getAttribute('data-alarms-filtered') ?? 0),
          empty: b?.querySelector('[data-alarms-empty]')?.innerText ?? ''};
}"""


async def rails_text(page: Page) -> str:
    """The rendered text of every rail, without the X-band fleet banner.

    The banner names each radar that is not reporting, whatever the filters
    hide, so on a day the live fleet check fails it put the stand-in's name
    back in the text and failed the NEXRAD step for a reason the filter
    does not cover (seen 2026-11-24 on the dev stack).

    Args:
        page: The loaded dashboard.

    Returns:
        The rails' innerText with the banner's text taken out, or an empty
        string when the page has no rails.
    """
    got: str = await page.evaluate("""() => {
        const rails = document.querySelector('[data-rails]');
        if (!rails) return '';
        const banner = rails.querySelector('[data-fleet-banner]');
        const full = rails.innerText;
        return banner ? full.replace(banner.innerText, '') : full;
    }""")
    return got


async def stand_in_catalog(route: Route) -> None:
    """Serve the real check catalog with the stand-in radar's checks as NEXRAD."""
    resp = await route.fetch()
    rows: list[dict[str, Any]] = await resp.json()
    for r in rows:
        r["nexrad"] = r.get("target") == STAND_IN
    await route.fulfill(response=resp, json=rows)


def probe_alarm(alarm_id: int, check_id: str, target: str) -> dict[str, Any]:
    """One open, unacknowledged alarm in the shape /api/alarms returns."""
    opened = datetime.now(timezone.utc) - timedelta(hours=1)
    return {"id": alarm_id, "check_id": check_id, "target": target, "stage": "LB2",
            "severity": "warn", "opened_at": opened.isoformat(), "closed_at": None,
            "suppressed_by": None, "message": "probe alarm", "payload": None, "ack": None}


async def toggle(page: Page, attr: str) -> None:
    """Click one filter's checkbox and let the rails redraw."""
    await page.locator(f"[{attr}] input").first.click()
    await page.wait_for_timeout(STEP_SETTLE_MS)


async def reload(page: Page) -> None:
    """Reload the dashboard and let it settle."""
    _ = await page.reload(wait_until="load")
    await page.wait_for_timeout(LOAD_SETTLE_MS)


async def rail_steps(page: Page, outdir: Path) -> list[dict[str, Any]]:
    """Both filters on the rails, and that the choice survives a reload.

    Args:
        page: The dashboard, loaded with both filters off.
        outdir: Where the screenshot goes.

    Returns:
        One reading per step, with both filters off again at the end.
    """
    out: list[dict[str, Any]] = []
    before = await rails_text(page)
    out.append({"step": "both filters are offered", "want": [1, 1],
                "got": [await page.locator("[data-hide-radarca]").count(),
                        await page.locator("[data-hide-nexrad]").count()]})
    out.append({"step": f"{STAND_IN} is on the dashboard to start",
                "want": True, "got": STAND_IN in before})
    out.append({"step": "a RadarCA reading is on it to start", "want": True,
                "got": "Origin reachable" in before})
    await toggle(page, "data-hide-nexrad")
    after_nexrad = await rails_text(page)
    out.append({"step": f"hide NEXRAD removes {STAND_IN} and keeps the rest",
                "want": [False, True],
                "got": [STAND_IN in after_nexrad, "Origin reachable" in after_nexrad]})
    await toggle(page, "data-hide-radarca")
    after_both = await rails_text(page)
    _ = await page.screenshot(path=str(outdir / "dashboard_filtered.png"))
    out.append({"step": "hide RadarCA removes the RadarCA readings", "want": False,
                "got": "Origin reachable" in after_both})
    # The National Water Model feeds all read RadarCA and have no backend
    # half, so this filter empties their group. On prod (2026-11-24) the empty
    # group said "not exposed upstream", as if the products did not exist.
    empties: list[str] = await page.locator("[data-group-empty]").all_inner_texts()
    out.append({"step": "a product group the filter empties says it is filtered",
                "want": [True, True, False],
                "got": [len(empties) > 0,
                        all("hidden by the source filters" in e for e in empties),
                        "not exposed upstream" in after_both.lower()]})
    await reload(page)
    boxes = [await page.locator(f"[{a}] input").first.is_checked()
             for a in ("data-hide-radarca", "data-hide-nexrad")]
    out.append({"step": "both choices survive a reload", "want": [True, True], "got": boxes})
    await toggle(page, "data-hide-radarca")
    await toggle(page, "data-hide-nexrad")
    restored = await rails_text(page)
    out.append({"step": "turning both off brings everything back", "want": [True, True],
                "got": [STAND_IN in restored, "Origin reachable" in restored]})
    return out


def accounted(band: dict[str, Any]) -> bool:
    """Every open alarm in the header has a row, or a count that says where it went."""
    parts = band["rows"] + band["overflow"] + band["acked"] + band["filtered"]
    return bool(parts == band["open"])


async def band_steps(page: Page, served: list[dict[str, Any]],
                     outdir: Path) -> list[dict[str, Any]]:
    """The alarm band under hide NEXRAD, over an alarm list the probe serves.

    Args:
        page: The dashboard, with both filters off.
        served: The list the page's alarm fetch returns; this fills it.
        outdir: Where the screenshot goes.

    Returns:
        One reading per step, with both filters off again at the end.
    """
    out: list[dict[str, Any]] = []
    served[:] = [probe_alarm(-1, STAND_IN_CHECK, STAND_IN),
                 probe_alarm(-2, NEUTRAL_CHECK, "")]
    await reload(page)
    band = await page.evaluate(BAND_JS)
    out.append({"step": "unfiltered, both probe alarms have rows",
                "want": [2, 2, 0], "got": [band["open"], band["rows"], band["filtered"]]})
    await toggle(page, "data-hide-nexrad")
    band = await page.evaluate(BAND_JS)
    out.append({"step": "hide NEXRAD keeps the count, drops the row, and says so",
                "want": [2, 1, 1], "got": [band["open"], band["rows"], band["filtered"]]})
    out.append({"step": "every open alarm is a row or a stated count", "want": True,
                "got": accounted(band)})
    served[:] = served[:1]
    await reload(page)
    band = await page.evaluate(BAND_JS)
    _ = await page.screenshot(path=str(outdir / "alarm_band_all_filtered.png"))
    out.append({"step": "with every alarm filtered, the band says filtered, not acked",
                "want": [0, True, False],
                "got": [band["rows"], "hidden by the source filters" in band["empty"],
                        "acknowledged" in band["empty"]]})
    await toggle(page, "data-hide-nexrad")
    return out


async def run(base: str, outdir: Path) -> list[dict[str, Any]]:
    """Every step, in one browser.

    Args:
        base: The deployment's origin.
        outdir: Where the screenshots go.

    Returns:
        One reading per step.
    """
    served: list[dict[str, Any]] = []

    async def serve_alarms(route: Route) -> None:
        if served:
            await route.fulfill(json=served)
        else:
            await route.continue_()

    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        try:
            page = await (await browser.new_context(viewport=VIEWPORT)).new_page()
            await page.add_init_script(
                "if (!sessionStorage.getItem('probe-seeded')) {"
                " sessionStorage.setItem('probe-seeded', '1'); localStorage.clear();"
                " localStorage.setItem('sentinel-theme','dark'); }")
            await page.route("**/api/checks", stand_in_catalog)
            await page.route(lambda u: "/api/alarms?" in u and "status=open" in u, serve_alarms)
            catalog = await (await page.request.get(f"{base}/api/checks")).json()
            ids = {r.get("id") for r in catalog}
            out: list[dict[str, Any]] = [{"step": "the probe's check ids are in the catalog",
                                          "want": [True, True],
                                          "got": [STAND_IN_CHECK in ids, NEUTRAL_CHECK in ids]}]
            _ = await page.goto(f"{base}/dashboard", wait_until="load", timeout=60_000)
            await page.wait_for_timeout(LOAD_SETTLE_MS)
            out += await rail_steps(page, outdir)
            out += await band_steps(page, served, outdir)
            errs: list[str] = await page.evaluate("() => window.__errs || []")
            out.append({"step": "page errors", "want": [], "got": errs})
        finally:
            await browser.close()
    return out


def main() -> int:
    """Run the probe and report.

    Returns:
        0 when every step read what it should, 1 otherwise.
    """
    ap = argparse.ArgumentParser(description="dashboard source filter probe")
    ap.add_argument("outdir", type=Path)
    ap.add_argument("--base", default=DEFAULT_BASE)
    args = ap.parse_args()
    outdir: Path = args.outdir
    outdir.mkdir(parents=True, exist_ok=True)
    steps = asyncio.run(run(args.base, outdir))
    _ = (outdir / "dashboard_filters.json").write_text(json.dumps(steps, indent=1))
    bad = 0
    for s in steps:
        ok = s["got"] == s["want"]
        bad += 0 if ok else 1
        print(f"  {'ok  ' if ok else 'FAIL'}  {s['step']}: {s['got']!r}"
              + ("" if ok else f" (want {s['want']!r})"))
    print("all dashboard filter steps passed" if bad == 0 else f"{bad} FAILED")
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
