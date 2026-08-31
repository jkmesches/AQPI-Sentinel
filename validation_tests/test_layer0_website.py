"""Layer 0 — website / edge alive.

Validates: site reachable, correct app served, TLS cert healthy, origin not
just a stuck cache.
"""
from __future__ import annotations
import socket, ssl, subprocess, sys, time
from datetime import datetime, timezone
import requests

# Live-probe timeout. Deliberately matches the production ceiling
# (transports/http.DEFAULT_TIMEOUT_S) rather than being a round number: these
# tests hit the same endpoint the fleet does, and a ceiling below it fails for
# reasons unrelated to what is being tested. The 10s values here were set when
# upstream's p95 was ~2.4s; measured 2026-08-31 its p90 is 8-13s, so a 10s
# ceiling coin-flips. Raise this only alongside DEFAULT_TIMEOUT_S.
LIVE_TIMEOUT_S = 20

BASE = "https://radarca.engr.colostate.edu"
HOST = "radarca.engr.colostate.edu"
PUBLIC_MARKERS = ("Radar Data", "Atmospheric Forecast", "CoSMoS Data",
                  "National Water Model", "Reflectivity")
NOTFOUND_MARKERS = ("404: This page could not be found",
                    "next-error-h1", "_next/static")
MIN_PUBLIC_BYTES = 50_000
TLS_DAYS_FLOOR = 30


def check_public():
    r = requests.get(f"{BASE}/public", timeout=LIVE_TIMEOUT_S)
    ok_status = r.status_code == 200
    ok_type = r.headers.get("content-type", "").startswith("text/html")
    ok_size = len(r.content) >= MIN_PUBLIC_BYTES
    markers = {m: (m in r.text) for m in PUBLIC_MARKERS}
    ok_markers = all(markers.values())
    return {
        "pass": all([ok_status, ok_type, ok_size, ok_markers]),
        "http": r.status_code, "bytes": len(r.content),
        "content_type": r.headers.get("content-type", ""),
        "markers": markers,
        "nextjs_cache": r.headers.get("x-nextjs-cache", ""),
    }


def check_root_notfound():
    """Catch-all route renders Next.js not-found. Note: Next serves it with
    HTTP 200, so we assert via body markers, not status code."""
    r = requests.get(f"{BASE}/", timeout=LIVE_TIMEOUT_S)
    markers = {m: (m in r.text) for m in NOTFOUND_MARKERS}
    return {
        "pass": all(markers.values()),
        "http": r.status_code, "bytes": len(r.content),
        "markers": markers,
    }


def check_tls():
    ctx = ssl.create_default_context()
    with socket.create_connection((HOST, 443), timeout=LIVE_TIMEOUT_S) as s:
        with ctx.wrap_socket(s, server_hostname=HOST) as ss:
            cert = ss.getpeercert()
    not_after = datetime.strptime(cert["notAfter"], "%b %d %H:%M:%S %Y %Z").replace(tzinfo=timezone.utc)
    days = (not_after - datetime.now(timezone.utc)).days
    return {
        "pass": days > TLS_DAYS_FLOOR,
        "not_after": cert["notAfter"], "days_left": days,
        "subject": dict(x[0] for x in cert["subject"]),
        "issuer": dict(x[0] for x in cert["issuer"]),
    }


def check_dns():
    try:
        ips = sorted({i[4][0] for i in socket.getaddrinfo(HOST, 443)})
        return {"pass": bool(ips), "ips": ips}
    except Exception as e:
        return {"pass": False, "error": str(e)}


def check_origin_alive():
    """/public is served from Next.js full-route cache (s-maxage=31536000,
    HIT permanently) — it stays 200 even if origin is dead. Use an /api route
    instead; those are no-store, so a 200 here proves the Django app is live.
    """
    r = requests.get(f"{BASE}/api/radar-status/", timeout=LIVE_TIMEOUT_S)
    return {
        "pass": r.status_code == 200 and r.headers.get("content-type", "").startswith("application/json"),
        "http": r.status_code,
        "cache_control": r.headers.get("cache-control", ""),
        "bytes": len(r.content),
    }


def main():
    results = {
        "public":        check_public(),
        "root_notfound": check_root_notfound(),
        "tls":           check_tls(),
        "dns":           check_dns(),
        "origin_alive":  check_origin_alive(),
    }
    width = max(len(k) for k in results)
    n_pass = sum(1 for v in results.values() if v["pass"])
    for name, r in results.items():
        flag = "PASS" if r["pass"] else "FAIL"
        print(f"[{flag}] {name:<{width}}  {summary(name, r)}")
    print(f"\n{n_pass}/{len(results)} checks passed")
    return 0 if n_pass == len(results) else 1


def summary(name, r):
    if name == "public":
        miss = [m for m, ok in r["markers"].items() if not ok]
        s = f"HTTP {r['http']} {r['bytes']}B cache={r['nextjs_cache'] or '-'}"
        return s + (f" missing-markers={miss}" if miss else "")
    if name == "root_notfound":
        miss = [m for m, ok in r["markers"].items() if not ok]
        return f"HTTP {r['http']} {r['bytes']}B" + (f" missing={miss}" if miss else "")
    if name == "tls":
        return f"expires {r['not_after']} ({r['days_left']} days)"
    if name == "dns":
        return f"ips={r.get('ips') or r.get('error')}"
    if name == "origin_alive":
        return f"HTTP {r['http']} {r['bytes']}B cache-control={r['cache_control'] or '-'}"
    return str(r)


if __name__ == "__main__":
    sys.exit(main())
