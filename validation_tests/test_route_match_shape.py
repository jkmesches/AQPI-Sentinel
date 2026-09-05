"""Route matchers must see the same alarm shape at open time and dispatch time.

Run directly (no DB, no network):

    python validation_tests/test_route_match_shape.py

``status_at_open`` is stored inside the alarms.payload JSONB, but ``_matches``
does a flat ``alarm.get(k)``. The two call sites disagreed about shape:

  engine.evaluate()  → builds a flat dict {check_id, target, stage,
                       status_at_open} to resolve the hold-down.   MATCHES
  engine._process()  → passes the alarms ROW straight through.     NEVER MATCHED

So a route keyed on ``status_at_open`` appeared to work — it gated the
hold-down correctly — and then silently matched nothing when it came time to
actually notify. ``_process`` takes ``route is None`` as "no route configured"
and returns, so there is no error, no warning, and no log line. The only
symptom is an empty notification_log.

Production ran exactly one route, ``{status_at_open: error}``, and sent zero
notifications while 537 matching alarms opened in seven days. The last row in
notification_log predated the config change by months.

``compute_severity`` reads the same field, so duration-promotion to critical
was dead for the same reason and is asserted here too.

The regression to guard against is the mirror image: flattening must never let
a payload key shadow a real column, or a route keyed on ``stage`` could be
steered by attacker-ish or merely stale payload content.
"""
from __future__ import annotations
import asyncio
import os
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("SENTINEL_DB_URL", "postgresql://unused/unused")

from backend.alarms.models import AlertsConfig                  # noqa: E402
from backend.alarms.router import Router, flatten_alarm         # noqa: E402
from backend.checks.base import utcnow                          # noqa: E402

failures: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if cond else 'FAIL'}  {label}" + (f"  — {detail}" if detail else ""))
    if not cond:
        failures.append(label)


async def main() -> int:
    now = utcnow()
    # --- routes keyed on status_at_open must match at DISPATCH time ---------
    # status_at_open is written into payload, but evaluate() passes it flat.
    # A route matching it therefore worked at open time and silently matched
    # nothing at dispatch time — no error, no log, just a route that never
    # fired. Production ran exactly one such route.
    cfg = AlertsConfig.model_validate({
        "routes": [{"match": {"status_at_open": "error"}, "policy": "hard-page"}],
        "receivers": [{"name": "console", "console": True}],
        "escalation_policies": [
            {"name": "hard-page", "steps": [{"delay": "10m", "receivers": ["console"]}]}],
    })
    router = Router(cfg)
    row = {"id": 1, "check_id": "layer2.radar.XEBY", "target": "XEBY", "stage": "L2",
           "severity": "warn", "opened_at": now, "closed_at": None,
           "suppressed_by": None, "message": "x",
           "payload": {"status_at_open": "error", "result_payload": {}}}
    check("a status_at_open route matches an alarms ROW (dispatch path)",
          router.match(row, {}) is not None)
    check("...and still matches the flat shape evaluate() builds (open path)",
          router.match({"check_id": "c", "target": "t", "stage": "L2",
                        "status_at_open": "error"}, {}) is not None)
    check("a non-matching status_at_open still does not match",
          router.match({**row, "payload": {"status_at_open": "fail"}}, {}) is None)

    # A real column must never be shadowed by a payload key of the same name.
    shadowed = flatten_alarm({**row, "payload": {"stage": "L9", "status_at_open": "error"}})
    check("a column beats a payload key of the same name",
          shadowed["stage"] == "L2", shadowed["stage"])
    check("flatten_alarm tolerates a payload that is not a dict",
          flatten_alarm({"id": 1, "payload": None}) == {"id": 1, "payload": None})
    check("flatten_alarm decodes a string payload (un-codec'd JSONB)",
          flatten_alarm({"id": 1, "payload": '{"status_at_open":"error"}'}
                        ).get("status_at_open") == "error")

    # Severity promotion reads the same field; it was dead for the same reason.
    sev = router.compute_severity(
        {**row, "opened_at": now - timedelta(minutes=45)}, None, now)
    check("duration promotion sees status_at_open on a row (>30m → critical)",
          sev == "critical", sev)

    print(f"\n{len(failures)} FAILED: {', '.join(failures)}" if failures
          else "\nall route-shape assertions passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
