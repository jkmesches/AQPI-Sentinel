"""NOAA's archive as a second opinion on a NEXRAD outage.

Run directly (no DB, no network):

    python validation_tests/test_nexrad_notices.py

Joseph approved the rule on 2026-11-23 (backend/nexrad_notices.py has it in
full). The fixtures are that day's real NOAA Level II listings for KBBX, KMUX
and KDAX, fetched at 22:09Z, and the two NWS notices issued that day:

- KBBX: NOAA had no volume from 19:14:01Z to 21:09:51Z; the notice came at
  19:16Z, "down for routine maintenance through 22Z today".
- KMUX: no volume from 17:40:16Z to 19:16:15Z; the notice came at 13:42Z,
  four hours ahead. Our first frame after it was 19:27Z.
- KDAX: no gap.

So the rule is replayed at chosen instants of a real day, and each case's
answer is what that day's outage should have produced.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from itertools import pairwise
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("SENTINEL_DB_URL", "postgresql://unused/unused")
os.environ.setdefault("SENTINEL_BACKEND_ROOT", "/nonexistent-for-tests")
os.environ.setdefault("SENTINEL_RECENTFILES_DIR", "/nonexistent-for-tests/secondary")

import backend.checks as _all  # noqa: E402, F401  registers the checks
from backend import nexrad_notices as nn  # noqa: E402
from backend.checks import layer3_backend_processing as lb3  # noqa: E402
from backend.checks.base import CheckResult  # noqa: E402
from backend.checks.nexrad_upstream import second_opinion  # noqa: E402

FIX = ROOT / "validation_tests" / "fixtures" / "nexrad_notices"
INPUT_WINDOW = 3600.0
COMPOSITE_WINDOW = 900.0
failures: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    """Print one assertion and record a failure."""
    print(f"  {'ok  ' if cond else 'FAIL'}  {label}" + (f"  ({detail})" if detail else ""))
    if not cond:
        failures.append(label)


def at(hhmmss: str) -> datetime:
    """A UTC instant on 2026-11-23."""
    return datetime.strptime(f"20261123 {hhmmss}", "%Y%m%d %H:%M:%S").replace(
        tzinfo=timezone.utc)


def scans(site: str) -> list[datetime]:
    """The day's real listing for one site."""
    return nn.parse_listing((FIX / f"s3_{site}_20261123.xml").read_text(), site)


def replay(site: str, now: datetime, window_s: float) -> nn.Opinion:
    """The rule at one instant, from the listing as it stood then."""
    since = now - timedelta(seconds=nn.LISTING_SPAN_S)
    seen = [s for s in scans(site) if since <= s <= now]
    return nn.judge(seen, since, now, window_s)


def notice(site: str) -> dict[str, object]:
    """The day's real notice, as fetch_notice returns it."""
    doc = json.loads((FIX / f"ftm_{site}.json").read_text())
    return {"issued": datetime.fromisoformat(doc["issuanceTime"]),
            "text": nn.notice_text(doc["productText"]), "url": doc.get("@id")}


def test_listing() -> None:
    """The listing parser, against the real keys."""
    print("NOAA's listing:")
    kbbx = scans("KBBX")
    check("KBBX's 169 volumes are read and its _MDM files skipped", len(kbbx) == 169,
          str(len(kbbx)))
    gaps = [(a, b) for a, b in pairwise(kbbx) if (b - a).total_seconds() > 900]
    check("the one gap is 19:14:01 to 21:09:51",
          gaps == [(at("19:14:01"), at("21:09:51"))], str(gaps))
    check("another site's keys are not read as KBBX",
          nn.parse_listing((FIX / "s3_KMUX_20261123.xml").read_text(), "KBBX") == [])


def test_rule_on_the_day() -> None:
    """The rule replayed through KBBX's and KMUX's outages and KDAX's quiet day."""
    print("\nthe rule, replayed:")
    cases = [
        ("KBBX", "19:40:00", COMPOSITE_WINDOW, "also_stale", "out of the composite, NOAA down too"),
        ("KBBX", "20:30:00", INPUT_WINDOW, "also_stale", "nothing staged, NOAA down too"),
        ("KBBX", "21:15:00", INPUT_WINDOW, "returned", "NOAA back at 21:09:51, inside the hold"),
        ("KBBX", "21:15:00", COMPOSITE_WINDOW, "returned", "the same, on the composite's window"),
        ("KBBX", "21:31:00", INPUT_WINDOW, "fresh", "past the hold, so a gap now is ours"),
        ("KMUX", "19:27:00", INPUT_WINDOW, "returned", "our first frame came inside the hold"),
        ("KMUX", "19:40:00", INPUT_WINDOW, "fresh", "24 min after NOAA's return"),
        ("KDAX", "21:55:00", INPUT_WINDOW, "fresh", "no gap all day"),
        ("KDAX", "21:55:00", COMPOSITE_WINDOW, "fresh", "nor on the shorter window"),
    ]
    for site, hhmmss, window, want, why in cases:
        got = replay(site, at(hhmmss), window)
        check(f"{site} at {hhmmss}, window {window:g} s: {want} ({why})",
              got.verdict == want, got.verdict)
    stale = replay("KBBX", at("20:30:00"), INPUT_WINDOW)
    check("...naming NOAA's newest scan", stale.newest == at("19:14:01"), str(stale.newest))
    back = replay("KBBX", at("21:15:00"), INPUT_WINDOW)
    check("...and the scan it came back with", back.returned_at == at("21:09:51"),
          str(back.returned_at))
    now = at("12:00:00")
    since = now - timedelta(seconds=nn.LISTING_SPAN_S)
    check("an unreadable archive excuses nothing",
          not nn.judge(None, since, now, INPUT_WINDOW).excuses)
    empty = nn.judge([], since, now, INPUT_WINDOW)
    check("an empty listing is stale with no newest scan",
          empty.verdict == "also_stale" and empty.newest is None)


def test_notices() -> None:
    """The notice text and when it may label a gap."""
    print("\nthe notices:")
    check("KBBX's notice reads as its message alone",
          notice("BBX")["text"] == "KBBX WILL BE DOWN FOR ROUTINE MAINTENANCE THROUGH 22Z TODAY.",
          str(notice("BBX")["text"]))
    check("KMUX's 80-column wrap rejoins the split word",
          notice("MUX")["text"] == ("Radar will be unavailable from 0900-1300 (local time) "
                                    "for preventative maintenance."), str(notice("MUX")["text"]))
    bbx = notice("BBX")
    check("a notice from today may label a gap", nn.usable_notice(bbx, at("20:30:00")) is bbx)
    later = at("19:16:00") + timedelta(hours=25)
    check("one over a day old may not", nn.usable_notice(bbx, later) is None)
    back = {**bbx, "text": "KBBX HAS RETURNED TO ROUTINE OPERATIONS."}
    check("a return-to-service notice may not", nn.usable_notice(back, at("20:30:00")) is None)
    check("no notice is no label", nn.usable_notice(None, at("20:30:00")) is None)


def failing(site: str, now: datetime, summary: str = "NOTHING STAGED") -> CheckResult:
    """A failing LB3 result for one radar."""
    return CheckResult(check_id=f"layer3.input.{site}", target=site, stage="LB3",
                       status="fail", started_at=now, finished_at=now, summary=summary)


def fakes(site: str, ftm: str | None) -> tuple[object, object, list[str]]:
    """Fetchers over the fixtures, and a list of the notice lookups made.

    Args:
        site: Whose listing the scan fetcher serves.
        ftm: Whose notice the notice fetcher serves, or None for none.

    Returns:
        The scan fetcher, the notice fetcher, and the sites asked for notices.
    """
    asked: list[str] = []

    async def fake_scans(s: str, since: datetime, now: datetime) -> nn.Listing:
        _ = s
        return [x for x in scans(site) if since <= x <= now], None

    async def fake_notice(s: str, now: datetime) -> dict[str, object] | None:
        _ = now
        asked.append(s)
        return notice(ftm) if ftm else None
    return fake_scans, fake_notice, asked


def apply(result: CheckResult, site: str, ftm: str | None, window: float,
          ours: datetime | None = None) -> tuple[CheckResult, list[str]]:
    """second_opinion over the fixtures."""
    s, n, asked = fakes(site, ftm)
    got = asyncio.run(second_opinion(result, window_s=window, ours_newest=ours,
                                     scans=s, notice=n))  # type: ignore[arg-type]
    return got, asked


def test_second_opinion() -> None:
    """What a check result becomes."""
    print("\nthe result a check reports:")
    out, asked = apply(failing("KBBX", at("20:30:00")), "KBBX", "BBX", INPUT_WINDOW)
    check("KBBX nothing staged during the outage is a warn", out.status == "warn", out.status)
    check("...naming NOAA's newest scan and quoting NWS",
          "since 19:14Z" in out.summary and "NWS 19:16Z: KBBX WILL BE DOWN" in out.summary
          and out.summary.endswith("Ours: NOTHING STAGED"), out.summary)
    check("...with the reading in its payload",
          out.payload["upstream"]["verdict"] == "also_stale"
          and str(out.payload["upstream"].get("notice", {}).get("text", ""))
          .startswith("KBBX WILL"))
    out, asked = apply(failing("KBBX", at("21:40:00")), "KBBX", "BBX", INPUT_WINDOW)
    check("with NOAA current it stays a fail, said to be ours",
          out.status == "fail" and "gap is on our side" in out.summary, out.summary)
    check("...and no notice is looked up, since none could change it", asked == [])
    out, _ = apply(failing("KMUX", at("19:27:00")), "KMUX", "MUX", INPUT_WINDOW)
    check("KMUX inside the hold is a warn that says until when",
          out.status == "warn" and "until 19:36Z" in out.summary, out.summary)
    check("...and the morning's notice labels it",
          "NWS 13:42Z: Radar will be unavailable" in out.summary, out.summary)
    _, n, _ = fakes("KBBX", "BBX")

    async def unreadable(site: str, since: datetime, now: datetime) -> nn.Listing:
        _ = (site, since, now)
        return None, "NOAA archive unreadable: ConnectError"
    out = asyncio.run(second_opinion(failing("KBBX", at("20:30:00")), window_s=INPUT_WINDOW,
                                     ours_newest=None, scans=unreadable, notice=n))  # type: ignore[arg-type]
    check("an unreadable archive leaves the fail, and says so",
          out.status == "fail" and "could not be read" in out.summary, out.summary)
    passing = CheckResult(check_id="layer3.input.KDAX", target="KDAX", stage="LB3",
                          status="pass", started_at=at("21:58:00"),
                          finished_at=at("21:58:00"), summary="3 staged")
    out, _ = apply(passing, "KDAX", None, INPUT_WINDOW, ours=at("21:50:00"))
    newest = max(s for s in scans("KDAX") if s <= at("21:58:00"))
    check("a pass stays a pass and logs how far ours is behind NOAA",
          out.status == "pass" and out.metrics.get("behind_noaa_s")
          == (newest - at("21:50:00")).total_seconds(), str(out.metrics))
    xband = failing("XSCW", at("20:30:00"))
    out, asked = apply(xband, "KBBX", "BBX", INPUT_WINDOW)
    check("an X-band result comes back untouched", out is xband and asked == [])


def test_wired_into_the_check() -> None:
    """The real staging check on KBBX, with nothing staged, during the outage."""
    print("\nthe staging check itself:")
    s, n, _ = fakes("KBBX", "BBX")
    saved = (nn.fetch_scans, nn.fetch_notice, getattr(lb3, "utcnow"))  # noqa: B009  not exported
    nn.fetch_scans, nn.fetch_notice = s, n  # type: ignore[assignment]
    setattr(lb3, "utcnow", lambda: at("20:30:00"))  # noqa: B010  the module's own clock
    try:
        with tempfile.TemporaryDirectory() as empty:

            def staged_here() -> str:
                return empty

            for rid, want in (("KBBX", "warn"), ("XSCW", "fail")):
                chk = lb3.Layer3InputStagingCheck(radar_id=rid)
                chk._path = staged_here  # type: ignore[method-assign]
                got = asyncio.run(chk.run(None))
                check(f"{rid} with nothing staged reports {want}", got.status == want,
                      f"{got.status}: {got.summary}")
    finally:
        nn.fetch_scans, nn.fetch_notice = saved[0], saved[1]
        setattr(lb3, "utcnow", saved[2])  # noqa: B010  the module's own clock


def main() -> int:
    """Run every section.

    Returns:
        1 when any assertion failed, else 0.
    """
    test_listing()
    test_rule_on_the_day()
    test_notices()
    test_second_opinion()
    test_wired_into_the_check()
    print(f"\n{len(failures)} FAILED: {', '.join(failures)}" if failures
          else "\nall nexrad-notice assertions passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
