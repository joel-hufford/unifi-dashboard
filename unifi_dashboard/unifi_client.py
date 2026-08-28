"""Async client for the UniFi Network controller's local API.

Two auth modes are supported:

* ``api_key``     - an API key created under Network -> Settings -> Control
                    Plane -> Integrations, sent as ``X-API-KEY``. Needs UniFi
                    Network 9.x on UniFi OS 4.1+.
* ``local_admin`` - a local-only controller admin, logged in for a session
                    cookie. Works on every controller generation, including
                    self-hosted ones.

Both then read the same ``/api/s/<site>/...`` endpoints, which is where the
data this dashboard needs actually lives (WAN IP, per-interface throughput
counters, per-client signal). On a UniFi OS console those endpoints sit behind
a ``/proxy/network`` prefix; on a self-hosted controller they are at the root.
The prefix is probed once and cached.
"""

from __future__ import annotations

import logging

import httpx

from .config import UniFiConfig

log = logging.getLogger(__name__)

UNIFI_OS_PREFIX = "/proxy/network"


class UniFiError(RuntimeError):
    """Any failure talking to the controller."""


class UniFiAuthError(UniFiError):
    """Credentials or API key were rejected."""


class UniFiClient:
    def __init__(self, cfg: UniFiConfig) -> None:
        self.cfg = cfg
        headers = {"Accept": "application/json"}
        if cfg.api_key:
            headers["X-API-KEY"] = cfg.api_key
        self._http = httpx.AsyncClient(
            base_url=cfg.host.rstrip("/"),
            verify=cfg.verify_ssl,
            timeout=cfg.timeout,
            headers=headers,
            follow_redirects=False,
        )
        self._prefix: str | None = None
        self._authenticated = cfg.auth_mode == "api_key"

    async def aclose(self) -> None:
        await self._http.aclose()

    # -- plumbing ---------------------------------------------------------

    async def _detect_prefix(self) -> str:
        """Work out whether we are talking to UniFi OS or a bare controller."""
        if self._prefix is not None:
            return self._prefix
        for prefix in (UNIFI_OS_PREFIX, ""):
            try:
                resp = await self._http.get(f"{prefix}/status")
            except httpx.HTTPError as exc:  # network-level problem, not routing
                raise UniFiError(f"cannot reach {self.cfg.host}: {exc}") from exc
            if resp.status_code == 200 and "application/json" in resp.headers.get("content-type", ""):
                self._prefix = prefix
                log.info("controller looks like %s", "UniFi OS" if prefix else "a standalone controller")
                return prefix
        # Neither probe answered cleanly. Assume UniFi OS, which is the common
        # case, and let the first real request produce a useful error.
        self._prefix = UNIFI_OS_PREFIX
        return self._prefix

    async def _login(self) -> None:
        if self.cfg.auth_mode == "api_key":
            self._authenticated = True
            return
        if self.cfg.auth_mode == "none":
            raise UniFiAuthError(
                "no credentials configured: set unifi.api_key, or unifi.username and unifi.password"
            )
        prefix = await self._detect_prefix()
        path = "/api/auth/login" if prefix else "/api/login"
        payload = {"username": self.cfg.username, "password": self.cfg.password, "rememberMe": True}
        try:
            resp = await self._http.post(path, json=payload)
        except httpx.HTTPError as exc:
            raise UniFiError(f"login request failed: {exc}") from exc
        if resp.status_code in (400, 401, 403):
            raise UniFiAuthError(
                "controller rejected the login. Use a local-only admin account with 2FA disabled."
            )
        if resp.status_code >= 400:
            raise UniFiError(f"login failed with HTTP {resp.status_code}")
        # UniFi OS wants the CSRF token echoed back on writes. We only read,
        # but keeping it costs nothing and makes future writes work.
        token = resp.headers.get("x-csrf-token")
        if token:
            self._http.headers["X-CSRF-Token"] = token
        self._authenticated = True

    async def _request(self, endpoint: str, *, retry: bool = True) -> list[dict]:
        if not self._authenticated:
            await self._login()
        prefix = await self._detect_prefix()
        url = f"{prefix}/api/s/{self.cfg.site}{endpoint}"
        try:
            resp = await self._http.get(url)
        except httpx.HTTPError as exc:
            raise UniFiError(f"request to {endpoint} failed: {exc}") from exc

        if resp.status_code in (401, 403) and retry:
            # Session cookies expire; API keys do not, so a 401 there is fatal.
            if self.cfg.auth_mode == "local_admin":
                self._authenticated = False
                return await self._request(endpoint, retry=False)
            raise UniFiAuthError(
                "controller rejected the API key. Confirm it was created in Network -> Settings -> "
                "Control Plane -> Integrations, or fall back to a local admin account."
            )
        if resp.status_code >= 400:
            raise UniFiError(f"{endpoint} returned HTTP {resp.status_code}")

        try:
            body = resp.json()
        except ValueError as exc:
            raise UniFiError(f"{endpoint} did not return JSON") from exc
        data = body.get("data") if isinstance(body, dict) else None
        if data is None:
            raise UniFiError(f"{endpoint} returned an unexpected payload")
        return data

    # -- endpoints --------------------------------------------------------

    async def health(self) -> list[dict]:
        """Per-subsystem health: wan, www, lan, wlan, vpn."""
        return await self._request("/stat/health")

    async def devices(self) -> list[dict]:
        """Every adopted UniFi device, including the gateway's WAN counters."""
        return await self._request("/stat/device")

    async def clients(self) -> list[dict]:
        """Currently connected clients, wired and wireless."""
        return await self._request("/stat/sta")

    async def fetch_all(self) -> dict[str, list[dict]]:
        """One poll's worth of data. Sequential on purpose: the console is a
        small appliance and three concurrent requests is how you make it
        drop one."""
        return {
            "health": await self.health(),
            "devices": await self.devices(),
            "clients": await self.clients(),
        }
