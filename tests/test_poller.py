import pytest

from unifi_dashboard.config import Config
from unifi_dashboard.metrics import WanStatus
from unifi_dashboard.poller import Poller
from unifi_dashboard.storage import History


def make_poller(**overrides):
    cfg = Config(demo=True, poll_interval=1.0)
    for key, value in overrides.items():
        setattr(cfg, key, value)
    return Poller(cfg, History(":memory:"))


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

    for key in ("wan", "clients", "devices", "wlan", "window", "series", "stale_s"):
        assert key in payload
    series = payload["series"]
    assert set(series) == {"ts", "rx_bps", "tx_bps", "latency_ms", "loss_pct"}
    assert len({len(values) for values in series.values()}) == 1  # parallel arrays


@pytest.mark.asyncio
async def test_a_failed_poll_keeps_the_last_good_snapshot():
    poller = make_poller()
    good = await poller.tick()
    poller._record_failure("controller timed out")

    assert poller.snapshot["ok"] is False
    assert poller.snapshot["error"] == "controller timed out"
    # The numbers from the last successful poll are still there to display.
    assert poller.snapshot["wan"]["ip"] == good["wan"]["ip"]
