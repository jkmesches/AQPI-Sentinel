"""The stores' I/O pool, and the archive, retention and disk check on it.

Run directly (no DB, no network):

    python validation_tests/test_storage_io.py

The archive and cold stores may sit on a `hard` NFS mount, where a call blocks
until the server answers. backend/storage_io.py gives them a pool of their own
with a cap on calls in flight, so a stalled share cannot freeze the event loop
or hold the threads every other check uses. Calls blocked on an Event stand in
for a stall, since a hard mount blocks them the same way.
"""
from __future__ import annotations

import asyncio
import gzip
import os
import sys
import tempfile
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("SENTINEL_DB_URL", "postgresql://unused/unused")

import backend.checks as _all  # noqa: F401
import backend.checks.layer0_selfhost as selfhost
from backend import archive, config, retention, storage_io
from backend.registry import CHECKS
from backend.storage_io import STORAGE_IO, StorageBusyError, StorageIO

MARKER = ".sentinel-storage"
failures: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    """Record one assertion."""
    print(f"  {'ok  ' if cond else 'FAIL'}  {label}" + (f"  ({detail})" if detail else ""))
    if not cond:
        failures.append(label)


class Stall:
    """Fill an I/O pool's cap with calls that block until released.

    Args:
        io: The pool to stall.
    """

    def __init__(self, io: StorageIO) -> None:
        self._io = io
        self._gate = threading.Event()
        self._tasks: list[asyncio.Task[bool]] = []

    async def __aenter__(self) -> Stall:
        free = storage_io.MAX_IN_FLIGHT - self._io.in_flight
        self._tasks = [asyncio.create_task(self._io.run(self._gate.wait, 30, timeout_s=60))
                       for _ in range(free)]
        await asyncio.sleep(0.05)
        return self

    async def __aexit__(self, *_: object) -> None:
        self._gate.set()
        _ = await asyncio.wait_for(asyncio.gather(*self._tasks), 10)


async def elapsed(coro: Awaitable[Any]) -> tuple[Any, float]:
    """Await `coro` and time it.

    Args:
        coro: What to await.

    Returns:
        (its result, or the exception it raised; seconds taken).
    """
    t0 = time.monotonic()
    try:
        out: Any = await coro
    except Exception as e:  # noqa: BLE001  the test inspects whatever was raised
        out = e
    return out, time.monotonic() - t0


async def pool_rules() -> None:
    """The cap, the timeout and the count on a pool of its own."""
    for bad in ((0, 4), (4, 3)):
        try:
            _ = StorageIO(*bad)
            check(f"StorageIO{bad} is refused", False)
        except ValueError:
            check(f"StorageIO{bad} is refused", True)
    io = StorageIO(2, 3)
    check("a call returns its value", await io.run(sum, [1, 2], timeout_s=5) == 3)
    try:
        _ = await io.run(sum, [1], timeout_s=0)
        check("a timeout of 0 is refused", False)
    except ValueError:
        check("a timeout of 0 is refused", True)

    gate = threading.Event()
    stuck = [asyncio.create_task(io.run(gate.wait, 30, timeout_s=60)) for _ in range(3)]
    await asyncio.sleep(0.05)
    check("three blocked calls are counted", io.in_flight == 3, str(io.in_flight))
    out, took = await elapsed(io.run(sum, [1], timeout_s=5))
    check("a fourth is refused at once", isinstance(out, StorageBusyError) and took < 0.5,
          f"{type(out).__name__} in {took:.2f}s")

    ticks = 0

    async def ticker() -> None:
        nonlocal ticks
        for _ in range(10):
            await asyncio.sleep(0.01)
            ticks += 1

    await ticker()
    check("the event loop runs while the pool is stalled", ticks == 10, str(ticks))
    gate.set()
    _ = await asyncio.wait_for(asyncio.gather(*stuck), 10)
    check("the count returns to 0 once they return", io.in_flight == 0, str(io.in_flight))

    late = threading.Event()

    def slow_fail() -> None:
        late.wait(10)
        raise OSError("late failure")

    out, took = await elapsed(io.run(slow_fail, timeout_s=0.2))
    check("a call past its limit raises TimeoutError", isinstance(out, TimeoutError),
          type(out).__name__)
    check("...and still holds its place until its thread returns", io.in_flight == 1,
          str(io.in_flight))
    seen: list[dict[str, Any]] = []
    asyncio.get_running_loop().set_exception_handler(lambda _l, ctx: seen.append(ctx))
    late.set()
    for _ in range(50):
        if io.in_flight == 0:
            break
        await asyncio.sleep(0.02)
    check("...which it gives up when the thread returns", io.in_flight == 0, str(io.in_flight))
    check("...without an unretrieved-exception log", not seen, str(seen[:1]))
    asyncio.get_running_loop().set_exception_handler(None)


async def archive_on_a_stall(root: Path) -> None:
    """save_image and lookup_by_source give up fast when the store is stalled.

    Args:
        root: An empty folder for the archive.
    """
    async with Stall(STORAGE_IO):
        out, took = await elapsed(archive.save_image(
            None, root, source="x/y.png", content=b"\x89PNG not really", marker=None))
        check("a save during a stall gives up at once", out is None and took < 1,
              f"{out!r} in {took:.2f}s")
        check("...and writes nothing",
              not any(root.rglob("*.png")))  # noqa: ASYNC240  a local temp folder

        class Conn:
            async def fetchrow(self, *_: object) -> dict[str, str]:
                return {"sha256": "ab" * 32, "ext": "png"}

        class Acquire:
            async def __aenter__(self) -> Conn:
                return Conn()

            async def __aexit__(self, *_: object) -> None:
                return None

        class Pool:
            def acquire(self) -> Acquire:
                return Acquire()

        out, took = await elapsed(archive.lookup_by_source(Pool(), root, "x/y.png"))
        check("a read during a stall falls back at once", out is None and took < 1,
              f"{out!r} in {took:.2f}s")


class CopyPool:
    """A pool whose COPY streams set chunks to the output, or fails midway.

    Args:
        chunks: The COPY output.
        fail_after: Raise after this many chunks, or None to finish.
    """

    def __init__(self, chunks: list[bytes], fail_after: int | None = None) -> None:
        self.chunks = chunks
        self.fail_after = fail_after

    def acquire(self) -> CopyPool:
        return self

    async def __aenter__(self) -> CopyPool:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    async def copy_from_query(self, *_: object, output: Callable[[bytes], Awaitable[None]],
                              **__: object) -> str:
        for i, chunk in enumerate(self.chunks):
            if self.fail_after is not None and i == self.fail_after:
                raise ConnectionError("connection lost mid-COPY")
            await output(chunk)
        return f"COPY {len(self.chunks)}"


async def retention_export(root: Path) -> None:
    """The export streams into gzip on the pool, and a stall or failure leaves no file.

    Args:
        root: An empty folder; the cold store goes under it.
    """
    chunks = [f"{i},row {i}\n".encode() for i in range(2000)]
    dest = root / "cold" / "t" / "t_1.csv.gz"
    n = await retention._export_query(CopyPool(chunks), "q", [], dest)
    check("an export reports the COPY's row count", n == 2000, str(n))
    check("...and its file holds the rows, gzipped",
          dest.exists() and gzip.decompress(dest.read_bytes()) == b"".join(chunks))
    check("...with no .part left", not list(dest.parent.glob("*.part")))

    dest2 = root / "cold" / "t" / "t_2.csv.gz"
    out, _ = await elapsed(retention._export_query(CopyPool(chunks, fail_after=500), "q", [],
                                                   dest2))
    check("a COPY that fails midway raises", isinstance(out, ConnectionError),
          type(out).__name__)
    check("...and leaves neither the export nor its .part",
          not dest2.exists() and not list(dest2.parent.glob("t_2*")))

    dest3 = root / "cold" / "t" / "t_3.csv.gz"
    async with Stall(STORAGE_IO):
        out, took = await elapsed(retention._export_query(CopyPool(chunks), "q", [], dest3))
    check("an export during a stall raises OffloadError at once",
          isinstance(out, retention.OffloadError) and took < 1,
          f"{type(out).__name__} in {took:.2f}s")
    check("...and writes nothing", not dest3.exists())


async def disk_check_on_a_stall() -> None:
    """The disk check reports a stalled archive store as a warn, without waiting."""
    async with Stall(STORAGE_IO):
        out, took = await elapsed(CHECKS["layer0.self.disk"].run(None))
    ok = not isinstance(out, Exception)
    check("the disk check answers during a stall", ok and took < 2, f"{took:.2f}s")
    if ok:
        check("...and says the archive is unreachable",
              "archive UNREACHABLE" in out.summary and out.payload.get("archive_error") == "busy",
              out.summary)


async def marker_rules(root: Path) -> None:
    """With a marker named, nothing is written to a store that lacks it.

    Args:
        root: An empty folder; the archive and cold stores go under it.
    """
    for raw, want in (("", None), (".sentinel-storage", ".sentinel-storage")):
        os.environ["SENTINEL_STORAGE_MARKER"] = raw
        check(f"SENTINEL_STORAGE_MARKER={raw!r} reads as {want!r}",
              config._opt_marker("SENTINEL_STORAGE_MARKER") == want)
    for bad in ("a/b", "..", "."):
        os.environ["SENTINEL_STORAGE_MARKER"] = bad
        try:
            _ = config._opt_marker("SENTINEL_STORAGE_MARKER")
            check(f"a marker of {bad!r} is refused", False)
        except ValueError:
            check(f"a marker of {bad!r} is refused", True)
    os.environ.pop("SENTINEL_STORAGE_MARKER", None)

    arch, cold = root / "archive", root / "cold"
    for d in (arch, cold):
        d.mkdir(parents=True)
    out = await archive.save_image(None, arch, source="x/y.png", content=b"frame",
                                   marker=MARKER)
    made = sorted(p.name for p in arch.rglob("*"))
    check("a save to an archive without the marker is refused", out is None)
    check("...and creates nothing, not even a shard folder", not made, str(made))
    (arch / MARKER).touch()
    _ = await archive.save_image(None, arch, source="x/y.png", content=b"frame",
                                 marker=MARKER)
    files = list(arch.rglob("*.png"))
    check("with the marker in place the frame is written", len(files) == 1, str(files))

    out, _ = await elapsed(retention._require_store(cold, MARKER))
    check("a cold store without the marker stops the sweep",
          isinstance(out, retention.OffloadError) and "not attached" in str(out), repr(out))
    (cold / MARKER).touch()
    out, _ = await elapsed(retention._require_store(cold, MARKER))
    check("...and one with it lets it run", out is None, repr(out))

    real = selfhost.SETTINGS
    selfhost.SETTINGS = replace(real, archive_root=arch, cold_root=cold, storage_marker=MARKER)
    try:
        (cold / MARKER).unlink()
        r = await CHECKS["layer0.self.disk"].run(None)
        check("the disk check fails on a store without the marker",
              r.status == "fail" and "NOT ATTACHED" in r.summary
              and r.payload.get("detached") == [str(cold)], r.summary)
        (cold / MARKER).touch()
        r = await CHECKS["layer0.self.disk"].run(None)
        check("...and says nothing of it when both have one",
              "NOT ATTACHED" not in r.summary and "detached" not in r.payload, r.summary)
    finally:
        selfhost.SETTINGS = real


async def main() -> int:
    """Run every section.

    Returns:
        1 when an assertion failed.
    """
    with tempfile.TemporaryDirectory() as td:
        await pool_rules()
        await archive_on_a_stall(Path(td) / "archive")
        await retention_export(Path(td))
        await disk_check_on_a_stall()
        await marker_rules(Path(td) / "marked")
    check("nothing is left in flight", STORAGE_IO.in_flight == 0, str(STORAGE_IO.in_flight))
    print(f"\n{len(failures)} FAILED: {', '.join(failures)}" if failures
          else "\nall storage-io assertions passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
