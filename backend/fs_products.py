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
from typing import Any

from .config import PRODUCT_IMAGES_PREFIX, PRODUCTS, SETTINGS

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
