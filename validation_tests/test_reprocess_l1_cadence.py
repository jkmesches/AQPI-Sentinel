#!/usr/bin/env python3
"""D_cadence must re-verdict under a changed cadence_s.

Run:  python3 validation_tests/test_reprocess_l1_cadence.py

D_cadence was the one L1 sub-check the reprocess engine could not touch,
because its only input — the median spacing between the manifest's step
timestamps — was written to `metrics`, and `check_runs` stores the payload
only (metrics go to `metric_samples`). The engine said so in two places and
did the right thing with it: it left D_cadence at the recorded value rather
than recomputing from an assumed spacing.

That became visible on 2026-10-12. An operator set `cadence_s` from the admin
panel, D_cadence went to warn on `water_depth` and `water_level` — the stored
value no longer matched the manifests' real hourly step spacing — and a
retroactive reprocess could not clear it. The history brackets the edit:

    19:30:46  pass   D_cadence=pass
    20:01:11  pass   D_cadence=pass
    20:32:27  warn   D_cadence=warn    <- first edit took effect here
    20:42:44  warn   D_cadence=warn

`median_dt_s` is now written to the payload as well, so this asserts the hole
is closed in both directions, and that an OLD row — one written before the key
reached the payload — is still left alone rather than guessed at.
"""
from __future__ import annotations
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("SENTINEL_DB_URL", "postgresql://unused/unused")

from datetime import datetime, timezone                        # noqa: E402

from backend import thresholds as _t                           # noqa: E402
from backend.reprocess_engine import _reverdict_l1             # noqa: E402

failures: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if cond else 'FAIL'}  {label}" + (f"  — {detail}" if detail else ""))
    if not cond:
        failures.append(label)


def set_thresholds(product: str, **kw) -> None:
    """Install a threshold blob, as an admin panel edit would."""
    _t._cache = {"products": {product: dict(kw)}, "radars": {},
                 "l4": {}, "globals": {}}


def row(*, median_dt_s: float | None, recorded: str = "pass",
        product: str = "water_depth") -> dict:
    """An L1 row shaped the way layer1_product actually emits one."""
    payload = {
        "sub_status": {"C_freshness": "pass", "D_cadence": recorded,
                       "E_step_count": "skip"},
        "n_steps": 19,
        "product_label": product,
    }
    if median_dt_s is not None:
        payload["median_dt_s"] = median_dt_s
    return {"check_id": f"layer1.product.{product}", "target": product,
            "stage": "L1", "status": recorded, "summary": "",
            "finished_at": datetime(2026, 10, 12, 20, 32, tzinfo=timezone.utc),
            "payload": payload}


def verdict(r: dict) -> str | None:
    out = _reverdict_l1(r)
    if out is None:
        return None
    return (out[1].get("sub_status") or {}).get("D_cadence")


def main() -> int:
    # The live situation: 19 hourly steps, so the observed median is 3600.
    print("a cadence_s that matches the manifest's step spacing passes:")
    set_thresholds("water_depth", cadence_s=3600)
    check("median 3600 vs cadence_s 3600 -> pass",
          verdict(row(median_dt_s=3600.0, recorded="warn")) == "pass",
          str(verdict(row(median_dt_s=3600.0, recorded="warn"))))

    print("\nthe panel edit that caused this reproduces as warn:")
    set_thresholds("water_depth", cadence_s=300)
    check("median 3600 vs cadence_s 300 -> warn",
          verdict(row(median_dt_s=3600.0, recorded="pass")) == "warn",
          str(verdict(row(median_dt_s=3600.0, recorded="pass"))))
    set_thresholds("water_depth", cadence_s=120)
    check("median 3600 vs cadence_s 120 -> warn",
          verdict(row(median_dt_s=3600.0, recorded="pass")) == "warn")

    print("\nsetting it back clears the recorded warn — the thing that failed today:")
    set_thresholds("water_depth", cadence_s=3600)
    r = row(median_dt_s=3600.0, recorded="warn")
    out = _reverdict_l1(r)
    check("a recorded warn re-verdicts to pass",
          out is not None and (out[1]["sub_status"]["D_cadence"] == "pass"))
    check("...and the row's overall status follows it",
          out is not None and out[0] == "pass", out[0] if out else "-")

    print("\nthe 10% tolerance is applied, not ignored:")
    set_thresholds("water_depth", cadence_s=3600)
    check("median 3900 (8.3% off) -> pass",
          verdict(row(median_dt_s=3900.0)) == "pass")
    check("median 4100 (13.9% off) -> warn",
          verdict(row(median_dt_s=4100.0)) == "warn")

    print("\ncadence_s of None means nothing to compare against:")
    set_thresholds("water_depth", cadence_s=None)
    check("None -> pass, matching the live check's branch",
          verdict(row(median_dt_s=3600.0, recorded="warn")) == "pass")

    print("\nan OLD row with no median_dt_s is left alone, not guessed:")
    set_thresholds("water_depth", cadence_s=300)
    check("recorded pass stays pass",
          verdict(row(median_dt_s=None, recorded="pass")) == "pass")
    set_thresholds("water_depth", cadence_s=3600)
    check("recorded warn stays warn — we cannot re-derive it",
          verdict(row(median_dt_s=None, recorded="warn")) == "warn",
          str(verdict(row(median_dt_s=None, recorded="warn"))))

    print("\nthe live check and the engine share one tolerance default:")
    from backend.checks.layer1_product import DEFAULT_CADENCE_TOL
    from backend.reprocess_engine import _DEFAULT_CADENCE_TOL
    check("one constant, one home",
          DEFAULT_CADENCE_TOL is _DEFAULT_CADENCE_TOL
          or DEFAULT_CADENCE_TOL == _DEFAULT_CADENCE_TOL,
          f"{DEFAULT_CADENCE_TOL} / {_DEFAULT_CADENCE_TOL}")

    print("\nthe live check writes median_dt_s to the payload, not only metrics:")
    import inspect
    from backend.checks import layer1_product
    src = inspect.getsource(layer1_product)
    check('payload["median_dt_s"] is assigned',
          'payload["median_dt_s"]' in src)

    print()
    if failures:
        print(f"FAILED: {len(failures)} assertion(s)")
        return 1
    print("all D_cadence reprocess assertions passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
