#!/usr/bin/env python3
"""Two panes have to differ in one thing and agree on everything else.

A comparison is only worth anything if the panes are genuinely locked. If
they drift in time, the reader is comparing two minutes rather than two
products; if they drift in view, they are comparing two places. Either way
the picture looks like a comparison and is not one, which is the failure
this whole console keeps running into.

What "the same frame time" means took a correction. Each product has its
own step manifest, and on 2026-11-21 the deployed qpe_1hr inch manifest
sat four steps behind comp_ref's, so the panes held 05:44 and 05:52 and
both were right: a follower lands on the nearest frame its own product
publishes. Demanding equal timestamps therefore tests the deployment's
publishing luck. What the console owes the reader is the nearest frame it
has, and a mark on the pane when that is not the time under the strip.

So the two claims are checked separately. The barrier phase puts both
panes on one product, where every step does prepare a frame in both, and
counts how the rendezvous released them. The lock phase then puts them on
different products and checks each pane sits on its own nearest frame,
says so when that differs, and holds the same camera. It also checks
there is exactly one time strip and one compare toggle, since a second
pane writing the shared chrome fights the first for it.

Usage
-----
::

    python validation_tests/visual/shoot_compare.py OUTDIR [--base URL]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

VISUAL = Path(__file__).resolve().parent
sys.path.insert(0, str(VISUAL))

try:
    from playwright.async_api import Browser, Page, Route, async_playwright
    from playwright.async_api import TimeoutError as PlaywrightTimeout
except ImportError:  # pragma: no cover - environment check
    print("playwright is not installed: pip install playwright "
          "&& playwright install chromium", file=sys.stderr)
    raise SystemExit(2) from None

from capture import open_dock  # noqa: E402  sibling module, loaded by path

DEFAULT_BASE = "https://the test deployment"

# The console polls every 5 s and holds a websocket, so networkidle never
# settles. Same waits capture.py measured, plus room for a second map to
# build its style.
LOAD_SETTLE_MS = 6000
PANE_SETTLE_MS = 6000
ACTION_SETTLE_MS = 2500

# Wide, because two panes side by side need the room and a narrow window
# would be testing the responsive pass instead.
VIEWPORT = {"width": 2200, "height": 1100}

# What the second pane is switched to, so the two differ in one thing.
OTHER_PRODUCT = "qpe_1hr"

# How far apart the panes' cameras may sit and still count as locked. The
# published values are rounded to five places of longitude, which is about
# one meter, so anything above that rounding is a real drift.
CAMERA_EPS = 2e-5

# How far the panes may sit apart mid-drag. Looser than CAMERA_EPS: these
# are MapLibre's live values rather than the rounded published ones, and
# the follower is one jumpTo behind the pane under the cursor. A pane that
# is tracking stays within a few hundred meters of its partner, and one
# that waits for the release is a whole drag away, which at zoom 7 is
# degrees.
MID_DRAG_EPS = 0.01

# How far the pane's manifest and this harness's may sit apart and still
# be the same list a few seconds older. The composites publish every two
# minutes, so two cadences covers a rollover between the two fetches and
# anything beyond it is a different list.
MANIFEST_DRIFT_S = 240

# How many frames to step while watching the rendezvous. Each step
# prepares a frame in both panes, so six gives enough releases to tell a
# gate that is holding them together from one that is timing out.
STEP_COUNT = 6

# How many releases must have had both panes arrive. One per step, less
# one for slack. A share would be the wrong measure: the panes also
# arrive separately on their own 5 s poll and 2 min overlay sweep, and
# those are not a pair, so the gate is right to let them through alone.
# Counting the step-driven releases asks whether the barrier works
# without being diluted by arrivals it should not hold.
#
# This holds only while both panes are on one product. On two products
# the follower's nearest frame does not change on every step, so it is
# right not to arrive, and the count would be measuring the manifests.
MIN_TOGETHER = STEP_COUNT - 1

# How many staggers the page keeps, as MAX_GAPS in routes/+page.svelte.
# Past it the oldest are dropped, which breaks a difference taken across
# the stepping, so the barrier phase runs first while the list is short.
GAPS_CAP = 40

# The gate's wait, from $lib/map/frameGate. A step-driven pair arriving
# further apart than this cannot be released together however the gate
# behaves, so the wait would need re-measuring.
GATE_WAIT_MS = 400

# How far from the shared time a pane's step may sit, mirroring
# STEP_TOLERANCE_MS in $lib/map/permalink. Beyond it a pane stays where
# it is rather than jumping to an unrelated time.
FOLLOW_TOLERANCE_MS = 3_600_000

# How far behind the shared time to cut pane B's manifest for the stale
# phase. Eight minutes is what the deployment served on 2026-11-21, when
# the qpe_1hr inch manifest fell four two-minute steps behind comp_ref's.
#
# Measured from the shared time rather than from the manifest's newest
# frame, because the follower has to be unable to reach the time the
# panes share. Withholding a fixed count of newest frames does not do
# that: pane A sits several steps back by then, and the frames behind it
# are still on offer. Shifting the whole window does not either, since a
# 62-minute window moved back by eight minutes still covers that time.
STALE_GAP_MIN = 8


async def gate_stats(page: Page) -> dict[str, Any]:
    """How the frame rendezvous has been releasing.

    Published only under ?diag=probe. A gate whose releases are nearly all
    timeouts is not holding the panes together, it is delaying one of
    them, and from outside the two look identical.

    Args:
        page: The loaded console.

    Returns:
        The counters, or an empty dict when the probe is off.
    """
    got: dict[str, Any] | None = await page.evaluate(
        "() => window.__sentinelGate ? window.__sentinelGate() : null"
    )
    return got or {}


async def pane_states(page: Page) -> list[dict[str, Any]]:
    """Read every pane's published view.

    Args:
        page: The loaded console.

    Returns:
        One entry per pane, in document order.
    """
    js = """
      () => [...document.querySelectorAll('[data-view]')]
        .map((el) => JSON.parse(el.getAttribute('data-view')))
    """
    got: list[dict[str, Any]] = await page.evaluate(js)
    return got


async def product_steps(page: Page, product: str,
                        unit: str | None) -> list[str]:
    """Fetch a product's own step manifest, through the page's origin.

    The unit belongs on this call. The inch and millimeter manifests of
    one product list different frames at different times, so taking a
    pane's expected frame from the wrong one compares it against another
    product's clock.

    Args:
        page: The loaded console, used for its fetch and its origin.
        product: The product id, as the pane publishes it.
        unit: The pane's unit, or None for the canonical manifest.

    Returns:
        Every frame time the manifest lists, in its order.
    """
    q = f"product_id={product}" + (f"&unit={unit}" if unit else "")
    js = """
      async (q) => {
        const r = await fetch(`/api/upstream/product_steps?${q}`);
        if (!r.ok) return [];
        const d = await r.json();
        return (d.steps || []).map((s) => s.ts || null);
      }
    """
    got: list[str | None] = await page.evaluate(js, q)
    return [t for t in got if t]


async def serve_stale_steps(page: Page, product: str,
                            cutoff: str) -> None:
    """Serve one product a manifest that stops at a given time.

    Whether two products' manifests are at different ages on the day of a
    run is the deployment's business, and a check that only fires when
    they are proves nothing on the days they agree. Cutting the manifest
    makes the follower unable to reach the shared time every run.

    Args:
        page: The loaded console.
        product: Which product id to cut; others are served untouched.
        cutoff: The newest frame time to keep, ISO.

    Raises:
        PlaywrightTimeout: If the route cannot be installed.
    """
    end = datetime.fromisoformat(cutoff)

    async def handler(route: Route) -> None:
        if f"product_id={product}" not in route.request.url:
            await route.fallback()
            return
        got = await route.fetch()
        body = await got.json()
        kept = [s for s in body.get("steps", [])
                if s.get("ts") and datetime.fromisoformat(s["ts"]) <= end]
        body["steps"] = kept
        body["n"] = len(kept)
        # The pane sets its index straight from this, so an index past
        # the end of the shortened list would leave it on no step at all.
        body["current_idx"] = max(len(kept) - 1, 0)
        await route.fulfill(response=got, json=body)

    await page.route("**/api/upstream/product_steps?**", handler)


def nearest_step(times: list[str], want: str | None) -> str | None:
    """Which of these frames the console would land on for a given time.

    Mirrors stepForTime in $lib/map/permalink: the globally nearest frame,
    or nothing at all when the closest is further off than the tolerance.

    Args:
        times: The manifest's frame times.
        want: The time being followed, or None.

    Returns:
        The frame time it would pick, or None.
    """
    if not want or not times:
        return None
    try:
        aim = datetime.fromisoformat(want)
    except ValueError:
        return None
    best: str | None = None
    best_gap = float("inf")
    for raw in times:
        try:
            gap = abs((datetime.fromisoformat(raw) - aim).total_seconds())
        except ValueError:
            continue
        if gap < best_gap:
            best_gap = gap
            best = raw
    if best is None or best_gap * 1000 > FOLLOW_TOLERANCE_MS:
        return None
    return best


def expected_offset(at: str, shared: str) -> str | None:
    """What the pane's mark should read for this pair of times.

    Mirrors frameOffsetLabel and gapLabel in $lib/map/timeAxis over the
    range the follow tolerance allows, which is an hour either way.

    Args:
        at: The frame the pane settled on.
        shared: The time under the strip.

    Returns:
        The expected label, or None when the pane should carry no mark.
    """
    delta = (datetime.fromisoformat(at)
             - datetime.fromisoformat(shared)).total_seconds()
    if abs(delta) < 30:
        return None
    minutes = round(abs(delta) / 60)
    words = f"{minutes}m" if minutes < 60 else f"{minutes // 60}h"
    return f"{words} {'behind' if delta < 0 else 'ahead'}"


async def live_cameras(page: Page) -> list[list[float] | None]:
    """Each pane's camera as MapLibre holds it right now.

    Published only under ?diag=probe. `data-view` carries the camera a
    pane settled on, which is written at moveend, so it cannot say
    whether the panes agree while a drag is running.

    Args:
        page: The loaded console.

    Returns:
        One [longitude, latitude, zoom] per pane in pane order, with None
        for a pane whose probe answered nothing.
    """
    js = """
      () => {
        const panes = window.__sentinelPanes;
        if (!panes) return [];
        return Object.keys(panes).sort().map((k) => {
          const s = panes[k]();
          return s && s.camera
            ? [s.camera.center[0], s.camera.center[1], s.camera.zoom]
            : null;
        });
      }
    """
    got: list[list[float] | None] = await page.evaluate(js)
    return got


def judge_mid_drag(cams: list[list[float] | None]) -> list[str]:
    """Check the panes agree while one of them is being dragged.

    Args:
        cams: Each pane's live camera.

    Returns:
        One message per way they disagree.
    """
    if len(cams) != 2:
        return [f"{len(cams)} panes published a live camera, expected 2; "
                f"this needs ?diag=probe"]
    a, b = cams
    if a is None or b is None:
        return [f"a pane published no live camera mid-drag: {a} and {b}"]
    names = ("longitude", "latitude", "zoom")
    out: list[str] = []
    for name, x, y in zip(names, a, b, strict=True):
        if abs(x - y) > MID_DRAG_EPS:
            out.append(
                f"{name} is {x} in the dragged pane and {y} in the other "
                f"while the button is down; the follower is waiting for "
                f"the release instead of tracking")
    return out


async def chrome_counts(page: Page) -> dict[str, int]:
    """Count the pieces there must be exactly one of.

    Args:
        page: The loaded console.

    Returns:
        How many panes, time strips, toggles and labels are on the page.
    """
    js = """
      () => ({
        panes: document.querySelectorAll('[data-view]').length,
        strips: document.querySelectorAll('[data-time-controls]').length,
        toggles: document.querySelectorAll('[data-pane-add]').length,
        labels: document.querySelectorAll('[data-pane-label]').length
      })
    """
    got: dict[str, int] = await page.evaluate(js)
    return got


async def canvas_sizes(page: Page) -> list[dict[str, float]]:
    """How much room each pane's map got.

    Two panes of different widths cover different ground at the same zoom,
    so the comparison would be between unequal areas.

    Args:
        page: The loaded console.

    Returns:
        One width and height per map canvas.
    """
    js = """
      () => [...document.querySelectorAll('.maplibregl-canvas')]
        .map((c) => ({ w: c.clientWidth, h: c.clientHeight }))
    """
    got: list[dict[str, float]] = await page.evaluate(js)
    return got


async def open_console(browser: Browser, base: str) -> Page:
    """Load the console with a known view and no stored prefs.

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
        "{activeRadars:[], moment:'Reflectivity', composite:'comp_ref'}));")
    # With the probe on, so the frame rendezvous publishes its counters.
    await page.goto(f"{base}/?diag=probe", wait_until="load", timeout=45_000)
    await page.wait_for_timeout(LOAD_SETTLE_MS)
    return page


def judge_single(counts: dict[str, int]) -> list[str]:
    """Check the console before compare is turned on.

    Args:
        counts: What chrome_counts found.

    Returns:
        One message per defect.
    """
    problems: list[str] = []
    if counts["panes"] != 1:
        problems.append(f"{counts['panes']} panes before compare is on")
    if counts["toggles"] != 1:
        problems.append(f"{counts['toggles']} compare toggles, expected 1")
    if counts["labels"] != 0:
        problems.append(
            f"{counts['labels']} pane labels with one pane; naming a pane "
            f"A when there is no B says there is a B")
    return problems


def judge_compare(counts: dict[str, int], panes: list[dict[str, Any]],
                  canvases: list[dict[str, float]]) -> list[str]:
    """Check the console with both panes open.

    Args:
        counts: What chrome_counts found.
        panes: Each pane's published view.
        canvases: Each map canvas's size.

    Returns:
        One message per defect.
    """
    problems: list[str] = []
    if counts["panes"] != 2:
        problems.append(f"{counts['panes']} panes with compare on")
        return problems
    if counts["strips"] != 1:
        problems.append(
            f"{counts['strips']} time strips; two would disagree about the "
            f"time the panes share")
    if counts["toggles"] != 1:
        problems.append(
            f"{counts['toggles']} compare toggles; the second pane must not "
            f"offer one, since turning compare off would remove the control "
            f"that did it")
    if counts["labels"] != 2:
        problems.append(
            f"{counts['labels']} pane labels; two maps of the same ground "
            f"are otherwise indistinguishable in a screenshot")
    if len(canvases) != 2:
        problems.append(f"{len(canvases)} map canvases, expected 2")
    elif abs(canvases[0]["w"] - canvases[1]["w"]) > 4:
        problems.append(
            f"the panes are {canvases[0]['w']} and {canvases[1]['w']} wide; "
            f"a comparison between unequal areas is not one")
    elif abs(canvases[0]["h"] - canvases[1]["h"]) > 4:
        # The first version of this checked only the width, and the time
        # strip sat inside the primary pane, so that pane was 80 px
        # shorter and the two covered different ground north to south.
        problems.append(
            f"the panes are {canvases[0]['h']} and {canvases[1]['h']} tall; "
            f"at one zoom they cover different ground")
    if panes[0] == panes[1]:
        problems.append(
            "both panes show the same view, so there is nothing to compare")
    return problems


def judge_painting(panes: list[dict[str, Any]]) -> list[str]:
    """Check each pane is painting the step it says it is on.

    The follower used to move its scrubber and keep whatever imagery it
    had last fetched, so a pane read as being at a minute it was not
    showing, which is the fault this mode exists to avoid.

    Args:
        panes: Each pane's published view.

    Returns:
        One message per pane whose imagery and scrubber disagree.
    """
    problems: list[str] = []
    for name, pane in zip(("A", "B"), panes, strict=True):
        if pane.get("frame") is None or pane.get("step") is None:
            problems.append(
                f"pane {name} published no frame or step: "
                f"{pane.get('frame')} at step {pane.get('step')}; what it "
                f"is showing cannot be checked"
            )
        elif pane["frame"] != pane["step"]:
            problems.append(
                f"pane {name} is at step {pane['step']} and painting "
                f"{pane['frame']}; its scrubber and its imagery are a "
                f"frame apart"
            )
    return problems


def judge_manifest(served: list[str], fetched: list[str]) -> list[str]:
    """Check the pane and this harness were given the same frames.

    Reported against the deployment rather than against the console.
    The pane cannot do better than the list it is handed, and saying
    which one it got is what tells a stale upstream apart from a pane
    that ignored the shared time.

    Args:
        served: The frame times the pane was given.
        fetched: The frame times the API returns now.

    Returns:
        One message when the two lists are too far apart to be the same
        manifest a few seconds older.
    """
    if not served or not fetched:
        return []
    gap = abs((datetime.fromisoformat(served[-1])
               - datetime.fromisoformat(fetched[-1])).total_seconds())
    if gap <= MANIFEST_DRIFT_S:
        return []
    return [f"pane B was served frames ending {served[-1]} and the API "
            f"answers {fetched[-1]}, {round(gap / 60)} minutes apart; the "
            f"deployment handed the console a stale manifest"]


def judge_following(panes: list[dict[str, Any]],
                    follower_times: list[str]) -> list[str]:
    """Check pane B sits on the nearest frame its own product has.

    Held to the list the pane was served rather than to one fetched
    here, because the two are fetched at different moments and have
    disagreed. On 2026-11-21 a pane held frames ending eleven hours back
    while the API answered with the current hour, and the pane was right
    to stay where it was.

    Args:
        panes: Each pane's published view.
        follower_times: Every frame time pane B's product publishes now.

    Returns:
        One message per way it failed to follow or to say so.
    """
    a, b = panes
    served = [t for t in (b.get("stepTimes") or []) if t]
    if not served:
        return ["pane B published no step list; this needs ?diag=probe"]
    problems = judge_manifest(served, follower_times)
    want = nearest_step(served, a.get("step"))
    if want is None:
        return [*problems,
                f"the {len(served)} frames pane B was served have none "
                f"within an hour of {a.get('step')}, so it was right to "
                f"stay put; what it was served is the fault"]
    if b.get("step") != want:
        return [*problems,
                f"pane B is at {b.get('step')}; the nearest frame in the "
                f"list it was served is {want}, so it did not follow"]
    shared = a.get("step")
    if not shared:
        return ["pane A published no step, so there is no shared time"]
    mark = expected_offset(want, shared)
    if b.get("offset") != mark:
        return [
            f"pane B is {mark or 'on the shared time'} and its label "
            f"says {b.get('offset')!r}; an unmarked pane reads as the "
            f"minute under the strip"
        ]
    return []


def judge_locked(panes: list[dict[str, Any]],
                 follower_times: list[str]) -> list[str]:
    """Check the panes differ in the product and agree on the rest.

    Args:
        panes: Each pane's published view.
        follower_times: Every frame time pane B's product publishes.

    Returns:
        One message per field that drifted.
    """
    if len(panes) != 2:
        return ["there are not two panes to compare"]
    problems: list[str] = []
    a, b = panes
    if a["product"] == b["product"]:
        problems.append(
            f"both panes are on {a['product']!r}, so the one thing being "
            f"compared is not different")
    if a["moment"] != b["moment"]:
        problems.append(
            f"moment drifted: {a['moment']!r} against {b['moment']!r}")
    problems += judge_painting(panes)
    problems += judge_following(panes, follower_times)
    return problems


def judge_cameras(panes: list[dict[str, Any]]) -> list[str]:
    """Check both panes settled on the same camera after one was dragged.

    Args:
        panes: Each pane's published view.

    Returns:
        One message per way they disagree.
    """
    if len(panes) != 2:
        return ["there are not two panes to compare"]
    a, b = (p.get("camera") for p in panes)
    if a is None or b is None:
        return [f"a pane published no camera: {a} and {b}; the lock cannot "
                f"be checked"]
    names = ("longitude", "latitude", "zoom")
    out: list[str] = []
    for name, x, y in zip(names, a, b, strict=True):
        if abs(x - y) > CAMERA_EPS:
            out.append(
                f"{name} drifted between the panes: {x} against {y}; they "
                f"are showing different places")
    return out


def judge_gate(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    """Check the rendezvous held the panes together while stepping.

    Read as the difference across the stepping, because the counters are
    cumulative and the panes also arrive on their own 5 s poll. Both panes
    are on one product here, so every step does prepare a frame in both
    and the barrier has something to hold.

    Args:
        before: The counters before the first step.
        after: The counters after the last one.

    Returns:
        One message per defect.
    """
    if not after:
        return ["the gate published no counters; this needs ?diag=probe"]
    released = after.get("released", 0) - before.get("released", 0)
    if released == 0:
        return [
            (
                f"the gate released nothing while stepping {STEP_COUNT} "
                f"frames; the panes are not going through it at all"
            )
        ]
    together = after.get("together", 0) - before.get("together", 0)
    problems: list[str] = []
    if together < MIN_TOGETHER:
        problems.append(
            f"{together} of {released} releases had both panes, under the "
            f"{MIN_TOGETHER} expected from {STEP_COUNT} steps on one "
            f"product; the gate is timing out rather than holding them "
            f"together"
        )
    # The gate's wait is set from this stagger. Every step-driven pair
    # measured under 160 ms on 2026-11-21, so a run where they are all
    # seconds apart means something changed in how the follower pane
    # learns about a step.
    if len(after.get("gaps", [])) >= GAPS_CAP:
        problems.append(
            f"the gate is holding its cap of {GAPS_CAP} staggers, so the "
            f"oldest have been dropped and the difference across the "
            f"stepping cannot be taken from them"
        )
        return problems
    added = after.get("gaps", [])[len(before.get("gaps", [])) :]
    gaps = sorted(g for g in added if g < 1000)
    if len(gaps) < MIN_TOGETHER:
        problems.append(
            f"only {len(gaps)} of {len(added)} arrivals were within a "
            f"second of each other; the panes are no longer stepping "
            f"together"
        )
    elif gaps[-1] > GATE_WAIT_MS:
        problems.append(
            f"the slowest step-driven pair was {gaps[-1]} ms apart, over "
            f"the gate's {GATE_WAIT_MS} ms wait; the wait needs "
            f"re-measuring"
        )
    return problems


async def check_stale(page: Page, outdir: Path,
                      report: dict[str, Any]) -> list[str]:
    """Serve pane B a stale manifest and read what the pane says.

    The pane cannot reach the shared time here, because its product no
    longer publishes a frame at it. What it owes the reader is the
    nearest frame it has and a mark saying how far back that is, and this
    is the only phase that makes the mark appear every run.

    Args:
        page: The loaded console, with both panes open on two products.
        outdir: Where the screenshot goes.
        report: Collects what the panes published under the stale manifest.

    Returns:
        One message per defect.
    """
    product = report["panes"][1]["product"]
    unit = report["panes"][1].get("unit")
    shared = report["panes"][0].get("step")
    if not shared:
        return ["pane A is on no step, so there is no time to cut behind"]
    cutoff = (datetime.fromisoformat(shared)
              - timedelta(minutes=STALE_GAP_MIN)).isoformat()
    await serve_stale_steps(page, product, cutoff)
    # The manifest is fetched once per product, so the pane has to be
    # sent away and brought back for the route to be the thing it reads.
    # Through the dock, pointed at the second pane: there is one product
    # control on the page now, and which pane it drives is the dock's tab.
    await open_dock(page, 1)
    picker = page.locator("[data-dock-slot] select").first
    await picker.select_option("comp_now")
    await page.wait_for_timeout(ACTION_SETTLE_MS)
    await picker.select_option(product)
    await page.wait_for_timeout(ACTION_SETTLE_MS)

    # Read through the same route, so this is the list the pane was
    # served rather than a second transformation of a newer one.
    short = await product_steps(page, product, unit)
    panes = await pane_states(page)
    report["stale"] = {"cutoff": cutoff, "panes": panes,
                       "newest": short[-1] if short else None}
    marks = await page.locator("[data-frame-offset]").all_inner_texts()
    report["stale"]["marks"] = marks
    await page.screenshot(path=str(outdir / "compare-stale.png"))

    problems = judge_painting(panes) + judge_following(panes, short)
    want = nearest_step(short, panes[0].get("step"))
    if want == panes[0].get("step"):
        problems.append(
            f"the manifest cut at {cutoff} still offers "
            f"{panes[0].get('step')}, so no mark was due and this phase "
            f"tested nothing")
    elif want is None:
        problems.append(
            f"the manifest cut at {cutoff} has no frame within an hour of "
            f"{panes[0].get('step')}, so this phase tested nothing")
    # Folded, because the label is set in small caps by the stylesheet
    # and inner_text reports what the reader sees.
    elif ([m.strip().lower() for m in marks]
          != [str(panes[1].get("offset")).lower()]):
        problems.append(
            f"the pane published offset {panes[1].get('offset')!r} and the "
            f"map shows {marks}; a mark only in the markup is not on screen")
    await page.unroute("**/api/upstream/product_steps?**")
    return problems


async def run(browser: Browser, base: str, outdir: Path) -> dict[str, Any]:
    """Open the console, turn compare on, and check the panes.

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

    report["single"] = await chrome_counts(page)
    problems += judge_single(report["single"])
    await page.screenshot(path=str(outdir / "compare-off.png"))

    await page.locator("[data-pane-add]").first.click()
    await page.wait_for_timeout(PANE_SETTLE_MS)
    report["counts"] = await chrome_counts(page)

    # The barrier, while both panes are still on one product. Every step
    # then prepares a frame in both and the rendezvous has two arrivals to
    # hold. On two products the follower's nearest frame does not move on
    # every step, so the same count would be measuring the manifests.
    before = await gate_stats(page)
    for _ in range(STEP_COUNT):
        await page.locator("body").press("ArrowLeft")
        await page.wait_for_timeout(ACTION_SETTLE_MS)
    report["gate"] = await gate_stats(page)
    report["gate_before"] = before
    problems += judge_gate(before, report["gate"])
    await page.screenshot(path=str(outdir / "compare-stepped.png"))

    # Then put the second pane on another product, so the panes differ in
    # the one thing a comparison is for.
    await open_dock(page, 1)
    await page.locator("[data-dock-slot] select").first.select_option(
        OTHER_PRODUCT)
    await page.wait_for_timeout(ACTION_SETTLE_MS)

    # Drag one pane and check the other follows. This is the claim the
    # whole mode rests on: panes that drift in view are comparing two
    # places, and the picture looks like a comparison either way.
    box = await page.locator(".maplibregl-canvas").first.bounding_box()
    if box:
        await page.mouse.move(box["x"] + 200, box["y"] + 300)
        await page.mouse.down()
        await page.mouse.move(box["x"] + 340, box["y"] + 380, steps=10)
        # Read both cameras while the button is still down. The follower
        # used to wait for the release, which is not a locked pane, it is
        # a pane that catches up, and the drag is what the reader watches.
        # `data-view` carries the settled camera and says nothing here, so
        # this reads each pane's live one.
        report["mid_drag"] = await live_cameras(page)
        problems += judge_mid_drag(report["mid_drag"])
        await page.screenshot(path=str(outdir / "compare-mid-drag.png"))
        await page.mouse.up()
        await page.wait_for_timeout(ACTION_SETTLE_MS)

    report["panes"] = await pane_states(page)
    report["canvases"] = await canvas_sizes(page)
    problems += judge_cameras(report["panes"])
    problems += judge_compare(report["counts"], report["panes"],
                              report["canvases"])
    # Pane B's own manifest, with its own unit, which is the only list
    # that says which frame it should be sitting on.
    follower = report["panes"][1] if len(report["panes"]) == 2 else {}
    times = await product_steps(page, follower.get("product", OTHER_PRODUCT),
                                follower.get("unit"))
    report["follower_times"] = {"product": follower.get("product"),
                                "unit": follower.get("unit"),
                                "n": len(times),
                                "newest": times[-1] if times else None}
    problems += judge_locked(report["panes"], times)
    await page.screenshot(path=str(outdir / "compare-on.png"))

    problems += await check_stale(page, outdir, report)

    # And off again, which has to leave one working pane behind. The
    # remove control, not the add one: the console counts panes, so the
    # way back from two is down rather than a second press of the same
    # button.
    await page.locator("[data-pane-remove]").first.click()
    await page.wait_for_timeout(ACTION_SETTLE_MS)
    report["after"] = await chrome_counts(page)
    if report["after"]["panes"] != 1:
        problems.append(
            f"{report['after']['panes']} panes after turning compare off")
    await page.screenshot(path=str(outdir / "compare-closed.png"))

    await page.context.close()
    report["problems"] = problems
    return report


async def shoot(outdir: Path, base: str) -> int:
    """Check compare mode on the deployed console.

    Args:
        outdir: Where to write the screenshots and the report.
        base: The deployment's origin.

    Returns:
        Process exit status, non-zero when the panes are not locked.
    """
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        try:
            report = await run(browser, base, outdir)
        except PlaywrightTimeout as e:
            report = {"problems": [f"the console stopped answering: {e}"]}
        await browser.close()
    (outdir / "report.json").write_text(json.dumps(report, indent=1))

    for pane in report.get("panes", []):
        print(
            f"pane: product={pane['product']!r} step={pane.get('step')} "
            f"painting={pane.get('frame')} offset={pane.get('offset')!r} "
            f"camera={pane.get('camera')}"
        )
    if "follower_times" in report:
        print(f"pane B's manifest: {report['follower_times']}")
    if "counts" in report:
        print(f"chrome: {report['counts']}")
    if "gate" in report:
        print(f"gate: {report['gate']}")

    if report["problems"]:
        print("\nFAIL:")
        for p in report["problems"]:
            print(f"  {p}")
        return 1
    print(
        f"\nok: the barrier released the panes together on one product, "
        f"and on two each sits on its own nearest frame and says how far "
        f"that is from the shared time; images in {outdir}"
    )
    return 0


def main() -> int:
    """Parse arguments and run.

    Returns:
        Process exit status.
    """
    ap = argparse.ArgumentParser(description="check compare mode")
    ap.add_argument("outdir", type=Path)
    ap.add_argument("--base", default=DEFAULT_BASE)
    args = ap.parse_args()
    args.outdir.mkdir(parents=True, exist_ok=True)
    return asyncio.run(shoot(args.outdir, args.base))


if __name__ == "__main__":
    sys.exit(main())
