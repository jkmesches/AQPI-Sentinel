"""One-shot: retroactively cascade-demote historical check_runs rows.

For every fail/error row, walk depends_on transitively at that row's
finished_at, find any ancestor that was unhealthy at the time, and
demote the row to status=skip with reason=upstream_unhealthy. Same
mutation the scheduler now does at run time, applied to rows that
existed BEFORE the cascade-demote feature shipped.

Idempotent — every rewritten row gets payload.cascade_retro_v=1 and
originals stashed under payload.original_status / .original_summary.
Re-running is a no-op.

Run inside the backend container so SENTINEL_DB_URL resolves and the
local /api/checks endpoint is reachable:
    docker exec sentinel-backend python /tmp/cascade_retro.py [--dry]
"""
from __future__ import annotations

import asyncio
import bisect
import json
import os
import sys

import asyncpg
import httpx


# ---------------------------------------------------------------------------
# Plain-English label helper — minimal port of backend/check_labels.py.
# Keeping it inlined avoids importing backend.registry which triggers
# circular-init issues when this script is run via `docker exec`.
# ---------------------------------------------------------------------------

_PRODUCTS = {
    "comp_ref": "Composite Reflectivity", "comp_now": "Composite Nowcast",
    "qpe_15min": "QPE 15-min", "qpe_1hr": "QPE 1-hour",
    "precip_rate_radar": "Precipitation Rate",
    "fcst_total_precip": "Forecast — Total Precipitation",
    "fcst_total_precip_cum": "Forecast — Cumulative Precipitation",
    "fcst_precip_rate": "Forecast — Precip Rate",
    "fcst_temp": "Forecast — Temperature",
    "water_level": "Water Level", "water_depth": "Water Depth",
    "max_water_level": "Max Water Level", "max_water_depth": "Max Water Depth",
}
_L0_TARGETS = {
    "tls_cert": "TLS certificate", "origin_alive": "Origin reachable",
    "public": "Public dashboard page", "root_notfound": "Root URL (404 check)",
    "internet": "Sentinel Internet", "dns": "Sentinel DNS",
}


def pretty_check_label(check_id: str, target: str) -> str:
    cid = check_id or ""
    t = target or ""
    if cid.startswith("layer0.tls."):           return "TLS certificate"
    if cid.startswith("layer0.origin."):        return "Origin reachable"
    if cid.startswith("layer0.website.public"): return "Public dashboard page"
    if cid.startswith("layer0.website.root"):   return "Root URL (404 check)"
    if cid.startswith("layer0.net.internet"):   return "Sentinel Internet"
    if cid.startswith("layer0.net.dns"):        return "Sentinel DNS"
    if cid.startswith("layer0."):               return _L0_TARGETS.get(t) or t.replace("_", " ").title()
    if cid.startswith("layer1.product."):       return _PRODUCTS.get(t) or t.replace("_", " ").title()
    if cid.startswith("layer1.stream."):        return "Stream Reach canary"
    if cid.startswith("layer1.vector."):        return t.replace("_", " ").title()
    if cid.startswith("layer2.radar."):         return t or cid
    if cid.startswith("layer3."):               return f"Overlay reconcile — {_PRODUCTS.get(t, t)}"
    if cid.startswith("layer4.xband."):         return t or cid
    if cid.startswith("layer4."):               return _PRODUCTS.get(t) or t.replace("_", " ").title()
    return t.replace("_", " ").title() if t else cid


# ---------------------------------------------------------------------------
# Dep graph + status timeline helpers
# ---------------------------------------------------------------------------

def transitive_deps(deps: dict[str, list[str]], check_id: str) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    frontier = list(deps.get(check_id) or [])
    while frontier:
        nxt: list[str] = []
        for d in frontier:
            if d in seen:
                continue
            seen.add(d)
            out.append(d)
            nxt.extend(deps.get(d) or [])
        frontier = nxt
    return out


async def load_status_timeline(pool, check_id: str) -> tuple[list[float], list[str]]:
    """Sorted (finished_at_epoch, raw_status) for one check. Uses
    payload.original_status when present so already-demoted ancestors
    still reveal their underlying unhealthy state — same logic the live
    `_find_unhealthy_ancestor` performs via the in-process cache."""
    rows = await pool.fetch(
        """
        SELECT
            EXTRACT(EPOCH FROM finished_at) AS ts,
            CASE
                WHEN payload->>'original_status' IS NOT NULL
                    THEN payload->>'original_status'
                ELSE status
            END AS eff
        FROM check_runs
        WHERE check_id = $1
        ORDER BY finished_at ASC
        """,
        check_id,
    )
    return ([float(r["ts"]) for r in rows], [r["eff"] for r in rows])


def status_at(tl: tuple[list[float], list[str]], ts: float) -> str | None:
    epochs, statuses = tl
    if not epochs or ts < epochs[0]:
        return None
    idx = bisect.bisect_right(epochs, ts) - 1
    return statuses[idx] if idx >= 0 else None


async def main() -> int:
    url = os.environ.get("SENTINEL_DB_URL")
    if not url:
        print("SENTINEL_DB_URL is not set", file=sys.stderr)
        return 2

    dry = "--dry" in sys.argv or os.environ.get("DRY") == "1"

    # Pull the live registry over HTTP — avoids circular imports + always
    # reflects what the scheduler thinks the dep graph is right now.
    async with httpx.AsyncClient(timeout=15) as http:
        r = await http.get("http://localhost:8000/api/checks")
        r.raise_for_status()
        checks = r.json()
    deps: dict[str, list[str]] = {c["id"]: list(c.get("depends_on") or []) for c in checks}
    targets_by_id: dict[str, str] = {c["id"]: c.get("target") or "" for c in checks}

    candidates = [cid for cid, d in deps.items() if d]
    print(f"checks with depends_on: {len(candidates)} of {len(deps)}")

    ancestor_ids: set[str] = set()
    for cid in candidates:
        ancestor_ids.update(transitive_deps(deps, cid))
    print(f"distinct ancestors to load: {len(ancestor_ids)}")

    pool = await asyncpg.create_pool(url, min_size=1, max_size=4)
    timelines: dict[str, tuple[list[float], list[str]]] = {}
    for aid in ancestor_ids:
        timelines[aid] = await load_status_timeline(pool, aid)

    total_changes = 0
    for cid in candidates:
        rows = await pool.fetch(
            """
            SELECT id, finished_at, status, summary, payload, target, stage
            FROM check_runs
            WHERE check_id = $1
              AND status IN ('fail', 'error')
              AND (payload IS NULL OR (payload->>'cascade_retro_v') IS NULL)
              AND (payload IS NULL OR (payload->>'reason') IS DISTINCT FROM 'upstream_unhealthy')
            ORDER BY finished_at
            """,
            cid,
        )
        if not rows:
            continue
        cid_deps = transitive_deps(deps, cid)
        per_check_changes = 0
        for r in rows:
            ts = r["finished_at"].timestamp()
            suppressor: str | None = None
            for aid in cid_deps:
                tl = timelines.get(aid)
                if not tl:
                    continue
                st = status_at(tl, ts)
                if st in ("fail", "error"):
                    suppressor = aid
                    break
            if not suppressor:
                continue
            per_check_changes += 1
            label = pretty_check_label(suppressor, targets_by_id.get(suppressor, ""))
            if dry:
                if per_check_changes <= 3:
                    print(f"  [run #{r['id']}] {r['status']} → skip (upstream={label})")
                continue
            payload = r["payload"]
            if isinstance(payload, str):
                payload = json.loads(payload)
            elif payload is None:
                payload = {}
            payload = dict(payload)
            payload.setdefault("original_status", r["status"])
            payload.setdefault("original_summary", r["summary"])
            payload["reason"] = "upstream_unhealthy"
            payload["suppressed_by"] = suppressor
            payload["cascade_retro_v"] = 1
            await pool.execute(
                "UPDATE check_runs "
                "SET status = 'skip', summary = $2, payload = $3 "
                "WHERE id = $1",
                r["id"],
                f'Upstream "{label}" unhealthy',
                json.dumps(payload),
            )
        if per_check_changes:
            print(f"  {cid}: {per_check_changes} rows demoted")
        total_changes += per_check_changes

    print(f"\n{'DRY: would demote' if dry else 'demoted'} {total_changes} rows total")
    await pool.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
