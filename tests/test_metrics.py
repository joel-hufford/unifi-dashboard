from unifi_dashboard import metrics


def test_wan_reads_identity_and_rates(health, devices):
    wan = metrics.wan_from(health, devices)
    assert wan.online is True
    assert wan.ip == "203.0.113.47"
    assert wan.isp == "Example Fiber"
    assert wan.uptime_s == 1083600
    assert wan.gateway_latency_ms == 11
    assert wan.rx_bps == 5242880.0
    assert wan.tx_bps == 262144.0
    assert wan.speedtest_down_mbps == 934.2


def test_wan_offline_when_health_says_so(health, devices):
    health = [dict(entry) for entry in health]
    health[0]["status"] = "error"
    health[1]["status"] = "error"
    assert metrics.wan_from(health, devices).online is False


def test_wan_falls_back_to_cumulative_counters(devices_counters_only):
    wan = metrics.wan_from([], devices_counters_only)
    assert wan.rx_bps is None and wan.tx_bps is None
    assert wan.rx_bytes == 1000000
    assert wan.tx_bytes == 200000
    assert wan.ip == "198.51.100.9"
    # No health payload at all, so "up" comes off the interface itself.
    assert wan.online is True


def test_client_counts(clients):
    counts = metrics.clients_from(clients)
    assert (counts.total, counts.wired, counts.wireless, counts.guest) == (6, 2, 4, 1)


def test_device_counts(devices):
    counts = metrics.devices_from(devices)
    assert (counts.total, counts.online, counts.offline, counts.upgradable) == (4, 3, 1, 1)


def test_signal_conversion_handles_rssi_scale():
    assert metrics.signal_of({"signal": -55}) == -55
    assert metrics.signal_of({"rssi": 18}) == -78          # 0-95 scale, relative to noise
    assert metrics.signal_of({"rssi": -64}) == -64         # already dBm on some firmware
    assert metrics.signal_of({}) is None


def test_score_from_signal_is_clamped():
    assert metrics.score_from_signal(-40) == 100.0
    assert metrics.score_from_signal(-90) == 0.0
    assert metrics.score_from_signal(-67.5) == 50.0
    assert metrics.score_from_signal(None) is None


def test_client_score_prefers_controller_satisfaction():
    assert metrics.client_score({"satisfaction": 88, "signal": -80}) == 88.0
    # -1 is the controller's "not enough samples yet" sentinel, so fall back.
    assert metrics.client_score({"satisfaction": -1, "signal": -67.5}) == 50.0


def test_band_mapping():
    assert metrics.band_of({"radio": "ng"}) == "2.4 GHz"
    assert metrics.band_of({"radio": "na"}) == "5 GHz"
    assert metrics.band_of({"radio": "6e"}) == "6 GHz"
    assert metrics.band_of({"channel": 6}) == "2.4 GHz"
    assert metrics.band_of({"channel": 149}) == "5 GHz"
    assert metrics.band_of({}) is None


def test_wlan_quality_summarises_wireless_clients_only(clients):
    quality = metrics.wlan_quality_from(clients, weak_signal_dbm=-70)
    assert quality.rated == 4                       # the two wired clients are skipped
    assert quality.weak == 2                        # the -71 iPad and the -78 thermostat
    assert quality.bands == {"5 GHz": 1, "2.4 GHz": 2, "6 GHz": 1}
    assert sum(quality.histogram.values()) == 4
    assert quality.worst[0].name == "Thermostat"    # worst first
    assert quality.worst[0].score == 0.0
    assert quality.score is not None and 0 <= quality.score <= 100


def test_wlan_quality_with_no_wireless_clients():
    quality = metrics.wlan_quality_from([{"mac": "x", "is_wired": True}])
    assert quality.rated == 0
    assert quality.score is None
    assert quality.worst == []


def test_band_label_thresholds():
    assert metrics.band_label(95) == "good"
    assert metrics.band_label(70) == "good"
    assert metrics.band_label(60) == "fair"
    assert metrics.band_label(10) == "poor"
    assert metrics.band_label(None) == "unknown"
