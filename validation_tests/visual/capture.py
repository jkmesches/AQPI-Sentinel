#!/usr/bin/env python3
"""Record what the map console shows, so a refactor can be shown not to change it.

Why this exists
---------------
Slice 3 of ``docs/map-console-plan.md`` moves 3,277 lines of MapView into
modules. The claim it has to support is "nothing user-visible changed", and
the map had no test of any kind, because its logic lives inside a ``.svelte``
file where no ``.mjs`` runner can reach it.

Pixels alone will not do. The imagery under the overlays is live and changes
every few minutes, so a pixel diff taken an hour apart is mostly noise. A
screenshot also cannot see a layer inserted at the wrong ``beforeId``, a paint
property dropped, or a source that stopped being added, and those are the
faults a decomposition produces.

So the capture is structural. ``?diag=probe`` publishes
``window.__sentinelProbe()``, which reports the camera, every layer in paint
order, the sources this component adds, the paint and layout of each layer on
them, and the component's own control state. That is deterministic across runs
and it is what the refactor must preserve. Screenshots are written beside it
for a human to look at. Nothing compares them automatically.

State comes from ``localStorage`` rather than from clicking, because the
markup is being rewritten and a selector written today would not survive the
slice it exists to check. ``mapPrefs`` is a tested module with its own
vocabulary validation, and driving through it exercises the restore path the
console uses on every load.

Usage
-----
::

    python validation_tests/visual/capture.py OUT_DIR [--base URL] [--shots]

``OUT_DIR`` is created if absent. Compare two of them with ``compare.py``.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import NotRequired, TypeAlias, TypedDict

try:
    from playwright.async_api import Page, async_playwright
    from playwright.async_api import TimeoutError as PlaywrightTimeout
except ImportError:  # pragma: no cover - environment check, not a code path
    print("playwright is not installed: pip install playwright "
          "&& playwright install chromium", file=sys.stderr)
    raise SystemExit(2) from None

Json: TypeAlias = "str | int | float | bool | list[Json] | dict[str, Json] | None"

DEFAULT_BASE = "https://the test deployment"
PREFS_KEY = "sentinel-map-prefs"
PREFS_VERSION = 1
VIEWPORT = {"width": 1600, "height": 950}

# The console fetches a step list, then a frame, then applies both behind the
# frame barrier, and MapLibre fires `idle` well before any of that has run.
#
# A flat wait was the first design and it was wrong. At 9 s the forecast
# composite landed on some runs and not others, which made the baseline
# disagree with itself about whether a layer exists. The poll below waits for
# the structure to stop changing instead, which is the condition that was
# meant all along.
SETTLE_POLL_MS = 750
SETTLE_STABLE_POLLS = 4
SETTLE_MIN_MS = 4000
SETTLE_MAX_MS = 30_000

# Volatility is measured across two independent loads of the same state, not
# across two reads of one load. A reload is what exposes the things that
# differ run to run: a fresh blob: id, an epoch baked into a style expression,
# and the order two overlays finish fetching in. A second read of one page
# sees none of those and the baseline then reports them as regressions.
RELOAD_SETTLE_MS = 1500

# Attempts per state before it is recorded as failed.
#
# A state failed once in 56 loads across two captures of the same build: the
# forecast composite never appeared inside the 30 s cap, while the same state
# loaded in 0.5 s on 12 of 12 reloads measured straight afterward and the
# upstream answered in under 120 ms. At roughly 1 in 56, a 28-state run would
# carry a failure about half the time and never finish clean.
#
# A retry makes a run usable without hiding the rate: the first error is kept
# on the record and counted in the manifest.
ATTEMPTS = 2

# The camera lands on a float that a fitBounds animation chose, so it is
# recorded at a precision a reader could tell apart on screen.
CAMERA_PLACES = 4

# A JSON document from a browser probe, so the depth is small and fixed by the
# shape of a MapLibre style. The two walkers below carry this bound.
MAX_DEPTH = 40


class StateRecord(TypedDict):
    """One captured state, as written to `<name>.json`."""

    name: str
    prefs: dict[str, Json]
    snapshot: dict[str, Json]
    counts: dict[str, int]
    volatile: list[str]
    unstableOrder: bool
    # Present only when a first attempt failed; carries that error.
    retried: NotRequired[str]


@dataclass(frozen=True)
class RunConfig:
    """Where to capture from and what to write."""

    base: str
    out: Path
    shots: bool


def default_prefs(**over: Json) -> dict[str, Json]:
    """Build one prefs object, with the console's defaults under any overrides.

    Args:
        **over: Fields to replace.

    Returns:
        A dict ready for `localStorage`, carrying the schema version.
    """
    base: dict[str, Json] = {
        "composite": "comp_ref", "moment": "Reflectivity", "activeRadars": [],
        "nexrad": False, "opacity": 0.8, "panelOpen": True,
        "watersheds": False, "reservoirs": False, "terrain": False,
        "terrain3D": False, "streamGauges": False, "burnScars": False,
        # Two, matching mapPrefs.DEFAULT_PREFS, so "00-default" is the state
        # a first visit lands on rather than one this file invented.
        "burnScarYears": 2, "tiltEl": 0, "camera": None,
    }
    base.update(over)
    return {"v": PREFS_VERSION, **base}


# Present in every state, so each entry below names only what it adds.
BASE_SOURCES = frozenset({"radars", "ranges"})


def want(*sources: str) -> frozenset[str]:
    """The sources a state must have produced before it is recorded.

    Args:
        *sources: Ids beyond the two every state carries.

    Returns:
        The full expected set.
    """
    return BASE_SOURCES | frozenset(sources)


# The matrix. Each entry is one page load, and its name becomes a file name,
# so names stay short and stable: renaming a state reads as one removed plus
# one added.
#
# The third field is what the state must have loaded. Waiting for the
# structure to stop changing is not enough on its own, because a state that
# has loaded nothing yet is also not changing. The forecast composite is slow
# enough to settle that way, and two captures then disagreed about whether
# `comp-overlay-layer` exists at all.
#
# Measured from a capture rather than reasoned out. `18-tilt` has
# `tilt-overlay` and no `comp-overlay`, because tilt mode takes the scrubber
# and drops the composite, which is the sort of thing a guess gets wrong.
#
# The radar ids are AQPI's. A deployment that does not publish them fails
# those states loudly, since `mapPrefs` validates against the deployment's own
# vocabulary and the console falls back to something else.
STATES: list[tuple[str, dict[str, Json], frozenset[str]]] = [
    ("00-default", default_prefs(), want("comp-overlay")),
    ("01-composite-off", default_prefs(composite="none"), want()),
    ("02-composite-nowcast", default_prefs(composite="comp_now"),
     want("comp-overlay")),
    ("03-composite-qpe15", default_prefs(composite="qpe_15min"),
     want("comp-overlay")),
    ("04-composite-qpe1h", default_prefs(composite="qpe_1hr"),
     want("comp-overlay")),
    ("05-composite-rate", default_prefs(composite="precip_rate_radar"),
     want("comp-overlay")),
    ("06-composite-fcst", default_prefs(composite="fcst_total_precip"),
     want("comp-overlay")),
    ("07-opacity-low", default_prefs(opacity=0.25), want("comp-overlay")),
    ("08-opacity-full", default_prefs(opacity=1.0), want("comp-overlay")),
    ("09-panel-closed", default_prefs(panelOpen=False), want("comp-overlay")),
    ("10-nexrad", default_prefs(nexrad=True), want("comp-overlay", "nexrad")),
    ("11-nexrad-no-composite", default_prefs(nexrad=True, composite="none"),
     want("nexrad")),
    ("12-radar-one", default_prefs(activeRadars=["XEBY"]),
     want("comp-overlay", "radar-XEBY-src")),
    ("13-radar-two", default_prefs(activeRadars=["XEBY", "XSCR"]),
     want("comp-overlay", "radar-XEBY-src", "radar-XSCR-src")),
    ("14-radar-velocity", default_prefs(activeRadars=["XEBY"], moment="Velocity"),
     want("comp-overlay", "radar-XEBY-src")),
    ("15-radar-zdr",
     default_prefs(activeRadars=["XEBY"], moment="Differential Reflectivity"),
     want("comp-overlay", "radar-XEBY-src")),
    ("16-radar-phidp", default_prefs(activeRadars=["XEBY"], moment="PhiDP"),
     want("comp-overlay", "radar-XEBY-src")),
    ("17-radar-rhohv", default_prefs(activeRadars=["XEBY"], moment="RhoHV"),
     want("comp-overlay", "radar-XEBY-src")),
    ("18-tilt", default_prefs(activeRadars=["XEBY"], tiltEl=1),
     want("tilt-overlay")),
    ("19-watersheds", default_prefs(watersheds=True),
     want("comp-overlay", "watersheds")),
    ("20-reservoirs", default_prefs(reservoirs=True),
     want("comp-overlay", "reservoirs")),
    ("21-stream-gauges", default_prefs(streamGauges=True),
     want("comp-overlay", "stream-gauges")),
    ("22-burn-scars", default_prefs(burnScars=True),
     want("comp-overlay", "burn-scars")),
    ("23-burn-scars-wide", default_prefs(burnScars=True, burnScarYears=30),
     want("comp-overlay", "burn-scars")),
    ("24-terrain", default_prefs(terrain=True),
     want("comp-overlay", "terrain-dem")),
    ("25-terrain-3d", default_prefs(terrain=True, terrain3D=True),
     want("comp-overlay", "terrain-dem")),
    ("26-overlays-all",
     default_prefs(watersheds=True, reservoirs=True, streamGauges=True,
                   burnScars=True, nexrad=True),
     want("comp-overlay", "nexrad", "watersheds", "reservoirs",
          "stream-gauges", "burn-scars")),
    ("27-camera-zoomed",
     default_prefs(camera={"center": [-122.3, 37.9], "zoom": 10.5,
                           "bearing": 0, "pitch": 0}),
     want("comp-overlay")),
]

_TS_PATTERNS = [
    # An object-URL id, minted fresh on every load. The NEXRAD mosaic is
    # fetched and handed to MapLibre as a blob, so this changes every run and
    # says nothing about structure.
    (re.compile(r"blob:https?://[^/]+/[0-9a-fA-F-]{36}"), "blob:<ID>"),
    # Step timestamps in frame URLs and labels, e.g. 20261120-2105 and
    # 20261120T210500Z.
    (re.compile(r"\d{8}[-T]\d{4,6}Z?"), "<TS>"),
    # Ages the panel renders, e.g. +2m38s, -5d2h51m, 13.1h.
    (re.compile(r"[+-]?\d+(\.\d+)?[dhms](\d+[hms])*\b"), "<AGE>"),
    # Cache-busting and frame-index query parameters. `step` joins them
    # because the index follows a step list whose length drifts; see
    # UPSTREAM_OWNED.
    (re.compile(r"([?&](?:frame|step|t|ts|_)=)[^&\"]*"), r"\1<N>"),
]

# Epoch milliseconds, which the burn-scar layer bakes into its own paint and
# filter expressions to age each perimeter. The window runs 2001 to 2286, so
# a genuine constant of that size would be masked; none of this component's
# are anywhere near it.
EPOCH_MS_MIN = 1_000_000_000_000
EPOCH_MS_MAX = 9_999_999_999_999

# Fields whose value is upstream's to choose, by flattened path prefix.
#
# The tilt overlay's corners are built from the `center` and `range_km` that
# `/api/upstream/tilt_steps` reports for the scan on screen, and radar-display
# reports a slightly different range per scan. Two captures twenty minutes
# apart measured the corners moving about 60 m, which is the scan changing
# rather than the code.
#
# Named here instead of rounded away, so every other extent still compares
# exactly. A composite placed on the wrong box is the fault this whole capture
# exists to catch, and coarsening the geometry to silence one source would
# blunt it everywhere.
#
# The step counts belong here too, for a different reason. The radar
# composites roll a fixed window and sit still at 31, but the forecast keeps
# growing: `fcst_total_precip` went from 69 steps to 74 across one afternoon
# as further forecast hours published. A baseline that pinned the number
# would report a difference on that state every day.
#
# Losing them costs the capture its view of a broken step list, so the
# playback logic gets its own runner assertions rather than leaning on this.
UPSTREAM_OWNED = (
    "sources.tilt-overlay.coordinates",
    "state.stepCount",
    "state.stepIdx",
)


def scrub(value: Json, depth: int = 0) -> Json:  # bounded: MAX_DEPTH
    """Replace the parts of a snapshot that move on their own.

    A frame URL carries the timestamp of the step it shows, and that advances
    every few minutes whatever the code does. The rest of the URL is
    structure: which endpoint, which radar, which moment, which elevation.

    Args:
        value: Any JSON-shaped value from the probe.
        depth: Recursion depth, bounded by MAX_DEPTH.

    Returns:
        The same shape with timestamps, ages and cache keys tokenized.

    Raises:
        RecursionError: If the payload nests past MAX_DEPTH, which a MapLibre
            style never does and a runaway structure would.
    """
    if depth > MAX_DEPTH:
        raise RecursionError(f"probe payload nests past {MAX_DEPTH} levels")
    if isinstance(value, list):
        return [scrub(v, depth + 1) for v in value]
    if isinstance(value, dict):
        return {k: scrub(v, depth + 1) for k, v in value.items()}
    return scrub_leaf(value)


def scrub_leaf(value: Json) -> Json:
    """Tokenize one leaf value.

    Args:
        value: A string, number, bool or None from the probe.

    Returns:
        The value with timestamps, ids and epochs replaced.
    """
    if isinstance(value, str):
        out = value
        for pat, repl in _TS_PATTERNS:
            out = pat.sub(repl, out)
        return out
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and EPOCH_MS_MIN <= value <= EPOCH_MS_MAX:
        return "<EPOCH_MS>"
    if isinstance(value, float):
        return round(value, CAMERA_PLACES)
    return value


def flatten(value: Json, prefix: str = "", depth: int = 0) -> dict[str, Json]:  # bounded: MAX_DEPTH
    """Flatten to a path-to-leaf map, so a diff can name what moved.

    Args:
        value: A JSON-shaped value.
        prefix: Path accumulated so far.
        depth: Recursion depth, bounded by MAX_DEPTH.

    Returns:
        Leaf values keyed by dotted path, with list indices in brackets.

    Raises:
        RecursionError: If the payload nests past MAX_DEPTH.
    """
    if depth > MAX_DEPTH:
        raise RecursionError(f"probe payload nests past {MAX_DEPTH} levels")
    out: dict[str, Json] = {}
    if isinstance(value, dict):
        for k, v in value.items():
            out.update(flatten(v, f"{prefix}.{k}" if prefix else str(k),
                               depth + 1))
    elif isinstance(value, list):
        for i, v in enumerate(value):
            out.update(flatten(v, f"{prefix}[{i}]", depth + 1))
    else:
        out[prefix] = value
    return out


async def read_probe(page: Page) -> dict[str, Json] | None:
    """Read the probe, or None when the map has not published it.

    Args:
        page: The loaded console page.

    Returns:
        The raw probe payload.
    """
    result = await page.evaluate(
        "() => window.__sentinelProbe ? window.__sentinelProbe() : null")
    return result if isinstance(result, dict) else None


def as_ids(value: Json) -> list[str]:
    """Coerce a snapshot's `layerOrder` to a list of ids.

    Args:
        value: The field as the probe reported it.

    Returns:
        The ids, or an empty list when the field is absent or malformed.
    """
    if not isinstance(value, list):
        return []
    return [str(v) for v in value]


def fingerprint(snap: dict[str, Json] | None) -> str | None:
    """Reduce a snapshot to the part that has to stop changing.

    Source and layer ids, and nothing that advances on its own. A frame url
    stepping forward would otherwise keep resetting the settle counter and
    every state would wait out its cap.

    Args:
        snap: A raw probe payload.

    Returns:
        A comparable string, or None when the probe is not published yet.
    """
    if snap is None:
        return None
    sources = snap.get("sources")
    layers = snap.get("layers")
    order = snap.get("layerOrder")
    return json.dumps([
        sorted(sources) if isinstance(sources, dict) else None,
        sorted(layers) if isinstance(layers, dict) else None,
        len(order) if isinstance(order, list) else None,
    ])


def source_ids(snap: dict[str, Json] | None) -> frozenset[str]:
    """The source ids a snapshot reports.

    Args:
        snap: A raw probe payload.

    Returns:
        The ids, empty when the probe is absent or reports no sources.
    """
    sources = (snap or {}).get("sources")
    return frozenset(sources) if isinstance(sources, dict) else frozenset()


async def settle(page: Page, expect: frozenset[str]) -> dict[str, Json] | None:
    """Wait for the state to load and its structure to stop changing.

    Both conditions, because a page that has loaded nothing is also not
    changing. Stability alone let the forecast composite settle as absent.

    Args:
        page: The loaded console page.
        expect: Source ids this state must have produced.

    Returns:
        The probe payload once ready, or the last read at the cap.
    """
    last: str | None = None
    stable = 0
    waited = 0
    while waited < SETTLE_MAX_MS:
        await page.wait_for_timeout(SETTLE_POLL_MS)
        waited += SETTLE_POLL_MS
        snap = await read_probe(page)
        now = fingerprint(snap)
        if now is not None and now == last and expect <= source_ids(snap):
            stable += 1
            if stable >= SETTLE_STABLE_POLLS and waited >= SETTLE_MIN_MS:
                return snap
        else:
            stable = 0
        last = now
    return await read_probe(page)


def count_of(snap: dict[str, Json]) -> dict[str, int]:
    """Summarize a snapshot, for the console line and the manifest.

    Args:
        snap: A scrubbed probe payload.

    Returns:
        Layer, own-layer and own-source counts.
    """
    order = snap.get("layerOrder")
    layers = snap.get("layers")
    sources = snap.get("sources")
    return {
        "layers": len(order) if isinstance(order, list) else 0,
        "ours": len(layers) if isinstance(layers, dict) else 0,
        "sources": len(sources) if isinstance(sources, dict) else 0,
    }


async def capture_state(page: Page, cfg: RunConfig, name: str,
                        prefs: dict[str, Json],
                        expect: frozenset[str]) -> StateRecord:
    """Load one state and record it.

    Args:
        page: A page already on the target origin, so `localStorage` is
            writable before the console loads.
        cfg: Where to capture from and what to write.
        name: Matrix entry name, used as the file name.
        prefs: The `mapPrefs` object to restore from.
        expect: Source ids this state must produce before it is recorded.

    Returns:
        The scrubbed snapshot with the keys found to be volatile.

    Raises:
        RuntimeError: If the probe never appears or the state never loads
            what it asked for. Recording either would be a lie, and a
            baseline entry saying a layer is absent is the kind of lie a
            later refactor gets blamed for.
        TypeError: If the probe returns something other than an object.
    """
    await page.evaluate(
        "([k, v]) => localStorage.setItem(k, JSON.stringify(v))",
        [PREFS_KEY, prefs])
    # `load`, never `networkidle`. The console polls every 5 s and holds a
    # websocket open, so the network never goes idle and a capture keyed on
    # that waits out its timeout on every state.
    await page.goto(f"{cfg.base}/?diag=probe", wait_until="load",
                    timeout=60_000)
    first = await settle(page, expect)
    if first is None:
        raise RuntimeError("the probe never published; is the deployment "
                           "running a build that knows ?diag=probe")
    missing = expect - source_ids(first)
    if missing:
        raise RuntimeError(f"never loaded {sorted(missing)} within "
                           f"{SETTLE_MAX_MS / 1000:g}s")
    if cfg.shots:
        await page.screenshot(path=str(cfg.out / f"{name}.png"))

    await page.reload(wait_until="load", timeout=60_000)
    await page.wait_for_timeout(RELOAD_SETTLE_MS)
    # Not gated on `expect`. The reload exists to measure volatility, and a
    # state that loaded once and not twice has shown exactly that. Recording
    # the difference marks those keys volatile, which is honest about this
    # deployment. A refactor that drops a layer for good still fails loudly,
    # on the first load's gate above.
    second = await settle(page, expect)

    scrubbed = scrub(first)
    if not isinstance(scrubbed, dict):
        raise TypeError(f"probe returned {type(first).__name__}, not an object")
    left = flatten(scrubbed)
    right = flatten(scrub(second if second is not None else first))
    keys = sorted(set(left) | set(right))
    volatile = [k for k in keys
                if left.get(k) != right.get(k) or k.startswith(UPSTREAM_OWNED)]

    order_a = as_ids(scrubbed.get("layerOrder"))
    order_b = as_ids((second or {}).get("layerOrder"))
    return {"name": name, "prefs": prefs, "snapshot": scrubbed,
            "counts": count_of(scrubbed), "volatile": volatile,
            # Two loads of one state can put the layers in different orders.
            # The cause is in this component, and `compare.py` downgrades an
            # order change on such a state to a warning, so a later refactor
            # does not wear the blame for it.
            "unstableOrder": order_a != order_b}


async def capture_with_retry(
    page: Page, cfg: RunConfig, name: str,
    prefs: dict[str, Json], expect: frozenset[str]
) -> tuple[StateRecord, str]:
    """Capture one state, allowing for a transient failure to load.

    A state that fails and then succeeds is recorded, with what went wrong
    kept on the record and printed. The console genuinely failed to show
    what it was asked for, and a reader of the page would have seen the same
    thing, so the retry buys a usable run without hiding the rate.

    Args:
        page: A page already on the target origin.
        cfg: Where to capture from and what to write.
        name: Matrix entry name.
        prefs: The `mapPrefs` object to restore from.
        expect: Source ids this state must produce.

    Returns:
        The record, and the first attempt's error when one was needed.

    Raises:
        RuntimeError: If every attempt failed, carrying the last message.
    """
    note = ""
    for attempt in range(ATTEMPTS):
        try:
            return await capture_state(page, cfg, name, prefs, expect), note
        except (RuntimeError, TypeError, RecursionError,
                TimeoutError, PlaywrightTimeout) as e:
            note = str(e)
            if attempt + 1 < ATTEMPTS:
                print(f"  retry {name}: {note}")
    raise RuntimeError(note)


async def run(cfg: RunConfig) -> int:
    """Walk the matrix and write one JSON file per state plus a manifest.

    Args:
        cfg: Where to capture from and what to write.

    Returns:
        Process exit status, non-zero when any state failed to capture.
    """
    cfg.out.mkdir(parents=True, exist_ok=True)
    captured: list[str] = []
    retried: list[dict[str, str]] = []
    failed: list[dict[str, str]] = []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page(viewport=VIEWPORT)
        # localStorage needs an origin before the first write.
        await page.goto(f"{cfg.base}/?diag=probe",
                        wait_until="domcontentloaded", timeout=60_000)
        for name, prefs, expect in STATES:
            try:
                rec, note = await capture_with_retry(page, cfg, name, prefs, expect)
            except RuntimeError as e:
                print(f"  FAIL  {name}: {e}")
                failed.append({"name": name, "error": str(e)})
                continue
            if note:
                rec["retried"] = note
                retried.append({"name": name, "error": note})
            (cfg.out / f"{name}.json").write_text(
                json.dumps(rec, indent=1, sort_keys=True) + "\n")
            c = rec["counts"]
            print(f"  ok    {name:<24} {c['layers']:>3} layers, "
                  f"{c['ours']:>2} ours, {c['sources']:>2} sources, "
                  f"{len(rec['volatile']):>2} volatile"
                  f"{'  (retried)' if note else ''}")
            captured.append(name)
        await browser.close()
    (cfg.out / "manifest.json").write_text(
        json.dumps({"base": cfg.base, "states": captured,
                    "retried": retried, "failed": failed},
                   indent=1, sort_keys=True) + "\n")
    print(f"\n{len(captured)}/{len(STATES)} states captured into {cfg.out}"
          + (f"; {len(retried)} needed a retry" if retried else ""))
    return 1 if failed else 0


def main() -> int:
    """Parse arguments and capture.

    Returns:
        Process exit status.
    """
    ap = argparse.ArgumentParser(description="capture the map console's structure")
    ap.add_argument("out", type=Path, help="directory to write the capture to")
    ap.add_argument("--base", default=DEFAULT_BASE,
                    help=f"deployment origin (default {DEFAULT_BASE})")
    ap.add_argument("--shots", action="store_true",
                    help="also write a PNG per state, for a human to read")
    args = ap.parse_args()
    return asyncio.run(run(RunConfig(base=args.base, out=args.out,
                                     shots=args.shots)))


if __name__ == "__main__":
    sys.exit(main())
