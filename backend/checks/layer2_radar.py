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

Yes, this means each cycle makes 6 redundant ``/api/radar-status/`` calls —
that endpoint is 215 B and the call takes ~70 ms; the simplicity of fully
self-contained per-radar checks is worth the marginal redundancy. If load
ever matters we can add a 30-s memo to the HTTP client.
"""
from __future__ import annotations
from datetime import datetime

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

# Default GHOST_UP detection threshold (used when a radar isn't listed in
# RADAR_SILENT_FAIL_S). Per-radar overrides exist because different radars
# have legitimately different scan cadences; a one-size threshold either
# false-fires on the slower ones (CBAND ~5 min) or lags on the faster ones
# (XSWR's perfect 2-min cadence wants a tight 4 min threshold).
SILENT_FAIL_S_DEFAULT = 600

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

    def __init__(self, radar_id: str):
        self.radar_id = radar_id
        self.folder = RADAR_FOLDER[radar_id]
        self.id = f"layer2.radar.{radar_id}"
        self.target = radar_id
        self.silent_fail_s = RADAR_SILENT_FAIL_S.get(radar_id, SILENT_FAIL_S_DEFAULT)

    async def _declared(self, ctx) -> str | None:
        """Look up this radar's status from the shared /api/radar-status/ row.
        Returns None on transport error."""
        try:
            r = await ctx.http.get(f"{SETTINGS.base}/api/radar-status/")
            if r.status_code != 200:
                return None
            rows = r.json()
        except Exception:
            return None
        mapping = {STATUS_TO_RADAR.get(row["radar"], row["radar"]): row["status"]
                   for row in rows}
        return mapping.get(self.radar_id)

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
        declared = await self._declared(ctx)
        if declared is None:
            return CheckResult(
                check_id=self.id, target=self.target, stage=self.stage,
                status="error",
                started_at=t0, finished_at=utcnow(),
                summary="radar-status API unreachable",
                payload={"radar": self.radar_id, "folder": self.folder,
                         "declared": None},
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
        now = utcnow()
        if primary_err or primary_n is None:
            fresh = False
        elif primary_n == 0:
            fresh = False
        elif primary_ts is None:
            fresh = True
        else:
            fresh = (now - primary_ts).total_seconds() <= self.silent_fail_s

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

        summary = f"declared={declared}  obs={primary_n}  → {verdict}"
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
                },
                "moments": moments,
                "moment_newest_ts": {
                    m: (ts.isoformat() if ts else None)
                    for m, ts in moment_newest.items()
                },
                "dead_moments": dead_moments,
                "expected_absent_moments": sorted(expected_absent),
                "verdict": verdict,
            },
            metrics=metrics,
        )


for _r in RADAR_FOLDER:
    register(Layer2RadarReconcile(radar_id=_r))
