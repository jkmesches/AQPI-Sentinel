# Adding a new check

Sentinel checks are small Python classes that get auto-registered
and scheduled. Adding one is intentionally low-ceremony — you write
the class, drop it into `backend/checks/`, and the system picks
it up on the next backend start.

This doc walks through the structural pieces and a worked example.

**Companion**: [`ARCHITECTURE.md`](ARCHITECTURE.md) for the full
flow from check tick to alarm dispatch.

---

## The Check class shape

Every check inherits from `backend.checks.base.Check`:

```python
class Check:
    id:         str             # unique, dotted: "layerN.kind.identifier"
    stage:      str             # one of "L0", "L1", "L2", "L3", "L4-T1T2"
    target:     str             # what's being checked (a product id, radar code, etc.)
    cadence_s:  int             # seconds between runs
    depends_on: list[str]       # other check_ids this depends on

    async def run(self, ctx: CheckContext) -> CheckResult: ...
```

The class is **registered** by calling `register(instance)` at
module load — see existing modules for the pattern.

**`CheckResult`** is the envelope your `run()` method returns:

```python
@dataclass
class CheckResult:
    check_id:    str
    target:      str
    stage:       str
    status:      Literal['pass', 'warn', 'fail', 'error', 'skip']
    started_at:  datetime
    finished_at: datetime
    summary:     str                          # one-line human-readable
    payload:     dict | None  = None          # structured data for drilldown
    metrics:     dict | None  = None          # numeric metrics for sparklines
```

The scheduler handles exceptions for you — if `run()` raises, the
scheduler catches it and emits a `status=error` CheckResult with the
humanized exception in the summary (see
[`backend/errors.py`](https://github.com/jkmesches/SentinelProject/blob/main/backend/errors.py)).
So you only need explicit error handling for cases where you want a
specific summary string.

---

## Worked example: a tiny new check

Let's add a check that probes a hypothetical `/api/observatory-status`
endpoint on the upstream and asserts a specific JSON field equals
`OPERATIONAL`.

### 1. Pick a place

Decide which stage layer this belongs to. The probe is upstream
connectivity-adjacent (asserts an endpoint returns something
sensible), so it's L0. Create
`backend/checks/layer0_observatory.py`.

### 2. Write the class

```python
"""Observatory status check — asserts /api/observatory-status
returns OPERATIONAL."""
from __future__ import annotations

from ..config import SETTINGS
from ..errors import humanize_error
from ..registry import register
from .base import Check, CheckResult, utcnow


class ObservatoryStatusCheck(Check):
    id         = "layer0.observatory.status"
    target     = "observatory"
    stage      = "L0"
    cadence_s  = 120
    # Demote to skip if the origin itself is unhealthy — avoids
    # painting this cell red when the actual root cause is upstream.
    depends_on: list[str] = ["layer0.origin.alive"]

    async def run(self, ctx) -> CheckResult:
        t0 = utcnow()
        url = f"{SETTINGS.base}/api/observatory-status"

        # The scheduler catches transport exceptions for us, so we
        # only handle in-band failures here. ctx.http is the shared
        # httpx client with the right timeout + User-Agent.
        r = await ctx.http.get(url)
        if r.status_code != 200:
            return CheckResult(
                check_id=self.id, target=self.target, stage=self.stage,
                status="fail",
                started_at=t0, finished_at=utcnow(),
                summary=f"HTTP {r.status_code}",
                payload={"http": r.status_code},
            )
        try:
            doc = r.json()
        except ValueError:
            return CheckResult(
                check_id=self.id, target=self.target, stage=self.stage,
                status="fail",
                started_at=t0, finished_at=utcnow(),
                summary="Response was not JSON",
                payload={"bytes": len(r.content)},
            )

        status_field = (doc.get("status") or "").upper()
        passed = status_field == "OPERATIONAL"
        return CheckResult(
            check_id=self.id, target=self.target, stage=self.stage,
            status="pass" if passed else "fail",
            started_at=t0, finished_at=utcnow(),
            summary=f"observatory={status_field or 'MISSING'}",
            payload={
                "observatory_status": status_field,
                "raw":                doc,
            },
            metrics={"latency_ms": (utcnow() - t0).total_seconds() * 1000},
        )


register(ObservatoryStatusCheck())
```

### 3. Wire it into `backend/checks/__init__.py`

Add the import so the module loads at package import time:

```python
# backend/checks/__init__.py
from . import layer0_observatory  # noqa: F401
```

`register()` runs at import time, which means the check is in the
`CHECKS` global by the time the scheduler asks for it.

### 4. Add the user-facing label

`frontend/src/lib/format.ts:prettyCheckLabel()`:

```typescript
if (checkId.startsWith('layer0.observatory.')) return 'Observatory status';
```

And `backend/check_labels.py:pretty_check_label()` (mirror —
keep in sync):

```python
if cid.startswith("layer0.observatory."): return "Observatory status"
```

### 5. Restart the backend

```bash
docker compose -f ops/docker-compose.prod.yml \
    --env-file ops/.env.prod up -d --build backend
```

`scheduler started with N checks` — N should be one higher than before.

### 6. Verify

- Hit `/api/checks` — the new check should appear with its
  `depends_on` array.
- Hit `/api/status` — within `cadence_s`, the new check shows up
  in the L0 stage with a fresh result.
- The dashboard's Site rail (left panel of `/`) shows it
  automatically — no UI change needed.
- Open the drilldown — the payload + metrics are visible.

---

## Sub-check verdicts

For richer checks (anything more complex than a single yes/no
probe), use **sub-check verdicts**. The L1 product check is the
canonical example:

```python
sub = {}
sub["A_api"] = "pass" if r.status_code == 200 else "fail"
sub["B_schema"] = "pass" if has_valid_schema(doc) else "fail"
sub["C_freshness"] = "pass" if age_s <= max_age else "fail"
# ...
overall_status = worst_of(*sub.values())
```

The `worst_of()` helper in `backend/checks/helpers.py` folds the
sub-checks into a single overall status. Store the sub-checks in
`payload.sub_status` so the drilldown can show each verdict
individually:

```python
return CheckResult(
    ...,
    status=worst_of(*sub.values()),
    payload={"sub_status": sub, ...},
)
```

The naming convention (`A_`, `B_`, `C_`, ...) gives the drilldown a
stable order.

---

## Metrics and sparklines

Anything in `metrics` becomes available to the sparkline + history
metric API. Two conventions:

1. **Count-like metrics** (`n_steps`, `images_Reflectivity`,
   `image_bytes`) — integer or float counts. Sparkline plots
   arrival rate per cadence bucket.
2. **Age / latency** (`age_s`, `latency_ms`, `primary_age_s`) —
   single-shot numbers per run. Sparkline still plots arrival rate
   (one bucket = one sample).

The frontend's `Sparkline` component knows the cadence and picks
the right window automatically (see
[`92-glossary.md` § Sparklines](92-glossary.md#sparklines)).

---

## Thresholds

If your check has tunable knobs, **don't hard-code them**. Use the
threshold registry:

```python
from .. import thresholds as _thresholds

class MyCheck(Check):
    async def run(self, ctx):
        # ...
        max_age = _thresholds.get_product(self.target, "max_freshness_s")
        # ... or for radar-level knobs:
        silent_fail = _thresholds.get_radar(self.target, "silent_fail_s")
```

The registry falls through DB → `config.py` → hard-coded sentinels
(see [`92-glossary.md` § Threshold
registry](92-glossary.md#threshold-registry)). Adding a knob via
`config.py` makes it editable in `/admin/thresholds` automatically.

---

## Cadence guidance

A few rules of thumb:

- **60–120 s** for connectivity and high-cadence freshness checks.
- **300–600 s** for moderately-cadenced products.
- **30–60 min** for slow-cadence forecasts.
- **1 h+** for static-asset checks (TLS cert expiry, vector
  overlays).
- Avoid sub-30-second cadences — every check shares the same
  upstream and the same asyncpg pool. 30 s is the practical floor.

The scheduler stagger initial-start by topological rank, so a low-
cadence check at rank 0 doesn't block your high-cadence rank-1
checks on cold start.

---

## Testing locally

`scripts/restart-be.sh` cycles the backend with hot dependencies
intact. Or, the slow-but-safe path:

```bash
docker compose -f ops/docker-compose.dev.yml --env-file ops/.env.prod \
    up -d --build backend
docker logs -f sentinel-backend
```

You'll see `scheduler started with N checks` and per-check
`evaluating ...` lines at DEBUG level.

To exercise a specific check in isolation, the simplest path is a
quick standalone script:

```python
# scratch.py
import asyncio
from backend.checks.layer0_observatory import ObservatoryStatusCheck
from backend.checks.transports.http import HttpClient

class _Ctx:
    http = HttpClient()

async def main():
    r = await ObservatoryStatusCheck().run(_Ctx())
    print(r)

asyncio.run(main())
```

Run with `.venv/bin/python scratch.py`.

---

## What to NOT do

- **Don't mutate global state** from inside `run()`. Each tick is
  isolated; the scheduler doesn't guarantee serialization across
  different checks.
- **Don't catch `asyncio.CancelledError`** — let it propagate.
  The scheduler uses cancellation to stop checks on shutdown.
- **Don't block on synchronous I/O** without `to_thread()`. Pure
  Python I/O wrapping a sync library hangs the entire event loop.
- **Don't open your own connection pool**. Use `ctx.http` (the
  shared httpx client) and `ctx.pool` (the shared asyncpg pool).
- **Don't bypass the threshold registry** for knobs that
  operators should be able to edit at runtime.
- **Don't emit `status=error` for "expected" failures** — that's
  reserved for the check itself crashing. Use `fail` for "the
  probe found something wrong" and `error` only for transport /
  parse failures.

---

## Where to go from here

- **[`14-extending-api.md`](#)** — adding API endpoints (custom
  metrics, admin actions, external-facing routes).
- **[`16-alarm-engine.md`](#)** — adding custom alarm sinks
  (Slack, PagerDuty, etc.).
- **[`ARCHITECTURE.md`](ARCHITECTURE.md)** — the cross-cutting
  flow your check participates in.
