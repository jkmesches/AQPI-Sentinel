"""Upstream slow-episode correlation, and the read-timeout retry exemption.

Pure unit tests: no network, no database. Run directly:

    python validation_tests/test_upstream_episode.py

Both mechanisms come from the 2026-08-31 investigation into "a lot of Server
took too long to respond". The finding that drove them: radarca does not fail
to respond. 112 live probes of /api/radar-status/ all returned HTTP 200. It
answers slowly — steady-state p90 8-13s and rising, with episodes pushing the
tail to 26-42s — and anything of ours still waiting when an episode lands
raises httpx.ReadTimeout.

1. Read timeouts must not be retried. Upstream has already spent a full
   timeout of work on us; the retry lands while it is still saturated, and
   measured counters said it rescues about 1 in 9 while doubling our wall cost
   per cycle. The dangerous edge is over-correcting: connect failures cost
   upstream nothing and must STAY retryable, so the negative cases below carry
   more weight than the positive one.

2. Episode correlation. 153 of 268 read timeouts over 7 days landed in 20
   minutes where 4+ distinct checks timed out together, one burst covering 15
   checks in a single minute. The check collapses those into one page.

The failure mode this whole file guards against is an excuse that spreads too
far: a correlation check that suppresses real independent failures is strictly
worse than no correlation check, because it makes the grid lie in the
reassuring direction. Hence the negative assertions — isolated failures must
still page, self/network checks must never be excusable by an upstream event,
and every check must keep its own true verdict and timeline cell.
"""
from __future__ import annotations
import asyncio
import os
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("SENTINEL_DB_URL", "postgresql://unused/unused")

import httpx                                              # noqa: E402
import backend.checks as _all_checks                      # noqa: E402,F401  (import order)
import backend.checks.layer0_episode as EP                # noqa: E402
from backend.checks.base import Check, utcnow             # noqa: E402
from backend.registry import CHECKS                       # noqa: E402
from backend.checks.transports.http import HttpClient     # noqa: E402
from backend.alarms.suppression import (                  # noqa: E402
    build_depends_on_index, compute_suppression,
)

failures: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if cond else 'FAIL'}  {label}" + (f"  — {detail}" if detail else ""))
    if not cond:
        failures.append(label)


def _mock(client: HttpClient, handler) -> HttpClient:
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return client


async def test_read_timeout_not_retried() -> None:
    print("\n[1] read timeouts are not retried; other failures still are")

    calls = {"n": 0}

    def read_timeout(_req):
        calls["n"] += 1
        raise httpx.ReadTimeout("upstream went quiet")

    c = _mock(HttpClient(), read_timeout)
    try:
        await c.get("https://x/api/radar-status/")
    except httpx.ReadTimeout:
        pass
    check("ReadTimeout attempts the request exactly once", calls["n"] == 1, f"{calls['n']} attempts")
    check("ReadTimeout is not counted as a retry", c.retries_attempted == 0, str(c.retries_attempted))
    check("ReadTimeout increments its own counter", c.read_timeouts == 1, str(c.read_timeouts))

    # Negative case, and the one that matters most: a connect failure costs
    # upstream nothing, so exempting it too would delete the retry outright.
    calls["n"] = 0

    def connect_err(_req):
        calls["n"] += 1
        raise httpx.ConnectTimeout("no SYN-ACK")

    c2 = _mock(HttpClient(), connect_err)
    try:
        await c2.get("https://x/")
    except httpx.ConnectTimeout:
        pass
    check("ConnectTimeout is still retried", calls["n"] == 2, f"{calls['n']} attempts")
    check("ConnectTimeout still counts as a retry", c2.retries_attempted == 1)
    check("ConnectTimeout does not touch the read-timeout counter", c2.read_timeouts == 0)

    # 5xx must stay retryable too — it was measured to be transient upstream.
    calls["n"] = 0

    def five_oh_three(_req):
        calls["n"] += 1
        return httpx.Response(503)

    c3 = _mock(HttpClient(), five_oh_three)
    await c3.get("https://x/")
    check("503 is still retried", calls["n"] == 2, f"{calls['n']} attempts")

    # A retry that eventually works must still be counted as a save.
    seq = {"n": 0}

    def flaky(_req):
        seq["n"] += 1
        return httpx.Response(503 if seq["n"] == 1 else 200)

    c4 = _mock(HttpClient(), flaky)
    r = await c4.get("https://x/")
    check("a recovered 503 counts as a successful retry",
          r.status_code == 200 and c4.retries_succeeded == 1)


async def test_episode_detection() -> None:
    print("\n[2] episode detection: correlated vs isolated")

    now = utcnow()
    EP._timeouts.clear()

    check("no timeouts is not an episode", not EP.in_episode(now))

    # Isolated failures are the common case (114 of 134 affected minutes had
    # 1-3 checks) and must never be excused.
    for i in range(EP.EPISODE_MIN_CHECKS - 1):
        EP.record_upstream_timeout(f"layer1.product.p{i}", now)
    check(f"{EP.EPISODE_MIN_CHECKS - 1} checks is below the bar, not an episode",
          not EP.in_episode(now), str(EP.episode_members(now)))

    EP.record_upstream_timeout("layer1.product.p9", now)
    check(f"{EP.EPISODE_MIN_CHECKS} checks together IS an episode", EP.in_episode(now))

    # The same check timing out repeatedly is one check, not a crowd — a naive
    # counter keyed on events rather than check ids would call this an episode.
    EP._timeouts.clear()
    for _ in range(10):
        EP.record_upstream_timeout("layer1.product.same", now)
    check("one check timing out 10 times is not an episode",
          not EP.in_episode(now), str(EP.episode_members(now)))

    # Window expiry: stale timeouts must not accumulate into a permanent
    # episode, or the check latches on and never clears.
    EP._timeouts.clear()
    old = now - timedelta(seconds=EP.EPISODE_WINDOW_S + 30)
    for i in range(EP.EPISODE_MIN_CHECKS + 2):
        EP.record_upstream_timeout(f"layer1.product.q{i}", old)
    check("timeouts older than the window do not count",
          not EP.in_episode(now), str(EP.episode_members(now)))
    check("...and are excluded from the member list", EP.episode_members(now) == [])


async def test_episode_check_result() -> None:
    print("\n[3] the check's own verdict")

    now = utcnow()
    chk = CHECKS[EP.EPISODE_CHECK_ID]

    EP._timeouts.clear()
    r = await chk.run(None)
    check("passes when nothing is timing out", r.status == "pass", r.status)
    check("...and does not claim a visibility gap", r.payload.get("reason") is None)

    for i in range(EP.EPISODE_MIN_CHECKS):
        EP.record_upstream_timeout(f"layer1.product.r{i}", now)
    r = await chk.run(None)
    check("fails during an episode", r.status == "fail", r.status)
    check("names the scope in the summary",
          "one upstream event" in r.summary and str(EP.EPISODE_MIN_CHECKS) in r.summary,
          r.summary[:90])
    check("tags the cell as a visibility gap, not a broken thing",
          r.payload.get("reason") == "upstream_api")
    check("reports the members", len(r.payload["members"]) == EP.EPISODE_MIN_CHECKS)
    check("emits a metric for trending", r.metrics.get("timed_out_checks") == float(EP.EPISODE_MIN_CHECKS))

    # It explains origin.alive's timeout, so being demoted to skip by
    # origin.alive would erase the explanation exactly when it is needed.
    check("has no depends_on that could demote it", not chk.depends_on, str(chk.depends_on))
    EP._timeouts.clear()


async def test_wiring_and_suppression() -> None:
    print("\n[4] suppression wiring — reach, and limits on reach")

    wired = [c.id for c in CHECKS.values()
             if EP.EPISODE_CHECK_ID in (c.alarm_only_depends_on or [])]
    unwired = [c.id for c in CHECKS.values()
               if EP.EPISODE_CHECK_ID not in (c.alarm_only_depends_on or [])]

    check("upstream-facing checks are wired", len(wired) > 20, f"{len(wired)} wired")
    check("every unwired check is explicitly exempt",
          all(c.startswith(EP.EXEMPT_PREFIXES) for c in unwired), str(unwired))
    for must_be_exempt in ("layer0.net.internet", "layer0.net.dns", "layer0.self.disk"):
        check(f"{must_be_exempt} can never be excused by upstream",
              must_be_exempt not in wired)
    check("the episode check does not suppress itself",
          EP.EPISODE_CHECK_ID not in wired)

    # The base class attribute is a shared mutable default; appending to it
    # instead of rebinding per instance would wire every check ever defined,
    # exempt ones included, and the test above would still pass.
    check("Check.alarm_only_depends_on class default was not mutated",
          EP.EPISODE_CHECK_ID not in Check.alarm_only_depends_on,
          str(Check.alarm_only_depends_on))

    # Wiring twice must not duplicate — __init__ runs once, but a reload or a
    # test importing the module again should stay idempotent.
    before = dict((c.id, list(c.alarm_only_depends_on or [])) for c in CHECKS.values())
    EP.attach_episode_suppression(CHECKS.values())
    after = dict((c.id, list(c.alarm_only_depends_on or [])) for c in CHECKS.values())
    check("re-wiring is idempotent", before == after)

    # Existing correlations must survive: X-band radars already point at the
    # fleet check, and must now carry both suppressors, not have one replaced.
    xband = CHECKS.get("layer2.radar.XSCV")
    if xband is not None:
        deps = xband.alarm_only_depends_on or []
        check("X-band keeps its fleet correlation alongside the episode one",
              "layer2.xband.fleet" in deps and EP.EPISODE_CHECK_ID in deps, str(deps))

    # End to end through the real suppression graph.
    idx = build_depends_on_index(CHECKS.values(), include_alarm_only=True)
    latest = {cid: "pass" for cid in CHECKS}
    latest[EP.EPISODE_CHECK_ID] = "fail"
    victim = "layer1.product.qpe_1hr"
    if victim in CHECKS:
        got = compute_suppression(victim, latest, idx)
        check("a product alarm is suppressed while an episode is failing",
              got == EP.EPISODE_CHECK_ID, str(got))

    # And the negative: with no episode, an isolated failure still pages.
    latest[EP.EPISODE_CHECK_ID] = "pass"
    if victim in CHECKS:
        got = compute_suppression(victim, latest, idx)
        check("an isolated failure is NOT suppressed when there is no episode",
              got is None, str(got))

    # The scheduler must not be able to demote a check to `skip` via this
    # dependency — that would replace a true "we could not measure" with a
    # false "we did not look".
    sched_idx = build_depends_on_index(CHECKS.values(), include_alarm_only=False)
    leaks = [cid for cid, deps in sched_idx.items() if EP.EPISODE_CHECK_ID in deps]
    check("episode dep never reaches the scheduler's demotion graph",
          not leaks, str(leaks[:4]))


async def main() -> int:
    await test_read_timeout_not_retried()
    await test_episode_detection()
    await test_episode_check_result()
    await test_wiring_and_suppression()
    print(f"\n{len(failures)} FAILED: {', '.join(failures)}" if failures
          else "\nall upstream-episode assertions passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
