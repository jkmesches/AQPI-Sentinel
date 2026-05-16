"""Layer 1 — stream endpoint canary.

Hits both ``/api/get_stream_data/<comid>/<YYYYMMDD_HH>`` and
``/api/get_observed_stream_data/<comid>/<YYYYMMDD>`` with a B-status COMID
from the dashboard's stream_data.csv. We don't expect data (these often
return ``{"headers": ["COMID"], "values": [...]}`` echoing the input);
what we're proving is the route resolves and returns valid JSON.

The error-message-vs-validator mismatch is noted: the API responds
``Use YYYYMMDD_HHMM`` but the validator actually accepts ``YYYYMMDD_HH``.
We send the format that works.
"""
from __future__ import annotations
from datetime import datetime, timezone

from ..config import SETTINGS
from ..registry import register
from .base import Check, CheckResult, utcnow
from .helpers import worst_of

CANARY_COMID = "8921935"   # B-status, from /data/stream_data.csv


@register
class Layer1StreamCanary(Check):
    id = "layer1.stream.canary"
    stage = "L1"
    target = "stream"
    cadence_s = 300
    depends_on = ["layer0.origin.alive"]

    async def run(self, ctx) -> CheckResult:
        t0 = utcnow()
        now = datetime.now(timezone.utc)
        ts_hour = now.strftime("%Y%m%d_%H")
        ds = now.strftime("%Y%m%d")

        results = {}
        for label, url in [
            ("forecast", f"{SETTINGS.base}/api/get_stream_data/{CANARY_COMID}/{ts_hour}"),
            ("observed", f"{SETTINGS.base}/api/get_observed_stream_data/{CANARY_COMID}/{ds}"),
        ]:
            try:
                r = await ctx.http.get(url)
                ok = r.status_code == 200
                results[label] = {
                    "status": "pass" if ok else "fail",
                    "http": r.status_code,
                    "snippet": r.text[:120],
                }
            except Exception as e:
                results[label] = {"status": "error", "error": str(e)}

        overall = worst_of(*(d["status"] for d in results.values()))
        return CheckResult(
            check_id=self.id, target=self.target, stage=self.stage,
            status=overall,
            started_at=t0, finished_at=utcnow(),
            summary=f"forecast={results['forecast'].get('http', '-')} "
                    f"observed={results['observed'].get('http', '-')}",
            payload={"comid": CANARY_COMID, "results": results},
        )
