"""Blocking file I/O on the archive and cold stores, run off the event loop.

Either store may sit on a `hard` NFS mount, where a call blocks until the
server answers. Their calls run on a pool of their own, with a cap on calls in
flight and a time limit on each await. A stalled share then ties up at most
WORKERS threads, new calls past the cap fail at once with StorageBusyError, and
asyncio's default pool, which the disk check and the K2 tree readers share,
stays free.

An operator can also name a marker file (SENTINEL_STORAGE_MARKER) that must
exist at the root of each store before anything is written there. A share that
failed to attach leaves its mount point as an empty folder on the local disk,
and Docker creates a missing bind-mount source, so without the marker the
archive would quietly fill the local disk under the share's path.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TypeVar

log = logging.getLogger(__name__)

T = TypeVar("T")

# Four threads cover the archive's write rate (a frame every few seconds at
# most) and a retention export at once. Sixteen in flight bounds the frames
# held in memory while a stalled share holds the four.
WORKERS = 4
MAX_IN_FLIGHT = 16
BUSY_LOG_INTERVAL_S = 60.0


class StorageBusyError(RuntimeError):
    """Too many store calls are waiting on the share to start another."""


class StorageNotMountedError(RuntimeError):
    """The store's marker file is missing, so its share is not attached."""


class StorageIO:
    """A bounded pool for the stores' blocking calls, used from one event loop.

    Args:
        workers: Threads in the pool, at least 1.
        max_in_flight: Calls submitted and not yet returned, at least `workers`.

    Raises:
        ValueError: For a worker count or cap out of range.
    """

    def __init__(self, workers: int, max_in_flight: int) -> None:
        if workers < 1 or max_in_flight < workers:
            raise ValueError(
                f"need 1 <= workers <= max_in_flight, got {workers} and {max_in_flight}")
        self._pool = ThreadPoolExecutor(max_workers=workers,
                                        thread_name_prefix="storage-io")
        self._max = max_in_flight
        self._in_flight = 0
        self._last_busy_log = float("-inf")

    @property
    def in_flight(self) -> int:
        """Calls submitted to the pool that have not returned."""
        return self._in_flight

    async def run(self, fn: Callable[..., T], *args: object, timeout_s: float) -> T:
        """Run `fn(*args)` on the pool and await it for at most `timeout_s`.

        A call that times out keeps its thread and its place in the count until
        it returns, since a thread blocked in the kernel cannot be stopped.

        Args:
            fn: A blocking function.
            *args: Its arguments.
            timeout_s: Seconds to wait, above 0.

        Returns:
            What `fn` returned.

        Raises:
            ValueError: For a timeout of 0 or less.
            StorageBusyError: When MAX_IN_FLIGHT calls are already in flight.
            TimeoutError: When the call has not returned in `timeout_s`.
        """
        if timeout_s <= 0:
            raise ValueError(f"timeout_s must be above 0, got {timeout_s}")
        if self._in_flight >= self._max:
            self._note_busy()
            raise StorageBusyError(f"{self._in_flight} storage calls are still in flight")
        loop = asyncio.get_running_loop()
        fut = loop.run_in_executor(self._pool, fn, *args)
        self._in_flight += 1
        fut.add_done_callback(self._release)
        # Shielded, so a timeout cancels only this wait and the count still
        # holds the call until its thread returns.
        return await asyncio.wait_for(asyncio.shield(fut), timeout_s)

    def _release(self, fut: asyncio.Future[T]) -> None:
        self._in_flight -= 1
        # Marks a late failure as seen. After a timeout nobody awaits the
        # future, and asyncio would log "exception was never retrieved".
        if not fut.cancelled():
            _ = fut.exception()

    def _note_busy(self) -> None:
        now = time.monotonic()
        if now - self._last_busy_log >= BUSY_LOG_INTERVAL_S:
            self._last_busy_log = now
            log.warning("storage: %d calls in flight, so new ones are refused; "
                        "the archive or cold share is probably stalled", self._in_flight)


def require_marker(root: Path, marker: str | None) -> None:
    """Raise StorageNotMountedError when `marker` is set and missing from `root`.

    Runs on the pool: on a stalled share the test blocks like any other call.

    Args:
        root: The store's root folder.
        marker: The marker's file name, or None when no marker is required.

    Raises:
        StorageNotMountedError: When the marker is required and absent.
    """
    if marker and not (root / marker).exists():
        raise StorageNotMountedError(f"{root} has no {marker}, so its share is not attached")


# One pool for the process. The two stores share it, so together they can never
# hold more than WORKERS threads on a stalled share.
STORAGE_IO = StorageIO(WORKERS, MAX_IN_FLIGHT)
