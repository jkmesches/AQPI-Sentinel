"""The CoSMoS cycle manifest checks and the LB1 rule that reads it.

Run directly (no DB, no network, no mount):

    python validation_tests/test_cosmos_cycle.py

Why this exists: the four LB1 CoSMoS products go stale for reasons a frame's
age cannot tell apart. Joseph's rule of 2026-11-23 (wiki plans/74- item 3)
lets a late product warn instead of fail when the cycle manifest is under
150 min old, names one of the two skips, and the product is under 240 min
old. Every input here is a real manifest from the model host. The replay at
the end runs the rule over the observer's samples from the night of
2026-11-23, when a t23z skip and a warmstart stand-down left the products
184 min without a publish, and checks that it would have warned the whole
way and never failed.
"""
from __future__ import annotations

import asyncio
import json
import math
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "validation_tests" / "fixtures" / "cosmos_manifest"
MOUNT = tempfile.mkdtemp(prefix="cosmos-")
sys.path.insert(0, str(ROOT))
os.environ["SENTINEL_PROFILE"] = "aqpi"
os.environ["SENTINEL_BACKEND_ROOT"] = MOUNT
os.environ.setdefault("SENTINEL_DB_URL", "postgresql://unused/unused")

import backend.checks  # noqa: E402,F401  registers the checks before the modules below
from backend.alarms.suppression import is_inconclusive  # noqa: E402
from backend.checks import layer1_cosmos_cycle as cyc  # noqa: E402
from backend.checks.layer1_backend_product import (  # noqa: E402
    Layer1BackendProductCheck, _product_dir)
from backend.cosmos_manifest import (  # noqa: E402
    CycleManifest, ManifestError, explained_gap, hrrr_complete, parse_lock_time,
    parse_manifest)
from backend.reprocess_engine import _reverdict_lb1  # noqa: E402

UTC = timezone.utc
LB1_LIMIT_S = 7200.0
failures: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    """Print one assertion and record a failure."""
    print(f"  {'ok  ' if cond else 'FAIL'}  {label}" + (f"  — {detail}" if detail else ""))
    if not cond:
        failures.append(label)


def fixture(name: str) -> dict[str, Any]:
    """One real manifest, parsed as JSON."""
    doc: dict[str, Any] = json.loads((FIXTURES / name).read_text())
    return doc


def at(stamp: str) -> datetime:
    """A UTC instant from an ISO string ending in Z."""
    return datetime.fromisoformat(stamp.replace("Z", "+00:00"))


STANDDOWN = parse_manifest(fixture("standdown-02-skipped_warmstart_running-20261123_0000.json"))
SHORT_HRRR = parse_manifest(fixture("standdown-01-skipped_incomplete_hrrr-20261122_2300.json"))
NO_FORECAST = parse_manifest(fixture("exhaustion-02-no_forecast_exit1-20261122_2100.json"))
RAN = parse_manifest(fixture("exhaustion-03-ran-20261122_2200.json"))


def test_parser() -> None:
    """Every real manifest parses, and the two traps in them are handled."""
    print("parsing the real manifests:")
    check("the stand-down's lock time is 01:25:01Z, offset applied",
          STANDDOWN.lock_since == datetime(2026, 11, 23, 1, 25, 1, 785167, tzinfo=UTC),
          str(STANDDOWN.lock_since))
    check("20 HRRR files against 19 expected is complete", hrrr_complete(STANDDOWN) is True)
    check("5 of 19 is not", hrrr_complete(SHORT_HRRR) is False)
    check("a skip for short HRRR carries no lock time", SHORT_HRRR.lock_since is None)
    check("wrote_at is UTC", STANDDOWN.wrote_at == at("2026-11-23T02:00:44Z"))
    check("the outcomes read as written",
          [m.outcome for m in (RAN, NO_FORECAST, SHORT_HRRR, STANDDOWN)]
          == ["ran", "no_forecast_exit1", "skipped_incomplete_hrrr",
              "skipped_warmstart_running"])
    good = fixture("standdown-02-skipped_warmstart_running-20261123_0000.json")
    for label, doc in (("a list", []), ("no wrote_at", {**good, "wrote_at": None}),
                       ("a naive wrote_at", {**good, "wrote_at": "2026-11-23T02:00:44"}),
                       ("a garbled wrote_at", {**good, "wrote_at": "yesterday"}),
                       ("a wrote_at that overflows UTC",
                        {**good, "wrote_at": "0001-01-01T00:00:00+05:00"}),
                       ("a wrote_at that overflows the other way",
                        {**good, "wrote_at": "9999-12-31T23:59:59-05:00"}),
                       ("a non-string wrote_at", {**good, "wrote_at": 1700000000}),
                       ("no outcome", {**good, "outcome": ""})):
        try:
            _ = parse_manifest(doc)
            check(f"{label} is refused", False, "parsed")
        except ManifestError:
            check(f"{label} is refused", True)
    for reason, want in (
            ("warmstart holds model/active since 2026-11-23 06:55:00 +0530",
             datetime(2026, 11, 23, 1, 25, tzinfo=UTC)),
            ("since 2026-11-22 18:25:01.5 -0700", datetime(2026, 11, 23, 1, 25, 1, 500000,
                                                         tzinfo=UTC)),
            ("since 2026-11-22 18:25:01 -9900", None), ("no time here", None),
            ("since 2026-13-40 18:25:01 -0700", None),
            ("since 0001-01-01 00:00:00 +0700", None),
            ("since 9999-12-31 23:59:59 -0500", None)):
        got = parse_lock_time(reason)
        check(f"lock time in {reason!r}", got == want, str(got))


def manifest(outcome: str, wrote_at: datetime) -> CycleManifest:
    """A manifest with one outcome, written at one instant."""
    return CycleManifest(metfile="20261123_0000", wrote_at=wrote_at, outcome=outcome,
                         reason="r", file_count=19, file_count_expected=19, lock_since=None)


def test_rule() -> None:
    """The rule's three conditions and their edges."""
    print("\nJoseph's rule, at its edges:")
    now = at("2026-11-23T02:22:34Z")
    check("tonight's first LB1 fail would have been explained",
          explained_gap(STANDDOWN, now, 121 * 60) == STANDDOWN.reason)
    for mins, want in ((149.9, True), (150.0, False)):
        m = manifest("skipped_incomplete_hrrr", now - timedelta(minutes=mins))
        check(f"a manifest {mins} min old {'vouches' if want else 'does not'}",
              (explained_gap(m, now, 7300) is not None) is want)
    fresh = manifest("skipped_warmstart_running", now - timedelta(minutes=5))
    for age, want in ((240 * 60, True), (240 * 60 + 1, False), (math.nan, False),
                      (math.inf, False)):
        check(f"a product {age} s old {'is excused' if want else 'is not'}",
              (explained_gap(fresh, now, age) is not None) is want)
    for outcome in ("ran", "no_forecast_exit1", "publish_failed", "something_new"):
        check(f"{outcome} explains nothing",
              explained_gap(manifest(outcome, now), now, 7300) is None)
    check("no manifest explains nothing", explained_gap(None, now, 7300) is None)
    for ahead_s in (1, 5 * 3600):
        ahead = manifest("skipped_incomplete_hrrr", now + timedelta(seconds=ahead_s))
        check(f"a manifest dated {ahead_s} s ahead vouches for nothing",
              explained_gap(ahead, now, 7300) is None)
    check("one written this instant vouches",
          explained_gap(manifest("skipped_incomplete_hrrr", now), now, 7300) is not None)
    check("a skip with no reason names its outcome",
          explained_gap(CycleManifest("m", now, "skipped_incomplete_hrrr", "", None, None,
                                      None), now, 7300) == "skipped_incomplete_hrrr")


def lb1_verdict(age_s: float, m: CycleManifest | None, now: datetime) -> str:
    """The LB1 verdict the live check gives at the AQPI limit."""
    if age_s <= LB1_LIMIT_S:
        return "pass"
    return "warn" if explained_gap(m, now, age_s) else "fail"


def replay_rows() -> list[tuple[datetime, str, dict[str, datetime]]]:
    """The observer's samples from the stand-down night.

    Returns:
        Per sample, its time, the wrote_at of the manifest in place, and each
        product's newest frame mtime.
    """
    rows = []
    for line in (FIXTURES / "standdown-timeline.txt").read_text().splitlines():
        parts = line.split(" | ")
        if len(parts) < 5:
            continue
        wrote = parts[3].split()[-1]
        frames = {}
        for item in parts[4].removeprefix("frames:").split(";"):
            bits = item.split()
            if len(bits) == 3:
                frames[bits[0]] = at(bits[2])
        rows.append((at(parts[0]), wrote, frames))
    return rows


def test_replay() -> None:
    """Tonight's samples never fail under the rule, and did under the old one."""
    print("\nreplaying the night of 2026-11-23:")
    by_wrote = {m.wrote_at: m for m in (SHORT_HRRR, STANDDOWN)}
    rows = replay_rows()
    check("all 42 samples read", len(rows) == 42, str(len(rows)))
    verdicts: list[str] = []
    old: list[str] = []
    for when, wrote, frames in rows:
        m = by_wrote.get(at(wrote))
        for mtime in frames.values():
            age = (when - mtime).total_seconds()
            verdicts.append(lb1_verdict(age, m, when))
            old.append("pass" if age <= LB1_LIMIT_S else "fail")
    check("the old rule failed during the gap", "fail" in old, f"{old.count('fail')} fails")
    check("the new rule never fails", "fail" not in verdicts,
          f"{verdicts.count('warn')} warns, {verdicts.count('pass')} passes")
    # The 03:00Z cycle published water_level at 03:25:30Z, reported by the
    # AQPI Backend session. Thirty seconds before, the stand-down's manifest
    # was still the newest one.
    before = at("2026-11-23T03:25:00Z")
    age = (before - at("2026-11-23T00:21:30Z")).total_seconds()
    check("just before the 03:25:30Z publish it still warns",
          lb1_verdict(age, STANDDOWN, before) == "warn", f"{age / 60:.1f} min")


def write_manifest(doc: dict[str, Any] | str | None) -> None:
    """Put a manifest on the fake mount.

    Args:
        doc: The manifest, raw text written as is, or None to remove it.
    """
    path = Path(cyc.manifest_path())
    path.parent.mkdir(parents=True, exist_ok=True)
    if doc is None:
        path.unlink(missing_ok=True)
        return
    path.write_text(doc if isinstance(doc, str) else json.dumps(doc))


def live(outcome: str, minutes_ago: float) -> dict[str, Any]:
    """A real manifest rewritten to one outcome and age."""
    doc = fixture("standdown-02-skipped_warmstart_running-20261123_0000.json")
    wrote = datetime.now(UTC) - timedelta(minutes=minutes_ago)
    return {**doc, "outcome": outcome, "wrote_at": wrote.strftime("%Y-%m-%dT%H:%M:%SZ")}


def run(check_obj: Any) -> Any:
    """Run one check once."""
    return asyncio.run(check_obj.run(None))


def test_manifest_age_check() -> None:
    """Item 11: pass, warn past 150 min, fail past 240, and the read failures."""
    print("\nitem 11, the manifest's age:")
    age_check = cyc.CosmosManifestAgeCheck()
    for minutes, want in ((10, "pass"), (151, "warn"), (241, "fail"), (-3, "warn")):
        write_manifest(live("ran", minutes))
        r = run(age_check)
        check(f"a manifest {minutes} min old reads {want}", r.status == want, r.summary)
    write_manifest(None)
    check("no manifest is a fail", run(age_check).status == "fail")
    write_manifest("{not json")
    check("a torn manifest is an error", run(age_check).status == "error")
    write_manifest({"outcome": "ran"})
    check("a manifest with no wrote_at is an error", run(age_check).status == "error")
    write_manifest("[" * 100_000)
    r = run(age_check)
    check("a manifest nested past the recursion limit is an error, not an exception",
          r.status == "error" and "nests too deeply" in r.summary, r.summary)
    write_manifest({**live("ran", 10), "wrote_at": "0001-01-01T00:00:00+05:00"})
    check("a wrote_at that overflows is an error", run(age_check).status == "error")
    write_manifest(live("ran", 10))
    original_load, original_timeout = cyc._load, cyc.FS_TIMEOUT_S

    def hang(path: str) -> CycleManifest:
        import time
        time.sleep(0.5)
        return original_load(path)
    cyc._load, cyc.FS_TIMEOUT_S = hang, 0.05
    try:
        r = run(age_check)
        check("a hung mount is an error, not a hang", r.status == "error", r.summary)
    finally:
        cyc._load, cyc.FS_TIMEOUT_S = original_load, original_timeout


def test_cycle_check() -> None:
    """Item 3: each outcome's verdict, and a stale manifest declines to judge."""
    print("\nitem 3, the cycle's outcome:")
    cycle = cyc.CosmosCycleCheck()
    for outcome, want in (("ran", "pass"), ("skipped_incomplete_hrrr", "warn"),
                          ("skipped_warmstart_running", "warn"),
                          ("no_forecast_exit1", "fail"), ("publish_failed", "fail"),
                          ("brand_new_outcome", "error")):
        write_manifest(live(outcome, 10))
        r = run(cycle)
        check(f"{outcome} reads {want}", r.status == want, r.summary)
    write_manifest(live("skipped_warmstart_running", 10))
    r = run(cycle)
    check("the stand-down names the lock time in UTC", "lock taken 01:25Z" in r.summary,
          r.summary)
    check("the manifest's reason does not land in the inconclusive key",
          r.payload.get("reason") is None and "warmstart" in r.payload["manifest_reason"])
    write_manifest(live("ran", 151))
    r = run(cycle)
    check("a manifest past 150 min is an inconclusive skip, never a stale pass",
          r.status == "skip" and is_inconclusive(r.status, r.payload), r.summary)
    write_manifest(live("skipped_incomplete_hrrr", -180))
    r = run(cycle)
    check("so is a manifest dated three hours ahead of this host",
          r.status == "skip" and is_inconclusive(r.status, r.payload), r.summary)
    check("item 3 depends on item 11", cycle.depends_on == [cyc.MANIFEST_CHECK_ID])


def stale_product(product: str, minutes_ago: float) -> None:
    """Replace a product's frames on the fake mount with one aged frame.

    Args:
        product: The product id.
        minutes_ago: How old the frame's mtime is.
    """
    d = Path(_product_dir(product))
    d.mkdir(parents=True, exist_ok=True)
    for old in d.iterdir():
        old.unlink()
    frame = d / "20261123_1600.png"
    frame.write_bytes(b"\x89PNG")
    t = (datetime.now(UTC) - timedelta(minutes=minutes_ago)).timestamp()
    os.utime(frame, (t, t))


def test_lb1() -> None:
    """The LB1 product check applies the rule to CoSMoS products alone."""
    print("\nLB1 with the manifest:")
    water = Layer1BackendProductCheck("water_level")
    stale_product("water_level", 1)
    limit_min = run(water).payload["max_age_s"] / 60
    late = limit_min + 10
    cases = (("skipped_warmstart_running", 20, late, "warn"),
             ("skipped_incomplete_hrrr", 20, late, "warn"),
             ("ran", 20, late, "fail"), ("publish_failed", 20, late, "fail"),
             ("skipped_warmstart_running", 151, late, "fail"),
             ("skipped_warmstart_running", 20, 241, "fail"))
    for outcome, m_age, p_age, want in cases:
        write_manifest(live(outcome, m_age))
        stale_product("water_level", p_age)
        r = run(water)
        check(f"{outcome}, manifest {m_age} min, product {p_age:.0f} min: {want}",
              r.status == want, r.summary)
    write_manifest(live("skipped_warmstart_running", 20))
    stale_product("water_level", late)
    r = run(water)
    check("the warn says why", r.summary.startswith("BACKEND LATE, cycle skipped ("),
          r.summary)
    check("...and records that the manifest vouched",
          r.payload["cycle_manifest"]["explains_gap"] is True)
    write_manifest(None)
    r = run(water)
    check("no manifest leaves it failed, and says so",
          r.status == "fail" and "no cycle manifest" in r.payload["cycle_manifest"]["error"])
    write_manifest("[" * 100_000)
    r = run(water)
    check("a manifest too deep to read leaves it failed rather than erroring",
          r.status == "fail" and "nests too deeply" in r.payload["cycle_manifest"]["error"],
          r.status)
    write_manifest(live("skipped_warmstart_running", -30))
    check("a manifest dated ahead excuses nothing", run(water).status == "fail")
    stale_product("water_level", 1)
    r = run(water)
    check("a fresh product never reads the manifest", "cycle_manifest" not in r.payload)
    write_manifest(live("skipped_warmstart_running", 20))
    comp = Layer1BackendProductCheck("comp_ref")
    stale_product("comp_ref", 24 * 60)
    check("a product outside CoSMoS is not excused", run(comp).status == "fail")


def test_reprocess() -> None:
    """A retroactive reprocess keeps what the manifest vouched for."""
    print("\nretroactive reprocess:")
    base = {"age_s": 130 * 60.0, "max_age_s": 7200.0,
            "sub_status": {"A_present": "pass", "B_freshness": "warn"}}
    row = {"target": "water_level"}
    limit = _reverdict_lb1({**row, "payload": {**base, "age_s": 0.0}})
    check("a fresh row stays a pass", limit is not None and limit[0] == "pass")
    vouched = {**base, "cycle_manifest": {"explains_gap": True}}
    for label, payload, want in (
            ("a vouched row stays a warn", vouched, "warn"),
            ("a row the manifest did not vouch for fails",
             {**base, "cycle_manifest": {"explains_gap": False}}, "fail"),
            ("a row with no manifest record fails", base, "fail"),
            ("a vouched row past the ceiling fails", {**vouched, "age_s": 241 * 60.0}, "fail")):
        got = _reverdict_lb1({**row, "payload": payload})
        check(label, got is not None and got[0] == want, str(got and got[0]))


def main() -> int:
    """Run every section.

    Returns:
        The process exit status, 1 when any assertion failed.
    """
    test_parser()
    test_rule()
    test_replay()
    test_manifest_age_check()
    test_cycle_check()
    test_lb1()
    test_reprocess()
    print(f"\n{len(failures)} FAILED: {', '.join(failures)}" if failures
          else "\nall CoSMoS cycle assertions passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
