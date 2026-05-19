"""Plain-English mapping for exception types.

Radar engineers should not have to read `httpx.ConnectTimeout` or
`gaierror` in a status summary. This module centralizes the translation
from Python exception class names to short, human-readable phrases so
every check / route that surfaces a transport error reads the same way.

Keep the catalog small and tightly scoped to errors that actually show
up in production. For unmapped classes the helper falls back to a
camel-case split (e.g. `RemoteDisconnected` → `Remote Disconnected`) so
even un-anticipated types stay readable.

The technical class name is dropped from user-visible strings but
should still be persisted in structured payload fields by the caller
(see scheduler.py:_handle_check_error) for downstream diagnostics.
"""
from __future__ import annotations

_FRIENDLY: dict[str, str] = {
    # httpx transport / connection
    "ConnectTimeout":           "Connection timed out",
    "ConnectError":             "Could not connect",
    "ConnectionRefusedError":   "Connection refused",
    "ConnectionResetError":     "Connection was reset",
    "ConnectionAbortedError":   "Connection aborted",
    "ReadTimeout":              "Server took too long to respond",
    "WriteTimeout":             "Connection stalled while sending",
    "PoolTimeout":              "Network busy",
    "RemoteProtocolError":      "Server returned an invalid response",
    "RemoteDisconnected":       "Server disconnected unexpectedly",
    "ProtocolError":            "Protocol error",
    # asyncio
    "TimeoutError":             "Operation timed out",
    "CancelledError":           "Operation was cancelled",
    # name resolution
    "gaierror":                 "DNS lookup failed",
    "herror":                   "DNS lookup failed",
    # TLS
    "SSLError":                 "TLS handshake failed",
    "SSLZeroReturnError":       "Connection closed during TLS handshake",
    "CertificateError":         "TLS certificate is invalid",
    "SSLCertVerificationError": "TLS certificate could not be verified",
    # HTTP-layer
    "TooManyRedirects":         "Too many redirects",
    "InvalidURL":               "Invalid URL",
    "HTTPStatusError":          "Server returned an error",
    "DecodingError":            "Response body could not be decoded",
    "JSONDecodeError":          "Server returned malformed JSON",
    # OS / socket
    "OSError":                  "Network error",
    "BrokenPipeError":          "Connection was broken",
}


def _split_camel(name: str) -> str:
    """Insert spaces before capital letters that follow lower-case."""
    out: list[str] = []
    prev_lower = False
    for ch in name:
        if ch.isupper() and prev_lower:
            out.append(" ")
        out.append(ch)
        prev_lower = ch.islower()
    return "".join(out)


def humanize_error(exc: BaseException | str | None) -> str:
    """Plain-English description of `exc`.

    Accepts an exception instance, a bare class-name string, or None.
    For instances, walks `__cause__` / `__context__` looking for the
    deepest mapped type — httpx wraps many transport errors and the
    inner cause is usually the more honest description.
    """
    if exc is None:
        return "Unknown error"
    if isinstance(exc, str):
        return _FRIENDLY.get(exc, _split_camel(exc) if exc else "Unknown error")

    seen: list[str] = []
    cur: BaseException | None = exc
    while cur is not None:
        name = type(cur).__name__
        seen.append(name)
        if name in _FRIENDLY:
            return _FRIENDLY[name]
        # Stop unbounded chain walks (cycles in __context__ are rare but
        # possible; keep this defensive).
        if len(seen) > 8:
            break
        cur = cur.__cause__ or cur.__context__

    # No mapping hit. Use the outermost class with a camel-case split so
    # at least the result reads like words rather than a Python identifier.
    return _split_camel(seen[0]) if seen else "Unknown error"
