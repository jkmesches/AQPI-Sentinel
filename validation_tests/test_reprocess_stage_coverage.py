#!/usr/bin/env python3
"""Every registered stage must be reprocessable, or visibly not.

This bug has shipped three times:

  2026-08-26  L2 had no branch in the reprocess dispatch. Jobs reported
              success having re-verdicted nothing (see _reverdict_l2's
              docstring).
  2026-10-03  LB1 and LB2 had no branch, the same day they were added, for
              the same reason (v0.5.3).

Each fix was another `elif`, which leaves the next stage to repeat it. The
failure is invisible by construction: an unhandled stage falls through to
`continue`, every row still counts as evaluated, and the job finishes green.
A reprocess that changed nothing looks exactly like a threshold that needed
no change.

So the assertions here are structural. They compare the stages the check
registry actually produces against the stages the reprocess engine can act
on, and require every one to be either handled or explicitly declared as
having nothing to recompute. A fourth stage added without a handler fails
this test instead of silently doing nothing in production.

Run:  python3 validation_tests/test_reprocess_stage_coverage.py
"""
from __future__ import annotations
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("SENTINEL_DB_URL", "postgresql://unused/unused")
# Register the backend checks too, so their stages are in scope even on a
# deployment that does not mount the shares. Without this the test would pass
# on shirejoe and fail only on cira-aqpi — i.e. exactly where it is not run.
os.environ.setdefault("SENTINEL_BACKEND_ROOT", "/nonexistent-for-registration")
os.environ.setdefault("SENTINEL_SSCB_ROOT", "/nonexistent-for-registration")

import backend.checks as _all                                   # noqa: E402,F401
from backend.registry import CHECKS                             # noqa: E402
from backend import reprocess_engine as R                       # noqa: E402
from backend.stages import stage_descriptor                     # noqa: E402

failures: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if cond else 'FAIL'}  {label}" + (f"  — {detail}" if detail else ""))
    if not cond:
        failures.append(label)


def main() -> int:
    live = sorted({c.stage for c in CHECKS.values()})
    handled = set(R._REVERDICT)
    declared = set(R._NO_REVERDICT)

    print("stages the check registry actually produces:")
    print(f"  {', '.join(live)}")
    check("the backend checks registered, so LB stages are in scope",
          "LB1" in live and "LB2" in live, ", ".join(live))

    print("\nevery live stage is accounted for:")
    for st in live:
        where = ("handled" if st in handled
                 else "declared no-op" if st in declared else None)
        check(f"stage {st} ({stage_descriptor(st)})", where is not None,
              where or "NOT in _REVERDICT and NOT in _NO_REVERDICT — "
                       "a reprocess job will skip it")

    print("\nthe registry does not name stages that do not exist:")
    for st in sorted(handled | declared):
        ok = st in live
        check(f"{st} is a real stage", ok,
              "" if ok else "registered for reprocessing but no check emits it")

    print("\nhandled and no-op are disjoint (a stage cannot be both):")
    overlap = handled & declared
    check("no stage is in both tables", not overlap, ", ".join(sorted(overlap)))

    print("\nthe UI picker derives from the registry:")
    check("reverdict_stages() returns exactly the handled set",
          set(R.reverdict_stages()) == handled,
          f"{R.reverdict_stages()} vs {sorted(handled)}")
    check("...and is sorted, so the picker order is stable",
          R.reverdict_stages() == sorted(R.reverdict_stages()))

    print("\nan unhandled stage is counted, not swallowed:")
    # The job must be able to say "I skipped N rows of stage X". Without this
    # the only symptom of a missing handler is a job that changes nothing.
    job = R.ReprocessJob("t", __import__("datetime").datetime.now(
        __import__("datetime").timezone.utc), __import__("datetime").datetime.now(
        __import__("datetime").timezone.utc), None)
    check("the job tracks an unhandled count", hasattr(job, "n_unhandled"))
    check("...and which stages were unhandled", hasattr(job, "unhandled_stages"))
    d = job.to_dict()
    check("...and reports both in its status payload",
          "n_unhandled" in d and "unhandled_stages" in d, ", ".join(sorted(d)))

    print(f"\n{len(failures)} FAILED: {', '.join(failures)}" if failures
          else "\nall reprocess stage-coverage assertions passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
