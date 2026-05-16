"""Layer 2 — per-radar reconciliation.

One Check per radar (XSCV / XSCW / XSCR / XSWR / XEBY / CBAND). Each one:

  1. Fetches ``/api/radar-status/`` and looks up its radar's declared status.
  2. Fetches ``/api/xbandRadarImages/`` for the radar's primary moment
     (Reflectivity → CorrReflectivity) over the last hour.
  3. Also probes every other moment (Velocity, RhoHV, etc.) for the
     moment-availability matrix — exposed in the payload.
  4. Reconciles declared × observed → one of:
        HEALTHY            (declared UP, images flowing)
        CONFIRMED_DOWN     (declared DOWN, no images)
        GHOST_UP           (declared UP, no images in last hour)
        STUCK_DOWN_FLAG    (declared DOWN, images flowing)
        OBSERVED_API_ERROR

Yes, this means each cycle makes 6 redundant ``/api/radar-status/`` calls —
that endpoint is 215 B and the call takes ~70 ms; the simplicity of fully
self-contained per-radar checks is worth the marginal redundancy. If load
ever matters we can add a 30-s memo to the HTTP client.
"""
from __future__ import annotations

from ..config import (
    RADAR_FOLDER,
    SETTINGS,
    STATUS_TO_RADAR,
    X_MOMENTS,
    moment_to_prefix,
)
from ..registry import register
from .base import Check, CheckResult, utcnow

PRIMARY_MOMENT = "Reflectivity"
SILENT_FAIL_S = 600   # 10 min of silence with declared=UP = ghost


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

    async def _moment_count(self, ctx, prefix: str) -> int | None:
        try:
            r = await ctx.http.get(
                f"{SETTINGS.base}/api/xbandRadarImages/",
                params={"radarFolder": self.folder, "productPrefix": prefix},
            )
            if r.status_code != 200:
                return None
            return len(r.json().get("images", []) or [])
        except Exception:
            return None

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

        # Per-moment counts. Primary moment is reused for the verdict.
        moments: dict[str, int | None] = {}
        for m in X_MOMENTS:
            prefix = moment_to_prefix(self.radar_id, m)
            moments[m] = await self._moment_count(ctx, prefix)

        primary_n = moments.get(PRIMARY_MOMENT)
        primary_err = primary_n is None

        # Verdict
        if primary_err:
            verdict = "OBSERVED_API_ERROR"
        elif declared == "UP" and primary_n > 0:
            verdict = "HEALTHY"
        elif declared == "UP" and primary_n == 0:
            verdict = "GHOST_UP"
        elif declared == "DOWN" and primary_n == 0:
            verdict = "CONFIRMED_DOWN"
        elif declared == "DOWN" and primary_n > 0:
            verdict = "STUCK_DOWN_FLAG"
        else:
            verdict = "OBSERVED_API_ERROR"

        # Per-moment partial-failure detection (only meaningful when UP and
        # primary is flowing).
        dead_moments = [
            m for m, n in moments.items()
            if n == 0 and m != PRIMARY_MOMENT and verdict == "HEALTHY"
        ]

        metrics: dict[str, float] = {}
        for m, n in moments.items():
            if n is not None:
                metrics[f"images_{m.replace(' ', '_')}"] = float(n)

        summary = f"declared={declared}  obs={primary_n}  → {verdict}"
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
                "observed": {"primary": primary_n, "primary_moment": PRIMARY_MOMENT},
                "moments": moments,
                "dead_moments": dead_moments,
                "verdict": verdict,
            },
            metrics=metrics,
        )


for _r in RADAR_FOLDER:
    register(Layer2RadarReconcile(radar_id=_r))
