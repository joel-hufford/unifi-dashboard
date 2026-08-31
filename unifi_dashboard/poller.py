"""The background loop that keeps one current snapshot and one history table."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import asdict

from . import alarm as alarm_rules
from .config import Config
from .demo import DemoSource, seed_history
from .dnsprobe import DnsResult, resolve
from .metrics import (
    active_link,
    clients_from,
    devices_from,
    wan_from,
    wan_links_from,
    wlan_quality_from,
)
from .ping import PingResult, ping
from .publicip import PublicIp, PublicIpProbe
from .storage import History

log = logging.getLogger(__name__)

# A gateway speed test takes roughly half a minute; past this we stop claiming
# it is still running rather than spinning forever.
SPEEDTEST_TIMEOUT_S = 150.0
# While a test is pending the controller is polled faster, so the result is
# seen when it lands rather than up to a full interval later.
SPEEDTEST_POLL_S = 3.0


class Poller:
    def __init__(self, cfg: Config, store: History, client=None) -> None:
        self.cfg = cfg
        self.store = store
        self.client = client
        self.demo = DemoSource(fault=cfg.demo_fault) if cfg.demo else None
        self.snapshot: dict = {"ok": False, "error": "starting up", "generated_at": None}
        self.last_success: float | None = None
        self._task: asyncio.Task | None = None
        self._stopping = asyncio.Event()
        # Kept so a rate can be differenced from cumulative counters when the
        # firmware does not report the "-r" instantaneous fields.
        self._prev_counters: tuple[float, float, float] | None = None
        # Keeps the last raw controller payloads for /api/debug/raw.
        self.last_raw: dict[str, list[dict]] = {}
        self.public_ip = PublicIpProbe(cfg.public_ip)
        # A speed test is fire-and-forget: the gateway publishes the result in
        # the www health subsystem, so completion is detected by the timestamp
        # moving past the one recorded when it was asked for.
        self._speedtest = {"requested_at": None, "baseline_ts": None, "error": None}
        self._last_wan = None

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
            interval = self.cfg.poll_interval
            if self._speedtest_pending():
                interval = min(interval, SPEEDTEST_POLL_S)
            delay = max(1.0, interval - (time.monotonic() - started))
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass

    # -- one poll ---------------------------------------------------------

    async def tick(self) -> dict:
        now = time.time()
        # The two probes and the controller fetch talk to different places,
        # so they run together.
        probe_task = asyncio.create_task(self._probe())
        dns_task = asyncio.create_task(self._resolve())
        try:
            raw = await self._fetch()
        except BaseException:
            probe_task.cancel()
            dns_task.cancel()
            raise
        probe = await probe_task
        dns = await dns_task
        self.last_raw = raw

        wan = wan_from(raw["health"], raw["devices"])
        links = wan_links_from(raw["health"], raw["devices"])
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
                "dns_ok": dns.ok,
                "dns_ms": dns.elapsed_ms,
            }
        )

        self._last_wan = wan
        current = active_link(links)
        on_backup = bool(current and current is not links[0] and current.active)

        public = await self._public_ip(current.ip if current else wan.ip)

        state = alarm_rules.evaluate(
            self.cfg.alarm,
            controller_ok=True,
            wan_up=wan.online,
            internet_reachable=probe.reachable,
            dns_ok=dns.ok,
            loss_pct=probe.loss_pct,
            latency_ms=probe.avg_ms,
            on_backup=on_backup,
        )

        self.last_success = now
        self.snapshot = {
            "ok": True,
            "error": None,
            "generated_at": now,
            "alarm": asdict(state),
            "dns": asdict(dns),
            "speedtest": self._speedtest_state(wan),
            "public_ip": {
                **asdict(public),
                # A venue-supplied line is almost always NAT'd; knowing whether
                # you are behind one - and how many - is the point of showing
                # both addresses at once.
                "behind_nat": (
                    None if not public.address or not (current and current.ip)
                    else public.address != current.ip
                ),
            },
            "wan_links": [asdict(link) for link in links],
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

    async def _public_ip(self, wan_ip: str | None) -> PublicIp:
        if self.demo is not None:
            return self.demo.public_ip(wan_ip)
        return await self.public_ip.get(wan_ip)

    async def _resolve(self) -> DnsResult:
        if self.demo is not None:
            return self.demo.dns(self.cfg.dns.probe_host)
        return await resolve(self.cfg.dns)

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
        # A poll failure tells us nothing about the WAN itself, so the alarm
        # says exactly that rather than claiming an outage we cannot see.
        state = alarm_rules.evaluate(
            self.cfg.alarm,
            controller_ok=False,
            wan_up=None,
            internet_reachable=None,
            dns_ok=None,
            loss_pct=None,
            latency_ms=None,
        )
        self.snapshot = {
            **self.snapshot,
            "ok": False,
            "error": message,
            "alarm": asdict(state),
            "last_success": self.last_success,
        }

    def _speedtest_pending(self) -> bool:
        requested = self._speedtest["requested_at"]
        if not requested or self._last_wan is None:
            return bool(requested)
        state = self._speedtest_state(self._last_wan)
        return bool(state["running"])

    def _speedtest_state(self, wan) -> dict:
        requested = self._speedtest["requested_at"]
        baseline = self._speedtest["baseline_ts"]
        finished = bool(
            requested and wan.speedtest_ts and (baseline is None or wan.speedtest_ts > baseline)
        )
        elapsed = (time.time() - requested) if requested else None
        running = bool(requested and not finished and elapsed < SPEEDTEST_TIMEOUT_S)
        return {
            "running": running,
            "finished": finished,
            "timed_out": bool(requested and not finished and not running),
            "requested_at": requested,
            "elapsed_s": round(elapsed, 1) if elapsed is not None else None,
            "status": wan.speedtest_status,
            "down_mbps": wan.speedtest_down_mbps,
            "up_mbps": wan.speedtest_up_mbps,
            "ping_ms": wan.speedtest_ping_ms,
            "ts": wan.speedtest_ts,
            "error": self._speedtest["error"],
        }

    async def start_speedtest(self) -> dict:
        """Ask the gateway to run a speed test.

        The only write this dashboard makes. The result arrives through the
        normal poll, so this returns immediately with the pending state.
        """
        wan = self.snapshot.get("wan") or {}
        self._speedtest = {
            "requested_at": time.time(),
            "baseline_ts": wan.get("speedtest_ts"),
            "error": None,
        }
        try:
            if self.demo is not None:
                self.demo.start_speedtest()
            elif self.client is not None:
                await self.client.start_speedtest()
            else:
                raise RuntimeError("no UniFi client configured")
        except Exception as exc:
            log.warning("speed test could not be started: %s", exc)
            self._speedtest = {"requested_at": None, "baseline_ts": None, "error": str(exc)}
            raise
        return {**(self.snapshot.get("speedtest") or {}), "running": True, "error": None,
                "requested_at": self._speedtest["requested_at"], "finished": False}

    async def recheck_public_ip(self) -> dict:
        """Force a public-address lookup and fold it into the current snapshot.

        Only the public lookup is throttled - the interface address comes from
        the controller on every poll - so this is the only thing worth forcing.
        """
        self.public_ip.invalidate()
        wan_ip = (self.snapshot.get("wan") or {}).get("ip")
        result = await self._public_ip(wan_ip)
        payload = {
            **asdict(result),
            "behind_nat": (
                None if not result.address or not wan_ip else result.address != wan_ip
            ),
        }
        if self.snapshot.get("ok"):
            self.snapshot["public_ip"] = payload
        return payload

    # -- API payload ------------------------------------------------------

    def dashboard(self, minutes: int) -> dict:
        rows = self.store.window(minutes)
        summary = self.store.summary(minutes)
        now = time.time()
        snapshot = dict(self.snapshot)
        # Recomputed per request rather than per poll: "running" and the
        # elapsed count are derived from the clock, and a caller watching a
        # speed test should not see them lag a whole poll interval behind.
        if self._last_wan is not None:
            snapshot["speedtest"] = self._speedtest_state(self._last_wan)
        snapshot["stale_s"] = round(now - self.last_success, 1) if self.last_success else None
        snapshot["poll_interval"] = self.cfg.poll_interval
        snapshot["window"] = summary
        snapshot["series"] = {
            "ts": [row["ts"] for row in rows],
            "dns_ok": [row["dns_ok"] for row in rows],
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
