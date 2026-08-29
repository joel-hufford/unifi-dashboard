"""HTTP surface: one JSON endpoint and the static kiosk page."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .config import Config
from .metrics import _WAN_KEY, find_gateway
from .poller import Poller
from .storage import History
from .unifi_client import UniFiClient

log = logging.getLogger(__name__)
STATIC_DIR = Path(__file__).parent / "static"

MAX_WINDOW_MINUTES = 1440


def create_app(cfg: Config) -> FastAPI:
    store = History(cfg.history.resolved_path() if not cfg.demo else ":memory:", cfg.history.retention_minutes)
    client = None if cfg.demo else UniFiClient(cfg.unifi)
    poller = Poller(cfg, store, client)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await poller.start()
        try:
            yield
        finally:
            await poller.stop()
            store.close()

    app = FastAPI(title="UniFi Dashboard", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.cfg = cfg
    app.state.poller = poller

    @app.get("/api/dashboard")
    async def dashboard(minutes: int = Query(default=cfg.history.window_minutes, ge=1, le=MAX_WINDOW_MINUTES)):
        capped = min(minutes, cfg.history.retention_minutes)
        payload = poller.dashboard(capped)
        payload["config"] = {
            "window_minutes": cfg.history.window_minutes,
            "retention_minutes": cfg.history.retention_minutes,
            "ping_target": cfg.ping.target,
            "dns_host": cfg.dns.probe_host,
            "theme": cfg.ui.theme,
            "throughput_scale": cfg.charts.throughput_scale,
            "log_decades": cfg.charts.log_decades,
            "demo": cfg.demo,
        }
        # 200 even when the controller is down: the page wants the last good
        # snapshot plus the error, not an exception.
        return JSONResponse(payload)

    @app.get("/api/debug/wan")
    async def debug_wan():
        """The gateway's WAN interfaces and the WAN health subsystems, exactly
        as the controller returned them. This is the payload to send when WAN
        detection gets something wrong: it holds no client names or MACs, only
        the uplink detail."""
        raw = poller.last_raw
        gateway = find_gateway(raw.get("devices") or [])
        interfaces = {}
        if isinstance(gateway, dict):
            interfaces = {key: value for key, value in gateway.items() if _WAN_KEY.match(key)}
        return {
            "gateway_type": (gateway or {}).get("type"),
            "gateway_model": (gateway or {}).get("model"),
            "wan_interfaces": interfaces,
            "health": [
                entry for entry in (raw.get("health") or [])
                if entry.get("subsystem") in ("wan", "www")
            ],
            "parsed": poller.snapshot.get("wan_links", []),
        }

    @app.get("/api/healthz")
    async def healthz():
        return {"ok": bool(poller.snapshot.get("ok")), "last_success": poller.last_success}

    @app.get("/")
    async def index():
        page = STATIC_DIR / "index.html"
        if not page.is_file():
            raise HTTPException(status_code=500, detail="static assets are missing")
        return FileResponse(page, headers={"Cache-Control": "no-store"})

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app
