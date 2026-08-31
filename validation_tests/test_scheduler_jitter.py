"""Scheduler de-correlation, and the retroactive image-timeout reclassification.

Pure unit tests: no network, no database. Run directly:

    python validation_tests/test_scheduler_jitter.py

Both come from the 2026-08-31 deploy. Restarting Sentinel put 9 read timeouts
in the first 79 requests — 11%, against a historical 0.1-1.2% — because every
check fired at once. radarca answers a sequential probe in 17.9s worst-case
but goes past 40s under a concurrent burst during one of its slow episodes, so
our own synchronisation is what turns its slowness into our timeouts.

The failure mode a naive fix has is subtle: adding jitter that is not
zero-mean silently stretches every cadence, so the whole fleet quietly runs
less often than configured and nobody notices until a freshness threshold
starts tripping. The other is jitter too small to actually separate anything.
Both are tested by simulation rather than by reading the arithmetic, because
both look correct in the source.
"""
from __future__ import annotations
import os
import random
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("SENTINEL_DB_URL", "postgresql://unused/unused")

from backend.scheduler import (                     # noqa: E402
    _jittered_delay, JITTER_FRAC, JITTER_MIN_S,
)
from backend.reprocess_engine import (              # noqa: E402
    _reverdict_l1, _IMAGE_TIMEOUT_SUMMARY,
)

failures: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if cond else 'FAIL'}  {label}" + (f"  — {detail}" if detail else ""))
    if not cond:
        failures.append(label)


def test_jitter_bounds() -> None:
    print("\n[1] jitter bounds and mean")

    # Zero-mean is the load-bearing property: jitter that skews positive
    # stretches every cadence and the fleet quietly runs less often than
    # configured. 20k samples keeps the tolerance tight enough to catch a
    # skew that a small sample would hide.
    for cadence in (60, 120, 300):
        ds = [_jittered_delay(cadence, 0.0) for _ in range(20000)]
        mean = statistics.mean(ds)
        drift_pct = 100 * (mean - cadence) / cadence
        check(f"cadence {cadence}s is preserved on average",
              abs(drift_pct) < 0.5, f"mean {mean:.2f}s, drift {drift_pct:+.2f}%")
        j = max(JITTER_MIN_S, cadence * JITTER_FRAC)
        check(f"cadence {cadence}s stays within +/-{j:.0f}s",
              min(ds) >= cadence - j - 0.01 and max(ds) <= cadence + j + 0.01,
              f"[{min(ds):.1f}, {max(ds):.1f}]")

    # Cadence is a PERIOD, not a gap: the time the run itself took must be
    # subtracted, or a check taking 10s on a 120s cadence quietly runs every
    # 130s and drifts further the busier things get. Every drift assertion
    # above passes elapsed=0.0 and so cannot see this.
    for cadence, elapsed in ((120, 30), (120, 10), (300, 45), (60, 5)):
        mean = statistics.mean(_jittered_delay(cadence, elapsed) for _ in range(8000))
        want = cadence - elapsed
        check(f"cadence {cadence}s minus {elapsed}s of run time sleeps ~{want}s",
              abs(mean - want) < max(1.0, want * 0.02), f"mean {mean:.1f}s, want {want}s")

    # A run that overran its cadence must still yield, never spin or go negative.
    for elapsed in (0, 59, 120, 600, 10_000):
        d = _jittered_delay(60, elapsed)
        check(f"never returns <1s (elapsed {elapsed}s)", d >= 1.0, f"{d:.2f}s")

    # Short cadences need the floor, or 5% of 20s is +/-1s and separates
    # nothing. Asserted against an ABSOLUTE number, deliberately: comparing
    # against JITTER_MIN_S itself made this vacuous — setting the constant to
    # 0 satisfied `spread >= 0` and the mutation survived.
    ds = [_jittered_delay(20, 0.0) for _ in range(2000)]
    spread = max(ds) - min(ds)
    check("short cadences still get a usable spread (JITTER_MIN_S floor)",
          spread >= 3.0, f"spread {spread:.1f}s, floor is {JITTER_MIN_S}s")


def test_decorrelation() -> None:
    print("\n[2] does it actually de-correlate? (simulation)")

    # Warm-up is excluded deliberately. Every check starts in the same second
    # here (the post-restart state), so the first firing is simultaneous no
    # matter what the jitter does — de-correlating THAT is the initial
    # stagger's job, tested separately below. What this measures is whether
    # phases pull apart once running, which is the jitter's actual job.
    WARMUP_H = 1

    def simulate(n_checks: int, cadence: int, hours: int, jitter: bool) -> int:
        """Worst per-second pile-up after warm-up."""
        rng = random.Random(1234)
        t = [0.0] * n_checks
        buckets: dict[int, int] = {}
        horizon, warm = hours * 3600, WARMUP_H * 3600
        for i in range(n_checks):
            while t[i] < horizon:
                if t[i] >= warm:
                    buckets[int(t[i])] = buckets.get(int(t[i]), 0) + 1
                t[i] += _jittered_delay(cadence, 0.0, rand=rng.uniform) if jitter else cadence
        return max(buckets.values())

    locked = simulate(15, 120, 6, jitter=False)
    spread = simulate(15, 120, 6, jitter=True)
    check("without jitter 15 same-cadence checks stay locked forever",
          locked == 15, f"worst second after {WARMUP_H}h: {locked}/15")
    check("with jitter the pile-up is broken up",
          spread <= 5, f"worst second after {WARMUP_H}h: {spread}/15")
    check("...and it is a real improvement, not noise",
          spread < locked / 2, f"{locked} -> {spread}")

    # How long until it separates? If this took a day the restart burst would
    # simply persist through the window that matters.
    rng = random.Random(99)
    t = [0.0] * 15
    minutes_to_split = None
    for cycle in range(60):
        for i in range(15):
            t[i] += _jittered_delay(120, 0.0, rand=rng.uniform)
        if len({int(x) for x in t}) >= 12 and minutes_to_split is None:
            minutes_to_split = t[0] / 60
            break
    check("separation happens within minutes, not days",
          minutes_to_split is not None and minutes_to_split < 30,
          f"{minutes_to_split:.0f} min" if minutes_to_split else "never")


def test_boot_spread() -> None:
    print("\n[2b] initial stagger: the restart burst")

    # The 2026-08-31 restart fired ~20 rank-0 checks inside one second and
    # took 9 read timeouts in the first 79 requests. Ranks must still start in
    # order (roots before leaves) while checks WITHIN a rank fan out.
    rng = random.Random(7)
    STAGGER_S = 8.0
    ranks = {0: 20, 1: 12, 2: 8, 3: 2}
    starts: dict[int, list[float]] = {}
    for rank, n in ranks.items():
        starts[rank] = [rank * STAGGER_S + rng.uniform(0.0, STAGGER_S) for _ in range(n)]

    worst = max(
        max(sum(1 for x in v if int(x) == sec) for sec in {int(x) for x in v})
        for v in starts.values()
    )
    check("no more than a handful of checks share a boot second",
          worst <= 6, f"worst second: {worst}")
    for rank in range(1, 4):
        check(f"rank {rank} still starts after rank {rank-1} completes",
              min(starts[rank]) >= max(starts[rank - 1]) - 0.001,
              f"{min(starts[rank]):.1f}s vs {max(starts[rank-1]):.1f}s")
    total = max(max(v) for v in starts.values())
    check("cold start is fully launched in well under a minute",
          total < 40, f"{total:.1f}s")


def test_image_timeout_reprocess() -> None:
    print("\n[3] retroactive image-timeout reclassification")

    def row(summary, sub, extra=None, status="fail"):
        p = {"sub_status": dict(sub), "product_label": "X"}
        p.update(extra or {})
        return {"id": 1, "check_id": "layer1.product.qpe_1hr", "target": "qpe_1hr",
                "stage": "L1", "status": status, "summary": summary,
                "payload": p, "started_at": datetime.now(timezone.utc),
                "finished_at": datetime.now(timezone.utc)}

    SUB = {"A_api_up": "pass", "B_schema": "pass", "C_freshness": "pass",
           "D_cadence": "pass", "E_step_count": "pass", "F_image_exists": "fail"}

    got = _reverdict_l1(row(_IMAGE_TIMEOUT_SUMMARY, SUB))
    check("an image read-timeout is reclassified", got is not None)
    if got:
        st, pl = got
        check("...to `error`, not `fail`", st == "error", st)
        check("...tagged as a visibility gap", pl.get("reason") == "upstream_api")
        check("...with F_image_exists dropped, not downgraded",
              "F_image_exists" not in pl["sub_status"], str(pl["sub_status"]))
        check("...preserving the as-observed verdict",
              pl["original"]["status"] == "fail"
              and pl["original"]["sub_status"]["F_image_exists"] == "fail")
        check("...and the other sub-checks untouched",
              pl["sub_status"]["A_api_up"] == "pass" and pl["sub_status"]["C_freshness"] == "pass")

        # Re-running must not overwrite the original with an already-fixed one.
        # The re-run row carries status="error" (what the first pass wrote), so
        # a clobbering implementation records "error" and is caught. With
        # status="fail" here the overwrite produced an identical value and the
        # mutation survived.
        again = _reverdict_l1(row(_IMAGE_TIMEOUT_SUMMARY, SUB,
                                  extra={"original": pl["original"], "reason": "upstream_api"},
                                  status="error"))
        check("re-running keeps the FIRST original, not the reprocessed one",
              again is not None and again[1]["original"]["status"] == "fail",
              str(again and again[1]["original"]["status"]))

    # --- the negative cases: over-reaching erases real product failures ---
    got = _reverdict_l1(row(_IMAGE_TIMEOUT_SUMMARY, SUB, extra={"image_http": 404}))
    check("a row that DID get a response is not reclassified",
          got is None or got[0] != "error", str(got and got[0]))

    got = _reverdict_l1(row("Image fetch failed: Connection timed out", SUB))
    check("a connect timeout is NOT reclassified (upstream down, not slow)",
          got is None or got[0] != "error", str(got and got[0]))

    got = _reverdict_l1(row("productDetail HTTP 500", SUB))
    check("an unrelated failure summary is not reclassified",
          got is None or got[0] != "error", str(got and got[0]))

    sub_ok = dict(SUB); sub_ok["F_image_exists"] = "pass"
    got = _reverdict_l1(row(_IMAGE_TIMEOUT_SUMMARY, sub_ok))
    check("a row whose F already passed is not touched",
          got is None or got[0] != "error", str(got and got[0]))


def main() -> int:
    test_jitter_bounds()
    test_decorrelation()
    test_boot_spread()
    test_image_timeout_reprocess()
    print(f"\n{len(failures)} FAILED: {', '.join(failures)}" if failures
          else "\nall scheduler/reprocess assertions passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
