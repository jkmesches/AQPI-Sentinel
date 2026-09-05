"""Inconclusive skips must not read as recovery.

Run directly (no DB, no network):

    python validation_tests/test_alarm_inconclusive_skip.py

The scheduler rewrites fail/error to ``skip`` when a dependency is unhealthy
(or our own DNS / uplink is down), so one upstream fault doesn't paint 37
downstream cells red. That rewrite is a decision NOT TO JUDGE. Everything
downstream of it used to read the resulting row as good news, which inverted
the whole point:

  fail  → alarm opens, notification fires
  skip  → alarm CLOSES  ("resolved!" — the radar never came back)
  fail  → a BRAND NEW alarm opens, escalation restarts at step 1

Measured on production in the 7 days to 2026-09-05: XEBY opened 18 alarms
while being continuously down, and 17 of 17 closes were caused by a demoted
skip — none by an actual pass. The CoSMoS products (water_depth, water_level,
max_water_depth, max_water_level) showed the identical pattern. Fleet-wide
there were 3,274 demoted skips in the window.

Two independent paths had to learn the difference, and missing either one
re-opens the hole:

  - engine.evaluate(), which closes alarms on a good run.
  - store.non_pass_streak_start(), which decides whether the hold-down has
    been satisfied. If a demoted skip breaks the streak, a permanently-down
    target re-qualifies for a fresh alarm a few minutes after every blip
    even when evaluate() correctly declines to close.

What must NOT change: an INTRINSIC skip (a check that ran and legitimately
had nothing to assess) still closes instantly, and so does a pass. Recovery
latency is the thing a hold-down must never buy.
"""
from __future__ import annotations
import asyncio
import os
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("SENTINEL_DB_URL", "postgresql://unused/unused")

import backend.checks as _all                                   # noqa: E402,F401
from backend.alarms import suppression as sup                   # noqa: E402
from backend.alarms.engine import AlarmEngine                   # noqa: E402
from backend.alarms.router import Router                        # noqa: E402
from backend.checks.base import CheckResult, utcnow             # noqa: E402

failures: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if cond else 'FAIL'}  {label}" + (f"  — {detail}" if detail else ""))
    if not cond:
        failures.append(label)


class FakeStore:
    def __init__(self, streak_start=None, existing=None):
        self.streak_start = streak_start
        self.existing = existing
        self.opened: list[dict] = []
        self.closed: list[int] = []

    async def find_open_alarm(self, check_id, target):
        return self.existing

    async def non_pass_streak_start(self, check_id, target):
        return self.streak_start

    async def latest_per_check_map(self):
        return {}

    async def open_alarm(self, **kw):
        self.opened.append(kw)
        return len(self.opened)

    async def fetch_alarm(self, alarm_id):
        return {"id": alarm_id, **self.opened[alarm_id - 1]}

    async def close_alarm(self, alarm_id, when=None):
        self.closed.append(alarm_id)


def make_engine(store, cfg=None):
    from backend.alarms.models import load_config
    from backend.registry import CHECKS
    eng = AlarmEngine.__new__(AlarmEngine)
    eng.store = store
    eng.cfg = cfg or load_config("alerts.yaml")
    eng.router = Router(eng.cfg)
    eng.depends_on_index = sup.build_depends_on_index(
        CHECKS.values(), include_alarm_only=True)
    eng._emit = lambda *a, **k: asyncio.sleep(0)
    return eng


def result(status="fail", payload=None, stage="L2",
           check_id="layer2.radar.XEBY", target="XEBY"):
    now = utcnow()
    return CheckResult(
        check_id=check_id, target=target, stage=stage, status=status,
        started_at=now - timedelta(seconds=1), finished_at=now,
        summary="x", payload=payload,
    )


OPEN_ALARM = {"id": 7, "check_id": "layer2.radar.XEBY", "target": "XEBY"}


async def main() -> int:
    now = utcnow()

    # --- the reported bug ---------------------------------------------------
    for reason in sorted(sup.INCONCLUSIVE_SKIP_REASONS):
        st = FakeStore(existing=dict(OPEN_ALARM))
        await make_engine(st).evaluate(
            result(status="skip", payload={"reason": reason,
                                           "original_status": "fail"}))
        check(f"demoted skip ({reason}) does NOT close the open alarm",
              st.closed == [], f"closed={st.closed}")

    # It must not open one either — it is not evidence of failure.
    st = FakeStore(streak_start=now - timedelta(hours=6))
    await make_engine(st).evaluate(
        result(status="skip", payload={"reason": "upstream_unhealthy"}))
    check("demoted skip does not open a new alarm either",
          not st.opened and not st.closed, f"opened={len(st.opened)}")

    # --- what must NOT regress: recovery stays instant ----------------------
    st = FakeStore(existing=dict(OPEN_ALARM))
    await make_engine(st).evaluate(result(status="pass"))
    check("a pass still closes immediately", st.closed == [7], str(st.closed))

    st = FakeStore(existing=dict(OPEN_ALARM))
    await make_engine(st).evaluate(result(status="skip", payload=None))
    check("an intrinsic skip (no payload) still closes", st.closed == [7],
          str(st.closed))

    st = FakeStore(existing=dict(OPEN_ALARM))
    await make_engine(st).evaluate(
        result(status="skip", payload={"reason": "no_differentiating_subchecks"}))
    check("an intrinsic skip with an unrelated reason still closes",
          st.closed == [7], str(st.closed))

    # A fail carrying the marker is still a fail — only skip is inconclusive.
    check("is_inconclusive requires status=skip",
          not sup.is_inconclusive("fail", {"reason": "upstream_unhealthy"}))
    check("is_inconclusive tolerates a missing payload",
          not sup.is_inconclusive("skip", None))

    # --- drift guard: the scheduler must keep emitting known reasons --------
    # If a reason string is renamed at the producer and not here, every one of
    # the assertions above still passes while production silently reverts to
    # closing alarms on demoted skips. Assert against the real code paths.
    from backend.scheduler import Scheduler
    sch = Scheduler.__new__(Scheduler)
    sch._latest_status = {"layer0.origin.alive": "fail"}
    sch._depends_on = {"layer2.radar.XEBY": ["layer0.origin.alive"]}

    class _Chk:
        id = "layer2.radar.XEBY"
        target = "XEBY"
    demoted = sch._maybe_demote_for_unhealthy_dep(_Chk(), result(status="fail"))
    check("scheduler's upstream demote is recognised as inconclusive",
          sup.is_inconclusive(demoted.status, demoted.payload),
          f"status={demoted.status} reason={(demoted.payload or {}).get('reason')!r}")

    class _Net:
        online = False
        def snapshot(self):
            return {}

    class _Ctx:
        network = _Net()
    sch.ctx = _Ctx()
    off = sch._maybe_downgrade_for_network(_Chk(), result(status="fail"))
    check("scheduler's offline demote is recognised as inconclusive",
          sup.is_inconclusive(off.status, off.payload),
          f"status={off.status} reason={(off.payload or {}).get('reason')!r}")

    dns = sch._maybe_downgrade_for_dns_summary(
        _Chk(), result(status="fail", payload={"x": 1}))
    # Only asserts the marker path when the summary actually trips the
    # detector; the point is the reason string, not the detector itself.
    if dns.status == "skip":
        check("scheduler's DNS demote is recognised as inconclusive",
              sup.is_inconclusive(dns.status, dns.payload),
              f"reason={(dns.payload or {}).get('reason')!r}")
    else:
        check("DNS demote reason string is registered",
              "local_dns_error" in sup.INCONCLUSIVE_SKIP_REASONS)

    print(f"\n{len(failures)} FAILED: {', '.join(failures)}" if failures
          else "\nall inconclusive-skip assertions passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
