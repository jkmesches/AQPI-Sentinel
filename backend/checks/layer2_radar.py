"""Layer 2 — per-radar reconciliation.

One Check per radar (XSCV / XSCW / XSCR / XSWR / XEBY / CBAND). Each one:

  1. Fetches ``/api/radar-status/`` and looks up its radar's declared status.
  2. Fetches ``/api/xbandRadarImages/`` for the radar's primary moment
     (Reflectivity → CorrReflectivity) over the last hour. The endpoint
     returns up to ~1 h of filenames in chronological order; we use the
     last entry's embedded timestamp to decide whether the radar is
     CURRENTLY emitting vs. just has stale frames still in the window.
  3. Also probes every other moment (Velocity, RhoHV, etc.) for the
     moment-availability matrix — exposed in the payload.
  4. Reconciles declared × observed-freshness → one of:
        HEALTHY            (declared UP, fresh images flowing)
        CONFIRMED_DOWN     (declared DOWN, no fresh images)
        GHOST_UP           (declared UP, no fresh images in last
                            SILENT_FAIL_S seconds — silent failure)
        STUCK_DOWN_FLAG    (declared DOWN, fresh images flowing →
                            declaration is stuck)
        OBSERVED_API_ERROR

The ``/api/radar-status/`` fetch is shared across all six radar checks via a
short-TTL single-flight memo (see ``_radar_status_map``). It used to be six
independent calls per cycle on the reasoning that the endpoint is 215 B and
~70 ms, so the redundancy was worth the simplicity. That stopped being true
on 2026-08-16, when upstream latency changed regime: a 25-probe sample
measured p50 3.4 s / p90 17.5 s. Six serial slow calls per cycle is bad, but
the real problem was correctness — one slow upstream response produced six
independent "radar unreachable" errors, which rendered as six simultaneous
radar outages. One fetch, one verdict.
"""
from __future__ import annotations
import asyncio
import time
from datetime import datetime
from typing import Any

from .. import thresholds as _thresholds
from ..config import (
    RADAR_FOLDER,
    RADAR_SILENT_FAIL_S,
    SETTINGS,
    STATUS_TO_RADAR,
    X_MOMENTS,
    moment_to_prefix,
)
from ..registry import register
from .base import Check, CheckResult, utcnow
from .helpers import parse_filename_ts

PRIMARY_MOMENT = "Reflectivity"

# --- shared /api/radar-status/ fetch -------------------------------------
#
# TTL is deliberately shorter than the radar cadence (120 s) so every cycle
# still gets a fresh reading, but the six radar checks within one cycle share
# a single upstream call. The lock makes it single-flight: the first check
# through fetches, the rest await that same result rather than piling six
# concurrent requests onto an already-slow endpoint.
_STATUS_TTL_S = 45.0
_status_cache: dict[str, Any] = {"at": None, "mapping": None, "error": None}
_status_lock = asyncio.Lock()


async def _radar_status_map(ctx) -> tuple[dict[str, str] | None, str | None]:
    """Return (radar_id -> declared status, error_reason).

    Exactly one of the two is non-None. The error string is deliberately
    specific about *which* failure occurred — the previous code collapsed
    every path into the literal summary "radar-status API unreachable",
    including the case where the API answered 200 and simply didn't mention
    the radar, which made 10,023 log lines actively misleading.
    """
    now = time.monotonic()
    async with _status_lock:
        c = _status_cache
        if c["at"] is not None and (now - c["at"]) < _STATUS_TTL_S:
            return c["mapping"], c["error"]
        try:
            r = await ctx.http.get(f"{SETTINGS.base}/api/radar-status/")
            if r.status_code != 200:
                mapping, err = None, f"radar-status API HTTP {r.status_code}"
            else:
                rows = r.json()
                mapping = {
                    STATUS_TO_RADAR.get(row["radar"], row["radar"]): row["status"]
                    for row in rows
                }
                err = None
        except Exception as e:
            mapping, err = None, f"radar-status API unreachable: {type(e).__name__}"
        c["at"], c["mapping"], c["error"] = time.monotonic(), mapping, err
        return mapping, err


def _reset_status_cache() -> None:
    """Test hook — drop the memo so a test can control the next fetch."""
    _status_cache.update({"at": None, "mapping": None, "error": None})


# --- fleet-wide correlation ----------------------------------------------
#
# Why this exists: on 2026-08-13 four X-band radars went GHOST_UP at
# 12:29:52 and recovered at 19:29:54 three days later — the same second, to
# sub-second precision, across four geographically separate sites. Measured
# over 14 days, 80.2% of all GHOST_UP runs occurred while 4-5 radars were
# ghosting simultaneously; only 2.8% were isolated to one radar.
#
# Radars at different sites do not fail in lockstep. Such an episode is ONE
# upstream event, and reporting it as five independent multi-day radar
# outages both over-counts incidents and pages five times for one problem.
#
# The fleet check below turns that correlation into a first-class verdict;
# the X-band radar checks depend on it, so the existing dependency-suppression
# machinery records their alarms but suppresses five duplicate notifications.
XBAND_FLEET = frozenset(r for r in RADAR_FOLDER if r != "CBAND")
FLEET_CHECK_ID = "layer2.xband.fleet"

# 4 of 5, not all 5: a genuinely-broken radar during a systemic event should
# not stop us recognising the systemic event.
FLEET_SYSTEMIC_MIN = 4

# Which verdicts count toward a systemic diagnosis. Deliberately NOT
# "anything that isn't HEALTHY":
#
#   GHOST_UP           declared UP, no fresh data — the signature of the real
#                      episodes, and the thing that should never correlate
#                      across sites.
#   OBSERVED_API_ERROR we could not observe the radar at all.
#
# Excluded on purpose:
#   CONFIRMED_DOWN     upstream declares it down and it is down. A correctly
#                      reported, already-known outage is not evidence of an
#                      anomaly, and counting it inflates the systemic tally
#                      with radars nobody is confused about.
#   STUCK_DOWN_FLAG    data IS flowing (declared down, images fresh). Counting
#                      a radar that is actively producing as "unhealthy" for
#                      correlation purposes would be plainly wrong.
#
# Measured over 14 days to 2026-08-25: "anything not HEALTHY" would mark 5,300
# minutes systemic vs 4,887 for this definition — 413 minutes (7.8%) that were
# padded by declared-down radars rather than genuine correlation. The real
# 79-hour episode was entirely GHOST_UP, so it is still caught.
SYSTEMIC_VERDICTS = frozenset({"GHOST_UP", "OBSERVED_API_ERROR"})

# How long a published verdict stays usable. Checks run on a 120 s cadence and
# the fleet check may run before or after its peers within a tick, so accept
# anything from roughly the last two cycles. These episodes last hours; a
# one-tick lag is immaterial.
_VERDICT_TTL_S = 300.0
_last_verdict: dict[str, tuple[datetime, str]] = {}


def _publish_verdict(radar_id: str, verdict: str, when: datetime) -> None:
    _last_verdict[radar_id] = (when, verdict)


def _not_reporting_xband(now: datetime) -> set[str]:
    """X-band radars that should be producing data but aren't (or can't be
    observed), per their most recent non-stale verdict. See SYSTEMIC_VERDICTS
    for why this is narrower than "not HEALTHY"."""
    out: set[str] = set()
    for rid in XBAND_FLEET:
        rec = _last_verdict.get(rid)
        if rec is None:
            continue
        when, verdict = rec
        if (now - when).total_seconds() <= _VERDICT_TTL_S and verdict in SYSTEMIC_VERDICTS:
            out.add(rid)
    return out


def _reset_fleet_state() -> None:
    """Test hook."""
    _last_verdict.clear()


# Default GHOST_UP detection threshold (used when a radar isn't listed in
# RADAR_SILENT_FAIL_S). Per-radar overrides exist because different radars
# have legitimately different scan cadences; a one-size threshold either
# false-fires on the slower ones (CBAND ~5 min) or lags on the faster ones
# (XSWR's perfect 2-min cadence wants a tight 4 min threshold).
SILENT_FAIL_S_DEFAULT = 600

# ±10% hysteresis band around silent_fail_s. A radar transitions FROM
# fresh TO stale only when age exceeds threshold × (1 + HYSTERESIS). It
# transitions back to fresh only when age drops below threshold × (1 -
# HYSTERESIS). Within the band, the previous fresh/stale state is held.
# Prevents flapping when a radar's actual cadence sits right at the
# threshold boundary (CBAND was the original instigator).
HYSTERESIS = 0.10

# Per-check fresh/stale state for hysteresis. Module-level so it survives
# across run() calls; resets on process restart (in which case the first
# run after restart trips the upper-bound threshold cleanly).
_LAST_FRESH: dict[str, bool] = {}

# X-band radars publish no imagery for these moments at all (upstream
# quirk — only CBAND emits RhoHV). Without this list, every X-band
# radar's `dead_moments` field would always include "RhoHV" on a healthy
# run, which would false-trip any future alert that fired on dead_moments.
# Empty set means "all moments expected."
EXPECTED_ABSENT_MOMENTS: dict[str, set[str]] = {
    "XEBY":  {"RhoHV"},
    "XSCV":  {"RhoHV"},
    "XSCW":  {"RhoHV"},
    "XSCR":  {"RhoHV"},
    "XSWR":  {"RhoHV"},
    "CBAND": set(),
}


# Verdict → CheckResult.status mapping
_VERDICT_STATUS = {
    "HEALTHY":           "pass",
    "CONFIRMED_DOWN":    "fail",
    "GHOST_UP":          "fail",   # silent failure: more concerning than confirmed
    "STUCK_DOWN_FLAG":   "warn",
    "OBSERVED_API_ERROR": "error",
    "DECLARED_API_ERROR": "error",
}


class Layer2RadarReconcile(Check):
    stage = "L2"
    cadence_s = 120
    depends_on = ["layer0.origin.alive"]

    # X-band members additionally depend on the fleet check (set in __init__)
    # so that a fleet-wide event suppresses the five per-radar alarms in
    # favour of one systemic alarm. See Layer2XbandFleet.

    def __init__(self, radar_id: str):
        self.radar_id = radar_id
        self.folder = RADAR_FOLDER[radar_id]
        self.id = f"layer2.radar.{radar_id}"
        self.target = radar_id
        # silent_fail_s is read fresh on every run() so admin edits take
        # effect without restart. Cached self.silent_fail_s retained as a
        # last-resort static default if the threshold module isn't ready.
        self.silent_fail_s = RADAR_SILENT_FAIL_S.get(radar_id, SILENT_FAIL_S_DEFAULT)
        # Per-instance so CBAND — a different band at a different site, which
        # stayed healthy right through the multi-day X-band episodes — is not
        # suppressed by an X-band fleet event.
        if radar_id in XBAND_FLEET:
            self.depends_on = [*Layer2RadarReconcile.depends_on, FLEET_CHECK_ID]

    async def _declared(self, ctx) -> tuple[str | None, str | None]:
        """Look up this radar's declared status. Returns (status, error).

        Exactly one is non-None. Distinguishing "upstream didn't answer" from
        "upstream answered but omitted this radar" matters: the first is an
        upstream-wide event affecting all six checks at once, the second is
        specific to this radar. They previously produced identical summaries.
        """
        mapping, err = await _radar_status_map(ctx)
        if err is not None:
            return None, err
        if mapping is None or self.radar_id not in mapping:
            return None, f"{self.radar_id} absent from radar-status payload"
        return mapping[self.radar_id], None

    async def _moment_data(
        self, ctx, prefix: str,
    ) -> tuple[int | None, datetime | None]:
        """Return (count, newest_filename_ts).

        Both are None on transport error. count=0 + newest=None when the
        list is empty. The upstream returns chronologically-sorted PNG
        filenames; we parse the timestamp out of the last entry to gate
        on recency, not just presence.
        """
        try:
            r = await ctx.http.get(
                f"{SETTINGS.base}/api/xbandRadarImages/",
                params={"radarFolder": self.folder, "productPrefix": prefix},
            )
            if r.status_code != 200:
                return None, None
            images = r.json().get("images", []) or []
        except Exception:
            return None, None
        if not images:
            return 0, None
        # Filenames come oldest→newest. Walk from the end so the first
        # parseable one is the newest emitted scan.
        newest_ts: datetime | None = None
        for name in reversed(images):
            ts = parse_filename_ts(name if isinstance(name, str) else "")
            if ts is not None:
                newest_ts = ts
                break
        return len(images), newest_ts

    async def run(self, ctx) -> CheckResult:
        t0 = utcnow()
        declared, declared_err = await self._declared(ctx)
        if declared is None:
            return CheckResult(
                check_id=self.id, target=self.target, stage=self.stage,
                status="error",
                started_at=t0, finished_at=utcnow(),
                summary=declared_err or "radar-status unavailable",
                # upstream_api marks this as "our probe could not determine the
                # radar's state", NOT "the radar is down". The history/timeline
                # layer ranks it below a real failure on that basis.
                payload={"radar": self.radar_id, "folder": self.folder,
                         "declared": None, "reason": "upstream_api",
                         "error": declared_err},
            )

        # Per-moment counts + newest-filename-ts. Primary moment drives
        # the verdict; the rest populate the moments matrix.
        moments: dict[str, int | None] = {}
        moment_newest: dict[str, datetime | None] = {}
        for m in X_MOMENTS:
            prefix = moment_to_prefix(self.radar_id, m)
            n, ts = await self._moment_data(ctx, prefix)
            moments[m] = n
            moment_newest[m] = ts

        primary_n = moments.get(PRIMARY_MOMENT)
        primary_err = primary_n is None
        primary_ts = moment_newest.get(PRIMARY_MOMENT)

        # "Fresh" = at least one image AND its timestamp is within this
        # radar's silent_fail_s window (per-radar threshold; see config
        # RADAR_SILENT_FAIL_S for the values + rationale). Without this
        # gate, a stale 1 h window would masquerade as a healthy radar.
        # Tolerant of missing/unparseable filename timestamps: treat as
        # fresh when we have a non-zero count but can't parse the time
        # (better to false-pass than to false-fail on filename-format
        # drift).
        #
        # Hysteresis: when the newest scan's age sits between
        # threshold × (1 ± HYSTERESIS), we hold the previous verdict.
        # Outside the band we transition. This stops flapping at the
        # boundary — important for radars (CBAND) whose cadence is
        # naturally variable.
        now = utcnow()
        # Read live values so admin edits to silent_fail_s / hysteresis take
        # effect on the next tick. Defaults preserve current behavior if the
        # thresholds module hasn't initialized yet (won't happen post-boot).
        silent_fail_s = float(_thresholds.get_radar(
            self.radar_id, "silent_fail_s", self.silent_fail_s,
        ))
        hyst = float(_thresholds.get_global("hysteresis", HYSTERESIS))
        upper = silent_fail_s * (1 + hyst)
        lower = silent_fail_s * (1 - hyst)
        prev_fresh = _LAST_FRESH.get(self.id, True)
        if primary_err or primary_n is None:
            fresh = False
        elif primary_n == 0:
            fresh = False
        elif primary_ts is None:
            fresh = True
        else:
            age_s = (now - primary_ts).total_seconds()
            if prev_fresh:
                # currently fresh: stale only once age exceeds upper bound
                fresh = age_s <= upper
            else:
                # currently stale: fresh only once age drops below lower bound
                fresh = age_s <= lower
        _LAST_FRESH[self.id] = fresh

        # Verdict
        if primary_err:
            verdict = "OBSERVED_API_ERROR"
        elif declared == "UP" and fresh:
            verdict = "HEALTHY"
        elif declared == "UP":            # not fresh — count=0 OR last image too old
            verdict = "GHOST_UP"
        elif declared == "DOWN" and not fresh:
            verdict = "CONFIRMED_DOWN"
        elif declared == "DOWN" and fresh:
            verdict = "STUCK_DOWN_FLAG"
        else:
            verdict = "OBSERVED_API_ERROR"

        # Per-moment partial-failure detection. Only meaningful when the
        # radar is HEALTHY. EXPECTED_ABSENT_MOMENTS filters out moments
        # this radar never publishes anyway (e.g. RhoHV on X-band).
        expected_absent = EXPECTED_ABSENT_MOMENTS.get(self.radar_id, set())
        dead_moments = [
            m for m, n in moments.items()
            if n == 0
            and m != PRIMARY_MOMENT
            and m not in expected_absent
            and verdict == "HEALTHY"
        ]

        metrics: dict[str, float] = {}
        for m, n in moments.items():
            if n is not None:
                metrics[f"images_{m.replace(' ', '_')}"] = float(n)
        if primary_ts is not None:
            metrics["primary_age_s"] = float((now - primary_ts).total_seconds())

        # Publish this radar's verdict for the fleet check (and read back how
        # many peers are currently not reporting) BEFORE building the summary, so
        # a systemic event is visible in the text an operator reads first.
        _publish_verdict(self.radar_id, verdict, now)
        systemic = None
        if self.radar_id in XBAND_FLEET:
            peers = _not_reporting_xband(now)
            if verdict in SYSTEMIC_VERDICTS and len(peers) >= FLEET_SYSTEMIC_MIN:
                systemic = {"scope": "xband", "not_reporting": sorted(peers),
                            "n": len(peers), "of": len(XBAND_FLEET)}

        summary = f"declared={declared}  obs={primary_n}  → {verdict}"
        if systemic:
            summary += f"  [systemic: {systemic['n']}/{systemic['of']} X-band not reporting]"
        if primary_ts is not None and primary_n and primary_n > 0:
            age_s = int((now - primary_ts).total_seconds())
            summary += f"  last={age_s}s"
        if dead_moments:
            summary += f"  dead_moments={dead_moments}"

        return CheckResult(
            check_id=self.id, target=self.target, stage=self.stage,
            status=_VERDICT_STATUS.get(verdict, "error"),
            started_at=t0, finished_at=utcnow(),
            summary=summary,
            payload={
                "radar": self.radar_id,
                "folder": self.folder,
                "declared": declared,
                "observed": {
                    "primary": primary_n,
                    "primary_moment": PRIMARY_MOMENT,
                    "primary_newest_ts": primary_ts.isoformat() if primary_ts else None,
                    "primary_age_s": (
                        int((now - primary_ts).total_seconds()) if primary_ts else None
                    ),
                    "fresh": fresh,
                    "silent_fail_s": self.silent_fail_s,
                    "silent_fail_band": [int(lower), int(upper)],
                },
                "moments": moments,
                "moment_newest_ts": {
                    m: (ts.isoformat() if ts else None)
                    for m, ts in moment_newest.items()
                },
                "dead_moments": dead_moments,
                "expected_absent_moments": sorted(expected_absent),
                "verdict": verdict,
                # See the _declared error path: OBSERVED_API_ERROR means the
                # image-list endpoint failed, so we could not observe the
                # radar — that is a gap in our visibility, not evidence the
                # radar is broken. Tagged so history ranks it below a
                # genuine failure instead of painting it the same red.
                "reason": ("upstream_api" if verdict == "OBSERVED_API_ERROR"
                           else None),
                "systemic": systemic,
            },
            metrics=metrics,
        )


class Layer2XbandFleet(Check):
    """Fleet-wide X-band correlation — is this one event or five?

    Fails when FLEET_SYSTEMIC_MIN or more of the five X-band radars are
    simultaneously not reporting, which empirically means an upstream/systemic
    cause rather than coincident independent failures. The per-radar checks
    list this one in depends_on, so when it fails their alarms are opened but
    marked suppressed_by — the operator gets one actionable page describing
    the real scope instead of five saying the same thing.

    Deliberately reads verdicts published by the radar checks rather than
    re-fetching upstream: it must agree with them by construction, and it
    adds no load to an endpoint whose slowness started this whole
    investigation.
    """

    id         = FLEET_CHECK_ID
    target     = "xband-fleet"
    stage      = "L2"
    cadence_s  = 120
    depends_on = ["layer0.origin.alive"]

    async def run(self, ctx) -> CheckResult:
        t0 = utcnow()
        now = utcnow()
        not_reporting = _not_reporting_xband(now)
        known = [r for r in XBAND_FLEET if r in _last_verdict]

        # No verdicts yet (fresh boot, or this check ran before its peers).
        # Skip rather than assert health from absence of evidence.
        if len(known) < FLEET_SYSTEMIC_MIN:
            return CheckResult(
                check_id=self.id, target=self.target, stage=self.stage,
                status="skip", started_at=t0, finished_at=utcnow(),
                summary=f"awaiting radar verdicts ({len(known)}/{len(XBAND_FLEET)} reported)",
                payload={"reason": "insufficient_data", "reported": sorted(known)},
            )

        n, total = len(not_reporting), len(XBAND_FLEET)
        systemic = n >= FLEET_SYSTEMIC_MIN
        verdicts = {r: _last_verdict[r][1] for r in sorted(known)}
        return CheckResult(
            check_id=self.id, target=self.target, stage=self.stage,
            status=("fail" if systemic else "pass"),
            started_at=t0, finished_at=utcnow(),
            summary=(
                f"SYSTEMIC: {n}/{total} X-band radars not reporting "
                f"({', '.join(sorted(not_reporting))}) — one upstream event, not {n} radar outages"
                if systemic else
                f"{n}/{total} X-band radars not reporting"
            ),
            payload={"not_reporting": sorted(not_reporting), "n": n, "of": total,
                     "systemic": systemic, "verdicts": verdicts},
            metrics={"not_reporting_radars": float(n)},
        )


for _r in RADAR_FOLDER:
    register(Layer2RadarReconcile(radar_id=_r))
register(Layer2XbandFleet())
