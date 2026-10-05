"""Layer-3 BACKEND processing — the steps between a radar arriving and a product.

LB2 answers "is this radar's data landing on disk". L1/LB1 answer "is a
published product fresh". Between those two sits processing that nothing was
watching, and on 2026-10-05 that gap cost twelve hours:

* ``Gen_X-band_QPE.py`` stopped at 04:07 UTC. LB2 noticed only because it was
  pointed at that script's OUTPUT by mistake, and it reported the stall as five
  radar outages — three of which were arriving within a minute. Fixing LB2's
  target removed the false alarms and also removed the only thing watching the
  producer at all. This module puts that back, at a severity that matches what
  it means.
* The composite driver selects its inputs with ``ls -1rt | tail -1`` and
  applies no staleness guard to them, so a radar can age out of the composite
  silently. XEBY did exactly that between 16:16Z and 16:44Z while we were
  looking at it. Nothing in the system said so.

Two checks, one stage:

``layer3.composite.<radar>``
    Is this radar named in the composite's latest input receipt, and how old is
    the contribution it named? One per radar in
    ``config.COMPOSITE_EXPECTED_RADARS``; target is the bare radar id so the
    dashboard can collapse it under that radar.

``layer3.backend.drops``
    Is ``Gen_X-band_QPE.py`` still producing? One instance covering every
    folder, deliberately INFORMATIONAL — see ``Layer3DropsProducerCheck``.

=== What the receipt can and cannot tell us ===

The receipt records INTENT, not outcome. The driver writes it (lines 79-91)
and then reads it back (line 121), so it says which file ``ls | tail -1``
selected — not which files the composite successfully ingested. If the
composite then fails on one of them, nothing on disk says so: there is no
second file recording what was actually blended, and the only ``.txt`` at that
depth is the receipt itself. A radar present here with a fresh contribution is
therefore evidence that it was OFFERED to the composite, which is strictly
weaker than evidence that it is IN the composite. The check's wording stays
inside what the file supports.
"""
from __future__ import annotations
import asyncio
import os
import re
from datetime import timezone
from typing import Any

from ..config import (BACKEND_SOURCE, COMPOSITE_CONTRIB_FAIL_S,
                      COMPOSITE_CONTRIB_WARN_S, COMPOSITE_EXPECTED_RADARS,
                      COMPOSITE_PIPELINE_LAG_S, COMPOSITE_RECEIPT,
                      COMPOSITE_RECEIPT_SETTLE_S, DROPS_SILENT_INFO_S,
                      DROPS_TREE, RADAR_FOLDER, RADAR_SILENT_FAIL_S, SETTINGS)
from ..errors import humanize_error
from .. import thresholds as _thresholds
from ..registry import register
from .base import Check, CheckResult, utcnow
from .helpers import headroom, worst_of

FS_TIMEOUT_S = 8.0

# Every line in the receipt carries YYYYMMDD then a separator then HHMMSS,
# across all three naming conventions the composite ingests:
#
#     XSCR_volume_20261005-163823_drops.nc        X-band       '-'
#     AQPI.SSCB_20261005_163719_drops.nc          C-band       '_'
#     cfrad_KSOX_20260917_155622_drops.nc         NEXRAD       '_'
#
# One pattern rather than three, because the separator is the only thing that
# varies and an 8-then-6 digit run does not otherwise occur in these names.
# Searched, not matched: the timestamp is mid-name in all three.
_TS_RE = re.compile(r"(\d{8})[-_](\d{6})")

# Receipt version cache, shared by every participation check.
#
# Load-bearing, not an optimisation. Each radar's check asks about the same
# file, and the file is rewritten every 120 s. Reading it once per check would
# let XEBY be judged against one composite and CBAND against the next, so
# "which radars are in the composite" would be stitched together from two
# different composites and could report a set that never existed. Keyed on the
# receipt's own (mtime, size) so every radar in a cycle sees one version.
_receipt_cache: tuple[tuple[float, int], dict[str, Any]] | None = None


def _reset_receipt_cache() -> None:
    """Test hook."""
    global _receipt_cache
    _receipt_cache = None


def _receipt_path() -> str:
    return os.path.join(SETTINGS.backend_root, COMPOSITE_RECEIPT)


class TornReceipt(Exception):
    """The receipt was rewritten while we were reading it."""


def _read_receipt_blocking(path: str) -> dict[str, Any]:
    """Blocking. Parse the receipt, refusing a torn read.

    The writer truncates and then appends one line per radar with no temp file
    and no rename, so a read can land on a short file. The bias is directional,
    not random: appends run X-band, then NEXRAD, then SSCB, so a torn read
    under-reports CBAND and over-reports the X-bands. A check that trusted one
    would invent "CBAND absent from the composite" alarms forever.

    Completeness is not decidable from the content — a complete file and a
    nearly-complete one differ by exactly the line we would be looking for — so
    two mechanisms cover two DIFFERENT tears, and neither alone is enough:

      settle window   we arrived mid-write. The mtime is then only moments old,
                      so wait it out. The stat bracket cannot catch this one:
                      between two appends the mtime is momentarily stable, so a
                      read that starts and finishes inside that gap sees a
                      short file with nothing changing under it.
      stat bracket    the write STARTED during our read. The mtime or size then
                      differs across the read, which is detected rather than
                      guessed.

    A third layer sits outside this function and is what makes the residual
    risk acceptable: the writer could in principle stall more than
    COMPOSITE_RECEIPT_SETTLE_S between two appends (nine ``ls`` calls over NFS),
    and such a read would look clean. But an alarm needs the condition to
    persist through the 5-minute hold-down — two to three cycles of this check
    — so a one-off torn read self-clears long before it can notify anyone.
    """
    st0 = os.stat(path)
    # Arrived mid-rewrite: wait out the window rather than read a short file.
    # The write is a handful of filesystem ops, so this settles almost at once.
    age = utcnow().timestamp() - st0.st_mtime
    if 0 <= age < COMPOSITE_RECEIPT_SETTLE_S:
        import time
        time.sleep(COMPOSITE_RECEIPT_SETTLE_S - age)
        st0 = os.stat(path)

    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        raw = fh.read()

    st1 = os.stat(path)
    if (st1.st_mtime, st1.st_size) != (st0.st_mtime, st0.st_size):
        raise TornReceipt(
            f"receipt changed during read (mtime {st0.st_mtime}->{st1.st_mtime}, "
            f"size {st0.st_size}->{st1.st_size})")

    lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    # First line is a header ("Radar files for composite:"), not a path. Keyed
    # off "is it a path" rather than a line index, so a writer that drops or
    # reworders the header does not shift every entry by one.
    contributions: dict[str, dict[str, Any]] = {}
    unparsed: list[str] = []
    for ln in lines:
        if not ln.startswith("/"):
            continue
        base = os.path.basename(ln)
        # The radar is the PARENT DIRECTORY, not the filename: the three naming
        # conventions spell the radar differently inside the name (XSCR_volume,
        # AQPI.SSCB, cfrad_KSOX) while the directory is uniform. The real path
        # contains a double slash (recentfiles//XSCR) because the script
        # concatenates a path that already ends in "/" — harmless here, since
        # dirname/basename collapse it, but it is why this does not split on
        # "/" and index.
        rdir = os.path.basename(os.path.dirname(ln))
        m = _TS_RE.search(base)
        if not m or not rdir:
            unparsed.append(ln)
            continue
        try:
            from datetime import datetime
            dt = datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S")
        except ValueError:
            unparsed.append(ln)
            continue
        contributions[rdir] = {
            "path": ln,
            "declared_ts": dt.replace(tzinfo=timezone.utc).timestamp(),
        }
    return {
        "contributions": contributions,
        "unparsed": unparsed,
        "receipt_mtime": st1.st_mtime,
        "line_count": len(lines),
    }


async def _receipt(path: str) -> dict[str, Any]:
    """Parsed receipt for this cycle, reusing the cached parse of this version.

    One retry on a torn read: the write window is milliseconds and the cadence
    is 120 s, so a second tear in a row means something other than the normal
    rewrite is happening and the caller should hear about it.
    """
    global _receipt_cache
    st = await asyncio.to_thread(os.stat, path)
    key = (st.st_mtime, st.st_size)
    if _receipt_cache is not None and _receipt_cache[0] == key:
        return _receipt_cache[1]
    try:
        parsed = await asyncio.to_thread(_read_receipt_blocking, path)
    except TornReceipt:
        parsed = await asyncio.to_thread(_read_receipt_blocking, path)
    _receipt_cache = ((parsed["receipt_mtime"], st.st_size), parsed)
    return parsed


def _contrib_bands(radar_id: str) -> tuple[float, float]:
    """(warn_s, fail_s) for this radar's contribution age.

    Derived per radar rather than one global number, because the inputs differ
    by a factor of three: a contribution cannot be fresher than the radar's own
    arrival, and those cadences run from XEBY's 5 min to CBAND's 18 min.
    Judging CBAND by an X-band band would call a healthy C-band contribution
    late on every cycle.

        fail = that radar's silence limit + the pipeline budget
        warn = 0.8x fail, the same band shape LB1/LB2 already use

    COMPOSITE_CONTRIB_{WARN,FAIL}_S override per radar when measurement says
    one needs its own number; both are empty by default.
    """
    silent_s = _thresholds.get_radar(
        radar_id, "backend_silent_s", RADAR_SILENT_FAIL_S.get(radar_id, 900))
    fail_s = float(COMPOSITE_CONTRIB_FAIL_S.get(
        radar_id, silent_s + COMPOSITE_PIPELINE_LAG_S))
    warn_s = float(COMPOSITE_CONTRIB_WARN_S.get(radar_id, fail_s * 0.8))
    return warn_s, fail_s


class Layer3CompositeParticipationCheck(Check):
    """Is this radar being offered to the composite, and how fresh is what it offers?

    Two sub-checks:

    ``A_included``  the radar's directory appears in the latest receipt.
                    Absent is a fail: the composite is running without it, which
                    is how XEBY left the AQPI composite on 2026-10-05 without a
                    single alarm anywhere in the system.
    ``B_fresh``     how old the contribution it named is, against bands derived
                    from that radar's own silence limit.

    ``A_included`` is reported even when ``B_fresh`` cannot be: a radar present
    with an unparseable filename is a different fault from a radar missing.
    """

    stage = "LB3"
    depends_on: list[str] = []

    def __init__(self, radar_id: str):
        self.radar_id = radar_id
        self.receipt_dir = COMPOSITE_EXPECTED_RADARS[radar_id]
        self.id = f"layer3.composite.{radar_id}"
        # Bare radar id, so the dashboard can group this row with that radar's
        # LB2 arrival row under one expander.
        self.target = radar_id
        self.cadence_s = 120
        # The receipt lives under backend_root on whichever share this profile
        # publishes to. Unlike LB2, CBAND is NOT special here: participation
        # reads the one shared receipt, so every radar's answer comes off the
        # same mount. That is also why CBAND cannot serve as an independent
        # witness for this check the way it does for arrival.
        self.source_tag, self.source_label = BACKEND_SOURCE

    async def run(self, ctx) -> CheckResult:
        t0 = utcnow()
        sub: dict[str, str] = {}
        metrics: dict[str, float] = {}
        path = _receipt_path()
        payload: dict[str, Any] = {
            "path": path, "receipt_dir": self.receipt_dir,
            "source": "backend-filesystem",
        }

        try:
            parsed = await asyncio.wait_for(_receipt(path), FS_TIMEOUT_S)
        except asyncio.TimeoutError:
            return CheckResult(
                check_id=self.id, target=self.target, stage=self.stage,
                status="error", started_at=t0, finished_at=utcnow(),
                summary=f"receipt read timed out after {FS_TIMEOUT_S:g}s — mount may be hung",
                payload=payload, metrics={"fs_timeout": 1.0})
        except TornReceipt as e:
            # Twice in a row. Not a verdict about the radar: we could not read
            # a consistent receipt, which is a gap in visibility.
            return CheckResult(
                check_id=self.id, target=self.target, stage=self.stage,
                status="error", started_at=t0, finished_at=utcnow(),
                summary=f"receipt rewritten under us twice — {e}",
                payload=payload, metrics={"fs_timeout": 0.0})
        except FileNotFoundError:
            # The composite is not running at all, or not where we think. That
            # is a real fault but it is not THIS radar's fault, and six radars
            # reporting it would be six rows saying one thing.
            metrics["fs_timeout"] = 0.0
            payload["receipt_absent"] = True
            sub["A_included"] = "fail"
            return _final(self, t0, sub, payload, metrics,
                          f"composite receipt absent — {path}")
        except OSError as e:
            return CheckResult(
                check_id=self.id, target=self.target, stage=self.stage,
                status="error", started_at=t0, finished_at=utcnow(),
                summary=f"receipt read failed: {humanize_error(e)}",
                payload=payload, metrics={"fs_timeout": 0.0})

        metrics["fs_timeout"] = 0.0
        contrib = parsed["contributions"].get(self.receipt_dir)
        payload["receipt_radars"] = sorted(parsed["contributions"])
        payload["receipt_age_s"] = round(t0.timestamp() - parsed["receipt_mtime"], 1)

        warn_s, fail_s = _contrib_bands(self.radar_id)
        payload["contrib_warn_s"], payload["contrib_fail_s"] = warn_s, fail_s

        if contrib is None:
            sub["A_included"] = "fail"
            metrics["included"] = 0.0
            others = ", ".join(payload["receipt_radars"]) or "none"
            return _final(self, t0, sub, payload, metrics,
                          f"NOT IN COMPOSITE — {self.receipt_dir} absent from the "
                          f"latest receipt (present: {others})")

        sub["A_included"] = "pass"
        metrics["included"] = 1.0
        age_s = t0.timestamp() - contrib["declared_ts"]
        metrics["contrib_age_s"] = float(age_s)
        # Same normalized 0..1 sparkline axis as LB1/LB2. See helpers.headroom.
        metrics["headroom"] = headroom(age_s, fail_s)
        payload["contrib_path"] = contrib["path"]
        payload["age_s"] = round(age_s, 1)

        if age_s <= warn_s:
            sub["B_fresh"] = "pass"
        elif age_s <= fail_s:
            sub["B_fresh"] = "warn"
        else:
            sub["B_fresh"] = "fail"

        summary = (f"in composite, contribution {age_s / 60:.1f} min old"
                   f"  (limit {fail_s / 60:.0f} min)")
        if sub["B_fresh"] == "fail":
            summary = "STALE CONTRIBUTION — " + summary
        return _final(self, t0, sub, payload, metrics, summary)


class Layer3DropsProducerCheck(Check):
    """Is Gen_X-band_QPE.py still producing? INFORMATIONAL by design.

    === Why this is capped at warn ===

    DROPS holds this script's output and nothing in the live product chain
    reads it, so a stall here does not make any published product wrong. LB2
    used to gate radar health on it, which is how one dead script became five
    radar failures for twelve hours.

    The lesson is not "stop watching DROPS" — it is "watch it at the right
    severity". In this codebase's severity model a ``warn`` maps to ``info``
    and is never auto-promoted, while a ``fail`` is promoted to critical after
    30 minutes; a producer dead for 12 h would therefore page on a ``fail``.
    So the ceiling here is a deliberate severity decision, not an observation:
    ``payload.age_s`` carries the true age for the reprocess engine, and the
    summary states the real duration in words so the row is never gentler than
    the fact. ``alerts.yaml`` additionally routes this check id to a
    non-escalating policy, so it cannot page even if this ceiling is lifted —
    two independent statements of the same intent, because one of them will
    eventually be edited by someone who does not know about the other.

    One check for the whole producer, not one per folder. The folders freeze
    together because it is one process, so per-folder checks would be five rows
    restating one fact — the duplication the fleet correlation exists to
    prevent. Per-folder detail is in ``sub_status`` instead.

    Directory mtime is a sound basis here: DROPS entries are CREATED and never
    overwritten in place (verified against a 629-entry day directory — every
    entry carries its own scan timestamp, there is no fixed-name entry, and
    there are no dotfiles), so the mtime is a true "something last landed".
    """

    id = "layer3.backend.drops"
    target = "drops-qpe"
    stage = "LB3"
    cadence_s = 300
    depends_on: list[str] = []

    def __init__(self):
        self.source_tag, self.source_label = BACKEND_SOURCE

    async def run(self, ctx) -> CheckResult:
        t0 = utcnow()
        sub: dict[str, str] = {}
        metrics: dict[str, float] = {}
        root = os.path.join(SETTINGS.backend_root, DROPS_TREE)
        payload: dict[str, Any] = {"path": root, "source": "backend-filesystem",
                                   "informational": True}

        # Only the X-band folders: DROPS is Gen_X-band_QPE.py's output and
        # CBAND does not pass through it.
        folders = {rid: RADAR_FOLDER[rid] for rid in sorted(RADAR_FOLDER)
                   if rid != "CBAND"}

        def read_all() -> dict[str, float | None]:
            out: dict[str, float | None] = {}
            for rid, folder in folders.items():
                try:
                    out[rid] = os.stat(os.path.join(root, folder)).st_mtime
                except OSError:
                    out[rid] = None
            return out

        try:
            mtimes = await asyncio.wait_for(
                asyncio.to_thread(read_all), FS_TIMEOUT_S)
        except asyncio.TimeoutError:
            return CheckResult(
                check_id=self.id, target=self.target, stage=self.stage,
                status="error", started_at=t0, finished_at=utcnow(),
                summary=f"DROPS read timed out after {FS_TIMEOUT_S:g}s — mount may be hung",
                payload=payload, metrics={"fs_timeout": 1.0})

        metrics["fs_timeout"] = 0.0
        limit = float(DROPS_SILENT_INFO_S)
        payload["silent_s"] = limit
        ages: dict[str, float] = {}
        for rid, mt in sorted(mtimes.items()):
            if mt is None:
                sub[rid] = "warn"
                continue
            age = t0.timestamp() - mt
            ages[rid] = round(age / 60, 1)
            # Capped at warn on purpose — see the class docstring.
            sub[rid] = "pass" if age <= limit else "warn"
        payload["folder_age_min"] = ages
        if ages:
            newest = min(ages.values()) * 60
            payload["age_s"] = round(newest, 1)
            metrics["age_s"] = float(newest)
            metrics["headroom"] = headroom(newest, limit)

        stalled = sorted(r for r, v in sub.items() if v != "pass")
        if not stalled:
            summary = (f"producing — newest output "
                       f"{min(ages.values()):.1f} min ago" if ages else "producing")
        else:
            worst = max(ages.values()) if ages else 0.0
            summary = (f"DROPS PRODUCER STALLED — {len(stalled)}/{len(folders)} "
                       f"folders quiet, oldest {worst / 60:.1f} h "
                       f"({', '.join(stalled)}). Informational: no live product "
                       f"reads this tree.")
        return _final(self, t0, sub, payload, metrics, summary)


def _final(check: Check, t0, sub: dict[str, str], payload: dict, metrics: dict,
           summary: str) -> CheckResult:
    payload["sub_status"] = sub
    return CheckResult(
        check_id=check.id, target=check.target, stage=check.stage,
        status=worst_of(*sub.values()) if sub else "pass",
        started_at=t0, finished_at=utcnow(),
        summary=summary, payload=payload, metrics=metrics,
    )


# --------------------------------------------------------------------------
# Registration. Gated on backend_root like LB1/LB2 — a profile with no mounted
# tree has nothing to read. NOT gated on RADARCA_ONLY_MODULES: these read the
# filesystem and are exactly the checks that must survive a profile with no
# HTTP origin.
# --------------------------------------------------------------------------
if SETTINGS.backend_root:
    for _rid in sorted(COMPOSITE_EXPECTED_RADARS):
        register(Layer3CompositeParticipationCheck(radar_id=_rid))
    # DROPS_TREE is empty on a profile whose composite has no such step, which
    # is a statement each profile makes for itself rather than inheriting from
    # a profile-name comparison.
    if DROPS_TREE:
        register(Layer3DropsProducerCheck())
