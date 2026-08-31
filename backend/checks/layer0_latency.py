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

import time

from ..config import SETTINGS
from ..errors import humanize_error
from ..registry import register
from .base import Check, CheckResult, utcnow
from .layer0_episode import in_episode
from .transports.http import DEFAULT_TIMEOUT_S

# Well beyond the worst observed (42s during a slow episode on 2026-08-31), so
# a sample is only lost if upstream has genuinely stopped answering.
CANARY_TIMEOUT_S = 75.0

# === Load-bearing: sample where the interesting behaviour is ===
#
# The first version sampled every 300s flat, and the first hour of data showed
# why that is not enough. Episodes last about a minute; a 300s sampler catches
# one second in every 300, so of 13 samples exactly one landed inside an
# episode minute — and the largest reading (19.5s) fell ~10s BEFORE an episode
# rather than during it. The canary characterised the baseline well and almost
# entirely missed the regime that produces the timeouts, which is the regime it
# was built to measure.
#
# So: baseline every BASELINE_INTERVAL_S, but while the episode detector says
# one is in progress, sample every EPISODE_INTERVAL_S instead. That is a burst
# of extra requests precisely when upstream is already struggling, which is why
# the episode interval is not tighter — one probe every 45s against a saturated
# origin is a rounding error next to the fleet, and without it the tail during
# an episode stays unmeasured.
BASELINE_INTERVAL_S = 300.0
EPISODE_INTERVAL_S = 45.0

# Monotonic, so a clock step cannot make the canary stop sampling entirely.
_last_probe_at: float | None = None


@register
class Layer0OriginLatency(Check):
    id         = "layer0.origin.latency"
    stage      = "L0"
    target     = "origin-latency"
    # Runs every 60s but only PROBES when due (see the interval
    # constants). The short cadence exists to react to an episode
    # quickly, not to add load.
    cadence_s  = 60
    reports_timeout_rate = True
    # Parented on the same upstream-blame chain as origin.alive: if our own
    # network is down this measurement is meaningless, not evidence about them.
    depends_on = ["layer0.net.internet", "layer0.net.dns"]

    async def run(self, ctx) -> CheckResult:
        global _last_probe_at
        t0 = utcnow()

        episode = in_episode()
        now_m = time.monotonic()
        due_after = EPISODE_INTERVAL_S if episode else BASELINE_INTERVAL_S
        if _last_probe_at is not None and (now_m - _last_probe_at) < due_after:
            # Not due. Skip rather than probe: this check runs on a short
            # cadence only so it can react to an episode quickly, not so it can
            # add load every minute.
            return CheckResult(
                check_id=self.id, target=self.target, stage=self.stage,
                status="skip", started_at=t0, finished_at=utcnow(),
                summary=f"next sample in {due_after - (now_m - _last_probe_at):.0f}s"
                        + (" (episode cadence)" if episode else ""),
                payload={"reason": "not_due", "episode": episode,
                         "interval_s": due_after},
            )
        _last_probe_at = now_m

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
                metrics={"latency_censored_ms": elapsed_ms, "timed_out": 1.0,
                         "during_episode": 1.0 if episode else 0.0},
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
                     "over_ceiling": over, "ceiling_s": DEFAULT_TIMEOUT_S,
                     "during_episode": episode},
            metrics={
                "latency_ms": elapsed_ms,
                # 1 when this request would have been killed by the
                # operational ceiling. avg() over a window is the truncation
                # rate the censored metric cannot show.
                "over_ceiling": 1.0 if over else 0.0,
                "timed_out": 0.0,
                # Lets the tail be computed separately for the two regimes:
                #   ... WHERE metric='latency_ms' AND during_episode = 1
                # is the distribution that actually decides the ceiling.
                "during_episode": 1.0 if episode else 0.0,
            },
        )
