"""An alarm that opened on a warn reopens when its check goes to fail.

Run directly (no DB, no network):

    python validation_tests/test_alarm_warn_reopen.py

An alarm takes its route from the status it opened on and keeps it. AQPI
routes warn-opened alarms to a silent policy, so before this an alarm that
opened on a warn and then went to fail stayed silent for the rest of its life.
The CoSMoS stand-down made that the nightly case. LB1 warns on an explained
gap, and a failed publish after it would never page (Joseph, 2026-11-23).

The ways this goes wrong, each silent:

  - Reopening on the first fail run. The warn period counts as non-pass, so
    the ordinary hold-down would already be satisfied and one transient fail
    would page. The reopen must measure the fail run alone.
  - Missing the warn because the payload is still a JSON string, which asyncpg
    hands back for a double-encoded jsonb column.
  - Failing closed when the streak lookup throws.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("SENTINEL_DB_URL", "postgresql://unused/unused")

import backend.checks as _all  # noqa: F401  registers the checks the engine indexes
from backend.alarms.engine import AlarmEngine
from backend.alarms.models import AlertsConfig, Route
from backend.alarms.router import Router
from backend.checks.base import CheckResult, Status, utcnow

HOLD_S = 300
failures: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    """Print one assertion and record a failure."""
    print(f"  {'ok  ' if cond else 'FAIL'}  {label}" + (f"  — {detail}" if detail else ""))
    if not cond:
        failures.append(label)


class FakeStore:
    """Records what the engine tried to do, with a separate broken-run start."""

    def __init__(self, existing: dict[str, Any] | None, *,
                 non_pass_start: datetime | None = None,
                 broken_start: datetime | None = None,
                 raise_on_broken: bool = False) -> None:
        self.existing = existing
        self.non_pass_start = non_pass_start
        self.broken_start = broken_start
        self.raise_on_broken = raise_on_broken
        self.opened: list[dict[str, Any]] = []
        self.closed: list[int] = []

    async def find_open_alarm(self, check_id: str, target: str) -> dict[str, Any] | None:
        """The open alarm this store was built with."""
        return self.existing

    async def non_pass_streak_start(self, check_id: str, target: str) -> datetime | None:
        """Start of the non-pass run."""
        return self.non_pass_start

    async def broken_streak_start(self, check_id: str, target: str) -> datetime | None:
        """Start of the fail-and-error run."""
        if self.raise_on_broken:
            raise RuntimeError("database unavailable")
        return self.broken_start

    async def latest_per_check_map(self) -> dict[str, Any]:
        """No other check's status."""
        return {}

    async def open_alarm(self, **kw: Any) -> int:
        """Record an open and return its id."""
        self.opened.append(kw)
        return 100 + len(self.opened)

    async def fetch_alarm(self, alarm_id: int) -> dict[str, Any]:
        """The row just opened."""
        return {"id": alarm_id, **self.opened[alarm_id - 101]}

    async def close_alarm(self, alarm_id: int, when: datetime | None = None) -> None:
        """Record a close."""
        self.closed.append(alarm_id)


def make_engine(store: FakeStore) -> AlarmEngine:
    """An engine over the fake store, with one route and a 5 min hold-down.

    Args:
        store: The fake store.

    Returns:
        The engine, built without its constructor's database setup.
    """
    eng = AlarmEngine.__new__(AlarmEngine)
    eng.store = store
    eng.cfg = AlertsConfig(routes=[Route(match={}, policy="standard")], hold_down="5m")
    eng.router = Router(eng.cfg)
    eng.depends_on_index = {}
    setattr(eng, "_emit", lambda *a, **k: asyncio.sleep(0))  # noqa: B010  replaces a method
    return eng


def result(status: Status) -> CheckResult:
    """One LB1 water_level result finishing now."""
    now = utcnow()
    return CheckResult(check_id="layer1.backend.water_level", target="water_level",
                       stage="LB1", status=status, started_at=now - timedelta(seconds=1),
                       finished_at=now, summary="x")


def alarm(opened_on: str, *, as_text: bool = False) -> dict[str, Any]:
    """An open alarm row that opened on `opened_on`."""
    payload: Any = {"status_at_open": opened_on, "result_payload": {}}
    return {"id": 7, "check_id": "layer1.backend.water_level", "target": "water_level",
            "stage": "LB1", "payload": json.dumps(payload) if as_text else payload}


async def run_case(existing: dict[str, Any] | None, status: Status,
                   **store_kw: Any) -> FakeStore:
    """Evaluate one result against one store and return the store."""
    st = FakeStore(existing, **store_kw)
    await make_engine(st).evaluate(result(status))
    return st


async def main() -> int:
    """Run every case.

    Returns:
        The process exit status, 1 when any assertion failed.
    """
    now = utcnow()
    long_ago = now - timedelta(seconds=HOLD_S + 60)
    just_now = now - timedelta(seconds=30)

    st = await run_case(alarm("warn"), "fail", non_pass_start=long_ago, broken_start=long_ago)
    check("a warn alarm whose check has failed past the hold-down closes", st.closed == [7],
          str(st.closed))
    check("...and a new alarm opens on fail",
          len(st.opened) == 1 and st.opened[0]["payload"]["status_at_open"] == "fail",
          str([o["payload"]["status_at_open"] for o in st.opened]))

    st = await run_case(alarm("warn"), "fail", non_pass_start=long_ago, broken_start=just_now)
    check("one fail run after a long warn does not reopen (the warn is not counted)",
          not st.closed and not st.opened, f"closed {st.closed}, opened {len(st.opened)}")

    st = await run_case(alarm("warn", as_text=True), "fail", broken_start=long_ago)
    check("a payload still in JSON text is read too", st.closed == [7] and len(st.opened) == 1)

    st = await run_case(alarm("warn"), "error", broken_start=long_ago)
    check("an error reopens like a fail",
          st.closed == [7] and st.opened[0]["payload"]["status_at_open"] == "error")

    st = await run_case(alarm("warn"), "fail", raise_on_broken=True)
    check("a failed streak lookup reopens anyway (fails open)",
          st.closed == [7] and len(st.opened) == 1)

    st = await run_case(alarm("warn"), "fail", broken_start=None)
    check("no recorded fail run reopens rather than holding", st.closed == [7])

    st = await run_case(alarm("warn"), "warn", broken_start=long_ago)
    check("a warn alarm that stays warn is left alone", not st.closed and not st.opened)

    st = await run_case(alarm("fail"), "fail", broken_start=long_ago)
    check("a fail alarm that stays fail is left alone", not st.closed and not st.opened)

    st = await run_case(alarm("error"), "fail", broken_start=long_ago)
    check("an error alarm that goes to fail is left alone", not st.closed and not st.opened)

    st = await run_case(alarm("warn"), "pass")
    check("a pass still closes a warn alarm at once", st.closed == [7] and not st.opened)

    st = await run_case({"id": 9, "check_id": "c", "target": "t"}, "fail",
                        broken_start=long_ago)
    check("an alarm row without status_at_open is left alone", not st.closed and not st.opened)

    print(f"\n{len(failures)} FAILED: {', '.join(failures)}" if failures
          else "\nall warn-reopen assertions passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
