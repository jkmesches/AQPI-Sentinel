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
* The composite driver selects its inputs with ``ls -1rt | tail -1``, so a
  radar can age out of the composite silently. XEBY did exactly that between
  16:16Z and 16:44Z while we were looking at it. Nothing in the system said so.

  CORRECTION, 2026-10-09. An earlier version of this paragraph said the
  composite "applies no staleness guard to its inputs". It does: Renzo Bechini,
  who wrote it, reports a 15-minute delay check INSIDE the composite program —
  downstream of the ``ls -1rt`` selection in the driver, which is why reading
  the driver alone says otherwise. Files exceeding it are discarded and omitted
  from the output's ``RadarNamesInComposite`` attribute. Confirmed by
  measurement on 2026-10-12: XSWR was excluded from four consecutive composites
  on 2026-10-08 and readmitted once producing again, so no published composite
  was built from the 148-minute-old scan we had reported. The consequence for
  THIS module is in ``Layer3CompositeParticipationCheck``: a run-log block
  proves the file was READ, which is upstream of that discard, and is therefore
  weaker evidence than it was written to be.

Two checks, one stage:

``layer3.composite.<radar>``
    Is this radar named in the composite's latest input receipt, and how old is
    the contribution it named? One per radar in
    ``config.COMPOSITE_EXPECTED_RADARS``; target is the bare radar id so the
    dashboard can collapse it under that radar.

``layer3.backend.drops``
    Is ``Gen_X-band_QPE.py`` still producing? One instance covering every
    folder, deliberately INFORMATIONAL — see ``Layer3DropsProducerCheck``.

=== Two sources, and the better one answers a stronger question ===

PRIMARY — the composite's own per-run log (``config.COMPOSITE_RUN_LOG_DIR``).
One file per run, with a block per radar the composite actually processed,
including the netCDF dimensions it read and the contribution age it computed
itself. A block is evidence of INGESTION. These files are written once per run
and the next run is 120 s later, so reading the second-newest is always a
complete file — there is no torn read to defend against if you never read the
file that is being written.

FALLBACK — the input receipt (``config.COMPOSITE_RECEIPT``). This records
INTENT, not outcome: the driver writes it and then reads it back, so it says
which file ``ls -1rt | tail -1`` selected, not what was successfully ingested.
A radar present there is evidence it was OFFERED to the composite, which is
strictly weaker. It is also truncate-then-append with no rename, so it carries
a torn-read hazard the run log does not — see ``COMPOSITE_RECEIPT_SETTLE_S``.

The check's wording follows whichever source answered, and the source is
recorded in ``payload.participation_source``. A row must not claim "in the
composite" on evidence that only supports "offered to" it.

What NEITHER source carries is a statement of absence. The composite's
``ls: cannot access`` lines go to stderr and land in a rolling log that is
truncated every run, so it is useless as history. Absence is inferred from a
missing radar block, which is still positive evidence because the block
appears for every radar that was ingested.
"""
from __future__ import annotations
import asyncio
import os
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from ..config import (BACKEND_SOURCE, COMPOSITE_CONTRIB_FAIL_S,
                      COMPOSITE_CONTRIB_OVERRIDE_S, COMPOSITE_CONTRIB_WARN_S,
                      COMPOSITE_EXPECTED_RADARS, COMPOSITE_INPUT_DIR,
                      COMPOSITE_INPUT_RETENTION_S, COMPOSITE_INPUT_STALE_WARN_S,
                      COMPOSITE_INPUT_HOST_SUBPATH, COMPOSITE_INPUT_TS_RE,
                      COMPOSITE_RECEIPT,
                      COMPOSITE_RECEIPT_SETTLE_S, COMPOSITE_RUN_LOG_DIR,
                      DROPS_SILENT_INFO_S, DROPS_TREE, MAX_RUN_LOG_ENTRIES,
                      RADAR_FOLDER, SETTINGS)
from ..config import RAW_VOLUME_TS_RE
from ..errors import humanize_error
# One implementation of "newest observation these filenames declare", shared
# rather than copied. LB2 owns it because that is where the basis was
# established; checks/__init__ imports layer2_backend_radar first, so this is
# resolved by the time this module loads.
from .layer2_backend_radar import _newest_declared
from .. import thresholds as _thresholds
from ..registry import CHECKS, register
from .base import Check, CheckResult, utcnow
from .helpers import first_resolvable, headroom, worst_of

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
        "source": "receipt",
        "unparsed": unparsed,
        "receipt_mtime": st1.st_mtime,
        "line_count": len(lines),
    }


async def _participation() -> dict[str, Any]:
    """This cycle's participation facts, from the best source available.

    Prefers the composite's per-run log, which answers the stronger question
    (ingested, not merely offered), carries the composite's own age
    computation, and cannot be read mid-write. Falls back to the receipt when
    the run-log directory is absent, empty, or in an unrecognised format.

    The fallback is TAGGED in `source` rather than substituted silently: the
    two sources support different claims, and a row that says "in the
    composite" on receipt evidence alone would be overstating what was read.

    Cached per source version so every radar in a cycle sees ONE composite
    run. Otherwise XEBY could be judged against one run and CBAND the next,
    and the reported set would be stitched from two runs and could be a set
    that never existed.
    """
    global _receipt_cache
    if COMPOSITE_RUN_LOG_DIR:
        d = os.path.join(SETTINGS.backend_root, COMPOSITE_RUN_LOG_DIR)
        try:
            path, run_ts = await asyncio.to_thread(_pick_run_log, d)
            if _receipt_cache is not None and _receipt_cache[0] == ("run_log", path):
                return _receipt_cache[1]
            parsed = await asyncio.to_thread(_read_run_log_blocking, d)
            _receipt_cache = (("run_log", parsed["run_log"]), parsed)
            return parsed
        except (FileNotFoundError, NotADirectoryError, ValueError, OSError):
            # Fall through to the receipt. Deliberately broad: every one of
            # these means "this source could not answer", and the fallback can.
            pass

    path = _receipt_path()
    st = await asyncio.to_thread(os.stat, path)
    key = ("receipt", f"{st.st_mtime}:{st.st_size}")
    if _receipt_cache is not None and _receipt_cache[0] == key:
        return _receipt_cache[1]
    try:
        parsed = await asyncio.to_thread(_read_receipt_blocking, path)
    except TornReceipt:
        # One retry: the write window is milliseconds against a 120 s cadence,
        # so a second tear means something other than the normal rewrite.
        parsed = await asyncio.to_thread(_read_receipt_blocking, path)
    _receipt_cache = (key, parsed)
    return parsed


# ==========================================================================
# PRIMARY source: the composite's own per-run log
# ==========================================================================
#
# One file per run under COMPOSITE_RUN_LOG_DIR, written once, with a block per
# radar the composite actually processed. See config.COMPOSITE_RUN_LOG_DIR for
# a verbatim block and for why this beats the receipt.

_RADAR_BLOCK_RE = re.compile(r"^\*+\s*RADAR:\s*(\S+?)\s*\*+\s*$", re.M)
# `GetNetCDFdim` with real dimensions is the ingestion evidence: it means the
# file was opened and its header read, not merely named.
_DIM_RE = re.compile(r"#radials\s*=\s*(\d+).*?#gates[^=]*=\s*(\d+)"
                     r"(?:.*?#sweeps\s*=\s*(\d+))?", re.S)
# Logged as start-minus-end, so negative. The sign is not load-bearing; the
# magnitude is the contribution age.
_SECONDS_RE = re.compile(r"secondsStarttoEnd\s*=\s*(-?\d+)")
_SCAN_START_RE = re.compile(r"startDateTimeScan\s*=\s*(\S+)")
_FILENAME_RE = re.compile(r"^\s*filename:\s*(\S+)\s*$", re.M)


def _parse_run_log(text: str, run_ts: float | None) -> dict[str, dict[str, Any]]:
    """Per-radar ingestion facts from one composite run log.

    A radar appears here only if the composite processed it, so the KEYS are
    the ingestion evidence and the absence of a key is the absence of a
    contribution.

    `age_s` comes from the composite's own `secondsStarttoEnd`. `derived_age_s`
    is computed independently as (run time from the filename) minus (the scan
    start the log records), and the two are compared.

    === Why both, rather than trusting the one number ===

    The field is labelled start-minus-END-of-scan, which read literally is a
    SCAN DURATION, not an age — and a band fitted to an age would be measuring
    the wrong quantity entirely. The evidence that it is an age is that one
    radar's distribution reaches 64,828 s: an 18-hour span is impossible for a
    4-sweep X-band volume, so `endDateTimeScan` must be the composite's own
    reference time rather than anything read out of the file. That inference is
    sound but it IS an inference, and the label contradicts it. So the derived
    figure is carried alongside and a disagreement is recorded instead of being
    silently resolved in favour of whichever was computed first.
    """
    out: dict[str, dict[str, Any]] = {}
    marks = list(_RADAR_BLOCK_RE.finditer(text))
    for i, m in enumerate(marks):
        rdir = m.group(1)
        body = text[m.end(): marks[i + 1].start() if i + 1 < len(marks) else len(text)]
        dims = _DIM_RE.search(body)
        rec: dict[str, Any] = {
            # No dimensions means the block exists but the read did not land.
            # Recorded rather than dropped: "named but not read" is a distinct
            # and more alarming state than "not named".
            "ingested": bool(dims),
            "age_s": None,
            "derived_age_s": None,
            "volume_path": None,
        }
        if dims:
            rec["radials"] = int(dims.group(1))
            rec["gates"] = int(dims.group(2))
            if dims.group(3):
                rec["sweeps"] = int(dims.group(3))
        sec = _SECONDS_RE.search(body)
        if sec:
            rec["age_s"] = abs(float(sec.group(1)))
        fn = _FILENAME_RE.search(body)
        if fn:
            rec["volume_path"] = fn.group(1)
        st = _SCAN_START_RE.search(body)
        if st and run_ts is not None:
            try:
                from datetime import datetime
                dt = datetime.strptime(st.group(1).rstrip("Z"), "%Y-%m-%dT%H:%M:%S")
                rec["derived_age_s"] = round(
                    run_ts - dt.replace(tzinfo=timezone.utc).timestamp(), 1)
            except ValueError:
                pass
        if rec["age_s"] is not None and rec["derived_age_s"] is not None:
            # One composite cycle of slack. Beyond that the two fields are not
            # measuring the same thing and someone needs to know.
            rec["age_disagrees"] = abs(rec["age_s"] - rec["derived_age_s"]) > 120
        out[rdir] = rec
    return out


def _pick_run_log(dirpath: str) -> tuple[str, float | None]:
    """Blocking. The SECOND-NEWEST run log, which is always complete.

    The newest file may still be open: the name says `startproc`, so it is
    created when the run begins and written as the run proceeds. The next run
    starts 120 s later, so the previous file is finished — reading it avoids
    the torn-read problem entirely rather than defending against it.

    The cost is up to ~240 s of reporting lag against a 900 s fail band. That
    is accepted deliberately: a late-but-correct answer beats a prompt one
    computed from half a file, and the alternative (newest, with a settle
    window) would need to know whether these files are written incrementally
    or on completion, which is not established.

    Ordering is by the timestamp IN THE NAME, never by mtime. At ~5,000
    retained files a stat per entry over NFS is the read that hangs, and the
    name already carries the run time.
    """
    stamped: list[tuple[float, str]] = []
    n = 0
    with os.scandir(dirpath) as it:
        for e in it:
            n += 1
            if n > MAX_RUN_LOG_ENTRIES:
                break
            if e.name.startswith(".") or not e.name.endswith(".txt"):
                continue
            m = _TS_RE.search(e.name)
            if not m:
                continue
            try:
                from datetime import datetime
                dt = datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S")
            except ValueError:
                continue
            stamped.append((dt.replace(tzinfo=timezone.utc).timestamp(), e.path))
    if not stamped:
        raise FileNotFoundError(f"no timestamped run logs in {dirpath}")
    stamped.sort()
    # One file only: it may be mid-write, but refusing to look at a directory
    # holding exactly one run is worse than reading it. Flagged by the caller.
    ts, path = stamped[-2] if len(stamped) >= 2 else stamped[-1]
    return path, ts


def _read_run_log_blocking(dirpath: str) -> dict[str, Any]:
    """Blocking. Parse the second-newest run log."""
    path, run_ts = _pick_run_log(dirpath)
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        text = fh.read()
    radars = _parse_run_log(text, run_ts)
    if not radars:
        # The file exists and holds no radar blocks. That is a real statement —
        # a run that ingested nothing — but it is indistinguishable from a
        # format change, so it is surfaced rather than reported as six absences.
        raise ValueError(f"no RADAR blocks in {path}")
    return {
        "contributions": radars,
        "source": "run_log",
        "run_log": path,
        "run_ts": run_ts,
        "receipt_mtime": run_ts,
        "line_count": text.count("\n"),
        "unparsed": [],
    }


def _contrib_bands(radar_id: str) -> tuple[float, float] | None:
    """(warn_s, fail_s) for a contribution age, or None when there is no band.

    None for a radar this profile does not monitor for ARRIVAL — which, since
    2026-10-12, means the three NEXRAD sites. They belong in the expected set
    because they are in the composite at every timestep, but their contribution
    ages are a different population: measured over 720 composites on
    2026-10-11, X-band p50 ran 140-236 s while NEXRAD p50 ran 597-717 s, and
    KMUX reached p95 = 952 s. Applying the uniform band to them would fire on
    most of their healthy runs. [M]

    That is not a number to be adjusted, it is a band of the wrong SHAPE: the
    difference is longer volume scans delivered over LDM, i.e. the delivery
    mechanism, not the radar's luck. A band per radar CLASS would be defensible
    where the per-radar band in the docstring below is not — one describes the
    transport, the other is fitted to each radar's own history and goes blind
    exactly where that history is already bad. It is not being fitted yet
    because the quantity itself is unsettled; see the note on `age_disagrees`
    and config.COMPOSITE_CONTRIB_FAIL_S.

    So those radars get A_included judged and their contribution age RECORDED,
    with no verdict on it. That is the same presence-only treatment the receipt
    fallback already gets for the same reason: a band is only valid for the
    basis it was fitted to.

    ONE UNIFORM PAIR for everything else.

    This was derived per radar from that radar's own silence limit. Measured
    over 1,440 composite runs that band is blind on XSCR — max 403 s against a
    1,080 s limit, so it can never fire on the one radar that is never late —
    while firing on 25% of XSCW's runs. See config.COMPOSITE_CONTRIB_FAIL_S for
    the distribution and for why per-radar normalisation is the wrong answer:
    a band tuned to each radar's own history silences exactly the radars that
    misbehave.

    Thresholds are read through the admin store first so a retroactive change
    applies, then fall back to the config constants.
    """
    if radar_id not in RADAR_FOLDER:
        return None
    ov = COMPOSITE_CONTRIB_OVERRIDE_S.get(radar_id)
    warn_s = _thresholds.get_global(
        "composite_contrib_warn_s",
        ov[0] if ov else COMPOSITE_CONTRIB_WARN_S)
    fail_s = _thresholds.get_global(
        "composite_contrib_fail_s",
        ov[1] if ov else COMPOSITE_CONTRIB_FAIL_S)
    return float(warn_s), float(fail_s)


class Layer3CompositeParticipationCheck(Check):
    """Is this radar in the composite, and how fresh is what it contributed?

    Two sub-checks:

    ``A_included``  the radar has a block in the latest composite run, with
                    dimensions logged — so the composite opened its file and
                    read it. Absent is a fail: the composite is running without
                    it, which is how XEBY left the AQPI composite on 2026-10-05
                    without a single alarm anywhere in the system.
    ``B_fresh``     how old that contribution was, against one uniform band.

    Three states, not two, and the third only exists because the run log
    reports outcomes: **absent** (no block), **named but not read** (a block
    with no dimensions — the composite tried this file and failed on it), and
    **in**. The receipt could never distinguish the middle one.

    ``A_included`` is reported even when ``B_fresh`` cannot be: a radar present
    with no usable timestamp is a different fault from a radar missing.

    On the receipt fallback the claim weakens to "offered to composite", since
    that file records only what ``ls | tail -1`` selected. The wording follows
    the evidence; see ``_participation``.
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
        # The receipt path, as the fallback location. The actual source used
        # is recorded in payload.participation_source once a read succeeds.
        payload: dict[str, Any] = {
            "receipt_path": _receipt_path(), "receipt_dir": self.receipt_dir,
            "source": "backend-filesystem",
        }

        try:
            parsed = await asyncio.wait_for(_participation(), FS_TIMEOUT_S)
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
            # Neither source answered: no run logs and no receipt. The
            # composite is not running at all, or not where we think.
            metrics["fs_timeout"] = 0.0
            payload["receipt_absent"] = True
            sub["A_included"] = "fail"
            return _final(self, t0, sub, payload, metrics,
                          f"no composite run log or receipt found under "
                          f"{SETTINGS.backend_root}")
        except OSError as e:
            return CheckResult(
                check_id=self.id, target=self.target, stage=self.stage,
                status="error", started_at=t0, finished_at=utcnow(),
                summary=f"receipt read failed: {humanize_error(e)}",
                payload=payload, metrics={"fs_timeout": 0.0})

        metrics["fs_timeout"] = 0.0
        src = parsed.get("source", "receipt")
        contrib = parsed["contributions"].get(self.receipt_dir)
        payload["participation_source"] = src
        payload["receipt_radars"] = sorted(parsed["contributions"])
        if parsed.get("run_log"):
            payload["run_log"] = parsed["run_log"]
        payload["receipt_age_s"] = round(t0.timestamp() - parsed["receipt_mtime"], 1)

        bands = _contrib_bands(self.radar_id)
        # None stays None in the payload rather than being filled with the
        # uniform pair: the reprocess engine re-verdicts from these, and
        # writing a band this radar was never judged against would let a
        # retroactive pass invent a verdict the live check declined to give.
        payload["contrib_warn_s"] = bands[0] if bands else None
        payload["contrib_fail_s"] = bands[1] if bands else None

        # The run log proves INGESTION (the composite opened the file and read
        # its dimensions); the receipt only shows what `ls | tail -1` selected,
        # which is what the composite was OFFERED. The wording follows the
        # evidence rather than claiming the stronger thing on the weaker
        # source.
        verb = "in composite" if src == "run_log" else "offered to composite"
        absent_verb = ("NOT IN COMPOSITE" if src == "run_log"
                       else "NOT OFFERED TO COMPOSITE")

        if contrib is None:
            sub["A_included"] = "fail"
            metrics["included"] = 0.0
            others = ", ".join(payload["receipt_radars"]) or "none"
            return _final(self, t0, sub, payload, metrics,
                          f"{absent_verb} — {self.receipt_dir} has no block in the "
                          f"latest composite run (present: {others})")

        sub["A_included"] = "pass"
        metrics["included"] = 1.0

        # Named but not read is a distinct, worse state than not named: the
        # composite tried and failed on this file. Only the run log can tell
        # the difference, since the receipt never reports an outcome.
        if src == "run_log" and not contrib.get("ingested", True):
            sub["A_included"] = "fail"
            metrics["included"] = 0.0
            payload["named_not_read"] = True
            return _final(self, t0, sub, payload, metrics,
                          f"NAMED BUT NOT READ — {self.receipt_dir} appears in the "
                          f"run but the composite logged no dimensions for it")

        # === The receipt fallback is PRESENCE-ONLY, by measurement ===
        #
        # The two sources have different ZERO POINTS. The run log's reference
        # is endDateTimeScan, which equals the run's TARGET time (verified
        # 120/120 runs). The receipt's only available reference is its own
        # mtime, and it is written when the loops run -- at processing START,
        # not at the target. The composite's own filenames show the gap:
        # target 17:12:00 against startproc 17:14:01, 121 s later.
        #
        # Measured across 13 receipt versions, receipt-derived ages run
        # +102..+148 s above log-derived ages for the same radars, clustering
        # near one composite cycle: [M]
        #
        #     XSCV +148s   XSWR +102s   XSCR +115s   XEBY +118s   SSCB +114s
        #
        # So banding both bases at one 600/900 makes the FALLBACK PATH
        # systematically ~115 s stricter for identical underlying staleness. A
        # row would drift toward warn purely because the check fell back, with
        # nothing having changed about the composite -- the same class of error
        # as judging XEBY by XSCR's cadence, one layer up.
        #
        # Rather than fit a correction off 13 cycles, or maintain two bands
        # against two bases, B_fresh is simply NOT ASSESSED on the receipt.
        # A_included still works perfectly there, because membership does not
        # depend on a reference time at all. The check degrades to the weaker
        # QUESTION rather than to a differently-calibrated answer to the
        # stronger one -- the same principle as the "offered to" versus "in
        # composite" wording above.
        if src != "run_log":
            payload["age_basis"] = "not_assessed_on_receipt"
            return _final(self, t0, sub, payload, metrics,
                          f"{verb} (contribution age not assessed — the receipt's "
                          f"reference time is ~1 cycle later than the run log's)")

        if contrib.get("age_s") is not None:
            age_s = float(contrib["age_s"])
            payload["age_basis"] = "composite_reported"
            if contrib.get("derived_age_s") is not None:
                payload["derived_age_s"] = contrib["derived_age_s"]
            # See _parse_run_log: the field's own label says scan duration, and
            # the evidence that it is an age is an inference. A disagreement
            # with the independently derived figure is surfaced, not resolved.
            if contrib.get("age_disagrees"):
                payload["age_disagrees"] = True
        else:
            # Present, readable, but carrying no usable timestamp. A_included
            # stands on its own; B_fresh is simply not assessable.
            payload["age_basis"] = "unavailable"
            return _final(self, t0, sub, payload, metrics,
                          f"{verb}, contribution age not reported")

        metrics["contrib_age_s"] = float(age_s)
        payload["contrib_path"] = contrib.get("volume_path") or contrib.get("path")
        payload["age_s"] = round(age_s, 1)

        if bands is None:
            # Recorded, not judged. See _contrib_bands. The age is in the
            # payload and the metric so the series accumulates toward a
            # per-class band; what is withheld is the verdict, because we have
            # no band this radar's population was fitted to.
            payload["age_basis_banded"] = False
            return _final(self, t0, sub, payload, metrics,
                          f"{verb}, contribution {age_s / 60:.1f} min old"
                          f"  (no band for this radar class)")
        warn_s, fail_s = bands
        # Same normalized 0..1 sparkline axis as LB1/LB2. See helpers.headroom.
        metrics["headroom"] = headroom(age_s, fail_s)

        if age_s <= warn_s:
            sub["B_fresh"] = "pass"
        elif age_s <= fail_s:
            sub["B_fresh"] = "warn"
        else:
            sub["B_fresh"] = "fail"

        summary = (f"{verb}, contribution {age_s / 60:.1f} min old"
                   f"  (limit {fail_s / 60:.0f} min)")
        if sub["B_fresh"] == "fail":
            summary = "STALE CONTRIBUTION — " + summary
        if payload.get("age_disagrees"):
            summary += "  [age fields disagree]"
        return _final(self, t0, sub, payload, metrics, summary)


_DROPS_LOOKBACK_DAYS = 2


def _drops_newest(folder: str, pattern: str, now: datetime) -> float | None:
    """Blocking. When output last landed under one DROPS radar folder, as the
    FILENAMES declare it.

    === Why this is not one stat on the folder ===

    It was, from v0.7.0 until 2026-10-12, and the folder's mtime does not
    answer this question. Measured: every radar folder holds ZERO direct files
    and exactly one year directory, so files land in `<folder>/2026/10/05/` and
    never touch `<folder>` itself. [M]

    Two consequences, and the second is the one that matters:

      1. The number meant "when something was last added to this folder" —
         which, with all five folders reading an identical 182 h and owned by a
         different user from their contents, was a sweep over their direct
         entries and not output arrival at all.
      2. THE CHECK COULD NOT DETECT RECOVERY. If the producer resumed, new
         files would land in a new day directory and `<folder>`'s mtime would
         stay frozen, so the check would report the same stall forever. Its
         agreement with reality during an actual stall was therefore not
         evidence that it worked: a frozen clock and a stopped producer are
         indistinguishable in exactly that one state.

    === Declared time, not mtime ===

    `_newest_declared` reads the timestamp out of the filename. That is the
    same choice LB2_FRESHNESS = "filename" exists to make, and for the same
    reason — an mtime can be re-touched while the observation it describes
    stays old. Today's finding sharpens it rather than softening it: something
    swept these directories' mtimes without writing data, so mtime on this tree
    is demonstrably not a record of arrival. The per-radar patterns match DROPS
    names unchanged, so this needs no new table and no new import guard.

    === Three reads, most specific first ===

    Flat layout, then today and yesterday constructed from the clock, then a
    bounded walk. The middle one is `first_resolvable` for the reason its own
    docstring gives: without the yesterday fallback this reports all five
    folders stalled in the first minutes of every UTC day, which is the LB2
    nuisance alarm reintroduced in a different tree.

    The walk exists only for the failure case. Constructing from the clock
    means a producer dead eight days answers "nothing today or yesterday",
    which loses the magnitude an operator wants — so when that happens, descend
    by numeric name to recover the true age. Bounded at today so a stray
    future-dated directory cannot blind the check, which is the hazard a pure
    descent has and clock construction does not.
    """
    # Flat layout, if this tree ever is one. Cheap, and it means the check does
    # not assume a layout — guessing one is what the rest of this week was
    # about.
    try:
        return _newest_declared(folder, pattern)
    except FileNotFoundError:
        pass

    candidates = [
        os.path.join(folder, (now - timedelta(days=d)).strftime("%Y/%m/%d"))
        for d in range(_DROPS_LOOKBACK_DAYS)
    ]
    try:
        ts, _path, _back = first_resolvable(
            candidates, lambda p: _newest_declared(p, pattern))
        return ts
    except FileNotFoundError:
        pass

    # Nothing recent. Walk to the newest day that exists, so the payload
    # carries the real age rather than only "absent" — the same reasoning as
    # the staging check reporting its siblings' age instead of their names.
    def newest_numeric(path: str, ceiling: str) -> str | None:
        try:
            with os.scandir(path) as it:
                names = sorted((e.name for e in it
                                if e.is_dir(follow_symlinks=False)
                                and e.name.isdigit() and e.name <= ceiling),
                               key=lambda n: (len(n), n))
        except OSError:
            return None
        return names[-1] if names else None

    cur = folder
    for fmt in ("%Y", "%m", "%d"):
        nxt = newest_numeric(cur, now.strftime(fmt))
        if nxt is None:
            return None
        cur = os.path.join(cur, nxt)
    try:
        return _newest_declared(cur, pattern)
    except (FileNotFoundError, OSError):
        return None


class Layer3DropsProducerCheck(Check):
    """Is Gen_X-band_QPE.py still producing? INFORMATIONAL by design.

    === The published qpe_* products are NOT this script's output ===

    They look like it. `layer1.backend.qpe_15min` and friends watch files named
    `qpe_15m_step_COMP_20261012_184200_rainfall.nc.png` — derived from `COMP_*`
    COMPOSITE files, a different artifact that happens to share the letters
    QPE. Measured 2026-10-12: all three published QPE products were 0-2 min
    fresh while this producer's tree had been quiet for over a week. [M]

    So a healthy `layer1.backend.qpe_*` row says NOTHING about whether this
    script is running, and a stall here does not make any published product
    wrong. I proposed exactly that inference as a way to tell a stalled
    producer from an abandoned output tree, and it would have returned a
    confident wrong answer — the same shape as the `recentfiles` decoy and the
    sweep-versus-volume confusion, two things named alike taken for one thing.

    === Why this is capped at warn ===

    Nothing in the live product chain reads DROPS, per the above, so a stall
    here does not make any published product wrong. LB2 used to gate radar
    health on it, which is how one dead script became five radar failures for
    twelve hours.

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

    Freshness comes from `_drops_newest`, which walks to the files. It used to
    be one stat on the radar folder, which could not detect recovery — see that
    function for the measurement and for why the old number agreed with reality
    by coincidence.
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
                    out[rid] = _drops_newest(os.path.join(root, folder),
                                             RAW_VOLUME_TS_RE[rid], t0)
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
        # Through the store, with the config constant as the default. Read
        # fresh every run so an admin edit applies without a restart, the same
        # way _contrib_bands does.
        #
        # This read DROPS_SILENT_INFO_S directly until 2026-10-05, while
        # _reverdict_lb3 read get_global("drops_silent_info_s"). So editing the
        # limit in /admin/thresholds changed RETROACTIVE verdicts and left live
        # ones alone -- a split brain between the check and its own reprocess
        # handler, with nothing anywhere reporting the disagreement.
        limit = float(_thresholds.get_global(
            "drops_silent_info_s", DROPS_SILENT_INFO_S))
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


# --------------------------------------------------------------------------
# Composite INPUT staging
# --------------------------------------------------------------------------

def _list_parent(path: str, limit: int = 40) -> tuple[list[str] | None, float | None]:
    """Blocking. (names beside `path`, age of the newest thing among them).

    Only ever called when `path` itself was absent, so this costs nothing in
    the steady state. It is the difference between "the mount is gone" and
    "the mount is fine and we are looking for the wrong name", which is a
    distinction no amount of staring at the failing path can make — and the
    one the reader will try to make by guessing.

    A dangling symlink reads as absent here while looking present on the host,
    which is the shape to expect when a tree is assembled from more than one
    mount. CBAND's raw data is on a different share from the X-bands (see
    SENTINEL_SSCB_ROOT), so it is the entry most likely to be a link.
    """
    parent = os.path.dirname(path.rstrip("/"))
    try:
        with os.scandir(parent) as it:
            names = sorted(e.name for i, e in enumerate(it) if i < limit)
    except OSError:
        return None, None
    # Newest mtime anywhere one level down. A name list alone is the trap the
    # decoy was built to spring: the abandoned tree had the RIGHT names, the
    # right conventions and a believable file count, so a sibling listing would
    # have confirmed "wrong name" with more confidence than the bare error did.
    # Age is what no amount of name-checking reaches.
    newest = 0.0
    for name in names:
        try:
            with os.scandir(os.path.join(parent, name)) as it2:
                for i, e in enumerate(it2):
                    if i >= limit:
                        break
                    newest = max(newest, e.stat(follow_symlinks=False).st_mtime)
        except OSError:
            continue
    return names, (time.time() - newest if newest else None)


def _scan_staging_blocking(path: str) -> dict[str, Any]:
    """Blocking. What is staged for one radar, in ONE directory read.

    Returns entry counts and the newest DECLARED observation time, plus the
    mtime of the file that declared it. Deliberately not a stat per file: the
    standing constraint on these hosts is to add no load, and a thirty-file
    stat storm per radar per tick would be the most expensive check in the
    suite for information the filenames already carry. Exactly one stat is
    taken, on the single newest-declared file, because the skew between what a
    file DECLARES and when it was WRITTEN is the only way to see a producer
    replaying or backfilling stale scans — and that is a distinct failure from
    anything an age alone can show.

    Dot-prefixed entries are skipped before matching, as in
    layer2_backend_radar._newest_declared: a writer's temporary files embed a
    real parseable timestamp and only the leading dot distinguishes them.
    """
    pat = re.compile(COMPOSITE_INPUT_TS_RE)
    entries = 0
    parsed = 0
    newest = 0.0
    newest_name = ""
    with os.scandir(path) as it:
        for e in it:
            if e.name.startswith("."):
                continue
            entries += 1
            m = pat.search(e.name)
            if not m:
                continue
            try:
                dt = datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S")
            except ValueError:
                continue
            parsed += 1
            ts = dt.replace(tzinfo=timezone.utc).timestamp()
            if ts > newest:
                newest = ts
                newest_name = e.name
    out: dict[str, Any] = {
        "entries": entries, "parsed": parsed,
        "newest": newest or None, "newest_name": newest_name or None,
        "newest_mtime": None,
    }
    if newest_name:
        try:
            out["newest_mtime"] = os.stat(os.path.join(path, newest_name)).st_mtime
        except OSError:
            # Pruned between the scan and the stat, which is normal in a tree
            # with an hour of retention. The skew is an extra, never the
            # verdict, so losing it costs nothing.
            pass
    return out


class Layer3InputStagingCheck(Check):
    """Is anything staged for this radar where the composite will look?

    The gap this closes, and why neither neighbour could see it:

        DROPS dated tree    <- layer2.backend.<radar>    (LB2)
        recentfiles/<DIR>   <- THIS CHECK
        composite output    <- layer3.composite.<radar>  (LB3)

    The case this ships for is NEXRAD. KBBX/KDAX/KMUX staging directories held
    zero files from 2026-09-17 to ~2026-10-06 after a K2 reboot left ``ldm``
    stopped, so the composite ran for eighteen days on six radars instead of
    nine. Nothing in Sentinel could say so, because nothing looked at the one
    directory where it was visible, and it was found by eye.

    It is NOT XSCW, which this docstring asserted until 2026-10-12. That
    argument set a staging reading from 10-12 beside a ``layer2.backend.XSCW``
    pass from 10-08 and treated the pair as one observation. Re-measured, XSCW
    was simply down — declared DOWN with obs=0, newest sweep stopped 09:08Z,
    LB2 failing at 623.7 min — and an empty staging directory is what a down
    radar looks like from here. A stale PASS is more dangerous than a stale
    FAIL: a stale fail makes you look at the thing, a stale pass makes you look
    away from it and invent a cause elsewhere.

    The two trees hold different OBJECTS, not just different paths: the dated
    tree holds per-elevation PPI SWEEPS and this one holds assembled VOLUMES.
    So this check's age and LB2's age are not two views of one quantity, and a
    discrepancy between them is structural rather than a fault — do not
    sanity-check one against the other, and do not read "sweeps present,
    volumes absent" as a located break unless both readings are from the same
    moment. See config.COMPOSITE_INPUT_HOST_SUBPATH.

    ``A_staged``  the directory holds at least one volume. Empty is the
                  headline failure: the composite has nothing to read.
    ``B_fresh``   how old the newest staged observation is. WARN-CAPPED, and
                  will stay that way until this check has produced its own
                  distribution — see config.COMPOSITE_INPUT_STALE_WARN_S for
                  why a band is not being fitted to one afternoon's sample.

    === Three refusals, each repeating a mistake already made in this file ===

    1. A directory with entries that none of our patterns match is an ERROR
       naming OUR pattern, never "nothing staged". Reporting our blindness as
       the subject's outage is what rendered a CBAND directory holding 295
       current files as "no data directory for the current UTC day".

    2. Empty is a TERMINAL state, not an age. This tree retains about an hour,
       so once staging stops the evidence ages out and the check cannot tell
       "stopped 70 minutes ago" from "stopped three days ago". The summary says
       nothing is staged; it must never imply it knows for how long.

    3. The alarm is gated on LB2 via ``alarm_only_depends_on``, not
       ``depends_on``. XSCV is down, so its staging directory is CORRECTLY
       empty, and a check that pages for that is a restatement of
       ``layer2.radar.XSCV`` that buries the one case we care about.
       ``depends_on`` would be wrong in the other direction: it demotes the
       result to skip, discarding an observation we actually made. The
       conjunction that matters is LB2 PASS plus staging EMPTY, and keeping
       the fail recorded while suppressing the page is exactly what the
       alarm-only form is for.
    """
    stage = "LB3"
    cadence_s = 120

    def __init__(self, radar_id: str):
        self.radar_id = radar_id
        self.id = f"layer3.input.{radar_id}"
        self.target = radar_id
        self.source_tag, self.source_label = BACKEND_SOURCE
        # Only when that check exists. The three NEXRAD sites have no DROPS
        # tree and no LB2, and are watched here or nowhere — read from the
        # registry rather than re-deriving LB2's own registration gate, which
        # would be a second place to keep in step.
        lb2 = f"layer2.backend.{radar_id}"
        self.alarm_only_depends_on = [lb2] if lb2 in CHECKS else []

    def _path(self) -> str:
        # recentfiles_dir IS the recentfiles directory — only the per-radar
        # name is appended. See config.COMPOSITE_INPUT_HOST_SUBPATH for why
        # this no longer joins a subpath, and config.COMPOSITE_INPUT_DIR for
        # why the name is a mapping (CBAND stages as SSCB).
        return os.path.join(SETTINGS.recentfiles_dir,
                            COMPOSITE_INPUT_DIR[self.radar_id])

    async def run(self, ctx) -> CheckResult:
        t0 = utcnow()
        sub: dict[str, str] = {}
        metrics: dict[str, float] = {}
        path = self._path()
        payload: dict[str, Any] = {
            "path": path, "source": "backend-filesystem",
            "retention_s": COMPOSITE_INPUT_RETENTION_S,
        }

        try:
            got = await asyncio.wait_for(
                asyncio.to_thread(_scan_staging_blocking, path), FS_TIMEOUT_S)
        except asyncio.TimeoutError:
            return CheckResult(
                check_id=self.id, target=self.target, stage=self.stage,
                status="error", started_at=t0, finished_at=utcnow(),
                summary=f"NFS read timed out after {FS_TIMEOUT_S:g}s — mount may be hung",
                payload=payload, metrics={"fs_timeout": 1.0},
            )
        except FileNotFoundError:
            # The staging directory itself, not its contents. All nine exist on
            # AQPI; their absence is a statement about the mount or about
            # Sounak's scripts, not about this radar.
            #
            # So the error carries the evidence needed to tell those apart,
            # because "staging directory does not exist" on its own sent two
            # sessions guessing: at a wrong directory NAME (the id-versus-folder
            # mapping, which was already correct), at the bind mount, and at a
            # dangling symlink. The parent listing distinguishes all three in
            # one line, and the cost is one extra read on a path that is
            # already failing.
            # The specific misconfiguration this setting invites: pointing it
            # at the PROJECT root rather than at recentfiles. Checked before
            # anything else because it has a precise answer, and because the
            # generic "parent unreadable" message is what an operator got for
            # it on the first deploy. Two stats, only on a path that already
            # failed. Deliberately a runtime probe rather than a startup
            # assertion: a mount can come up after the process does, and
            # refusing to boot on a slow NFS mount would trade this for a
            # worse failure.
            deep = os.path.join(SETTINGS.recentfiles_dir or "",
                                COMPOSITE_INPUT_HOST_SUBPATH,
                                COMPOSITE_INPUT_DIR[self.radar_id])
            if await asyncio.to_thread(os.path.isdir, deep):
                return CheckResult(
                    check_id=self.id, target=self.target, stage=self.stage,
                    status="error", started_at=t0, finished_at=utcnow(),
                    summary=(f"SENTINEL_RECENTFILES_DIR points one level too "
                             f"shallow: {path} does not exist but {deep} does. "
                             f"It must name the recentfiles directory itself, "
                             f"not the project root above it — which is also "
                             f"what the old SENTINEL_RECENTFILES_ROOT meant, "
                             f"so a v0.8.2 value lands here."),
                    payload={**payload, "absent": True, "misconfigured_root": deep},
                    metrics={"fs_timeout": 0.0},
                )
            sibs, sib_age = await asyncio.to_thread(_list_parent, path)
            extra = {**payload, "absent": True, "siblings": sibs,
                     "sibling_newest_age_s": sib_age}
            if sibs is None:
                why = ("its parent is unreadable too — the mount or the "
                       "bind-mount boundary, not this radar")
            elif not sibs:
                why = "its parent exists and is EMPTY — nothing is staged at all"
            elif sib_age is not None and sib_age > COMPOSITE_INPUT_RETENTION_S:
                # The decisive signal, and the one a sibling NAME list cannot
                # give: the neighbours are older than this tree retains, so
                # this is not the live staging tree whatever it is called.
                why = (f"its {len(sibs)} siblings were last written "
                       f"{sib_age / 86400:.0f} days ago — this is not a live "
                       f"staging tree, so the ROOT is wrong, not the name")
            else:
                why = (f"its parent holds {len(sibs)} other entr"
                       f"{'y' if len(sibs) == 1 else 'ies'} "
                       f"({', '.join(sibs[:6])}{'…' if len(sibs) > 6 else ''}) "
                       f"— so the tree is mounted and THIS name is not in it")
            # The resolved path in the SUMMARY, not only the payload. Two
            # sessions guessed for two rounds at the name and the mount while
            # the answer sat in payload.path: anyone who knows what that path
            # should be ends it in one read.
            return CheckResult(
                check_id=self.id, target=self.target, stage=self.stage,
                status="error", started_at=t0, finished_at=utcnow(),
                summary=f"staging directory does not exist — {path} — {why}",
                payload=extra, metrics={"fs_timeout": 0.0},
            )
        except OSError as e:
            return CheckResult(
                check_id=self.id, target=self.target, stage=self.stage,
                status="error", started_at=t0, finished_at=utcnow(),
                summary=f"staging read failed: {humanize_error(e)}",
                payload=payload, metrics={"fs_timeout": 0.0},
            )

        metrics["fs_timeout"] = 0.0
        metrics["staged_files"] = float(got["parsed"])
        payload["staged_entries"] = got["entries"]
        payload["staged_files"] = got["parsed"]

        if got["entries"] and not got["parsed"]:
            # See refusal 1. Our pattern, our problem — and saying so names the
            # thing to go and look at instead of sending someone to a radar
            # that is fine.
            return CheckResult(
                check_id=self.id, target=self.target, stage=self.stage,
                status="error", started_at=t0, finished_at=utcnow(),
                summary=(f"{got['entries']} staged file(s), none matching "
                         f"Sentinel's filename pattern — the pattern is stale, "
                         f"not the radar"),
                payload={**payload, "unparseable": True,
                         "pattern": COMPOSITE_INPUT_TS_RE},
                metrics=metrics,
            )

        if not got["parsed"]:
            sub["A_staged"] = "fail"
            # See refusal 2: no duration, because this tree cannot support one.
            return _final(self, t0, sub, payload, metrics,
                          "NOTHING STAGED — the composite has no input for "
                          "this radar")

        sub["A_staged"] = "pass"
        age_s = t0.timestamp() - float(got["newest"])
        warn_s = _thresholds.get_global("composite_input_stale_warn_s",
                                        COMPOSITE_INPUT_STALE_WARN_S)
        metrics["age_s"] = float(age_s)
        payload["age_s"] = round(age_s, 1)
        payload["stale_warn_s"] = float(warn_s) if warn_s is not None else None
        payload["newest_name"] = got["newest_name"]
        payload["newest_declared"] = datetime.fromtimestamp(
            float(got["newest"]), timezone.utc).isoformat()

        # Declared-versus-written skew. RECORDED, never judged — this check has
        # no characterised distribution for it yet. It is here because it is
        # the only quantity that can distinguish a producer that has stalled
        # from one that is replaying old scans into a fresh file, and because
        # retention bounds a file's MTIME while this tree is read by its
        # DECLARED time. Conflating those two is what made a 22.8 h
        # contribution age look impossible when it was merely unexplained.
        if got["newest_mtime"] is not None:
            skew = float(got["newest_mtime"]) - float(got["newest"])
            payload["staged_skew_s"] = round(skew, 1)
            metrics["staged_skew_s"] = skew

        # === Older than the tree retains is not staleness, it is the wrong tree ===
        #
        # Not a fitted band and not a threshold question: this tree prunes to
        # about an hour (config.COMPOSITE_INPUT_RETENTION_S, measured), so a
        # newest staged file older than the ENTIRE retention window is a
        # contradiction. Either the retention assumption is wrong or this is
        # not the live staging tree, and both are statements about Sentinel's
        # configuration rather than about the radar — hence error, not fail.
        #
        # This is what v0.8.0 needed and did not have. It resolved the staging
        # tree under backend_root, found a copy abandoned in March 2026 with
        # the right eight directory names, and reported "20 staged, newest
        # 298254.8 min old" as a PASS on every one of them. The age was in the
        # summary the whole time and nothing judged it.
        #
        # Worth being explicit, because it is the opposite of the band removed
        # an hour earlier: that band was a guess at what "late" means on a tree
        # working normally, and had no measurement behind it. This is a
        # consistency check against a documented property of the tree, and
        # needs none — 202 days against a 1 h window is not a close call.
        # Removing the unjustified band is what let the wrong root through, so
        # the lesson is not "keep guessed bands", it is that switching off a
        # judgement can take an unrelated guarantee with it.
        if age_s > COMPOSITE_INPUT_RETENTION_S:
            return CheckResult(
                check_id=self.id, target=self.target, stage=self.stage,
                status="error", started_at=t0, finished_at=utcnow(),
                summary=(f"newest staged file is {age_s / 86400:.1f} days old, "
                         f"older than this tree's entire "
                         f"{COMPOSITE_INPUT_RETENTION_S / 3600:.0f} h retention "
                         f"— {path} is not a live staging tree"),
                payload={**payload, "beyond_retention": True},
                metrics=metrics,
            )

        if warn_s is None:
            # No band — see config.COMPOSITE_INPUT_STALE_WARN_S. B_fresh is
            # not added to `sub` at all rather than being set to a passing
            # value: "we did not judge this" and "we judged it healthy" are
            # different claims, and recording the second would make the
            # staged-age series look assessed for the whole period it was not.
            # The age is still in the payload and the metric, which is what the
            # band will eventually be fitted from.
            return _final(self, t0, sub, payload, metrics,
                          f"{got['parsed']} staged, newest "
                          f"{age_s / 60:.1f} min old")
        warn_s = float(warn_s)
        # Same normalized 0..1 sparkline axis as the other backend checks, and
        # only meaningful once there is a budget to measure against.
        metrics["headroom"] = headroom(age_s, warn_s)
        if age_s > warn_s:
            sub["B_fresh"] = "warn"
            summary = (f"STAGING STALE — newest staged volume "
                       f"{age_s / 60:.1f} min old (warn at {warn_s / 60:.0f} min)")
        else:
            sub["B_fresh"] = "pass"
            summary = (f"{got['parsed']} staged, newest {age_s / 60:.1f} min old")
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
    # Gated on its OWN root, not on backend_root. Unset means these checks do
    # not exist, which is the honest answer for a deployment that has not
    # mounted the staging share — and is why the setting has no default. The
    # version that defaulted it under backend_root registered all nine against
    # a tree abandoned in March and reported eight of them healthy.
    #
    # Registered AFTER layer2_backend_radar (see checks/__init__._ALWAYS) so
    # each instance can read the registry to find out whether it has an LB2
    # check to gate its alarm on.
    if SETTINGS.recentfiles_dir:
        for _rid in sorted(COMPOSITE_INPUT_DIR):
            register(Layer3InputStagingCheck(radar_id=_rid))
