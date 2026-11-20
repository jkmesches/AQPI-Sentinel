"""Which origin a radar frame listing comes from.

Run directly (no DB, no network, no mount):

    python validation_tests/test_xband_origin.py

Why this exists: the map used to fetch every radar frame from radarca, and
now reads the published tree instead. The choice is keyed on whether this
deployment mounts that tree, which is the part that is easy to get wrong in
a way nothing notices.

Keying it on the profile instead would break the test deployment. That
deployment runs the aqpi profile with no SENTINEL_BACKEND_ROOT, so it has no
tree to read and radarca is its only origin. A profile-keyed test passes on
both CSU deployments and leaves the test deployment with an empty radar overlay.

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
from types import ModuleType

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

FAILURES: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if cond else 'FAIL'}  {label}{'  — ' + detail if detail else ''}")
    if not cond:
        FAILURES.append(label)


def load_upstream(root: str | None) -> ModuleType:
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

    async def never(*a: object, **k: object) -> list[str]:
        called.append("radarca")
        return []

    # Swapping a private module attribute is the point of this test: it is
    # how we observe whether the radarca path was taken at all.
    upstream._xband_listing_cached = never  # type: ignore[attr-defined]
    frames = asyncio.run(upstream._xband_frames("XEBY", "Reflectivity", None, None))
    check("mounted: frames come off the tree", len(frames) == 2, str(len(frames)))
    check("mounted: radarca is not called", not called, ", ".join(called))
    check("mounted: sources keep the archive's key shape",
          all(s.startswith("xband_chivo/ebay/") for _, s in frames),
          ", ".join(s for _, s in frames))


def test_unmounted() -> None:
    """Without a tree, the radarca path still answers, as the test deployment needs."""
    upstream = load_upstream(None)
    called: list[str] = []

    async def radarca(_ctx: object, folder: str, prefix: str,
                      _pool: object = None) -> list[str]:
        called.append(f"{folder}/{prefix}")
        return ["xband_chivo/ebay/ebay_CorrReflectivity_20261120-1200.png"]

    upstream._xband_listing_cached = radarca  # type: ignore[attr-defined]
    frames = asyncio.run(upstream._xband_frames("XEBY", "Reflectivity", None, None))
    check("unmounted: radarca is asked", called == ["ebay/CorrReflectivity"],
          ", ".join(called) or "not called")
    check("unmounted: its listing is returned", len(frames) == 1, str(len(frames)))
    check("unmounted: the timestamp is parsed",
          bool(frames) and frames[0][0].tzinfo is not None)


def test_l4_origin(root: str, day: datetime) -> None:
    """L4 opens the frame the map would show, off the mount where there is one.

    LB1 stats a file and never opens it, so a producer writing truncated PNGs
    stayed green. This check is what reads the bytes, which only helps if it
    reads the same ones the serving path hands out.
    """
    upstream = load_upstream(root)
    build_tree(root, day)
    from backend.checks.layer4_image import Layer4ImageCheck
    from backend.registry import CHECKS

    check("L4 registers on a mounted deployment",
          any(k.startswith("layer4.") for k in CHECKS),
          str(len([k for k in CHECKS if k.startswith("layer4.")])))

    chk = Layer4ImageCheck(kind="xband", identifier="XEBY")
    body, label = asyncio.run(chk._fetch_image(None))
    check("L4 reads a radar frame without a context",
          body is not None and label.startswith("ebay_"), str(label))
    check("...and the bytes are a PNG rather than an error string",
          bool(body) and body.startswith(b"\x89PNG"),
          repr(body[:8]) if body else "none")
    # `ctx` stays None on purpose: reaching radarca would raise on it, so a
    # clean read proves nothing touched the HTTP path.
    del upstream


def main() -> int:
    # Near now, because the listing keeps only the last hour. A fixed hour
    # would make this test pass or fail depending on the time of day.
    day = datetime.now(timezone.utc).replace(second=0, microsecond=0) - timedelta(minutes=10)
    print("--- a deployment that mounts the tree (the AQPI deployment, the XQPI deployment) ---")
    with tempfile.TemporaryDirectory() as root:
        test_mounted(root, day)
    print("--- a deployment that does not (the test deployment) ---")
    test_unmounted()
    print("--- layer 4 image QC ---")
    with tempfile.TemporaryDirectory() as root:
        test_l4_origin(root, day)
    print(f"\n{len(FAILURES)} FAILED: {', '.join(FAILURES)}" if FAILURES
          else "\nall xband-origin assertions passed")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    raise SystemExit(main())
