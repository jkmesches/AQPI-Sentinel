"""Per-tier notification gate — quiet is not the same as blind.

Run directly (no DB, no network):

    python validation_tests/test_alarm_tiers.py

Sentinel watches every radar and product twice: the `L*` stages ask radarca
what it believes, the `LB*` stages read the published files off K2/Trinity. A
site can reasonably want only one of them audible — the backend checks are new
there and still being tuned, or radarca is known to be lying this week. The
control exists so nobody reaches for the alternative, which is deleting routes
out of alerts.yaml and losing the alarms along with the noise.

Four ways a gate like this goes wrong, and every one of them is silent:

  1. GATING EVALUATION INSTEAD OF DISPATCH. A tier that stops recording leaves
     a hole in its own history, and the hole is invisible a week later when
     someone asks how that radar did. The alarm must still open, still appear
     on the timeline, still count in the daily report.

  2. FREEZING SEVERITY ON THE WAY OUT. The silence check returns before
     severity is recomputed, which is already how acknowledging an alarm once
     pinned five of them at the severity they held ninety minutes in. A gate
     placed next to it would do the same thing to a whole tier, for days.

  3. REPLAYING THE BACKLOG ON RE-ENABLE. A deliberate quiet period that ends
     in a burst of every step that came due meanwhile is worse than no gate —
     it is the crying-wolf failure the hold-down was built to prevent.

  4. FAILING CLOSED. A missing settings row, a double-encoded blob or a
     database error must leave both tiers audible. A gate that silences
     alerts when it cannot read its own configuration is the one failure mode
     that cannot be noticed by the person it fails.
"""
from __future__ import annotations
import asyncio
import logging
import os
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("SENTINEL_DB_URL", "postgresql://unused/unused")

import backend.checks as _all                                   # noqa: E402,F401
from backend.alarms import tiers as tier_lib                    # noqa: E402
from backend.alarms.engine import AlarmEngine                   # noqa: E402
from backend.alarms.models import AlertsConfig                  # noqa: E402
from backend.alarms.router import Router                        # noqa: E402
from backend.checks.base import utcnow                          # noqa: E402
from backend.stages import (                                    # noqa: E402
    TIERS, _STAGE_LABELS, _STAGE_TIERS, stage_tier,
)

# The fail-open paths log an exception on purpose. Quietened so a passing
# run does not print a traceback that reads like a crash — the assertions
# below are what prove the fallback happened.
logging.getLogger("backend.alarms.tiers").setLevel(logging.CRITICAL)

failures: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if cond else 'FAIL'}  {label}" + (f"  — {detail}" if detail else ""))
    if not cond:
        failures.append(label)


CFG = AlertsConfig.model_validate({
    "routes": [{"match": {}, "policy": "standard", "repeat_interval": "30m"}],
    "receivers": [{"name": "console", "console": True}],
    "escalation_policies": [
        {"name": "standard", "steps": [{"delay": "0m", "receivers": ["console"]}]}],
})


class TierStore:
    """Records what the engine dispatched, recorded and promoted."""
    def __init__(self):
        self.dispatched: list = []
        self.written: list[dict] = []
        self.severities: list = []
        self.notif_counts = 0

    async def is_acked(self, alarm_id):
        return False

    async def list_active_silences(self, now):
        return []

    async def update_alarm_severity(self, alarm_id, severity):
        self.severities.append((alarm_id, severity))

    async def update_alarm_suppression(self, alarm_id, suppressed_by):
        pass

    async def notification_count_for_step(self, alarm_id, step_idx):
        return self.notif_counts

    async def last_notification_at(self, alarm_id, step_idx):
        return utcnow() - timedelta(hours=2)

    async def write_notification(self, **kw):
        self.written.append(kw)


def make_engine(store):
    eng = AlarmEngine.__new__(AlarmEngine)
    eng.store = store
    eng.cfg = CFG
    eng.router = Router(CFG)
    eng.depends_on_index = {}
    eng._emit = lambda *a, **k: asyncio.sleep(0)

    async def _dispatch(alarm, route, step, step_idx):
        store.dispatched.append((alarm["id"], step_idx))
    eng._dispatch = _dispatch
    return eng


def alarm_row(now, stage="LB2", check_id="layer2.backend.XEBY"):
    # Opened three hours ago, so the step at delay 0m is long overdue and a
    # non-dispatch below is the gate's doing and not the clock's.
    return {"id": 7, "check_id": check_id, "target": "XEBY",
            "stage": stage, "severity": "warn", "opened_at": now - timedelta(hours=3),
            "closed_at": None, "suppressed_by": None, "message": "no data",
            "payload": {"status_at_open": "fail"}}


class FakePool:
    """Answers one settings row, and can fail the way a database does."""
    def __init__(self, value, boom=False):
        self.value = value
        self.boom = boom

    async def fetchrow(self, sql, *args):
        if self.boom:
            raise RuntimeError("connection reset by peer")
        return None if self.value is _MISSING else {"value": self.value}


_MISSING = object()


async def main() -> None:
    now = utcnow()

    # --- the taxonomy ----------------------------------------------------
    print("every stage declares its tier")
    check("the tier map covers the stage list exactly",
          set(_STAGE_TIERS) == set(_STAGE_LABELS),
          f"only in one: {set(_STAGE_TIERS) ^ set(_STAGE_LABELS)}")
    check("every declared tier is a real tier",
          set(_STAGE_TIERS.values()) <= set(TIERS),
          str(sorted(set(_STAGE_TIERS.values()))))
    check("the LB stages are the backend tier",
          all(stage_tier(s) == "backend" for s in ("LB1", "LB2", "LB3")))
    check("the L stages are the frontend tier",
          all(stage_tier(s) == "frontend"
              for s in ("L0", "L1", "L2", "L3", "L4-T1T2")))
    # Not a prefix test. A stage absent from the map reads as frontend —
    # audible — even when it is spelled like a backend one, because a gate
    # that guesses from a string would silence a stage nobody had classified.
    # The parity assertion above is what stops a stage being absent at all.
    check("a stage missing from the map is audible, however it is spelled",
          stage_tier("LBX-new") == "frontend" and stage_tier("K2") == "frontend",
          f'LBX-new -> {stage_tier("LBX-new")}')

    # --- resolution, which must fail open --------------------------------
    print("\nthe gate fails open on anything it cannot read")
    check("no config at all", tier_lib.may_notify(None, "LB2"))
    check("a blob that is not a dict", tier_lib.may_notify("backend=off", "LB2"))
    check("a tier the blob does not mention",
          tier_lib.may_notify({"frontend": False}, "LB2"))
    check("a non-boolean value", tier_lib.may_notify({"backend": "no"}, "LB2"))
    check("only an explicit false silences",
          tier_lib.may_notify({"backend": False}, "LB2") is False)
    check("and it silences only its own tier",
          tier_lib.may_notify({"backend": False}, "L2") is True)

    print("\nand on anything it cannot load")
    check("a missing settings row leaves both tiers audible",
          await tier_lib.load(FakePool(_MISSING)) == {"frontend": True,
                                                      "backend": True})
    check("a database error leaves both tiers audible",
          await tier_lib.load(FakePool(None, boom=True)) == {"frontend": True,
                                                             "backend": True})
    check("a double-encoded row is unwrapped, not discarded",
          await tier_lib.load(FakePool('{"backend": false}'))
          == {"frontend": True, "backend": False})
    check("an unparseable string leaves both tiers audible",
          await tier_lib.load(FakePool("{not json")) == {"frontend": True,
                                                         "backend": True})
    check("a stored blob is read",
          await tier_lib.load(FakePool({"backend": False}))
          == {"frontend": True, "backend": False})
    check("an unknown key in the blob is ignored",
          await tier_lib.load(FakePool({"sideways": False}))
          == {"frontend": True, "backend": True})

    # --- the engine ------------------------------------------------------
    print("\ndispatch, with both tiers audible")
    st = TierStore()
    await make_engine(st)._process(alarm_row(now), now, [], {}, None)
    check("the overdue step is dispatched", st.dispatched == [(7, 0)],
          str(st.dispatched))
    check("nothing is recorded as withheld", not st.written)

    st = TierStore()
    await make_engine(st)._process(alarm_row(now), now, [], {},
                                   {"frontend": True, "backend": True})
    check("an explicitly-enabled tier dispatches too", st.dispatched == [(7, 0)],
          str(st.dispatched))

    print("\ndispatch, with the backend tier disabled")
    st = TierStore()
    await make_engine(st)._process(alarm_row(now), now, [], {},
                                   {"frontend": True, "backend": False})
    check("the backend alarm is NOT dispatched", st.dispatched == [],
          str(st.dispatched))
    check("it is recorded where the message would have gone",
          len(st.written) == 1, str(len(st.written)))
    w = st.written[0] if st.written else {}
    check("recorded against the right alarm and step",
          w.get("alarm_id") == 7 and w.get("step_idx") == 0, str(w))
    check("with a status that says what happened, not 'failed'",
          w.get("status") == "tier-disabled", str(w.get("status")))
    check("and names the tier that was off",
          "backend" in (w.get("error") or ""), str(w.get("error")))
    check("severity is still recomputed — the record stays truthful",
          st.severities and st.severities[0][0] == 7, str(st.severities))

    print("\nthe gate is per tier, not global")
    st = TierStore()
    await make_engine(st)._process(
        alarm_row(now, stage="L2", check_id="layer2.radar.XEBY"), now, [], {},
        {"frontend": True, "backend": False})
    check("a frontend alarm still pages while the backend tier is off",
          st.dispatched == [(7, 0)], str(st.dispatched))
    check("and is not recorded as withheld", not st.written)

    st = TierStore()
    await make_engine(st)._process(
        alarm_row(now, stage="L2", check_id="layer2.radar.XEBY"), now, [], {},
        {"frontend": False, "backend": True})
    check("the other direction works as well", st.dispatched == [],
          str(st.dispatched))

    print("\nre-enabling does not replay the backlog")
    # The withheld row consumed this step's bookkeeping, which is what a
    # second pass sees. With a repeat_interval configured the step becomes due
    # again on its own schedule — so the tier comes back audible from the next
    # interval, not with everything it missed.
    st = TierStore()
    st.notif_counts = 1
    await make_engine(st)._process(alarm_row(now), now, [], {},
                                   {"frontend": True, "backend": False})
    check("a step already recorded as withheld is withheld again, not sent",
          st.dispatched == [] and len(st.written) == 1,
          f"dispatched={st.dispatched} written={len(st.written)}")
    st = TierStore()
    st.notif_counts = 1
    await make_engine(st)._process(alarm_row(now), now, [], {},
                                   {"frontend": True, "backend": True})
    check("once re-enabled the same step pages on the repeat interval",
          st.dispatched == [(7, 0)], str(st.dispatched))


if __name__ == "__main__":
    asyncio.run(main())
    print()
    if failures:
        print(f"{len(failures)} FAILED:")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    print("all tier-gate assertions passed")
