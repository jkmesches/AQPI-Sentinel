#!/usr/bin/env python3
"""The fleet checks must not assert health from stale verdicts.

Both fleet correlation checks — radarca-side (`layer2.xband.fleet`) and
backend-side (`layer2.backend.fleet`) — compute "how many radars are out" from
verdicts the per-radar checks publish into a module-level cache. Each had the
same defect, one expression apart:

  * the NUMERATOR (`_not_reporting_xband`) was TTL-gated;
  * the DENOMINATOR (`known`) counted raw membership in the cache.

A verdict stays in that cache forever once published. So if the per-radar
checks stop publishing, the numerator empties while the denominator stays
full, the skip floor does not trigger, and the check reports `pass`.

=== Why that is reachable in production, not hypothetical ===

`_publish_verdict` has exactly ONE call site, at the end of the per-radar
`run()`. The early return taken when radarca's `/api/radar-status/` is
unavailable is the only return before it, and it never reaches it. So while
the origin is unreachable, every per-radar check publishes nothing, every
cycle — and about five minutes later the operator sees five red radar rows
above a GREEN fleet row asserting nothing is systemic. That is inverted during
exactly the incident the check was built to characterise, and a single
unreachable HTTP origin is the most likely incident shape there is.

An earlier account blamed the L0 cascade — a failing L0 demotes the radar
checks, so they stop publishing. That mechanism does NOT work and is recorded
here so it is not re-derived: the scheduler runs a check and demotes its status
afterwards (see scheduler.py, `_latest_status`), so a demoted check has already
published a fresh verdict. Demotion cannot produce staleness.

Run:  python3 validation_tests/test_fleet_staleness.py
"""
from __future__ import annotations
import asyncio
import os
import re
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("SENTINEL_DB_URL", "postgresql://unused/unused")
os.environ.setdefault("SENTINEL_BACKEND_ROOT", "/backend/projects/aqpi/XBand/web-files")
os.environ.setdefault("SENTINEL_SSCB_ROOT", "/sscb")

from backend.checks import layer2_radar as l2                  # noqa: E402
from backend.checks import layer2_backend_radar as lb2         # noqa: E402
from backend.checks.base import utcnow                         # noqa: E402

failures: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if cond else 'FAIL'}  {label}" + (f"  — {detail}" if detail else ""))
    if not cond:
        failures.append(label)


def main() -> int:
    print("the production scenario: radarca unreachable, so nothing publishes")
    # Healthy cycle first — the checks ran and published, as they do normally.
    l2._reset_fleet_state()
    healthy = utcnow()
    for rid in sorted(l2.XBAND_FLEET):
        l2._publish_verdict(rid, "HEALTHY", healthy)
    r = asyncio.run(l2.Layer2XbandFleet().run(None))
    check("a healthy fleet reports pass", r.status == "pass",
          f"{r.status}: {r.summary}")

    # Now radarca goes away. Every per-radar check takes the early return and
    # publishes NOTHING, so the cache simply stops advancing. Reproduced by
    # ageing what is already there rather than by clearing it, because the
    # cache retaining pre-outage verdicts is the whole defect.
    stale = healthy - timedelta(seconds=l2._VERDICT_TTL_S + 60)
    for rid in sorted(l2.XBAND_FLEET):
        l2._last_verdict[rid] = (stale, "HEALTHY")
    r = asyncio.run(l2.Layer2XbandFleet().run(None))
    check("once every verdict is past the TTL, the fleet reports SKIP",
          r.status == "skip", f"{r.status}: {r.summary}")
    check("...not pass — which is what it did before 2026-10-05",
          r.status != "pass",
          "five red radar rows above a green fleet row")
    check("...and says it is awaiting verdicts, with a reason",
          r.payload.get("reason") == "insufficient_data", str(r.payload))

    print("\n...and the same scenario where the radars were actually out:")
    # The dangerous direction. Pre-outage verdicts said GHOST_UP, so a raw
    # membership count would read 5 known / 0 reporting and call it pass.
    l2._reset_fleet_state()
    for rid in sorted(l2.XBAND_FLEET):
        l2._last_verdict[rid] = (stale, "GHOST_UP")
    r = asyncio.run(l2.Layer2XbandFleet().run(None))
    check("stale GHOST_UP verdicts also yield skip, not pass",
          r.status == "skip", f"{r.status}: {r.summary}")

    print("\na partially stale fleet is judged on the fresh radars only:")
    l2._reset_fleet_state()
    now = utcnow()
    for rid in ("XEBY", "XSCR", "XSCV"):
        l2._publish_verdict(rid, "GHOST_UP", now)
    for rid in ("XSCW", "XSWR"):
        l2._last_verdict[rid] = (stale, "HEALTHY")
    r = asyncio.run(l2.Layer2XbandFleet().run(None))
    check("3 fresh GHOST_UP out of 5 is still systemic", r.status == "fail",
          f"{r.status}: {r.summary}")
    check("...and the stale two are not counted as healthy",
          sorted(r.payload["verdicts"]) == ["XEBY", "XSCR", "XSCV"],
          str(sorted(r.payload["verdicts"])))

    print("\nthe BACKEND fleet check has the same guarantee:")
    lb2._reset_lb_fleet_state()
    for rid in sorted(lb2.LB_FLEET):
        lb2._lb_publish_verdict(rid, "ARRIVING", healthy)
    r = asyncio.run(lb2.Layer2BackendFleetCheck().run(None))
    check("healthy -> pass", r.status == "pass", f"{r.status}: {r.summary}")
    lb_stale = healthy - timedelta(seconds=lb2._LB_VERDICT_TTL_S + 60)
    for rid in sorted(lb2.LB_FLEET):
        lb2._lb_last_verdict[rid] = (lb_stale, "ARRIVING")
    r = asyncio.run(lb2.Layer2BackendFleetCheck().run(None))
    check("all stale -> skip", r.status == "skip", f"{r.status}: {r.summary}")

    print("\nthe witness licenses a MOUNT claim, not a freshness claim:")
    # The first version branched on "is the witness verdict systemic", which
    # conflated two facts and got both edges wrong. What proves the host is up,
    # NFS is serving and our reads work is that the independent tree COULD BE
    # READ -- not that the data on it was fresh.
    def scope_with(witness: str | None, n_out: int = 3) -> tuple[str, str]:
        lb2._reset_lb_fleet_state()
        t = utcnow()
        out = sorted(lb2.LB_FLEET)
        for i, rid in enumerate(out):
            lb2._lb_publish_verdict(rid, "SILENT" if i < n_out else "ARRIVING", t)
        if witness is not None:
            lb2._lb_publish_verdict(lb2.LB_FLEET_WITNESS, witness, t)
        r = asyncio.run(lb2.Layer2BackendFleetCheck().run(None))
        return r.payload["scope"], r.summary

    sc, summ = scope_with("ARRIVING")
    check("an ARRIVING witness localises to the X-band path", sc == "xband-path", sc)
    check("...and that is the only case claiming host/NFS/clock are fine",
          "host, NFS and clock are fine" in summ, summ)

    sc, summ = scope_with("UNREADABLE")
    check("an UNREADABLE witness means the fault is wider",
          sc == "wider-than-xband", sc)
    check("...stated as a MOUNT failure, which is what was observed",
          "could not be read" in summ, summ)

    # CBAND failing a freshness threshold while its tree reads perfectly is not
    # an infrastructure fault. It happened for real on 2026-10-05: the LB2
    # basis change pushed CBAND past a stored 300 s limit while every cycle
    # read its mount without trouble.
    sc, summ = scope_with("SILENT")
    check("a SILENT witness still confirms the mount is healthy",
          "mount read" in summ and "healthy" in summ, summ)
    check("...so it is not reported as a mount-level fault",
          "could not be read" not in summ, summ)

    # CONFIG_ERROR tells us nothing about the host at all. It used to fall
    # through to the ARRIVING branch and assert the infrastructure was fine.
    sc, summ = scope_with("CONFIG_ERROR")
    check("a CONFIG_ERROR witness localises NOTHING", sc == "unlocalised", sc)
    check("...and must never claim the infrastructure is fine",
          "host, NFS and clock are fine" not in summ, summ)

    sc, _ = scope_with(None)
    check("an absent witness localises nothing either", sc == "unlocalised", sc)

    # The fact the mount claim rests on, separable from the freshness verdict.
    lb2._reset_lb_fleet_state()
    t = utcnow()
    for rid in sorted(lb2.LB_FLEET):
        lb2._lb_publish_verdict(rid, "SILENT", t)
    for w, expect in (("ARRIVING", True), ("SILENT", True),
                      ("UNREADABLE", False), ("CONFIG_ERROR", False)):
        lb2._lb_publish_verdict(lb2.LB_FLEET_WITNESS, w, utcnow())
        r = asyncio.run(lb2.Layer2BackendFleetCheck().run(None))
        check(f"witness {w} -> mount_readable={expect}",
              r.payload["witness_mount_readable"] is expect,
              str(r.payload["witness_mount_readable"]))

    print("\nthe mechanism that makes staleness reachable, pinned in the source:")
    src = Path(l2.__file__).read_text()
    pub_sites = [m.start() for m in re.finditer(r"^\s+_publish_verdict\(", src, re.M)]
    check("_publish_verdict has exactly one call site", len(pub_sites) == 1,
          f"{len(pub_sites)} call sites")
    # The early return for an unavailable radar-status endpoint sits ABOVE it,
    # which is why a verdict is never published during that outage. If someone
    # later moves the publish earlier, this assertion is the thing that notices.
    run_at = src.index("    async def run(self, ctx) -> CheckResult:\n        t0 = utcnow()\n        declared, declared_err = await self._declared(ctx)")
    early = src.index("radar-status unavailable", run_at)
    check("...and the radar-status early return precedes it",
          pub_sites and early < pub_sites[0],
          "so nothing is published while the origin is unreachable")
    check("both fleet checks gate the denominator by the same TTL as the numerator",
          "_fresh_verdicts(now)" in src
          and "_lb_fresh(now)" in Path(lb2.__file__).read_text(),
          "raw cache membership is what reported pass from stale evidence")

    print("\nsuppression is unaffected — a skip cannot silence a radar alarm:")
    # The per-radar checks list the fleet in alarm_only_depends_on, and
    # compute_suppression keys on fail/error. A fleet that now reports skip
    # where it used to report pass therefore suppresses nothing it did not
    # suppress before, so this fix cannot accidentally mute five radars.
    from backend.alarms.suppression import compute_suppression
    idx = {"layer2.radar.XEBY": [l2.FLEET_CHECK_ID]}
    for st in ("skip", "pass"):
        got = compute_suppression("layer2.radar.XEBY", {l2.FLEET_CHECK_ID: st}, idx)
        check(f"a fleet reporting {st!r} suppresses nothing", got is None, str(got))
    got = compute_suppression("layer2.radar.XEBY",
                              {l2.FLEET_CHECK_ID: "fail"}, idx)
    check("...while a failing fleet still does", got == l2.FLEET_CHECK_ID, str(got))

    print(f"\n{len(failures)} FAILED: {', '.join(failures)}" if failures
          else "\nall fleet-staleness assertions passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
