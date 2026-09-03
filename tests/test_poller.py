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


@pytest.mark.asyncio
async def test_speedtest_reports_idle_before_one_is_asked_for():
    snapshot = await make_poller().tick()
    test = snapshot["speedtest"]
    assert test["running"] is False and test["finished"] is False
    assert test["down_mbps"] is not None      # the gateway's previous result


@pytest.mark.asyncio
async def test_speedtest_runs_then_publishes_a_fresh_result():
    poller = make_poller()
    await poller.tick()
    before = poller.snapshot["wan"]["speedtest_ts"]

    started = await poller.start_speedtest()
    assert started["running"] is True

    # Still pending: the gateway has not published anything newer yet.
    during = (await poller.tick())["speedtest"]
    assert during["running"] is True and during["finished"] is False

    # The demo finishes after eight seconds; jump the clock instead of waiting.
    poller.demo.speedtest_until = 0.0
    after = (await poller.tick())["speedtest"]
    assert after["running"] is False
    assert after["finished"] is True
    assert after["ts"] > before               # completion is the timestamp moving


@pytest.mark.asyncio
async def test_a_speedtest_that_never_reports_times_out_rather_than_spinning():
    import unifi_dashboard.poller as poller_module

    poller = make_poller()
    await poller.tick()
    await poller.start_speedtest()
    # Pretend the request was made longer ago than we are willing to wait.
    poller._speedtest["requested_at"] -= poller_module.SPEEDTEST_TIMEOUT_S + 1

    state = (await poller.tick())["speedtest"]
    assert state["running"] is False
    assert state["timed_out"] is True


@pytest.mark.asyncio
async def test_a_refused_speedtest_records_the_reason_and_does_not_look_pending():
    poller = make_poller()
    await poller.tick()

    def refuse():
        raise RuntimeError("controller refused the command")

    poller.demo.start_speedtest = refuse
    with pytest.raises(RuntimeError):
        await poller.start_speedtest()

    state = (await poller.tick())["speedtest"]
    assert state["running"] is False
    assert "refused" in state["error"]


@pytest.mark.asyncio
async def test_a_hot_gateway_raises_the_alarm_while_the_wan_is_healthy():
    # The frame is there to make someone walk to the rack. A gateway cooking
    # itself is a reason to walk even when every link is up.
    poller = make_poller()
    poller.demo.fault = "hot"
    snapshot = await poller.tick()

    assert snapshot["wan"]["online"] is True
    assert snapshot["gateway"]["temperature_c"] >= 90
    assert snapshot["alarm"]["level"] == "critical"
    assert any(r.startswith("Gateway at") for r in snapshot["alarm"]["reasons"])


@pytest.mark.asyncio
async def test_a_cool_gateway_says_nothing():
    snapshot = await make_poller().tick()
    assert snapshot["gateway"]["temperature_c"] < 80
    assert snapshot["alarm"]["level"] == "ok"
