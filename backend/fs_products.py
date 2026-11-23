"""Read published product manifests and imagery from the mounted tree.

Why this exists
---------------
Every product path in ``api/routes/upstream`` goes to radarca: the manifest
through ``/api/productDetail`` and the frames through ``/api/imageData``. That
is correct for AQPI, where radarca is the display tier in front of the main archive.

A profile with no HTTP origin has neither. XQPI publishes FLOW's composites to
a secondary archive tree and nothing serves them over HTTP, so the map could show the
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

from .config import (NEXRAD_PLOTS_NAME, NEXRAD_PLOTS_TREE, NEXRAD_PLOTS_TS_FMT,
                     NEXRAD_PLOTS_TS_RE, NEXRAD_SITES, PRODUCT_IMAGES_PREFIX,
                     RADAR_FOLDER, RADAR_PLOTS_NAME, RADAR_PLOTS_TREE,
                     RADAR_PLOTS_TS_FMT, RADAR_PLOTS_TS_RE, SETTINGS,
                     TILT_IMAGE_DIR, TILT_JSON_DIR, TILT_TREE, manifest_rel,
                     moment_to_prefix)

# Matches layer1_backend_product.FS_TIMEOUT_S. A mount that does not answer in
# five seconds is wedged, and failing is better than holding the connection.
FS_TIMEOUT_S = 5.0

# Frames are small (8-250 KB observed). A cap stops a wrong path — a core
# dump, a .nc, a log — from being read into memory and handed to a browser as
# an image.
MAX_IMAGE_BYTES = 32 * 1024 * 1024

# How far back a moment listing reaches by default.
#
# The tree keeps whole days, where radarca served a rolling hour. Returning
# everything on disk handed the map 872 steps spanning 44 hours in place of
# the 18 it expects, which stretches the scrubber across two days and turns
# a 16-second playback into 13 minutes. One hour matches what the map was
# built around; the console work can ask for more once it has a time axis
# that can show it.
MOMENT_WINDOW_S = 3600.0


def available() -> bool:
    """Can this deployment read products off the filesystem at all?"""
    return bool(SETTINGS.backend_root)


def _root() -> str:
    return os.path.join(SETTINGS.backend_root, *PRODUCT_IMAGES_PREFIX)


def manifest_path(product_id: str, unit: str | None = None) -> str:
    """Absolute path to the product's published manifest.

    Args:
        product_id: The product.
        unit: Which unit's manifest, or None for the canonical one.

    Returns:
        An absolute path.

    Raises:
        KeyError: If the product is not published by this profile.
        ValueError: If the product does not have this unit.
    """
    return os.path.join(_root(), manifest_rel(product_id, unit))


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


async def read_steps(product_id: str,
                     unit: str | None = None) -> list[dict[str, Any]]:
    """The product's step manifest, in the shape productDetail returns.

    Args:
        product_id: The product.
        unit: Which unit's manifest, or None for the canonical one.

    Returns:
        The steps, as the manifest lists them.

    Raises:
        KeyError: If the product is not published by this profile.
        ValueError: If the product does not have this unit.
        TimeoutError: If the read outlives FS_TIMEOUT_S.
    """
    path = manifest_path(product_id, unit)
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


def is_scan_radar(radar: str) -> bool:
    """Whether this deployment can serve frames for a radar id at all.

    Args:
        radar: A radar id from a request.

    Returns:
        True for a radar in RADAR_FOLDER or NEXRAD_SITES.
    """
    return radar in RADAR_FOLDER or radar in NEXRAD_SITES


def plot_folder(radar: str) -> str:
    """The folder name a radar's frames are filed under.

    A NEXRAD site is filed under its own id. Every other radar uses the
    RADAR_FOLDER spelling, which differs from the id (``scvw`` for XSCV).

    Args:
        radar: Radar id.

    Returns:
        The folder.

    Raises:
        KeyError: If the radar is in neither table.
    """
    if radar in NEXRAD_SITES:
        return radar
    return RADAR_FOLDER[radar]


def _plot_layout(radar: str) -> tuple[str, str, str, str]:
    """The tree, name pattern, stamp pattern and stamp format for one radar.

    Args:
        radar: Radar id.

    Returns:
        (tree, name, ts_re, ts_fmt), the NEXRAD plotter's for a NEXRAD site
        and this profile's RADAR_PLOTS_* for every other radar.
    """
    if radar in NEXRAD_SITES:
        return (NEXRAD_PLOTS_TREE, NEXRAD_PLOTS_NAME, NEXRAD_PLOTS_TS_RE,
                NEXRAD_PLOTS_TS_FMT)
    return (RADAR_PLOTS_TREE, RADAR_PLOTS_NAME, RADAR_PLOTS_TS_RE, RADAR_PLOTS_TS_FMT)


def plots_dir(radar: str, moment: str, day: datetime) -> str:
    """Absolute directory holding one radar's frames for one moment, one UTC day.

    Args:
        radar: Radar id as ``RADAR_FOLDER`` keys it, e.g. "XEBY" or "FLOW",
            or a NEXRAD site.
        moment: Moment name in this profile's vocabulary.
        day: Any instant on the UTC day wanted; only the date is read.

    Returns:
        Absolute path, which may not exist.

    Raises:
        KeyError: If the radar or moment is not in this profile's tables.
    """
    tree = _plot_layout(radar)[0]
    rel = tree.format(
        radar=radar, folder=plot_folder(radar), prefix=moment_to_prefix(radar, moment))
    # Format first, then strftime: the template carries both kinds of
    # placeholder and strftime would choke on a stray brace.
    return os.path.join(_backend_root(), day.strftime(rel))


def _frame_re(radar: str, moment: str) -> re.Pattern[str]:
    """Pattern matching one frame filename, capturing its timestamp text.

    Everything but the timestamp is matched literally, so the colorbar each
    directory carries is excluded by having no timestamp rather than by a
    separate rule, and a sibling product cannot pass as a frame.
    """
    parts = {"radar": radar, "folder": plot_folder(radar),
             "prefix": moment_to_prefix(radar, moment)}
    _, name, ts_re, _ = _plot_layout(radar)
    head, tail = name.split("{ts}")
    return re.compile(
        f"^{re.escape(head.format(**parts))}"
        f"({ts_re})"
        f"{re.escape(tail.format(**parts))}$")


def _list_frames_blocking(radar: str, moment: str, days: int) -> list[tuple[datetime, str]]:
    """Scan `days` UTC days back, newest day first, for frames of one moment."""
    pat = _frame_re(radar, moment)
    ts_fmt = _plot_layout(radar)[3]
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
                    m.group(1), ts_fmt)
            except ValueError:
                continue
            found.append((ts.replace(tzinfo=timezone.utc), name))
    found.sort()
    return found


async def list_moment_frames(radar: str, moment: str, days: int = 2,
                             window_s: float = MOMENT_WINDOW_S,
                             ) -> list[tuple[datetime, str]]:
    """Frames for one radar and moment, oldest first, within a recent window.

    Two days are scanned rather than one, because a request just after
    00:00 UTC needs yesterday's directory. That is the rollover LB2 solves
    with a look-back, and it is about reaching the files rather than about
    how many to return.

    Args:
        radar: Radar id as ``RADAR_FOLDER`` keys it.
        moment: Moment name in this profile's vocabulary.
        days: How many UTC days back to scan. Must be at least 1.
        window_s: Keep frames no older than this many seconds. Zero or less
            keeps everything the scan found.

    Returns:
        (timestamp, filename) pairs, oldest first. Empty when the tree has
        nothing in the window, which is not an error.

    Raises:
        ValueError: If `days` is below 1.
        TimeoutError: If the mount does not answer within FS_TIMEOUT_S.
    """
    if days < 1:
        raise ValueError(f"days must be at least 1, got {days}")
    frames = await asyncio.wait_for(
        asyncio.to_thread(_list_frames_blocking, radar, moment, days), FS_TIMEOUT_S)
    if window_s <= 0:
        return frames
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=window_s)
    return [f for f in frames if f[0] >= cutoff]


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


# ---------------------------------------------------------------------------
# Per-elevation tilt frames (radar-display's document root)
# ---------------------------------------------------------------------------
#
# radardisplay.engr.colostate.edu serves this tree verbatim, so the URL path
# and the path on disk are the same string under a different root. Two frames
# fetched over HTTP on 2026-11-21 matched the files byte for byte.
#
# Its own root rather than a subpath of ``backend_root``: the tree sits under
# the AQPI web-files root on the main archive and carries FLOW alongside the five AQPI
# radars, while the XQPI deployment mounts only the secondary archive. One derived path
# would be right for one profile and a decoy for the other.

# What a tilt directory name may contain. The radar and the elevation reach
# these functions from query parameters, so both are checked before they are
# joined into a path. The product path is confined with ``realpath`` because
# its source string is free-form; these are confined by vocabulary, which is
# stricter and says so at the call. The cap fits the longest moment name the
# renderer writes, copolarcorrelation at 18 characters, with room to spare.
_TILT_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,31}$")
_TILT_EL_RANGE = (1, 4)


def tilt_root() -> str | None:
    """The mounted radar-display root, or None when this deployment has none.

    Returns:
        An absolute path, or None to leave the tilt routes on HTTP.
    """
    if SETTINGS.tilt_root:
        return str(SETTINGS.tilt_root)
    if SETTINGS.backend_root and TILT_TREE:
        return os.path.join(str(SETTINGS.backend_root), TILT_TREE)
    return None


def tilt_available() -> bool:
    """Can this deployment read tilts off the filesystem at all?"""
    return tilt_root() is not None


def _tilt_path(template: str, radar: str, el: int, name: str) -> str:
    """Absolute path to one file in a tilt directory.

    Args:
        template: ``TILT_IMAGE_DIR`` or ``TILT_JSON_DIR``.
        radar: Radar id as the directory names it.
        el: Elevation index, 1 to 4.
        name: The file inside that directory.

    Returns:
        An absolute path, which may not exist.

    Raises:
        RuntimeError: If no tilt root is configured; call ``tilt_available``.
        ValueError: If the radar or the elevation is outside its vocabulary.
    """
    root = tilt_root()
    if root is None:
        raise RuntimeError("no tilt root; call tilt_available() first")
    if _TILT_NAME_RE.match(radar) is None:
        raise ValueError(f"not a radar id: {radar!r}")
    if not _TILT_EL_RANGE[0] <= el <= _TILT_EL_RANGE[1]:
        raise ValueError(f"elevation out of range: {el}")
    return os.path.join(root, template.format(radar=radar, el=el), name)


def _read_json_blocking(path: str) -> dict[str, Any] | None:
    """One radar_plot JSON, or None when it is missing or not an object."""
    try:
        with open(path, "rb") as fh:
            doc = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None
    return doc if isinstance(doc, dict) else None


async def read_tilt_json(radar: str, el: int,
                         frame: int) -> dict[str, Any] | None:
    """Metadata for one tilt frame, in the shape radar-display serves.

    Args:
        radar: Radar id as the directory names it.
        el: Elevation index, 1 to 4.
        frame: Position in the rolling window, 0 being newest.

    Returns:
        The parsed object, or None when the file is absent or unreadable.
        The HTTP path returns None for those same conditions.

    Raises:
        ValueError: If the radar or the elevation is outside its vocabulary.
        TimeoutError: If the mount does not answer within FS_TIMEOUT_S.
    """
    path = _tilt_path(TILT_JSON_DIR, radar, el, f"radar_plot_{frame}.json")
    return await asyncio.wait_for(
        asyncio.to_thread(_read_json_blocking, path), FS_TIMEOUT_S)


async def read_tilt_image(radar: str, el: int, moment: str,
                          frame: int) -> FsResponse:
    """One tilt frame, as a response ``_fetch_image_upstream`` can consume.

    Args:
        radar: Radar id as the directory names it.
        el: Elevation index, 1 to 4.
        moment: Moment name as the filename spells it.
        frame: Position in the rolling window, 0 being newest.

    Returns:
        The frame, or a 404 response when it is missing.

    Raises:
        ValueError: If the radar, elevation or moment is outside its
            vocabulary.
        TimeoutError: If the mount does not answer within FS_TIMEOUT_S.
    """
    if _TILT_NAME_RE.match(moment) is None:
        raise ValueError(f"not a moment name: {moment!r}")
    path = _tilt_path(TILT_IMAGE_DIR, radar, el, f"{moment}_{frame}.png")
    return await asyncio.wait_for(
        asyncio.to_thread(_read_image_blocking, path), FS_TIMEOUT_S)
