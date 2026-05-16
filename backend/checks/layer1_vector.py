"""Layer 1 — static vector / data files (geojson, csv).

These are served by nginx, not the Django app, so they depend on the
``layer0.website.public`` (edge) check rather than ``origin.alive``. They
change rarely — alarm only on HTTP failure or empty body.
"""
from __future__ import annotations
from datetime import datetime, timezone

from ..config import SETTINGS
from ..registry import register
from .base import Check, CheckResult, utcnow


VECTOR_STATICS = {
    "flowlines":  "/geojson/flowlines.geojson",
    "watersheds": "/geojson/watersheds.geojson",
    "stream_csv": "/data/stream_data.csv",
}


class Layer1VectorCheck(Check):
    stage = "L1"
    depends_on = ["layer0.website.public"]
    cadence_s = 3600   # once an hour is plenty

    def __init__(self, name: str, path: str):
        self.name = name
        self.path = path
        self.id = f"layer1.vector.{name}"
        self.target = name

    async def run(self, ctx) -> CheckResult:
        t0 = utcnow()
        r = await ctx.http.head(f"{SETTINGS.base}{self.path}")
        size = int(r.headers.get("Content-Length", 0))
        last_mod = r.headers.get("Last-Modified", "")

        age_days: float | None = None
        if last_mod:
            try:
                dt = datetime.strptime(last_mod, "%a, %d %b %Y %H:%M:%S GMT").replace(
                    tzinfo=timezone.utc
                )
                age_days = (utcnow() - dt).total_seconds() / 86400
            except ValueError:
                pass

        ok = r.status_code == 200 and size > 0
        return CheckResult(
            check_id=self.id, target=self.target, stage=self.stage,
            status="pass" if ok else "fail",
            started_at=t0, finished_at=utcnow(),
            summary=f"HTTP {r.status_code} {size}B  age={age_days:.1f}d"
                    if age_days is not None
                    else f"HTTP {r.status_code} {size}B",
            payload={
                "http": r.status_code,
                "bytes": size,
                "last_modified": last_mod,
                "age_days": age_days,
            },
            metrics={"bytes": float(size),
                     **({"age_days": age_days} if age_days is not None else {})},
        )


for _name, _path in VECTOR_STATICS.items():
    register(Layer1VectorCheck(name=_name, path=_path))
