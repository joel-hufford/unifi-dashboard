"""HTTP surface: one JSON endpoint and the static kiosk page."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.types import Scope

from .config import Config
from .metrics import _WAN_KEY, find_gateway
from .poller import Poller
from .storage import History
from .unifi_client import UniFiClient

log = logging.getLogger(__name__)
STATIC_DIR = Path(__file__).parent / "static"

MAX_WINDOW_MINUTES = 1440


def asset_version() -> str:
    """A build id from the newest static file.

    The page and its assets must never be cached independently: a fresh
    index.html paired with a stale app.js means the script looks for elements
    that no longer exist, and the dashboard dies with a null dereference. The
    id is stamped into the asset URLs so a new build cannot be paired with an
    old one.
    """
    newest = 0.0
    for path in STATIC_DIR.glob("*"):
        if path.is_file():
            newest = max(newest, path.stat().st_mtime)
    return f"{int(newest)}"


class NoCacheStatic(StaticFiles):
    """Static assets on a kiosk are served over loopback, so revalidating
    every time costs nothing and removes a whole class of stale-asset bug."""

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache, must-revalidate"
        return response


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
        version = asset_version()
        html = page.read_text(encoding="utf-8")
        for asset in ("styles.css", "app.js"):
            html = html.replace(f"/static/{asset}", f"/static/{asset}?v={version}")
        return HTMLResponse(html, headers={"Cache-Control": "no-store"})

    app.mount("/static", NoCacheStatic(directory=STATIC_DIR), name="static")
    return app
