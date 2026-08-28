"""WAN reachability probe.

Latency and packet loss are measured from the Pi with the system ``ping``
rather than read off the controller. The controller's own figure is a gateway
health number that updates slowly and tells you nothing about the path between
this Pi and the internet, which is the thing on the wall you actually care
about.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass

from .config import PingConfig

log = logging.getLogger(__name__)

_TRANSMITTED = re.compile(r"(\d+)\s+packets transmitted,\s*(\d+)\s*(?:packets\s+)?received")
_RTT = re.compile(r"(?:rtt|round-trip)\s+min/avg/max/(?:mdev|stddev)\s*=\s*([\d.]+)/([\d.]+)/([\d.]+)/")


@dataclass
class PingResult:
    sent: int
    received: int
    min_ms: float | None = None
    avg_ms: float | None = None
    max_ms: float | None = None

    @property
    def loss_pct(self) -> float:
        if self.sent <= 0:
            return 100.0
        return 100.0 * (self.sent - self.received) / self.sent

    @property
    def reachable(self) -> bool:
        return self.received > 0


def parse_ping_output(text: str, *, expected: int) -> PingResult:
    """Parse iputils/BSD ``ping`` summary output."""
    sent, received = expected, 0
    if m := _TRANSMITTED.search(text):
        sent, received = int(m.group(1)), int(m.group(2))
    result = PingResult(sent=sent, received=received)
    if m := _RTT.search(text):
        result.min_ms = float(m.group(1))
        result.avg_ms = float(m.group(2))
        result.max_ms = float(m.group(3))
    return result


async def ping(cfg: PingConfig) -> PingResult:
    """Send ``cfg.count`` echo requests and summarise them."""
    args = [
        "ping",
        "-n",                       # no reverse DNS, keeps it fast
        "-c", str(cfg.count),
        "-i", str(cfg.interval),
        "-W", str(cfg.timeout),
        cfg.target,
    ]
    # Whole-run deadline, so a black-holed link cannot stall the poll loop.
    deadline = cfg.count * cfg.interval + cfg.timeout + 2.0
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except FileNotFoundError:
        log.warning("ping binary not found; latency and packet loss unavailable")
        return PingResult(sent=0, received=0)

    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=deadline)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return PingResult(sent=cfg.count, received=0)

    return parse_ping_output(stdout.decode(errors="replace"), expected=cfg.count)
