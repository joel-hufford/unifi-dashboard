"""What address the outside world sees us as.

The WAN interface address and the public address answer different questions.
On a venue-supplied line - a hotel handoff, a conference drop - the interface
address tells you what their DHCP gave you, and the public address tells you
what you are actually presenting to the internet. The gap between them is the
NAT you are sitting behind, which is exactly the thing nobody tells you about.

The lookup is a third-party HTTP request, so it is throttled hard: once per
interval, plus immediately whenever the WAN address changes (which is the
moment the answer can differ).
"""

from __future__ import annotations

import ipaddress
import logging
import time
from dataclasses import dataclass

import httpx

from .config import PublicIpConfig

log = logging.getLogger(__name__)


@dataclass
class PublicIp:
    address: str | None = None
    checked_at: float | None = None
    error: str | None = None
    enabled: bool = True


class PublicIpProbe:
    def __init__(self, cfg: PublicIpConfig) -> None:
        self.cfg = cfg
        self._result = PublicIp(enabled=cfg.enabled)
        self._last_wan_ip: str | None = None
        self._last_attempt: float = 0.0

    def _due(self, wan_ip: str | None, now: float) -> bool:
        if not self.cfg.enabled:
            return False
        if wan_ip != self._last_wan_ip:
            return True                        # the answer may have changed
        if self._result.address is None and self._result.error is None:
            return True                        # never asked
        interval = self.cfg.interval_minutes * 60
        # Back off to a shorter retry after a failure rather than waiting the
        # full interval to find out the network came back.
        if self._result.error:
            interval = min(interval, 60.0)
        return (now - self._last_attempt) >= interval

    async def get(self, wan_ip: str | None, *, client: httpx.AsyncClient | None = None) -> PublicIp:
        now = time.time()
        if not self.cfg.enabled:
            return PublicIp(enabled=False)
        if not self._due(wan_ip, now):
            return self._result

        self._last_attempt = now
        self._last_wan_ip = wan_ip
        owned = client is None
        http = client or httpx.AsyncClient(timeout=self.cfg.timeout)
        try:
            response = await http.get(self.cfg.url, headers={"Accept": "text/plain"})
            response.raise_for_status()
            address = _first_address(response.text)
            if address is None:
                raise ValueError("no IP address in the response")
            self._result = PublicIp(address=address, checked_at=now)
        except Exception as exc:                # any failure is just "unknown"
            log.info("public IP lookup failed: %s", exc)
            self._result = PublicIp(address=self._result.address, checked_at=self._result.checked_at,
                                    error=str(exc))
        finally:
            if owned:
                await http.aclose()
        return self._result


def _first_address(text: str) -> str | None:
    """Accept a bare address or a trivial JSON body, and validate it."""
    candidate = (text or "").strip().strip('"')
    if candidate.startswith("{"):
        import json
        try:
            payload = json.loads(candidate)
        except ValueError:
            return None
        for key in ("ip", "address", "origin", "query"):
            if isinstance(payload.get(key), str):
                candidate = payload[key].strip()
                break
        else:
            return None
    try:
        return str(ipaddress.ip_address(candidate))
    except ValueError:
        return None
