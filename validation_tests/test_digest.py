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
    evidence_url, render_text, spark, subject_line,
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
    check("every swatch carries a tooltip",
          all(c["title"] for c in bar_cells([10, None, 99] + [50] * 5)))

    data = {
        "since": "2026-09-07T17:30:03+00:00", "until": "2026-09-08T17:30:03+00:00",
        "tz": "America/Denver", "window_label": "Tue 08 Sep 11:30 MDT",
        "radars": [{"key": "XEBY", "availability": 0.0, "buckets": [0] * 8,
                    "description": "offline the whole window — day 52"},
                   {"key": "CBAND", "availability": 100.0, "buckets": [100] * 8,
                    "description": "nominal"}],
        "products": [], "runs": 32035, "inconclusive": 130,
        "conclusive_pct": 99.6,
    }
    txt = render_text(data, "https://x.test")
    check("text report names the worst subject", "XEBY" in txt)
    check("a fully nominal subject collapses to one line",
          "1 nominal: CBAND" in txt, [l for l in txt.splitlines() if "nominal" in l])
    check("the coverage caveat is stated, not implied",
          "could not be judged" in txt)
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

    print(f"\n{len(failures)} FAILED: {', '.join(failures)}" if failures
          else "\nall digest assertions passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
