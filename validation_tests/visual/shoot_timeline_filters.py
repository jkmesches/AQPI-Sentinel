#!/usr/bin/env python3
"""The timeline's RadarCA filter and the detail panel's failing filter.

Why this exists
---------------
Joseph asked on 2026-11-23 for two filters. "hide RadarCA" drops every check
whose readings come from radarca, which the backend decides per check,
because L4 reads the mount on one deployment and radarca on another.
"show failing" narrows the runs in a cell's detail panel to warn, fail and
error.

Each is checked against the deployment's own /api/checks and run statuses
rather than against counts written here, so the probe holds on a deployment
with or without a backend tier:

1. With the filter on, no row on screen is a check the API says reads
   radarca, and every check that does not read radarca is still there.
2. The choice survives a reload.
3. Clearing it brings every row back.
4. "show failing" leaves only warn, fail and error runs, and clearing it
   restores the full list.

Joseph added "hide NEXRAD" beside it the same day. The test deployment
watches no NEXRAD radar, so where its own catalog flags none the box must
not be offered, and the NEXRAD steps run against a catalog that flags every
XEBY check as NEXRAD, the stand-in the dashboard probe uses:

5. With it on, no stand-in row is on the Radars tab and every other row
   that was there still is.
6. The choice survives a reload.
7. Clearing it brings every row back.

Usage
-----
::

    python validation_tests/visual/shoot_timeline_filters.py OUTDIR [--base URL]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

try:
    from playwright.async_api import Browser, Page, Route, async_playwright
except ImportError:  # pragma: no cover - environment check
    print("playwright is not installed: pip install playwright "
          "&& playwright install chromium", file=sys.stderr)
    raise SystemExit(2) from None

DEFAULT_BASE = os.environ.get("SENTINEL_VISUAL_BASE", "http://localhost:5173")
VIEWPORT = {"width": 1600, "height": 1000}
SETTLE_MS = 4000
BAD = {"warn", "fail", "error"}
#: The Website tab's stages, as TABS in the timeline page lists them.
WEBSITE_STAGES = {"L0"}
RADARS_STAGES = {"L2", "LB2", "LB3", "L3", "L4-T1T2"}
#: The radar this probe pretends is NEXRAD, since the test deployment
#: watches none.
STAND_IN = "XEBY"
CHECK_ID_RE = re.compile(r"Check ID:\s+(\S+)")
#: How many bad cells step 4 opens looking for one with mixed runs.
MAX_CELLS_TRIED = 12


async def row_ids(page: Page) -> set[str]:
    """The check ids of every row label on screen."""
    titles: list[str] = await page.eval_on_selector_all(
        "div.tl-cell.sticky[title]", "els => els.map(e => e.title)")
    return {m.group(1) for t in titles if (m := CHECK_ID_RE.search(t))}


async def open_timeline(page: Page, base: str, tab: str = "website") -> None:
    """Load the timeline on one tab and wait for the grid."""
    await page.goto(f"{base}/timeline", wait_until="load", timeout=60_000)
    await page.wait_for_timeout(SETTLE_MS)
    await page.get_by_role("button", name=re.compile(tab, re.I)).first.click()
    await page.wait_for_timeout(1000)


async def stand_in_catalog(route: Route) -> None:
    """Serve the real check catalog with the stand-in radar's checks as NEXRAD."""
    resp = await route.fetch()
    rows: list[dict[str, Any]] = await resp.json()
    for r in rows:
        r["nexrad"] = r.get("target") == STAND_IN
    await route.fulfill(response=resp, json=rows)


async def check_nexrad(page: Page, base: str, checks: list[dict[str, Any]],
                       problems: list[str]) -> dict[str, Any]:
    """Steps 5 to 7, on the Radars tab with the stand-in flagged as NEXRAD.

    Args:
        page: A fresh page, with the stand-in catalog routed.
        base: The deployment's origin.
        checks: /api/checks as the deployment serves it, unrouted.
        problems: Collects one message per defect.

    Returns:
        What each step saw.
    """
    nexrad = {c["id"] for c in checks if c.get("target") == STAND_IN}
    keep = {c["id"] for c in checks
            if c["stage"] in RADARS_STAGES and c.get("target") != STAND_IN}
    await open_timeline(page, base, "radars")
    before = await row_ids(page)
    if not await page.locator("[data-hide-nexrad]").count():
        problems.append("hide NEXRAD is not offered, though the catalog flags "
                        f"{len(nexrad)} checks as NEXRAD")
        return {"before": len(before), "offered": False}
    await page.locator("[data-hide-nexrad] input").check()
    await page.wait_for_timeout(800)
    hidden = await row_ids(page)
    await page.reload(wait_until="load")
    await page.wait_for_timeout(SETTLE_MS)
    await page.get_by_role("button", name=re.compile("radars", re.I)).first.click()
    await page.wait_for_timeout(1000)
    kept_on = await page.locator("[data-hide-nexrad] input").is_checked()
    after_reload = await row_ids(page)
    await page.locator("[data-hide-nexrad] input").uncheck()
    await page.wait_for_timeout(800)
    restored = await row_ids(page)
    if hidden & nexrad:
        problems.append(f"NEXRAD rows stayed: {sorted(hidden & nexrad)}")
    if (keep & before) - hidden:
        problems.append(f"rows that are not NEXRAD vanished: {sorted((keep & before) - hidden)}")
    if not kept_on or after_reload != hidden:
        problems.append("the NEXRAD filter did not survive a reload")
    if restored != before:
        problems.append("clearing the NEXRAD filter did not bring every row back")
    if not before & nexrad:
        problems.append("the Radars tab showed no stand-in rows to begin with, so "
                        "step 5 tested nothing")
    return {"before": len(before), "hidden": len(hidden), "kept_on": kept_on,
            "restored": len(restored), "nexrad_on_tab": len(before & nexrad)}


async def check_radarca(page: Page, base: str, checks: list[dict[str, Any]],
                        problems: list[str]) -> dict[str, Any]:
    """Steps 1 to 3.

    Args:
        page: A fresh page.
        base: The deployment's origin.
        checks: /api/checks as the deployment serves it.
        problems: Collects one message per defect.

    Returns:
        What each step saw.
    """
    radarca = {c["id"] for c in checks if c.get("reads_radarca") is True}
    keep = {c["id"] for c in checks
            if c["stage"] in WEBSITE_STAGES and c.get("reads_radarca") is False}
    await open_timeline(page, base)
    before = await row_ids(page)
    await page.locator("[data-hide-radarca] input").check()
    await page.wait_for_timeout(800)
    hidden = await row_ids(page)
    await page.reload(wait_until="load")
    await page.wait_for_timeout(SETTLE_MS)
    await page.get_by_role("button", name=re.compile("website", re.I)).first.click()
    await page.wait_for_timeout(1000)
    kept_on = await page.locator("[data-hide-radarca] input").is_checked()
    after_reload = await row_ids(page)
    await page.locator("[data-hide-radarca] input").uncheck()
    await page.wait_for_timeout(800)
    restored = await row_ids(page)
    if hidden & radarca:
        problems.append(f"rows that read radarca stayed: {sorted(hidden & radarca)}")
    if keep - hidden:
        problems.append(f"rows that do not read radarca vanished: {sorted(keep - hidden)}")
    if not kept_on or after_reload != hidden:
        problems.append("the filter did not survive a reload")
    if restored != before:
        problems.append("clearing the filter did not bring every row back")
    if not before & radarca:
        problems.append("the tab showed no radarca rows to begin with, so step 1 "
                        "tested nothing")
    return {"before": len(before), "hidden": len(hidden), "kept_on": kept_on,
            "restored": len(restored), "radarca_on_tab": len(before & radarca)}


async def check_failing(page: Page, problems: list[str]) -> dict[str, Any]:
    """Step 4, on the first cell whose bucket had a bad run.

    Args:
        page: The loaded timeline.
        problems: Collects one message per defect.

    Returns:
        What the panel showed with and without the filter.
    """
    # An hour holds dozens of runs of a one-minute check, so a bad bucket
    # there usually mixes in the passing runs the filter has to remove.
    await page.get_by_role("button", name="1 hour", exact=True).first.click()
    await page.wait_for_timeout(SETTLE_MS)
    cell = page.locator(
        "[data-cell-status=warn], [data-cell-status=fail], [data-cell-status=error]")
    every: list[str] = []
    close = page.locator('button[aria-label="close"]')
    for i in range(min(await cell.count(), MAX_CELLS_TRIED)):
        # The previous cell's panel covers the grid with a backdrop, which
        # took every click after the first whenever the first cell's runs
        # were not mixed.
        if await close.count():
            await close.first.click()
            await page.wait_for_timeout(300)
        await cell.nth(i).click()
        await page.wait_for_timeout(2500)
        every = await page.eval_on_selector_all(
            "[data-run-status]", "els => els.map(e => e.dataset.runStatus)")
        if any(s in BAD for s in every) and any(s not in BAD for s in every):
            break
    else:
        problems.append("no bad bucket mixed passing runs in, so step 4 "
                        "tested nothing")
        return {"every": every}
    await page.locator("[data-failing-only] input").check()
    await page.wait_for_timeout(500)
    bad: list[str] = await page.eval_on_selector_all(
        "[data-run-status]", "els => els.map(e => e.dataset.runStatus)")
    await page.locator("[data-failing-only] input").uncheck()
    await page.wait_for_timeout(500)
    back: list[str] = await page.eval_on_selector_all(
        "[data-run-status]", "els => els.map(e => e.dataset.runStatus)")
    if any(s not in BAD for s in bad):
        problems.append(f"show failing left {sorted(set(bad) - BAD)} runs")
    if len(bad) != sum(1 for s in every if s in BAD):
        problems.append(f"show failing dropped bad runs: {len(bad)} of "
                        f"{sum(1 for s in every if s in BAD)}")
    if back != every:
        problems.append("clearing show failing did not restore the list")
    return {"every": every, "failing": bad}


async def run(base: str) -> dict[str, Any]:
    """Run every step in one browser.

    Args:
        base: The deployment's origin.

    Returns:
        The report, with a list of problems.
    """
    problems: list[str] = []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        try:
            page = await fresh_page(browser)
            resp = await page.request.get(f"{base}/api/checks")
            checks: list[dict[str, Any]] = await resp.json()
            radarca = await check_radarca(page, base, checks, problems)
            # The deployment's own catalog: where no check is NEXRAD the box
            # is not offered at all.
            offered = await page.locator("[data-hide-nexrad]").count()
            if not any(c.get("nexrad") is True for c in checks) and offered:
                problems.append("hide NEXRAD is offered where no check is NEXRAD")
            failing = await check_failing(page, problems)
            errs: list[str] = await page.evaluate("()=>window.__errs||[]")
            # Its own context, so the routed catalog cannot reach the steps above.
            stand_in = await fresh_page(browser)
            await stand_in.route("**/api/checks", stand_in_catalog)
            nexrad = await check_nexrad(stand_in, base, checks, problems)
            errs += await stand_in.evaluate("()=>window.__errs||[]")
        finally:
            await browser.close()
    return {"base": base, "radarca": radarca, "failing": failing, "nexrad": nexrad,
            "nexrad_offered_unrouted": offered, "errors": errs, "problems": problems}


async def fresh_page(browser: Browser) -> Page:
    """A page in its own context, recording errors, with storage cleared once.

    Args:
        browser: The running chromium.

    Returns:
        The new page.
    """
    ctx = await browser.new_context(viewport=VIEWPORT)
    page = await ctx.new_page()
    await page.add_init_script(
        "window.__errs=[];"
        "window.addEventListener('error',"
        "e=>window.__errs.push(String(e.message)));")
    await page.add_init_script(
        "if (!sessionStorage.getItem('probe-cleared')) {"
        " localStorage.clear(); sessionStorage.setItem('probe-cleared','1'); }")
    return page


def main() -> int:
    """Run the probe and report.

    Returns:
        0 when nothing was wrong, 1 otherwise.
    """
    ap = argparse.ArgumentParser(description="timeline filter probe")
    ap.add_argument("outdir", type=Path)
    ap.add_argument("--base", default=DEFAULT_BASE)
    args = ap.parse_args()
    outdir: Path = args.outdir
    outdir.mkdir(parents=True, exist_ok=True)
    report = asyncio.run(run(args.base))
    _ = (outdir / "timeline_filters.json").write_text(json.dumps(report, indent=1))
    print(json.dumps({k: report[k] for k in ("radarca", "failing", "nexrad",
                                             "nexrad_offered_unrouted")}, indent=1))
    for p in report["problems"]:
        print(f"  FAIL  {p}")
    print("all timeline filter steps passed" if not report["problems"]
          else f"{len(report['problems'])} FAILED")
    return 0 if not report["problems"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
