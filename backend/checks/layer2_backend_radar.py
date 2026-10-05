"""Layer-2 BACKEND radar arrival — read from the filesystem, not from radarca.

Companion to ``layer1_backend_product``. Where ``layer2_radar`` asks radarca
"does /api/radar-status/ say this radar is up, and are there images for it", this asks
the only question that is unambiguous: **is this radar's data landing on disk right now**.

Two trees, because the radars do not all arrive the same way:

* Five X-bands  — each writes a dated tree per radar under ``{backend_root}``,
  mapped by ``config.RADAR_DATED_TREE``. The directory names are not derivable
  from the radar id (XEBY's tree is ``EBAY``), so the table is the mapping.

  These pointed at ``{backend_root}/PRODUCTS/DROPS/<folder>`` until
  2026-10-05. That is the **output** of ``Gen_X-band_QPE.py``, one processing
  step downstream, so the check answered "is the QPE generator alive" rather
  than "is this radar delivering". When the generator stopped at 04:07 UTC all
  five checks failed for 12 hours while three of the radars were arriving
  within a minute throughout. See ``config.RADAR_DATED_TREE``.
* CBAND (SSCB)  — lands on trinity at ``/trinity/projects/aqpi/sscb/YYYY/MM/DD``, not in
  the X-band trees. It has no ACCEPT entry in iris's ldmd.conf and no
  file_process_sscb.sh on granite, so it does not arrive by the X-band push path.
  Monitored here because a C-band outage was previously undetectable.

  .. warning:: **Unresolved:** this docstring has said since v0.5.0 that CBAND
     also does not join the X-band composite. The composite's own input loops
     are reported to expect a ``radarC`` entry, which contradicts that. It
     decides whether CBAND belongs in the composite-participation check's
     expected set, and CBAND is the discriminator the fleet correlation below
     rests on, so it is flagged rather than assumed either way.

FLOW (XQPI/JPL) is covered by the ``xqpi`` profile, which registers one
instance of this check with its own tree and threshold.

Thresholds reuse ``config.RADAR_SILENT_FAIL_S``, which was characterised against real
per-radar cadence (XEBY 300 s ... CBAND 1080 s) and is already the number operators
reason about. Overridable per radar under ``backend_silent_s``.
"""
from __future__ import annotations
import asyncio
import os
import re
from datetime import datetime, timezone
from typing import Any

from ..config import (BACKEND_SOURCE, LB2_FRESHNESS,  # noqa: F401
                      RADAR_DATED_TREE, RADAR_FOLDER, RAW_VOLUME_TS_RE,
                      RADAR_SILENT_FAIL_S, SETTINGS)
from ..errors import humanize_error
from .. import thresholds as _thresholds
from ..registry import register
from .base import Check, CheckResult, utcnow
from .helpers import headroom, worst_of

FS_TIMEOUT_S = 5.0

# Radars whose data does not land in the DROPS tree. Value is a strftime template
# evaluated in UTC, matching how the writer dates its directories.
# NOTE: these are paths INSIDE the container, which differ from the host. cira-aqpi
# mounts /trinity on the host, but sentinel-backend sees only what the compose file
# bind-mounts. Hence a setting, not a literal.
def _special_trees() -> dict[str, str]:
    # str() first: SETTINGS.sscb_root is a Path (config._opt_path), and Path has no
    # .rstrip. Calling it raised AttributeError on every LB2 run in v0.5.0.
    if not SETTINGS.sscb_root:
        return {}
    return {"CBAND": str(SETTINGS.sscb_root).rstrip("/") + "/%Y/%m/%d"}


def _radar_path(radar_id: str, now) -> str:
    # Three layouts, most specific first: a radar on its own mount (CBAND via
    # SENTINEL_SSCB_ROOT), a radar dated inside this profile's own mount (XQPI's
    # FLOW), and AQPI's flat DROPS tree.
    tmpl = _special_trees().get(radar_id)
    if tmpl:
        return now.astimezone(timezone.utc).strftime(tmpl)
    rel = RADAR_DATED_TREE.get(radar_id)
    if rel:
        return now.astimezone(timezone.utc).strftime(
            os.path.join(SETTINGS.backend_root, rel))
    return os.path.join(SETTINGS.backend_root, "PRODUCTS", "DROPS",
                        RADAR_FOLDER[radar_id])


# Generous: one FLOW day holds ~2,900 volumes. If a directory ever exceeds
# this the walk stops early, which can only make the newest timestamp look
# OLDER than it is -- a false alarm rather than a masked outage, which is the
# correct direction to fail in.
MAX_SCAN_ENTRIES = 20_000


def _newest_declared(path: str, pattern: str) -> float:
    """Blocking. Newest observation time the FILENAMES declare, as an epoch.

    For trees where mtime is not a sound freshness basis -- see
    config.LB2_FRESHNESS and backend/profiles/xqpi.py for the gzip sweep that
    makes it unsound on trinity.

    Dot-prefixed entries are skipped before matching, and deliberately so
    rather than relying on the pattern to reject them: gzip leaves temporary
    files like `.flow-20261003-194148_..._PPI.netcdf.gz.4u4pxw` in the live
    directory (14 of them on inspection, three suffixes for one source
    volume). They are the newest entries by mtime and their names embed a
    real, parseable timestamp, so only the leading dot distinguishes them.

    No stat(2) per entry, unlike the mtime path -- the name carries everything
    needed, so this is cheaper than what it replaces despite the larger walk.
    """
    pat = re.compile(pattern)
    newest = 0.0
    n = 0
    with os.scandir(path) as it:
        for e in it:
            n += 1
            if n > MAX_SCAN_ENTRIES:
                break
            if e.name.startswith("."):
                continue
            m = pat.match(e.name)
            if not m:
                continue
            try:
                dt = datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S")
            except ValueError:
                continue
            ts = dt.replace(tzinfo=timezone.utc).timestamp()
            if ts > newest:
                newest = ts
    if not newest:
        # The directory exists but declares nothing readable. Same meaning as
        # an absent directory: let the silence threshold judge it.
        raise FileNotFoundError(f"no parseable volume filenames in {path}")
    return newest


def _dir_mtime(path: str) -> float:
    """Blocking. Directory mtime only — one stat, no listing.

    Valid here *because DROPS2 creates entries*: a directory's mtime moves when a file is
    added or removed in it. It would NOT be valid for a tree whose files are overwritten
    in place, which is why the product check scans for the newest file instead.
    """
    return os.stat(path).st_mtime


# ==========================================================================
# Fleet correlation — is this one event or five?
# ==========================================================================
#
# Ported from Layer2XbandFleet in layer2_radar.py. The mechanism is identical
# and the reasoning for the thresholds carries over; what differs is the
# evidence this side can bring, and it is strictly better.
#
# === Why the backend side is the stronger discriminator ===
#
# L2's fleet check reads verdicts derived from ONE radarca endpoint, so when
# it says "systemic" it cannot separate "the radars stopped" from "the API we
# ask about them stopped". This side reads the filesystem, and CBAND reads a
# DIFFERENT TREE ON A DIFFERENT MOUNT (trinity, via SENTINEL_SSCB_ROOT) from
# the five X-bands (K2, via SENTINEL_BACKEND_ROOT).
#
# That makes a passing CBAND positive evidence, not merely an absence of bad
# news: the host is up, NFS is serving, the clock is sane, and Sentinel's own
# reads are working. So "five X-bands silent while CBAND arrives normally"
# localises the fault to the X-band path specifically, which no amount of
# X-band-only evidence can do. CBAND silent AT THE SAME TIME means something
# wider, and the summary says which.
#
# CBAND is therefore excluded from the COUNT but reported as corroboration.
# Counting it would destroy exactly the independence that gives it its value.
LB_FLEET_CHECK_ID = "layer2.backend.fleet"
LB_FLEET = frozenset(r for r in RADAR_FOLDER if r != "CBAND")
LB_FLEET_WITNESS = "CBAND"

# Enter at 3, leave at 1, hold 10 min. Inherited from the L2 check, and the
# inherited part is the REASONING, not the measurement:
#
#   What transfers: radars at different physical sites do not fail
#   independently within the same minute. That is a property of the radars and
#   the sites, so 3 simultaneous is already systemic however you observed it.
#   The L2 history also showed 4-of-5 is too rare a bar to catch the real
#   episodes — it suppressed 0 alarms in 27,917 runs and missed the 2026-09-17
#   three-radar event entirely.
#
# ENTER=3 is now CONFIRMED on the arrival basis, independently of the L2
# figure. Reconstructed from the dated arrival trees rather than accumulated
# from check history -- 78,063 declared timestamps across five radars and 6.81
# days, simulated at 60 s ticks, 9,800 ticks: [M]
#
#     n not arriving    0      1      2      3      4      5
#     share         56.89% 30.26% 12.43%  0.43%  0.00%  0.00%
#
# n >= 4 NEVER occurs, 0 of 9,800. Same conclusion as the L2 history reached
# by different data on a different basis: 4-of-5 is a bar the real episodes do
# not reach.
#
# Episode structure matters more than the shares:
#     n >= 2:  12 episodes, min 4 min, median 44 min, max 468 min
#     n >= 3:   2 episodes, 34 min and 8 min
#
# EXIT=1 is supported by the episodes rather than by the share: exiting at <=2
# would release the latch during a state occupying 12.4% of the window across
# 12 episodes with a 44-minute median, so it would flap through most of them.
#
# === The real argument against ENTER=2, which the shares do not show ===
#
# The n>=2 episodes are dominated by the same two radars -- XEBY in five of
# the top eight, XSCW in six. So the two-radar state is not a weak systemic
# signal, it is two individually unreliable radars coinciding, which is the
# OPPOSITE of systemic. An enter at 2 would not merely be noisy: it would fire
# most often on exactly the pair whose failures are known to be independent,
# and so be systematically wrong about the thing it claims to detect.
#
# !!! Sample size, stated rather than glossed. Both n>=3 episodes are the same
#     trio on the same day three hours apart, so they are arguably one
#     underlying event sampled twice. The honest count is one to two systemic
#     events in 6.81 days, against 27,917 runs on the L2 side. This REPRODUCES
#     the L2 reasoning on the correct basis and sets a floor; it does not fit a
#     distribution, and it is not equivalent evidence to the L2 figure.
LB_FLEET_SYSTEMIC_ENTER = 3
LB_FLEET_SYSTEMIC_EXIT = 1

# Must outlast the alarm hold-down, same as the L2 constant: suppression is
# only consulted when a radar alarm OPENS, which is one hold-down (5 m, see
# alerts.yaml) after that radar started failing. A verdict that trips and
# clears inside that window suppresses nothing. 10 min covers the hold-down
# plus this check's cadence with room to spare.
#
# Checked against the measured episodes, because 10 min EXCEEDS the shorter of
# the two n>=3 episodes (8 min) and that looks alarming until it is worked
# through. It is correct, and the arithmetic is the reason:
#
#   t=0      n reaches 3, latch sets, `since` re-arms on every systemic tick
#   t=5 min  the radar alarms OPEN, one hold-down in -- latch is active, so
#            they are suppressed, which is the entire purpose
#   t=8 min  episode ends, n drops to <=1, dwell starts counting from the last
#            systemic tick
#   t=18 min latch releases
#
# So the latch outliving an 8-minute episode by 10 minutes is the design
# working, not failing: a dwell SHORTER than the episode would clear the latch
# mid-incident and unsuppress the duplicates. The cost is a row reading
# "SYSTEMIC (holding)" on a recovered fleet for 10 minutes -- labelled as such,
# with the window named in the summary -- plus the narrow case of a genuinely
# independent single-radar failure opening inside that window and being
# suppressed as systemic when it is not. That is the trade any latch buys, and
# it is why `latched` is reported separately in the payload.
LB_FLEET_MIN_DWELL_S = 600.0

# The verdict vocabulary this side publishes. Deliberately smaller than L2's.
#
#   ARRIVING      data is landing within the silence threshold
#   SILENT        the threshold is breached, or the day's directory is absent
#   UNREADABLE    we could not look — read timeout, or an OS error
#   CONFIG_ERROR  the profile's own pattern is malformed
#
# L2 needs GHOST_UP/CONFIRMED_DOWN/STUCK_DOWN_FLAG because radarca *declares*
# a state that can disagree with the data. A filesystem declares nothing, so
# there is no "says down and is down" case to exclude here: SILENT is the
# analog of GHOST_UP and carries the same meaning.
LB_SYSTEMIC_VERDICTS = frozenset({"SILENT", "UNREADABLE"})

# CONFIG_ERROR is excluded from the systemic set on purpose. A malformed
# pattern is shared config, so it fails every radar in the same tick by
# construction — a guaranteed instant 5/5. Counting it would make the check
# report a fleet-wide infrastructure event for a typo, which is the most
# misleading thing it could possibly say. config.py now compiles the patterns
# at import so this verdict should be unreachable in a running process; it
# exists because "should be unreachable" is not the same as "is".

# How long a published verdict stays usable. The radar checks run at 60 s and
# this one at 120 s, so a verdict can legitimately be up to two radar cycles
# old when read. 180 s accepts that without accepting a stale one.
_LB_VERDICT_TTL_S = 180.0
_lb_last_verdict: dict[str, tuple[datetime, str]] = {}

_lb_fleet_systemic: bool = False
_lb_fleet_since: datetime | None = None

# Whether correlation is possible on this profile at all.
#
# Below LB_FLEET_SYSTEMIC_ENTER members the question the check asks has no
# meaning: xqpi monitors one radar, and "is 3 of 1 radars silent" is not a
# threshold that can be reached. Registering it there would put a permanent
# skip row on the dashboard, and the row would be telling the truth, which is
# worse than its absence — it reads as a broken check rather than an
# inapplicable one.
#
# Recorded as a reason rather than a silent `if`, because "why is this check
# missing" is otherwise unanswerable. registry.DECLINED is not reused: its
# values are module names and its contract is specifically the no-HTTP-origin
# case, so widening it here would make that dict mean two things.
_FLEET_ACTIVE = len(LB_FLEET) >= LB_FLEET_SYSTEMIC_ENTER
FLEET_NOT_REGISTERED: str | None = (
    None if _FLEET_ACTIVE else
    f"{len(LB_FLEET)} correlatable radar(s) in this profile, "
    f"LB_FLEET_SYSTEMIC_ENTER is {LB_FLEET_SYSTEMIC_ENTER} — "
    f"a fleet of this size cannot reach the systemic threshold"
)


def _lb_publish_verdict(radar_id: str, verdict: str, when: datetime) -> None:
    _lb_last_verdict[radar_id] = (when, verdict)


def _lb_not_arriving(now: datetime) -> set[str]:
    """Fleet radars whose most recent non-stale verdict is in
    LB_SYSTEMIC_VERDICTS. CBAND is not a fleet member; see LB_FLEET."""
    out: set[str] = set()
    for rid in LB_FLEET:
        rec = _lb_last_verdict.get(rid)
        if rec is None:
            continue
        when, verdict = rec
        if ((now - when).total_seconds() <= _LB_VERDICT_TTL_S
                and verdict in LB_SYSTEMIC_VERDICTS):
            out.add(rid)
    return out


def _lb_fresh(now: datetime) -> dict[str, str]:
    """Fleet radars whose most recent verdict is still inside the TTL.

    Membership in _lb_last_verdict is NOT the same question. A verdict stays in
    that dict forever once published, so counting raw membership as "reported"
    lets the check assert `0/5 not arriving — pass` from verdicts that stopped
    updating hours ago. That is health claimed from stale evidence, and it is
    the precise failure the `skip` path below exists to avoid; it has to be the
    same freshness test or the floor does not hold.
    """
    out: dict[str, str] = {}
    for rid in LB_FLEET:
        rec = _lb_last_verdict.get(rid)
        if rec is None:
            continue
        when, verdict = rec
        if (now - when).total_seconds() <= _LB_VERDICT_TTL_S:
            out[rid] = verdict
    return out


def _lb_witness(now: datetime) -> str | None:
    """CBAND's current verdict, or None when it is absent or stale.

    None is a real answer and distinct from a bad one: it means the
    independent tree could not be consulted, so the fault cannot be localised
    this cycle. The summary must not imply otherwise.
    """
    rec = _lb_last_verdict.get(LB_FLEET_WITNESS)
    if rec is None:
        return None
    when, verdict = rec
    if (now - when).total_seconds() > _LB_VERDICT_TTL_S:
        return None
    return verdict


def _reset_lb_fleet_state() -> None:
    """Test hook."""
    global _lb_fleet_systemic, _lb_fleet_since
    _lb_last_verdict.clear()
    _lb_fleet_systemic = False
    _lb_fleet_since = None


class Layer2BackendRadarCheck(Check):
    """One per radar in config.RADAR_FOLDER. Reads the backend tree directly."""

    # Own stage — see layer1_backend_product for why.
    stage = "LB2"
    # Independent of radarca by design.
    depends_on: list[str] = []

    def __init__(self, radar_id: str):
        self.radar_id = radar_id
        # A radar in _special_trees() lives somewhere other than the profile's
        # default tree — on AQPI that is CBAND on trinity while the rest are on
        # K2. Otherwise it is wherever this profile publishes, which is K2 for
        # AQPI and trinity for XQPI. See config.BACKEND_SOURCE.
        if radar_id in _special_trees():
            self.source_tag, self.source_label = "TR", "Trinity"
        else:
            self.source_tag, self.source_label = BACKEND_SOURCE

        self.id = f"layer2.backend.{radar_id}"
        self.target = radar_id
        self.cadence_s = 60
        # Alarm suppression only — NOT depends_on. The fleet check is an
        # AGGREGATE OF THIS CHECK, so letting it demote us to `skip` would have
        # an aggregate marking its own inputs "not measured" when we measured
        # them precisely. See Check.alarm_only_depends_on.
        #
        # Set only when the fleet check actually registered. A dangling id is
        # tolerated by compute_suppression (an unknown id is never fail/error,
        # so it suppresses nothing) but it would be a lie in the check's own
        # declared dependencies, and `why is this not suppressed` is a question
        # someone will eventually ask of this attribute.
        if _FLEET_ACTIVE and radar_id in LB_FLEET:
            self.alarm_only_depends_on = [LB_FLEET_CHECK_ID]

    async def run(self, ctx) -> CheckResult:
        t0 = utcnow()
        sub: dict[str, str] = {}
        metrics: dict[str, float] = {}
        path = _radar_path(self.radar_id, t0)
        payload: dict[str, Any] = {"path": path, "source": "backend-filesystem"}

        if LB2_FRESHNESS == "filename":
            # Per radar, not per profile. AQPI's five X-bands are written by
            # one producer as `aqpi.<site>-<date>-<time>_...` while CBAND
            # comes off a different tree entirely as
            # `AQPI.SSCB_<date>_<time>.nc` — different case, different
            # separator, no hyphen. A single pattern matched 0 of 295 CBAND
            # files, and _newest_declared raises when nothing matches, so one
            # shared pattern would have reported "no data directory for the
            # current UTC day" against a directory holding 295 current files.
            pat = RAW_VOLUME_TS_RE[self.radar_id]
            def reader(p: str, _pat: str = pat) -> float:
                return _newest_declared(p, _pat)
        else:
            reader = _dir_mtime
        payload["freshness_basis"] = LB2_FRESHNESS

        try:
            mtime = await asyncio.wait_for(
                asyncio.to_thread(reader, path), FS_TIMEOUT_S)
        except asyncio.TimeoutError:
            _lb_publish_verdict(self.radar_id, "UNREADABLE", t0)
            return CheckResult(
                check_id=self.id, target=self.target, stage=self.stage, status="error",
                started_at=t0, finished_at=utcnow(),
                summary=f"NFS read timed out after {FS_TIMEOUT_S:g}s — mount may be hung",
                payload=payload, metrics={"fs_timeout": 1.0},
            )
        except FileNotFoundError:
            # For a dated tree this is the normal shape of "nothing has arrived today
            # yet", which just after 00Z is not yet a fault. The silence threshold is
            # what decides; treat absence as maximally stale and let it be judged.
            metrics["fs_timeout"] = 0.0
            sub["A_arriving"] = "fail"
            payload["absent"] = True
            _lb_publish_verdict(self.radar_id, "SILENT", t0)
            return _final(self, t0, sub, payload, metrics,
                          "no data directory for the current UTC day")
        except re.error as e:
            # NOT "UNREADABLE". Shared config fails every radar in the same
            # tick, so counting this toward the fleet tally would diagnose a
            # typo as a fleet-wide outage. See LB_SYSTEMIC_VERDICTS.
            _lb_publish_verdict(self.radar_id, "CONFIG_ERROR", t0)
            return CheckResult(
                check_id=self.id, target=self.target, stage=self.stage, status="error",
                started_at=t0, finished_at=utcnow(),
                summary=f"profile RAW_VOLUME_TS_RE is not a valid pattern: {e}",
                payload=payload, metrics={"fs_timeout": 0.0},
            )
        except OSError as e:
            _lb_publish_verdict(self.radar_id, "UNREADABLE", t0)
            return CheckResult(
                check_id=self.id, target=self.target, stage=self.stage, status="error",
                started_at=t0, finished_at=utcnow(),
                summary=f"backend read failed: {humanize_error(e)}",
                payload=payload, metrics={"fs_timeout": 0.0},
            )

        metrics["fs_timeout"] = 0.0
        age_s = t0.timestamp() - mtime
        metrics["age_s"] = float(age_s)

        silent_s = _thresholds.get_radar(
            self.radar_id, "backend_silent_s",
            RADAR_SILENT_FAIL_S.get(self.radar_id, 900),
        )
        metrics["backend_silent_s"] = float(silent_s)
        # Sparkline series. See helpers.headroom: the dashboard needs one
        # fixed 0..1 axis, and these limits differ per radar (300 s..1080 s).
        metrics["headroom"] = headroom(age_s, silent_s)
        # See layer1_backend_product: payload is what the reprocess engine can read.
        payload["age_s"] = round(age_s, 1)
        payload["silent_s"] = float(silent_s)

        # One threshold, three bands: a radar at 0.8x its characterised silence limit is
        # worth seeing before it crosses.
        if age_s <= silent_s * 0.8:
            sub["A_arriving"] = "pass"
        elif age_s <= silent_s:
            sub["A_arriving"] = "warn"
        else:
            sub["A_arriving"] = "fail"

        _lb_publish_verdict(
            self.radar_id,
            "SILENT" if sub["A_arriving"] == "fail" else "ARRIVING", t0)

        what = "newest volume" if LB2_FRESHNESS == "filename" else "last arrival"
        summary = (f"{what} {age_s / 60:.1f} min ago"
                   f"  (silent limit {silent_s / 60:.0f} min)")
        if sub["A_arriving"] == "fail":
            summary = "BACKEND SILENT — " + summary
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


class Layer2BackendFleetCheck(Check):
    """Backend-side fleet correlation — one event, or N radar outages?

    Fails when LB_FLEET_SYSTEMIC_ENTER or more fleet radars are simultaneously
    not arriving, which means an upstream or shared-infrastructure cause rather
    than coincident independent failures. The per-radar checks list this one in
    ``alarm_only_depends_on``, so their alarms are still recorded and still
    drawn, but the operator gets ONE page describing the real scope instead of
    five saying the same thing.

    Reads the verdicts the radar checks published this cycle rather than
    re-reading the filesystem. It must agree with them by construction, and
    re-walking five directories to compute an aggregate of five walks we just
    did would add NFS load for no new information.

    Verdict latches: once systemic it stays systemic until the count drops to
    LB_FLEET_SYSTEMIC_EXIT, and for at least LB_FLEET_MIN_DWELL_S regardless.
    See the constants for why both exist.

    What this check can say that the L2 one cannot: CBAND reads a different
    tree on a different mount, so its verdict localises the fault. See the
    block comment above LB_FLEET_CHECK_ID.
    """

    id         = LB_FLEET_CHECK_ID
    target     = "radar-fleet"
    # No new stage. This belongs with the checks it aggregates, and a stage of
    # its own would add an enumeration surface across the frontend for one row.
    stage      = "LB2"
    cadence_s  = 120
    # Independent of radarca, like every check in this module.
    depends_on: list[str] = []

    def __init__(self):
        self.source_tag, self.source_label = BACKEND_SOURCE

    async def run(self, ctx) -> CheckResult:
        t0 = utcnow()
        now = t0
        not_arriving = _lb_not_arriving(now)
        # Fresh verdicts only — see _lb_fresh. Raw dict membership would let a
        # fleet whose radar checks all stopped reporting read as healthy.
        fresh = _lb_fresh(now)
        known = sorted(fresh)

        # No verdicts yet (fresh boot, or this check ran before its peers).
        # Skip rather than assert health from an absence of evidence.
        if len(known) < LB_FLEET_SYSTEMIC_ENTER:
            return CheckResult(
                check_id=self.id, target=self.target, stage=self.stage,
                status="skip", started_at=t0, finished_at=utcnow(),
                summary=(f"awaiting radar verdicts "
                         f"({len(known)}/{len(LB_FLEET)} reported)"),
                payload={"reason": "insufficient_data",
                         "reported": sorted(known)},
            )

        n, total = len(not_arriving), len(LB_FLEET)

        global _lb_fleet_systemic, _lb_fleet_since
        held_for = ((now - _lb_fleet_since).total_seconds()
                    if _lb_fleet_since else 0.0)
        if not _lb_fleet_systemic:
            if n >= LB_FLEET_SYSTEMIC_ENTER:
                _lb_fleet_systemic, _lb_fleet_since = True, now
        else:
            if n >= LB_FLEET_SYSTEMIC_ENTER:
                # Still bad: re-arm the dwell so it measures from the most
                # recent systemic reading, not the first one.
                _lb_fleet_since = now
            elif n <= LB_FLEET_SYSTEMIC_EXIT and held_for >= LB_FLEET_MIN_DWELL_S:
                _lb_fleet_systemic, _lb_fleet_since = False, None
        systemic = _lb_fleet_systemic
        # Distinguish "still bad" from "held open by the dwell", so the row
        # says which rather than looking like a stuck check.
        latched = systemic and n < LB_FLEET_SYSTEMIC_ENTER

        witness = _lb_witness(now)
        # === What the witness actually licenses us to claim ===
        #
        # The first version branched on `witness in LB_SYSTEMIC_VERDICTS`,
        # which conflated two different facts and got both edge cases wrong:
        #
        #   * CONFIG_ERROR fell to the `else`, so a malformed pattern would
        #     have produced "host, NFS and clock are fine" on the strength of
        #     a verdict that tells us nothing about any of them.
        #   * SILENT produced "wider than the X-band path", so CBAND failing
        #     for a THRESHOLD reason read as an infrastructure fault. That is
        #     not hypothetical: on 2026-10-05 the LB2 basis change pushed
        #     CBAND past a stored 300 s limit while its tree was being read
        #     perfectly well every cycle.
        #
        # The distinction that matters for localisation is whether the
        # independent tree could be READ, not whether the data on it was
        # fresh. A successful read is what proves the host is up, NFS is
        # serving, the clock is sane and our own reads work. Freshness is a
        # separate question and a weaker signal.
        if witness is None or witness == "CONFIG_ERROR":
            scope, scope_note = "unlocalised", (
                f"{LB_FLEET_WITNESS} verdict unavailable — cannot tell the "
                f"X-band path from a wider fault this cycle")
        elif witness == "UNREADABLE":
            scope, scope_note = "wider-than-xband", (
                f"{LB_FLEET_WITNESS}'s own mount could not be read either — "
                f"this is wider than the X-band path")
        elif witness == "ARRIVING":
            scope, scope_note = "xband-path", (
                f"{LB_FLEET_WITNESS} arriving normally on its own mount — "
                f"host, NFS and clock are fine; fault is in the X-band path")
        else:
            # SILENT: the read SUCCEEDED, so the mount and host are fine; what
            # is shared is an absence of data rather than an absence of
            # service. Said precisely, because "wider than the X-band path"
            # alone would be read as an infrastructure fault.
            scope, scope_note = "wider-than-xband", (
                f"{LB_FLEET_WITNESS} is {witness} too, but its mount read "
                f"fine — host and NFS are healthy, so the fault spans both "
                f"data paths rather than being mount-level")

        verdicts = dict(sorted(fresh.items()))
        # When some radars did not report this cycle, "3/5 not arriving" reads
        # as "and the other 2 are fine" — which is not what was observed. Say
        # how many were actually seen, but only when it differs, so the
        # ordinary line stays short.
        seen = (f" [{len(known)}/{total} reported]"
                if len(known) != total else "")
        if systemic and not latched:
            summary = (f"SYSTEMIC: {n}/{total} radars not arriving{seen} "
                       f"({', '.join(sorted(not_arriving))}) — one event, "
                       f"not {n} radar outages. {scope_note}")
        elif latched:
            summary = (f"SYSTEMIC (holding): {n}/{total} not arriving{seen} — "
                       f"recent episode, still inside the "
                       f"{LB_FLEET_MIN_DWELL_S / 60:.0f} min window")
        else:
            summary = f"{n}/{total} radars not arriving{seen}"

        return CheckResult(
            check_id=self.id, target=self.target, stage=self.stage,
            status=("fail" if systemic else "pass"),
            started_at=t0, finished_at=utcnow(),
            summary=summary,
            payload={"not_arriving": sorted(not_arriving), "n": n, "of": total,
                     "systemic": systemic, "latched": latched,
                     "enter_at": LB_FLEET_SYSTEMIC_ENTER,
                     "exit_at": LB_FLEET_SYSTEMIC_EXIT,
                     "witness": LB_FLEET_WITNESS, "witness_verdict": witness,
                     # The fact the mount claim rests on, recorded separately
                     # from the freshness verdict that must not imply it.
                     "witness_mount_readable": (
                         witness in ("ARRIVING", "SILENT") if witness else None),
                     # Only meaningful when there is an event to localise.
                     # Reporting a scope on a passing fleet would imply we
                     # tried to attribute a fault that does not exist.
                     "scope": scope if systemic else None,
                     "verdicts": verdicts,
                     "source": "backend-filesystem"},
            metrics={"not_arriving_radars": float(n)},
        )


# --------------------------------------------------------------------------
# Register one instance per radar in the profile's table (FLOW on xqpi),
# plus the fleet correlation check where a fleet exists to correlate.
# --------------------------------------------------------------------------

# Gated identically to layer1_backend_product. CBAND additionally requires
# SENTINEL_SSCB_ROOT, since it does not live under the X-band trees.
if SETTINGS.backend_root:
    for _rid in RADAR_FOLDER:
        if _rid == "CBAND" and not SETTINGS.sscb_root:
            continue
        register(Layer2BackendRadarCheck(radar_id=_rid))
    if _FLEET_ACTIVE:
        register(Layer2BackendFleetCheck())
