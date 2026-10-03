#!/usr/bin/env python3
"""The sparkline metric contract: what the dashboard asks for, the checks emit.

Two halves have to agree, and they live in different languages:

  frontend/src/lib/format.ts   sparklineMetric() maps a check_id prefix to the
                               metric name the dashboard fetches and draws
  backend/checks/*.py          whichever check family that prefix belongs to
                               has to actually record that metric

When they disagree nothing errors. The rail asks for a series that was never
written, gets an empty array, and draws an empty cell — which is precisely the
bug v0.5.1 shipped (LB1/LB2 rows requested no series at all while their samples
sat in metric_samples), and it survived a release because nothing asserted the
two halves lined up.

Also covers helpers.headroom itself, which is the arithmetic the whole fixed-
axis sparkline rests on. Its negative-limit branch is not decoration: three of
the thirteen products publish FUTURE-dated steps and carry a negative
max_freshness_s (comp_now -2400, water_depth and max_water_depth -3600).

Run:  python3 validation_tests/test_sparkline_metric.py
"""
from __future__ import annotations
import inspect
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("SENTINEL_DB_URL", "postgresql://unused/unused")
# The backend families register only with these set; without them the contract
# below would pass vacuously by having nothing to check.
os.environ.setdefault("SENTINEL_BACKEND_ROOT", "/backend/projects/aqpi/XBand/web-files")
os.environ.setdefault("SENTINEL_SSCB_ROOT", "/sscb")

# checks first: backend.registry imports backend.checks.base, and every check
# module imports backend.registry back. Importing registry first catches it
# half-initialized.
from backend import checks as _checks                      # noqa: E402,F401  (registers)
from backend import registry                               # noqa: E402
from backend.checks.helpers import headroom                # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
failures: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if cond else 'FAIL'}  {label}" + (f"  — {detail}" if detail else ""))
    if not cond:
        failures.append(label)


def _prefixes_wanting(metric: str) -> list[str]:
    """Prefixes that format.ts maps to `metric`, read from the source."""
    src = (ROOT / "frontend/src/lib/format.ts").read_text()
    body = re.search(r"export function sparklineMetric\([^)]*\)[^{]*\{(.*?)\n\}", src, re.S)
    assert body, "sparklineMetric not found in format.ts"
    return re.findall(r"startsWith\('([^']+)'\)\)\s*return '" + metric + "'", body.group(1))


def main() -> int:
    print("headroom(), positive limits — the staleness budget:")
    for age, limit, want in [(0, 300, 1.0), (60, 300, 0.8), (150, 300, 0.5),
                             (300, 300, 0.0), (400, 300, 0.0), (1500, 1800, 1 / 6)]:
        got = headroom(age, limit)
        check(f"age={age} limit={limit} -> {want:.4f}", abs(got - want) < 5e-4, f"{got:.4f}")

    print("\nheadroom(), negative limits — the forecast lead-time budget:")
    # comp_now's measured p50 age is -3227 s against a -2400 s limit: 827 s of
    # lead beyond the minimum, i.e. 34% of the required lead still in hand.
    for age, limit, want in [(-3227, -2400, 0.3446), (-2400, -2400, 0.0),
                             (-2000, -2400, 0.0), (-4800, -2400, 1.0)]:
        got = headroom(age, limit)
        check(f"age={age} limit={limit} -> {want:.4f}", abs(got - want) < 5e-4, f"{got:.4f}")

    print("\nheadroom(), edges:")
    check("output never leaves 0..1",
          all(0.0 <= headroom(a, l) <= 1.0
              for a in (-10_000, -1, 0, 1, 10_000)
              for l in (-3600, -1, 1, 300, 90_000)))
    check("a zero limit degrades to 0 rather than dividing by zero",
          headroom(5, 0) == 0.0)
    check("monotone: more age is never more headroom",
          all(headroom(a, 300) >= headroom(a + 10, 300) for a in range(0, 400, 10)))
    check("it crosses zero exactly where the verdict flips (positive)",
          headroom(299, 300) > 0 and headroom(301, 300) == 0)
    check("...and where it flips for a negative limit",
          headroom(-2401, -2400) > 0 and headroom(-2399, -2400) == 0)

    print("\nthe contract — every family the dashboard plots records the metric:")
    prefixes = _prefixes_wanting("headroom")
    check("format.ts names at least the four monitored families",
          len(prefixes) >= 4, ", ".join(prefixes))

    # Map each registered check to the module that defines it, so this follows
    # the registry rather than a hand-written list of files — the hand-written
    # list is how the last four of these got missed.
    for prefix in prefixes:
        ids = [cid for cid in registry.CHECKS if cid.startswith(prefix)]
        check(f"{prefix}* is actually registered", bool(ids), f"{len(ids)} check(s)")
        if not ids:
            continue
        mods = {type(registry.CHECKS[cid]).__module__ for cid in ids}
        for mod in sorted(mods):
            src = inspect.getsource(sys.modules[mod])
            check(f"{prefix}* -> {mod.split('.')[-1]} records metrics['headroom']",
                  'metrics["headroom"]' in src)
            check(f"...and derives it with the shared helper, not by hand",
                  re.search(r'metrics\["headroom"\]\s*=\s*headroom\(', src) is not None)

    print("\nthe metric is recorded against the same threshold the verdict uses:")
    lb2 = inspect.getsource(sys.modules["backend.checks.layer2_backend_radar"])
    check("LB2 divides by the silent limit it bands on",
          re.search(r'metrics\["headroom"\] = headroom\(age_s, silent_s\)', lb2) is not None)
    l2 = inspect.getsource(sys.modules["backend.checks.layer2_radar"])
    check("L2 divides by the same per-radar silent_fail_s",
          re.search(r'metrics\["headroom"\] = headroom\(_age, silent_fail_s\)', l2) is not None)
    # This is what makes the two columns of the paired rail comparable at all:
    # a divergence between the traces is the sources disagreeing, not the
    # scales disagreeing.
    from backend.config import RADAR_SILENT_FAIL_S
    check("...and that table is the one LB2 defaults from",
          "RADAR_SILENT_FAIL_S" in lb2 and bool(RADAR_SILENT_FAIL_S))

    print()
    if failures:
        print(f"{len(failures)} FAILED: {', '.join(failures)}")
        return 1
    print("all sparkline-metric assertions passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
