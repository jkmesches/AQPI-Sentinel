"""Step-named forecast frames are served from the current model run.

Run directly (no DB, no network):

    python validation_tests/test_step_named_frames.py

The HRRR forecasts (rain rate, total and cumulative precip, temperature) name
their frames by step, C_hrrr_prate_step41.png, and every run overwrites the
same names. The image proxy keyed its memory cache and its archive lookup on
the file name, so each step showed the first copy Sentinel ever saw of it. On
2026-11-25 the map served temperature frames first captured in August, and a
rain rate frame with rain in it while radarca's own step was blank.

These checks run two model runs through the proxy routes with a fake origin.
The second run reuses every file name with new bytes and new valid times, and
the archive holds a stale copy under each name.
"""
from __future__ import annotations

import asyncio
import io
import os
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("SENTINEL_DB_URL", "postgresql://unused/unused")

from fastapi import HTTPException
from PIL import Image

import backend.api.routes.upstream as routes
from backend import config

PRODUCT = "fcst_precip_rate"
UNIT = "in/h"
TIMEOUT_S = 10
failures: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    """Record one assertion."""
    print(f"  {'ok  ' if cond else 'FAIL'}  {label}" + (f"  ({detail})" if detail else ""))
    if not cond:
        failures.append(label)


def png(alpha: int) -> bytes:
    """A 4x4 PNG whose every pixel has this alpha."""
    out = io.BytesIO()
    Image.new("RGBA", (4, 4), (200, 40, 40, alpha)).save(out, "PNG")
    return out.getvalue()


RUN_A, RUN_B, ARCHIVED = png(255), png(0), png(128)
NAMES = {RUN_A: "run A", RUN_B: "run B", ARCHIVED: "the archive's copy"}


class Origin:
    """A fake radarca imageData that serves whichever run is current."""

    def __init__(self) -> None:
        self.body = RUN_A

    async def get(self, url: str, **kw: Any) -> SimpleNamespace:
        return SimpleNamespace(status_code=200, content=self.body,
                               headers={"content-type": "image/png"})


def manifest(run_start: str) -> list[dict[str, str]]:
    """Three steps 15 min apart, named by step as the HRRR frames are."""
    hour = int(run_start[11:13])
    return [{"imageName": f"C_hrrr_prate_step{i + 1}.png",
             "timestamp": f"{run_start[:11]}{hour:02d}:{15 * i:02d}:00"}
            for i in range(3)]


class Setup:
    """The fake origin, manifest, archive and request for one scenario."""

    def __init__(self) -> None:
        self.origin = Origin()
        self.steps = manifest("2026-11-25T20:00:00")
        self.archive_reads = 0
        store = SimpleNamespace(pool=object())
        self.request = SimpleNamespace(app=SimpleNamespace(
            state=SimpleNamespace(context=SimpleNamespace(http=self.origin), store=store)))

    async def steps_cached(self, *a: object, **kw: object) -> list[dict[str, str]]:
        return self.steps

    async def archive_lookup(self, *a: object) -> tuple[bytes, str]:
        self.archive_reads += 1
        return ARCHIVED, "image/png"


async def archive_save(*a: object, **kw: object) -> None:
    return None


def start_patches(s: Setup) -> None:
    """Patch the manifest, the archive and the settings the routes read.

    Args:
        s: The scenario whose fakes stand in.
    """
    for p in (
        mock.patch.object(routes, "_product_steps_cached", s.steps_cached),
        mock.patch.object(routes, "_archive_lookup", s.archive_lookup),
        mock.patch.object(routes, "_archive_save", archive_save),
        mock.patch.object(routes, "_product_fetcher", return_value=None),
        mock.patch.object(routes, "SETTINGS", replace(config.SETTINGS, archive_enabled=True)),
    ):
        _ = p.start()


def reset() -> None:
    routes._IMAGE_CACHE.clear()
    routes._INFLIGHT.clear()
    routes._NEG_CACHE.clear()
    routes._activity_cache.clear()


async def by_step(s: Setup, step: int) -> tuple[bytes, str]:
    r = await asyncio.wait_for(
        routes.product_image_by_step(PRODUCT, step, s.request, unit=UNIT), TIMEOUT_S)
    return bytes(r.body), r.headers.get("x-sentinel-cache", "")


async def two_runs() -> None:
    """A new run's frame replaces the old one under the same name."""
    print("\nstep route, two runs:")
    reset()
    s = Setup()
    start_patches(s)
    try:
        body, prov = await by_step(s, 1)
        check("run A's step is fetched from the origin", body == RUN_A and prov == "upstream",
              prov)
        body, prov = await by_step(s, 1)
        check("...and served from memory the second time", body == RUN_A and prov == "lru",
              prov)
        s.origin.body, s.steps = RUN_B, manifest("2026-11-25T21:00:00")
        body, prov = await by_step(s, 1)
        check("run B's step, same file name, is run B's bytes", body == RUN_B,
              f"served {NAMES.get(body, 'unknown bytes')} via {prov}")
        check("...and the archive, which knows only the name, was never read",
              s.archive_reads == 0, f"{s.archive_reads} reads")
        r = await asyncio.wait_for(
            routes.latest_product_image(PRODUCT, s.request, unit=UNIT), TIMEOUT_S)
        check("the latest-frame route serves run B too", bytes(r.body) == RUN_B,
              NAMES.get(bytes(r.body), "unknown bytes"))
    finally:
        mock.patch.stopall()


async def time_named_control() -> None:
    """A product with times in its file names still reads the archive first."""
    print("\ntime-named control:")
    reset()
    s = Setup()
    s.steps = [{"imageName": "20261125_2140.png", "timestamp": "2026-11-25T21:40:00"}]
    start_patches(s)
    try:
        r = await asyncio.wait_for(
            routes.product_image_by_step("comp_ref", 0, s.request), TIMEOUT_S)
        check("comp_ref's frame comes from the archive",
              bytes(r.body) == ARCHIVED and r.headers.get("x-sentinel-cache") == "disk",
              r.headers.get("x-sentinel-cache", ""))
    finally:
        mock.patch.stopall()


async def activity_two_runs() -> None:
    """The activity sparkline reads each run's own frame."""
    print("\nactivity, two runs:")
    reset()
    s = Setup()
    start_patches(s)
    try:
        first = await asyncio.wait_for(
            routes.step_activity(s.request, product_id=PRODUCT), TIMEOUT_S)
        s.origin.body, s.steps = RUN_B, manifest("2026-11-25T21:00:00")
        second = await asyncio.wait_for(
            routes.step_activity(s.request, product_id=PRODUCT), TIMEOUT_S)
        a = [st["activity"] for st in first["steps"]]
        b = [st["activity"] for st in second["steps"]]
        check("run A's frames are all rain, run B's are clear",
              a == [1.0, 1.0, 1.0] and b == [0.0, 0.0, 0.0], f"{a}, {b}")
    finally:
        mock.patch.stopall()


def key_rules() -> None:
    """The key adds the valid time for step-named products only."""
    print("\nkeys:")
    src = "precip_rate/images/in/C_hrrr_prate_step1.png"
    key = routes._frame_key(PRODUCT, src,
                            {"imageName": "x", "timestamp": "2026-11-25T21:00:00"})
    check("a step-named frame's key carries its valid time",
          key == f"{src}@2026-11-25T21:00:00", str(key))
    check("a time-named frame keeps its plain name",
          routes._frame_key("comp_ref", "composite_ref_max/images/x.png",
                            {"timestamp": "t"}) is None)
    try:
        _ = routes._frame_key(PRODUCT, src, {"imageName": "C_hrrr_prate_step1.png"})
        check("a step-named frame without a valid time is refused", False)
    except HTTPException as e:
        check("a step-named frame without a valid time is refused", e.status_code == 502)
    named = sorted(p for p in config.PRODUCTS if config.is_step_named(p))
    check("the four HRRR forecasts are the step-named products",
          named == ["fcst_precip_rate", "fcst_temp", "fcst_total_precip",
                    "fcst_total_precip_cum"], str(named))


async def main() -> int:
    """Run every section.

    Returns:
        1 when an assertion failed.
    """
    key_rules()
    await two_runs()
    await time_named_control()
    await activity_two_runs()
    print(f"\n{len(failures)} FAILED: {', '.join(failures)}" if failures
          else "\nall step-named frame assertions passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
