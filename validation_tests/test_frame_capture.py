"""The CoSMoS frame capture: new targets, observation rows and the unchanged-frame skip.

Run directly (no DB, no network, no mount):

    python validation_tests/test_frame_capture.py

Plan 80 §1 (2026-11-24) adds water_level, max_water_depth and max_water_level
to the image QC checks, so each cycle's frames reach the archive, and has
every product capture record the valid time it draws. Ways this goes wrong,
each silent:

  - A target that is listed but never registered, so nothing captures it.
  - The observation carrying the capture time instead of the step's valid
    time, which files every frame under the minute it was read.
  - The skip comparing the wrong thing, so it either reads every run (the
    K2 load it exists to cut) or never reads a changed frame (a frozen
    check that reports an old frame as current).
  - The backfill reading max_water_depth's frames as water_depth's.
"""
from __future__ import annotations

import asyncio
import io
import os
import sys
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("SENTINEL_DB_URL", "postgresql://unused/unused")
# A mount root, so the checks register and read through fs_products, whose
# readers this test replaces.
os.environ.setdefault("SENTINEL_BACKEND_ROOT", "/nonexistent-test-mount")

from PIL import Image  # noqa: E402

from backend import fs_products as fs  # noqa: E402
from backend import thresholds  # noqa: E402
from backend.archive import Observation  # noqa: E402
from backend.archive.backfill import parse_origin, to_batch  # noqa: E402
from backend.checks import layer4_image as l4  # noqa: E402
from backend.checks.transports import CheckContext  # noqa: E402
from backend.config import L4_MOSAIC_TARGETS, RECORD_ONLY_TRIAL_CHECKS  # noqa: E402
from backend.registry import CHECKS  # noqa: E402

failures: list[str] = []
NEW = ("water_level", "max_water_depth", "max_water_level")


def check(label: str, cond: bool, detail: str = "") -> None:
    """Print one assertion and record a failure."""
    print(f"  {'ok  ' if cond else 'FAIL'}  {label}" + (f"  — {detail}" if detail else ""))
    if not cond:
        failures.append(label)


def png(seed: int) -> bytes:
    """Draw a small PNG whose pixels depend on a seed.

    Args:
        seed: Any small integer; two seeds give two different files.

    Returns:
        The PNG's bytes.
    """
    im = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    for i in range(64):
        im.putpixel((i, (i * seed) % 64), (30, 90 + seed, 200, 255))
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


class Mount:
    """A fake mount: one manifest step, one file, and counts of each read."""

    def __init__(self, name: str, valid: str) -> None:
        self.steps = [{"ts": valid, "imageName": name}]
        self.content = png(1)
        self.mtime_ns = 1_795_400_000_000_000_000
        self.reads = 0
        self.stats = 0

    async def read_steps(self, product_id: str, unit: str | None = None) -> list[dict[str, Any]]:
        """The manifest."""
        _ = (product_id, unit)
        return self.steps

    async def stat_image(self, source: str) -> fs.FileStat:
        """The file's size and mtime."""
        _ = source
        self.stats += 1
        return fs.FileStat(len(self.content), self.mtime_ns)

    async def read_image(self, source: str) -> fs.FsResponse:
        """The file's bytes."""
        _ = source
        self.reads += 1
        return fs.FsResponse(200, self.content)


def test_targets() -> None:
    """Each new product is listed, registered, and on the record-only trial."""
    print("targets:")
    for p in NEW:
        cid = f"layer4.mosaic.{p}"
        check(f"{p} is a mosaic target", p in L4_MOSAIC_TARGETS)
        check(f"{cid} is registered", cid in CHECKS)
        check(f"{cid} is on the record-only trial", cid in RECORD_ONLY_TRIAL_CHECKS)
    check("the trial holds only these three", RECORD_ONLY_TRIAL_CHECKS
          == {f"layer4.mosaic.{p}" for p in NEW}, str(sorted(RECORD_ONLY_TRIAL_CHECKS)))
    check("the older targets are still there",
          {"comp_ref", "comp_now", "water_depth"} <= set(L4_MOSAIC_TARGETS))


async def test_capture() -> None:
    """Observations, and the skip, through the real run() over a fake mount."""
    print("\ncapture through run():")
    mount = Mount("20261124_0300.png", "2026-11-24T03:00:00")
    saved: list[dict[str, Any]] = []
    touched: list[tuple[Observation, str]] = []

    async def save(*args: object, **kw: Any) -> str:
        _ = args
        saved.append(kw)
        return "sha"

    async def touch(pool: object, obs: Observation, sha: str) -> None:
        _ = pool
        touched.append((obs, sha))

    # setattr, because these replace module functions for the test's length.
    setattr(fs, "read_steps", mount.read_steps)  # noqa: B010  see above
    setattr(fs, "stat_image", mount.stat_image)  # noqa: B010  see above
    setattr(fs, "read_image", mount.read_image)  # noqa: B010  see above
    setattr(l4, "_archive_save", save)  # noqa: B010  see above
    setattr(l4, "_archive_touch", touch)  # noqa: B010  see above
    l4._LAST_READ.clear()
    chk = CHECKS["layer4.mosaic.water_level"]
    # A pool that is never used (the archive save is the fake above), and no
    # HTTP client, since a mount deployment never reaches for one.
    ctx = cast(CheckContext, SimpleNamespace(pool=object(), http=None))

    first = await chk.run(ctx)
    await asyncio.sleep(0)
    obs = saved[0].get("observation") if saved else None
    check("the first run reads the frame", mount.reads == 1, str(mount.reads))
    check("...and archives it with an observation", isinstance(obs, Observation),
          str([{k: v for k, v in s.items() if k != "content"} for s in saved]))
    want = datetime(2026, 11, 24, 3, tzinfo=timezone.utc)
    check("...whose time is the step's valid time, not the capture time",
          isinstance(obs, Observation) and obs.scan_ts == want, str(obs))
    check("...whose published_at is the file's mtime",
          isinstance(obs, Observation) and obs.published_at is not None
          and int(obs.published_at.timestamp()) == mount.mtime_ns // 10**9, str(obs))
    check("...for this product", isinstance(obs, Observation)
          and obs.product_id == "water_level")

    second = await chk.run(ctx)
    await asyncio.sleep(0)
    check("an unchanged file is not read again", mount.reads == 1, str(mount.reads))
    check("...nor archived again", len(saved) == 1, str(len(saved)))
    check("...and the run repeats the last verdict", second.status == first.status
          and second.metrics == first.metrics)
    check("...says so", "unchanged since" in second.summary, second.summary)
    check("...with this run's own times", second.started_at > first.started_at)
    check("...and moves the frame's last_seen_at, with the saved hash",
          [(o.scan_ts, s) for o, s in touched] == [(want, "sha")], str(touched))

    mount.mtime_ns += 3_600 * 10**9
    _ = await chk.run(ctx)
    check("a new mtime is read again", mount.reads == 2, str(mount.reads))
    mount.content = png(2)
    _ = await chk.run(ctx)
    check("so are new bytes with a new size", mount.reads == 3, str(mount.reads))
    mount.steps = [{"ts": "2026-11-24T04:00:00", "imageName": "20261124_0400.png"}]
    _ = await chk.run(ctx)
    check("so is the next step's file", mount.reads == 4, str(mount.reads))
    other = CHECKS["layer4.mosaic.max_water_level"]
    _ = await other.run(ctx)
    check("each check keeps its own last read", mount.reads == 5, str(mount.reads))
    await test_skip_guards(chk, ctx, mount)


async def test_skip_guards(chk: Any, ctx: CheckContext, mount: Mount) -> None:
    """The skip waits for a save that worked, and for unchanged thresholds.

    Args:
        chk: The water_level check, with a last read cached.
        ctx: The run's context.
        mount: The fake mount, its file unchanged since that read.
    """
    async def failing_save(*args: object, **kw: object) -> None:
        _ = (args, kw)

    gate: asyncio.Future[None] = asyncio.get_running_loop().create_future()

    async def slow_save(*args: object, **kw: object) -> str:
        _ = (args, kw)
        # Bounded, so a test that never opens the gate ends rather than hangs.
        await asyncio.wait_for(gate, 10)
        return "sha"

    setattr(l4, "_archive_save", failing_save)  # noqa: B010  test double
    mount.mtime_ns += 10**9
    _ = await chk.run(ctx)
    await asyncio.sleep(0)
    reads = mount.reads
    _ = await chk.run(ctx)
    check("after a failed archive save the frame is read again", mount.reads == reads + 1,
          f"{reads} -> {mount.reads}")
    setattr(l4, "_archive_save", slow_save)  # noqa: B010  test double
    mount.mtime_ns += 10**9
    _ = await chk.run(ctx)
    reads = mount.reads
    _ = await chk.run(ctx)
    check("while the save is still running the skip holds", mount.reads == reads,
          f"{reads} -> {mount.reads}")
    gate.set_result(None)
    await asyncio.sleep(0)

    real_get = thresholds.get_l4

    def stricter(product: str, knob: str, default: object) -> object:
        return 0.5 if knob == "extreme_threshold" else real_get(product, knob, default)

    setattr(thresholds, "get_l4", stricter)  # noqa: B010  test double
    try:
        reads = mount.reads
        _ = await chk.run(ctx)
        check("a changed threshold reads the frame again", mount.reads == reads + 1,
              f"{reads} -> {mount.reads}")
        _ = await chk.run(ctx)
        check("...and the skip resumes under the new value", mount.reads == reads + 1,
              f"{reads} -> {mount.reads}")
    finally:
        setattr(thresholds, "get_l4", real_get)  # noqa: B010  test double


def test_backfill_parse() -> None:
    """Backfill names: the product's own frames and nothing else."""
    print("\nbackfill parsing:")
    base = "https://x/api/imageData?file="
    for url, want in (
            (base + "water_depth/images/20261124_0300.png",
             ("water_depth/images/20261124_0300.png",
              datetime(2026, 11, 24, 3, tzinfo=timezone.utc))),
            (base + "max_water_depth/images/20261123_2200.png", None),
            (base + "water_depth/images/20261324_0300.png", None),
            (base + "water_depth/images/20261124_0300.png.bak", None),
            (base + "water_level/images/20261124_0300.png", None),
            ("https://x/other?file=water_depth/images/x.png", None)):
        got = parse_origin("water_depth", url)
        check(f"{url[len(base):] if url.startswith(base) else url} -> {want and want[1]}",
              got == want, str(got))
    now = datetime.now(timezone.utc)
    rows: list[Mapping[str, object]] = [
            {"sha256": "a", "origin_url": base + "water_depth/images/20261124_0300.png",
             "first_seen_at": now, "last_seen_at": now},
            {"sha256": "b", "origin_url": None, "first_seen_at": now, "last_seen_at": now},
            {"sha256": "c", "origin_url": base + "water_depth/images/20261124_0400.png",
             "first_seen_at": None, "last_seen_at": now}]
    batch = to_batch("water_depth", rows)
    check("rows missing a field are left out", batch.sha256 == ["a"], str(batch.sha256))
    check("...and the kept row keeps its archive times",
          batch.first_seen_at == [now] and batch.last_seen_at == [now])
    check("an hour later is an hour later", parse_origin(
        "water_depth", base + "water_depth/images/20261124_0400.png") == (
        "water_depth/images/20261124_0400.png",
        datetime(2026, 11, 24, 3, tzinfo=timezone.utc) + timedelta(hours=1)))


def main() -> int:
    """Run every section.

    Returns:
        The process exit status, 1 when any assertion failed.
    """
    test_targets()
    asyncio.run(test_capture())
    test_backfill_parse()
    print(f"\n{len(failures)} FAILED: {', '.join(failures)}" if failures
          else "\nall frame capture assertions passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
