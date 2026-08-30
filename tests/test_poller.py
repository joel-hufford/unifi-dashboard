import random

import pytest

from unifi_dashboard.config import Config
from unifi_dashboard.metrics import WanStatus
from unifi_dashboard.poller import Poller
from unifi_dashboard.storage import History


def make_poller(**overrides):
    cfg = Config(demo=True, poll_interval=1.0)
    for key, value in overrides.items():
        setattr(cfg, key, value)
    poller = Poller(cfg, History(":memory:"))
    if poller.demo is not None:
        # The demo source drops every ping 0.5% of the time by design, which
        # makes assertions about latency flaky roughly once in two hundred
        # runs. Seed it so a failure means something.
        poller.demo.rng = random.Random(1)
    return poller


def test_reported_rates_are_used_as_is():
    poller = make_poller()
    wan = WanStatus(rx_bps=1_000.0, tx_bps=200.0, rx_bytes=5, tx_bytes=5)
    assert poller._rates(wan, 1000.0) == (1_000.0, 200.0)


def test_counters_are_differenced_when_no_rate_is_reported():
    poller = make_poller()
    first = WanStatus(rx_bytes=1_000_000, tx_bytes=100_000)
    # The first poll has nothing to difference against.
    assert poller._rates(first, 1000.0) == (None, None)

    second = WanStatus(rx_bytes=1_100_000, tx_bytes=110_000)
    rx, tx = poller._rates(second, 1010.0)
    assert rx == 10_000.0     # 100 kB over 10 s
    assert tx == 1_000.0


def test_counter_rollback_is_skipped_rather_than_spiking():
    poller = make_poller()
    poller._rates(WanStatus(rx_bytes=1_000_000, tx_bytes=100_000), 1000.0)
    # A gateway reboot resets the counters; a naive difference would be hugely
    # negative, and clamping it to zero would still draw a bogus point.
    assert poller._rates(WanStatus(rx_bytes=5_000, tx_bytes=500), 1010.0) == (None, None)


def test_missing_counters_and_rates_yield_nothing():
    poller = make_poller()
    assert poller._rates(WanStatus(), 1000.0) == (None, None)


@pytest.mark.asyncio
async def test_tick_builds_a_full_snapshot_in_demo_mode():
    poller = make_poller()
    snapshot = await poller.tick()

    assert snapshot["ok"] is True
    assert snapshot["wan"]["ip"]
    assert snapshot["wan"]["rx_bps"] > 0
    assert snapshot["wan"]["latency_ms"] is not None
    assert snapshot["clients"]["total"] > 0
    assert snapshot["wlan"]["score"] is not None
    assert len(poller.store.window(60)) == 1


@pytest.mark.asyncio
async def test_dashboard_payload_has_the_shape_the_page_reads():
    poller = make_poller()
    await poller.tick()
    payload = poller.dashboard(60)

    for key in ("wan", "clients", "devices", "wlan", "window", "series", "stale_s",
                "wan_links", "alarm", "dns"):
        assert key in payload
    series = payload["series"]
    assert set(series) == {"ts", "rx_bps", "tx_bps", "latency_ms", "loss_pct", "dns_ok"}
    assert len({len(values) for values in series.values()}) == 1  # parallel arrays


@pytest.mark.asyncio
async def test_demo_reports_both_wan_links_with_the_primary_active():
    snapshot = await make_poller().tick()
    links = snapshot["wan_links"]

    assert [link["key"] for link in links] == ["wan1", "wan3"]
    assert links[0]["active"] is True and links[0]["cellular"] is False
    assert links[1]["active"] is False and links[1]["cellular"] is True
    assert snapshot["alarm"]["level"] == "ok"


@pytest.mark.asyncio
async def test_failover_moves_the_active_flag_and_raises_a_warning():
    poller = make_poller()
    poller.demo.fault = "failover"
    snapshot = await poller.tick()

    primary, backup = snapshot["wan_links"]
    assert primary["up"] is False and primary["active"] is False
    assert backup["active"] is True

    assert snapshot["alarm"]["level"] == "warning"
    assert "Running on the backup WAN" in snapshot["alarm"]["reasons"]
    # Throughput has to follow the link that is actually carrying traffic.
    assert snapshot["wan"]["rx_bps"] > 0
    assert snapshot["wan"]["ip"] == backup["ip"]


@pytest.mark.asyncio
async def test_dns_failure_is_critical_even_while_the_wan_is_up():
    poller = make_poller()
    poller.demo.fault = "dns"
    snapshot = await poller.tick()

    assert snapshot["dns"]["ok"] is False
    assert snapshot["wan"]["online"] is True      # routing is fine
    assert snapshot["alarm"]["level"] == "critical"
    assert snapshot["alarm"]["reasons"][0] == "DNS is not resolving"


@pytest.mark.asyncio
async def test_a_failed_poll_keeps_the_last_good_snapshot():
    poller = make_poller()
    good = await poller.tick()
    poller._record_failure("controller timed out")

    assert poller.snapshot["ok"] is False
    assert poller.snapshot["error"] == "controller timed out"
    # A poll failure says nothing about the WAN, so it must not claim an outage.
    assert poller.snapshot["alarm"]["level"] == "warning"
    assert poller.snapshot["alarm"]["reasons"] == ["Controller unreachable"]
    # The numbers from the last successful poll are still there to display.
    assert poller.snapshot["wan"]["ip"] == good["wan"]["ip"]


@pytest.mark.asyncio
async def test_public_address_is_reported_alongside_the_wan_address():
    snapshot = await make_poller().tick()
    public = snapshot["public_ip"]

    assert public["address"] == "198.51.100.7"
    # Different from the WAN address, so we are behind a NAT - the fact the
    # two-address display exists to surface.
    assert public["behind_nat"] is True
    assert snapshot["wan"]["ip"] != public["address"]
