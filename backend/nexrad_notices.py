"""NOAA's own archive and NWS notices, as a second opinion on a NEXRAD outage.

On 2026-11-23 the LB3 checks went red for KBBX during maintenance NWS had
announced, and KMUX had a planned gap of its own that morning. A red that a
known outside cause explains teaches people to ignore red, so Joseph approved
this rule the same day:

- A NEXRAD check that would fail reports warn when NOAA's public Level II
  archive is also stale for that site, meaning no volume within the check's
  own window. The summary names NOAA's newest scan and quotes the site's NWS
  notice (FTM) when one was issued in the last 24 h and is not a
  return-to-service message.
- NOAA fresh while ours is stale stays fail: the outage is ours.
- NOAA unreadable stays fail: a failed side channel must not hide a failure.
- A notice alone never turns a fail into a warn. It only labels the archive's
  verdict, so a notice about something unrelated cannot vouch for a gap.
- After the radar returns, the warn holds for RECOVERY_HOLD_S from NOAA's
  first scan after the gap, while our feed catches up.

Every call also measures how far our newest volume runs behind NOAA's newest,
so the hold can later be set from that distribution instead of one sample.
"""
from __future__ import annotations

import logging
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

import httpx

log = logging.getLogger(__name__)

ARCHIVE_URL = "https://unidata-nexrad-level2.s3.amazonaws.com/"
NOTICE_LIST_URL = "https://api.weather.gov/products/types/FTM/locations/{site}"
# The NWS API asks for a User-Agent. A contact address is Joseph's call, so
# this names the application only.
USER_AGENT = "AQPI-Sentinel (radar monitoring)"
REQUEST_TIMEOUT_S = 8.0

#: Joseph's stated rule, 2026-11-23. It rests on one recovery (KMUX back in
#: NOAA's archive at 19:16Z, our first frame 19:27Z, 11 min) and the NEXRAD
#: plotter's 7 to 10 min sweep-to-frame lag. `behind_noaa_s` is logged on every
#: run so it can be refit from the steady-state distribution.
RECOVERY_HOLD_S = 20 * 60
#: A notice older than this says nothing about today's gap.
NOTICE_MAX_AGE_S = 24 * 3600
#: How far back a listing reaches: the hold, plus the longest check window
#: (the staging tree's 1 h retention), plus 5 min, so a gap longer than the
#: window before a scan inside the hold is always in view.
LISTING_SPAN_S = RECOVERY_HOLD_S + 3600 + 300
#: Both NEXRAD checks run every 120 s, so one listing per site serves a cycle.
LISTING_TTL_S = 110.0
NOTICE_TTL_S = 600.0

Verdict = Literal["also_stale", "returned", "fresh", "unreadable"]
Listing = tuple[list[datetime] | None, str | None]
ScanFetcher = Callable[[str, datetime, datetime], Awaitable[Listing]]
NoticeFetcher = Callable[[str, datetime], Awaitable[dict[str, Any] | None]]

# Past or present tense only. "Will return to service at 22Z" announces an
# outage and must still label one, so neither "return" nor "will be ..."
# counts. Lookbehinds are fixed-width in Python, hence the two.
_RETURN_TO_SERVICE = re.compile(
    r"(?<!will )(?<!will be )\b(?:returned|restored)\b"
    r"|(?<!be )\bback\s+(?:in\s+service|to\s+(?:normal|routine|service|operation)|online)"
    r"|\bis\s+(?:now\s+)?operational\b|\bresumed\b", re.IGNORECASE)
#: A day holds 169 to 249 keys per site (2026-11-23), one page of 1,000.
#: Ten pages only bounds the loop.
MAX_PAGES = 10
#: The longest the second opinion may wait on either service in one run.
NETWORK_DEADLINE_S = 20.0


@dataclass(frozen=True)
class Opinion:
    """What NOAA's archive says about one site, against one check's window."""

    verdict: Verdict
    newest: datetime | None = None
    returned_at: datetime | None = None

    @property
    def excuses(self) -> bool:
        """Whether the archive vouches for the gap, so a fail becomes a warn."""
        return self.verdict in ("also_stale", "returned")


@dataclass(frozen=True)
class Page:
    """One page of an S3 listing."""

    scans: list[datetime]
    truncated: bool
    token: str | None


def read_page(xml: str, site: str) -> Page:
    """Read one ListObjectsV2 page, refusing anything it cannot account for.

    A body that parses to no scans would read as "NOAA has nothing either"
    and excuse every fail, so a captive portal's HTML, an empty body, and a
    key in a format this does not know are all errors, never empty listings.

    Args:
        xml: The response body.
        site: The four-letter site id, e.g. KBBX.

    Returns:
        The page's volume scan times, whether more pages follow, and the
        continuation token.

    Raises:
        ValueError: When the body is not a listing, or a key is not a volume
            or metadata file for this site.
    """
    if "<ListBucketResult" not in xml:
        raise ValueError("the response is not an S3 listing")
    volume = re.compile(rf"/{re.escape(site)}(\d{{8}}_\d{{6}})_V\d\d$")
    scans: list[datetime] = []
    for key in re.findall(r"<Key>([^<]*)</Key>", xml):
        m = volume.search(key)
        if m:
            # The keys name UTC scan times with no zone of their own.
            when = datetime.strptime(m.group(1) + "+0000", "%Y%m%d_%H%M%S%z")
            scans.append(when)
        elif not re.search(r"_V\d\d_MDM$", key):
            raise ValueError(f"unrecognized key in NOAA's listing: {key[:80]!r}")
    token = re.search(r"<NextContinuationToken>([^<]*)</NextContinuationToken>", xml)
    return Page(scans, "<IsTruncated>true</IsTruncated>" in xml,
                token.group(1) if token else None)


def parse_listing(xml: str, site: str) -> list[datetime]:
    """Scan times from one S3 listing page, metadata files skipped.

    Args:
        xml: The response body.
        site: The four-letter site id, e.g. KBBX.

    Returns:
        The volume scan times, in UTC, in key order.

    Raises:
        ValueError: As read_page.
    """
    return read_page(xml, site).scans


def judge(scans: list[datetime] | None, since: datetime, now: datetime,
          window_s: float, hold_s: float = RECOVERY_HOLD_S) -> Opinion:
    """Apply the rule to one listing.

    Args:
        scans: NOAA's scan times in [since, now], or None when unreadable.
        since: Where the listing starts. No scan before it is known, so it
            counts as the end of a gap.
        now: The check's time.
        window_s: The check's own staleness window.
        hold_s: How long the warn holds after the radar returns.

    Returns:
        The opinion. Only also_stale and returned excuse a fail.
    """
    if scans is None:
        return Opinion("unreadable")
    ordered = sorted(scans)
    if not ordered or (now - ordered[-1]).total_seconds() > window_s:
        return Opinion("also_stale", newest=ordered[-1] if ordered else None)
    returned = first_after_gap(ordered, since, window_s)
    if returned is not None and (now - returned).total_seconds() < hold_s:
        return Opinion("returned", newest=ordered[-1], returned_at=returned)
    return Opinion("fresh", newest=ordered[-1])


def first_after_gap(ordered: list[datetime], since: datetime,
                    window_s: float) -> datetime | None:
    """The latest scan that follows a silence longer than the window.

    Args:
        ordered: Scan times, oldest first.
        since: The listing's start, treated as the scan before the first.
        window_s: How long a silence must be to count as a gap.

    Returns:
        That scan, or None when the listing holds no such gap.
    """
    found = None
    prev = since
    for scan in ordered:
        if (scan - prev).total_seconds() > window_s:
            found = scan
        prev = scan
    return found


def notice_text(product_text: str) -> str:
    """The body of an FTM, with its WMO header and date line dropped.

    The products are fixed at 80 columns and wrap mid-word, so a line that
    fills the width joins the next with no space.

    Args:
        product_text: The product's `productText`.

    Returns:
        The message as one line.
    """
    body: list[str] = []
    for raw in product_text.splitlines():
        line = raw.rstrip()
        head = line.strip()
        if not head or head == "000" or head.startswith(("NOUS", "FTM")) \
                or head.upper().startswith("MESSAGE DATE"):
            continue
        body.append(line)
    out = ""
    for i, line in enumerate(body):
        joiner = "" if i and len(body[i - 1]) >= 80 else " "
        out = (out + joiner + line.strip()) if out else line.strip()
    return re.sub(r"\s+", " ", out).strip()


def usable_notice(notice: dict[str, Any] | None, now: datetime) -> dict[str, Any] | None:
    """The notice if it may label a gap: recent, and not a return to service.

    Args:
        notice: {"issued": datetime, "text": str}, or None.
        now: The check's time.

    Returns:
        The notice, or None.
    """
    if not notice:
        return None
    if (now - notice["issued"]).total_seconds() > NOTICE_MAX_AGE_S:
        return None
    if _RETURN_TO_SERVICE.search(str(notice["text"])):
        return None
    return notice


# One client for both public services, apart from the radarca client, whose
# User-Agent names Sentinel's radarca traffic. Created on first use.
_client: httpx.AsyncClient | None = None
# Per-site caches. The two NEXRAD checks are separate objects the scheduler
# runs on the same 120 s cadence, and these let them share one listing and
# one notice lookup instead of doubling the requests.
_listings: dict[str, tuple[float, Listing]] = {}
_notices: dict[str, tuple[float, dict[str, Any] | None]] = {}


def _http() -> httpx.AsyncClient:
    """The shared client, created on first use."""
    global _client  # noqa: PLW0603  one lazily built client per process
    if _client is None:
        _client = httpx.AsyncClient(timeout=REQUEST_TIMEOUT_S,
                                    headers={"User-Agent": USER_AGENT})
    return _client


async def fetch_scans(site: str, since: datetime, now: datetime) -> Listing:
    """NOAA's volume scan times for one site since a time, cached per cycle.

    Args:
        site: The four-letter site id.
        since: The listing's start.
        now: The check's time, which bounds the days listed.

    Returns:
        (scans, None), or (None, why) when the archive could not be read.
    """
    hit = _listings.get(site)
    if hit and time.monotonic() - hit[0] < LISTING_TTL_S:
        return hit[1]
    got = await _list_days(site, since, now)
    _listings[site] = (time.monotonic(), got)
    return got


async def _list_days(site: str, since: datetime, now: datetime) -> Listing:
    """List the one or two UTC days the span covers, from `since` on."""
    scans: list[datetime] = []
    try:
        for day in sorted({since.date(), now.date()}):
            prefix = f"{day:%Y/%m/%d}/{site}/"
            after = f"{prefix}{site}{since:%Y%m%d_%H%M%S}" if day == since.date() else None
            scans += await _list_prefix(site, prefix, after)
    except (httpx.HTTPError, ValueError) as exc:
        return None, f"NOAA archive unreadable: {type(exc).__name__}: {exc}"
    return [s for s in scans if s >= since], None


async def _list_prefix(site: str, prefix: str, start_after: str | None) -> list[datetime]:
    """Every page of one day's listing, following continuation tokens.

    Raises:
        ValueError: When a page cannot be read, or the listing is still
            truncated after MAX_PAGES. A partial listing would hide the newest
            scans and read as an outage.
    """
    scans: list[datetime] = []
    token: str | None = None
    for _ in range(MAX_PAGES):
        params = {"list-type": "2", "prefix": prefix}
        if token:
            params["continuation-token"] = token
        elif start_after:
            params["start-after"] = start_after
        resp = await _http().get(ARCHIVE_URL, params=params)
        _ = resp.raise_for_status()
        page = read_page(resp.text, site)
        scans += page.scans
        if not page.truncated:
            return scans
        if not page.token:
            raise ValueError("a truncated listing came with no continuation token")
        token = page.token
    raise ValueError(f"the listing was still truncated after {MAX_PAGES} pages")


async def fetch_notice(site: str, now: datetime) -> dict[str, Any] | None:
    """The site's newest NWS notice from the last day, cached for 10 min.

    Args:
        site: The four-letter site id; NWS files FTMs under the last three.
        now: The check's time.

    Returns:
        {"issued", "text", "url"}, or None when there is none or it cannot
        be read. A notice only labels a verdict, so failing to read one
        changes nothing else.
    """
    hit = _notices.get(site)
    if hit and time.monotonic() - hit[0] < NOTICE_TTL_S:
        return hit[1]
    got: dict[str, Any] | None = None
    try:
        got = await _newest_notice(site, now)
    # Each is a shape the response can take that this cannot read: a
    # non-JSON body, a list where a dict belongs, a missing field.
    except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError) as exc:
        log.warning("NWS notice for %s unreadable: %s: %s", site, type(exc).__name__, exc)
    _notices[site] = (time.monotonic(), got)
    return got


async def _newest_notice(site: str, now: datetime) -> dict[str, Any] | None:
    """Fetch the newest FTM's text, if it was issued within NOTICE_MAX_AGE_S."""
    accept = {"Accept": "application/ld+json"}
    resp = await _http().get(NOTICE_LIST_URL.format(site=site[1:]), headers=accept)
    _ = resp.raise_for_status()
    graph = resp.json().get("@graph") or []
    if not graph:
        return None
    newest = max(graph, key=lambda g: str(g.get("issuanceTime", "")))
    issued = datetime.fromisoformat(str(newest["issuanceTime"]))
    if issued.tzinfo is None:
        raise ValueError(f"issuanceTime {newest['issuanceTime']!r} names no zone")
    url = str(newest["@id"])
    # The link comes from the response, so it is followed only back to the
    # same API.
    if (now - issued).total_seconds() > NOTICE_MAX_AGE_S \
            or not url.startswith("https://api.weather.gov/products/"):
        return None
    prod = await _http().get(url, headers=accept)
    _ = prod.raise_for_status()
    text = notice_text(str(prod.json().get("productText", "")))
    return {"issued": issued, "text": text, "url": url}
