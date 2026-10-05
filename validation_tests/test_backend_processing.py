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

    print("\na radar present and fresh passes:")
    r = run("XSCR")
    check("XSCR passes", r.status == "pass", f"{r.status}: {r.summary}")
    check("...A_included and B_fresh both pass",
          r.payload["sub_status"] == {"A_included": "pass", "B_fresh": "pass"},
          str(r.payload["sub_status"]))
    check("...and carries age_s in payload for the reprocess engine",
          isinstance(r.payload.get("age_s"), float), str(r.payload.get("age_s")))
    check("...and headroom for the sparkline",
          0.0 <= r.metrics.get("headroom", -1) <= 1.0,
          str(r.metrics.get("headroom")))

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

    print("\na stale contribution warns, then fails:")
    _, fail_s = lb3._contrib_bands("XSCR")
    write_receipt(receipt_now({"XSCR": (fail_s * 0.9) / 60}))
    r = run("XSCR")
    check("inside the fail band but past warn -> warn",
          r.payload["sub_status"]["B_fresh"] == "warn", str(r.payload["sub_status"]))
    check("...and the overall status is warn, not fail", r.status == "warn", r.status)
    write_receipt(receipt_now({"XSCR": (fail_s + 120) / 60}))
    r = run("XSCR")
    check("past the fail band -> fail", r.payload["sub_status"]["B_fresh"] == "fail")
    check("...and says STALE CONTRIBUTION, not just a number",
          "STALE CONTRIBUTION" in r.summary, r.summary)
    check("...while A_included still passes — present-but-stale is not absent",
          r.payload["sub_status"]["A_included"] == "pass",
          "two distinct faults must not collapse into one")

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
    check("...with the age derived from the filename instead",
          r.payload["age_basis"] == "derived_from_filename",
          str(r.payload.get("age_basis")))

    print("\nthe DROPS producer check is informational by construction:")
    root = os.path.join(_TMP, "PRODUCTS", "DROPS")
    for rid, folder in RADAR_FOLDER.items():
        if rid != "CBAND":
            os.makedirs(os.path.join(root, folder), exist_ok=True)
    d = lb3.Layer3DropsProducerCheck()
    r = asyncio.run(d.run(None))
    check("a fresh DROPS tree passes", r.status == "pass", f"{r.status}: {r.summary}")
    check("...and CBAND is not among the folders it reads",
          "CBAND" not in r.payload["sub_status"],
          "DROPS is Gen_X-band_QPE.py's output; CBAND does not pass through it")
    check("...one check for the producer, not one per folder",
          len(r.payload["sub_status"]) == 5 and r.target == "drops-qpe",
          f"{len(r.payload['sub_status'])} folders in one row")

    # Age every folder past the limit — 12 h, the real 2026-10-05 duration.
    stale = time.time() - 12 * 3600
    for rid, folder in RADAR_FOLDER.items():
        if rid != "CBAND":
            os.utime(os.path.join(root, folder), (stale, stale))
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
    check("...and payload is flagged informational",
          r.payload.get("informational") is True)

    print(f"\n{len(failures)} FAILED: {', '.join(failures)}" if failures
          else "\nall LB3 assertions passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
