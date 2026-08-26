"""L2 reprocess — re-verdict GHOST_UP under changed thresholds, safely.

Run directly (no DB, no network):

    python validation_tests/test_reprocess_l2.py

Background: _reverdict_l2 read payload["reconcile"], a key layer2_radar has
never emitted, so L2 reprocessing was a silent no-op for every row (verified:
0 of 231,356 production L2 rows carry `reconcile`; 212,572 carry `observed`).

The load-bearing property is the second test. A radar publishing NO images is
not-fresh regardless of threshold, so those GHOST_UPs are real stoppages —
5,042 of XSWR's 7,950 over 14 days, including the 79-hour fleet episode. A
threshold change must never be able to erase them from history. If that test
ever fails, reprocessing has become capable of deleting evidence of real
outages.
"""
from __future__ import annotations
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("SENTINEL_DB_URL", "postgresql://unused/unused")

from backend import reprocess_engine as R          # noqa: E402
from backend import thresholds as _thresholds      # noqa: E402

failures: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if cond else 'FAIL'}  {label}" + (f"  — {detail}" if detail else ""))
    if not cond:
        failures.append(label)


NOW = datetime(2026, 8, 26, 0, 50, tzinfo=timezone.utc)


def row(*, verdict="GHOST_UP", status="fail", primary=25, age_s=353,
        silent_fail_s=240, extra=None):
    """A production-shaped L2 row."""
    newest = NOW - timedelta(seconds=age_s)
    payload = {
        "radar": "XSWR", "folder": "swyr", "verdict": verdict, "declared": "UP",
        "observed": {
            "fresh": verdict == "HEALTHY", "primary": primary,
            "primary_age_s": age_s, "silent_fail_s": silent_fail_s,
            "primary_moment": "Reflectivity",
            "primary_newest_ts": None if primary == 0 else newest.isoformat(),
        },
        "moments": {"Reflectivity": primary},
    }
    if extra:
        payload.update(extra)
    return {"id": 1, "target": "XSWR", "status": status,
            "finished_at": NOW, "payload": payload, "stage": "L2"}


def with_threshold(radar_threshold: int):
    # _cache starts as None (populated from the DB at boot); set it directly
    # so these tests are hermetic.
    _thresholds._cache = {
        "radars": {"XSWR": {"silent_fail_s": radar_threshold}},
        "globals": {"hysteresis": 0.10},
        "products": {}, "l4": {},
    }


def main() -> int:
    print("[1] the shape bug: production payloads must actually reprocess")
    with_threshold(660)
    out = R._reverdict_l2(row(age_s=353))
    check("a real-shaped payload is no longer a silent no-op", out is not None)
    if out:
        st, pl = out
        check("stale-at-240s run becomes HEALTHY at 660s", st == "pass" and pl["verdict"] == "HEALTHY")
        check("original verdict preserved for audit",
              pl["original"]["verdict"] == "GHOST_UP" and pl["original"]["status"] == "fail")
        check("original threshold preserved", pl["original"]["silent_fail_s"] == 240)
        check("raw observation untouched", pl["observed"]["primary"] == 25)

    print("\n[2] LOAD-BEARING: zero-image runs are never reclassified")
    with_threshold(100_000)                       # absurdly permissive
    check("zero images -> None even at a 100000s threshold",
          R._reverdict_l2(row(primary=0, age_s=99999)) is None,
          "these are real stoppages; no threshold may erase them")
    check("zero images -> None also when verdict is GHOST_UP and status fail",
          R._reverdict_l2(row(primary=0, verdict="GHOST_UP", status="fail")) is None)

    print("\n[3] non-threshold verdicts are preserved")
    with_threshold(660)
    for v in ("CONFIRMED_DOWN", "STUCK_DOWN_FLAG", "OBSERVED_API_ERROR"):
        check(f"{v} is left alone", R._reverdict_l2(row(verdict=v)) is None)

    print("\n[4] genuinely stale data still reads as GHOST_UP")
    with_threshold(660)
    out = R._reverdict_l2(row(verdict="HEALTHY", status="pass", age_s=5000))
    check("age 5000s stays/becomes GHOST_UP at 660s",
          out is not None and out[0] == "fail" and out[1]["verdict"] == "GHOST_UP")

    print("\n[5] repeated reprocess does not overwrite the first original")
    with_threshold(660)
    first = R._reverdict_l2(row(age_s=353))
    assert first is not None
    r2 = row(age_s=353)
    r2["payload"] = first[1]
    r2["status"] = first[0]
    with_threshold(240)                            # revert the threshold
    second = R._reverdict_l2(r2)
    check("re-running preserves the ORIGINAL as-observed verdict",
          second is not None and second[1]["original"]["verdict"] == "GHOST_UP",
          "not the intermediate reprocessed value")

    print("\n[6] malformed rows are skipped, not crashed on")
    check("missing observed -> None", R._reverdict_l2(
        {"id": 1, "target": "XSWR", "status": "fail", "finished_at": NOW,
         "payload": {"verdict": "GHOST_UP"}, "stage": "L2"}) is None)
    bad = row(); bad["payload"]["observed"]["primary_newest_ts"] = "not-a-date"
    check("unparseable timestamp -> None", R._reverdict_l2(bad) is None)
    check("empty payload -> None", R._reverdict_l2(
        {"id": 1, "target": "XSWR", "status": "fail", "finished_at": NOW,
         "payload": {}, "stage": "L2"}) is None)

    print(f"\n{'PASS' if not failures else 'FAILED: ' + ', '.join(failures)}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
