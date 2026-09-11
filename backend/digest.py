"""Daily activity digest — one row per radar and per product.

Answers the question an operator actually has in the morning: *how did each
radar do yesterday, and does anything need me today?* That is deliberately
NOT the question "what alarms fired", and the difference is not cosmetic.

An alarm-centric summary of 2026-09-05 would have opened with "nothing needs
attention": every open alarm was acknowledged. Meanwhile XSWR had been offline
for a continuous 9 h 14 m — 58% availability for the day — and had no open
alarm at report time. Organizing by subject surfaces it; organizing by alarm
hides it. For the same reason an acknowledgment never removes anything from
this report: an ack means "a human has seen it", not "it stopped happening".

=== Load-bearing: three ways this report could quietly lie ===

1. AVAILABILITY MUST EXCLUDE RUNS WE COULD NOT JUDGE. Cascade-demoted skips
   mean "we declined to form an opinion", not "healthy" — the same conflation
   that closed alarms on demoted skips and painted timeline cells green. They
   come out of the denominator, and the share excluded is disclosed on the
   report so a high number can be read with the right confidence.

2. RUN COUNTS ARE NOT DURATIONS. "273 failed checks" tells an operator
   nothing, and calling them "missed scans" would be simply wrong — a failed
   check is not a missed scan. Outages are measured as wall-clock spans, from
   the first bad run to the run that ended the outage.

3. A SINGLE BAD RUN IS NOT AN OUTAGE. Grouping consecutive bad runs into
   episodes and counting them makes XSCV look like it had 19 outages on a day
   it was fine: they were 19 isolated one-run blips totalling zero minutes.
   Reporting that as "19 outages" is precisely the crying-wolf failure this
   system exists to avoid, so sustained outages (2+ consecutive runs) and
   momentary blips are counted and described separately.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from .alarms.suppression import INCONCLUSIVE_SKIP_REASONS

log = logging.getLogger(__name__)

SETTINGS_KEY = "digest"

# Verdicts carry Sentinel's internal vocabulary. The report never prints them:
# an operator should not have to learn "GHOST_UP" to read their morning email.
# These phrasings match the glosses already used in docs/04-faq.md so the
# report, the app and the docs describe the same event the same way.
VERDICT_PLAIN = {
    "CONFIRMED_DOWN":     "offline — the source reported it too",
    "GHOST_UP":           "reported online but sent nothing",
    "STUCK_DOWN_FLAG":    "sending data while still flagged offline",
    "OBSERVED_API_ERROR": "couldn't be checked",
}
# The one an operator must not miss: the source claims the radar is fine while
# no data arrives, so nothing else in the chain will report it.
SILENT_FAILURE = "GHOST_UP"

DEFAULTS: dict[str, Any] = {
    "enabled":    False,
    "recipients": [],
    "hour":       7,                    # local hour in `tz`
    "tz":         "America/Denver",
    "products":   True,
}


@dataclass
class Subject:
    """One radar or product row."""
    key: str
    label: str
    judged: int = 0                 # runs we could form an opinion about
    passed: int = 0
    inconclusive: int = 0           # demoted skips — excluded from availability
    outages: list[dict] = field(default_factory=list)   # sustained, 2+ runs
    blips: int = 0                  # isolated single-run failures
    blip_seconds: float = 0.0
    degraded: list[dict] = field(default_factory=list)  # sustained `warn`
    degraded_blips: int = 0
    verdicts: dict[str, int] = field(default_factory=dict)
    buckets: list[float | None] = field(default_factory=list)

    @property
    def availability(self) -> float | None:
        if not self.judged:
            return None
        return 100.0 * self.passed / self.judged

    @property
    def offline_seconds(self) -> float:
        return sum(o["seconds"] for o in self.outages)

    @property
    def degraded_seconds(self) -> float:
        return sum(o["seconds"] for o in self.degraded)

    @property
    def silent_failures(self) -> int:
        return self.verdicts.get(SILENT_FAILURE, 0)


def _fmt_duration(seconds: float) -> str:
    s = int(round(seconds))
    if s < 60:
        return f"{s}s"
    h, m = divmod(s // 60, 60)
    return f"{h}h {m:02d}m" if h else f"{m}m"


def describe(sub: Subject, *, kind: str = "radar",
             chronic_since: datetime | None = None,
             now: datetime | None = None) -> str:
    """The 'what happened' sentence, in plain language.

    Ordered by what an operator needs first: a total loss, then a partial one,
    then degradation, then silent failures, then the all-clear. Never prints a
    verdict enum, and never reports a count where a duration is the honest
    unit.

    The final "nominal" is guarded. An earlier version reached it whenever
    there were no fail/error episodes — so `fcst_temp`, healthy in only 17% of
    its checks because the rest were `warn`, was described as nominal on a
    report that showed 16.7% beside it. A line that contradicts the number
    next to it is worse than no line, so anything short of healthy has to say
    something.
    """
    avail = sub.availability
    # "offline" is radar language; a forecast product is not offline, it has
    # no data.
    dead = "offline" if kind == "radar" else "no data"
    bits: list[str] = []

    if avail is not None and avail <= 0.5:
        line = f"{dead} the whole window"
        if chronic_since is not None and now is not None:
            days = max(1, int((now - chronic_since).total_seconds() // 86400))
            line += f" — day {days}"
        return line

    if sub.outages:
        total = _fmt_duration(sub.offline_seconds)
        if len(sub.outages) == 1:
            bits.append(f"one outage, {total}")
        else:
            longest = _fmt_duration(max(o["seconds"] for o in sub.outages))
            bits.append(f"{dead} {total} across {len(sub.outages)} outages, "
                        f"longest {longest}")
    # Reported whether or not there was also a sustained outage. An earlier
    # version only mentioned blips when there were no outages, which left
    # CBAND described as "one outage, 5m" beside an availability of 97.5% —
    # the missing 31 minutes were isolated single-run failures the sentence
    # never accounted for. A description has to explain the number next to it.
    brief = sub.blips + sub.degraded_blips
    if brief:
        word = "also brief" if bits else "brief interruptions only"
        secs = _fmt_duration(sub.blip_seconds) if sub.blip_seconds else None
        bits.append(f"{word}, {secs} total" if secs else
                    f"{word} ({brief} single check{'s' if brief != 1 else ''})")

    if sub.degraded:
        stale = "stale" if kind == "product" else "degraded"
        bits.append(f"{stale} {_fmt_duration(sub.degraded_seconds)}")

    n = sub.silent_failures
    if n:
        bits.append(f"{n} check{'s' if n != 1 else ''}: reported online but sent nothing")

    if bits:
        return " · ".join(bits)
    # Nothing episodic to report. Only claim "nominal" if the number agrees.
    if avail is None:
        return "no checks in window"
    if avail >= 99.5:
        return "nominal"
    return f"healthy in {avail:.0f}% of checks, no sustained outage"


async def _subject_rows(pool, since, until, like: str, strip: str) -> dict[str, Subject]:
    """Per-subject counts, outage episodes and verdict tallies in one pass."""
    rows = await pool.fetch(
        """
        WITH runs AS (
            SELECT check_id, finished_at, status, payload->>'reason' AS reason,
                   substring(summary from '→ ([A-Z_]+)') AS verdict,
                   -- Three classes, not two. `warn` is neither healthy nor
                   -- an outage: for a radar it means the status flag is stale
                   -- while data flows, for a product it means stale data. It
                   -- lowers availability but calling it "offline" would be a
                   -- lie, so it gets its own episodes.
                   CASE WHEN status IN ('fail','error') THEN 2
                        WHEN status = 'warn' THEN 1 ELSE 0 END AS bad,
                   lead(finished_at) OVER (PARTITION BY check_id
                                           ORDER BY finished_at) AS next_at
            FROM check_runs
            WHERE check_id LIKE $3 AND finished_at > $1 AND finished_at <= $2
        ),
        grp AS (
            SELECT *,
                   row_number() OVER (PARTITION BY check_id ORDER BY finished_at)
                 - row_number() OVER (PARTITION BY check_id, bad ORDER BY finished_at)
                   AS g
            FROM runs
        )
        SELECT check_id, g, bad,
               count(*)                                       AS n_runs,
               min(finished_at)                               AS started,
               -- The outage ends when the next run happens, not at the last
               -- bad run: measuring to the last bad run understates every
               -- outage by one cadence interval.
               max(coalesce(next_at, $2))                     AS ended,
               count(*) FILTER (WHERE status = 'pass')         AS n_pass,
               count(*) FILTER (WHERE status = 'skip'
                                 AND reason = ANY($4::text[])) AS n_inconclusive,
               count(*) FILTER (WHERE status = 'skip')         AS n_skip
        FROM grp
        GROUP BY check_id, g, bad
        ORDER BY check_id, min(finished_at)
        """,
        since, until, like, list(INCONCLUSIVE_SKIP_REASONS),
    )

    subs: dict[str, Subject] = {}
    for r in rows:
        key = r["check_id"].replace(strip, "")
        sub = subs.setdefault(key, Subject(key=key, label=key))
        n = int(r["n_runs"])
        inconclusive = int(r["n_inconclusive"] or 0)
        skips = int(r["n_skip"] or 0)
        sub.inconclusive += inconclusive
        # Judged = everything we formed an opinion about. Intrinsic skips (no
        # reason) counted as "nothing wrong"; demoted ones are not judged.
        sub.judged += n - inconclusive
        sub.passed += int(r["n_pass"] or 0) + (skips - inconclusive)
        cls = int(r["bad"])
        if cls:
            seconds = (r["ended"] - r["started"]).total_seconds()
            if cls == 2 and n >= 2:
                sub.outages.append({"started": r["started"], "ended": r["ended"],
                                    "seconds": seconds, "runs": n})
            elif cls == 2:
                sub.blips += 1
                sub.blip_seconds += seconds
            elif n >= 2:
                sub.degraded.append({"started": r["started"], "ended": r["ended"],
                                     "seconds": seconds, "runs": n})
            else:
                sub.degraded_blips += 1
    return subs


async def _verdicts(pool, since, until, like: str, strip: str) -> dict[str, dict[str, int]]:
    rows = await pool.fetch(
        """
        SELECT check_id, substring(summary from '→ ([A-Z_]+)') AS verdict,
               count(*) AS n
        FROM check_runs
        WHERE check_id LIKE $3 AND finished_at > $1 AND finished_at <= $2
          AND status IN ('fail','error','warn')
        GROUP BY 1, 2
        """,
        since, until, like,
    )
    out: dict[str, dict[str, int]] = {}
    for r in rows:
        if not r["verdict"]:
            continue
        out.setdefault(r["check_id"].replace(strip, ""), {})[r["verdict"]] = int(r["n"])
    return out


async def _buckets(pool, since, until, like: str, strip: str,
                   n_buckets: int = 8) -> dict[str, list[float | None]]:
    """Availability per equal slice of the window — the shape of the day.

    Cheap to compute and it answers "when" without a sentence: a single
    contiguous outage looks different from the same downtime scattered.
    """
    span = (until - since).total_seconds() / n_buckets
    rows = await pool.fetch(
        """
        SELECT check_id,
               floor(extract(epoch from (finished_at - $1)) / $4)::int AS b,
               count(*) FILTER (WHERE status = 'pass')  AS n_pass,
               count(*) FILTER (WHERE NOT (status = 'skip'
                                 AND payload->>'reason' = ANY($5::text[]))) AS judged
        FROM check_runs
        WHERE check_id LIKE $3 AND finished_at > $1 AND finished_at <= $2
        GROUP BY 1, 2
        """,
        since, until, like, span, list(INCONCLUSIVE_SKIP_REASONS),
    )
    out: dict[str, list[float | None]] = {}
    for r in rows:
        key = r["check_id"].replace(strip, "")
        arr = out.setdefault(key, [None] * n_buckets)
        idx = min(max(int(r["b"]), 0), n_buckets - 1)
        judged = int(r["judged"] or 0)
        arr[idx] = (100.0 * int(r["n_pass"] or 0) / judged) if judged else None
    return out


async def _chronic_since(pool, check_id: str, target: str):
    """When the current unbroken non-pass run began — for the 'day N' counter.

    Reuses the hold-down's definition so the report and the alarm engine agree
    on when a problem started; an inconclusive skip does not break the streak.
    """
    from .alarms.suppression import INCONCLUSIVE_SKIP_REASONS
    return await pool.fetchval(
        """
        SELECT min(finished_at) FROM check_runs
        WHERE check_id = $1 AND target = $2
          AND finished_at > coalesce(
                (SELECT max(finished_at) FROM check_runs
                  WHERE check_id = $1 AND target = $2
                    AND (status = 'pass'
                         OR (status = 'skip'
                             AND coalesce(payload->>'reason','') <> ALL($3::text[])))),
                '-infinity'::timestamptz)
        """,
        check_id, target, list(INCONCLUSIVE_SKIP_REASONS),
    )


async def compute(pool, *, since: datetime, until: datetime,
                  tz: str = "America/Denver", products: bool = True) -> dict:
    """Everything the report states, computed once.

    Returned as plain data so the email, the JSON endpoint and any future web
    view render the same numbers rather than each recomputing them slightly
    differently.
    """
    zone = ZoneInfo(tz)

    async def family(like: str, strip: str, kind: str) -> list[dict]:
        subs = await _subject_rows(pool, since, until, like, strip)
        vmap = await _verdicts(pool, since, until, like, strip)
        bmap = await _buckets(pool, since, until, like, strip)
        out = []
        for key, sub in subs.items():
            sub.verdicts = vmap.get(key, {})
            sub.buckets = bmap.get(key, [])
            chronic = None
            avail = sub.availability
            if avail is not None and avail <= 0.5:
                chronic = await _chronic_since(pool, f"{strip}{key}", key)
            out.append({
                "key":            key,
                "kind":           kind,
                "name":           subject_name(kind, key),
                "availability":   sub.availability,
                "judged":         sub.judged,
                "inconclusive":   sub.inconclusive,
                "offline_seconds": sub.offline_seconds,
                "outages":        len(sub.outages),
                "blips":          sub.blips,
                "silent_failures": sub.silent_failures,
                "buckets":        sub.buckets,
                "chronic_since":  chronic.isoformat() if chronic else None,
                "degraded_seconds": sub.degraded_seconds,
                "description":    describe(sub, kind=kind, chronic_since=chronic,
                                           now=until),
            })
        # Worst first: attention should land without hunting.
        out.sort(key=lambda d: (d["availability"] if d["availability"] is not None else 101))
        return out

    radars = await family("layer2.radar.%", "layer2.radar.", "radar")
    prods = await family("layer1.product.%", "layer1.product.", "product") if products else []

    totals = await pool.fetchrow(
        """
        SELECT count(*) AS runs,
               count(*) FILTER (WHERE status = 'skip'
                                 AND payload->>'reason' = ANY($3::text[]))
                   AS inconclusive
        FROM check_runs WHERE finished_at > $1 AND finished_at <= $2
        """,
        since, until, list(INCONCLUSIVE_SKIP_REASONS),
    )
    runs = int(totals["runs"] or 0)
    incon = int(totals["inconclusive"] or 0)

    return {
        "since":    since.isoformat(),
        "until":    until.isoformat(),
        "tz":       tz,
        "window_label": (f"{until.astimezone(zone):%a %d %b %H:%M %Z}"),
        "radars":   radars,
        "products": prods,
        "runs":     runs,
        "inconclusive": incon,
        "conclusive_pct": (100.0 * (runs - incon) / runs) if runs else None,
    }


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
# Eight blocks, one per 3 h of the window. Shape at a glance: one contiguous
# outage looks different from the same downtime scattered across the day, and
# no sentence conveys that as fast.
_BLOCKS = "▁▂▃▄▅▆▇█"


def spark(buckets: list[float | None]) -> str:
    out = []
    for b in buckets or []:
        if b is None:
            out.append("·")            # nothing ran — not the same as 0%
        else:
            out.append(_BLOCKS[min(7, max(0, int(b / 100 * 7 + 0.5)))])
    return "".join(out) or "·" * 8


def subject_name(kind: str, key: str) -> str:
    """The human name for a radar or product, or "" when there isn't one.

    Sourced from the two places that already hold this vocabulary rather than
    from a third copy: RADAR_META for sites, check_labels for products. The
    module docstring in check_labels is explicit that emails, push payloads and
    both UIs must share one vocabulary, and a report that invented its own
    would be the surface where a drift is least likely to be noticed — nobody
    diffs yesterday's email against the dashboard.

    Imported inside the function because backend.api.routes.radars builds an
    APIRouter at import time, and digest.py is imported by check code paths
    that have no business pulling in the API layer.
    """
    if kind == "radar":
        try:
            from .api.routes.radars import RADAR_META
        except Exception:                                  # pragma: no cover
            return ""
        name = (RADAR_META.get(key) or {}).get("name") or ""
        # KBBX and friends are named after themselves in RADAR_META; repeating
        # the id as its own name reads like a bug in the report.
        return "" if name == key else name
    from .check_labels import product_label
    name = product_label(key)
    return "" if name == key else name


def subject_title(kind: str, key: str) -> str:
    """`XSWR · Sawyer Ridge` — id first, because the id is what appears in
    alarms, check ids and the evidence links, and the name is what tells a
    reader which hillside that is."""
    name = subject_name(kind, key)
    return f"{key} · {name}" if name else key


def evidence_url(public_url: str, check_id: str, target: str,
                 since: str, until: str,
                 status: str = "fail,error,warn,skip") -> str | None:
    """Deep link to the runs behind a row.

    Every declaration in the report links to the filtered history view that
    produced it, so a reader can check a claim rather than trust it. /history
    already accepts exactly these filters.

    The link carries a status filter, and must. A product check runs every
    ~60 s, so the 24 h window behind one of these rows holds ~1,440 runs while
    /history renders the newest 500 — meaning the link opened on the last ~8
    hours of the window and, on 2026-09-11, showed every one of qpe_15min,
    qpe_1hr, precip_rate_radar and comp_ref as unbroken green while the report
    correctly said 99.5-99.8%. All fourteen of their non-passing runs were in
    the truncated older end. The evidence link disproved the report.

    Only non-nominal rows are linked (render_text and the template both list
    perfect subjects as a collapsed "N nominal" line), so every link is
    attached to a claim about something going wrong, and "every run that was
    not a pass" is exactly the set that claim is about. Inconclusive skips are
    included even though availability excludes them from its denominator:
    they are still not passes, the reader can see the reason on each row, and
    the report discloses the conclusive share separately.
    """
    if not public_url:
        return None
    from urllib.parse import quote

    def _short(iso: str) -> str:
        # Minute precision, no microseconds, no +00:00 to percent-encode. The
        # full ISO form produced 180-character links that made the plain-text
        # part unreadable — and the text part is what a phone shows when it
        # cannot render HTML, so it has to stay legible.
        return iso.split(".")[0].replace("+00:00", "") + "Z"

    return (f"{public_url}/history?check_id={quote(check_id, safe='')}"
            f"&target={quote(target, safe='')}"
            f"&since={_short(since)}&until={_short(until)}&tab=checks"
            f"&status={quote(status, safe=',')}")


def _rows(data: dict, kind: str) -> list[dict]:
    return data["radars"] if kind == "radar" else data["products"]


def render_text(data: dict, public_url: str = "") -> str:
    since, until = data["since"], data["until"]
    L: list[str] = []
    L.append(f"AQPI Sentinel — 24 h to {data['window_label']}")
    L.append("")

    for kind, heading, prefix in (("radar", "RADARS", "layer2.radar."),
                                  ("product", "PRODUCTS", "layer1.product.")):
        rows = _rows(data, kind)
        if not rows:
            continue
        # Anything perfectly healthy collapses to one line. A row per nominal
        # product turns a 6-line report into a 20-line one and buries the two
        # rows that matter — the report has to stay scannable on a phone, and
        # length should track how much went wrong, not how much exists.
        nominal = [r for r in rows if r["availability"] is not None
                   and r["availability"] >= 99.95]
        listed = [r for r in rows if r not in nominal]
        # Two lines per subject rather than one wide row. Adding the name to
        # the aligned column pushed it past 100 characters — `fcst_total_precip_cum
        # · Forecast — Cumulative Precipitation` is 57 on its own — and the
        # text part is what a phone shows when it cannot render the HTML, so it
        # has to stay inside a narrow screen.
        L.append(heading)
        for r in listed:
            av = "n/a" if r["availability"] is None else f"{r['availability']:.1f}%"
            # `kind` from the section being rendered, not from the row: the
            # section always knows it, and requiring every caller to carry it
            # on each row made render_text throw on a payload that was
            # otherwise complete.
            L.append(f"  {subject_title(kind, r['key'])}")
            L.append(f"    {spark(r['buckets'])}  {av:>6}  {r['description']}")
            url = evidence_url(public_url, f"{prefix}{r['key']}", r["key"], since, until)
            if url:
                L.append(f"    {url}")
        if nominal:
            # Ids only here. These are the rows with nothing to report, and
            # spelling out six site names to say "nothing happened" buries the
            # rows that do need reading.
            names = ", ".join(r["key"] for r in nominal)
            L.append(f"  {len(nominal)} nominal: {names}")
        L.append("")

    if data["runs"]:
        L.append(f"{data['runs']:,} checks · {data['conclusive_pct']:.1f}% conclusive")
        if data["inconclusive"]:
            L.append(f"{data['inconclusive']:,} runs could not be judged (an upstream "
                     f"dependency was unhealthy) and are excluded from availability.")
    return "\n".join(L)


def subject_line(data: dict) -> str:
    """Verdict first, so the report is readable without opening it."""
    rows = data["radars"] + data["products"]
    down = [r for r in rows if r["availability"] is not None and r["availability"] <= 0.5]
    degraded = [r for r in rows
                if r["availability"] is not None and 0.5 < r["availability"] < 95]
    if down and degraded:
        return (f"Sentinel — {len(down)} down, {len(degraded)} degraded")
    if down:
        return f"Sentinel — {len(down)} down"
    if degraded:
        return f"Sentinel — {len(degraded)} degraded"
    return "Sentinel — all nominal"


# ---------------------------------------------------------------------------
# HTML rendering
# ---------------------------------------------------------------------------
# Email HTML is not web HTML. Rules that shape everything below:
#   * Tables for layout — Outlook still uses Word's rendering engine, and
#     flexbox/grid do not exist there.
#   * Inline styles — several clients strip <style> blocks entirely, so the
#     <style> block carries only the dark-mode and small-screen niceties and
#     nothing the layout depends on.
#   * One column, always. A two-column layout that "collapses" on mobile needs
#     media queries that Outlook and some webmail ignore; a single column is
#     legible everywhere without them.
#   * No external images. Most clients block them by default, so a bar chart
#     made of images would simply be missing — the bars are table cells with
#     background colors instead, which always render. Unicode blocks (▇█)
#     were the other option and they render inconsistently across fonts.
#   * Color never carries meaning alone — every row states its percentage and
#     its description in words.

_C_OK, _C_WARN, _C_BAD, _C_NONE = "#16a34a", "#d97706", "#dc2626", "#d1d5db"


def avail_color(pct: float | None) -> str:
    if pct is None:
        return _C_NONE
    if pct >= 99.5:
        return _C_OK
    if pct >= 95.0:
        return "#65a30d"
    if pct >= 50.0:
        return _C_WARN
    return _C_BAD


def bar_cells(buckets: list[float | None]) -> list[dict]:
    """Eight color swatches with a per-cell tooltip."""
    out = []
    for i, b in enumerate(buckets or [None] * 8):
        out.append({
            "color": avail_color(b),
            "title": "no checks" if b is None else f"{b:.0f}% healthy",
        })
    return out


def render_html(data: dict, public_url: str = "") -> str:
    from jinja2 import Environment, FileSystemLoader, select_autoescape
    from pathlib import Path as _P
    env = Environment(
        loader=FileSystemLoader(str(_P(__file__).parent / "templates")),
        autoescape=select_autoescape(["html"]),
        trim_blocks=True, lstrip_blocks=True,
    )
    env.globals.update(bar_cells=bar_cells, avail_color=avail_color,
                       evidence_url=evidence_url)
    sections = []
    for kind, heading, prefix in (("radar", "Radars", "layer2.radar."),
                                  ("product", "Products", "layer1.product.")):
        rows = _rows(data, kind)
        if not rows:
            continue
        nominal = [r for r in rows if r["availability"] is not None
                   and r["availability"] >= 99.95]
        sections.append({
            "heading": heading,
            "prefix":  prefix,
            "rows":    [r for r in rows if r not in nominal],
            "nominal": nominal,
        })
    html = env.get_template("digest.html").render(
        d=data, sections=sections, public_url=public_url,
        subject=subject_line(data),
        # Web-safe stack on purpose. Email clients are inconsistent about
        # system-font keywords, and the long -apple-system stack cost ~3 KB
        # repeated across every row for a difference nobody would notice.
        F="Helvetica,Arial,sans-serif",
    )
    # === Load-bearing: Gmail CLIPS a message body over ~102 KB ===
    #
    # It truncates mid-document and appends a "View entire message" link, so
    # the tail of the report — the confidence line and the dashboard button —
    # silently disappears for the readers most likely to be on Gmail. Template
    # indentation alone accounted for a third of the payload. Collapsing
    # whitespace between tags is safe here: there is no <pre> and no element
    # whose rendering depends on inter-tag spacing.
    import re as _re
    html = _re.sub(r">\s+<", "><", html)
    html = _re.sub(r"\s{2,}", " ", html)
    return html


# ---------------------------------------------------------------------------
# Daily send
# ---------------------------------------------------------------------------
class DigestTask:
    """Sends the report once a day at a wall-clock local hour.

    A wall-clock hour rather than a 24 h interval, for the same reason the
    retention sweep uses one: an interval timer restarts on every deploy, so a
    frequently-deployed service would send the report at a drifting time, or
    twice, or not at all on a day with two restarts.

    The guard is the (date, hour) it last sent in the CONFIGURED zone, not
    UTC. Getting that wrong would put the report an hour out twice a year at
    the DST boundaries — the same class of bug as radar-display's Mountain
    timestamps, and just as invisible until someone notices the mail arrives
    at the wrong time.

    That guard is PERSISTED, not held in memory. A restart inside the send
    hour would otherwise re-send: the tick runs every five minutes, so a
    deploy at 07:20 finds hour == 7 and no record of having sent, and every
    recipient gets the report twice. Held in memory the claim was worth
    exactly as much as the process's uptime, which is the one thing a deploy
    takes away.

    The claim is made BEFORE sending and conditionally, in one statement, so
    the failure mode is a skipped report rather than a duplicate one — the
    right way round for mail that goes to a mailing list — and so two backends
    against one database cannot both win the day.
    """

    STATE_KEY = "digest_state"

    def __init__(self, app, cfg: dict | None = None):
        self.app = app
        self.cfg = dict(DEFAULTS)
        if cfg:
            self.cfg.update(cfg)
        self._task = None
        # Set when the schedule changes under us; the next tick releases the
        # persisted claim so moving the hour forward still sends today.
        self._release_claim = False
        self.last_result: dict | None = None

    def reload(self, cfg: dict) -> None:
        """Apply a config change from the admin UI without a restart."""
        was = (self.cfg.get("enabled"), self.cfg.get("hour"), self.cfg.get("tz"))
        self.cfg = dict(DEFAULTS)
        self.cfg.update(cfg or {})
        if was != (self.cfg.get("enabled"), self.cfg.get("hour"), self.cfg.get("tz")):
            # Don't carry a "already sent today" claim across a schedule
            # change, or moving the hour forward silently skips today's
            # report. Deferred to the next tick because reload() is called
            # from a request handler and must not block on the database.
            self._release_claim = True
        log.info("digest: config reloaded — enabled=%s %02d:00 %s -> %d recipient(s)",
                 self.cfg.get("enabled"), int(self.cfg.get("hour", 7)),
                 self.cfg.get("tz"), len(self.cfg.get("recipients") or []))

    async def start(self) -> None:
        import asyncio
        self._task = asyncio.create_task(self._loop(), name="digest")
        log.info("digest: armed — enabled=%s, %02d:00 %s, %d recipient(s)",
                 self.cfg.get("enabled"), int(self.cfg.get("hour", 7)),
                 self.cfg.get("tz"), len(self.cfg.get("recipients") or []))

    async def stop(self) -> None:
        import asyncio
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass

    async def _loop(self) -> None:
        import asyncio
        while True:
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("digest: tick failed")
            await asyncio.sleep(300)

    async def _tick(self) -> None:
        if not self.cfg.get("enabled"):
            return
        recipients = [r for r in (self.cfg.get("recipients") or []) if r]
        if not recipients:
            return
        try:
            zone = ZoneInfo(str(self.cfg.get("tz") or "UTC"))
        except Exception:
            log.error("digest: unknown timezone %r — not sending",
                      self.cfg.get("tz"))
            return
        now_local = datetime.now(timezone.utc).astimezone(zone)
        pool = self.app.state.store.pool
        if self._release_claim:
            self._release_claim = False
            await self._release(pool)
        if now_local.hour != int(self.cfg.get("hour", 7)):
            return
        if not await self._claim(pool, now_local.date().isoformat()):
            return
        await self.send(recipients)

    async def _claim(self, pool, day: str) -> bool:
        """Claim `day` as sent, returning False if it was already claimed.

        One statement, so the check and the write cannot be separated by a
        restart or by a second backend. The conditional DO UPDATE is what
        makes it a claim rather than a read-then-write: a conflicting row
        whose date already equals `day` matches no WHERE, updates nothing,
        and returns nothing.
        """
        try:
            row = await pool.fetchrow(
                """
                INSERT INTO settings (key, value, updated_at, updated_by)
                VALUES ($1, jsonb_build_object('last_sent_date', $2::text),
                        now(), 'digest')
                ON CONFLICT (key) DO UPDATE
                   SET value = jsonb_build_object('last_sent_date', $2::text),
                       updated_at = now(), updated_by = 'digest'
                 WHERE settings.value->>'last_sent_date' IS DISTINCT FROM $2::text
                RETURNING key
                """,
                self.STATE_KEY, day,
            )
        except Exception:
            # A database that cannot record the claim cannot be trusted not to
            # send twice, so don't send. Louder than a duplicate would be, and
            # the report is a day's summary — missing one is recoverable from
            # /api/report/daily, an unwanted second copy to a mailing list is
            # not.
            log.exception("digest: could not claim %s — not sending", day)
            return False
        return row is not None

    async def _release(self, pool) -> None:
        try:
            await pool.execute("DELETE FROM settings WHERE key = $1",
                               self.STATE_KEY)
        except Exception:
            log.exception("digest: could not release the send claim")

    async def send(self, recipients: list[str]) -> dict:
        from .auth.email import send_transactional
        from .config import SETTINGS as _S
        pool = self.app.state.store.pool
        until = datetime.now(timezone.utc)
        since = until - timedelta(hours=24)
        data = await compute(pool, since=since, until=until,
                             tz=str(self.cfg.get("tz") or "UTC"),
                             products=bool(self.cfg.get("products", True)))
        subject = subject_line(data)
        text = render_text(data, _S.public_url)
        html = render_html(data, _S.public_url)
        sent, failed = [], []
        for to in recipients:
            ok, err = await send_transactional(pool, to=to, subject=subject,
                                               body=text, html=html)
            (sent if ok else failed).append(to if ok else f"{to}: {err}")
            if not ok:
                log.error("digest: delivery to %s failed — %s", to, err)
        self.last_result = {
            "at": until.isoformat(), "subject": subject,
            "sent": sent, "failed": failed,
        }
        log.info("digest: sent to %d/%d recipient(s)", len(sent), len(recipients))
        return self.last_result
