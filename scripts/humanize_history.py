"""One-shot: rewrite historical alarm/run summaries to plain English.

The rewriter targets a fixed set of legacy prefix patterns that the
earlier check code embedded raw exception class names into. Each rule
matches a literal prefix and humanizes the trailing fragment via the
same vocabulary as backend.errors._FRIENDLY (kept inlined here so the
script can run standalone in any environment with asyncpg).

Idempotent — every rewritten row gets `payload.humanized_v = 1` and the
original text stashed under `payload.raw_summary` / `payload.raw_message`.
A second pass skips already-marked rows. Safe to re-run.

Run inside the backend container so the SENTINEL_DB_URL env var
resolves to the right pool:
    docker exec sentinel-backend python /tmp/humanize_history.py
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys

import asyncpg


# ---------------------------------------------------------------------------
# Translation table — mirrors backend/errors.py._FRIENDLY for the classes we
# actually saw in production rows. Trimmed to keep the diff narrow; expand
# only if a re-scan turns up new technical names.
# ---------------------------------------------------------------------------
_FRIENDLY: dict[str, str] = {
    "ConnectTimeout":           "Connection timed out",
    "ConnectError":             "Could not connect",
    "ConnectionRefusedError":   "Connection refused",
    "ConnectionResetError":     "Connection was reset",
    "ReadTimeout":              "Server took too long to respond",
    "WriteTimeout":             "Connection stalled while sending",
    "PoolTimeout":              "Network busy",
    "RemoteProtocolError":      "Server returned an invalid response",
    "TimeoutError":             "Operation timed out",
    "CancelledError":           "Operation was cancelled",
    "SSLError":                 "TLS handshake failed",
    "SSLCertVerificationError": "TLS certificate could not be verified",
    "gaierror":                 "DNS lookup failed",
    "OSError":                  "Network error",
    "JSONDecodeError":          "Server returned malformed JSON",
    "HTTPStatusError":          "Server returned an error",
}

_DNS_MARKERS = (
    "[errno -2]", "[errno -3]", "[errno -5]",
    "gaierror",
    "no address associated with hostname",
    "name or service not known",
    "temporary failure in name resolution",
    "dns lookup failed",
)


def _tail_friendly(tail: str) -> str:
    """Translate the fragment that followed a known prefix."""
    s = (tail or "").strip().lstrip(":").strip()
    if not s:
        return ""
    low = s.lower()
    if any(m in low for m in _DNS_MARKERS):
        return "DNS lookup failed"
    m = re.match(r"^([A-Za-z][A-Za-z_]*)", s)
    if m and m.group(1) in _FRIENDLY:
        return _FRIENDLY[m.group(1)]
    # Already-friendly phrases pass through.
    if "timed out" in low:
        return "Operation timed out"
    if "connection refused" in low:
        return "Connection refused"
    return s


def humanize(s: str | None, depth: int = 0) -> str | None:
    """Rewrite a single summary/message string. Returns None if `s` is None
    (no change). Recurses once for nested `local DNS unavailable: ...`
    payloads — never deeper.
    """
    if s is None:
        return None
    s_in = s
    # Prefix rules — order matters: the DNS-wrapper rule recurses on the
    # body, so other prefixes inside it still get rewritten.
    m = re.match(r"^local DNS unavailable:\s*(.*)$", s)
    if m:
        body = m.group(1).strip().lstrip(":").strip()
        if depth < 2 and body:
            body = humanize(body, depth + 1) or "DNS lookup failed"
        body = body or "DNS lookup failed"
        return f"Local DNS unavailable ({body})"

    for pat, head in (
        (r"^A_api transport:\s*(.*)$",         "Manifest fetch failed"),
        (r"^image transport:\s*(.*)$",         "Image fetch failed"),
        (r"^TLS probe failed:\s*(.*)$",        "TLS probe failed"),
        (r"^page load failed:\s*(.*)$",        "Page load failed"),
        (r"^productDetail unreachable:?\s*(.*)$", "Upstream unavailable"),
    ):
        m = re.match(pat, s)
        if m:
            tail = _tail_friendly(m.group(1))
            return f"{head}: {tail}".rstrip(": ").rstrip()

    # Bare `ClassName: msg` (scheduler's old format).
    m = re.match(r"^([A-Z][A-Za-z]+):\s*(.*)$", s)
    if m and m.group(1) in _FRIENDLY:
        return _FRIENDLY[m.group(1)]

    return s_in  # unchanged


async def main() -> int:
    url = os.environ.get("SENTINEL_DB_URL")
    if not url:
        print("SENTINEL_DB_URL is not set", file=sys.stderr)
        return 2

    dry = "--dry" in sys.argv or os.environ.get("DRY") == "1"
    pool = await asyncpg.create_pool(url)

    # ---- check_runs.summary -------------------------------------------------
    runs = await pool.fetch(
        """
        SELECT id, summary, payload
        FROM check_runs
        WHERE summary IS NOT NULL
          AND (payload IS NULL OR (payload->>'humanized_v') IS NULL)
          AND summary ~ '^(A_api transport|image transport|TLS probe failed|page load failed|local DNS unavailable|productDetail unreachable|[A-Z][A-Za-z]+):'
        """
    )
    run_changes = 0
    for r in runs:
        old = r["summary"]
        new = humanize(old) or old
        if new == old:
            continue
        run_changes += 1
        if dry:
            print(f"[run #{r['id']}] {old!r} → {new!r}")
            continue
        payload = r["payload"]
        if isinstance(payload, str):
            payload = json.loads(payload)
        elif payload is None:
            payload = {}
        payload = dict(payload)
        payload.setdefault("raw_summary", old)
        payload["humanized_v"] = 1
        await pool.execute(
            "UPDATE check_runs SET summary = $2, payload = $3 WHERE id = $1",
            r["id"], new, json.dumps(payload),
        )

    # ---- alarms.message -----------------------------------------------------
    alarms = await pool.fetch(
        """
        SELECT id, message, payload
        FROM alarms
        WHERE message IS NOT NULL
          AND (payload IS NULL OR (payload->>'humanized_v') IS NULL)
          AND message ~ '^(A_api transport|image transport|TLS probe failed|page load failed|local DNS unavailable|productDetail unreachable|[A-Z][A-Za-z]+):'
        """
    )
    alarm_changes = 0
    for a in alarms:
        old = a["message"]
        new = humanize(old) or old
        if new == old:
            continue
        alarm_changes += 1
        if dry:
            print(f"[alarm #{a['id']}] {old!r} → {new!r}")
            continue
        payload = a["payload"]
        if isinstance(payload, str):
            payload = json.loads(payload)
        elif payload is None:
            payload = {}
        payload = dict(payload)
        payload.setdefault("raw_message", old)
        payload["humanized_v"] = 1
        await pool.execute(
            "UPDATE alarms SET message = $2, payload = $3 WHERE id = $1",
            a["id"], new, json.dumps(payload),
        )

    print(f"{'DRY: would rewrite' if dry else 'rewrote'} "
          f"{run_changes} check_runs.summary, "
          f"{alarm_changes} alarms.message")
    await pool.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
