#!/usr/bin/env python3
"""The keyboard has to move the console, and only once per press.

The assertions in ``validation_tests/js/test_map_keys.mjs`` cover the mapping
from a key to an act. They cannot see whether the act is wired to anything,
whether the page takes the key from the browser, or whether something else on
the page answers the same press. Every slice of this console has had a defect
of exactly that shape, so this drives real presses at the deployed page.

Three of these checks are the reason it exists.

A bare arrow must step time and not pan. MapLibre's keyboard handler pans with
the arrows whenever its canvas has focus, which it has as soon as anyone
clicks the map, so a shared arrow would do both.

A shifted arrow must pan and not step. That is what replaces the handler the
console turns off.

Space with the play button focused must toggle playback once. The button
answers space itself, so a console that also answered it would toggle twice
and read as a dead key.

Usage
-----
::

    python validation_tests/visual/shoot_keys.py OUTDIR [--base URL]
"""
from __future__ import annotations

import os
import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

VISUAL = Path(__file__).resolve().parent
sys.path.insert(0, str(VISUAL))

try:
    from playwright.async_api import Browser, Page, async_playwright
    from playwright.async_api import TimeoutError as PlaywrightTimeout
except ImportError:  # pragma: no cover - environment check
    print("playwright is not installed: pip install playwright "
          "&& playwright install chromium", file=sys.stderr)
    raise SystemExit(2) from None

from capture import open_dock  # noqa: E402  sibling module, loaded by path

DEFAULT_BASE = os.environ.get("SENTINEL_VISUAL_BASE",
                              "http://localhost:5173")

# The console polls every 5 s and holds a websocket, so networkidle never
# settles. Same waits capture.py measured.
LOAD_SETTLE_MS = 6000
KEY_SETTLE_MS = 1200
PANE_SETTLE_MS = 6000

# How much longer to give the map's style after the settle. The style
# comes from an outside tile host, and 20 s past a 6 s settle separates a
# slow host from one that is not answering.
MAP_READY_MS = 20_000


class MapNeverLoadedError(RuntimeError):
    """The console loaded without a map, so no key check means anything."""

VIEWPORT = {"width": 1600, "height": 950}

# How far apart two cameras may sit and still count as unmoved. The published
# longitude is rounded to five places, about a meter.
CAMERA_EPS = 2e-5


async def state(page: Page) -> dict[str, Any]:
    """Read everything a key press could have changed.

    Args:
        page: The loaded console.

    Returns:
        The time strip, the first pane's view, the panel and the help panel.
    """
    js = """
      () => {
        const strip = document.querySelector('[data-time-strip]');
        const view = document.querySelector('[data-view]');
        const panel = document.querySelector('[data-panel]');
        return {
          strip: strip ? JSON.parse(strip.getAttribute('data-time-strip')) : null,
          view: view ? JSON.parse(view.getAttribute('data-view')) : null,
          panel: panel ? panel.getAttribute('data-panel') : null,
          help: document.querySelectorAll('[data-help-panel]').length,
          panes: document.querySelectorAll('[data-view]').length,
          scrollY: window.scrollY
        };
      }
    """
    got: dict[str, Any] = await page.evaluate(js)
    return got


async def blur(page: Page) -> None:
    """Drop focus, so the next press lands on the page rather than a control.

    `locator("body").press()` does not move focus off a focusable element, so
    without this every press after the play-button check arrived with that
    button as its target. That is how the harness found the real defect, and
    it is also why the plain checks need a clean starting point.

    Args:
        page: The loaded console.
    """
    await page.evaluate(
        "() => { const a = document.activeElement;"
        " if (a && a !== document.body && a.blur) a.blur(); }")


async def tap(page: Page, key: str, settle: int = KEY_SETTLE_MS) -> None:
    """Press a key with nothing focused, and let the console react.

    Args:
        page: The loaded console.
        key: A Playwright key name.
        settle: How long to wait afterwards.
    """
    await blur(page)
    await page.locator("body").press(key)
    await page.wait_for_timeout(settle)


async def open_console(browser: Browser, base: str) -> Page:
    """Load the console on a known product with no stored prefs.

    Args:
        browser: The running chromium.
        base: The deployment's origin.

    Returns:
        The loaded page.

    Raises:
        PlaywrightTimeout: If the console does not load.
    """
    ctx = await browser.new_context(viewport=VIEWPORT, device_scale_factor=1)
    page = await ctx.new_page()
    await page.add_init_script(
        "localStorage.setItem('sentinel-theme', 'dark');"
        "localStorage.setItem('sentinel-map-prefs', JSON.stringify("
        "{activeRadars:[], moment:'Reflectivity', composite:'comp_ref',"
        " panelOpen:true}));")
    await page.goto(base, wait_until="load", timeout=45_000)
    await page.wait_for_timeout(LOAD_SETTLE_MS)
    # The map's style comes from an outside tile host, and on 2026-11-21
    # a run found it had not arrived: the canvas was black, no camera was
    # published, the strip held no frames, and every key check failed on
    # a console that was never there to take a key. Waiting for the
    # camera says that in one line instead of six.
    try:
        await page.wait_for_function(
            "() => { const el = document.querySelector('[data-view]');"
            " return el && JSON.parse(el.getAttribute('data-view')).camera; }",
            timeout=MAP_READY_MS)
    except PlaywrightTimeout:
        raise MapNeverLoadedError(
            f"no pane published a camera {LOAD_SETTLE_MS + MAP_READY_MS} ms "
            f"after load; the map's style never arrived, so nothing here "
            f"is about the keys") from None
    return page


def zoom_of(reading: dict[str, Any]) -> float | None:
    """The zoom the first pane published, if it published a camera.

    Args:
        reading: A state reading.

    Returns:
        The zoom, or None before the pane has a camera to report.
    """
    camera = (reading.get("view") or {}).get("camera")
    if not camera:
        return None
    return float(camera[2])


def camera_moved(before: dict[str, Any], after: dict[str, Any]) -> bool:
    """Whether the published camera changed.

    Args:
        before: A state reading.
        after: A later state reading.

    Returns:
        True when any of longitude, latitude or zoom moved.
    """
    a = (before.get("view") or {}).get("camera")
    b = (after.get("view") or {}).get("camera")
    if a is None or b is None:
        return False
    return any(abs(x - y) > CAMERA_EPS for x, y in zip(a, b, strict=True))


async def check_time_keys(page: Page, problems: list[str]) -> dict[str, Any]:
    """Step, jump and play from the keyboard.

    Args:
        page: The loaded console.
        problems: Collects failure messages.

    Returns:
        The state after the checks, for the caller to carry on from.
    """
    start = await state(page)
    if not start["strip"] or start["strip"]["totalSteps"] < 3:
        problems.append(
            f"the strip reports {start['strip']} so the time keys cannot be "
            f"checked; this needs a product with steps")
        return start

    await tap(page, "End")
    live = await state(page)
    last = live["strip"]["totalSteps"] - 1
    if live["strip"]["stepIdx"] != last:
        problems.append(f"End left the step at {live['strip']['stepIdx']}, "
                        f"not the live frame {last}")

    await tap(page, "ArrowLeft")
    back = await state(page)
    if back["strip"]["stepIdx"] != last - 1:
        problems.append(f"a bare left arrow moved the step from {last} to "
                        f"{back['strip']['stepIdx']}, expected {last - 1}")
    if camera_moved(live, back):
        problems.append(
            "a bare left arrow panned the map as well as stepping; MapLibre's "
            "keyboard handler is still on and one press does two things")

    await tap(page, "ArrowRight")
    fwd = await state(page)
    if fwd["strip"]["stepIdx"] != last:
        problems.append(f"a right arrow moved the step to "
                        f"{fwd['strip']['stepIdx']}, expected {last}")

    await tap(page, "Home")
    first = await state(page)
    if first["strip"]["stepIdx"] != 0:
        problems.append(f"Home left the step at {first['strip']['stepIdx']}, "
                        f"not the oldest frame")
    if first["scrollY"] != 0:
        problems.append(
            f"Home scrolled the page to {first['scrollY']}; the handled keys "
            f"have to be taken from the browser")
    return first


async def check_play_key(page: Page, problems: list[str]) -> None:
    """Space plays, and plays exactly once with a button focused.

    Args:
        page: The loaded console.
        problems: Collects failure messages.
    """
    was = (await state(page))["strip"]["playing"]
    await tap(page, "Space")
    now = (await state(page))["strip"]["playing"]
    if now == was:
        problems.append(f"space did not change playback, still {was}")
    await tap(page, "Space")
    if (await state(page))["strip"]["playing"] != was:
        problems.append("space did not toggle playback back")

    # With the play button focused the button answers space itself. A console
    # that also answers it toggles twice, which looks like a dead key.
    play = page.locator('[data-time-controls] button[aria-label="play"]')
    if await play.count() == 0:
        problems.append("no play button to focus; the double-fire check is "
                        "the reason this harness exists")
        return
    await play.first.focus()
    before = (await state(page))["strip"]["playing"]
    await page.keyboard.press("Space")
    await page.wait_for_timeout(KEY_SETTLE_MS)
    after = (await state(page))["strip"]["playing"]
    if after == before:
        problems.append(
            f"space with the play button focused left playback at {before}; "
            f"either nothing answered it or two things did and cancelled")
    await page.keyboard.press("Space")
    await page.wait_for_timeout(KEY_SETTLE_MS)

    # And the keys that button does NOT handle must still reach the console.
    # The first version of this treated any focused control as owning the
    # whole keyboard, so clicking play left the arrows dead.
    await play.first.focus()
    held = await state(page)
    await page.keyboard.press("ArrowLeft")
    await page.wait_for_timeout(KEY_SETTLE_MS)
    stepped = await state(page)
    if stepped["strip"]["stepIdx"] == held["strip"]["stepIdx"]:
        problems.append(
            f"an arrow with the play button focused did not step time, still "
            f"{held['strip']['stepIdx']}; a button does not own an arrow")


async def check_pan_and_zoom(page: Page, problems: list[str]) -> None:
    """A shifted arrow pans, and the zoom keys zoom.

    Args:
        page: The loaded console.
        problems: Collects failure messages.
    """
    before = await state(page)
    await tap(page, "Shift+ArrowLeft")
    after = await state(page)
    if not camera_moved(before, after):
        problems.append(
            "shift and a left arrow did not move the camera; turning "
            "MapLibre's handler off has taken keyboard panning away")
    if after["strip"]["stepIdx"] != before["strip"]["stepIdx"]:
        problems.append(
            f"shift and a left arrow also stepped time, "
            f"{before['strip']['stepIdx']} to {after['strip']['stepIdx']}")

    zoom_before = zoom_of(after)
    await tap(page, "+")
    zoomed = await state(page)
    zoom_after = zoom_of(zoomed)
    if zoom_before is None or zoom_after is None:
        problems.append(
            f"the pane published no camera, {zoom_before} then {zoom_after}; "
            f"the zoom keys cannot be checked")
        return
    if zoom_after <= zoom_before:
        problems.append(f"+ did not zoom in, {zoom_before} to {zoom_after}")
    await tap(page, "-")
    out = zoom_of(await state(page))
    if out is None or out >= zoom_after:
        problems.append(f"- did not zoom out, {zoom_after} to {out}")


async def check_product_and_panel(page: Page, problems: list[str]) -> None:
    """The product steps, the panel toggles, and a focused select keeps its keys.

    Args:
        page: The loaded console.
        problems: Collects failure messages.
    """
    start = await state(page)
    first_product = start["view"]["product"]
    await tap(page, "ArrowDown")
    nxt = await state(page)
    if nxt["view"]["product"] == first_product:
        problems.append(
            f"a down arrow did not change the product, still {first_product!r}")
    await tap(page, "ArrowUp")
    back = await state(page)
    if back["view"]["product"] != first_product:
        problems.append(
            f"an up arrow did not come back to {first_product!r}, landed on "
            f"{back['view']['product']!r}")

    if start["panel"] != "open":
        problems.append(f"the panel started {start['panel']!r}, so the toggle "
                        f"check reads the wrong way round")
    await tap(page, "[")
    if (await state(page))["panel"] != "closed":
        problems.append("[ did not close the dock")
    await tap(page, "[")
    if (await state(page))["panel"] != "open":
        problems.append("[ did not reopen the dock")

    # A focused picker owns space, the arrows and the characters, since all
    # three move the selection. Space is the one to test with, because an
    # arrow in a select legitimately changes the product and so reloads the
    # step list, which makes a step-index check read the wrong way.
    await open_dock(page)
    select = page.locator("[data-dock-slot] select").first
    if await select.count() == 0:
        problems.append("no product picker to focus for the typing guard")
        return
    await select.focus()
    held = await state(page)
    await page.keyboard.press("Space")
    await page.wait_for_timeout(KEY_SETTLE_MS)
    after = await state(page)
    if after["strip"]["playing"] != held["strip"]["playing"]:
        problems.append(
            f"space inside the product picker toggled playback, "
            f"{held['strip']['playing']} to {after['strip']['playing']}; the "
            f"guard is not holding")
    await page.keyboard.press("Escape")
    await blur(page)


async def check_help_and_compare(page: Page, problems: list[str]) -> None:
    """? opens the help, Escape closes it, and c opens the second pane.

    Args:
        page: The loaded console.
        problems: Collects failure messages.
    """
    await page.locator("body").click(position={"x": 8, "y": 8})
    await tap(page, "?")
    if (await state(page))["help"] != 1:
        problems.append("? did not open the shortcut list")
    await tap(page, "Escape")
    if (await state(page))["help"] != 0:
        problems.append("Escape did not close the shortcut list")

    # A shortcut nobody can find is not a feature. The only sign they exist
    # is this control, since `?` is undiscoverable by definition.
    toggle = page.locator("[data-help-toggle]")
    if await toggle.count() != 1:
        problems.append(
            f"{await toggle.count()} controls point at the shortcut list; "
            f"without one, the keyboard is reachable only by guessing")
    else:
        await toggle.first.click()
        await page.wait_for_timeout(KEY_SETTLE_MS)
        if (await state(page))["help"] != 1:
            problems.append("the shortcut control did not open the list")
        await tap(page, "Escape")

    await tap(page, "c", settle=PANE_SETTLE_MS)
    opened = await state(page)
    if opened["panes"] != 2:
        problems.append(f"c left {opened['panes']} panes, expected 2")
    if len(await page.locator("[data-time-controls]").all()) != 1:
        problems.append("two time strips with compare on; the panes would "
                        "disagree about the time they share")
    await tap(page, "c", settle=PANE_SETTLE_MS)
    if (await state(page))["panes"] != 1:
        problems.append("c did not close the comparison pane")


async def run(browser: Browser, base: str, outdir: Path) -> dict[str, Any]:
    """Drive the keyboard over the deployed console.

    Args:
        browser: The running chromium.
        base: The deployment's origin.
        outdir: Where the screenshots go.

    Returns:
        What each step showed, with a "problems" list.
    """
    report: dict[str, Any] = {"base": base}
    problems: list[str] = []
    page = await open_console(browser, base)
    report["start"] = await state(page)

    report["after_time"] = await check_time_keys(page, problems)
    await page.screenshot(path=str(outdir / "keys-time.png"))
    await check_play_key(page, problems)
    await check_pan_and_zoom(page, problems)
    await page.screenshot(path=str(outdir / "keys-camera.png"))
    await check_product_and_panel(page, problems)
    await page.screenshot(path=str(outdir / "keys-product.png"))
    await page.locator("body").click(position={"x": 8, "y": 8})
    await tap(page, "?")
    await page.screenshot(path=str(outdir / "keys-help.png"))
    await tap(page, "Escape")
    await check_help_and_compare(page, problems)
    await page.screenshot(path=str(outdir / "keys-end.png"))

    report["end"] = await state(page)
    await page.context.close()
    report["problems"] = problems
    return report


async def shoot(outdir: Path, base: str) -> int:
    """Check the keyboard on the deployed console.

    Args:
        outdir: Where to write the screenshots and the report.
        base: The deployment's origin.

    Returns:
        Process exit status, non-zero when a key is wrong.
    """
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        try:
            report = await run(browser, base, outdir)
        except PlaywrightTimeout as e:
            report = {"problems": [f"the console stopped answering: {e}"]}
        except MapNeverLoadedError as e:
            report = {"problems": [str(e)]}
        await browser.close()
    (outdir / "report.json").write_text(json.dumps(report, indent=1))

    if "start" in report:
        print(f"start: {report['start']['strip']} product="
              f"{report['start']['view']['product']!r}")
    if report["problems"]:
        print("\nFAIL:")
        for p in report["problems"]:
            print(f"  {p}")
        return 1
    print(f"\nok: the bare arrows step time, the shifted ones pan, space "
          f"toggles once, and a focused control keeps its keys; images in "
          f"{outdir}")
    return 0


def main() -> int:
    """Parse arguments and run.

    Returns:
        Process exit status.
    """
    ap = argparse.ArgumentParser(description="check the console keyboard")
    ap.add_argument("outdir", type=Path)
    ap.add_argument("--base", default=DEFAULT_BASE)
    args = ap.parse_args()
    args.outdir.mkdir(parents=True, exist_ok=True)
    return asyncio.run(shoot(args.outdir, args.base))


if __name__ == "__main__":
    sys.exit(main())
