#!/usr/bin/env python3
"""Unlinking panes, linking them again, and the opacity label at 100%.

Why this exists
---------------
Joseph asked on 2026-11-22 for a control that lets panes move on their
own: no second cursor, and each map panned and zoomed separately. Linked
panes share one camera through the page, so unlinking has to stop both
the publishing and the following, and linking again has to gather the
panes on one view rather than on the last view they shared.

The settings menu showed "100%" under the radars slider's thumb on
2026-11-23. A range input keeps its intrinsic width in a flex row, so the
value label shrank instead. The last step measures the label against the
slider.

Usage
-----
::

    python validation_tests/visual/shoot_pane_link.py OUTDIR [--base URL]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

VISUAL = Path(__file__).resolve().parent
sys.path.insert(0, str(VISUAL))

try:
    from playwright.async_api import Page, async_playwright
except ImportError:  # pragma: no cover - environment check
    print("playwright is not installed: pip install playwright "
          "&& playwright install chromium", file=sys.stderr)
    raise SystemExit(2) from None

from shoot_compare import live_cameras, open_console  # noqa: E402  sibling module, loaded by path

DEFAULT_BASE = os.environ.get("SENTINEL_VISUAL_BASE", "http://localhost:5173")
SETTLE_MS = 2500
#: Degrees or zoom levels two cameras may differ by and still be one view.
SAME_VIEW = 1e-6


def same(a: list[float] | None, b: list[float] | None) -> bool:
    """Whether two [lon, lat, zoom] cameras are one view."""
    return (a is not None and b is not None
            and all(abs(x - y) < SAME_VIEW for x, y in zip(a, b, strict=True)))


async def drag_pane(page: Page, index: int, dx: float) -> None:
    """Drag one pane's map sideways from its middle.

    Args:
        page: The loaded console.
        index: The pane, 0 for A.
        dx: How far to drag, in CSS pixels.
    """
    box = await page.locator("[data-view]").nth(index).bounding_box()
    if not box:
        return
    x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
    await page.mouse.move(x, y)
    await page.mouse.down()
    await page.mouse.move(x + dx, y, steps=8)
    await page.mouse.up()
    await page.wait_for_timeout(SETTLE_MS)


async def follow_mark_in_b(page: Page) -> bool:
    """Hover pane A and report whether pane B draws the shared cursor.

    Args:
        page: The loaded console, two panes open.

    Returns:
        True when pane B shows a follow mark.
    """
    box = await page.locator("[data-view]").nth(0).bounding_box()
    if not box:
        return False
    await page.mouse.move(box["x"] + box["width"] * 0.4, box["y"] + box["height"] * 0.4)
    await page.wait_for_timeout(500)
    marks = page.locator("[data-view]").nth(1).locator("[data-cursor-follow]")
    count: int = await marks.count()
    return count > 0


async def check_link(page: Page, problems: list[str]) -> dict[str, Any]:
    """Linked, unlinked, and linked again.

    Args:
        page: The loaded console, one pane open.
        problems: Collects one message per defect.

    Returns:
        What each step saw.
    """
    seen: dict[str, Any] = {}
    if await page.locator("[data-pane-link]").count() != 0:
        problems.append("the link button shows with one pane")
    await page.locator("[data-pane-add]").click()
    await page.wait_for_timeout(6000)
    button = page.locator("[data-pane-link]")
    seen["start"] = await button.get_attribute("data-pane-link")
    # Lowered, since the toolbar's CSS draws its labels in capitals.
    seen["start_text"] = (await button.inner_text()).strip().lower()
    await drag_pane(page, 1, 160)
    cams = await live_cameras(page)
    seen["linked_follow"] = same(cams[0], cams[1])
    seen["linked_cursor"] = await follow_mark_in_b(page)

    await button.click()
    await page.wait_for_timeout(500)
    seen["after_unlink"] = await button.get_attribute("data-pane-link")
    seen["after_unlink_text"] = (await button.inner_text()).strip().lower()
    before = await live_cameras(page)
    await drag_pane(page, 1, -220)
    after = await live_cameras(page)
    seen["unlinked_a_still"] = same(before[0], after[0])
    seen["unlinked_b_moved"] = not same(before[1], after[1])
    seen["unlinked_cursor"] = await follow_mark_in_b(page)

    await button.click()
    await page.wait_for_timeout(SETTLE_MS)
    relinked = await live_cameras(page)
    seen["relinked_on_a"] = same(relinked[0], after[0]) and same(relinked[1], after[0])
    seen["relinked_cursor"] = await follow_mark_in_b(page)

    want = {"start": "linked", "start_text": "unlink panes", "linked_follow": True,
            "linked_cursor": True, "after_unlink": "unlinked",
            "after_unlink_text": "link panes", "unlinked_a_still": True,
            "unlinked_b_moved": True, "unlinked_cursor": False,
            "relinked_on_a": True, "relinked_cursor": True}
    for key, value in want.items():
        if seen.get(key) != value:
            problems.append(f"{key}: saw {seen.get(key)!r}, want {value!r}")
    await page.locator("[data-pane-remove]").click()
    await page.wait_for_timeout(1500)
    if await page.locator("[data-pane-link]").count() != 0:
        problems.append("the link button stayed after going back to one pane")
    return seen


async def check_slider_label(page: Page, outdir: Path,
                             problems: list[str]) -> dict[str, Any]:
    """Put the radars slider at 100% and measure its label.

    Args:
        page: The loaded console.
        outdir: Where the settings screenshot goes.
        problems: Collects one message per defect.

    Returns:
        The slider's right edge and the label's left edge and fit.
    """
    await page.locator("[data-settings-toggle]").click()
    await page.wait_for_timeout(500)
    slider = page.locator("[data-opacity=radars]")
    await slider.fill("100")
    await page.wait_for_timeout(300)
    got: dict[str, Any] = await slider.evaluate("""(el) => {
        const label = el.nextElementSibling;
        const s = el.getBoundingClientRect();
        const l = label.getBoundingClientRect();
        return {slider_right: s.right, label_left: l.left, text: label.textContent.trim(),
                fits: label.scrollWidth <= label.clientWidth + 1};
    }""")
    menu = page.locator("[data-settings-menu]")
    _ = await menu.screenshot(path=str(outdir / "settings_100.png"))
    if got["label_left"] < got["slider_right"]:
        problems.append(f"the label starts at {got['label_left']:.1f}px, inside the "
                        f"slider ending at {got['slider_right']:.1f}px")
    if not got["fits"] or got["text"] != "100%":
        problems.append(f"the label reads {got['text']!r} and fits={got['fits']}")
    return got


async def run(base: str, outdir: Path) -> dict[str, Any]:
    """Run both checks in one browser.

    Args:
        base: The deployment's origin.
        outdir: Where screenshots go.

    Returns:
        The report.
    """
    problems: list[str] = []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        try:
            page = await open_console(browser, base)
            link = await check_link(page, problems)
            slider = await check_slider_label(page, outdir, problems)
        finally:
            await browser.close()
    return {"base": base, "link": link, "slider": slider, "problems": problems}


def main() -> int:
    """Run the probe and report.

    Returns:
        0 when nothing was wrong, 1 otherwise.
    """
    ap = argparse.ArgumentParser(description="pane link and slider label probe")
    ap.add_argument("outdir", type=Path)
    ap.add_argument("--base", default=DEFAULT_BASE)
    args = ap.parse_args()
    outdir: Path = args.outdir
    outdir.mkdir(parents=True, exist_ok=True)
    report = asyncio.run(run(args.base, outdir))
    _ = (outdir / "pane_link.json").write_text(json.dumps(report, indent=1))
    print(json.dumps({k: report[k] for k in ("link", "slider")}, indent=1))
    for p in report["problems"]:
        print(f"  FAIL  {p}")
    print("all pane link steps passed" if not report["problems"]
          else f"{len(report['problems'])} FAILED")
    return 0 if not report["problems"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
