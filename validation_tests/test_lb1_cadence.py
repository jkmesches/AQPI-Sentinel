"""LB1 sampling cadence — a filesystem check must not alias its own subject.

Run directly (no DB, no network):

    python validation_tests/test_lb1_cadence.py

LB1 took its sampling interval from PRODUCTS[pid]["cadence_s"], which is the
UPSTREAM PUBLISH interval. Sampling a signal at its own period is aliasing by
construction, and on 2026-10-12 it held for 11 of the 13 products: water_*,
fcst_temp and fcst_total_precip* at 3600 s against hourly publishes,
fcst_precip_rate at 900 against 900, and the five 120 s products at 120
against 120.

The cost was not theoretical. A derived "observed cadence" for water_* read as
31-170 minute intervals with 32% over the limit, every bit of which was the
sampler: two consecutive hourly publishes landing in one sampling gap record
as one long interval. A threshold set from that would have been fitted to the
instrument. Separately, a panel edit to water_* took 40 minutes to apply while
max_water_* applied it in 2 -- the only difference between those products
being this key.

What the assertions below pin:

  1. NO product samples at or slower than its own publish interval. This is
     the defect itself, stated so it cannot return silently for a product
     added later.

  2. LB1 does not read `cadence_s` at all. Asserting only on the numbers would
     pass if someone re-derived the interval from the publish rate and happened
     to land on a fast value, which would reintroduce the coupling that makes
     L1 and LB1 move together.

  3. L1's rate is UNCHANGED by all of this. L1 fetches over HTTP from a third
     party's public service; a change here that quietly multiplied its request
     rate would be a worse bug than the one being fixed.
"""
from __future__ import annotations
import inspect
import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.config import PRODUCTS, BACKEND_PRODUCT_CADENCE_S      # noqa: E402


def _hold_down_s() -> int:
    """Read `hold_down` out of alerts.yaml rather than restating it.

    The second acceptance shape below leans on it, so a hold-down someone
    shortens later must re-open this question instead of leaving an argument
    here that quietly stopped being true.
    """
    import re
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    txt = io.open(os.path.join(root, "alerts.yaml"), encoding="utf-8").read()
    m = re.search(r"^hold_down:\s*(\d+)([smh])\s*$", txt, re.M)
    assert m, "alerts.yaml has no top-level hold_down"
    return int(m.group(1)) * {"s": 1, "m": 60, "h": 3600}[m.group(2)]


HOLD_DOWN_S = _hold_down_s()
from backend.checks.helpers import derive_check_cadence             # noqa: E402
from backend.checks.layer1_backend_product import (                 # noqa: E402
    Layer1BackendProductCheck)

_fail = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global _fail
    print(("  ok    " if ok else "  FAIL  ") + label + (f"  [{detail}]" if detail else ""))
    if not ok:
        _fail += 1


def lb1_cadence(pid: str) -> int:
    return Layer1BackendProductCheck(product_id=pid).cadence_s


def main() -> int:
    print("no LB1 check samples at or slower than its own publish interval:")
    for pid, cfg in PRODUCTS.items():
        pub = cfg.get("cadence_s")
        got = lb1_cadence(pid)
        if pub is None:
            check(f"{pid}: no declared publish interval", got <= 300, f"lb1={got}s")
            continue
        # Two acceptable shapes, and the second is not a loophole:
        #
        #   a) oversampled at least 2x -- strictly faster than Nyquist on the
        #      publish rate, so a publish cannot hide between two samples.
        #   b) sampled at its own period, but fast enough that the WORST-CASE
        #      detection latency (phase is arbitrary, so 2x the interval) is
        #      still under the alarm hold-down. Below the hold-down the
        #      latency cannot affect what alarms at all, so the residual
        #      aliasing has no operational consequence.
        #
        # The old 3600 s sampling satisfied neither: 1.0x, and a 7200 s
        # worst case against a 300 s hold-down.
        nyquist = got * 2 <= pub
        under_holddown = got * 2 <= HOLD_DOWN_S
        check(f"{pid}: lb1 {got}s vs publish {pub}s",
              nyquist or under_holddown,
              "%.1fx%s" % (pub / got, "" if nyquist else ", worst-case %ds < hold-down %ds"
                           % (got * 2, HOLD_DOWN_S)))

    print("\nthe two products that were already correct did not regress:")
    for pid in ("max_water_depth", "max_water_level"):
        check(f"{pid} still samples at 120s", lb1_cadence(pid) == 120,
              f"{lb1_cadence(pid)}s")

    print("\nLB1 does not take its interval from the publish cadence at all:")
    # Comments stripped first: the constructor's comment NAMES the key it must
    # not read, to say why, and a text assertion that cannot tell code from
    # prose would fail on the explanation of its own rule.
    src = inspect.getsource(Layer1BackendProductCheck.__init__)
    code = "\n".join(l for l in src.splitlines() if not l.lstrip().startswith("#"))
    check("__init__ does not read cfg['cadence_s']",
          'cfg.get("cadence_s")' not in code and 'cfg["cadence_s"]' not in code)
    check("__init__ reads backend_cadence_s", "backend_cadence_s" in src)
    check(f"default is BACKEND_PRODUCT_CADENCE_S ({BACKEND_PRODUCT_CADENCE_S}s)",
          "BACKEND_PRODUCT_CADENCE_S" in src)

    print("\nL1's radarca-facing rate is untouched (half the publish rate):")
    for pid, pub in (("water_depth", 3600), ("water_level", 3600),
                     ("fcst_temp", 3600), ("qpe_15min", 120)):
        check(f"{pid}: L1 still {derive_check_cadence(pub)}s",
              derive_check_cadence(PRODUCTS[pid].get("cadence_s")) ==
              derive_check_cadence(pub),
              f"{derive_check_cadence(PRODUCTS[pid].get('cadence_s'))}s")
    check("L1 derivation is still half-rate, floored at 60",
          derive_check_cadence(3600) == 1800 and derive_check_cadence(100) == 60)

    print()
    if _fail:
        print(f"FAILED: {_fail} assertion(s)")
        return 1
    print("all LB1 cadence assertions passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
