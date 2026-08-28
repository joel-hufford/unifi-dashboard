"""HTTP surface: one JSON endpoint and the static kiosk page."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .config import Config
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
            "demo": cfg.demo,
        }
        # 200 even when the controller is down: the page wants the last good
        # snapshot plus the error, not an exception.
        return JSONResponse(payload)

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
