"""Daily-report signal selection — the worst-of-two-tiers merge, against real SQL.

Needs a Postgres (`make pg-up`); everything else in validation_tests/ runs
without one. That is deliberate and the whole point of this file: the merge
lives in SQL, so a Python double would test a re-implementation and not the
query that ships. Run it directly:

    make pg-up && ../.venv/bin/python validation_tests/test_digest_sources.py

It builds its own throwaway schema, fills it with a hand-computed day, and
drops it again, so it cannot see or disturb dev data.

Every radar and product can be observed two independent ways — `L*` asks the
radarca display tier what it believes, `LB*` reads the published files — and
this is the first thing in Sentinel to report them as ONE row. Five ways that
goes quietly wrong, all of them pinned below:

 1. COUNTING RUNS INSTEAD OF TIMESTEPS. Two tiers reporting the same minute is
    one observation of that minute. Summed as runs, a combined subject gets
    double the denominator of its neighbours and its availability stops being
    comparable to theirs — while still being printed in the same column.

 2. LOSING THE WORSE READING. The merge only earns its name if a passing tier
    cannot cover for a failing one. Both directions have to be tested: the
    bug that reads `frontend fail + backend pass` correctly can still take
    the frontend's word for it when the frontend is the happy one.

 3. ATTRIBUTING THE HEALTHY TIER'S SUB-CHECKS. A minute whose verdict was set
    by a backend `B_fresh` failure must not also be described with the
    frontend's `C_freshness` warning. Naming a condition that did not cause
    the verdict is how the report ends up confidently describing the wrong
    fault.

 4. CALLING A HALF-BLIND MINUTE INCONCLUSIVE. If one tier errors and the other
    reports cleanly, the minute IS conclusive — the error is a fact about one
    probe, not about the radar. Excluding it would quietly shrink the
    denominator exactly when half the instrumentation is broken.

 5. NEVER DISCOVERING A BACKEND-ONLY SUBJECT. The xqpi profile registers no
    radarca checks at all. A subject universe read from the configured source
    would report nothing there, and the report would arrive empty rather than
    wrong, which is harder to notice.
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("SENTINEL_DB_URL", "postgresql://unused/unused")

import asyncpg                                                 # noqa: E402

from backend import digest as D                                # noqa: E402
from backend.db.store import _init_codecs                      # noqa: E402

DSN = os.environ.get(
    "SENTINEL_TEST_DB_URL",
    "postgresql://sentinel:sentinel-dev@127.0.0.1:5432/sentinel",
)
SCHEMA = "digest_src_test"

failures: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if cond else 'FAIL'}  {label}" + (f"  — {detail}" if detail else ""))
    if not cond:
        failures.append(label)


def near(a, b, tol=0.01) -> bool:
    return a is not None and abs(a - b) <= tol


# ---------------------------------------------------------------------------
# One synthetic day, hand-computable
# ---------------------------------------------------------------------------
# A fixed window so every expected number below is arithmetic, not a
# re-derivation of the code under test. 239 timesteps of 360 s, the first one
# step INSIDE the window (the queries are half-open on the left, so a run
# exactly on `since` would silently not be counted) and the last one step
# short of its end — the backend tier reports 20 s after the frontend one, and
# a last timestep sitting exactly on `until` would push that run past the
# right edge, giving the two tiers different run counts for no reason the
# assertions below are about.
UNTIL = datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)
SINCE = UNTIL - timedelta(hours=24)
N = 239
STEP = 360


def t(i: int) -> datetime:
    return SINCE + timedelta(seconds=(i + 1) * STEP)


DDL = f"""
DROP SCHEMA IF EXISTS {SCHEMA} CASCADE;
CREATE SCHEMA {SCHEMA};
CREATE TABLE {SCHEMA}.check_runs (
  id            BIGSERIAL PRIMARY KEY,
  check_id      TEXT NOT NULL,
  target        TEXT NOT NULL,
  stage         TEXT NOT NULL,
  status        TEXT NOT NULL,
  started_at    TIMESTAMPTZ NOT NULL,
  finished_at   TIMESTAMPTZ NOT NULL,
  summary       TEXT,
  payload       JSONB
);
CREATE INDEX ON {SCHEMA}.check_runs (check_id, finished_at DESC);
"""


def rows() -> list[tuple]:
    out: list[tuple] = []

    def add(check_id, target, stage, status, when, summary=None, payload=None):
        out.append((check_id, target, stage, status, when, when, summary, payload))

    # The backend tier runs 20 s after the frontend one, inside the same 60 s
    # bin. That offset is the realistic case and the one that matters: the two
    # tiers do not share a clock, and a merge keyed on the raw timestamp would
    # never pair them at all.
    def fe(key, i, status, summary=None, payload=None):
        add(f"layer2.radar.{key}", key, "L2", status, t(i), summary, payload)

    def be(key, i, status, summary=None, payload=None):
        add(f"layer2.backend.{key}", key, "LB2", status,
            t(i) + timedelta(seconds=20), summary, payload)

    for i in range(N):
        # XEBY — both tiers, failing in DISJOINT stretches, so the combined
        # row can only be right if each tier's bad minutes survive the other
        # tier's pass. Frontend 100..109, backend 50..59.
        fe_bad = 100 <= i <= 109
        fe("XEBY", i, "fail" if fe_bad else "pass",
           "radarca says online, no scans → GHOST_UP" if fe_bad else "scan 61 s old")
        be_bad = 50 <= i <= 59
        be("XEBY", i, "fail" if be_bad else "pass",
           "newest volume 912 s old (limit 300 s)" if be_bad
           else "newest volume 74 s old")

        # CBAND — frontend only, one `skip` so the excluded counter has
        # something to carry.
        fe("CBAND", i, "skip" if i == 5 else "pass",
           None if i == 5 else "scan 55 s old",
           {"reason": "upstream-degraded"} if i == 5 else None)

        # XSCR — backend only. Must be discovered anyway, and its `warn`
        # stretch must land in the degraded counters, not the outage ones.
        be("XSCR", i, "warn" if 10 <= i <= 14 else "pass",
           "newest volume 420 s old (warn at 300 s)" if 10 <= i <= 14
           else "newest volume 66 s old")

        # XSCW — the half-blind case. The frontend probe errors for the first
        # half of the day while the backend reports cleanly throughout.
        fe("XSCW", i, "error" if i < 120 else "pass",
           "connection reset" if i < 120 else "scan 58 s old")
        be("XSCW", i, "pass", "newest volume 70 s old")

        # XSWR — dead all day on both tiers, and already dead when the window
        # opened (see the pre-window rows below).
        fe("XSWR", i, "fail", "radarca says offline → CONFIRMED_DOWN")
        be("XSWR", i, "fail", "newest volume 32104 s old (limit 300 s)")

        # Not a radar. A fleet-correlation verdict sits under the backend
        # radar prefix and would otherwise be reported as a radar called
        # "fleet" with its own availability.
        add("layer2.backend.fleet", "fleet", "LB2", "fail", t(i),
            "4 of 5 radars silent → systemic")

        # A product, to exercise the other family and the sub-check
        # attribution. Both tiers unhappy in the same stretch, at DIFFERENT
        # severities and for different reasons.
        add("layer1.product.qpe_15min", "qpe_15min", "L1",
            "warn" if i >= 200 else "pass", t(i),
            "manifest 19 m old" if i >= 200 else "manifest 2 m old",
            {"sub_status": {"C_freshness": "warn", "B_nonempty": "pass"}}
            if i >= 200 else None)
        add("layer1.backend.qpe_15min", "qpe_15min", "LB1",
            "fail" if i >= 200 else "pass", t(i) + timedelta(seconds=20),
            "newest file 2114 s old (limit 900 s)" if i >= 200
            else "newest file 118 s old",
            {"sub_status": {"B_fresh": "fail"}} if i >= 200 else None)

    # XSWR before the window. The newest reading on each side is a fail, so
    # the first episode abuts the window edge and must be reported from its
    # true start rather than from `since`. The two tiers last passed at
    # DIFFERENT times on purpose: the merge takes the LATER one, which is an
    # upper bound on when the worst-of was last healthy, so the stated
    # duration stays a lower bound.
    add("layer2.radar.XSWR", "XSWR", "L2", "pass",
        SINCE - timedelta(seconds=600), "scan 60 s old")
    add("layer2.backend.XSWR", "XSWR", "LB2", "pass",
        SINCE - timedelta(seconds=1200), "newest volume 70 s old")
    add("layer2.radar.XSWR", "XSWR", "L2", "fail",
        SINCE - timedelta(seconds=120), "radarca says offline → CONFIRMED_DOWN")
    add("layer2.backend.XSWR", "XSWR", "LB2", "fail",
        SINCE - timedelta(seconds=120), "newest volume 480 s old (limit 300 s)")
    return out


FE = ("layer2.radar.",)
BE = ("layer2.backend.",)
BOTH = ("layer2.radar.", "layer2.backend.")
BIN = D.COMBINE_BIN_S


async def main() -> None:
    try:
        conn = await asyncpg.connect(DSN, timeout=5)
    except Exception as e:                                  # noqa: BLE001
        print(f"\n  cannot reach Postgres at {DSN}: {e}")
        print("  start it with:  make pg-up")
        sys.exit(2)
    await conn.execute(DDL)
    await conn.close()

    # `server_settings`, NOT `SET search_path` in `init`. asyncpg runs
    # `RESET ALL` when a connection goes back to the pool, so a search_path
    # set in `init` survives exactly one acquire — after which every query
    # silently reads public.check_runs instead, which on a dev box is the real
    # database with the real radars in it. The first version of this file did
    # that and reported a seeded-row count of 99,369.
    pool = await asyncpg.create_pool(
        DSN, min_size=1, max_size=4, init=_init_codecs,
        server_settings={"search_path": SCHEMA})
    try:
        await pool.executemany(
            "INSERT INTO check_runs (check_id, target, stage, status,"
            " started_at, finished_at, summary, payload)"
            " VALUES ($1,$2,$3,$4,$5,$6,$7,$8)", rows())
        n = await pool.fetchval("SELECT count(*) FROM check_runs")
        print(f"\nseeded {n} runs into schema {SCHEMA}\n")
        await run_assertions(pool)
    finally:
        await pool.close()
        conn = await asyncpg.connect(DSN, timeout=5)
        await conn.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        await conn.close()


async def run_assertions(pool) -> None:
    # -- the single-family path is unchanged -------------------------------
    # Passing one prefix with bin_s = 0 makes the timestep the run's own
    # timestamp, so the collapse groups one row at a time. If that is not a
    # no-op, every existing number in the report moved.
    print("single family (what the report did before this existed)")
    subs = await D._subject_rows(pool, SINCE, UNTIL, FE, 0)
    x = subs["XEBY"]
    check("XEBY/frontend judged = 239 runs", x.judged == 239, str(x.judged))
    check("XEBY/frontend passed = 229", x.passed == 229, str(x.passed))
    check("XEBY/frontend one sustained outage", len(x.outages) == 1,
          f"{len(x.outages)} outages, {x.blips} blips")
    check("XEBY/frontend outage is 10 runs long",
          x.outages and x.outages[0]["runs"] == 10)
    check("XEBY/frontend outage measured to the recovering run",
          x.outages and near(x.outages[0]["seconds"], 3600),
          str(x.outages[0]["seconds"]) if x.outages else "")
    check("CBAND counts its skip as excluded, not as healthy",
          subs["CBAND"].excluded == 1 and subs["CBAND"].judged == 238,
          f"excluded={subs['CBAND'].excluded} judged={subs['CBAND'].judged}")
    check("a fleet verdict is not a radar", "fleet" not in subs,
          ",".join(sorted(subs)))
    check("a backend-only radar has no frontend row", "XSCR" not in subs,
          ",".join(sorted(subs)))

    # -- the merge ---------------------------------------------------------
    print("\ncombined — the worse of the two, per timestep")
    csubs = await D._subject_rows(pool, SINCE, UNTIL, BOTH, BIN)
    c = csubs["XEBY"]
    check("denominator counts timesteps, not the 478 runs in them",
          c.judged == 239, str(c.judged))
    check("both tiers' bad minutes survive the other tier's pass",
          c.passed == 219, str(c.passed))
    check("availability is 91.6%, not either tier's 95.8%",
          near(c.availability, 100.0 * 219 / 239, 0.01), f"{c.availability:.3f}")
    check("two sustained outages", len(c.outages) == 2,
          f"{len(c.outages)} outages, {c.blips} blips")
    starts = sorted(o["started"] for o in c.outages)
    check("frontend pass + backend fail → combined fail",
          bool(starts) and starts[0] == t(50), str(starts[:1]))
    check("frontend fail + backend pass → combined fail",
          len(starts) > 1 and starts[1] == t(100), str(starts[1:]))
    check("each outage still measures 1 h of wall clock",
          all(near(o["seconds"], 3600) for o in c.outages),
          str([o["seconds"] for o in c.outages]))

    print("\ncombined — a subject that exists on one tier only")
    r = csubs["XSCR"]
    check("backend-only radar is reported", r.judged == 239, str(r.judged))
    check("its warn stretch is degraded, not an outage",
          len(r.degraded) == 1 and not r.outages,
          f"{len(r.degraded)} degraded, {len(r.outages)} outages")
    check("degraded span is 5 timesteps of 360 s",
          r.degraded and near(r.degraded[0]["seconds"], 1800),
          str(r.degraded[0]["seconds"]) if r.degraded else "")
    w = csubs["XSCW"]
    check("the half-blind radar reads 100%, from the tier that could see",
          w.judged == 239 and near(w.availability, 100.0),
          f"judged={w.judged} avail={w.availability}")

    print("\ncombined — an episode already running when the window opened")
    s = csubs["XSWR"]
    check("one outage covering the whole window", len(s.outages) == 1,
          f"{len(s.outages)} outages")
    ep = s.outages[0] if s.outages else {}
    check("flagged as truncated by the window edge", bool(ep.get("truncated")))
    check("true start is the LATER tier's last pass (a lower-bound duration)",
          ep.get("true_started") == SINCE - timedelta(seconds=600),
          str(ep.get("true_started")))

    # -- sub-check attribution --------------------------------------------
    print("\nsub-check names follow the reading that set the verdict")
    psubs = await D._subject_rows(pool, SINCE, UNTIL,
                                  D._FAMILIES["product"]["combined"], BIN)
    q = psubs["qpe_15min"]
    check("the failing tier's condition is named",
          q.fail_subs == {"B_fresh"}, str(sorted(q.fail_subs)))
    check("the passed-over warning is NOT attributed to the fail",
          "C_freshness" not in q.fail_subs and not q.warn_subs,
          f"fail={sorted(q.fail_subs)} warn={sorted(q.warn_subs)}")
    check("passing runs' sub_status is not expanded into either set",
          "B_nonempty" not in q.fail_subs | q.warn_subs)
    check("the fail stretch runs to the window edge",
          q.outages and near(q.outages[0]["seconds"], 39 * STEP),
          str(q.outages[0]["seconds"]) if q.outages else "")
    pfe = await D._subject_rows(pool, SINCE, UNTIL,
                                 D._FAMILIES["product"]["frontend"], 0)
    check("read frontend-only, the same product is merely degraded",
          pfe["qpe_15min"].warn_subs == {"C_freshness"}
          and not pfe["qpe_15min"].outages,
          str(sorted(pfe["qpe_15min"].warn_subs)))

    # -- verdict vocabulary ------------------------------------------------
    print("\nnamed verdicts come from the tier that has them")
    vmap = await D._verdicts(pool, SINCE, UNTIL, BOTH)
    check("combined XEBY keeps its 10 GHOST_UP runs",
          vmap.get("XEBY", {}).get("GHOST_UP") == 10,
          str(vmap.get("XEBY")))
    vbe = await D._verdicts(pool, SINCE, UNTIL, BE)
    check("the backend tier has no verdict vocabulary at all",
          not vbe.get("XEBY"), str(vbe.get("XEBY")))

    # -- buckets -----------------------------------------------------------
    print("\nbuckets — a half-blind minute is still a conclusive minute")
    bfe = await D._buckets(pool, SINCE, UNTIL, FE, 0)
    b0 = bfe["XSCW"][0]
    check("frontend-only: the errored slice judges nothing",
          b0 and b0["n"] == 0 and b0["avail"] is None,
          str(b0))
    check("frontend-only: and says how much it could not see",
          b0 and b0["n_excluded"] == 29, str(b0))
    bc = await D._buckets(pool, SINCE, UNTIL, BOTH, BIN)
    c0 = bc["XSCW"][0]
    check("combined: the same slice is 100% from the readable tier",
          c0 and c0["n"] == 29 and near(c0["avail"], 100.0), str(c0))
    check("combined: nothing is left unjudged",
          c0 and c0["n_excluded"] == 0, str(c0))
    tot = sum(b["n"] for b in bc["XEBY"] if b)
    check("every timestep lands in exactly one slice", tot == N, str(tot))
    check("no slice double-counts the two tiers",
          all(b is None or b["n"] <= 30 for b in bc["XEBY"]),
          str([b["n"] if b else None for b in bc["XEBY"]]))

    # -- subject discovery -------------------------------------------------
    print("\nsubject discovery reads both tiers")
    keys = await D._subject_keys(pool, SINCE, UNTIL, "radar")
    check("every radar is found, whichever tier it lives on",
          keys == {"XEBY", "CBAND", "XSCR", "XSCW", "XSWR"},
          ",".join(sorted(keys)))
    check("the fleet verdict is not in the universe either",
          "fleet" not in keys)

    # -- compute(), as the report sees it ---------------------------------
    print("\ncompute() honours the per-subject configuration")
    cfg = {
        "default_source": {"radar": "frontend", "product": "combined"},
        "subjects": {
            "radar:XEBY":  {"source": "combined"},
            "radar:XSCR":  {"source": "backend"},
            "radar:CBAND": {"show": False},
            "radar:XSCW":  {"source": "nonsense"},
        },
    }
    data = await D.compute(pool, since=SINCE, until=UNTIL, tz="UTC", cfg=cfg)
    by = {r["key"]: r for r in data["radars"]}
    check("a hidden subject is absent entirely", "CBAND" not in by,
          ",".join(sorted(by)))
    check("XEBY is built from the combined signal",
          by.get("XEBY", {}).get("source") == "combined"
          and near(by["XEBY"]["availability"], 100.0 * 219 / 239, 0.01),
          f"{by.get('XEBY', {}).get('source')} "
          f"{by.get('XEBY', {}).get('availability')}")
    check("XSCR is built from the backend signal",
          by.get("XSCR", {}).get("source") == "backend",
          str(by.get("XSCR", {}).get("source")))
    check("an unknown source falls back to frontend, not to a crash",
          by.get("XSCW", {}).get("source") == "frontend",
          str(by.get("XSCW", {}).get("source")))
    check("the row says which signal it came from, for every row",
          all(r["source"] in D.SOURCES for r in data["radars"]))
    avails = [r["availability"] if r["availability"] is not None else 101
              for r in data["radars"]]
    check("rows are still worst-first across the mixed sources",
          avails == sorted(avails), str([round(a, 1) for a in avails]))
    check("the product kind takes its own default",
          data["products"] and data["products"][0]["source"] == "combined",
          str([(p["key"], p["source"]) for p in data["products"]]))
    check("a dead radar still gets its chronic-since date",
          by.get("XSWR", {}).get("chronic_since", "").startswith("2026-10-09T11:58"),
          str(by.get("XSWR", {}).get("chronic_since")))

    # The pre-v0.8 behaviour, reachable by passing no config at all: every
    # subject from its kind's default. Kept as an assertion because an
    # existing deployment upgrades into exactly this state.
    print("\ncompute() with no configuration reproduces the old report")
    plain = await D.compute(pool, since=SINCE, until=UNTIL, tz="UTC", cfg=None)
    pby = {r["key"]: r for r in plain["radars"]}
    check("defaults to the profile's source for every radar",
          all(r["source"] == D.DEFAULTS["default_source"]["radar"]
              for r in plain["radars"]))
    check("nothing is hidden by default",
          set(pby) == {"XEBY", "CBAND", "XSCW", "XSWR"},
          ",".join(sorted(pby)))
    check("and the frontend numbers match the single-family query",
          near(pby["XEBY"]["availability"], 100.0 * 229 / 239),
          str(pby["XEBY"]["availability"]))
    # XSCR is the one that goes missing: it exists only on the backend tier,
    # and this profile's default asks the frontend. That is why the default
    # is derived from HAS_RADARCA rather than hard-coded — see DEFAULTS.
    check("a backend-only radar is absent when the default asks the frontend",
          "XSCR" not in pby and "XSCR" in await D._subject_keys(
              pool, SINCE, UNTIL, "radar"))

    # -- rendering ---------------------------------------------------------
    print("\nthe rendered report survives mixed sources")
    text = D.render_text(data, "https://example.invalid")
    check("every shown radar appears in the text body",
          all(k in text for k in ("XEBY", "XSCR", "XSWR")))
    check("the hidden one does not", "CBAND" not in text)
    html = D.render_html(data, "https://example.invalid")
    check("html renders without raising", len(html) > 1000, str(len(html)))
    # One dead radar (XSWR, 0%) and two degraded rows — XEBY at 91.6% from
    # the combined signal and the product at 83.7%. XSCR at 97.9% is neither,
    # which is the point: the counts are taken from the configured source of
    # each row, not from one tier for all of them.
    check("the subject line counts across the mixed sources",
          D.subject_line(data) == "Sentinel — 1 down, 2 degraded",
          D.subject_line(data))


if __name__ == "__main__":
    asyncio.run(main())
    print()
    if failures:
        print(f"{len(failures)} FAILED:")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    print("all assertions passed")
