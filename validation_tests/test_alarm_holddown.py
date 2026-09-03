"""Alarm hold-down — a condition must persist before it is an alarm.

Run directly (no DB, no network):

    python validation_tests/test_alarm_holddown.py

Before this, one non-pass run opened an alarm and one pass closed it. Measured
over the 7 days to 2026-09-03: 2,308 alarms, 91.9% of which closed in under
five minutes. They flapped and self-resolved before anyone could look, and
each would have been an email the moment SMTP was configured. Replaying the
real check_runs through this rule yields 143 alarms instead of 2,308 — a 93.8%
reduction — while keeping every alarm that ran long enough to matter.

The two ways a hold-down goes wrong, and both are silent:

  - Gating the CLOSE as well as the open. Recovery must stay instant; an
    alarm that lingers after the problem is fixed teaches people to ignore
    the dashboard.
  - Failing closed on an infrastructure error. If the streak lookup throws
    and the code treats that as "not persisted yet", a real outage is
    swallowed by the very mechanism meant to reduce noise. It must fail OPEN.
"""
from __future__ import annotations
import asyncio
import os
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("SENTINEL_DB_URL", "postgresql://unused/unused")

import backend.checks as _all                                  # noqa: E402,F401
from backend.alarms.engine import AlarmEngine                  # noqa: E402
from backend.alarms.models import AlertsConfig, Route, load_config  # noqa: E402
from backend.checks.base import CheckResult, utcnow            # noqa: E402

failures: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if cond else 'FAIL'}  {label}" + (f"  — {detail}" if detail else ""))
    if not cond:
        failures.append(label)


class FakeStore:
    """Minimal store: records what the engine tried to do."""
    # `existing`, not `open_alarm`: an attribute of that name would shadow the
    # open_alarm() method below and the engine would call None.
    def __init__(self, streak_start=None, existing=None, raise_on_streak=False):
        self.streak_start = streak_start
        self.existing = existing
        self.raise_on_streak = raise_on_streak
        self.opened: list[dict] = []
        self.closed: list[int] = []

    async def find_open_alarm(self, check_id, target):
        return self.existing

    async def non_pass_streak_start(self, check_id, target):
        if self.raise_on_streak:
            raise RuntimeError("database unavailable")
        return self.streak_start

    async def latest_per_check_map(self):
        return {}

    async def open_alarm(self, **kw):
        self.opened.append(kw)
        return len(self.opened)          # the engine expects an id, not a row

    async def fetch_alarm(self, alarm_id):
        return {"id": alarm_id, **self.opened[alarm_id - 1]}

    async def close_alarm(self, alarm_id, when=None):
        self.closed.append(alarm_id)


def make_engine(store, cfg=None):
    eng = AlarmEngine.__new__(AlarmEngine)
    eng.store = store
    eng.cfg = cfg or load_config("alerts.yaml")
    from backend.alarms.router import Router
    eng.router = Router(eng.cfg)
    from backend.alarms import suppression as sup
    from backend.registry import CHECKS
    eng.depends_on_index = sup.build_depends_on_index(CHECKS.values(), include_alarm_only=True)
    eng._emit = lambda *a, **k: asyncio.sleep(0)
    return eng


def result(status="fail", stage="L1", check_id="layer1.product.comp_now", age_s=0):
    now = utcnow()
    return CheckResult(
        check_id=check_id, target="comp_now", stage=stage, status=status,
        started_at=now - timedelta(seconds=1), finished_at=now, summary="x",
    )


async def main() -> int:
    now = utcnow()

    # --- a brief blip must NOT open an alarm --------------------------------
    st = FakeStore(streak_start=now - timedelta(seconds=30))
    await make_engine(st).evaluate(result())
    check("a 30s blip does not open an alarm (hold-down 300s)", not st.opened,
          f"{len(st.opened)} opened")

    # --- a sustained condition MUST open one --------------------------------
    st = FakeStore(streak_start=now - timedelta(seconds=600))
    await make_engine(st).evaluate(result())
    check("a 10-minute condition does open an alarm", len(st.opened) == 1,
          f"{len(st.opened)} opened")

    # The boundary must not be a coin flip.
    st = FakeStore(streak_start=now - timedelta(seconds=299))
    await make_engine(st).evaluate(result())
    check("just under the hold-down stays closed", not st.opened)
    st = FakeStore(streak_start=now - timedelta(seconds=301))
    await make_engine(st).evaluate(result())
    check("just over the hold-down opens", len(st.opened) == 1)

    # --- per-route override -------------------------------------------------
    # L0 pages hard, so it uses a shorter hold-down than everything else.
    st = FakeStore(streak_start=now - timedelta(seconds=150))
    await make_engine(st).evaluate(result(stage="L0", check_id="layer0.origin.alive"))
    check("L0 opens at 150s (its own 120s hold-down)", len(st.opened) == 1,
          f"{len(st.opened)} opened")
    st = FakeStore(streak_start=now - timedelta(seconds=150))
    await make_engine(st).evaluate(result(stage="L1"))
    check("L1 does NOT open at 150s (global 300s applies)", not st.opened)

    # --- recovery must stay instant ----------------------------------------
    # Gating the close would leave alarms lingering after the fix, which
    # teaches people to distrust the dashboard.
    st = FakeStore(streak_start=None, existing={"id": 7, "check_id": "c", "target": "t"})
    await make_engine(st).evaluate(result(status="pass"))
    check("a pass closes an open alarm immediately, with no hold-down",
          st.closed == [7], str(st.closed))

    # --- an already-open alarm is untouched --------------------------------
    st = FakeStore(streak_start=now - timedelta(seconds=10),
                   existing={"id": 9, "check_id": "c", "target": "t"})
    await make_engine(st).evaluate(result())
    check("an already-open alarm is not re-opened", not st.opened)

    # --- infrastructure failure must fail OPEN ------------------------------
    st = FakeStore(raise_on_streak=True)
    await make_engine(st).evaluate(result())
    check("a failed streak lookup still opens the alarm (fails OPEN)",
          len(st.opened) == 1, f"{len(st.opened)} opened")

    # --- hold_down: 0 restores the old behaviour ---------------------------
    cfg = AlertsConfig(routes=[Route(match={}, policy="standard")], hold_down="0")
    st = FakeStore(streak_start=now - timedelta(seconds=1))
    await make_engine(st, cfg).evaluate(result())
    check("hold_down 0 opens on the first bad run (old behaviour)",
          len(st.opened) == 1)

    # --- no streak recorded at all -----------------------------------------
    # If the current result somehow is not yet visible in history, opening is
    # the safe direction: better a noisy alarm than a swallowed outage.
    st = FakeStore(streak_start=None)
    await make_engine(st).evaluate(result())
    check("no streak found opens rather than silently dropping", len(st.opened) == 1)

    print(f"\n{len(failures)} FAILED: {', '.join(failures)}" if failures
          else "\nall hold-down assertions passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
