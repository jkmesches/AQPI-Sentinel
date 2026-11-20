"""Which origin a radar frame listing comes from.

Run directly (no DB, no network, no mount):

    python validation_tests/test_xband_origin.py

Why this exists: the map used to fetch every radar frame from radarca, and
now reads the published tree instead. The choice is keyed on whether this
deployment mounts that tree, which is the part that is easy to get wrong in
a way nothing notices.

Keying it on the profile instead would break aqpi.local.shirejoe.com. That
deployment runs the aqpi profile with no SENTINEL_BACKEND_ROOT, so it has no
tree to read and radarca is its only origin. A profile-keyed test passes on
both CSU deployments and leaves shirejoe with an empty radar overlay.

The reverse matters too. A mounted deployment must never fall through to
radarca, because removing it from the serving path is the intended result.
"""
from __future__ import annotations
import asyncio
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

FAILURES: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if cond else 'FAIL'}  {label}{'  — ' + detail if detail else ''}")
    if not cond:
        FAILURES.append(label)


def load_upstream(root: str | None):
    """Import the upstream route module fresh, with or without a mount.

    Args:
        root: Value for SENTINEL_BACKEND_ROOT, or None to leave it unset.

    Returns:
        The freshly imported module.
    """
    for name in [m for m in sys.modules if m.startswith("backend")]:
        del sys.modules[name]
    os.environ["SENTINEL_PROFILE"] = "aqpi"
    if root is None:
        os.environ.pop("SENTINEL_BACKEND_ROOT", None)
    else:
        os.environ["SENTINEL_BACKEND_ROOT"] = root
    from backend.api.routes import upstream
    return upstream


def build_tree(root: str, day: datetime) -> None:
    """Two ebay reflectivity frames on one UTC day, in the AQPI layout.

    Args:
        root: Directory standing in for SENTINEL_BACKEND_ROOT.
        day: The UTC day to write under.
    """
    d = os.path.join(root, "PRODUCTS", "Chivo", "plots", "ebay",
                     day.strftime("%Y/%m/%d"))
    os.makedirs(d, exist_ok=True)
    for minute in (0, 3):
        ts = (day + timedelta(minutes=minute)).strftime("%Y%m%d-%H%M")
        Path(d, f"ebay_CorrReflectivity_{ts}.png").write_bytes(b"\x89PNG\r\n\x1a\n")


def test_mounted(root: str, day: datetime) -> None:
    """With a tree, the frames come from it and radarca is never called."""
    upstream = load_upstream(root)
    build_tree(root, day)
    called: list[str] = []

    async def never(*a: object, **k: object) -> list:
        called.append("radarca")
        return []

    upstream._xband_listing_cached = never
    frames = asyncio.run(upstream._xband_frames("XEBY", "Reflectivity", None, None))
    check("mounted: frames come off the tree", len(frames) == 2, str(len(frames)))
    check("mounted: radarca is not called", not called, ", ".join(called))
    check("mounted: sources keep the archive's key shape",
          all(s.startswith("xband_chivo/ebay/") for _, s in frames),
          ", ".join(s for _, s in frames))


def test_unmounted() -> None:
    """Without a tree, the radarca path still answers, as shirejoe needs."""
    upstream = load_upstream(None)
    called: list[str] = []

    async def radarca(_ctx: object, folder: str, prefix: str,
                      _pool: object = None) -> list:
        called.append(f"{folder}/{prefix}")
        return ["xband_chivo/ebay/ebay_CorrReflectivity_20261120-1200.png"]

    upstream._xband_listing_cached = radarca
    frames = asyncio.run(upstream._xband_frames("XEBY", "Reflectivity", None, None))
    check("unmounted: radarca is asked", called == ["ebay/CorrReflectivity"],
          ", ".join(called) or "not called")
    check("unmounted: its listing is returned", len(frames) == 1, str(len(frames)))
    check("unmounted: the timestamp is parsed",
          bool(frames) and frames[0][0].tzinfo is not None)


def main() -> int:
    day = datetime.now(timezone.utc).replace(hour=12, minute=0, second=0, microsecond=0)
    print("--- a deployment that mounts the tree (cira-aqpi, cira-xqpi) ---")
    with tempfile.TemporaryDirectory() as root:
        test_mounted(root, day)
    print("--- a deployment that does not (aqpi.local.shirejoe.com) ---")
    test_unmounted()
    print(f"\n{len(FAILURES)} FAILED: {', '.join(FAILURES)}" if FAILURES
          else "\nall xband-origin assertions passed")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    raise SystemExit(main())
