#!/usr/bin/env python3
"""LB3 — composite participation and the DROPS producer.

The parser is the risky part of this stage, so it is tested against the
receipt EXACTLY as it exists on cira-aqpi, double slash and all:

    Radar files for composite:
    /trinity/.../recentfiles//XSCR/XSCR_volume_20261005-163823_drops.nc
    /trinity/.../recentfiles//SSCB/AQPI.SSCB_20261005_163719_drops.nc

Three things in that one file would each break a plausible parser:

  * the radar is spelled three ways across the naming conventions
    (``XSCR_volume``, ``AQPI.SSCB``, ``cfrad_KSOX``) while the DIRECTORY is
    uniform — so identity comes from the directory, not the filename;
  * ``recentfiles//XSCR`` carries a real double slash, because the writer
    concatenates a path that already ends in "/";
  * the timestamp separator differs by convention (``-`` for X-band, ``_`` for
    C-band and NEXRAD).

And the file is truncate-then-append with no rename, so a torn read is
reachable in normal operation and biased: the appends run X-band, then NEXRAD,
then SSCB, so a torn read systematically drops CBAND. A check that trusted one
would invent "CBAND absent from the composite" forever.

Run:  python3 validation_tests/test_backend_processing.py
"""
from __future__ import annotations
import asyncio
import os
import time as _time
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
_TMP = tempfile.mkdtemp(prefix="sentinel-lb3-")
os.environ.setdefault("SENTINEL_DB_URL", "postgresql://unused/unused")
os.environ["SENTINEL_BACKEND_ROOT"] = _TMP
os.environ.setdefault("SENTINEL_SSCB_ROOT", "/sscb")

from backend.checks import layer3_backend_processing as lb3   # noqa: E402
from backend.config import (COMPOSITE_EXPECTED_RADARS,         # noqa: E402
                            COMPOSITE_RECEIPT, RADAR_FOLDER,
                            RADAR_SILENT_FAIL_S)

failures: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if cond else 'FAIL'}  {label}" + (f"  — {detail}" if detail else ""))
    if not cond:
        failures.append(label)


PREFIX = "/trinity/projects/aqpiqpedev/PRODUCTS/Radar_processed/recentfiles/"


def line(rdir: str, name: str) -> str:
    # The double slash is deliberate and real: PREFIX already ends in "/".
    return f"{PREFIX}/{rdir}/{name}"


def stamp(dt: datetime, sep: str = "-") -> str:
    return dt.strftime(f"%Y%m%d{sep}%H%M%S")


def write_receipt(lines: list[str], mtime_age_s: float = 60.0) -> str:
    p = os.path.join(_TMP, COMPOSITE_RECEIPT)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w") as fh:
        fh.write("Radar files for composite:\n")
        for ln in lines:
            fh.write(ln + "\n")
    # Back-date so the settle window does not make every test sleep 5s.
    t = time.time() - mtime_age_s
    os.utime(p, (t, t))
    lb3._reset_receipt_cache()
    return p


def receipt_now(ages_min: dict[str, float]) -> list[str]:
    """One line per radar, each aged by the given number of minutes."""
    out = []
    now = datetime.now(timezone.utc)
    for rdir, age in ages_min.items():
        dt = datetime.fromtimestamp(now.timestamp() - age * 60, timezone.utc)
        if rdir == "SSCB":
            out.append(line(rdir, f"AQPI.SSCB_{stamp(dt, '_')}_drops.nc"))
        else:
            out.append(line(rdir, f"{rdir}_volume_{stamp(dt, '-')}_drops.nc"))
    return out


RUN_LOG_DIR = "PRODUCTS/Composite_QPE/log_SRI"


def radar_block(rdir: str, age_s: float, *, dims: bool = True,
                run_dt: datetime | None = None, derived_skew_s: float = 0.0,
                reported_age_s: float | None = None) -> str:
    """One RADAR block, in the exact shape the composite writes.

    `reported_age_s` lets a test make secondsStarttoEnd disagree with what the
    scan start implies, which is the condition the cross-check exists for.
    """
    run_dt = run_dt or datetime.now(timezone.utc)
    start = datetime.fromtimestamp(run_dt.timestamp() - age_s + derived_skew_s,
                                   timezone.utc)
    end = run_dt
    secs = int(round(reported_age_s if reported_age_s is not None else age_s))
    name = (f"AQPI.SSCB_{start.strftime('%Y%m%d_%H%M%S')}_drops.nc"
            if rdir == "SSCB"
            else f"{rdir}_volume_{start.strftime('%Y%m%d-%H%M%S')}_drops.nc")
    out = [f"************************** RADAR: {rdir} **************************",
           f"filename: {PREFIX}/{rdir}/{name}"]
    if dims:
        out.append("GetNetCDFdim: #radials = 2719  #gates (rangebins) = 675  #sweeps = 4")
    out += ["radar height = 608.7m",
            "radar lon, lat = -122.062,37.8156",
            f"startDateTimeScan  = {start.strftime('%Y-%m-%dT%H:%M:%S')}Z",
            f"endDateTimeScan    = {end.strftime('%Y-%m-%dT%H:%M:%S')}Z",
            f"secondsStarttoEnd = -{secs}"]
    return "\n".join(out) + "\n"


def write_run_logs(runs: list[tuple[datetime, str]]) -> str:
    """Write one per-run file per (run time, body). Returns the directory."""
    d = os.path.join(_TMP, RUN_LOG_DIR)
    os.makedirs(d, exist_ok=True)
    for f in os.listdir(d):
        os.remove(os.path.join(d, f))
    for run_dt, body in runs:
        fn = f"composite_SRI_startproc_{run_dt.strftime('%Y%m%d-%H%M%S')}.txt"
        with open(os.path.join(d, fn), "w") as fh:
            fh.write("composite run\n" + body)
    lb3._reset_receipt_cache()
    return d


def drop_run_logs() -> None:
    d = os.path.join(_TMP, RUN_LOG_DIR)
    if os.path.isdir(d):
        for f in os.listdir(d):
            os.remove(os.path.join(d, f))
        os.rmdir(d)
    lb3._reset_receipt_cache()


def run(rid: str):
    return asyncio.run(
        lb3.Layer3CompositeParticipationCheck(radar_id=rid).run(None))


def main() -> int:
    print("the receipt as it actually exists — three naming conventions, one parser:")
    real = [
        line("XSCR", "XSCR_volume_20261005-163823_drops.nc"),
        line("XSCV", "XSCV_volume_20261005-163941_drops.nc"),
        line("XSWR", "XSWR_volume_20261005-163928_drops.nc"),
        line("SSCB", "AQPI.SSCB_20261005_163719_drops.nc"),
    ]
    p = write_receipt(real)
    parsed = lb3._read_receipt_blocking(p)
    got = parsed["contributions"]
    check("all four radars parsed", sorted(got) == ["SSCB", "XSCR", "XSCV", "XSWR"],
          str(sorted(got)))
    check("nothing was left unparsed", not parsed["unparsed"], str(parsed["unparsed"]))
    check("the double slash in recentfiles// did not break identity",
          "XSCR" in got, str(sorted(got)))
    check("the C-band underscore separator parsed as a timestamp",
          "SSCB" in got and got["SSCB"]["declared_ts"] > 0)
    # 16:38:23Z on 2026-10-05
    want = datetime(2026, 10, 5, 16, 38, 23, tzinfo=timezone.utc).timestamp()
    check("...and the X-band timestamp is read as UTC, to the second",
          abs(got["XSCR"]["declared_ts"] - want) < 1,
          f"{got['XSCR']['declared_ts']} vs {want}")
    want_c = datetime(2026, 10, 5, 16, 37, 19, tzinfo=timezone.utc).timestamp()
    check("...and so is the C-band one",
          abs(got["SSCB"]["declared_ts"] - want_c) < 1)
    check("identity comes from the DIRECTORY, not the filename",
          got["SSCB"]["path"].endswith("AQPI.SSCB_20261005_163719_drops.nc"),
          "SSCB's filename says AQPI.SSCB; its directory says SSCB")

    print("\nthe NEXRAD convention parses too, though it is not in the expected set:")
    p = write_receipt([line("KSOX", "cfrad_KSOX_20260917_155622_drops.nc")])
    got2 = lb3._read_receipt_blocking(p)["contributions"]
    check("cfrad_<RID>_<date>_<time> parses", "KSOX" in got2, str(sorted(got2)))
    check("...and is NOT in COMPOSITE_EXPECTED_RADARS",
          "KSOX" not in COMPOSITE_EXPECTED_RADARS,
          "the three NEXRAD trees do not exist; see config")

    print("\nCBAND's receipt directory is SSCB — not derivable from either name:")
    check("config maps CBAND -> SSCB",
          COMPOSITE_EXPECTED_RADARS.get("CBAND") == "SSCB",
          str(COMPOSITE_EXPECTED_RADARS.get("CBAND")))
    check("...and RADAR_FOLDER disagrees with both",
          RADAR_FOLDER["CBAND"] == "sscb",
          "three spellings of one radar: CBAND / SSCB / sscb")

    print("\na radar absent from the receipt fails — this is XEBY on 2026-10-05:")
    write_receipt(receipt_now({"XSCR": 3, "XSCV": 3, "XSWR": 3, "SSCB": 5}))
    r = run("XEBY")
    check("XEBY reports fail", r.status == "fail", r.status)
    check("...with A_included = fail", r.payload["sub_status"]["A_included"] == "fail")
    # Wording follows the SOURCE. On the receipt fallback the strongest claim
    # available is "offered to", because that file records only what
    # `ls | tail -1` selected. Claiming "not in the composite" on it would be
    # overstating the evidence in the other direction.
    check("...and says so in words, scoped to what the receipt can support",
          r.summary.startswith("NOT OFFERED TO COMPOSITE"), r.summary)
    check("...and records which source answered",
          r.payload["participation_source"] == "receipt",
          str(r.payload.get("participation_source")))
    check("...and names who IS present, so the row is actionable",
          "XSCR" in r.summary and "SSCB" in r.summary, r.summary)
    check("...and records included=0 as a metric",
          r.metrics.get("included") == 0.0)

    print("\na radar present in the receipt passes, on presence alone:")
    r = run("XSCR")
    check("XSCR passes", r.status == "pass", f"{r.status}: {r.summary}")
    # B_fresh is deliberately NOT assessed from the receipt. The two sources
    # have different zero points -- the run log references the run's TARGET
    # time, the receipt only its own mtime, which is written at processing
    # START about one cycle later. Measured, receipt-derived ages run
    # +102..+148 s above log-derived ages for the same radars. Banding both
    # against one 600/900 would make the fallback silently ~115 s stricter, so
    # a row would drift toward warn purely because the check fell back.
    check("...with A_included only — no B_fresh on this basis",
          r.payload["sub_status"] == {"A_included": "pass"},
          str(r.payload["sub_status"]))
    check("...recorded as a basis, not as a missing value",
          r.payload["age_basis"] == "not_assessed_on_receipt",
          str(r.payload.get("age_basis")))
    check("...and the summary says WHY, so the row is not just quieter",
          "reference time" in r.summary, r.summary)
    check("...and no age or headroom is published from a basis we cannot band",
          r.payload.get("age_s") is None and r.metrics.get("headroom") is None,
          f"age_s={r.payload.get('age_s')} headroom={r.metrics.get('headroom')}")

    print("\nONE uniform band, because a per-radar band is blind where it matters:")
    # This block asserted the opposite until 2026-10-05: bands derived from
    # each radar's own silence limit. Measured over 1,440 composite runs that
    # rule can NEVER fire on XSCR (max contribution age 403 s against a
    # 1,080 s band) while firing on 25% of XSCW's runs. Inverted rather than
    # deleted, so the derivation cannot quietly come back.
    w_x, f_x = lb3._contrib_bands("XEBY")
    w_c, f_c = lb3._contrib_bands("CBAND")
    check("every radar gets the same band", (w_x, f_x) == (w_c, f_c),
          f"XEBY {w_x:.0f}/{f_x:.0f} vs CBAND {w_c:.0f}/{f_c:.0f}")
    check("...which is NOT derived from the radar's silence limit",
          abs(f_c - f_x) < 1 and RADAR_SILENT_FAIL_S["CBAND"] != RADAR_SILENT_FAIL_S["XEBY"],
          "silence limits differ by 780s; the bands do not differ at all")
    check("warn 600s / fail 900s, the thinnest gap in the pooled distribution",
          (w_x, f_x) == (600.0, 900.0), f"{w_x:.0f}/{f_x:.0f}")
    # The band has to be able to fire on the radar that is never late, or it is
    # not measuring that radar at all. XSCR's observed max is 403s.
    check("...and 900s is reachable by XSCR, whose observed max is 403s",
          f_x > 403, f"{f_x:.0f}s > 403s")
    # ...and clear of the healthy mode: p50 111-320s, well-behaved p90 204-408s.
    check("...while sitting clear of the healthy mode (p90 up to 408s)",
          w_x > 408, f"warn {w_x:.0f}s > 408s")

    print("\na stale contribution is NOT banded from the receipt:")
    # This section used to assert warn/fail bands against receipt-derived
    # ages. That is exactly the comparison the measurement above rules out, so
    # it now asserts the opposite: the receipt answers presence and declines
    # the freshness question. The warn/fail bands are exercised against the
    # run log instead, further down, which is the basis they were fitted to.
    _, fail_s = lb3._contrib_bands("XSCR")
    write_receipt(receipt_now({"XSCR": (fail_s + 600) / 60}))
    r = run("XSCR")
    check("a wildly stale contribution still reports pass from the receipt",
          r.status == "pass", f"{r.status}: {r.summary}")
    check("...because presence is all this source can answer",
          "B_fresh" not in r.payload["sub_status"], str(r.payload["sub_status"]))
    check("...which is a weaker question, not a wrong answer",
          r.payload["sub_status"]["A_included"] == "pass",
          "degrade the question, never the calibration")

    print("\nan absent receipt is a fail, and says so plainly:")
    pth = os.path.join(_TMP, COMPOSITE_RECEIPT)
    os.rename(pth, pth + ".gone")
    lb3._reset_receipt_cache()
    r = run("XSCR")
    check("receipt absent -> fail", r.status == "fail", r.status)
    check("...flagged in payload", r.payload.get("receipt_absent") is True)
    check("...and not reported as an error", r.status != "error",
          "the composite not running is a fault, not a failure to observe")
    os.rename(pth + ".gone", pth)
    lb3._reset_receipt_cache()

    print("\nthe torn read — reachable in normal operation, and biased:")
    # Truncate-then-append means a read can see the header plus the X-bands but
    # not yet SSCB. The whole point: that must NOT read as "CBAND absent".
    torn = receipt_now({"XSCR": 3, "XSCV": 3, "XSWR": 3})   # SSCB not yet appended
    write_receipt(torn)
    r = run("CBAND")
    check("a torn receipt still reports CBAND absent if we trust it",
          r.payload["sub_status"]["A_included"] == "fail",
          "this is the FAILURE MODE the mtime bracket exists to prevent")

    # Now prove the bracket catches a real tear: rewrite the file mid-read by
    # hooking open() so the content changes between the two stat() calls.
    full = receipt_now({"XSCR": 3, "XSCV": 3, "XSWR": 3, "SSCB": 5})
    p = write_receipt(full)
    real_open = open
    state = {"n": 0}

    def tearing_open(*a, **k):
        fh = real_open(*a, **k)
        state["n"] += 1
        if state["n"] == 1:
            # Append after the read has taken its first stat, and bump mtime.
            with real_open(p, "a") as w:
                w.write(line("SSCB", "AQPI.SSCB_20261005_170000_drops.nc") + "\n")
            os.utime(p, None)
        return fh

    lb3._reset_receipt_cache()
    import builtins
    builtins.open = tearing_open
    try:
        caught = False
        try:
            lb3._read_receipt_blocking(p)
        except lb3.TornReceipt:
            caught = True
    finally:
        builtins.open = real_open
    check("a receipt rewritten mid-read raises TornReceipt rather than parsing",
          caught, "detected via stat() bracket, not guessed from content")

    print("\nthe settle window covers the tear the stat bracket cannot:")
    # Between two appends the mtime is momentarily STABLE, so a read that
    # starts and finishes in that gap sees a short file with nothing changing
    # under it — the bracket is blind to this one. The settle window is what
    # catches it, by refusing to read a receipt that was just written.
    short = receipt_now({"XSCR": 3, "XSCV": 3, "XSWR": 3})   # SSCB not yet appended
    p2 = write_receipt(short, mtime_age_s=0.2)               # as if mid-write
    t_start = time.time()
    lb3._read_receipt_blocking(p2)
    waited = time.time() - t_start
    check("a receipt written 0.2 s ago is waited out, not read immediately",
          waited >= 4.0, f"waited {waited:.1f}s for a {4.8:.1f}s remaining window")
    p3 = write_receipt(short, mtime_age_s=60.0)
    t_start = time.time()
    lb3._read_receipt_blocking(p3)
    check("...while a settled receipt is read without waiting",
          time.time() - t_start < 1.0, f"{time.time() - t_start:.2f}s")

    print("\nevery radar in a cycle sees ONE receipt version:")
    # Otherwise XEBY could be judged against one composite and CBAND the next,
    # and the reported set would be stitched from two composites.
    write_receipt(receipt_now({"XSCR": 3, "SSCB": 5}))
    seen = [tuple(run(r).payload.get("receipt_radars") or [])
            for r in ("XSCR", "CBAND", "XSCV")]
    check("all three radars report the same receipt contents",
          len(set(seen)) == 1, str(seen))
    check("...and the cache key is the receipt's own (mtime, size)",
          lb3._receipt_cache is not None, "parsed once, read by all")

    print("\nthe PRIMARY source: the composite's own per-run log")
    # Written once per run, 120 s apart, so the second-newest is always
    # complete. That retires the torn-read problem rather than defending
    # against it.
    now = datetime.now(timezone.utc)
    older = datetime.fromtimestamp(now.timestamp() - 120, timezone.utc)
    complete = "".join(radar_block(r, a, run_dt=older) for r, a in
                       [("XEBY", 200), ("XSCR", 180), ("XSCV", 150),
                        ("XSCW", 210), ("XSWR", 190), ("SSCB", 300)])
    # The NEWEST file is deliberately short, as a file still being written
    # would be. Nothing may read it.
    partial = radar_block("XEBY", 60, run_dt=now)
    write_run_logs([(older, complete), (now, partial)])

    r = run("XSCV")
    check("the run log is preferred over the receipt",
          r.payload["participation_source"] == "run_log",
          str(r.payload.get("participation_source")))
    check("...and the SECOND-newest run is the one read",
          r.payload["run_log"].endswith(
              f"composite_SRI_startproc_{older.strftime('%Y%m%d-%H%M%S')}.txt"),
          os.path.basename(r.payload["run_log"]))
    check("...so a half-written newest file cannot be read",
          sorted(r.payload["receipt_radars"]) ==
          ["SSCB", "XEBY", "XSCR", "XSCV", "XSCW", "XSWR"],
          "6 radars from the complete file, not 1 from the partial one")
    check("XSCV passes", r.status == "pass", f"{r.status}: {r.summary}")
    check("...and now claims IN the composite, not merely offered to it",
          r.summary.startswith("in composite"), r.summary)
    check("...on the strength of logged netCDF dimensions",
          r.payload["age_basis"] == "composite_reported", str(r.payload))

    print("\nthe age comes from the composite, and is cross-checked:")
    check("age is the composite's own secondsStarttoEnd",
          abs(r.payload["age_s"] - 150) < 2, str(r.payload["age_s"]))
    check("...and an independently derived age is carried alongside",
          abs(r.payload["derived_age_s"] - 150) < 2,
          str(r.payload.get("derived_age_s")))
    check("...which agrees, so no disagreement is flagged",
          not r.payload.get("age_disagrees"), str(r.payload.get("age_disagrees")))
    # The field is LABELLED start-minus-end-of-scan, which read literally is a
    # scan duration. If it ever stops behaving as an age the check must say so
    # rather than silently band the wrong quantity.
    write_run_logs([(older, radar_block("XSCV", 150, run_dt=older,
                                        reported_age_s=9000))])
    r = run("XSCV")
    check("a secondsStarttoEnd that disagrees with the scan start is flagged",
          r.payload.get("age_disagrees") is True, str(r.payload.get("age_disagrees")))
    check("...and the disagreement is visible in the summary",
          "age fields disagree" in r.summary, r.summary)

    print("\nnamed but not read is a THIRD state, which the receipt could not see:")
    write_run_logs([(older, radar_block("XSCV", 150, run_dt=older, dims=False)
                     + radar_block("XSCR", 150, run_dt=older))])
    r = run("XSCV")
    check("a block with no dimensions fails", r.status == "fail", r.status)
    check("...and says NAMED BUT NOT READ", "NAMED BUT NOT READ" in r.summary,
          r.summary)
    check("...distinctly from being absent", r.payload.get("named_not_read") is True,
          "the composite tried this file and failed on it")
    r = run("XSCR")
    check("...while its neighbour in the same run still passes",
          r.status == "pass", f"{r.status}: {r.summary}")

    print("\nabsence from a run is inferred from a missing block:")
    write_run_logs([(older, "".join(radar_block(x, 150, run_dt=older)
                                    for x in ("XSCR", "XSCV", "SSCB")))])
    r = run("XEBY")
    check("XEBY fails", r.status == "fail", r.status)
    check("...and says NOT IN COMPOSITE, which the run log does support",
          r.summary.startswith("NOT IN COMPOSITE"), r.summary)

    print("\nthe uniform band applies to run-log ages too:")
    write_run_logs([(older, radar_block("XSCR", 1200, run_dt=older))])
    r = run("XSCR")
    check("a 20-minute contribution fails against the 900s band",
          r.payload["sub_status"]["B_fresh"] == "fail", str(r.payload["sub_status"]))
    check("...and A_included still passes — in-but-stale is not absent",
          r.payload["sub_status"]["A_included"] == "pass")
    write_run_logs([(older, radar_block("XSCR", 700, run_dt=older))])
    r = run("XSCR")
    check("a 700s contribution warns", r.payload["sub_status"]["B_fresh"] == "warn",
          str(r.payload["sub_status"]))

    print("\nevery radar in a cycle sees ONE composite run:")
    write_run_logs([(older, complete)])
    seen = [tuple(run(x).payload["receipt_radars"]) for x in ("XSCR", "CBAND", "XSCV")]
    check("all three radars report the same run contents", len(set(seen)) == 1,
          str(len(set(seen))))

    print("\nand it falls back to the receipt, saying so, when the run log is gone:")
    drop_run_logs()
    write_receipt(receipt_now({"XSCR": 3, "SSCB": 5}))
    r = run("XSCR")
    check("the receipt answers when there is no run log",
          r.payload["participation_source"] == "receipt",
          str(r.payload.get("participation_source")))
    check("...and the claim weakens to 'offered to', not 'in'",
          r.summary.startswith("offered to composite"), r.summary)
    check("...and declines the freshness question rather than re-basing it",
          r.payload["age_basis"] == "not_assessed_on_receipt"
          and "B_fresh" not in r.payload["sub_status"],
          str(r.payload.get("age_basis")))

    print("\nthe DROPS producer check is informational by construction:")
    root = os.path.join(_TMP, "PRODUCTS", "DROPS")

    def write_drops(age_s: float) -> None:
        """A DATED tree with real files, which is the layout on cira-aqpi.

        This fixture used to create EMPTY folders and assert that a "fresh
        DROPS tree passes" — and it did, because the check stat'd the folder
        and a just-created directory looks new. So the test was passing on the
        bug: a directory with no output in it read as output arriving. Writing
        files is what makes the assertion mean anything, and it is the same
        directory-exists-but-declares-nothing conflation as the LB2 rollover.
        """
        # Clear first. The fresh case writes a 1-minute-old file, and without
        # this the "dead 12 h" case below still has it and reads 1 minute —
        # the old fixture got away with ageing the folder mtime because that
        # was the only thing the check read, which is the bug.
        import shutil as _sh
        for rid, folder in RADAR_FOLDER.items():
            if rid != "CBAND":
                _sh.rmtree(os.path.join(root, folder), ignore_errors=True)
        when = time.time() - age_s
        stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime(when))
        day = time.strftime("%Y/%m/%d", time.gmtime(when))
        for rid, folder in RADAR_FOLDER.items():
            if rid == "CBAND":
                continue
            d = os.path.join(root, folder, day)
            os.makedirs(d, exist_ok=True)
            # The NAME carries the time; mtime is set elsewhere on purpose so
            # a regression to reading mtimes fails rather than passes.
            f = os.path.join(d, f"aqpi.{folder}-{stamp}_3257_2_2_PPI.netcdf.drops")
            with open(f, "w") as fh:
                fh.write("x")
            os.utime(f, (when, when))
            # Frozen a week back on purpose: the folder's own mtime must not
            # be what the check reports, or it cannot see recovery.
            old = time.time() - 182 * 3600
            os.utime(os.path.join(root, folder), (old, old))

    write_drops(60)
    d = lb3.Layer3DropsProducerCheck()
    r = asyncio.run(d.run(None))
    check("a fresh DROPS tree passes", r.status == "pass", f"{r.status}: {r.summary}")
    check("...and CBAND is not among the folders it reads",
          "CBAND" not in r.payload["sub_status"],
          "DROPS is Gen_X-band_QPE.py's output; CBAND does not pass through it")
    check("...one check for the producer, not one per folder",
          len(r.payload["sub_status"]) == 5 and r.target == "drops-qpe",
          f"{len(r.payload['sub_status'])} folders in one row")

    # Age the OUTPUT past the limit — 12 h, the real 2026-10-05 duration. The
    # folder mtimes stay frozen at 182 h throughout, so a check reading them
    # would report 182 h here and would have reported 182 h above too.
    write_drops(12 * 3600)
    r = asyncio.run(d.run(None))
    check("a producer dead 12 h reports WARN, never fail",
          r.status == "warn", r.status)
    check("...because fail would auto-promote to critical at 30 min and page",
          r.status != "fail", "severity is a routing decision, not an observation")
    check("...but the summary states the real duration in words",
          "STALLED" in r.summary and "12.0 h" in r.summary, r.summary)
    check("...and says no live product reads the tree",
          "no live product" in r.summary, r.summary)
    check("...while payload.age_s carries the true age for reprocess",
          r.payload.get("age_s", 0) > 11 * 3600, str(r.payload.get("age_s")))
    check("...and it is the OUTPUT age, not the frozen folder mtime",
          r.payload.get("age_s", 0) < 24 * 3600,
          f"folders read 182 h; check reads "
          f"{r.payload.get('age_s', 0) / 3600:.1f} h")
    check("...and payload is flagged informational",
          r.payload.get("informational") is True)

    print("\nDROPS freshness basis:")

    test_drops_newest()


    print(f"\n{len(failures)} FAILED: {', '.join(failures)}" if failures
          else "\nall LB3 assertions passed")
    return 1 if failures else 0



# ---------------------------------------------------------------------------
# DROPS freshness must come from the FILES, not from the radar folder
# ---------------------------------------------------------------------------
def test_drops_newest() -> None:
    """The producer check has to be able to see RECOVERY.

    From v0.7.0 until 2026-10-12 it did one stat on `DROPS/<radar>`. Measured
    on cira-aqpi: every radar folder holds ZERO direct files and exactly one
    year directory, so output lands in `<radar>/2026/10/05/` and never touches
    `<radar>` itself.

    The number therefore meant "when something was last added to this folder" —
    all five read an identical 182 h, owned by a different user from their
    contents, which is a sweep rather than output arrival. And if the producer
    resumed, new files would land in a new day directory while the folder mtime
    stayed frozen, so the check would report the same stall forever.

    That is why its agreement with reality during the actual stall proved
    nothing: a frozen clock and a stopped producer look identical in exactly
    that state. The assertion that discriminates is RECOVERY, not staleness —
    the old implementation passes a staleness test and fails this one.
    """
    import tempfile
    from backend.checks.layer3_backend_processing import _drops_newest

    from datetime import datetime as _dt, timezone as _tz
    from backend.config import RAW_VOLUME_TS_RE

    now = _time.time()
    NOW = _dt.fromtimestamp(now, _tz.utc)
    PAT = RAW_VOLUME_TS_RE["XEBY"]
    root = Path(tempfile.mkdtemp(prefix="drops-"))

    def touch(folder: Path, site: str, age_s: float, *, dated: bool = True) -> str:
        """Write one DROPS file whose NAME declares `age_s` ago.

        The name is what matters now: freshness comes from the declared
        timestamp, not the mtime, for the same reason LB2_FRESHNESS is
        "filename" — and because something swept these directories' mtimes
        without writing data, so mtime on this tree is demonstrably not a
        record of arrival. The mtimes below are set to a DIFFERENT time on
        purpose, so a test that silently went back to reading them fails.
        """
        when = _dt.fromtimestamp(now - age_s, _tz.utc)
        name = f"aqpi.{site}-{when:%Y%m%d-%H%M%S}_3257_2_2_PPI.netcdf.drops"
        d = folder / when.strftime("%Y/%m/%d") if dated else folder
        d.mkdir(parents=True, exist_ok=True)
        p = d / name
        p.write_text("x")
        os.utime(p, (now - 999_999, now - 999_999))
        return name

    # Recovery: the radar folder is frozen a week back, a file landed a minute
    # ago in a NEW day directory. The old one-stat basis reports the week.
    ebay = root / "ebay"
    touch(ebay, "ebay", 182 * 3600)
    touch(ebay, "ebay", 60)
    os.utime(ebay, (now - 182 * 3600, now - 182 * 3600))
    got = _drops_newest(str(ebay), PAT, NOW)
    check("recovery in a new day directory is visible",
          got is not None and now - got < 120,
          f"{(now - got) / 3600:.1f} h" if got else "None")
    check("...and the frozen folder mtime is NOT what is reported",
          got is not None and now - got < 3600,
          f"folder reads {182} h, check reads {(now - got) / 3600:.1f} h" if got else "None")

    # A genuine stall still reads as stale.
    scrz = root / "scrz"
    touch(scrz, "scrz", 182 * 3600)
    got = _drops_newest(str(scrz), PAT, NOW)
    check("a real stall still reports the true age",
          got is not None and abs((now - got) - 182 * 3600) < 120,
          f"{(now - got) / 3600:.1f} h" if got else "None")

    # The flat layout the original basis assumed must still work.
    flat = root / "flat"
    touch(flat, "flat", 300, dated=False)
    got = _drops_newest(str(flat), PAT, NOW)
    check("a folder holding files directly is read directly",
          got is not None and abs((now - got) - 300) < 60,
          f"{(now - got):.0f} s" if got else "None")

    # An empty newest day directory falls back a day rather than reading as
    # fresh — the LB2 rollover lesson, inverted: a directory created before its
    # first file must not count as output having arrived.
    scvw = root / "scvw"
    touch(scvw, "scvw", 4 * 3600)
    (scvw / NOW.strftime("%Y/%m/%d")).mkdir(parents=True, exist_ok=True)
    got = _drops_newest(str(scvw), PAT, NOW)
    check("an empty new day directory does not read as fresh output",
          got is not None and abs((now - got) - 4 * 3600) < 120,
          f"{(now - got) / 3600:.1f} h" if got else "None")

    # A non-numeric stray must not be mistaken for the newest period.
    swyr = root / "swyr"
    touch(swyr, "swyr", 182 * 3600)
    (swyr / "tmp").mkdir(parents=True, exist_ok=True)
    got = _drops_newest(str(swyr), PAT, NOW)
    check("a non-numeric stray directory is not taken for the newest year",
          got is not None and abs((now - got) - 182 * 3600) < 120,
          f"{(now - got) / 3600:.1f} h" if got else "None")

    # A stray FUTURE-dated directory must not blind the walk. A pure
    # descend-by-newest-name would pick 2099, find it empty, and report
    # nothing; the walk is capped at today for exactly this.
    scwa = root / "scwa"
    touch(scwa, "scwa", 182 * 3600)
    (scwa / "2099" / "01" / "01").mkdir(parents=True, exist_ok=True)
    got = _drops_newest(str(scwa), PAT, NOW)
    check("a future-dated stray directory does not blind the walk",
          got is not None and abs((now - got) - 182 * 3600) < 120,
          f"{(now - got) / 3600:.1f} h" if got else "None")

    # And the declared time is what is read, not the mtime — every file above
    # was stamped ~11.6 days old on disk regardless of its name.
    check("freshness comes from the declared name, not the file mtime",
          got is not None and abs((now - got) - 999_999) > 3600,
          f"mtimes say {999_999 / 3600:.0f} h; check says "
          f"{(now - got) / 3600:.1f} h" if got else "None")


if __name__ == "__main__":
    sys.exit(main())
