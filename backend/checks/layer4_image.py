"""Layer 4 Tier 1 + Tier 2 — image-based health on a single product/radar.

One parameterized Check class registered for:
  * each X-band radar (kind="xband") — fetches latest CorrReflectivity
  * each mosaic product (kind="mosaic") — fetches latest scan PNG

Each cycle:
  1. Resolve "latest scan" via the appropriate radarca endpoint
  2. Fetch the PNG bytes
  3. Compute Tier 1 stats
  4. Compute Tier 2 heuristics
  5. Compare pHash to previous run (frozen-frame across-run detection)
  6. Emit one CheckResult — status is OK unless a heuristic verdict trips
"""
from __future__ import annotations
import asyncio
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any

from ..archive import (  # noqa: TID252  file convention
    Observation, save_image as _archive_save, touch_observation as _archive_touch)
from ..config import (HAS_RADARCA, L4_MOMENT,  # noqa: TID252  file convention
                      L4_MOSAIC_TARGETS, L4_XBAND_TARGETS, PRODUCTS,
                      RADAR_FOLDER, SETTINGS, image_path, l4_profile)
from .. import fs_products as _fs  # noqa: TID252  file convention
from .transports import CheckContext
from .. import thresholds as _thresholds
from ..registry import register
from .base import Check, CheckResult, utcnow
from .helpers import worst_of
from .imaging import tier1_stats, tier2_heuristics


# Module-level in-memory store of last pHash per (check_id, target). Survives
# the process; will be moved to image_archive in a later milestone (P4.0).
_LAST_PHASH: dict[str, str] = {}
# The SOURCE the previous pHash came from. Without this, re-sampling one
# published image reports it as frozen against itself — see the frozen-frame
# block below.
_LAST_SOURCE: dict[str, str] = {}
# A frame file on the mount as (source, size, mtime_ns).
FrameKey = tuple[str, int, int]
# The QC thresholds a read judged a frame with: extreme_threshold,
# skip_frozen, frozen_min_cov_pct, skip_range_ring.
Knobs = tuple[float, bool, float, bool]
# Archive saves and touches in flight. The event loop holds only a weak
# reference to a task, so one with no other reference can be collected
# before it finishes.
_BACKGROUND: set[asyncio.Task[str | None] | asyncio.Task[None]] = set()


@dataclass(frozen=True)
class Frame:
    """One frame a check fetched, or chose not to read because it is unchanged.

    Attributes:
        content: The PNG bytes, or None when the frame matched the last read.
        source: The source path or file name it came from.
        scan_ts: The valid time of the step it draws, for a product frame.
        published_at: The file's mtime on the mount, UTC.
        key: The file's stat key on the mount, for the unchanged-frame skip.
    """

    content: bytes | None
    source: str
    scan_ts: datetime | None = None
    published_at: datetime | None = None
    key: FrameKey | None = None


@dataclass(frozen=True)
class LastRead:
    """The last product frame a check read off the mount, and what came of it.

    Attributes:
        key: The file's stat key when it was read.
        knobs: The thresholds the read judged it with.
        result: The result the read produced.
        frame: The frame, for its product and valid time.
        save: The archive save the read started, or None with no archive.
    """

    key: FrameKey
    knobs: Knobs
    result: CheckResult
    frame: Frame
    save: asyncio.Task[str | None] | None

    def saved_sha(self) -> str | None:
        """The archived frame's sha256, once its save has finished and worked."""
        s = self.save
        if s is None or not s.done() or s.cancelled() or s.exception() is not None:
            return None
        return s.result()

    def reusable(self, key: FrameKey, knobs: Knobs) -> bool:
        """Whether a run may repeat this read instead of reading the file.

        Only for the same file at the same thresholds, and only while the
        read's archive save is in flight or worked. After a failed save the
        frame is read again, so its observation row is written.

        Args:
            key: The file's stat key now.
            knobs: The thresholds now.

        Returns:
            True when the read can be repeated.
        """
        if key != self.key or knobs != self.knobs:
            return False
        return self.save is None or not self.save.done() or self.saved_sha() is not None


# The last product frame each check read off the mount (see LastRead).
_LAST_READ: dict[str, LastRead] = {}


def _keep(task: asyncio.Task[str | None] | asyncio.Task[None]) -> None:
    """Hold a background task until it finishes, so it is not collected early."""
    _BACKGROUND.add(task)
    task.add_done_callback(_BACKGROUND.discard)


def _parse_step_ts(s: dict) -> datetime | None:
    """Raw upstream mosaic step dicts carry `timestamp`, not `ts` (the latter
    is what our /api/upstream/product_steps proxy emits to the frontend, but
    we hit productDetail directly here). Treat as UTC, parse, return None on
    missing / unparseable."""
    ts = s.get("timestamp") or s.get("ts")
    if not ts:
        return None
    try:
        # Normalize trailing Z + missing tz to UTC.
        if ts.endswith("Z"):
            ts = ts[:-1] + "+00:00"
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (TypeError, ValueError):
        return None


def _step_closest_to_now(steps: list[dict]) -> dict | None:
    """Pick the step whose timestamp is nearest to wall-clock now. Falls back
    to `steps[-1]` if no step has a parseable timestamp (observed products
    don't always include `ts`)."""
    if not steps:
        return None
    now = datetime.now(timezone.utc)
    best: tuple[float, dict] | None = None
    for s in steps:
        dt = _parse_step_ts(s)
        if dt is None:
            continue
        delta = abs((dt - now).total_seconds())
        if best is None or delta < best[0]:
            best = (delta, s)
    if best is not None:
        return best[1]
    return steps[-1]


class Layer4ImageCheck(Check):
    """Either an X-band scan check (kind=xband) or a mosaic-product check."""

    stage = "L4-T1T2"
    cadence_s = 120
    depends_on: list[str] = []

    def __init__(self, *, kind: str, identifier: str):
        assert kind in ("xband", "mosaic"), kind
        self.kind = kind
        # _fetch_image reads the mount whenever there is one.
        self.radarca_readings = not _fs.available()
        self.id = f"layer4.{kind}.{identifier}"
        self.target = identifier
        self.identifier = identifier
        # depends_on lists both the immediate predecessor (L2 / L1) AND
        # layer0.origin.alive directly. Cascade-demote's BFS would find
        # origin via the L2/L1 chain on its own — but listing it directly
        # makes the dependency relationship explicit in the registry, so
        # introspection tools and the /m/uptime focus-mode pull the right
        # set of "related" checks without traversing two hops first.
        if kind == "xband":
            self.depends_on = [
                f"layer2.radar.{identifier}",
                "layer0.origin.alive",
            ]
            self.folder = RADAR_FOLDER[identifier]
        else:
            self.depends_on = [
                f"layer1.product.{identifier}",
                "layer0.origin.alive",
            ]

    # ------------------------------------------------------------------
    async def _fs_radar_image(self) -> Frame | str:
        """The newest rendered frame for this radar, off the mount."""
        frames = await _fs.list_moment_frames(self.identifier, L4_MOMENT)
        if not frames:
            return f"no {L4_MOMENT} frames in the window"
        ts, name = frames[-1]
        fr = await _fs.read_moment_image(self.identifier, L4_MOMENT, name, ts)
        if fr.status_code != 200:
            return f"frame unreadable on the mount (HTTP {fr.status_code})"
        return Frame(fr.content, name)

    async def _fs_product_image(self, knobs: Knobs) -> Frame | str:
        """The frame nearest now for this product, off the mount.

        The file is stat'ed first. When its name, size and mtime match the
        last frame this check read at the same thresholds, the bytes are not
        read again: a CoSMoS frame changes hourly and this runs every 120 s,
        and K2 has a saturation history (wiki incidents/48-).

        Args:
            knobs: The thresholds this run judges with.

        Returns:
            The frame, with no content when the last read can be repeated, or
            an error message.
        """
        steps = await _fs.read_steps(self.identifier)
        if not steps:
            return "no steps in the published manifest"
        target_step = _step_closest_to_now(steps)
        if target_step is None:
            return "no parseable step timestamps"
        file_path = image_path(self.identifier, target_step["imageName"])
        scan_ts = _parse_step_ts(target_step)
        st = await _fs.stat_image(file_path)
        key = None if st is None else (file_path, st.size, st.mtime_ns)
        published = (None if st is None
                     else datetime.fromtimestamp(st.mtime_ns / 1e9, tz=timezone.utc))
        last = _LAST_READ.get(self.id)
        if key is not None and last is not None and last.reusable(key, knobs):
            return Frame(None, file_path, scan_ts, published, key)
        ir = await _fs.read_image(file_path)
        if ir.status_code != 200:
            return f"frame unreadable on the mount (HTTP {ir.status_code})"
        return Frame(ir.content, file_path, scan_ts, published, key)

    async def _fs_image(self, knobs: Knobs) -> Frame | str:
        """The newest frame off the mount, for a deployment that has one.

        LB1 answers whether a file arrived and never opens it, so a producer
        writing truncated or empty PNGs stayed green there. The XQPI deployment showed
        that live: requests for the newest composite frame returned 502 from
        the PNG signature check while LB1 called the product healthy, because
        no check on that deployment read a frame's bytes at all.
        """
        if self.kind == "xband":
            return await self._fs_radar_image()
        return await self._fs_product_image(knobs)

    async def _fetch_image(self, ctx: CheckContext, knobs: Knobs) -> Frame | str:
        """Return the frame, or an error message.

        Prefers the mount, the same rule the serving path uses, so this check
        inspects the bytes an operator will actually be shown. A mount that
        fails is reported rather than quietly retried against radarca, since
        the point of running here is to notice when the tree is wrong.
        """
        if _fs.available():
            try:
                return await self._fs_image(knobs)
            except (OSError, TimeoutError, KeyError, ValueError) as e:
                return f"mount read failed: {e}"
        return await self._http_image(ctx)

    async def _http_image(self, ctx: CheckContext) -> Frame | str:
        """The newest frame from radarca, for a deployment with no mount."""
        if self.kind == "xband":
            return await self._http_radar_image(ctx)
        return await self._http_product_image(ctx)

    async def _http_radar_image(self, ctx: CheckContext) -> Frame | str:
        """The newest radar frame from radarca's xband endpoint."""
        r = await ctx.http.get(
            f"{SETTINGS.base}/api/xbandRadarImages/",
            params={"radarFolder": self.folder, "productPrefix": "CorrReflectivity"},
        )
        if r.status_code != 200:
            return f"xbandRadarImages HTTP {r.status_code}"
        imgs = r.json().get("images") or []
        if not imgs:
            return "no images in 1h window"
        latest_file = imgs[-1]
        ir = await ctx.http.get(f"{SETTINGS.base}/api/imageData",
                                params={"file": latest_file})
        if ir.status_code != 200 or not ir.headers.get("content-type", "").startswith("image/png"):
            return f"imageData HTTP {ir.status_code}"
        return Frame(ir.content, latest_file)

    async def _http_product_image(self, ctx: CheckContext) -> Frame | str:
        """The frame nearest now for this product, from radarca."""
        cfg = PRODUCTS[self.identifier]
        r = await ctx.http.get(
            f"{SETTINGS.base}/api/productDetail", params={"file": cfg["details"]},
        )
        if r.status_code != 200:
            return f"productDetail HTTP {r.status_code}"
        steps = r.json().get("steps", [])
        if not steps:
            return "no steps"
        # Pick the step closest to wall-clock now (in either direction) rather
        # than steps[-1]. For observed products (comp_ref, qpe_*) `steps[-1]`
        # IS roughly "now" so this is a no-op. For forecast products
        # (water_depth, comp_now, max_water_depth, water_level), `steps[-1]`
        # is the furthest-out forecast horizon — e.g. water_depth's `-1` is
        # tomorrow 06:00, not the current depth. QC-ing tomorrow's forecast
        # as if it were the current state is what made these tickers warn.
        target_step = _step_closest_to_now(steps)
        if target_step is None:
            return "no parseable step timestamps"
        latest = target_step["imageName"]
        file_path = image_path(self.identifier, latest)
        ir = await ctx.http.get(f"{SETTINGS.base}/api/imageData",
                                params={"file": file_path})
        if ir.status_code != 200 or not ir.headers.get("content-type", "").startswith("image/png"):
            return f"imageData HTTP {ir.status_code}"
        return Frame(ir.content, file_path, _parse_step_ts(target_step))

    # ------------------------------------------------------------------
    def _unchanged(self, ctx: CheckContext, t0: datetime) -> CheckResult | None:
        """The last read's result again, for a frame whose file has not changed.

        Also moves the frame's last_seen_at, as a read would have, once the
        read's archive save has given its sha256.

        Args:
            ctx: The run's context, for the pool.
            t0: When this run started.

        Returns:
            The last result with this run's times and an "unchanged since"
            note, or None when there is no last read to repeat.
        """
        last = _LAST_READ.get(self.id)
        if last is None:
            return None
        sha = last.saved_sha()
        if sha is not None and ctx.pool is not None and last.frame.scan_ts is not None:
            obs = Observation(self.identifier, last.frame.scan_ts, last.frame.published_at)
            _keep(asyncio.create_task(_archive_touch(ctx.pool, obs, sha)))
        prev = last.result
        return replace(
            prev, started_at=t0, finished_at=utcnow(),
            summary=f"{prev.summary}  unchanged since {prev.finished_at:%H:%M}Z",
            payload={**prev.payload, "unchanged_since": prev.finished_at.isoformat()},
        )

    def _skip(self, t0: datetime, reason: str) -> CheckResult:
        """A skip result for a run that has no frame to judge.

        Args:
            t0: When this run started.
            reason: Why there is no frame.

        Returns:
            The result.
        """
        return CheckResult(
            check_id=self.id, target=self.target, stage=self.stage,
            status="skip", started_at=t0, finished_at=utcnow(),
            summary=f"no image: {reason}", payload={"reason": reason},
        )

    def _archive(self, ctx: CheckContext, frame: Frame,
                 content: bytes) -> asyncio.Task[str | None] | None:
        """Archive a captured frame in the background.

        The timeline detail panel can then still show it after the upstream
        rotates the file out (radarca keeps only ~2 h of composites and ~1 h
        of X-band scans). A product frame also records the valid time it
        draws, so the archive can list a product's frames by time.

        Args:
            ctx: The run's context, for the pool.
            frame: The frame.
            content: Its bytes.

        Returns:
            The save's task, or None when nothing is archived.
        """
        if not SETTINGS.archive_enabled or ctx.pool is None:
            return None
        obs = (None if self.kind != "mosaic" or frame.scan_ts is None
               else Observation(self.identifier, frame.scan_ts, frame.published_at))
        task = asyncio.create_task(_archive_save(
            ctx.pool, SETTINGS.archive_root, source=frame.source, content=content,
            marker=SETTINGS.storage_marker, content_type="image/png",
            origin_url=f"{SETTINGS.base}/api/imageData?file={frame.source}",
            observation=obs))
        _keep(task)
        return task

    def _knobs(self) -> tuple[dict[str, Any], Knobs]:
        """This product's QC profile and the thresholds a run judges with.

        Forecast products and color-ramp scalar fields get relaxed
        thresholds. Live values come from backend.thresholds, which falls
        through to config.L4_PROFILES on a fresh DB, so behavior is unchanged
        before any admin edit.

        Returns:
            The profile, and extreme_threshold, skip_frozen,
            frozen_min_cov_pct and skip_range_ring.
        """
        prof = l4_profile(self.identifier)
        knobs = (
            float(_thresholds.get_l4(self.identifier, "extreme_threshold",
                                     prof["extreme_threshold"])),
            bool(_thresholds.get_l4(self.identifier, "skip_frozen", prof["skip_frozen"])),
            float(_thresholds.get_l4(self.identifier, "frozen_min_cov_pct",
                                     prof["frozen_min_cov_pct"])),
            bool(_thresholds.get_l4(self.identifier, "skip_range_ring",
                                    prof.get("skip_range_ring", False))),
        )
        return prof, knobs

    async def run(self, ctx) -> CheckResult:
        t0 = utcnow()
        # Read before the frame, so an admin's threshold change makes the
        # unchanged-frame skip read the frame again under the new values.
        prof, knobs = self._knobs()
        frame = await self._fetch_image(ctx, knobs)
        if isinstance(frame, str):
            return self._skip(t0, frame)
        if frame.content is None:
            # Only a check with a last read can return no content, since the
            # skip compares against it.
            repeated = self._unchanged(ctx, t0)
            return repeated or self._skip(t0, "frame unchanged but no earlier result")
        png_bytes, src = frame.content, frame.source
        save = self._archive(ctx, frame, png_bytes)
        extreme_threshold, skip_frozen, frozen_min_cov_pct, skip_range_ring = knobs

        # Offload CPU work to a thread so the asyncio loop stays responsive.
        t1 = await asyncio.to_thread(tier1_stats, png_bytes)
        t2 = await asyncio.to_thread(
            tier2_heuristics, png_bytes, self.kind, extreme_threshold
        )

        # Per-radar opt-out of range-ring detection. CBAND's image geometry
        # (1800×1800, wider FOV, different physics) doesn't match the
        # detector's X-band tuning, so we mark it N/A rather than report
        # spurious RING_PEAKs. All other Tier-2 detectors are
        # scale-invariant and apply unchanged.
        if skip_range_ring:
            t2["range_ring"] = {"verdict": "N/A", "skipped_by_profile": True}

        # Demote SATURATED to OK_LOW_COV on sparse scenes: when only a few
        # percent of pixels are active, "40% of them are in one color bin"
        # is statistical noise rather than a real anomaly. Same coverage
        # floor as the frozen-frame gate.
        if (t2["extreme"]["verdict"] == "SATURATED"
                and t1["coverage_pct"] < frozen_min_cov_pct):
            t2["extreme"]["verdict"] = "OK_LOW_COV"

        # Frozen-frame detection — gated by coverage and by profile.
        # A pHash match on a near-empty frame is expected ("calm world,"
        # not "stuck feed"); a pHash match on a slow-cadence forecast
        # product is also expected. Both demote to QUIET, which counts as
        # pass in the rollup.
        prev_phash = _LAST_PHASH.get(self.id)
        prev_source = _LAST_SOURCE.get(self.id)

        # === Load-bearing: only compare DIFFERENT published frames ===
        #
        # FROZEN means "upstream published a new frame and it is pixel
        # identical to the last one" — a stuck feed. It does NOT mean "we
        # looked at the same file twice", which is guaranteed to match and
        # says nothing at all.
        #
        # This check runs every 120s, but publish cadences vary: CBAND
        # publishes every ~240s, so half of its runs re-sampled the frame
        # they had already seen and reported it as frozen against itself.
        # Measured over 24h: of 366 runs where the source was unchanged,
        # 366 flagged FROZEN; of 352 runs with a genuinely new image, 0 did.
        # That was 1,520 warnings in 7 days, 42x the next noisiest radar,
        # and every one of them was the detector comparing a frame to
        # itself.
        #
        # Tuning CBAND's thresholds would have hidden this rather than
        # fixed it, and would have suppressed real stuck-feed detection
        # during exactly the high-coverage weather where it matters most.
        if prev_source is not None and src == prev_source:
            frozen_verdict = "OK_SAME_FRAME"       # nothing new to compare
        elif prev_phash is not None and prev_phash == t1["phash"]:
            if skip_frozen:
                frozen_verdict = "QUIET_SLOW"      # slow-cadence product
            elif t1["coverage_pct"] < frozen_min_cov_pct:
                frozen_verdict = "QUIET_LOW_COV"   # quiet sky, just clutter
            else:
                frozen_verdict = "FROZEN"          # genuine stuck feed
        else:
            frozen_verdict = "OK"
        _LAST_PHASH[self.id] = t1["phash"]
        if src:
            _LAST_SOURCE[self.id] = src

        sub_verdicts: list[str] = []
        # Verdicts we treat as "pass" (no anomaly worth alarming on).
        OK_VERDICTS = (
            "OK", "N/A", "EMPTY", "TOO_SPARSE",
            "QUIET_LOW_COV", "QUIET_SLOW", "OK_LOW_COV",
            "OK_SAME_FRAME",
        )
        def _v(name: str, verdict: str):
            sub_verdicts.append("pass" if verdict in OK_VERDICTS else "warn")

        _v("extreme",    t2["extreme"]["verdict"])
        _v("speckle",    t2["speckle"]["verdict"])
        _v("range_ring", t2["range_ring"]["verdict"])
        _v("frozen",     frozen_verdict)

        overall = worst_of(*sub_verdicts) if sub_verdicts else "pass"

        result = CheckResult(
            check_id=self.id, target=self.target, stage=self.stage,
            status=overall,
            started_at=t0, finished_at=utcnow(),
            summary=(
                f"cov={t1['coverage_pct']}% autocorr={t1['autocorr']}  "
                f"ext={t2['extreme']['verdict']}  spk={t2['speckle']['verdict']}  "
                f"ring={t2['range_ring']['verdict']}  frozen={frozen_verdict}"
            ),
            payload={
                "source": src,
                "profile": prof,
                "tier1": t1,
                "tier2": {
                    **t2,
                    "frozen": {"verdict": frozen_verdict, "prev_phash": prev_phash,
                               "prev_source": prev_source},
                },
            },
            metrics={
                "coverage_pct": float(t1["coverage_pct"]),
                "mean_value":   float(t1["mean_value"]),
                "std_value":    float(t1["std_value"]),
                "autocorr":     float(t1["autocorr"]),
            },
        )
        if frame.key is not None:
            _LAST_READ[self.id] = LastRead(frame.key, knobs, result, frame, save)
        return result


# --------------------------------------------------------------------------
# Register: 5 X-band radars + CBAND, plus 3 mosaic products that produce
# real (non-placeholder) imagery. CBAND uses kind="xband" because it
# shares the X-band fetch endpoint (/api/xbandRadarImages/), but its L4
# profile skips the polar range-ring detector (see config.L4_PROFILES).
# --------------------------------------------------------------------------
# Gated here rather than by the module list, because this module now has two
# possible sources and the list has room for one answer. Opening a frame needs
# either a mount to read or an origin to ask; a deployment with neither gets
# no image QC, which is the honest result rather than a check that fails
# forever.
if _fs.available() or HAS_RADARCA:
    for r in L4_XBAND_TARGETS:
        register(Layer4ImageCheck(kind="xband", identifier=r))

    for p in L4_MOSAIC_TARGETS:
        register(Layer4ImageCheck(kind="mosaic", identifier=p))
