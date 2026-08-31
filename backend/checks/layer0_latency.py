"""Uncensored latency canary.

Every other check kills a request at DEFAULT_TIMEOUT_S, which means every
latency we record is conditioned on NOT having timed out. The distribution is
censored at the ceiling and can never show a value above it — so it cannot be
used to decide whether the ceiling is in the right place. That circularity is
exactly how "p99 is 19.2s, so 20s is not cutting into traffic" got believed on
2026-08-31 while 1-2% of runs were being killed at precisely 20s.

This check exists to break the circle. It probes the same endpoint the fleet
leans on hardest, with a ceiling far above the operational one, so the tail
past DEFAULT_TIMEOUT_S is observed rather than inferred. It is the only place
in the stack that can answer "how long would that request have taken?".

Cost is deliberately trivial: one request every five minutes, never
concurrent with itself. That is ~288 uncensored samples a day, which is ample
to characterise a tail, against a fleet that already issues tens of thousands.

It does NOT alarm on slowness. A slow-but-successful response is precisely the
thing being measured, and turning it into a page would re-import the noise
this whole line of work has been removing. The verdict is `pass` whenever
upstream answers at all; the signal lives in the metrics. Only a failure to
answer within CANARY_TIMEOUT_S — far beyond anything observed, including the
42s seen during an episode — is treated as an error.

Reading it:

    -- true tail, including what the operational ceiling would have hidden
    SELECT percentile_cont(0.99) WITHIN GROUP (ORDER BY value)
    FROM metric_samples
    WHERE check_id = 'layer0.origin.latency' AND metric = 'latency_ms';

    -- share of requests the operational ceiling would have killed
    SELECT avg(value) FROM metric_samples
    WHERE check_id = 'layer0.origin.latency' AND metric = 'over_ceiling';

If `over_ceiling` sits materially above zero for a sustained period, the
ceiling is truncating real traffic and DEFAULT_TIMEOUT_S wants revisiting.
If it is ~0 while checks still time out, the cause is contention or episodes,
not the ceiling, and raising it would not help.
"""
from __future__ import annotations

import httpx

from ..config import SETTINGS
from ..errors import humanize_error
from ..registry import register
from .base import Check, CheckResult, utcnow
from .transports.http import DEFAULT_TIMEOUT_S

# Well beyond the worst observed (42s during a slow episode on 2026-08-31), so
# a sample is only lost if upstream has genuinely stopped answering.
CANARY_TIMEOUT_S = 75.0


@register
class Layer0OriginLatency(Check):
    id         = "layer0.origin.latency"
    stage      = "L0"
    target     = "origin-latency"
    cadence_s  = 300
    reports_timeout_rate = True
    # Parented on the same upstream-blame chain as origin.alive: if our own
    # network is down this measurement is meaningless, not evidence about them.
    depends_on = ["layer0.net.internet", "layer0.net.dns"]

    async def run(self, ctx) -> CheckResult:
        t0 = utcnow()
        try:
            r = await ctx.http.get(
                f"{SETTINGS.base}/api/radar-status/",
                timeout=CANARY_TIMEOUT_S,
            )
        except Exception as e:  # noqa: BLE001 — a lost sample is not an outage
            elapsed_ms = (utcnow() - t0).total_seconds() * 1000
            return CheckResult(
                check_id=self.id, target=self.target, stage=self.stage,
                status="error", started_at=t0, finished_at=utcnow(),
                summary=f"no answer within {CANARY_TIMEOUT_S:.0f}s: {humanize_error(e)}",
                payload={"exception": type(e).__name__, "reason": "upstream_api",
                         "ceiling_s": CANARY_TIMEOUT_S},
                metrics={"latency_censored_ms": elapsed_ms, "timed_out": 1.0},
            )

        elapsed_ms = (utcnow() - t0).total_seconds() * 1000
        over = elapsed_ms > DEFAULT_TIMEOUT_S * 1000
        return CheckResult(
            check_id=self.id, target=self.target, stage=self.stage,
            # `pass` even when slow: measuring the tail is the job, and paging
            # on it would recreate the noise this is meant to explain.
            status="pass" if r.status_code == 200 else "warn",
            started_at=t0, finished_at=utcnow(),
            summary=(
                f"{elapsed_ms / 1000:.1f}s"
                + (f" — past the {DEFAULT_TIMEOUT_S:.0f}s ceiling, "
                   f"this request would have failed every other check" if over else "")
            ),
            payload={"http": r.status_code, "latency_ms": round(elapsed_ms),
                     "over_ceiling": over, "ceiling_s": DEFAULT_TIMEOUT_S},
            metrics={
                "latency_ms": elapsed_ms,
                # 1 when this request would have been killed by the
                # operational ceiling. avg() over a window is the truncation
                # rate the censored metric cannot show.
                "over_ceiling": 1.0 if over else 0.0,
                "timed_out": 0.0,
            },
        )
