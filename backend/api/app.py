"""FastAPI app factory. Lifespan owns the store + scheduler lifecycle."""
from __future__ import annotations
import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from ..alarms.engine import AlarmEngine
from ..alarms.models import load_config as load_alerts_config
from ..checks.network import NetworkMonitor
from ..checks.transports import CheckContext
from ..checks.transports.browser import BrowserClient
from ..checks.transports.http import HttpClient
from ..config import SETTINGS
from ..db.store import Store
from ..registry import CHECKS, all_stages
from ..scheduler import Scheduler
from .routes import admin as admin_routes
from .routes import alarms as alarms_routes
from .routes import auth as auth_routes
from .routes import checks as checks_routes
from .routes import debug as debug_routes
from .routes import history as history_routes
from .routes import radars as radars_routes
from .routes import silences as silences_routes
from .routes import status as status_routes
from .routes import upstream as upstream_routes
from .ws import ConnectionManager, router as ws_router

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("starting sentinel — %d checks registered, stages=%s",
             len(CHECKS), sorted(all_stages()))
    store = Store(SETTINGS.db_url)
    await store.connect()

    # Bootstrap the first admin from env vars on a fresh database. No-op
    # if users already exist.
    if SETTINGS.admin_email and SETTINGS.admin_password:
        from .. import auth as _A
        created = await _A.bootstrap_admin(
            store.pool,
            email=SETTINGS.admin_email,
            password=SETTINGS.admin_password,
            display_name=SETTINGS.admin_display_name or None,
        )
        if created:
            log.info("bootstrapped first admin: %s", SETTINGS.admin_email)

    ws = ConnectionManager()
    app.state.ws = ws

    alerts_cfg = load_alerts_config()
    engine = AlarmEngine(store, alerts_cfg)
    # alarm events → all WS clients
    async def _push_alarm(event_type, payload):
        await ws.broadcast({"type": event_type, "alarm": payload})
    engine.add_listener(_push_alarm)
    await engine.start()

    net = NetworkMonitor()
    await net.start()
    ctx = CheckContext(
        http=HttpClient(), browser=BrowserClient(),
        network=net, pool=store.pool,
    )
    sched = Scheduler(store, ctx, engine=engine)
    # scheduler also emits run events for the live stream
    # WS run events: broadcast ONLY when status changes (or first event for
    # a check). On a healthy system 95%+ of consecutive runs return the
    # same status, so without this gate we were pushing ~30 events/min for
    # no UI-visible reason — the source of the idle-tab freeze. The 5s
    # polling refresh keeps non-transition state fresh on its own.
    #
    # Metric samples (sparkline data) ride on these transition events,
    # which means sparklines update only on transitions + every 5s polling
    # tick. That's plenty for an at-a-glance dashboard; finer-grained
    # streaming can be added per-page if it's ever needed.
    _last_broadcast_status: dict[str, str] = {}

    def _maybe_broadcast(r):
        key = r.check_id
        prev = _last_broadcast_status.get(key)
        if prev == r.status:
            return
        _last_broadcast_status[key] = r.status
        asyncio.create_task(ws.broadcast({
            "type": "run",
            "run": {
                "check_id":    r.check_id,
                "target":      r.target,
                "stage":       r.stage,
                "status":      r.status,
                "prev_status": prev,                 # None on first event
                "started_at":  r.started_at.isoformat(),
                "finished_at": r.finished_at.isoformat(),
                "summary":     r.summary,
                "metrics":     r.metrics,
            },
        }))

    sched.on_result = _maybe_broadcast
    await sched.start()

    app.state.store = store
    app.state.scheduler = sched
    app.state.context = ctx
    app.state.engine = engine
    try:
        yield
    finally:
        await sched.stop()
        await engine.stop()
        await ctx.aclose()
        await store.close()
        log.info("sentinel stopped")


def create_app() -> FastAPI:
    app = FastAPI(title="Sentinel", version="0.1.0", lifespan=lifespan)
    app.include_router(status_routes.router)
    app.include_router(checks_routes.router)
    app.include_router(alarms_routes.router)
    app.include_router(silences_routes.router)
    app.include_router(history_routes.router)
    app.include_router(radars_routes.router)
    app.include_router(upstream_routes.router)
    app.include_router(debug_routes.router)
    app.include_router(auth_routes.router)
    app.include_router(admin_routes.router)
    app.include_router(ws_router)

    @app.get("/")
    async def root():
        return {"name": "Sentinel", "version": "0.1.0",
                "checks": len(CHECKS), "stages": sorted(all_stages())}

    return app
