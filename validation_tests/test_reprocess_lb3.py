#!/usr/bin/env python3
"""LB3 rows must actually re-verdict under a changed threshold.

Being listed in `reverdict_stages()` only means the admin UI will OFFER the
stage. It says nothing about whether the handler recomputes anything, and a
handler that returns None for every row is indistinguishable from a missing
one except that it does not show up as unhandled. `_reverdict_l2` sat in
exactly that state until 2026-08-26 — it read `payload["reconcile"]`, a key
layer2_radar has never emitted, so L2 reprocessing was a silent no-op and the
belief that "historical L2 can't be reprocessed" grew up around it.

So this exercises the handler against the payload shapes the LB3 checks
actually emit, and asserts the verdict MOVES when the threshold moves.

=== One thing is deliberately NOT recomputed ===

`A_included` — whether a radar had a block in the composite run. That is a
fact about a file read at the time, not a threshold comparison, and no change
to a threshold can alter it. Recomputing it from what the payload holds would
be inventing an observation. Only `B_fresh` re-bands.

Run:  python3 validation_tests/test_reprocess_lb3.py
"""
from __future__ import annotations
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("SENTINEL_DB_URL", "postgresql://unused/unused")

from backend import thresholds as _t                      # noqa: E402
from backend.reprocess_engine import _reverdict_lb3, _reverdict_lb2, _REVERDICT  # noqa: E402

failures: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if cond else 'FAIL'}  {label}" + (f"  — {detail}" if detail else ""))
    if not cond:
        failures.append(label)


def set_globals(**kw) -> None:
    """Install a threshold blob, as an admin edit would."""
    _t._cache = {"products": {}, "radars": {}, "l4": {}, "globals": dict(kw)}


def participation_row(age_s: float, *, included: bool = True,
                      warn: float = 600.0, fail: float = 900.0) -> dict:
    """A row in the shape Layer3CompositeParticipationCheck persists."""
    sub = {"A_included": "pass" if included else "fail"}
    payload = {"receipt_dir": "XSWR", "participation_source": "run_log",
               "contrib_warn_s": warn, "contrib_fail_s": fail,
               "age_basis": "composite_reported", "sub_status": sub}
    if included:
        sub["B_fresh"] = "pass" if age_s <= warn else "warn" if age_s <= fail else "fail"
        payload["age_s"] = age_s
    return {"check_id": "layer3.composite.XSWR", "target": "XSWR",
            "payload": payload}


def drops_row(ages_min: dict[str, float], limit: float = 3600.0) -> dict:
    sub = {r: "pass" if m * 60 <= limit else "warn" for r, m in ages_min.items()}
    return {"check_id": "layer3.backend.drops", "target": "drops-qpe",
            "payload": {"informational": True, "silent_s": limit,
                        "folder_age_min": dict(ages_min), "sub_status": sub}}


def main() -> int:
    check("LB3 has a reverdict handler at all", "LB3" in _REVERDICT)

    print("\nparticipation: B_fresh re-bands when the threshold moves")
    # 700 s was warn under 600/900. It is the interesting value because it
    # moves in BOTH directions, which a single-direction test would miss.
    row = participation_row(700.0)
    check("the stored row starts as warn",
          row["payload"]["sub_status"]["B_fresh"] == "warn")

    set_globals(composite_contrib_warn_s=800, composite_contrib_fail_s=1200)
    got = _reverdict_lb3(row)
    check("loosened -> pass", got and got[0] == "pass", str(got and got[0]))
    check("...and the new bands are written into the payload",
          got and got[1]["contrib_warn_s"] == 800.0 and got[1]["contrib_fail_s"] == 1200.0,
          str(got and (got[1]["contrib_warn_s"], got[1]["contrib_fail_s"])))

    set_globals(composite_contrib_warn_s=300, composite_contrib_fail_s=500)
    got = _reverdict_lb3(row)
    check("tightened -> fail", got and got[0] == "fail", str(got and got[0]))

    set_globals(composite_contrib_warn_s=600, composite_contrib_fail_s=900)
    got = _reverdict_lb3(row)
    check("back to the original bands -> warn again",
          got and got[0] == "warn", str(got and got[0]))
    check("...which proves the handler reads the STORE, not the payload",
          got is not None,
          "a payload-only handler could not have produced three answers")

    print("\nA_included is preserved, never recomputed:")
    set_globals(composite_contrib_warn_s=1, composite_contrib_fail_s=2)
    got = _reverdict_lb3(participation_row(700.0))
    check("a pass stays pass even when B_fresh is driven to fail",
          got and got[1]["sub_status"]["A_included"] == "pass",
          str(got and got[1]["sub_status"]))
    check("...and the overall status is the worst of the two",
          got and got[0] == "fail", str(got and got[0]))

    # A radar absent from the run has no contribution age, so there is nothing
    # threshold-driven to recompute and the row must be left exactly as it is.
    got = _reverdict_lb3(participation_row(0.0, included=False))
    check("an absent-radar row returns None, leaving the fail intact",
          got is None, str(got))

    print("\nthe QPE producer re-bands per folder, and keeps its warn ceiling:")
    set_globals(drops_silent_info_s=3600)
    row = drops_row({"XEBY": 20.0, "XSCR": 90.0})
    got = _reverdict_lb3(row)
    check("at a 1 h limit: 20 min passes, 90 min warns",
          got and got[1]["sub_status"] == {"XEBY": "pass", "XSCR": "warn"},
          str(got and got[1]["sub_status"]))
    set_globals(drops_silent_info_s=600)
    got = _reverdict_lb3(row)
    check("tightened to 10 min: both warn",
          got and got[1]["sub_status"] == {"XEBY": "warn", "XSCR": "warn"},
          str(got and got[1]["sub_status"]))
    # The cap is a severity decision, not an observation -- a fail would
    # auto-promote to critical at 30 min and page for a tree no live product
    # reads. It must survive reprocessing too.
    check("...and NEVER fail, however stale, so it cannot page retroactively",
          got and got[0] == "warn", str(got and got[0]))
    set_globals(drops_silent_info_s=1)
    got = _reverdict_lb3(drops_row({"XEBY": 100000.0}))
    check("even at an absurd staleness the ceiling holds",
          got and got[0] == "warn", str(got and got[0]))

    # A folder with no recorded age was unreadable at the time. Guessing it
    # healthy would manufacture an observation nobody made.
    r = drops_row({"XEBY": 20.0})
    r["payload"]["sub_status"]["XSCW"] = "warn"      # recorded, age missing
    set_globals(drops_silent_info_s=3600)
    got = _reverdict_lb3(r)
    check("a folder with no recorded age keeps its stored verdict",
          got and got[1]["sub_status"]["XSCW"] == "warn",
          str(got and got[1]["sub_status"]))

    print("\nthe fleet row is not a radar row, and LB2's handler leaves it alone:")
    # layer2.backend.fleet shares stage LB2 with the per-radar checks, so a
    # reprocess pass over LB2 hands it to _reverdict_lb2. Its payload has no
    # age_s and no A_arriving, so the handler declines -- which is what we
    # want: whether evidence was stale at the time is a fact about that
    # moment, the same asymmetry as A_included.
    fleet = {"check_id": "layer2.backend.fleet", "target": "radar-fleet",
             "payload": {"n": 3, "of": 5, "systemic": True, "latched": False,
                         "scope": "xband-path", "verdicts": {},
                         "sub_status": {}}}
    check("a fleet row returns None from the LB2 handler",
          _reverdict_lb2(fleet) is None, str(_reverdict_lb2(fleet)))

    print("\nfalling back to the payload when nothing is stored:")
    _t._cache = None
    got = _reverdict_lb3(participation_row(700.0, warn=400.0, fail=500.0))
    check("with no blob, the row's own recorded bands are used",
          got and got[0] == "fail", str(got and got[0]))
    check("...so a reprocess still works on an instance that never seeded",
          got and got[1]["contrib_fail_s"] == 500.0,
          str(got and got[1]["contrib_fail_s"]))

    print(f"\n{len(failures)} FAILED: {', '.join(failures)}" if failures
          else "\nall LB3 reprocess assertions passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
