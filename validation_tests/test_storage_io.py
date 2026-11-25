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
from typing import Any, cast
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("SENTINEL_DB_URL", "postgresql://unused/unused")

import backend.checks as _all  # noqa: F401
import backend.checks.layer0_selfhost as selfhost
from backend import archive, config, retention, storage_io
from backend.registry import CHECKS
from backend.checks.transports import CheckContext
from backend.storage_io import STORAGE_IO, StorageBusyError, StorageIO

MARKER = ".sentinel-storage"
# The disk check reads no transport, so it is given none.
NO_CONTEXT = cast(CheckContext, None)
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

    async def __aexit__(self, *exc: object) -> None:
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
    with mock.patch.object(retention, "SLOT_WAIT_S", 0.3):
        async with Stall(STORAGE_IO):
            out, took = await elapsed(retention._export_query(CopyPool(chunks), "q", [], dest3))
    check("an export during a stall raises OffloadError once no slot frees in SLOT_WAIT_S",
          isinstance(out, retention.OffloadError) and 0.25 < took < 2,
          f"{type(out).__name__} in {took:.2f}s")
    check("...and writes nothing", not dest3.exists())


async def export_waits_for_a_slot(root: Path) -> None:
    """An export refused a slot by a full pool waits for one, then runs.

    Args:
        root: An empty folder; the cold store goes under it.
    """
    print("\nretention export and a full pool:")
    chunks = [f"{i},row {i}\n".encode() for i in range(200)]
    dest = root / "cold" / "w" / "w_1.csv.gz"
    refused = asyncio.Event()
    real_note = STORAGE_IO._note_busy

    def note_busy() -> None:
        refused.set()
        real_note()

    with mock.patch.object(STORAGE_IO, "_note_busy", note_busy), \
            mock.patch.object(retention, "SLOT_WAIT_S", 10.0):
        async with Stall(STORAGE_IO):
            task = asyncio.create_task(
                retention._export_query(CopyPool(chunks), "q", [], dest))
            _ = await asyncio.wait_for(refused.wait(), 5)  # NOLINT(PY032) wait_for bounds it
        out, took = await elapsed(asyncio.wait_for(task, 10))
    check("an export refused a slot waits for one and then runs",
          out == 200 and dest.exists(), f"{out!r} in {took:.2f}s")


async def late_finish(root: Path) -> None:
    """An export whose finish timed out publishes nothing once the store answers.

    Without this, the rename landed after the sweep had given up and kept the
    rows, and the next sweep exported those rows a second time.

    Args:
        root: An empty folder; the cold store goes under it.
    """
    print("\nretention export finished late:")
    chunks = [f"{i},row {i}\n".encode() for i in range(200)]
    dest = root / "cold" / "l" / "l_1.csv.gz"
    gate, done = threading.Event(), threading.Event()
    real_fsync = os.fsync

    def stalled_fsync(fd: int) -> None:
        _ = gate.wait(5)
        try:
            real_fsync(fd)
        finally:
            done.set()

    with mock.patch.object(os, "fsync", stalled_fsync), \
            mock.patch.object(retention, "FILE_TIMEOUT_S", 0.3):
        out, _ = await elapsed(retention._export_query(CopyPool(chunks), "q", [], dest))
        gate.set()
        finished = await asyncio.to_thread(done.wait, 5)
        for _ in range(40):
            if STORAGE_IO.in_flight == 0:
                break
            await asyncio.sleep(0.05)
    check("an export whose finish times out raises OffloadError",
          isinstance(out, retention.OffloadError), repr(out))
    check("...and its finish, once the store answers, publishes nothing and leaves no file",
          finished and not dest.exists() and not list(dest.parent.glob("l_1*")),
          str(sorted(p.name for p in dest.parent.glob("*"))))


async def disk_check_on_a_stall() -> None:
    """The disk check reports a stalled archive store as a warn, without waiting."""
    async with Stall(STORAGE_IO):
        out, took = await elapsed(CHECKS["layer0.self.disk"].run(NO_CONTEXT))
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

    stores = replace(config.SETTINGS, archive_root=arch, cold_root=cold, storage_marker=MARKER,
                     archive_enabled=True, db_retention_days=60)
    with mock.patch.object(selfhost, "SETTINGS", stores):
        (cold / MARKER).unlink()
        r = await CHECKS["layer0.self.disk"].run(NO_CONTEXT)
        check("the disk check fails on a store without the marker",
              r.status == "fail" and "NOT ATTACHED" in r.summary
              and r.payload.get("detached") == [str(cold)], r.summary)
        (cold / MARKER).touch()
        r = await CHECKS["layer0.self.disk"].run(NO_CONTEXT)
        check("...and says nothing of it when both have one",
              "NOT ATTACHED" not in r.summary and "detached" not in r.payload, r.summary)


def blob_writers(root: Path) -> None:
    """Two writers of one frame never publish a part-written file.

    Writer 1 renames its file while writer 2, which found no file yet, is
    stopped halfway through its own write. With one temporary name for both,
    writer 2's open truncated the file writer 1 then renamed into place.

    Args:
        root: An empty folder for the archive.
    """
    print("\narchive writers of one frame:")
    content = b"X" * 100_000
    path = root / "ab" / ("ab" + "0" * 62 + ".png")
    ready, opened = threading.Event(), threading.Event()
    renamed, release = threading.Event(), threading.Event()
    real_write, real_replace = Path.write_bytes, os.replace
    who = threading.local()

    def write_bytes(self: Path, data: bytes) -> int:
        if getattr(who, "n", 0) != 2:
            return real_write(self, data)
        with open(self, "wb") as f:
            opened.set()
            _ = release.wait(5)
            return f.write(data)

    def replace(src: Path, dst: Path) -> None:
        if getattr(who, "n", 0) == 1:
            ready.set()
            _ = opened.wait(5)
            real_replace(src, dst)
            renamed.set()
        else:
            real_replace(src, dst)

    def writer(n: int) -> None:
        who.n = n
        archive._write_blob(root, path, content, None)

    with mock.patch.object(Path, "write_bytes", write_bytes), \
            mock.patch.object(os, "replace", replace):
        first = threading.Thread(target=writer, args=(1,))
        first.start()
        _ = ready.wait(5)
        second = threading.Thread(target=writer, args=(2,))
        second.start()
        _ = renamed.wait(5)
        size = path.stat().st_size if path.exists() else -1
        release.set()
        first.join(5)
        second.join(5)
    check("a frame renamed while another writer is mid-write is whole",
          size == len(content), f"{size} of {len(content)} bytes")
    check("...and both writers finish with a whole file and no temporary left",
          not first.is_alive() and not second.is_alive()
          and path.stat().st_size == len(content) and not list(root.rglob("*.tmp")),
          str(sorted(p.name for p in root.rglob("*"))))


def blob_repairs(root: Path) -> None:
    """A frame cut short is written again, and a failed write leaves nothing.

    Args:
        root: An empty folder for the archive.
    """
    content = b"X" * 100_000
    path = root / "ab" / ("ab" + "0" * 62 + ".png")
    path.parent.mkdir(parents=True)
    _ = path.write_bytes(b"cut short")
    archive._write_blob(root, path, content, None)
    check("a frame cut short is written again", path.read_bytes() == content)
    real_write = Path.write_bytes

    def failing_write(self: Path, data: bytes) -> int:
        _ = real_write(self, data[:10])
        raise OSError(5, "Input/output error")

    other = root / "cd" / ("cd" + "0" * 62 + ".png")
    with mock.patch.object(Path, "write_bytes", failing_write):
        try:
            archive._write_blob(root, other, content, None)
            raised = False
        except OSError:
            raised = True
    check("a failed write raises and leaves no temporary file",
          raised and not other.exists() and not list(other.parent.glob("*.tmp")),
          str(sorted(p.name for p in other.parent.glob("*"))))


def unlink_stop(root: Path) -> None:
    """A prune's unlink call removes nothing once its stop flag is set.

    The flag is set when the call times out, and the thread runs on after
    that, so without it a share that answers hours later would lose frames
    saved again in the meantime.

    Args:
        root: An empty folder.
    """
    print("\nprune unlinks:")
    root.mkdir(parents=True)
    files = [root / f"{n}.png" for n in range(3)]
    for f in files:
        _ = f.write_bytes(b"frame")
    stop = threading.Event()
    stop.set()
    freed = retention._unlink_all(files, stop)
    check("an unlink call whose stop flag is set removes nothing",
          freed == 0 and all(f.exists() for f in files), f"freed {freed}")
    stop.clear()
    freed = retention._unlink_all(files, stop)
    check("...and one whose flag is clear removes every file",
          freed == 15 and not any(f.exists() for f in files), f"freed {freed}")


async def store_probe_faults(root: Path) -> None:
    """The disk check reports a store whose marker probe errors or stalls.

    Args:
        root: An empty folder; the archive and cold stores go under it.
    """
    print("\nmarker probe faults:")
    arch, cold = root / "archive", root / "cold"
    for d in (arch, cold):
        d.mkdir(parents=True)
        (d / MARKER).touch()
    stores = replace(config.SETTINGS, archive_root=arch, cold_root=cold, storage_marker=MARKER,
                     archive_enabled=True, db_retention_days=60)
    real_probe = selfhost._store_state
    gate = threading.Event()

    def refused(r: Path, marker: str | None) -> str:
        if r == cold:
            raise PermissionError(13, "Permission denied", str(r / str(marker)))
        return real_probe(r, marker)

    def stalled(r: Path, marker: str | None) -> str:
        if r == cold:
            _ = gate.wait(5)
        return real_probe(r, marker)

    with mock.patch.object(selfhost, "SETTINGS", stores):
        with mock.patch.object(selfhost, "_store_state", refused):
            out, _ = await elapsed(CHECKS["layer0.self.disk"].run(NO_CONTEXT))
        ok = not isinstance(out, Exception)
        check("the disk check answers when the cold store refuses the probe", ok, repr(out))
        if ok:
            check("...with a warn that names the cold store as unreadable",
                  out.status == "warn"
                  and out.payload.get("store_errors") == {str(cold): "unreadable"}, out.summary)
        with mock.patch.object(selfhost, "_store_state", stalled), \
                mock.patch.object(selfhost, "STORE_PROBE_TIMEOUT_S", 0.3):
            out, took = await elapsed(CHECKS["layer0.self.disk"].run(NO_CONTEXT))
        gate.set()
        ok = not isinstance(out, Exception)
        check("the disk check answers while the cold store's probe stalls",
              ok and took < 2, f"{took:.2f}s, {out!r}" if not ok else f"{took:.2f}s")
        if ok:
            check("...with a warn that names the cold store as unreachable",
                  out.status == "warn" and "UNREACHABLE" in out.summary
                  and out.payload.get("store_errors") == {str(cold): "unreachable"},
                  out.summary)
    for _ in range(40):
        if STORAGE_IO.in_flight == 0:
            break
        await asyncio.sleep(0.05)


async def store_write_probe(root: Path) -> None:
    """The disk check fails a store that refuses a new file, marker or not.

    A share once held both markers while it refused every new file the
    backend made in their folders, and the check passed.

    Args:
        root: An empty folder; the archive and cold stores go under it.
    """
    print("\nstore write probe:")
    arch, cold = root / "archive", root / "cold"
    for d in (arch, cold):
        d.mkdir(parents=True)
        (d / MARKER).touch()
    stores = replace(config.SETTINGS, archive_root=arch, cold_root=cold, storage_marker=MARKER,
                     archive_enabled=True, db_retention_days=60)
    disk = CHECKS["layer0.self.disk"]
    with mock.patch.object(selfhost, "SETTINGS", stores):
        r = await disk.run(NO_CONTEXT)
        left = sorted(p.name for d in (arch, cold) for p in d.glob(".sentinel-write-probe*"))
        check("two writable stores pass, and the probe leaves no file",
              "NOT WRITABLE" not in r.summary and not left, f"{r.summary}; left {left}")
        if os.geteuid() == 0:
            print("  skip  a read-only folder does not stop root; run as a normal user")
        else:
            cold.chmod(0o555)
            try:
                r = await disk.run(NO_CONTEXT)
                check("a cold store that refuses a new file fails the check",
                      r.status == "fail" and "NOT WRITABLE" in r.summary
                      and r.payload.get("unwritable") == [str(cold)], r.summary)
                with mock.patch.object(selfhost, "SETTINGS", replace(stores, storage_marker=None)):
                    r = await disk.run(NO_CONTEXT)
                check("...with no marker set as well",
                      r.status == "fail" and r.payload.get("unwritable") == [str(cold)],
                      r.summary)
            finally:
                cold.chmod(0o755)
    off = replace(stores, archive_enabled=False, db_retention_days=None)
    with mock.patch.object(selfhost, "SETTINGS", off):
        states = await selfhost._store_states()
    check("a deployment that writes neither store probes neither", states == {}, str(states))
    missing = replace(stores, cold_root=root / "not-yet", storage_marker=None)
    with mock.patch.object(selfhost, "SETTINGS", missing):
        states = await selfhost._store_states()
    check("a store folder not created yet is absent, not a fault",
          states.get(str(root / "not-yet")) == "absent" and not (root / "not-yet").exists(),
          str(states))


async def main() -> int:
    """Run every section.

    Returns:
        1 when an assertion failed.
    """
    with tempfile.TemporaryDirectory() as td:
        await pool_rules()
        await archive_on_a_stall(Path(td) / "archive")
        await retention_export(Path(td))
        await export_waits_for_a_slot(Path(td))
        await late_finish(Path(td))
        await disk_check_on_a_stall()
        await marker_rules(Path(td) / "marked")
        blob_writers(Path(td) / "blobs")
        blob_repairs(Path(td) / "repairs")
        unlink_stop(Path(td) / "unlink")
        await store_probe_faults(Path(td) / "probed")
        await store_write_probe(Path(td) / "written")
    check("nothing is left in flight", STORAGE_IO.in_flight == 0, str(STORAGE_IO.in_flight))
    print(f"\n{len(failures)} FAILED: {', '.join(failures)}" if failures
          else "\nall storage-io assertions passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
