#!/usr/bin/env python3
"""The LB1/LB2 filesystem checks — paths, verdict bands, and failing safely.

v0.5.0 shipped these with no tests, and each of the three patch releases that
same day fixed something a test would have caught in seconds:

  v0.5.1  `_special_trees()` called .rstrip("/") on SETTINGS.sscb_root, which
          config._opt_path() returns as a Path. Path has no .rstrip, so EVERY
          layer2.backend.* run raised AttributeError immediately. The check
          family was wholly non-functional on the deployment that has the
          mounts.
  v0.5.3  age_s was recorded in metrics but not payload. check_runs stores
          payload and not metrics, and the reprocess engine reads only
          check_runs — so the one observation a retroactive threshold change
          needs was unreachable.

Both are cheap to pin and neither needs a filesystem: the first is a type
error in pure path construction, the second is a key in a dict.

What is NOT covered here: real NFS behaviour. A hung mount, a dangling
symlink, a directory of 100k entries — those need the actual shares and
belong in a probe run on cira-aqpi, not in the offline suite.

Run:  python3 validation_tests/test_backend_checks.py
"""
from __future__ import annotations
import asyncio
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("SENTINEL_DB_URL", "postgresql://unused/unused")
# Registration is gated on these; set before importing so the modules load.
os.environ.setdefault("SENTINEL_BACKEND_ROOT", "/backend/projects/aqpi/XBand/web-files")
os.environ.setdefault("SENTINEL_SSCB_ROOT", "/sscb")

from backend.checks import layer1_backend_product as lb1        # noqa: E402
from backend.checks import layer2_backend_radar as lb2          # noqa: E402
from backend.config import PRODUCTS, RADAR_FOLDER, SETTINGS     # noqa: E402

failures: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if cond else 'FAIL'}  {label}" + (f"  — {detail}" if detail else ""))
    if not cond:
        failures.append(label)


def main() -> int:
    print("the v0.5.1 regression — a Path is not a str:")
    check("sscb_root really is a Path, not a str",
          isinstance(SETTINGS.sscb_root, Path), type(SETTINGS.sscb_root).__name__)
    try:
        trees = lb2._special_trees()
        ok = True
    except AttributeError as e:
        trees, ok = {}, False
        print(f"        raised: {e}")
    check("_special_trees() survives a Path root", ok)
    check("...and yields a CBAND template with the date appended",
          trees.get("CBAND", "").endswith("/%Y/%m/%d"), str(trees))

    now = datetime(2026, 10, 3, 18, 30, tzinfo=timezone.utc)
    check("CBAND resolves to its dated tree on trinity, not DROPS",
          lb2._radar_path("CBAND", now) == "/sscb/2026/10/03",
          lb2._radar_path("CBAND", now))
    xb = next(r for r in RADAR_FOLDER if r != "CBAND")
    # Was "resolves under PRODUCTS/DROPS". DROPS holds the QPE generator's
    # OUTPUT, so the check measured the generator rather than the radar — on
    # 2026-10-05 that generator stopped and all five X-bands failed for 12 h
    # while three were arriving within a minute. Inverted rather than deleted,
    # so the old target cannot quietly return.
    check(f"an X-band ({xb}) resolves to the raw arrival tree, not DROPS",
          "/PRODUCTS/DROPS/" not in lb2._radar_path(xb, now)
          and lb2._radar_path(xb, now).endswith("/2026/10/03"),
          lb2._radar_path(xb, now))
    check("...and the two trees are genuinely different",
          lb2._radar_path("CBAND", now) != lb2._radar_path(xb, now))

    print("\nthe date is evaluated in UTC, not local time:")
    # A local-time template silently reads yesterday's directory for several
    # hours a day depending on the host's zone.
    late = datetime(2026, 10, 3, 23, 30, tzinfo=timezone.utc)
    check("23:30Z lands on the 3rd", lb2._radar_path("CBAND", late).endswith("/10/03"),
          lb2._radar_path("CBAND", late))
    early = datetime(2026, 10, 4, 0, 30, tzinfo=timezone.utc)
    check("00:30Z lands on the 4th", lb2._radar_path("CBAND", early).endswith("/10/04"),
          lb2._radar_path("CBAND", early))

    print("\nproduct paths reuse the unit-subdir rules rather than restating them:")
    d = lb1._product_dir("water_level")
    check("a product dir sits under realtime/product_images",
          "/realtime/product_images/" in d, d)
    check("...rooted at backend_root", d.startswith(str(SETTINGS.backend_root)), d)
    # These two carry unit subdirs in config; the point is that _product_dir
    # goes through image_path instead of rebuilding the convention.
    unit = lb1._product_dir("fcst_temp")
    check("a unit-subdir product keeps its subdir", unit.rstrip("/") != d.rstrip("/"),
          unit)
    check("every configured product yields a path without raising",
          all(isinstance(lb1._product_dir(p), str) for p in PRODUCTS))

    print("\nLB2 verdict bands (0.8x of the characterised silence limit):")
    # Drives the REAL run() with the filesystem stubbed out. An earlier draft of
    # this test re-implemented the band arithmetic and compared it to itself,
    # which would have passed no matter what the check actually did.
    from backend.config import RADAR_SILENT_FAIL_S
    RID = "XEBY"
    LIMIT = RADAR_SILENT_FAIL_S.get(RID, 900)

    # Stub BOTH readers. The check picks one from config.LB2_FRESHNESS, and
    # stubbing only the other silently exercises nothing — which is what
    # happened when AQPI moved from dir_mtime to filename: these assertions
    # kept running against a real (absent) path and failed as a missing mount
    # rather than testing the bands at all.
    def verdict(age_s: float) -> str:
        orig_dir, orig_decl = lb2._dir_mtime, lb2._newest_declared
        fake = lambda *a, **k: __import__("time").time() - age_s
        lb2._dir_mtime, lb2._newest_declared = fake, fake
        try:
            r = asyncio.run(lb2.Layer2BackendRadarCheck(radar_id=RID).run(None))
        finally:
            lb2._dir_mtime, lb2._newest_declared = orig_dir, orig_decl
        return r.payload["sub_status"]["A_arriving"]

    check(f"fresh arrival passes ({RID}, limit {LIMIT}s)", verdict(10) == "pass",
          verdict(10))
    check("well inside 0.8x passes", verdict(LIMIT * 0.5) == "pass")
    check("just past 0.8x warns before it breaches",
          verdict(LIMIT * 0.8 + 5) == "warn", verdict(LIMIT * 0.8 + 5))
    check("just under the limit is warn, not fail",
          verdict(LIMIT - 5) == "warn", verdict(LIMIT - 5))
    check("past the limit fails", verdict(LIMIT + 30) == "fail", verdict(LIMIT + 30))

    # And the summary has to say which it is, in words an operator reads.
    orig_dir, orig_decl = lb2._dir_mtime, lb2._newest_declared
    _late = lambda *a, **k: __import__("time").time() - (LIMIT + 30)
    lb2._dir_mtime, lb2._newest_declared = _late, _late
    try:
        r = asyncio.run(lb2.Layer2BackendRadarCheck(radar_id=RID).run(None))
    finally:
        lb2._dir_mtime, lb2._newest_declared = orig_dir, orig_decl
    check("a breach is labelled BACKEND SILENT, not just a number",
          "BACKEND SILENT" in r.summary, r.summary)
    check("...and the overall status is fail", r.status == "fail", r.status)
    check("...with age_s in the payload for the reprocess engine",
          isinstance(r.payload.get("age_s"), float), str(r.payload.get("age_s")))

    print("\nthe v0.5.3 regression — reprocess reads payload, not metrics:")
    src1 = (Path(lb1.__file__)).read_text()
    src2 = (Path(lb2.__file__)).read_text()
    check("LB1 writes age_s into payload", 'payload["age_s"]' in src1)
    check("LB2 writes age_s into payload", 'payload["age_s"]' in src2)
    check("LB1 writes its threshold into payload too",
          'payload["max_age_s"]' in src1)
    check("LB2 writes its threshold into payload too",
          'payload["silent_s"]' in src2)

    print("\nfailing safely — a wedged mount is not a broken product:")
    check("both modules bound every filesystem read with a timeout",
          "FS_TIMEOUT_S" in src1 and "FS_TIMEOUT_S" in src2)
    check("...and the timeout is short enough not to stall a 60s cadence",
          lb1.FS_TIMEOUT_S <= 10 and lb2.FS_TIMEOUT_S <= 10,
          f"{lb1.FS_TIMEOUT_S}s / {lb2.FS_TIMEOUT_S}s")
    # A timeout means "we could not look", which must route like a broken
    # check, not like a product that stopped being produced.
    check("a timeout reports error, not fail",
          'status="error"' in src1.split("asyncio.TimeoutError")[1][:400]
          and 'status="error"' in src2.split("asyncio.TimeoutError")[1][:400])
    # A missing directory IS a statement about the product.
    check("a missing directory reports fail, not error",
          'status="fail"' in src1.split("FileNotFoundError")[1][:400])

    print("\nindependence from radarca is the whole point:")
    check("LB1 declares no dependency on the origin",
          lb1.Layer1BackendProductCheck.depends_on == [])
    check("LB2 declares no dependency on the origin",
          lb2.Layer2BackendRadarCheck.depends_on == [])
    check("LB1 and LB2 carry their own stages, not L1/L2",
          lb1.Layer1BackendProductCheck.stage == "LB1"
          and lb2.Layer2BackendRadarCheck.stage == "LB2")

    # === The assertions above were true while the thing they protect was not ===
    #
    # `depends_on == []` held throughout, and until 2026-10-12 every backend
    # check was nonetheless given `alarm_only_depends_on =
    # [layer0.origin.episode]` by attach_episode_suppression, whose premise
    # ("everything except the exempt prefixes calls radarca") was written
    # before the backend tier existed. So a radarca slow episode silenced
    # alarms from checks that read K2 and Trinity and owe the web tier nothing.
    #
    # That is backwards in the worst possible way: the backend tier exists to
    # be the independent witness when radarca is misbehaving, and where the two
    # disagree the disagreement IS the signal — so a radarca episode is exactly
    # when those alarms matter, and it was exactly when they went quiet.
    #
    # Asserted over the REGISTRY rather than over the classes, because the
    # wiring happens to instances at import time and reading the class
    # attribute is what let this through for two releases.
    from backend.registry import CHECKS as _REG                 # noqa: E402
    from backend.stages import stage_tier as _tier              # noqa: E402
    _silenced = sorted(
        c.id for c in _REG.values()
        if _tier(c.stage) == "backend"
        and "layer0.origin.episode" in (c.alarm_only_depends_on or [])
    )
    check("no backend check can be silenced by a radarca episode",
          not _silenced, ", ".join(_silenced))
    _frontend_wired = [
        c.id for c in _REG.values()
        if "layer0.origin.episode" in (c.alarm_only_depends_on or [])
    ]
    # The other half of the same guarantee: an exemption that accidentally
    # covered everything would also make this test pass, and would disable
    # episode suppression wholesale.
    check("...while the radarca checks are still wired to it",
          len(_frontend_wired) > 10, f"{len(_frontend_wired)} wired")
    check("...and every one of those is a frontend-tier check",
          all(_tier(_REG[i].stage) == "frontend" for i in _frontend_wired))

    print(f"\n{len(failures)} FAILED: {', '.join(failures)}" if failures
          else "\nall backend-check assertions passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
