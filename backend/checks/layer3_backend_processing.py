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
from datetime import timezone
from typing import Any

from ..config import (BACKEND_SOURCE, COMPOSITE_CONTRIB_FAIL_S,
                      COMPOSITE_CONTRIB_OVERRIDE_S, COMPOSITE_CONTRIB_WARN_S,
                      COMPOSITE_EXPECTED_RADARS, COMPOSITE_RECEIPT,
                      COMPOSITE_RECEIPT_SETTLE_S, COMPOSITE_RUN_LOG_DIR,
                      DROPS_SILENT_INFO_S, DROPS_TREE, MAX_RUN_LOG_ENTRIES,
                      RADAR_FOLDER, SETTINGS)
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


def _contrib_bands(radar_id: str) -> tuple[float, float]:
    """(warn_s, fail_s) for a contribution age. ONE UNIFORM PAIR.

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

        warn_s, fail_s = _contrib_bands(self.radar_id)
        payload["contrib_warn_s"], payload["contrib_fail_s"] = warn_s, fail_s

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
        # Same normalized 0..1 sparkline axis as LB1/LB2. See helpers.headroom.
        metrics["headroom"] = headroom(age_s, fail_s)
        payload["contrib_path"] = contrib.get("volume_path") or contrib.get("path")
        payload["age_s"] = round(age_s, 1)

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
