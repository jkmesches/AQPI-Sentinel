#!/usr/bin/env python3
"""Forecast-manifest parity: what counts as broken, and what is just untidy.

The upstream stitches a short-range and a long-range block into one list and
re-lists the long-range block one or more times. Measured over seven days to
2026-09-11, `temperature/details_F.json` was served at 19, 74, 129, 187 and
243 entries — one more replay each time — and the old rule ("every index is
the previous plus one") called every replay a mismatch. Result: warn on 265
of 341 runs, flapping pass/warn as the upstream alternated between its short
and long forms, and 25 alarms opened and auto-closed in a week for a condition
that never went away.

These assertions pin both halves of the fix:

  a replay of an already-published block does NOT warn, and
  a dropped or reordered forecast hour still DOES.

The first four cases run against manifests captured from production rather
than hand-written ones, because the shape that broke this is the shape that
matters and a fixture I invent is a fixture that agrees with me.

Run:  python3 validation_tests/test_forecast_parity.py
"""
from __future__ import annotations
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("SENTINEL_DB_URL", "postgresql://unused/unused")

from backend.checks.layer1_product import classify_step_sequence   # noqa: E402

SAMPLES = Path(__file__).resolve().parent / "samples" / "forecast_manifests"

failures: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if cond else 'FAIL'}  {label}" + (f"  — {detail}" if detail else ""))
    if not cond:
        failures.append(label)


def load(name: str) -> list[tuple[int, str]]:
    d = json.loads((SAMPLES / name).read_text())
    out = []
    for s in d["steps"]:
        m = re.search(r"step(\d+)\.png$", s["imageName"])
        if m:
            out.append((int(m.group(1)), s["timestamp"]))
    return out


def synth(*blocks: tuple[int, int, int]) -> list[tuple[int, str]]:
    """Build (step, ts) from (first_step, last_step, first_hour) blocks.

    Hours are minted per block so a replay carries the SAME timestamps as the
    block it replays — which is what production does, and what makes the
    first-occurrence rule the thing under test.
    """
    out = []
    for lo, hi, h0 in blocks:
        for k, step in enumerate(range(lo, hi + 1)):
            out.append((step, f"2026-09-12T{h0 + k:02d}:00:00"))
    return out


def main() -> int:
    print("\n=== captured from production ===")

    long_ = load("fcst_temp_129.json")
    r = classify_step_sequence(long_)
    check("the 129-entry manifest is 3 blocks", r["blocks"] == 3, str(r["blocks"]))
    check("...with 55 redundant listings", r["repeated_entries"] == 55,
          str(r["repeated_entries"]))
    check("...one step carrying two forecast times (the seam)",
          r["steps_multi_ts"] == 1, str(r["steps_multi_ts"]))
    check("...and NO defect — this is the run that warned 265 times",
          r["defects"] == [], str(r["defects"]))

    short = load("fcst_temp_19.json")
    rs = classify_step_sequence(short)
    check("the 19-entry manifest is one clean block",
          rs["blocks"] == 1 and rs["defects"] == [] and rs["repeated_entries"] == 0)

    rate = classify_step_sequence(load("fcst_precip_rate_72.json"))
    check("fcst_precip_rate stays clean (it never warned, and must not start)",
          rate["defects"] == [], str(rate["defects"]))

    prcp = classify_step_sequence(load("fcst_total_precip_18.json"))
    check("fcst_total_precip stays clean", prcp["defects"] == [], str(prcp["defects"]))

    print("\n=== the shapes that must still warn ===")

    gap = synth((0, 5, 0)) + synth((8, 12, 8))
    rg = classify_step_sequence(gap)
    check("a dropped forecast hour is a defect", rg["defects"] == ["missing_steps"],
          str(rg["defects"]))
    check("...naming what is missing",
          rg["first_defect"]["after_step"] == 5 and rg["first_defect"]["missing"] == 2,
          str(rg["first_defect"]))

    back = [(0, "2026-09-12T00:00:00"), (1, "2026-09-12T01:00:00"),
            (2, "2026-09-12T02:00:00"), (3, "2026-09-12T01:30:00")]
    rb = classify_step_sequence(back)
    check("time running backwards inside a block is a defect",
          "block_time_disorder" in rb["defects"], str(rb["defects"]))

    # A replay whose timestamps do NOT match the block it repeats is not a
    # replay at all — it is the upstream publishing two different answers for
    # the same steps, which is the case the permissive rule must not swallow.
    conflict = synth((0, 4, 0)) + [(i, f"2026-09-13T{i:02d}:00:00") for i in range(2, 5)]
    rc = classify_step_sequence(conflict)
    check("a 'replay' that disagrees about the times is still caught",
          rc["defects"] != [], str(rc["defects"]))

    print("\n=== degenerate input ===")
    check("an empty manifest yields no defect and no blocks",
          classify_step_sequence([])["blocks"] == 0)
    one = classify_step_sequence([(7, "2026-09-12T00:00:00")])
    check("a single step is clean", one["defects"] == [] and one["blocks"] == 1)

    print("\n=== the regression this replaces ===")
    # Under the old rule every one of these warned. Under the new rule none do,
    # and that is the whole point — but assert it against the counts actually
    # observed in production rather than against the fixture alone.
    for entries, label in ((74, "74"), (129, "129"), (187, "187"), (243, "243")):
        n_replays = {74: 0, 129: 1, 187: 2, 243: 3}[entries]
        seq = synth((0, 18, 0)) + synth(*[(18, 72, 14)] * (n_replays + 1))
        rr = classify_step_sequence(seq)
        check(f"the {label}-entry shape does not warn",
              rr["defects"] == [], f"{rr['defects']} blocks={rr['blocks']}")

    print(f"\n{len(failures)} FAILED: {', '.join(failures)}" if failures
          else "\nall forecast-parity assertions passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
