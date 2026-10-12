"""Upstream slow-episode correlation — is this one event or fifteen?

radarca does not fail to respond. Measured 2026-08-31 across 112 probes of
``/api/radar-status/``, every single one returned HTTP 200. What it does is
answer *slowly*: steady-state p90 on that endpoint has been 8-13s all week and
is drifting up, and on top of that baseline it has slow episodes every half
hour or so that push the tail to 26-42s. Anything of ours still waiting when
the episode hits raises ``httpx.ReadTimeout``.

Those timeouts arrive in bursts, because the episode hits every in-flight
request at once rather than picking on one check. Over the 7 days to
2026-08-31 there were 268 read-timeout events, and 153 of them (57%) fell
inside just 20 minutes where 4 or more distinct checks timed out together —
one burst covered 15 checks in a single minute. Treated independently that is
fifteen pages describing one event on someone else's server.

This check is the single point of blame for those. It is the same shape as
``layer2.xband.fleet``: it re-reads what the other checks already observed
rather than probing upstream again, so it agrees with them by construction and
adds no load to the endpoint whose slowness is the whole problem.

What it deliberately does NOT do is hide anything. Every check keeps its own
true verdict and its own timeline cell — a read timeout is a real gap in our
visibility and the grid must keep saying so. The only thing that collapses is
the alarm: affected checks list this one in ``alarm_only_depends_on``, so
their alarms are still opened but marked ``suppressed_by``, and the operator
gets one page naming the real scope instead of fifteen saying the same thing.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import httpx

from ..config import HAS_RADARCA
from ..stages import stage_tier
from ..registry import register
from .base import Check, CheckResult, utcnow

EPISODE_CHECK_ID = "layer0.origin.episode"

# How far back to look when deciding whether timeouts are correlated.
#
# Sized from the data, not chosen round: the bursts observed were confined to
# a single minute, but our checks run on cadences from 60s to 300s, so members
# of one episode can be recorded up to a few minutes apart simply because they
# were not all due at the same instant. Three minutes covers that spread
# without reaching so far back that two genuinely separate episodes merge.
EPISODE_WINDOW_S = 180

# How many distinct checks must time out inside the window before we call it
# an episode rather than coincidence.
#
# 4 matches FLEET_SYSTEMIC_MIN in layer2_radar for the same reason: at 3 and
# below the 7-day data is dominated by isolated timeouts (114 of the 134
# affected minutes had 1-3 checks and are genuinely independent), while every
# occurrence at 4+ was a burst. Lowering this would start collapsing unrelated
# failures into a shared excuse, which is the one way this check could lie.
EPISODE_MIN_CHECKS = 4

# Checks that never talk to radarca, and so can never be part of an upstream
# episode. Their failures are ours, and must never be excused by one.
#
# This list covers the L0 self-and-network probes. The OTHER population that
# never talks to radarca is the whole backend tier, and it is excluded by
# stages.stage_tier() in attach_episode_suppression rather than by being spelled
# out here — one derivation from the stage each check already declares, instead
# of a prefix list that has to be edited every time a backend check family is
# added. The LB3 work added two such families in a week.
EXEMPT_PREFIXES = ("layer0.net.", "layer0.self.", EPISODE_CHECK_ID)

# check_id -> when it most recently hit an upstream read timeout. In-process
# and deliberately not persisted: this answers "is an episode happening right
# now", and a fresh process has no opinion until it observes one.
_timeouts: dict[str, datetime] = {}


def record_upstream_timeout(check_id: str, when: datetime | None = None) -> None:
    """Called by the scheduler when a check dies of a read timeout."""
    _timeouts[check_id] = when or utcnow()


def note_upstream_exception(check_id: str, exc: BaseException,
                            when: datetime | None = None) -> bool:
    """Record `exc` if it is an upstream read timeout. Returns whether it was.

    For checks that catch transport errors themselves instead of letting them
    reach the scheduler. Without this the episode detector silently
    undercounts: on the 2026-08-31 deploy it saw 10 of 14 concurrent timeouts,
    because the four product checks handle their own image-fetch failures.
    """
    if not isinstance(exc, httpx.ReadTimeout):
        return False
    record_upstream_timeout(check_id, when)
    return True


def episode_members(now: datetime | None = None) -> list[str]:
    """Checks that timed out inside the correlation window, most recent first."""
    now = now or utcnow()
    cutoff = now - timedelta(seconds=EPISODE_WINDOW_S)
    return sorted(cid for cid, ts in _timeouts.items() if ts >= cutoff)


def in_episode(now: datetime | None = None) -> bool:
    """True when enough checks have timed out together to blame upstream."""
    return len(episode_members(now)) >= EPISODE_MIN_CHECKS


def attach_episode_suppression(checks) -> int:
    """Point every upstream-facing check's alarm suppression at this one.

    Done centrally rather than by editing twenty check classes: membership is
    a property of "does it call radarca". Returns the number wired, so a caller
    (and the test suite) can assert it is not silently zero.

    === The backend tier is exempt, and was not always ===

    This function's premise — "everything except the exempt prefixes calls
    radarca" — was true when it was written and stopped being true when LB1
    landed. Until 2026-10-12 every `LB*` check was quietly given
    `alarm_only_depends_on = [layer0.origin.episode]`, so a radarca slow
    episode silenced alarms from checks that read K2 and Trinity and owe the
    web tier nothing.

    That is precisely backwards. The backend tier exists to be the independent
    witness when radarca is misbehaving, and where the two disagree the
    disagreement IS the signal — so a radarca episode is the moment those
    alarms matter most, and it was the moment they went quiet. It also slipped
    past the test that was meant to catch it: test_backend_checks asserts
    `depends_on == []`, which stayed true, while this function wired the
    alarm-only form behind it.

    Keyed on stages.stage_tier() rather than on a prefix list, so a new backend
    check family is exempt by virtue of declaring an LB stage instead of by
    somebody remembering to add it here.
    """
    n = 0
    for c in checks:
        if c.id.startswith(EXEMPT_PREFIXES):
            continue
        if stage_tier(getattr(c, "stage", "")) == "backend":
            continue
        deps = list(getattr(c, "alarm_only_depends_on", None) or [])
        if EPISODE_CHECK_ID not in deps:
            deps.append(EPISODE_CHECK_ID)
            # Per-instance, never on the class: the base class attribute is a
            # shared mutable default and appending to it would wire every
            # check ever defined, exempt ones included.
            c.alarm_only_depends_on = deps
        n += 1
    return n


class Layer0OriginEpisode(Check):
    """Fails while an upstream slow episode is in progress."""

    id         = EPISODE_CHECK_ID
    target     = "origin-episode"
    stage      = "L0"
    cadence_s  = 60
    # No depends_on: this must stay observable even when origin.alive is the
    # check that timed out. It is the explanation for that timeout, so being
    # demoted to skip by it would erase the explanation exactly when needed.

    async def run(self, ctx) -> CheckResult:
        t0 = utcnow()
        members = episode_members(t0)
        n = len(members)
        systemic = n >= EPISODE_MIN_CHECKS
        return CheckResult(
            check_id=self.id, target=self.target, stage=self.stage,
            status=("fail" if systemic else "pass"),
            started_at=t0, finished_at=utcnow(),
            summary=(
                f"UPSTREAM SLOW EPISODE: {n} checks timed out within "
                f"{EPISODE_WINDOW_S}s ({', '.join(members)}) — one upstream "
                f"event, not {n} independent failures"
                if systemic else
                f"{n} check(s) timed out in the last {EPISODE_WINDOW_S}s"
                if n else "no upstream read timeouts"
            ),
            payload={
                "members": members, "n": n, "min_checks": EPISODE_MIN_CHECKS,
                "window_s": EPISODE_WINDOW_S, "systemic": systemic,
                # Marks the cell as a visibility gap rather than evidence the
                # monitored thing is broken — same vocabulary layer2 uses.
                "reason": "upstream_api" if systemic else None,
            },
            metrics={"timed_out_checks": float(n)},
        )


# Correlates slow episodes on the radarca origin. With no HTTP origin there is
# nothing to correlate and the check would fail forever — but the module still
# has to import, because attach_episode_suppression lives here and the registry
# wiring calls it on every profile. So gate the registration, not the import.
if HAS_RADARCA:
    register(Layer0OriginEpisode())
