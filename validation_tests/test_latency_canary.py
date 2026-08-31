"""Uncensored latency canary, and the censored-observation metrics.

Run directly (no DB, no network):

    python validation_tests/test_latency_canary.py

Every other check kills a request at DEFAULT_TIMEOUT_S, so every latency we
record is conditioned on not having timed out: the distribution is censored at
the ceiling and structurally cannot show a value above it. Using it to judge
the ceiling is circular, and that circularity was believed in practice on
2026-08-31 ("p99 is 19.2s, so 20s is not cutting into traffic") while 1-2% of
runs were being killed at exactly 20s.

Two mechanisms fix that, and both have a way to be quietly useless:

  - The censored metric must be emitted on SUCCESS as well as failure, or
    avg(timed_out) has no denominator and silently reads as 1.0 — implying
    everything times out.
  - The canary must actually use a ceiling above the operational one. If it
    inherits DEFAULT_TIMEOUT_S it is censored in exactly the same way as the
    thing it is supposed to measure, while looking like it works.

Both are asserted below, along with the property that makes the canary safe to
run at all: it must not alarm on slowness, or it re-imports the noise this
whole line of work exists to remove.
"""
from __future__ import annotations
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("SENTINEL_DB_URL", "postgresql://unused/unused")

import httpx                                                  # noqa: E402
import backend.checks as _all                                 # noqa: E402,F401
from backend.registry import CHECKS                           # noqa: E402
from backend.checks.layer0_latency import CANARY_TIMEOUT_S    # noqa: E402
from backend.checks.transports.http import DEFAULT_TIMEOUT_S  # noqa: E402

failures: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if cond else 'FAIL'}  {label}" + (f"  — {detail}" if detail else ""))
    if not cond:
        failures.append(label)


class _Resp:
    def __init__(self, code=200, body=b"[]"):
        self.status_code, self.content = code, body
        self.headers = {"content-type": "application/json"}


class _Ctx:
    """Records the timeout each call was made with, and fakes elapsed time."""
    def __init__(self, delay_s=0.0, exc=None):
        self.delay_s, self.exc, self.timeouts = delay_s, exc, []

        class http:
            @staticmethod
            async def get(url, **kw):
                self.timeouts.append(kw.get("timeout"))
                if self.exc:
                    raise self.exc
                if self.delay_s:
                    await asyncio.sleep(self.delay_s)
                return _Resp()
        self.http = http


async def main() -> int:
    canary = CHECKS["layer0.origin.latency"]

    # --- 1. the canary must not inherit the operational ceiling ----------
    ctx = _Ctx()
    r = await canary.run(ctx)
    check("the canary passes its own, longer ceiling to the client",
          ctx.timeouts == [CANARY_TIMEOUT_S], str(ctx.timeouts))
    check("that ceiling is meaningfully above the operational one",
          CANARY_TIMEOUT_S > DEFAULT_TIMEOUT_S * 2,
          f"canary {CANARY_TIMEOUT_S}s vs ceiling {DEFAULT_TIMEOUT_S}s")

    # --- 2. it records an uncensored latency, with a denominator ---------
    check("a fast response is `pass`", r.status == "pass", r.status)
    check("latency is recorded", "latency_ms" in r.metrics, str(sorted(r.metrics)))
    check("timed_out is emitted on SUCCESS too, so avg() has a denominator",
          r.metrics.get("timed_out") == 0.0, str(r.metrics.get("timed_out")))
    check("a fast response is not over the ceiling",
          r.metrics.get("over_ceiling") == 0.0, str(r.metrics.get("over_ceiling")))

    # --- 3. a response past the operational ceiling ----------------------
    # Waiting a real 20s would make the suite useless, so shrink the ceiling
    # the module compares against and cross it for real. This exercises the
    # actual comparison rather than asserting it against itself.
    import backend.checks.layer0_latency as LC
    real_ceiling = LC.DEFAULT_TIMEOUT_S
    LC.DEFAULT_TIMEOUT_S = 0.05
    try:
        slow = _Ctx(delay_s=0.12)
        r2 = await canary.run(slow)
    finally:
        LC.DEFAULT_TIMEOUT_S = real_ceiling
    check("a response past the ceiling sets over_ceiling",
          r2.metrics["over_ceiling"] == 1.0,
          f"latency={r2.metrics['latency_ms']:.0f}ms over={r2.metrics['over_ceiling']}")
    check("...and says so in the summary, naming the ceiling",
          "would have failed" in r2.summary, r2.summary)
    check("...while still being recorded as a successful measurement",
          r2.metrics["timed_out"] == 0.0 and r2.status == "pass")

    fast = _Ctx(delay_s=0)
    r2b = await canary.run(fast)
    check("a fast response does not set over_ceiling",
          r2b.metrics["over_ceiling"] == 0.0, str(r2b.metrics["over_ceiling"]))

    # 100ms is comfortably under a 20_000ms ceiling but over a bare `20`, so
    # this is the case that separates the correct comparison from a ms/s
    # unit mix-up. A near-instant response cannot: it is under both.
    modest = _Ctx(delay_s=0.1)
    r2c = await canary.run(modest)
    check("a 100ms response is not over a 20s ceiling (ms/s units)",
          r2c.metrics["over_ceiling"] == 0.0,
          f"latency={r2c.metrics['latency_ms']:.0f}ms ceiling={DEFAULT_TIMEOUT_S}s")

    # --- 4. slowness must never page ------------------------------------
    # The canary exists to observe slow responses; alarming on them would
    # recreate exactly the noise it was built to explain.
    check("a successful response is `pass` regardless of how slow",
          r.status == "pass" and r2.status == "pass",
          f"fast={r.status} slow-past-ceiling={r2.status}")
    check("the canary has no alarm-worthy verdict on a 200",
          r.status not in ("fail", "error"))

    # --- 5. genuine non-response IS an error, and is marked censored -----
    dead = _Ctx(exc=httpx.ReadTimeout(""))
    r3 = await canary.run(dead)
    check("no answer within the canary ceiling is an error", r3.status == "error", r3.status)
    check("...marked as a visibility gap, not a broken upstream",
          r3.payload.get("reason") == "upstream_api")
    check("...and recorded as a censored observation",
          r3.metrics.get("timed_out") == 1.0 and "latency_censored_ms" in r3.metrics,
          str(sorted(r3.metrics)))

    # --- 5b. the rate metric needs BOTH outcomes -------------------------
    # A `timed_out` series built only from failures averages to 1.0 and reads
    # as "everything times out". Measured 0.87 in prod on first deploy, purely
    # because most checks emitted the 1 and only two emitted the 0.
    from backend.checks.base import Check as _Check
    from backend.registry import CHECKS as _ALL
    opted = [c for c in _ALL.values() if getattr(c, "reports_timeout_rate", False)]
    check("only checks that emit the 0 case opt into the rate",
          {c.id for c in opted} == {"layer0.origin.alive", "layer0.origin.latency"},
          str(sorted(c.id for c in opted)))
    check("the base default is off, so a new check cannot opt in by accident",
          _Check.reports_timeout_rate is False)
    check("the canary emits the success side", r.metrics.get("timed_out") == 0.0)

    # --- 6. it must be cheap enough to leave running ---------------------
    check("cadence is low enough to be negligible load",
          canary.cadence_s >= 300, f"{canary.cadence_s}s")
    check("it cannot be demoted by the thing it measures",
          "layer0.origin.alive" not in canary.depends_on, str(canary.depends_on))

    print(f"\n{len(failures)} FAILED: {', '.join(failures)}" if failures
          else "\nall latency-canary assertions passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
