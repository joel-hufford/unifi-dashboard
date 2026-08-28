"""The background loop that keeps one current snapshot and one history table."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import asdict

from .config import Config
from .demo import DemoSource, seed_history
from .metrics import clients_from, devices_from, wan_from, wlan_quality_from
from .ping import PingResult, ping
from .storage import History

log = logging.getLogger(__name__)


class Poller:
    def __init__(self, cfg: Config, store: History, client=None) -> None:
        self.cfg = cfg
        self.store = store
        self.client = client
        self.demo = DemoSource() if cfg.demo else None
        self.snapshot: dict = {"ok": False, "error": "starting up", "generated_at": None}
        self.last_success: float | None = None
        self._task: asyncio.Task | None = None
        self._stopping = asyncio.Event()
        # Kept so a rate can be differenced from cumulative counters when the
        # firmware does not report the "-r" instantaneous fields.
        self._prev_counters: tuple[float, float, float] | None = None

    # -- lifecycle --------------------------------------------------------

    async def start(self) -> None:
        if self.demo is not None:
            seed_history(
                self.store, self.demo,
                minutes=self.cfg.history.retention_minutes,
                interval=self.cfg.poll_interval,
            )
        self._task = asyncio.create_task(self._run(), name="unifi-poller")

    async def stop(self) -> None:
        self._stopping.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self.client is not None:
            await self.client.aclose()

    async def _run(self) -> None:
        while not self._stopping.is_set():
            started = time.monotonic()
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # a poll failure must never kill the loop
                log.warning("poll failed: %s", exc)
                self._record_failure(str(exc))
            delay = max(1.0, self.cfg.poll_interval - (time.monotonic() - started))
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass

    # -- one poll ---------------------------------------------------------

    async def tick(self) -> dict:
        now = time.time()
        probe_task = asyncio.create_task(self._probe())
        try:
            raw = await self._fetch()
        except BaseException:
            probe_task.cancel()
            raise
        probe = await probe_task

        wan = wan_from(raw["health"], raw["devices"])
        clients = clients_from(raw["clients"])
        devices = devices_from(raw["devices"])
        wlan = wlan_quality_from(raw["clients"], weak_signal_dbm=self.cfg.wlan.weak_signal_dbm)

        rx_bps, tx_bps = self._rates(wan, now)

        self.store.record(
            {
                "ts": now,
                "rx_bps": rx_bps,
                "tx_bps": tx_bps,
                "latency_ms": probe.avg_ms,
                "ping_sent": probe.sent,
                "ping_recv": probe.received,
                "clients": clients.total,
                "wlan_score": wlan.score,
                "wan_up": wan.online,
            }
        )

        self.last_success = now
        self.snapshot = {
            "ok": True,
            "error": None,
            "generated_at": now,
            "wan": {
                **asdict(wan),
                "rx_bps": rx_bps,
                "tx_bps": tx_bps,
                "latency_ms": probe.avg_ms,
                "latency_min_ms": probe.min_ms,
                "latency_max_ms": probe.max_ms,
                "loss_pct": round(probe.loss_pct, 1),
                "reachable": probe.reachable,
                "ping_target": self.cfg.ping.target,
            },
            "clients": asdict(clients),
            "devices": asdict(devices),
            "wlan": asdict(wlan),
        }
        return self.snapshot

    async def _fetch(self) -> dict[str, list[dict]]:
        if self.demo is not None:
            return self.demo.fetch_all()
        if self.client is None:
            raise RuntimeError("no UniFi client configured")
        return await self.client.fetch_all()

    async def _probe(self) -> PingResult:
        if self.demo is not None:
            return self.demo.ping(self.cfg.ping.count)
        return await ping(self.cfg.ping)

    def _rates(self, wan, now: float) -> tuple[float | None, float | None]:
        """Prefer the controller's instantaneous rate; difference the counters
        when it is not reported."""
        if wan.rx_bps is not None or wan.tx_bps is not None:
            self._prev_counters = None
            return wan.rx_bps, wan.tx_bps

        if wan.rx_bytes is None or wan.tx_bytes is None:
            return None, None

        previous = self._prev_counters
        self._prev_counters = (now, wan.rx_bytes, wan.tx_bytes)
        if previous is None:
            return None, None

        prev_ts, prev_rx, prev_tx = previous
        elapsed = now - prev_ts
        if elapsed <= 0:
            return None, None
        # A counter that went backwards means the gateway rebooted or the
        # counter wrapped; skip the interval rather than draw a huge spike.
        if wan.rx_bytes < prev_rx or wan.tx_bytes < prev_tx:
            return None, None
        return (wan.rx_bytes - prev_rx) / elapsed, (wan.tx_bytes - prev_tx) / elapsed

    def _record_failure(self, message: str) -> None:
        self.snapshot = {
            **self.snapshot,
            "ok": False,
            "error": message,
            "last_success": self.last_success,
        }

    # -- API payload ------------------------------------------------------

    def dashboard(self, minutes: int) -> dict:
        rows = self.store.window(minutes)
        summary = self.store.summary(minutes)
        now = time.time()
        snapshot = dict(self.snapshot)
        snapshot["stale_s"] = round(now - self.last_success, 1) if self.last_success else None
        snapshot["poll_interval"] = self.cfg.poll_interval
        snapshot["window"] = summary
        snapshot["series"] = {
            "ts": [row["ts"] for row in rows],
            "rx_bps": [row["rx_bps"] for row in rows],
            "tx_bps": [row["tx_bps"] for row in rows],
            "latency_ms": [row["latency_ms"] for row in rows],
            "loss_pct": [
                None if not row["ping_sent"]
                else round(100.0 * (row["ping_sent"] - row["ping_recv"]) / row["ping_sent"], 1)
                for row in rows
            ],
        }
        snapshot["server_time"] = now
        return snapshot
