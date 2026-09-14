"""Daily digest — the report must not contradict its own numbers.

Run directly (no DB, no network):

    python validation_tests/test_digest.py

The digest states a percentage and a sentence side by side, so any disagreement
between them is visible to the reader and corrosive to trust in the whole
report. Both of the defects pinned here were found by running the real thing
against production rather than by reading the code:

  1. `fcst_temp` was rendered as "16.7%  nominal". Availability counts `warn`
     runs as unhealthy, but the description only looked at fail/error
     episodes — and a product that is merely stale produces `warn`. The
     sentence and the number were computed from different definitions.

  2. CBAND was rendered as "97.6%  one outage, 5m". The other 31 minutes were
     isolated single-run failures, which the description skipped entirely
     because it only mentioned blips when there was no sustained outage.

The third property is the one this system keeps relearning: availability is
computed over runs we could actually judge. Cascade-demoted skips mean "we
declined to form an opinion", and counting them as healthy is the same mistake
that closed alarms on demoted skips and painted timeline cells green.
"""
from __future__ import annotations
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("SENTINEL_DB_URL", "postgresql://unused/unused")

from backend.digest import (                                   # noqa: E402
    DEFAULTS, DigestTask, Subject, avail_color, bar_cells, describe,
    evidence_url, render_text, spark, subject_line, subject_name, subject_title,
    bar_bands, blend_color, render_html, _wire_len, _BAR_PX,
    _GMAIL_CLIP_BYTES, _COMPACT_ABOVE_WIRE_BYTES,
    _C_OK, _C_WARN, _C_BAD, _C_ERROR, _C_SKIP, _C_NONE,
)

failures: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if cond else 'FAIL'}  {label}" + (f"  — {detail}" if detail else ""))
    if not cond:
        failures.append(label)


def sub(**kw) -> Subject:
    s = Subject(key=kw.pop("key", "XTEST"), label="XTEST")
    for k, v in kw.items():
        setattr(s, k, v)
    return s


def ep(seconds: float, runs: int = 5) -> dict:
    now = datetime.now(timezone.utc)
    return {"started": now, "ended": now + timedelta(seconds=seconds),
            "seconds": seconds, "runs": runs}


class FakePool:
    """Records statements and replays a scripted answer for the claim.

    Deliberately does NOT emulate ON CONFLICT — emulating Postgres here would
    only test the emulation. What it tests is the decision the task makes given
    an answer: claim won -> send, claim lost -> silent, claim raised -> silent.
    The SQL's own semantics are asserted by shape below and exercised against a
    real database by ops when the digest runs.
    """

    def __init__(self, claim_result="won", fail=False):
        self.claim_result = claim_result
        self.fail = fail
        self.statements: list[str] = []

    async def fetchrow(self, sql, *args):
        self.statements.append(sql)
        if self.fail:
            raise RuntimeError("connection reset")
        return {"key": args[0]} if self.claim_result == "won" else None

    async def execute(self, sql, *args):
        self.statements.append(sql)


class FakeApp:
    def __init__(self, pool):
        self.state = type("S", (), {"store": type("T", (), {"pool": pool})()})()


def tick(cfg, pool, *, sent: list) -> None:
    """Run one _tick with send() stubbed, at the configured hour."""
    import asyncio
    task = DigestTask(FakeApp(pool), cfg)

    async def _send(recipients):
        sent.append(list(recipients))
    task.send = _send
    asyncio.run(task._tick())
    return task


def main() -> int:
    print("description must agree with the number beside it:")
    # Defect 1: warn-only degradation described as nominal.
    s = sub(judged=48, passed=8, degraded=[ep(69300, 40)])
    d = describe(s, kind="product")
    check("a product healthy in 17% of checks is not called nominal",
          "nominal" not in d, d)
    check("...and says what actually happened", "stale" in d, d)

    # Defect 2: blips hidden behind a sustained outage.
    s = sub(judged=720, passed=703, outages=[ep(300, 3)], blips=15, blip_seconds=1140)
    d = describe(s)
    check("blips are reported even when there is also an outage",
          "brief" in d, d)
    check("...alongside the outage, not instead of it", "outage" in d, d)

    # The guard that makes both of the above impossible to reintroduce
    # silently: anything short of healthy has to say something.
    s = sub(judged=100, passed=80)          # 80%, no episodes recorded at all
    d = describe(s)
    check("low availability with no episodes still refuses to say nominal",
          "nominal" not in d, d)
    s = sub(judged=100, passed=100)
    check("a genuinely clean subject is nominal", describe(s) == "nominal")

    print("wording:")
    s = sub(judged=700, passed=0)
    check("a radar at zero is 'offline'", "offline" in describe(s, kind="radar"))
    check("a product at zero is 'no data', not offline",
          "no data" in describe(s, kind="product"))
    chronic = datetime.now(timezone.utc) - timedelta(days=52)
    check("a chronic outage carries a day counter",
          "day 52" in describe(s, chronic_since=chronic, now=datetime.now(timezone.utc)))
    # Internal vocabulary must not leak into an operator's inbox.
    s = sub(judged=700, passed=690, verdicts={"GHOST_UP": 4})
    d = describe(s)
    check("the silent-failure case is described, not named",
          "reported online but sent nothing" in d and "GHOST_UP" not in d, d)
    for enum in ("CONFIRMED_DOWN", "STUCK_DOWN_FLAG", "OBSERVED_API_ERROR"):
        check(f"{enum} never appears verbatim", enum not in d)

    print("durations, not counts:")
    s = sub(judged=716, passed=418, outages=[ep(33360, 273)])
    d = describe(s)
    check("an outage is stated as a duration", "9h 16m" in d, d)
    check("...and not as a run count", "273" not in d, d)

    print("availability excludes what we could not judge:")
    s = sub(judged=0, passed=0, inconclusive=50)
    check("a fully inconclusive window has no availability, not 0%",
          s.availability is None, str(s.availability))
    check("...and says so rather than implying an outage",
          describe(s) == "no checks in window", describe(s))
    s = sub(judged=100, passed=100, inconclusive=900)
    check("inconclusive runs do not drag a healthy subject down",
          s.availability == 100.0, str(s.availability))

    print("subject line carries the verdict:")
    down = {"key": "XEBY", "availability": 0.0}
    deg = {"key": "XSWR", "availability": 58.3}
    ok = {"key": "CBAND", "availability": 99.9}
    check("all-clear says so",
          subject_line({"radars": [ok], "products": []}) == "Sentinel — all nominal")
    check("a down subject leads the subject line",
          "1 down" in subject_line({"radars": [down, ok], "products": []}))
    check("down and degraded are both counted",
          subject_line({"radars": [down, deg], "products": []})
          == "Sentinel — 1 down, 1 degraded")

    print("evidence links:")
    u = evidence_url("https://x.test", "layer2.radar.XSWR", "XSWR",
                     "2026-09-07T17:30:03.255429+00:00",
                     "2026-09-08T17:30:03.255429+00:00")
    check("link targets the filtered history view", "/history?check_id=" in u, u)
    check("timestamps are trimmed, not microsecond ISO",
          ".255429" not in u and "%3A" not in u, u)
    check("link stays short enough for the plain-text part", len(u) < 160, str(len(u)))
    check("no public_url means no broken half-link",
          evidence_url("", "a", "b", "c", "d") is None)

    print("rendering:")
    check("spark renders one block per bucket", len(spark([50] * 8)) == 8)
    check("a bucket with no checks is not drawn as zero",
          spark([None]) == "·", spark([None]))
    check("color tracks severity",
          avail_color(100) != avail_color(60) != avail_color(0))
    check("no data has its own color", avail_color(None) == "#d1d5db")
    check("bar_cells always yields 8 swatches", len(bar_cells([])) == 8)
    # Tooltips only where the colour is ambiguous — a plain single-status bin
    # says it itself, and 152 of them cost ~6 KB of the size budget.
    check("an ambiguous swatch carries a tooltip",
          all(bar_cells([None])[0]["title"] for _ in (0,)))
    check("a plain single-status swatch does not",
          bar_cells([{"n": 5, "n_pass": 5}])[0]["title"] == "")
    check("a swatch with excluded runs keeps one",
          bar_cells([{"n": 5, "n_pass": 5, "n_excluded": 3}])[0]["title"] != "")

    data = {
        "since": "2026-09-07T17:30:03+00:00", "until": "2026-09-08T17:30:03+00:00",
        "tz": "America/Denver", "window_label": "Tue 08 Sep 11:30 MDT",
        "radars": [{"key": "XEBY", "availability": 0.0, "buckets": [0] * 8,
                    "description": "offline the whole window — day 52"},
                   {"key": "CBAND", "availability": 100.0, "buckets": [100] * 8,
                    "description": "nominal"}],
        "products": [], "runs": 32035, "excluded": 130,
        "conclusive_pct": 99.6,
    }
    txt = render_text(data, "https://x.test")
    check("text report names the worst subject", "XEBY" in txt)
    # Healthy subjects used to collapse into "N nominal: CBAND". They now get a
    # full row: a radar reported at 100% reads differently from one merely
    # absent from the trouble list, and the number is the thing a reader most
    # often wants to confirm.
    check("a healthy subject gets its own row with its uptime",
          "CBAND" in txt and "100.0%" in txt,
          [l for l in txt.splitlines() if "CBAND" in l or "100.0" in l])
    check("...and is not collapsed into a nominal summary line",
          "nominal: CBAND" not in txt and "1 nominal" not in txt)
    check("...while the worst subject still comes first",
          txt.index("XEBY") < txt.index("CBAND"))
    check("the coverage caveat is stated, not implied",
          "are not counted above" in txt and "pass, warn or fail" in txt,
          [l for l in txt.splitlines() if "not counted" in l])
    check("defaults ship disabled", DEFAULTS["enabled"] is False)

    # --- the daily send claim ------------------------------------------------
    # A restart inside the send hour used to re-send: the guard was in memory,
    # the tick runs every 5 minutes, and a deploy at 07:20 sees hour == 7 with
    # no memory of having sent. Every recipient gets the report twice.
    hour = datetime.now(timezone.utc).astimezone(
        __import__("zoneinfo").ZoneInfo("America/Denver")).hour
    cfg = {"enabled": True, "recipients": ["a@example.com"], "hour": hour,
           "tz": "America/Denver", "products": False}

    sent: list = []
    pool = FakePool(claim_result="won")
    tick(cfg, pool, sent=sent)
    check("sends when the day is unclaimed", sent == [["a@example.com"]], repr(sent))

    sent = []
    tick(cfg, FakePool(claim_result="lost"), sent=sent)
    check("a second process in the same hour sends nothing", sent == [])

    sent = []
    tick(cfg, FakePool(fail=True), sent=sent)
    check("an unrecordable claim sends nothing rather than risking a duplicate",
          sent == [])

    sent = []
    tick({**cfg, "hour": (hour + 12) % 24}, FakePool(claim_result="won"), sent=sent)
    check("sends nothing outside the configured hour", sent == [])

    sent = []
    off = tick({**cfg, "enabled": False}, FakePool(claim_result="won"), sent=sent)
    check("disabled sends nothing and does not touch the database",
          sent == [] and off.app.state.store.pool.statements == [])

    sent = []
    norecip = FakePool(claim_result="won")
    tick({**cfg, "recipients": []}, norecip, sent=sent)
    check("no recipients claims nothing", sent == [] and norecip.statements == [])

    # The claim must be one statement, and conditional. Two statements — read
    # then write — is the bug with extra steps, and an unconditional DO UPDATE
    # always returns a row, so every tick in the hour would "win".
    claim_sql = FakePool(claim_result="won")
    tick(cfg, claim_sql, sent=[])
    sql = " ".join(claim_sql.statements[-1].split())
    check("the claim is a single INSERT ... ON CONFLICT",
          sql.count(";") == 0 and "INSERT INTO settings" in sql
          and "ON CONFLICT" in sql)
    check("the claim is conditional, so a re-claim returns no row",
          "IS DISTINCT FROM" in sql and "RETURNING" in sql)
    check("the claim is stored under its own key, not the config row",
          DigestTask.STATE_KEY != "digest"
          and DigestTask.STATE_KEY == "digest_state")

    # A schedule change has to release the claim, or moving the hour forward
    # skips today entirely.
    released = FakePool(claim_result="won")
    t2 = DigestTask(FakeApp(released), cfg)
    t2.reload({**cfg, "hour": (hour + 1) % 24})
    check("a schedule change queues a release", t2._release_claim is True)

    # Each of these needs a FRESH task. Re-using t2 would assert True against a
    # flag the previous line had already set, which passes whatever reload does.
    for label, change in (("re-saving the same schedule", {}),
                          ("a recipient-only change", {"recipients": ["b@example.com"]}),
                          ("toggling products", {"products": True})):
        fresh = DigestTask(FakeApp(released), cfg)
        fresh.reload({**cfg, **change})
        check(f"{label} does not release the claim", fresh._release_claim is False)

    for label, change in (("disabling", {"enabled": False}),
                          ("a timezone change", {"tz": "UTC"})):
        fresh = DigestTask(FakeApp(released), cfg)
        fresh.reload({**cfg, **change})
        check(f"{label} releases the claim", fresh._release_claim is True)

    # --- swatches use the timeline's encoding ---------------------------------
    # A reader who opens the grid after reading the report must not have to
    # translate. Same palette, same stack: fail, error, warn, skip, then the
    # share that really passed.
    def stack(b):
        return [(x["color"], x["px"]) for x in bar_bands(b)]

    check("the palette is the grid's light theme, verbatim",
          (_C_OK, _C_WARN, _C_BAD, _C_ERROR) ==
          ("#16a34a", "#9a6905", "#B91C1C", "#6D28D9"))

    allpass = stack({"n": 30, "n_pass": 30, "avail": 100.0})
    check("an untroubled bucket is one solid green swatch",
          allpass == [(_C_OK, _BAR_PX)], str(allpass))

    # The report covers pass/warn/fail only. `n` is already the count of those,
    # so XEBY's 22:00 bucket (12 fail, 2 error, 1 skip) reports n=12 and reads
    # as wholly failing rather than mostly failing with a violet cap.
    xeby = stack({"n": 12, "n_pass": 0, "n_fail": 12, "n_excluded": 3})
    check("a bucket with no passing runs shows no green",
          all(c != _C_OK for c, _ in xeby), str(xeby))
    check("...and no violet or grey, which the report no longer uses",
          all(c not in (_C_ERROR, _C_SKIP) for c, _ in xeby), str(xeby))
    check("...stacked most-severe LAST, because HTML rows paint top-down",
          xeby[-1][0] == _C_BAD, str(xeby))

    warned = stack({"n": 20, "n_pass": 10, "n_warn": 10})
    check("warn still earns its own band", any(c == _C_WARN for c, _ in warned),
          str(warned))
    almost = stack({"n": 720, "n_pass": 1, "n_fail": 719})
    check("a 0.1% healthy share earns no green pixel",
          all(c != _C_OK for c, _ in almost), str(almost))

    # === the safety property ===
    # Excluding a status must never turn a blind spot green. A slice in which
    # every run errored or skipped has no verdict to report, and the one thing
    # it must not look like is a slice where nothing went wrong.
    blind = stack({"n": 0, "n_pass": 0, "n_excluded": 30})
    check("a slice with nothing but errors and skips is NOT green",
          all(c != _C_OK for c, _ in blind), str(blind))
    check("...it is the no-verdict grey", blind == [(_C_NONE, _BAR_PX)], str(blind))
    from backend.digest import bar_cells as _bc
    tip = _bc([{"n": 0, "n_pass": 0, "n_excluded": 30}])[0]["title"]
    check("...and says so, so it differs from a slice where nothing ran",
          "not counted" in tip, tip)

    # A subject with NO verdicts at all is now far more reachable than before,
    # because error and skip no longer count. The two ways to get there are
    # different statements and must not share a sentence.
    from backend.digest import Subject as _S, describe as _d
    _now = datetime.now(timezone.utc)
    blind_sub = _S(key="X", label="X"); blind_sub.excluded = 1440
    msg = _d(blind_sub, kind="radar", chronic_since=None, now=_now)
    check("a subject whose every run was excluded does not claim nothing ran",
          "no checks in window" not in msg, msg)
    check("...it says how many ran and that none were conclusive",
          "1,440" in msg and "nothing conclusive" in msg, msg)
    check("a subject that genuinely had no runs still says so",
          _d(_S(key="Y", label="Y"), kind="radar", chronic_since=None,
             now=_now) == "no checks in window")
    check("a bucket with no runs is the no-data grey",
          stack(None) == [(_C_NONE, _BAR_PX)])

    for label, b in (("mixed", {"n": 15, "n_pass": 0, "n_fail": 12, "n_error": 2, "n_skip": 1}),
                     ("warn", {"n": 20, "n_pass": 10, "n_warn": 10}),
                     ("rare", {"n": 720, "n_pass": 719, "n_error": 1})):
        tot = sum(px for _, px in stack(b))
        check(f"the {label} swatch fills exactly {_BAR_PX}px", tot == _BAR_PX, str(tot))

    # spark stays the share that PASSED — the same quantity as the green part
    # of a cell — so the text and HTML halves of one email cannot disagree.
    check("spark reads availability off the bucket",
          spark([{"avail": 100.0, "n": 5}, {"avail": 0.0, "n": 5}, None]) == "█▁·",
          spark([{"avail": 100.0, "n": 5}, {"avail": 0.0, "n": 5}, None]))
    check("spark still tolerates a bare float bucket",
          spark([100.0, 0.0, None]) == "█▁·", spark([100.0, 0.0, None]))

    # === Outlook renders the same bars as everyone else ===
    # Bins are stacked background-coloured table ROWS, not a CSS gradient. Word,
    # which is what Outlook renders through, has no gradients: the gradient
    # version collapsed to its bgcolor fallback and painted a 95%-healthy bin
    # solid red, which is the "worst of the bin" distortion the proportional
    # encoding exists to avoid. It stopped being an acceptable degradation once
    # nearly every recipient was on Outlook.
    tpl = (Path(__file__).resolve().parent.parent
           / "backend" / "templates" / "digest.html").read_text()
    check("no CSS gradient survives in the template",
          "linear-gradient" not in tpl)
    check("bins are drawn as bgcolor table rows", "bgcolor=\"{{ b.color }}\"" in tpl)

    _seed = {"window_label": "x", "since": "2026-09-13T13:00:00+00:00",
             "until": "2026-09-14T13:00:00+00:00", "runs": 100,
             "conclusive_pct": 100.0, "excluded": 0, "products": []}
    _mixed = {"key": "XR", "kind": "radar", "name": "A Site", "availability": 40.0,
              "description": "offline", "buckets":
              [{"avail": 40.0, "n": 30, "n_pass": 12, "n_fail": 12, "n_warn": 6}] * 8}
    _clean = dict(_mixed, key="XC", availability=100.0, description="nominal",
                  buckets=[{"avail": 100.0, "n": 30, "n_pass": 30}] * 8)
    mixed = render_html(dict(_seed, radars=[_mixed]), "https://x.test", compact=False)
    clean = render_html(dict(_seed, radars=[_clean]), "https://x.test", compact=False)
    check("a mixed bin draws a row per status, not one solid block",
          mixed.count("bgcolor") > clean.count("bgcolor"),
          f"mixed {mixed.count('bgcolor')} vs clean {clean.count('bgcolor')}")
    check("a single-colour bin stays one cell, which is what keeps the size down",
          clean.count("bgcolor") <= 10, str(clean.count("bgcolor")))

    # The stacked form costs ~4x the markup, so on a day where every bin is
    # mixed it passes Gmail's clip. Detail gives way; the report's tail does not.
    big = dict(_seed, radars=[dict(_mixed, key=f"XR{i:02d}") for i in range(6)],
               products=[dict(_mixed, key=f"p_{i:02d}", kind="product") for i in range(13)])
    auto = render_html(big, "https://x.test")
    comp = render_html(big, "https://x.test", compact=True)
    stacked = render_html(big, "https://x.test", compact=False)
    # === the unit is the encoded body, not the HTML ===
    # The HTML ships quoted-printable, which costs +11% on this markup. A
    # threshold measured on len(html) is therefore unsafe by that margin: the
    # old 95,000 allowed ~105,450 on the wire, past the clip, so the guard
    # could say "fine" while Gmail truncated the report.
    check("the compact threshold is stated in wire bytes, under the clip",
          _COMPACT_ABOVE_WIRE_BYTES < _GMAIL_CLIP_BYTES,
          f"{_COMPACT_ABOVE_WIRE_BYTES:,} vs clip {_GMAIL_CLIP_BYTES:,}")
    check("encoding really does inflate, so the distinction matters",
          _wire_len(stacked) > len(stacked) * 1.05,
          f"html {len(stacked):,} -> wire {_wire_len(stacked):,}")
    check("an all-mixed report would exceed the clip if left stacked",
          _wire_len(stacked) > _GMAIL_CLIP_BYTES, f"{_wire_len(stacked):,} on the wire")
    check("...so it falls back to compact automatically",
          len(auto) == len(comp), f"auto {len(auto):,} vs compact {len(comp):,}")
    check("...and the compact form ships well inside the clip",
          _wire_len(comp) < _GMAIL_CLIP_BYTES, f"{_wire_len(comp):,} on the wire")


    # --- size is a correctness constraint ------------------------------------
    # Gmail clips a body over ~102 KB and drops the tail silently, so a report
    # that grows past it loses its own conclusion. Rendering the swatches as
    # nested colour rows took production's email from 46 KB to 91 KB — inside
    # 11 KB of the threshold, which a busier day would have crossed.
    def row(k, kind):
        return {"key": k, "kind": kind, "name": "A Long Enough Site Name",
                "availability": 42.5, "description": "offline 9h 16m across 4 outages, "
                "longest 1h 06m · also brief, 26m total · 7 checks: reported online "
                "but sent nothing",
                "buckets": [{"avail": 10.0, "n": 30, "n_pass": 3, "n_fail": 20,
                             "n_error": 4, "n_warn": 2, "n_skip": 1}] * 8}
    worst = {
        "window_label": "Fri 11 Sep 07:00 MDT",
        "since": "2026-09-10T13:00:00+00:00", "until": "2026-09-11T13:00:00+00:00",
        "runs": 31986, "conclusive_pct": 99.4, "excluded": 183,
        "radars":   [row(f"XR{i:02d}", "radar") for i in range(6)],
        "products": [row(f"product_number_{i:02d}", "product") for i in range(13)],
    }
    worst_size = len(render_html(worst, "https://aqpi.local.shirejoe.com"))
    check("even an everything-is-broken report stays under Gmail's clip",
          worst_size < 95_000, f"{worst_size:,} bytes (clip at ~102,400)")

    # The worst case alone is a weak guard: production's real report doubled
    # from 46 KB to 91 KB and would still have passed it. A typical day —
    # roughly what 2026-09-11 looked like, 14 subjects worth listing — is the
    # number that actually moves when the markup regresses.
    # Shaped like a real day rather than a bad one. Production on 2026-09-14
    # had 19 subjects and 152 bins, of which 117 were a single colour, 28 were
    # two and 7 were three — healthy subjects dominate, and a single-colour bin
    # is a quarter of the markup. The previous fixture made every bin a full
    # mix, which is a fleet-wide outage, not a typical morning; it rendered at
    # 95.1 KB and tipped the compact fallback, so the assertion below was
    # measuring the wrong day.
    def _bins(n_mixed: int):
        mixed = {"avail": 40.0, "n": 30, "n_pass": 12, "n_fail": 12, "n_warn": 6}
        clean = {"avail": 100.0, "n": 30, "n_pass": 30}
        return [mixed] * n_mixed + [clean] * (8 - n_mixed)

    typical = dict(
        worst,
        radars=[dict(r, buckets=_bins(2 if i < 2 else 0))
                for i, r in enumerate(worst["radars"])],
        products=[dict(r, buckets=_bins(1 if i < 3 else 0))
                  for i, r in enumerate(worst["products"])],
    )
    typical_size = len(render_html(typical, "https://aqpi.local.shirejoe.com"))
    # Size alone stopped being the useful guard once render_html gained its
    # automatic compact fallback: nothing can ship above _COMPACT_ABOVE_BYTES
    # any more, so "is it small" is now true by construction. What can still
    # regress is the thing the fallback trades away — a normal day must keep
    # its proportional bars rather than quietly dropping to solid blocks,
    # because solid blocks are the Outlook rendering that prompted all this.
    #
    # Production on 2026-09-14 rendered 71.0 KB stacked. This fixture is
    # harsher (every bin a full mix, every description at full length), so it
    # sits higher; the assertion is that it still stays on the stacked side.
    typical_auto = render_html(typical, "https://aqpi.local.shirejoe.com")
    typical_stacked = render_html(typical, "https://aqpi.local.shirejoe.com", compact=False)
    check("a typical day keeps its proportional bins",
          len(typical_auto) == len(typical_stacked),
          f"auto {len(typical_auto):,} vs stacked {len(typical_stacked):,}")
    check("...and still lands inside the clip once encoded",
          _wire_len(typical_auto) < _GMAIL_CLIP_BYTES,
          f"{_wire_len(typical_auto):,} bytes on the wire")

    # Whatever render_html returns must fit once encoded, on every shape. This
    # is the property the whole fallback exists for, so it is asserted on the
    # thing that actually ships rather than on the HTML it is made from.
    for label, payload in (("typical", typical), ("worst", worst), ("all-mixed", big)):
        w = _wire_len(render_html(payload, "https://aqpi.local.shirejoe.com"))
        check(f"the {label} report fits on the wire", w < _GMAIL_CLIP_BYTES,
              f"{w:,} bytes encoded")

    # The blend replaced a four-step availability ramp that could not tell
    # 40% warn from 40% fail. Anchors stay exact; the mix stays monotone.
    check("an all-pass bin blends to exactly the pass colour",
          blend_color({"n": 10, "n_pass": 10}) == _C_OK.lower())
    check("an all-fail bin blends to exactly the fail colour",
          blend_color({"n": 10, "n_fail": 10}) == _C_BAD.lower())
    check("40% warn and 40% fail no longer look identical",
          blend_color({"n": 10, "n_pass": 6, "n_warn": 4})
          != blend_color({"n": 10, "n_pass": 6, "n_fail": 4}))
    reds = [int(blend_color({"n": 100, "n_pass": 100 - f, "n_fail": f})[1:3], 16)
            for f in range(0, 101, 10)]
    check("more failure always moves the swatch toward red",
          all(b > a for a, b in zip(reds, reds[1:])), str(reds))
    check("a bin with no verdict stays the no-data grey",
          blend_color({"n": 0, "n_excluded": 5}) == _C_NONE)

    # --- names -------------------------------------------------------------
    # "XSWR" tells you nothing unless you have five X-band call signs
    # memorised. The names come from the two tables that already hold this
    # vocabulary — RADAR_META and check_labels — so the report cannot drift
    # from the dashboard, which is the surface where a drift would go longest
    # without being noticed.
    check("a radar carries its site name", subject_title("radar", "XSWR") == "XSWR · Sawyer Ridge",
          subject_title("radar", "XSWR"))
    check("a product carries its product name",
          subject_title("product", "fcst_temp") == "fcst_temp · Forecast — Temperature",
          subject_title("product", "fcst_temp"))
    check("a NEXRAD named after itself is not repeated",
          subject_title("radar", "KBBX") == "KBBX", subject_title("radar", "KBBX"))
    check("an unknown subject degrades to its id rather than blank",
          subject_title("radar", "XNEW") == "XNEW" and subject_name("radar", "XNEW") == "")
    check("every X-band site resolves",
          all(subject_name("radar", r) for r in ("XSCV", "XSCW", "XSCR", "XEBY", "XSWR", "CBAND")))

    # The rendered text must actually show them — the helper being right is not
    # the same as the report using it.
    rendered = render_text({
        "window_label": "Fri 11 Sep 07:00 MDT", "since": "2026-09-10T13:00:00+00:00",
        "until": "2026-09-11T13:00:00+00:00", "runs": 0, "conclusive_pct": 100.0,
        "excluded": 0,
        "radars": [{"key": "XSWR", "kind": "radar", "name": "Sawyer Ridge",
                    "availability": 58.3, "buckets": [], "description": "offline 9h 16m"}],
        "products": [],
    })
    check("the rendered report names the site", "Sawyer Ridge" in rendered,
          [l for l in rendered.splitlines() if "XSWR" in l])

    # In HTML the two kinds lead with different halves: a radar's call sign is
    # the vocabulary the lab speaks, a product's id is not a word anyone says.
    _s = {"window_label": "x", "since": "2026-09-13T13:00:00+00:00",
          "until": "2026-09-14T13:00:00+00:00", "runs": 10,
          "conclusive_pct": 100.0, "excluded": 0}
    _b = [{"avail": 100.0, "n": 5, "n_pass": 5}] * 8
    both = render_html(dict(
        _s,
        radars=[{"key": "XSWR", "kind": "radar", "name": "Sawyer Ridge",
                 "availability": 100.0, "description": "nominal", "buckets": _b}],
        products=[{"key": "max_water_level", "kind": "product",
                   "name": "Max Water Level", "availability": 100.0,
                   "description": "nominal", "buckets": _b}]), "https://x.test")
    # Assert on which text sits in the BOLD span. Comparing string positions in
    # the whole document does not work: the id also appears earlier, inside the
    # evidence link's href, so an index test passes for the wrong reason.
    import re as _re2
    bold = _re2.findall(r'font:600 16px[^"]*">([^<]+)</span>', both)
    check("a radar leads with its id", "XSWR" in bold, str(bold))
    check("a product leads with its name", "Max Water Level" in bold, str(bold))
    # Products carry the name alone. Both together reached 59 characters for
    # "Forecast — Cumulative Precipitation · fcst_total_precip_cum", wide
    # enough to push the bars out of the column on that row.
    prod_cell = both[both.index("Products"):]
    check("a product's id is not printed beside its name",
          "&nbsp;· max_water_level" not in prod_cell, "id still trailing")
    check("...but the id still reaches the reader through the evidence link",
          "layer1.product.max_water_level" in prod_cell)
    check("a radar still carries both", "&nbsp;· Sawyer Ridge" in both)

    # Bins must share one box model — a row mixing single- and multi-colour
    # bins previously sat them at different heights and corner radii.
    tplsrc = (Path(__file__).resolve().parent.parent
              / "backend" / "templates" / "digest.html").read_text()
    import re as _re
    boxes = _re.findall(r'<td\{% if c\.title %\}[^>]*?%\}[^>]*>', tplsrc)
    geo = {_re.sub(r'bgcolor="[^"]*" ?', '', b) for b in boxes}
    check("every bin variant shares one geometry", len(geo) == 1,
          f"{len(boxes)} variants, {len(geo)} distinct geometries")
    check("...and that geometry pins height as well as width",
          all('height="16"' in b and "height:16px" in b for b in boxes))

    # The bar strip is right-aligned next to the availability figure, so if
    # that figure sizes to its text the bars shift by the width difference
    # between "0.0%" and "100.0%" and stop lining up down the column.
    avail_td = _re.search(r'<td[^>]*avail_color\(r\.availability\)[^>]*>', tplsrc)
    check("the availability cell is a fixed width, so the bars line up",
          avail_td is not None and 'width="54"' in avail_td.group(0)
          and "width:54px" in avail_td.group(0),
          avail_td.group(0)[:120] if avail_td else "cell not found")

    # --- evidence links ------------------------------------------------------
    # 2026-09-11: the report said qpe_15min/qpe_1hr/precip_rate_radar/comp_ref
    # were 99.5-99.8% over 24 h and every evidence link opened on a page of
    # unbroken green. These products run every ~60 s (~1,437 runs per window),
    # /history renders the newest 500, and all 19 non-passing runs were in the
    # truncated older end. The link disproved the claim it was meant to support.
    ev = evidence_url("https://s.example", "layer1.product.qpe_15min", "qpe_15min",
                      "2026-09-10T13:00:00+00:00", "2026-09-11T13:00:00+00:00")
    check("the evidence link filters to non-passing runs",
          "status=fail,error,warn,skip" in ev, ev)
    check("...and keeps the window and the subject",
          "check_id=layer1.product.qpe_15min" in ev
          and "since=2026-09-10T13:00:00Z" in ev
          and "until=2026-09-11T13:00:00Z" in ev, ev)
    check("...and stays short enough to read in the plain-text part",
          len(ev) < 200, f"{len(ev)} chars")
    check("no public_url means no link rather than a broken one",
          evidence_url("", "layer1.product.qpe_15min", "qpe_15min",
                       "2026-09-10T13:00:00+00:00", "2026-09-11T13:00:00+00:00") is None)

    print(f"\n{len(failures)} FAILED: {', '.join(failures)}" if failures
          else "\nall digest assertions passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
