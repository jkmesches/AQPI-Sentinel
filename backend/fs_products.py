"""Read published product manifests and imagery from the mounted tree.

Why this exists
---------------
Every product path in ``api/routes/upstream`` goes to radarca: the manifest
through ``/api/productDetail`` and the frames through ``/api/imageData``. That
is correct for AQPI, where radarca is the display tier in front of K2.

A profile with no HTTP origin has neither. XQPI publishes FLOW's composites to
a trinity tree and nothing serves them over HTTP, so the map could show the
radar and its range ring but every composite returned 502 — the picker offered
products that could not be fetched.

The logical source string is unchanged: ``image_path()`` output, exactly what
the radarca path uses. That matters because ``_serve_source``'s LRU, archive,
single-flight and negative cache are all keyed on it, so a deployment reading
from the filesystem inherits every one of those protections rather than
re-implementing them.

A hung NFS mount blocks ``open``/``stat`` indefinitely, and unlike a check
stalling one cycle this is a request a browser is waiting on. Hence the same
timeout the LB1 check uses, for the same reason.
"""
from __future__ import annotations
import asyncio
import json
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from .config import (PRODUCT_IMAGES_PREFIX, PRODUCTS, RADAR_FOLDER,
                     RADAR_PLOTS_NAME, RADAR_PLOTS_TREE, RADAR_PLOTS_TS_FMT,
                     RADAR_PLOTS_TS_RE, SETTINGS, moment_to_prefix)

# Matches layer1_backend_product.FS_TIMEOUT_S. A mount that does not answer in
# five seconds is wedged, and failing is better than holding the connection.
FS_TIMEOUT_S = 5.0

# Frames are small (8-250 KB observed). A cap stops a wrong path — a core
# dump, a .nc, a log — from being read into memory and handed to a browser as
# an image.
MAX_IMAGE_BYTES = 32 * 1024 * 1024


def available() -> bool:
    """Can this deployment read products off the filesystem at all?"""
    return bool(SETTINGS.backend_root)


def _root() -> str:
    return os.path.join(SETTINGS.backend_root, *PRODUCT_IMAGES_PREFIX)


def manifest_path(product_id: str) -> str:
    """Absolute path to the product's published manifest."""
    return os.path.join(_root(), PRODUCTS[product_id]["details"])


def source_path(source: str) -> str:
    """Absolute path for a logical source string (``image_path()`` output).

    ``source`` is attacker-reachable in principle: it arrives from a query
    parameter on /api/upstream/image_by_source.png. Normalise and confine it to
    the published root so a ``../`` cannot walk out of the mount — the radarca
    path is naturally confined by being a URL, and this one is not.
    """
    root = os.path.realpath(_root())
    full = os.path.realpath(os.path.join(root, source.lstrip("/")))
    if full != root and not full.startswith(root + os.sep):
        raise ValueError(f"source escapes the published root: {source!r}")
    return full


class FsResponse:
    """Minimal stand-in for the httpx response ``_fetch_image_upstream`` expects.

    Only ``status_code``, ``headers`` and ``content`` are read there, so this
    carries exactly those rather than pulling in a real Response.
    """

    __slots__ = ("status_code", "headers", "content")

    def __init__(self, status_code: int, content: bytes = b"",
                 content_type: str = "image/png"):
        self.status_code = status_code
        self.content = content
        self.headers = {"content-type": content_type} if status_code == 200 else {}


def _read_steps_blocking(path: str) -> list[dict[str, Any]]:
    with open(path, "rb") as fh:
        doc = json.load(fh)
    steps = doc.get("steps") or []
    # Ordered oldest-first, like radarca's productDetail: the callers index
    # steps[-1] for "latest" and treat the index as a scrub position, so an
    # unsorted manifest would scrub backwards.
    return sorted(steps, key=lambda s: str((s or {}).get("timestamp") or ""))


def _read_image_blocking(path: str) -> FsResponse:
    try:
        size = os.path.getsize(path)
    except OSError:
        return FsResponse(404)
    if size > MAX_IMAGE_BYTES:
        return FsResponse(502)
    with open(path, "rb") as fh:
        body = fh.read()
    # Trust the bytes, not the extension: a truncated or half-written frame
    # would otherwise be cached and archived as a valid PNG.
    if not body.startswith(b"\x89PNG\r\n\x1a\n"):
        return FsResponse(502)
    return FsResponse(200, body, "image/png")


async def read_steps(product_id: str) -> list[dict[str, Any]]:
    """The product's step manifest, in the shape productDetail returns."""
    path = manifest_path(product_id)
    return await asyncio.wait_for(
        asyncio.to_thread(_read_steps_blocking, path), FS_TIMEOUT_S)


async def read_image(source: str) -> FsResponse:
    """One frame, as a response ``_fetch_image_upstream`` can consume."""
    try:
        path = source_path(source)
    except ValueError:
        return FsResponse(404)
    return await asyncio.wait_for(
        asyncio.to_thread(_read_image_blocking, path), FS_TIMEOUT_S)


# ---------------------------------------------------------------------------
# Rendered radar moment frames
# ---------------------------------------------------------------------------
#
# A sibling of the product tree rather than a subtree of it, so these resolve
# against ``SETTINGS.backend_root`` directly instead of ``_root()``. On AQPI
# the products sit under realtime/product_images/ and the frames under
# PRODUCTS/Chivo/plots/, with nothing above them in common but the root.
#
# Each profile declares its own layout rather than deriving one, because the
# two trees are shaped differently. AQPI partitions by radar and puts the
# moment in the filename; XQPI partitions by date and gives each moment a
# directory. Both are read through the same three constants.


def _backend_root() -> str:
    """The mount root as a string, for path joins.

    ``SETTINGS.backend_root`` is optional and ``available()`` is the guard
    every caller passes first. Failing here names the misuse rather than
    letting a join raise on None several frames away.
    """
    root = SETTINGS.backend_root
    if not root:
        raise RuntimeError("backend_root is not set; call available() first")
    return str(root)


def plots_dir(radar: str, moment: str, day: datetime) -> str:
    """Absolute directory holding one radar's frames for one moment, one UTC day.

    Args:
        radar: Radar id as ``RADAR_FOLDER`` keys it, e.g. "XEBY" or "FLOW".
        moment: Moment name in this profile's vocabulary.
        day: Any instant on the UTC day wanted; only the date is read.

    Returns:
        Absolute path, which may not exist.

    Raises:
        KeyError: If the radar or moment is not in this profile's tables.
    """
    rel = RADAR_PLOTS_TREE.format(
        radar=radar, folder=RADAR_FOLDER[radar], prefix=moment_to_prefix(radar, moment))
    # Format first, then strftime: the template carries both kinds of
    # placeholder and strftime would choke on a stray brace.
    return os.path.join(_backend_root(), day.strftime(rel))


def _frame_re(radar: str, moment: str) -> re.Pattern[str]:
    """Pattern matching one frame filename, capturing its timestamp text.

    Everything but the timestamp is matched literally, so the colorbar each
    directory carries is excluded by having no timestamp rather than by a
    separate rule, and a sibling product cannot pass as a frame.
    """
    parts = {"radar": radar, "folder": RADAR_FOLDER[radar],
             "prefix": moment_to_prefix(radar, moment)}
    head, tail = RADAR_PLOTS_NAME.split("{ts}")
    return re.compile(
        f"^{re.escape(head.format(**parts))}"
        f"({RADAR_PLOTS_TS_RE})"
        f"{re.escape(tail.format(**parts))}$")


def _list_frames_blocking(radar: str, moment: str, days: int) -> list[tuple[datetime, str]]:
    """Scan `days` UTC days back, newest day first, for frames of one moment."""
    pat = _frame_re(radar, moment)
    found: list[tuple[datetime, str]] = []
    now = datetime.now(timezone.utc)
    for back in range(days):
        day = now - timedelta(days=back)
        try:
            names = os.listdir(plots_dir(radar, moment, day))
        except OSError:
            # A day with no directory is normal: the tree is created on the
            # day's first publish, and a quiet radar may have no yesterday.
            continue
        for name in names:
            m = pat.match(name)
            if m is None:
                continue
            try:
                # The tree names frames in UTC with no offset in the text,
                # so the zone is attached here rather than parsed.
                ts = datetime.strptime(  # noqa: DTZ007  zone attached below
                    m.group(1), RADAR_PLOTS_TS_FMT)
            except ValueError:
                continue
            found.append((ts.replace(tzinfo=timezone.utc), name))
    found.sort()
    return found


async def list_moment_frames(radar: str, moment: str,
                             days: int = 2) -> list[tuple[datetime, str]]:
    """Frames for one radar and moment, oldest first.

    Two days by default rather than one, because the map's window is about an
    hour and a request just after 00:00 UTC needs yesterday's directory. The
    same rollover that LB2 solves with a look-back.

    Args:
        radar: Radar id as ``RADAR_FOLDER`` keys it.
        moment: Moment name in this profile's vocabulary.
        days: How many UTC days back to scan. Must be at least 1.

    Returns:
        (timestamp, filename) pairs, oldest first. Empty when the tree has
        nothing, which is not an error.

    Raises:
        ValueError: If `days` is below 1.
        TimeoutError: If the mount does not answer within FS_TIMEOUT_S.
    """
    if days < 1:
        raise ValueError(f"days must be at least 1, got {days}")
    return await asyncio.wait_for(
        asyncio.to_thread(_list_frames_blocking, radar, moment, days), FS_TIMEOUT_S)


async def read_moment_image(radar: str, moment: str, name: str,
                            day: datetime) -> FsResponse:
    """One rendered frame, as a response ``_fetch_image_upstream`` can consume.

    Args:
        radar: Radar id as ``RADAR_FOLDER`` keys it.
        moment: Moment name in this profile's vocabulary.
        name: Frame filename, checked against the pattern before it reaches
            the path, so a caller cannot hand in a traversal or a sibling.
        day: The UTC day the frame belongs to, which selects the directory.

    Returns:
        The frame, or a 404 response when the name is not a frame or the file
        is missing, matching what the radarca path returned for the same.

    Raises:
        TimeoutError: If the mount does not answer within FS_TIMEOUT_S.
    """
    if _frame_re(radar, moment).match(name) is None:
        return FsResponse(404)
    path = os.path.join(plots_dir(radar, moment, day), name)
    return await asyncio.wait_for(
        asyncio.to_thread(_read_image_blocking, path), FS_TIMEOUT_S)
