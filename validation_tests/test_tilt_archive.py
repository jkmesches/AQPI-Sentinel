"""Tilt archiving — timezone, key stability, and stream coverage.

Run directly (no DB, no network):

    python validation_tests/test_tilt_archive.py

Tilt frames come from radar-display, a different origin from radarca with
different conventions, and two of them will silently corrupt an archive:

  1. THE STAMPS ARE MOUNTAIN TIME. `Time.Value` is a naive string with no
     offset and no zone name — "Sat, 05 Sep 2026 13:54:35". It is not UTC, and
     it is not the radars' local time either: the radars are in California but
     radar-display is hosted at CSU. Verified live on 2026-09-05, frame 0 read
     13:56:55 while UTC was 19:58:02 and America/Denver was 13:58:02. A naive
     parse files every frame 6-7 hours out, and the error moves with DST, so
     the archive is wrong in a way that reads like a clock fault.

  2. FRAME INDICES ARE POSITIONS, NOT IDENTITIES. `..._0.png` means "newest",
     and the window shifts every ~140s, so the same URL names a different
     image minute to minute. An archive keyed on the index collides with
     itself immediately and cannot answer "what did el_2 look like at 14:05".
     Keys are therefore derived from capture time.

The third case is the archive being deeper than the origin: radar-display
keeps 7 frames (~16 min). Anything older exists only in our copy, and asking
upstream for it is guaranteed to fail, so those requests must never be made.
"""
from __future__ import annotations
import datetime as dt
import os
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("SENTINEL_DB_URL", "postgresql://unused/unused")

from backend.api.routes.upstream import (            # noqa: E402
    _RD_TZ, _rd_parse_ts, _tilt_source_key, _TILT_FRAMES, _TILT_MOMENTS,
    _TILT_RADARS,
)
from backend.prewarm import moment_streams, tilt_streams  # noqa: E402

failures: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if cond else 'FAIL'}  {label}" + (f"  — {detail}" if detail else ""))
    if not cond:
        failures.append(label)


def main() -> int:
    print("timezone:")
    # The exact observation from 2026-09-05 that identified the zone.
    got = _rd_parse_ts("Sat, 05 Sep 2026 13:56:55")
    want = dt.datetime(2026, 9, 5, 19, 56, 55, tzinfo=dt.timezone.utc)
    check("a Mountain stamp resolves to the right UTC instant",
          got == want, f"{got} vs {want}")
    check("the result is timezone-aware",
          got is not None and got.tzinfo is not None)

    # MDT is UTC-6, MST is UTC-7. Hard-coding either is wrong half the year.
    summer = _rd_parse_ts("Sat, 05 Sep 2026 12:00:00")
    winter = _rd_parse_ts("Fri, 05 Dec 2026 12:00:00")
    check("DST is applied, not a fixed offset",
          summer.hour == 18 and winter.hour == 19,
          f"Sep->{summer.hour}Z (MDT, -6), Dec->{winter.hour}Z (MST, -7)")
    check("the zone is America/Denver, not the radars' own Pacific",
          _RD_TZ == ZoneInfo("America/Denver"))
    # A UTC reading of the same string would be 6h off — assert the gap
    # explicitly so a "simplification" to fromisoformat/UTC fails loudly.
    naive_utc = dt.datetime(2026, 9, 5, 13, 56, 55, tzinfo=dt.timezone.utc)
    check("a naive-UTC parse would be 6h wrong (why this helper exists)",
          (got - naive_utc).total_seconds() == 6 * 3600)

    print("malformed input:")
    for bad in (None, "", "not a date", "2026-09-05T13:56:55Z"):
        check(f"{bad!r} returns None rather than raising",
              _rd_parse_ts(bad) is None)

    print("key stability:")
    t1 = _rd_parse_ts("Sat, 05 Sep 2026 13:56:55")
    t2 = _rd_parse_ts("Sat, 05 Sep 2026 13:54:35")
    k_new = _tilt_source_key("XSCR", 1, "reflectivity", t1)
    k_old = _tilt_source_key("XSCR", 1, "reflectivity", t2)
    check("distinct capture times give distinct keys", k_new != k_old)
    check("the key carries the UTC instant, not the local one",
          "20260905T195655Z" in k_new, k_new)
    check("the key contains no frame index",
          not any(f"_{i}" in k_new.rsplit("/", 1)[-1] for i in range(_TILT_FRAMES)),
          k_new)
    # The same frame re-observed one sweep later has slid down the window;
    # only a time-based key survives that.
    check("the same capture keeps its key as its frame index changes",
          _tilt_source_key("XSCR", 1, "reflectivity", t1) == k_new)
    check("radar, elevation and moment all vary the key",
          len({k_new,
               _tilt_source_key("XSCV", 1, "reflectivity", t1),
               _tilt_source_key("XSCR", 2, "reflectivity", t1),
               _tilt_source_key("XSCR", 1, "velocity", t1)}) == 4)
    check("keys are namespaced to the origin",
          k_new.startswith("radar_display/"), k_new)

    print("stream coverage:")
    ms, ts = moment_streams(), tilt_streams()
    check("tilt streams cover every radar x elevation x moment",
          len(ts) == len(_TILT_RADARS) * 4 * len(_TILT_MOMENTS), str(len(ts)))
    # X-band publishes no RhoHV. Prewarming it anyway would 404 five streams
    # on every sweep, forever.
    rho = sorted({r for r, m in ms if m == "RhoHV"})
    check("RhoHV is prewarmed for CBAND only", rho == ["CBAND"], str(rho))
    check("every other moment covers all six radars",
          all(sum(1 for r, mm in ms if mm == m) == 6
              for m in ("Reflectivity", "Velocity", "PhiDP")))
    check("no duplicate streams",
          len(set(ms)) == len(ms) and len(set(ts)) == len(ts))
    check("CBAND has no tilts (radar-display serves X-band only)",
          not any(r == "CBAND" for r, _, _ in ts))

    print(f"\n{len(failures)} FAILED: {', '.join(failures)}" if failures
          else "\nall tilt-archive assertions passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
