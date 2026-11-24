"""Alarms on the NEXRAD radars are recorded and never notify.

Run directly (no DB, no network):

    python validation_tests/test_alarm_record_only.py

Joseph, 2026-11-23: users must not receive warnings about the NEXRAD radars.
The LB3 composite checks watch KBBX, KDAX and KMUX, and KBBX had two alarms
open that afternoon. They must still open, escalate and show everywhere an
alarm shows, and must never send.

Ways this goes wrong, each silent:

  - Gating evaluation instead of sending, which loses the record.
  - Gating the engine's step loop and missing web push, which fires from a
    listener on alarm_open and never passes through that loop.
  - A NEXRAD radar added to the map or the composite table and not to
    config.NEXRAD_RADARS, whose alarms would then notify again.
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import asyncpg

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "validation_tests"))
os.environ.setdefault("SENTINEL_DB_URL", "postgresql://unused/unused")

# After the path setup above, which these need.
from test_alarm_tiers import TierStore, alarm_row, make_engine  # noqa: E402

from backend.alarms import record_only as rol  # noqa: E402
from backend.alarms.engine import AlarmEngine  # noqa: E402
from backend.alarms.models import (  # noqa: E402
    AlertsConfig, EscalationPolicy, EscalationStep, Receiver, Route)
from backend.alarms.record_only import STATUS, record_only  # noqa: E402
from backend.alarms.web_push import make_listener  # noqa: E402
from backend.api.routes import admin as admin_routes  # noqa: E402
from backend.api.routes.radars import RADAR_META  # noqa: E402
from backend.checks.base import utcnow  # noqa: E402
from backend.config import (  # noqa: E402
    COMPOSITE_EXPECTED_RADARS, NEXRAD_RADARS, NEXRAD_SITES)

failures: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    """Print one assertion and record a failure."""
    print(f"  {'ok  ' if cond else 'FAIL'}  {label}" + (f"  — {detail}" if detail else ""))
    if not cond:
        failures.append(label)


def nexrad_row(target: str = "KBBX") -> dict[str, object]:
    """An overdue LB3 alarm on one radar."""
    row = alarm_row(utcnow(), stage="LB3", check_id=f"layer3.input.{target}")
    return {**row, "target": target}


async def test_engine() -> None:
    """The step loop records a NEXRAD alarm's steps and sends none."""
    print("the engine's step loop:")
    now = utcnow()
    st = TierStore()
    await make_engine(st)._process(nexrad_row(), now, [], {}, None)
    check("a NEXRAD alarm sends nothing", st.dispatched == [], str(st.dispatched))
    check("...and records each step it kept back", len(st.written) == 1
          and st.written[0].get("status") == STATUS, str(st.written))
    check("...against its alarm and step",
          bool(st.written) and st.written[0].get("alarm_id") == 7
          and st.written[0].get("step_idx") == 0)
    check("its severity is still recomputed", bool(st.severities), str(st.severities))
    st = TierStore()
    await make_engine(st)._process(nexrad_row("XEBY"), now, [], {}, None)
    check("an X-band alarm on the same stage still sends", st.dispatched == [(7, 0)],
          str(st.dispatched))
    st = TierStore()
    await make_engine(st)._process(nexrad_row(), now, [], {},
                                   {"frontend": True, "backend": False})
    check("with its tier off as well it is recorded once, not twice",
          st.dispatched == [] and len(st.written) == 1, str(st.written))


def test_rule_and_tables() -> None:
    """The predicate, and that every NEXRAD radar anywhere is in the set."""
    print("\nthe rule and the tables it must cover:")
    for alarm, want in (({"target": "KDAX"}, True), ({"target": "XEBY"}, False),
                        ({"target": None}, False), ({}, False),
                        ({"target": "kdax"}, False)):
        check(f"{alarm} is record-only: {want}", record_only(alarm) is want)
    # An alarm_open payload is the alarm row, which carries the target.
    check("an alarm_open payload is read the same way",
          record_only({"id": 1, "check_id": "layer3.input.KMUX", "target": "KMUX",
                       "severity": "warn"}))
    # RADAR_META's `kind` is the one place a radar is called NEXRAD, so the
    # set is held to it both ways rather than to a guess from the name.
    map_nexrad = {rid for rid, m in RADAR_META.items() if m.get("kind") == "nexrad"}
    check("every NEXRAD radar on the map is in NEXRAD_RADARS",
          map_nexrad <= NEXRAD_RADARS, str(sorted(map_nexrad - NEXRAD_RADARS)))
    check("nothing but a NEXRAD radar is in it, so no network radar goes quiet",
          NEXRAD_RADARS <= map_nexrad, str(sorted(NEXRAD_RADARS - map_nexrad)))
    check("every NEXRAD site with frames is in it", set(NEXRAD_SITES) <= NEXRAD_RADARS)
    unknown = set(COMPOSITE_EXPECTED_RADARS) - set(RADAR_META)
    check("every radar the composite checks watch has a kind on the map", not unknown,
          str(sorted(unknown)))


class PushStore:
    """The two things the push listener reads from the store."""

    def __init__(self) -> None:
        self.pool = object()
        self.silences: list[dict[str, object]] = []

    async def list_active_silences(self, now: datetime) -> list[dict[str, object]]:
        """Every silence, whatever `now` is; the matcher checks the window."""
        _ = now
        return self.silences


async def test_web_push() -> None:
    """The real listener, over a fake store and a sender that records."""
    print("\nthe web push listener:")
    sent: list[dict[str, object]] = []

    async def send(pool: object, msg: dict[str, object]) -> dict[str, int]:
        _ = pool
        sent.append(msg)
        return {"sent": 1, "failed": 0, "expired": 0, "filtered": 0, "deferred": 0}

    store = PushStore()
    listener = make_listener(store, send)
    for radar in sorted(NEXRAD_RADARS):
        await listener("alarm_open", nexrad_row(radar))
    check("an alarm opening on any NEXRAD radar pushes nothing", sent == [], str(sent))
    await listener("alarm_open", nexrad_row("XEBY"))
    check("one opening on an X-band radar pushes once",
          [m.get("check_id") for m in sent] == ["layer3.input.XEBY"], str(sent))
    now = utcnow()
    store.silences = [{"starts": now - timedelta(hours=1), "ends": now + timedelta(hours=1),
                       "matchers": {"target": "XEBY"}}]
    await listener("alarm_open", nexrad_row("XEBY"))
    check("...and nothing while a silence covers it", len(sent) == 1, str(len(sent)))
    store.silences = []
    await listener("alarm_resolve", nexrad_row("XEBY"))
    check("...and nothing on an event other than alarm_open", len(sent) == 1)
    src = (ROOT / "backend" / "api" / "app.py").read_text()
    check("the app registers this listener with the real sender",
          "add_listener(_make_push_listener(store, _push.dispatch_push))" in src)


class RecordingSink:
    """A console sink that records what it was asked to send."""

    def __init__(self) -> None:
        self.alarms: list[dict[str, object]] = []

    async def send(self, alarm: dict[str, object], *_: object) -> SimpleNamespace:
        """Record the alarm and report it delivered."""
        self.alarms.append(alarm)
        return SimpleNamespace(delivered=True, error=None)


async def test_admin_test_alert() -> None:
    """The admin route's "send a test" for a route matching one radar."""
    print("\nthe admin test alert:")
    cfg = AlertsConfig(
        receivers=[Receiver(name="ops", console=True)],
        escalation_policies=[EscalationPolicy(name="page",
                                              steps=[EscalationStep(receivers=["ops"])])],
        routes=[Route(match={"target": "KBBX"}, policy="page"),
                Route(match={"target": "XEBY"}, policy="page")])
    sink = RecordingSink()

    async def execute(*_: object) -> None:
        return None

    state = SimpleNamespace(engine=SimpleNamespace(cfg=cfg, sinks={"console": sink}),
                            store=SimpleNamespace(pool=SimpleNamespace(execute=execute)))
    request = SimpleNamespace(app=SimpleNamespace(state=state))
    admin = {"email": "admin@example.invalid"}
    got = await admin_routes.test_alert(request, admin, {"route_index": 0})
    check("a test of a NEXRAD route sends nothing", sink.alarms == [], str(sink.alarms))
    check("...and says it is record-only", not got["ok"]
          and "record-only" in str(got["results"][0].get("error")), str(got))
    got = await admin_routes.test_alert(request, admin, {"route_index": 1})
    check("a test of an X-band route still sends", got["ok"] and len(sink.alarms) == 1,
          str(got))


TRIAL_ID = "layer4.mosaic.water_level"


def trial_alarm(opened_at: datetime) -> dict[str, object]:
    """An alarm on a trial check, opened at a given time.

    Args:
        opened_at: When it opened.

    Returns:
        The alarm row.
    """
    return {**alarm_row(utcnow(), stage="LB2", check_id=TRIAL_ID),
            "target": "water_level", "opened_at": opened_at}


def test_trial_rule() -> None:
    """The record-only trial, counted from the check's first run."""
    print("\nthe record-only trial:")
    rol._FIRST_RUN.clear()
    t0 = datetime(2026, 11, 24, 3, tzinfo=timezone.utc)
    check("a trial check that has not run is record-only",
          record_only(trial_alarm(t0)))
    _ = rol.note_run(TRIAL_ID, t0)
    check("an alarm opened a day in is record-only",
          record_only(trial_alarm(t0 + timedelta(days=1))))
    check("...and the reason names the trial's end",
          "2026-12-08 03:00Z" in str(rol.reason(trial_alarm(t0 + timedelta(days=1)))),
          str(rol.reason(trial_alarm(t0 + timedelta(days=1)))))
    check("one opened a minute before the end is record-only",
          record_only(trial_alarm(t0 + rol.TRIAL - timedelta(minutes=1))))
    check("one opened at the end notifies",
          not record_only(trial_alarm(t0 + rol.TRIAL)))
    check("one opened fifteen days in notifies",
          not record_only(trial_alarm(t0 + timedelta(days=15))))
    _ = rol.note_run(TRIAL_ID, t0 + timedelta(days=30))
    check("a later run does not restart the trial",
          rol.trial_ends(TRIAL_ID) == t0 + rol.TRIAL, str(rol.trial_ends(TRIAL_ID)))
    rol.seed({TRIAL_ID: t0 - timedelta(days=2)})
    check("an earlier stored run moves the start back",
          rol.trial_ends(TRIAL_ID) == t0 - timedelta(days=2) + rol.TRIAL)
    rol._FIRST_RUN.clear()
    _ = rol.note_run(TRIAL_ID, datetime(2026, 11, 24, 3))  # noqa: DTZ001  the naive case under test
    check("a run time without a timezone is taken as UTC",
          rol.trial_ends(TRIAL_ID) == t0 + rol.TRIAL, str(rol.trial_ends(TRIAL_ID)))
    _ = rol.note_run("layer4.mosaic.water_depth", t0)
    check("a check not on the trial gets no clock",
          rol.trial_ends("layer4.mosaic.water_depth") is None)
    check("...and its alarms notify", not record_only(
        {**trial_alarm(t0), "check_id": "layer4.mosaic.water_depth"}))
    undated = {"check_id": TRIAL_ID, "target": "water_level"}
    rol._FIRST_RUN[TRIAL_ID] = utcnow() - timedelta(days=1)
    check("an alarm without opened_at, inside the trial, is record-only",
          record_only(undated))
    rol._FIRST_RUN[TRIAL_ID] = utcnow() - timedelta(days=30)
    check("...and after it, notifies", not record_only(undated))
    check("the NEXRAD rule is unchanged", record_only({"target": "KBBX"})
          and rol.reason({"target": "KBBX"}) is not None)


async def test_trial_engine() -> None:
    """The step loop, evaluate() and the start-up seed, for a trial check."""
    print("\nthe trial in the engine:")
    rol._FIRST_RUN.clear()
    now = utcnow()
    _ = rol.note_run(TRIAL_ID, now - timedelta(days=1))
    st = TierStore()
    await make_engine(st)._process(trial_alarm(now - timedelta(hours=3)), now, [], {}, None)
    check("a trial alarm sends nothing", st.dispatched == [], str(st.dispatched))
    check("...and records why", len(st.written) == 1 and st.written[0].get("status") == STATUS
          and "trial" in str(st.written[0].get("error")), str(st.written))
    rol._FIRST_RUN.clear()
    _ = rol.note_run(TRIAL_ID, now - timedelta(days=20))
    st = TierStore()
    await make_engine(st)._process(trial_alarm(now - timedelta(hours=3)), now, [], {}, None)
    check("after the trial the same alarm sends", st.dispatched == [(7, 0)], str(st.dispatched))

    class ProbeError(Exception):
        """Raised by the store's first call, to see what ran before it."""

    class FirstCallStore:
        def __init__(self) -> None:
            self.saved: list[tuple[str, datetime]] = []

        async def save_first_run(self, check_id: str, first_run: datetime) -> None:
            self.saved.append((check_id, first_run))

        async def find_open_alarm(self, check_id: str, target: str) -> None:
            _ = (check_id, target)
            raise ProbeError

    rol._FIRST_RUN.clear()
    eng = AlarmEngine.__new__(AlarmEngine)
    first_store = FirstCallStore()
    eng.store = first_store
    started = now - timedelta(minutes=5)
    result = SimpleNamespace(check_id=TRIAL_ID, target="water_level", started_at=started,
                             status="warn", payload={})
    try:
        await eng.evaluate(result)  # type: ignore[arg-type]
    except ProbeError:
        pass
    check("evaluate() starts the clock before it touches the store",
          rol.trial_ends(TRIAL_ID) == started + rol.TRIAL, str(rol.trial_ends(TRIAL_ID)))
    check("...and stores the start, so a restart finds it",
          first_store.saved == [(TRIAL_ID, started)], str(first_store.saved))
    first_store.saved.clear()
    try:
        await eng.evaluate(result)  # type: ignore[arg-type]
    except ProbeError:
        pass
    check("...once, not on every run", first_store.saved == [], str(first_store.saved))


async def test_trial_seed() -> None:
    """The start-up seed, and a store that fails, for a trial check."""
    print("\nthe trial's start at start-up:")
    now = utcnow()
    started = now - timedelta(minutes=5)
    eng = AlarmEngine.__new__(AlarmEngine)

    class SeedStore:
        def __init__(self, fail: bool) -> None:
            self.fail = fail
            self.saved: list[tuple[str, datetime]] = []

        async def first_runs(self, ids: object) -> dict[str, datetime]:
            _ = ids
            if self.fail:
                raise asyncpg.PostgresConnectionError("down")
            return {TRIAL_ID: now - timedelta(days=3)}

        async def save_first_run(self, check_id: str, first_run: datetime) -> None:
            if self.fail:
                raise asyncpg.PostgresConnectionError("down")
            self.saved.append((check_id, first_run))

    rol._FIRST_RUN.clear()
    seed_store = SeedStore(fail=False)
    eng.store = seed_store
    await eng._seed_trials()
    check("start-up loads the first stored run",
          rol.trial_ends(TRIAL_ID) == now - timedelta(days=3) + rol.TRIAL)
    check("...and stores it, before retention can trim the run it came from",
          seed_store.saved == [(TRIAL_ID, now - timedelta(days=3))], str(seed_store.saved))
    rol._FIRST_RUN.clear()
    eng.store = SeedStore(fail=True)
    await eng._seed_trials()
    check("a database that fails at start-up leaves the clock unset, not raised",
          rol.trial_ends(TRIAL_ID) is None)
    _ = rol.note_run(TRIAL_ID, started)
    await eng._save_first_run(TRIAL_ID, started)
    check("a store that fails to save is logged, not raised", True)
    rol._FIRST_RUN.clear()


def main() -> int:
    """Run every section.

    Returns:
        The process exit status, 1 when any assertion failed.
    """
    asyncio.run(test_engine())
    test_rule_and_tables()
    asyncio.run(test_web_push())
    asyncio.run(test_admin_test_alert())
    test_trial_rule()
    asyncio.run(test_trial_engine())
    asyncio.run(test_trial_seed())
    print(f"\n{len(failures)} FAILED: {', '.join(failures)}" if failures
          else "\nall record-only assertions passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
