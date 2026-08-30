"""Synthetic data source, for building the UI without a controller.

``unifi-dashboard --demo`` swaps the real client and the real ping probe for
these, and backfills an hour of history so the graphs have something in them
on first paint.
"""

from __future__ import annotations

import math
import random
import time

from .dnsprobe import DnsResult
from .publicip import PublicIp
from .ping import PingResult

_MACS = ["ac:1f:6b", "78:8a:20", "b4:fb:e4", "f0:9f:c2", "24:5a:4c"]
_NAMES = [
    "Living room TV", "Joel's MacBook", "Kitchen iPad", "Office desktop", "Shop printer",
    "Garage camera", "Thermostat", "Pixel 9", "Basement AP client", "Doorbell",
    "Sonos kitchen", "Workshop laptop", "Guest phone", "NAS", "Roomba",
]
_APS = ["Office AP", "Living room AP", "Shop AP"]
_VENDORS = ["Apple", "Google", "Espressif", "Sonos", "Ubiquiti", "Intel", "Samsung"]
_BANDS = ["ng", "na", "na", "6e"]


class DemoSource:
    """Random-walk generator that looks like a busy small network."""

    #: Fault injections, so the alarm states can be seen without breaking a
    #: real network to do it. Selected with --demo-fault.
    FAULTS = ("none", "quiet", "wan-down", "dns", "loss", "latency", "failover")

    def __init__(self, seed: int | None = None, fault: str = "none") -> None:
        self.rng = random.Random(seed)
        self.start = time.time()
        self.rx_level = 3e6
        self.tx_level = 4e5
        self.client_count = 42
        self.fault = fault if fault in self.FAULTS else "none"

    # -- shaping ----------------------------------------------------------

    def _throughput(self, t: float) -> tuple[float, float]:
        """Bytes/second down and up, with occasional streaming-sized bursts."""
        if self.fault == "quiet":
            return self._quiet_throughput()
        wave = 0.5 + 0.5 * math.sin(t / 420.0)
        self.rx_level = max(2.0e5, self.rx_level * 0.82 + wave * 6.0e6 * 0.18)
        self.tx_level = max(3.0e4, self.tx_level * 0.85 + wave * 6.0e5 * 0.15)
        if self.rng.random() < 0.06:
            self.rx_level += self.rng.uniform(1.0e7, 4.2e7)
        if self.rng.random() < 0.04:
            self.tx_level += self.rng.uniform(6.0e5, 3.0e6)
        return (
            self.rx_level * self.rng.uniform(0.85, 1.15),
            self.tx_level * self.rng.uniform(0.85, 1.15),
        )

    def _quiet_throughput(self) -> tuple[float, float]:
        """A mostly idle link: tens of kbps of background chatter, with a rare
        burst. This is what a small site actually looks like, and it is the
        case a peak-scaled linear axis renders as a flat line."""
        rx = self.rng.uniform(2_000, 40_000)
        tx = self.rng.uniform(1_500, 25_000)
        if self.rng.random() < 0.02:
            rx += self.rng.uniform(2.0e6, 1.0e7)
            tx += self.rng.uniform(1.0e5, 4.0e5)
        return rx, tx

    def public_ip(self, wan_ip: str | None = None) -> PublicIp:
        if self.fault == "wan-down":
            return PublicIp(error="lookup failed")
        # Deliberately different from the WAN address, which is the case the
        # two-address display exists for.
        return PublicIp(address="198.51.100.7", checked_at=time.time())

    def dns(self, host: str = "cloudflare.com") -> DnsResult:
        if self.fault in ("dns", "wan-down"):
            return DnsResult(host=host, ok=False, error="Temporary failure in name resolution")
        return DnsResult(
            host=host, ok=True, elapsed_ms=round(self.rng.uniform(18, 46), 1), address="104.16.132.229"
        )

    def ping(self, count: int = 3) -> PingResult:
        if self.fault == "wan-down":
            return PingResult(sent=count, received=0)
        base = 13.5 + 3.0 * math.sin(time.time() / 190.0)
        spike = self.rng.random() < 0.05
        latency = base + self.rng.uniform(-1.5, 2.5) + (self.rng.uniform(20, 90) if spike else 0.0)
        received = count
        if self.rng.random() < 0.03:
            received = count - 1
        if self.rng.random() < 0.005:
            received = 0
        if self.fault == "loss" and self.rng.random() < 0.45:
            received = max(0, count - 2)
        if self.fault == "latency":
            latency += 380
        result = PingResult(sent=count, received=received)
        if received:
            result.avg_ms = round(latency, 1)
            result.min_ms = round(latency * 0.92, 1)
            result.max_ms = round(latency * 1.18, 1)
        return result

    # -- controller payloads ---------------------------------------------

    def fetch_all(self) -> dict[str, list[dict]]:
        rx, tx = self._throughput(time.time() - self.start)
        self.client_count = max(28, min(56, self.client_count + self.rng.choice([-1, 0, 0, 0, 1])))
        uptime = int(time.time() - self.start) + 1_083_600

        failed_over = self.fault == "failover"
        wan_down = self.fault == "wan-down"
        primary_ip = "203.0.113.47"
        backup_ip = "100.71.14.9"
        active_ip = backup_ip if failed_over else primary_ip

        health = [
            {
                "subsystem": "wan",
                "status": "error" if wan_down else "ok",
                "wan_ip": "" if wan_down else active_ip,
                "gw_name": "Dream Machine Pro",
                "num_adopted": 7,
                "num_disconnected": 0,
            },
            {
                "subsystem": "www",
                "status": "ok",
                "latency": round(11 + self.rng.uniform(-2, 4), 1),
                "uptime": uptime,
                "xput_down": 934.2,
                "xput_up": 41.6,
                "speedtest_ping": 11.4,
                "speedtest_lastrun": time.time() - 5400,
                "isp_name": "Example Fiber",
            },
            {"subsystem": "lan", "status": "ok", "num_user": 11, "num_guest": 0, "num_adopted": 4},
            {"subsystem": "wlan", "status": "ok", "num_user": self.client_count - 11, "num_ap": 3},
        ]

        devices = [
            {
                "type": "udm",
                "name": "Dream Machine Pro",
                "model": "UDMPRO",
                "state": 1,
                "upgradable": False,
                # Shaped like a real UCG-Max: no wan_networkgroup, `name` is
                # the interface name, and the cellular link is an mbb tunnel.
                "wan1": {
                    "up": not (failed_over or wan_down),
                    "ifname": "eth4",
                    "name": "eth4",
                    "type": "ethernet",
                    "media": "2.5GE",
                    "netmask": "255.255.255.0",
                    "mac": "aa:bb:cc:00:11:22",
                    "speed": 1000,
                    "max_speed": 2500,
                    "ip": "" if (failed_over or wan_down) else primary_ip,
                    "rx_bytes-r": 0 if (failed_over or wan_down) else rx,
                    "tx_bytes-r": 0 if (failed_over or wan_down) else tx,
                    "rx_bytes": int(rx * 8000),
                    "tx_bytes": int(tx * 8000),
                    "uptime": uptime,
                    "isp_name": "Example Fiber",
                },
                "wan3": {
                    "up": True,
                    "ifname": "gre1",
                    "name": "gre1",
                    "type": "wireless_5g",
                    "mbb_state": "ready",
                    "netmask": "255.255.255.224",
                    "mbb": {
                        "rat": "5G", "signal_pct": 78,
                        "lte_rsrp": -92, "lte_sinr": 17.2,
                        "nr_rsrp": -83, "nr_sinr": 10,
                        "nr_ca": [{"band": 71, "primary": True}, {"band": 41, "primary": False}],
                    },
                    "ip": backup_ip if failed_over else "",
                    "rx_bytes-r": rx * 0.2 if failed_over else 0,
                    "tx_bytes-r": tx * 0.2 if failed_over else 0,
                    "rx_bytes": int(rx * 400),
                    "tx_bytes": int(tx * 400),
                    "uptime": 86_400,
                    "isp_name": "Example Cellular",
                },
            },
            {"type": "usw", "name": "Office switch", "state": 1, "upgradable": True},
            {"type": "uap", "name": "Office AP", "state": 1, "upgradable": False},
            {"type": "uap", "name": "Living room AP", "state": 1, "upgradable": False},
            {"type": "uap", "name": "Shop AP", "state": 1, "upgradable": False},
        ]

        clients = []
        for i in range(self.client_count):
            wired = i < 11
            if wired:
                clients.append(
                    {
                        "mac": f"{_MACS[i % len(_MACS)]}:00:{i:02x}",
                        "name": f"{_NAMES[i % len(_NAMES)]}",
                        "is_wired": True,
                        "ip": f"10.0.1.{20 + i}",
                        "network": "LAN",
                        "oui": _VENDORS[i % len(_VENDORS)],
                        "uptime": 3600 * (i + 2),
                        "sw_name": "Office switch",
                    }
                )
                continue
            # A few clients are deliberately far from their AP so the weak
            # count and the worst-clients list are never empty.
            far = i % 9 == 0
            signal = self.rng.uniform(-82, -71) if far else self.rng.uniform(-67, -42)
            clients.append(
                {
                    "mac": f"{_MACS[i % len(_MACS)]}:11:{i:02x}",
                    "name": (
                        _NAMES[i % len(_NAMES)]
                        if i < len(_NAMES)
                        else f"{_NAMES[i % len(_NAMES)]} {i // len(_NAMES) + 1}"
                    ),
                    "is_wired": False,
                    "is_guest": i % 13 == 0,
                    "signal": round(signal, 1),
                    "radio": _BANDS[i % len(_BANDS)],
                    "essid": "Home" if i % 13 else "Home Guest",
                    "ap_displayname": _APS[i % len(_APS)],
                    "ip": f"10.0.{2 if i % 13 else 3}.{20 + i}",
                    "network": "Home" if i % 13 else "Guest",
                    "oui": _VENDORS[i % len(_VENDORS)],
                    "uptime": 600 * (i + 1),
                    "tx_rate": int(self.rng.uniform(120_000, 1_200_000)),
                    "rx_rate": int(self.rng.uniform(120_000, 1_200_000)),
                }
            )
        return {"health": health, "devices": devices, "clients": clients}


def seed_history(store, source: "DemoSource", *, minutes: int, interval: float) -> None:
    """Backfill the sample table so a fresh demo has a full graph window."""
    now = time.time()
    steps = int(minutes * 60 / interval)
    batch = []
    for step in range(steps, 0, -1):
        ts = now - step * interval
        rx, tx = source._throughput(ts - source.start)
        probe = source.ping()
        probe_dns = source.dns()
        batch.append(
            {
                "ts": ts,
                "rx_bps": rx,
                "tx_bps": tx,
                "latency_ms": probe.avg_ms,
                "ping_sent": probe.sent,
                "ping_recv": probe.received,
                "clients": source.client_count,
                "wlan_score": round(78 + 8 * math.sin(ts / 600.0), 1),
                "wan_up": True,
                "dns_ok": probe_dns.ok,
                "dns_ms": probe_dns.elapsed_ms,
            }
        )
    store.record_many(batch)
