"""DNS reachability probe.

Being able to ping 8.8.8.8 and being able to resolve a name are different
failures with different causes - a working ICMP path with dead DNS is one of
the most common "the internet is broken" states on a small network, and the
one users notice first. So it gets its own probe and its own indicator.

Resolution goes through the system resolver on purpose: that is the path an
actual client takes, gateway forwarding and all.
"""

from __future__ import annotations

import asyncio
import socket
import time
from dataclasses import dataclass

from .config import DnsConfig


@dataclass
class DnsResult:
    host: str
    ok: bool
    elapsed_ms: float | None = None
    error: str | None = None
    address: str | None = None


async def resolve(cfg: DnsConfig) -> DnsResult:
    loop = asyncio.get_running_loop()
    started = time.monotonic()
    try:
        infos = await asyncio.wait_for(
            loop.getaddrinfo(cfg.probe_host, None, proto=socket.IPPROTO_TCP),
            timeout=cfg.timeout,
        )
    except asyncio.TimeoutError:
        return DnsResult(host=cfg.probe_host, ok=False, error=f"timed out after {cfg.timeout:g}s")
    except socket.gaierror as exc:
        return DnsResult(host=cfg.probe_host, ok=False, error=str(exc))
    except OSError as exc:
        return DnsResult(host=cfg.probe_host, ok=False, error=str(exc))

    elapsed = (time.monotonic() - started) * 1000.0
    address = infos[0][4][0] if infos else None
    return DnsResult(host=cfg.probe_host, ok=True, elapsed_ms=round(elapsed, 1), address=address)
