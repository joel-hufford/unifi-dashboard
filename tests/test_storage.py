import time

from unifi_dashboard.storage import History


def make_history(**kwargs):
    return History(":memory:", **kwargs)


def test_window_returns_only_recent_samples():
    store = make_history(retention_minutes=180)
    now = time.time()
    store.record_many([{"ts": now - offset, "rx_bps": 1.0} for offset in (30, 600, 5400)])
    assert len(store.window(60, now=now)) == 2
    assert len(store.window(180, now=now)) == 3


def test_summary_averages_and_peaks():
    store = make_history()
    now = time.time()
    store.record_many([
        {"ts": now - 30, "rx_bps": 1_000_000, "tx_bps": 100_000, "latency_ms": 10,
         "ping_sent": 3, "ping_recv": 3, "wan_up": True},
        {"ts": now - 20, "rx_bps": 3_000_000, "tx_bps": 300_000, "latency_ms": 30,
         "ping_sent": 3, "ping_recv": 2, "wan_up": True},
    ])
    summary = store.summary(60, now=now)
    assert summary["samples"] == 2
    assert summary["avg_rx_bps"] == 2_000_000
    assert summary["max_rx_bps"] == 3_000_000
    assert summary["avg_tx_bps"] == 200_000
    assert summary["max_tx_bps"] == 300_000
    assert summary["avg_latency_ms"] == 20
    assert summary["min_latency_ms"] == 10
    assert summary["max_latency_ms"] == 30
    assert summary["packets_sent"] == 6
    assert summary["packets_lost"] == 1
    assert summary["loss_pct"] == round(100 / 6, 2)
    assert summary["uptime_pct"] == 100.0


def test_summary_of_an_empty_window_is_all_none():
    store = make_history()
    summary = store.summary(60)
    assert summary["samples"] == 0
    assert summary["loss_pct"] is None
    assert summary["uptime_pct"] is None
    assert summary["avg_rx_bps"] is None


def test_uptime_percentage_counts_down_samples():
    store = make_history()
    now = time.time()
    store.record_many([
        {"ts": now - 30, "wan_up": True},
        {"ts": now - 20, "wan_up": False},
        {"ts": now - 10, "wan_up": True},
        {"ts": now - 5, "wan_up": True},
    ])
    assert store.summary(60, now=now)["uptime_pct"] == 75.0


def test_old_rows_are_pruned_on_write():
    store = make_history(retention_minutes=1)
    now = time.time()
    store.record({"ts": now - 3600, "rx_bps": 1.0})
    store.record({"ts": now, "rx_bps": 2.0})
    assert len(store.window(1440, now=now)) == 1
