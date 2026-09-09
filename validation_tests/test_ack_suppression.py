"""An acknowledged alarm must not page anyone, including on repeats.

Run directly (no DB, no network):

    python validation_tests/test_ack_suppression.py

Ack is the operator saying "seen it, stop telling me". It is stored per
ALARM ROW in alarm_acks, keyed on alarm_id with no user scoping — so an ack
by anyone silences that alarm for everyone, which is the intended shared-
ownership behavior for a team on one rotation.

Three separate paths can notify, and ack has to hold on all of them:

  engine._process   → email / console / webhook, plus every repeat_interval
                      re-send. Gated by is_acked() before route matching, so
                      an acked alarm never even resolves a policy.
  push (immediate)  → fires from the alarm_open listener. An ack cannot
                      exist before the alarm does, so this one always goes
                      out; that is the notification the ack responds to.
  push (deferred)   → the smart-delay re-checks liveness after delay_s and
                      must honour an ack placed during the window.

The subtle one is REVOCATION. is_acked() filters `revoked_at IS NULL`; the
deferred-push re-check originally did a bare EXISTS on alarm_acks, so an
un-ack inside the delay window left the device silent — the operator says
"page me again" and nothing comes. The two paths must agree.

A second, larger trap is not visible from this file: an ack is bound to the
alarm row, not to the check/target. Anything that closes and re-opens an
alarm therefore discards it. Production acked layer2.radar.XEBY twice
(alarms 27484 and 27386); both were closed by a demoted skip and every
re-open arrived unacked and paged again. See test_alarm_inconclusive_skip.
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
from backend.alarms.engine import AlarmEngine                   # noqa: E402
from backend.alarms.models import AlertsConfig                  # noqa: E402
from backend.alarms.router import Router                        # noqa: E402
from backend.checks.base import utcnow                          # noqa: E402

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


class AckStore:
    """Store stub that reports an ack and records dispatch attempts."""
    def __init__(self, acked: bool):
        self.acked = acked
        self.dispatched: list = []
        self.notif_counts = 0

    async def is_acked(self, alarm_id):
        return self.acked

    async def list_active_silences(self, now):
        return []

    async def update_alarm_severity(self, *a, **k):
        pass

    async def notification_count_for_step(self, alarm_id, step_idx):
        # >0 exercises the repeat_interval branch rather than a first send.
        return self.notif_counts

    async def last_notification_at(self, alarm_id, step_idx):
        return utcnow() - timedelta(hours=2)   # repeat is well overdue


def make_engine(store):
    eng = AlarmEngine.__new__(AlarmEngine)
    eng.store = store
    eng.cfg = CFG
    eng.router = Router(CFG)
    eng._emit = lambda *a, **k: asyncio.sleep(0)

    async def _dispatch(alarm, route, step, step_idx):
        store.dispatched.append((alarm["id"], step_idx))
    eng._dispatch = _dispatch
    return eng


def alarm_row(now):
    return {"id": 1, "check_id": "layer2.radar.XEBY", "target": "XEBY",
            "stage": "L2", "severity": "warn", "opened_at": now - timedelta(hours=3),
            "closed_at": None, "suppressed_by": None, "message": "down",
            "payload": {"status_at_open": "fail"}}


class FakePool:
    """Records the SQL it is asked to run and answers from a fixed row."""
    def __init__(self, row):
        self.row = row
        self.queries: list[str] = []

    async def fetchrow(self, sql, *args):
        self.queries.append(sql)
        if "push_subscriptions" in sql:
            return {"id": 1}
        return self.row

    async def execute(self, *a, **k):
        pass


async def main() -> int:
    now = utcnow()

    # --- engine path: first send ------------------------------------------
    st = AckStore(acked=False)
    await make_engine(st)._process(alarm_row(now), now, [])
    check("an UNacked alarm dispatches", st.dispatched == [(1, 0)], str(st.dispatched))

    st = AckStore(acked=True)
    await make_engine(st)._process(alarm_row(now), now, [])
    check("an ACKed alarm does not dispatch", st.dispatched == [], str(st.dispatched))

    # --- engine path: the repeat ------------------------------------------
    # The repeat branch is what the question is really about: a step already
    # fired once, repeat_interval has long since elapsed, and the ticker runs
    # every 15s forever. Ack must stop it.
    st = AckStore(acked=False); st.notif_counts = 1
    await make_engine(st)._process(alarm_row(now), now, [])
    check("an UNacked alarm re-sends once repeat_interval elapses",
          st.dispatched == [(1, 0)], str(st.dispatched))

    st = AckStore(acked=True); st.notif_counts = 1
    await make_engine(st)._process(alarm_row(now), now, [])
    check("an ACKed alarm does NOT re-send on repeat_interval",
          st.dispatched == [], str(st.dispatched))

    # Ack is checked before routing, so it holds regardless of route/policy.
    st = AckStore(acked=True); st.notif_counts = 5
    row = {**alarm_row(now), "severity": "critical"}
    await make_engine(st)._process(row, now, [])
    check("ack outranks severity and repeat count", st.dispatched == [])

    # --- deferred push path ------------------------------------------------
    import backend.push as push

    async def run_delayed(closed_at, acked_row):
        pool = FakePool({"closed_at": closed_at, "acked": acked_row})
        sent = []
        orig_sleep, orig_send = asyncio.sleep, push._send_one
        async def no_sleep(_s): return None
        def fake_send(sub, payload, vapid):
            sent.append(payload); return True, None, 201
        asyncio.sleep = no_sleep          # type: ignore[assignment]
        push._send_one = fake_send        # type: ignore[assignment]
        try:
            await push._send_delayed(pool, {"id": 1, "endpoint": "e"},
                                     {"alarm_id": 1}, {}, 600.0)
        finally:
            asyncio.sleep = orig_sleep    # type: ignore[assignment]
            push._send_one = orig_send    # type: ignore[assignment]
        return sent, pool

    sent, _ = await run_delayed(None, False)
    check("deferred push sends when still open and unacked", len(sent) == 1)

    sent, _ = await run_delayed(None, True)
    check("deferred push is suppressed by an ack placed during the delay",
          sent == [])

    sent, _ = await run_delayed(now, False)
    check("deferred push is suppressed when the alarm self-resolved", sent == [])

    # The revocation bug: a bare EXISTS on alarm_acks treats an un-acked
    # alarm as acked, so "page me again" silently does nothing.
    _, pool = await run_delayed(None, False)
    ack_sql = [q for q in pool.queries if "alarm_acks" in q]
    check("the deferred-push ack check filters revoked acks",
          bool(ack_sql) and "revoked_at IS NULL" in ack_sql[0],
          "bare EXISTS would treat a revoked ack as still acked")

    print(f"\n{len(failures)} FAILED: {', '.join(failures)}" if failures
          else "\nall ack-suppression assertions passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
