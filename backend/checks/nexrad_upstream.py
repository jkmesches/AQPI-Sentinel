"""Turn a NEXRAD check's fail into a warn when NOAA vouches for the gap.

The rule and its reasons are in backend/nexrad_notices.py. This applies it to
a finished CheckResult, so each LB3 check computes its own verdict exactly as
before and passes it through here last. A check on a radar outside
config.NEXRAD_RADARS comes back unchanged.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any

from .. import nexrad_notices as nn  # noqa: TID252  the package imports relatively throughout
from ..config import NEXRAD_RADARS  # noqa: TID252  the package imports relatively throughout
from .base import CheckResult, utcnow

log = logging.getLogger(__name__)


def _when(t: datetime | None, now: datetime) -> str:
    """HH:MMZ, with the date in front when it is not the check's own day."""
    if t is None:
        return "never"
    return t.strftime("%H:%MZ" if t.date() == now.date() else "%m-%d %H:%MZ")


def _excused_summary(site: str, opinion: nn.Opinion, notice: dict[str, Any] | None,
                     ours: str, now: datetime) -> str:
    """The warn's summary: what NOAA shows, the notice if any, then ours."""
    if opinion.verdict == "returned" and opinion.returned_at is not None:
        until = opinion.returned_at + timedelta(seconds=nn.RECOVERY_HOLD_S)
        head = (f"radar back upstream: NOAA's archive has {site} again from "
                f"{_when(opinion.returned_at, now)}, so this holds at warn until "
                f"{_when(until, now)} while our feed catches up")
    elif opinion.newest is None:
        head = (f"upstream outage: NOAA's archive has no {site} volume in the "
                f"last {nn.LISTING_SPAN_S // 60} min either")
    else:
        head = (f"upstream outage: NOAA's archive has no {site} volume since "
                f"{_when(opinion.newest, now)} either")
    said = (f" NWS {_when(notice['issued'], now)}: {str(notice['text'])[:160]}"
            if notice else "")
    return f"{head}.{said} Ours: {ours}"


def _kept_summary(site: str, opinion: nn.Opinion, ours: str, now: datetime) -> str:
    """A fail that stays a fail, with what NOAA showed beside it."""
    if opinion.verdict == "unreadable":
        return f"{ours} (NOAA's archive could not be read, so nothing vouches for this gap)"
    return (f"{ours} (NOAA's archive has {site} current, newest "
            f"{_when(opinion.newest, now)}, so the gap is on our side)")


async def second_opinion(result: CheckResult, *, window_s: float,
                         ours_newest: datetime | None, excusable: bool,
                         fetchers: tuple[nn.ScanFetcher, nn.NoticeFetcher] | None = None
                         ) -> CheckResult:
    """Apply NOAA's archive to one NEXRAD check result.

    Args:
        result: The check's own verdict.
        window_s: The check's staleness window, which NOAA is judged against.
        ours_newest: The scan time of our newest volume, for `behind_noaa_s`,
            or None where the check has no such time.
        excusable: Whether the check failed for want of input, the one cause
            an upstream outage explains. A composite that is not running, or
            that opened a file and could not read it, stays a fail.
        fetchers: (list scans, read notice). None means nexrad_notices'
            fetch_scans and fetch_notice, looked up at call time so a test
            can replace them there.

    Returns:
        The result, with the archive's reading in its payload, `behind_noaa_s`
        in its metrics when both times are known, and fail turned to warn
        when the archive vouches for the gap.
    """
    if result.target not in NEXRAD_RADARS:
        return result
    excuse = result.status == "fail" and excusable
    lag = ours_newest is not None and result.status != "error"
    if not (excuse or lag):
        return result
    try:
        out = await _apply(result, window_s=window_s, ours_newest=ours_newest,
                           excuse=excuse,
                           fetchers=fetchers or (nn.fetch_scans, nn.fetch_notice))
    # The side channel must never turn the check's own verdict into an
    # error, so anything it raises leaves the result as the check made it.
    except Exception as exc:
        log.exception("NOAA second opinion failed for %s", result.check_id)
        return replace(result, payload={**result.payload, "upstream": {
            "verdict": "unreadable", "error": f"{type(exc).__name__}: {exc}"}})
    return replace(out, finished_at=utcnow())


async def _apply(result: CheckResult, *, window_s: float, ours_newest: datetime | None,
                 excuse: bool, fetchers: tuple[nn.ScanFetcher, nn.NoticeFetcher]
                 ) -> CheckResult:
    """The body of second_opinion, inside its guard."""
    site, now = result.target, result.finished_at
    since = now - timedelta(seconds=nn.LISTING_SPAN_S)
    try:
        listed, error = await asyncio.wait_for(fetchers[0](site, since, now),
                                               nn.NETWORK_DEADLINE_S)
    except asyncio.TimeoutError:
        listed, error = None, f"NOAA archive timed out after {nn.NETWORK_DEADLINE_S:g} s"
    opinion = nn.judge(listed, since, now, window_s)
    upstream: dict[str, Any] = {
        "verdict": opinion.verdict, "window_s": window_s, "error": error,
        "noaa_newest": opinion.newest.isoformat() if opinion.newest else None,
        "noaa_scans_listed": None if listed is None else len(listed),
        "returned_at": opinion.returned_at.isoformat() if opinion.returned_at else None,
    }
    metrics = dict(result.metrics)
    if opinion.newest is not None and ours_newest is not None:
        metrics["behind_noaa_s"] = (opinion.newest - ours_newest).total_seconds()
    payload = {**result.payload, "upstream": upstream}
    if not excuse:
        return replace(result, payload=payload, metrics=metrics)
    if not opinion.excuses:
        return replace(result, payload=payload, metrics=metrics,
                       summary=_kept_summary(site, opinion, result.summary, now))
    # A notice only labels, so one that times out or cannot be read leaves
    # the warn standing without a quote.
    try:
        found = await asyncio.wait_for(fetchers[1](site, now), nn.NETWORK_DEADLINE_S)
    except asyncio.TimeoutError:
        found = None
    said = nn.usable_notice(found, now)
    if said:
        upstream["notice"] = {"issued": said["issued"].isoformat(), "text": said["text"],
                              "url": said.get("url")}
    return replace(result, status="warn", payload=payload, metrics=metrics,
                   summary=_excused_summary(site, opinion, said, result.summary, now))
