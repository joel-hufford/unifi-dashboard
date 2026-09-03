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


# --- multiple WAN links ----------------------------------------------------

GATEWAY_DUAL_WAN = [
    {
        "type": "udm",
        # Deliberately out of order, with a gap: a cellular backup commonly
        # lands in wan3 with no wan2 at all.
        "wan3": {"up": True, "ifname": "wwan0", "ip": "100.71.14.9",
                 "isp_name": "Example Cellular", "uptime": 86400},
        "wan1": {"up": True, "ifname": "eth8", "ip": "203.0.113.47",
                 "isp_name": "Example Fiber", "rx_bytes-r": 5e6, "tx_bytes-r": 2e5},
        "wan_networkgroup": "not a slot",
        "wanX": {"up": True},
    }
]
HEALTH_ON_PRIMARY = [{"subsystem": "wan", "status": "ok", "wan_ip": "203.0.113.47"}]
HEALTH_ON_BACKUP = [{"subsystem": "wan", "status": "ok", "wan_ip": "100.71.14.9"}]


def test_wan_links_are_discovered_sorted_and_filtered():
    links = metrics.wan_links_from(HEALTH_ON_PRIMARY, GATEWAY_DUAL_WAN)
    assert [link.key for link in links] == ["wan1", "wan3"]   # numeric order, gap kept
    assert links[0].label == "WAN 1" and links[1].label == "WAN 3"


def test_cellular_link_is_recognised_by_interface_name():
    links = metrics.wan_links_from(HEALTH_ON_PRIMARY, GATEWAY_DUAL_WAN)
    assert links[0].cellular is False
    assert links[1].cellular is True


def test_the_active_link_is_the_one_holding_the_wan_address():
    on_primary = metrics.wan_links_from(HEALTH_ON_PRIMARY, GATEWAY_DUAL_WAN)
    assert metrics.active_link(on_primary).key == "wan1"

    on_backup = metrics.wan_links_from(HEALTH_ON_BACKUP, GATEWAY_DUAL_WAN)
    assert metrics.active_link(on_backup).key == "wan3"
    assert sum(link.active for link in on_backup) == 1


def test_active_link_falls_back_to_the_first_link_that_is_up():
    devices = [{"type": "udm",
                "wan1": {"up": False, "ip": ""},
                "wan3": {"up": True, "ifname": "wwan0", "ip": "100.71.14.9"}}]
    links = metrics.wan_links_from([], devices)
    assert metrics.active_link(links).key == "wan3"


def test_wan_from_follows_a_failover_into_a_non_contiguous_slot():
    # The regression this guards: a hardcoded wan1/wan2 lookup finds nothing
    # when traffic moves to wan3, and throughput silently goes blank.
    devices = [{"type": "udm",
                "wan1": {"up": False, "ip": "", "rx_bytes-r": 0},
                "wan3": {"up": True, "ifname": "wwan0", "ip": "100.71.14.9",
                         "rx_bytes-r": 1.2e6, "tx_bytes-r": 4.5e4, "uptime": 86400}}]
    wan = metrics.wan_from(HEALTH_ON_BACKUP, devices)
    assert wan.ip == "100.71.14.9"
    assert wan.rx_bps == 1.2e6
    assert wan.tx_bps == 4.5e4


def test_no_gateway_yields_no_links():
    assert metrics.wan_links_from([], []) == []
    assert metrics.active_link([]) is None


# --- against a real UCG-Max payload ----------------------------------------
# Captured from a live gateway with a UniFi cellular backup, with addresses and
# MACs scrubbed. Every assertion here corresponds to something the synthetic
# fixtures got wrong.

def test_real_ucg_max_dual_wan(ucg_max):
    links = metrics.wan_links_from(ucg_max["health"], [ucg_max["gateway"]])
    assert [link.key for link in links] == ["wan1", "wan3"]

    primary, backup = links

    # Labels: `name` is the interface name on this firmware ("eth4", "gre1"),
    # so it must not be used as a label.
    assert (primary.label, backup.label) == ("WAN 1", "WAN 3")

    # A 2.5GbE ethernet WAN reports media "2.5GE". Substring-matching "5g"
    # against that flagged the primary as cellular.
    assert primary.cellular is False
    assert primary.up is True and primary.active is True

    # The real cellular signal is the mbb block, not the interface name: this
    # gateway tunnels its cellular backup over gre1.
    assert backup.cellular is True
    assert backup.rat == "5G"
    assert backup.signal_pct == 100
    assert backup.active is False and backup.up is True   # connected but idle

    # ISP and uptime are only in the health subsystems, never on the interface.
    assert primary.isp == "Example Broadband"
    assert primary.uptime_s == 1426
    assert backup.isp is None


def test_real_payload_flattens_to_the_active_link(ucg_max):
    wan = metrics.wan_from(ucg_max["health"], [ucg_max["gateway"]])
    assert wan.online is True
    assert wan.ip == "192.0.2.20"
    assert wan.isp == "Example Broadband"
    assert wan.rx_bps == 2777 and wan.tx_bps == 28390


def test_prefix_length_from_netmask():
    assert metrics.prefix_length("255.255.255.0") == 24
    assert metrics.prefix_length("255.255.255.224") == 27
    assert metrics.prefix_length("not a mask") is None
    assert metrics.prefix_length(None) is None


def test_cellular_radio_detail_prefers_the_camped_technology(ucg_max):
    links = metrics.wan_links_from(ucg_max["health"], [ucg_max["gateway"]])
    backup = links[1]
    # The modem reports both LTE and NR figures; camped on 5G, the NR ones are
    # the meaningful pair (nr_rsrp -83, not lte_rsrp -92).
    assert backup.rsrp == -83
    assert backup.sinr == 10
    assert backup.bands == "n71 + n41"


def test_link_speed_exposes_an_underperforming_port(ucg_max):
    primary = metrics.wan_links_from(ucg_max["health"], [ucg_max["gateway"]])[0]
    assert primary.speed_mbps == 1000
    assert primary.max_speed_mbps == 2500      # negotiated below capability
    assert primary.prefix == 24
    assert primary.mac


# --- the client directory --------------------------------------------------

def test_client_list_sorts_by_address_numerically(clients):
    rows = metrics.client_list_from(clients)
    # String ordering would put .10 before .9; addresses have to sort as numbers.
    listed = [c for c in [
        {"mac": "a", "ip": "10.0.0.10"}, {"mac": "b", "ip": "10.0.0.9"},
        {"mac": "c", "ip": "10.0.0.100"},
    ]]
    assert [row.ip for row in metrics.client_list_from(listed)] == \
        ["10.0.0.9", "10.0.0.10", "10.0.0.100"]
    assert len(rows) == len(clients)


def test_clients_without_an_address_sort_last():
    rows = metrics.client_list_from([
        {"mac": "a", "name": "No lease"},
        {"mac": "b", "name": "Has one", "ip": "10.0.0.5"},
    ])
    assert [row.name for row in rows] == ["Has one", "No lease"]


def test_client_entries_carry_what_the_directory_shows(clients):
    rows = {row.mac: row for row in metrics.client_list_from(clients)}

    wireless = rows["aa:bb:cc:00:00:03"]
    assert wireless.name == "Joel's MacBook"
    assert wireless.wired is False
    assert wireless.ssid == "Home"
    assert wireless.ap == "Office AP"
    assert wireless.signal_dbm == -52

    wired = rows["aa:bb:cc:00:00:01"]
    assert wired.wired is True
    assert wired.signal_dbm is None      # a cable has no signal strength


def test_unnamed_clients_fall_back_to_vendor_then_mac():
    # Addressless clients sort by name, so look them up rather than assuming
    # the order they were passed in.
    rows = {row.mac: row for row in metrics.client_list_from([
        {"mac": "aa:bb:cc:00:00:09", "oui": "Espressif"},
        {"mac": "aa:bb:cc:00:00:0a"},
    ])}
    assert rows["aa:bb:cc:00:00:09"].name == "Espressif"
    assert rows["aa:bb:cc:00:00:0a"].name == "aa:bb:cc:00:00:0a"


def test_nothing_is_active_when_the_controller_reports_the_wan_down():
    # Reported from hardware: with both uplinks disconnected the cellular link
    # still showed green. It is a GRE tunnel, so it reports up whenever the
    # interface exists - guessing the active link from `up` alone lit it during
    # a total outage.
    health = [{"subsystem": "wan", "status": "error", "wan_ip": ""},
              {"subsystem": "www", "status": "error"}]
    devices = [{"type": "udm",
                "wan1": {"up": False, "ifname": "eth4", "ip": ""},
                "wan3": {"up": True, "ifname": "gre1", "type": "wireless_5g",
                         "mbb_state": "ready", "ip": ""}}]

    links = metrics.wan_links_from(health, devices)
    assert [link.active for link in links] == [False, False]
    assert metrics.wan_from(health, devices).online is False


def test_a_link_without_an_address_is_never_guessed_as_active():
    # No health data at all, so guessing is allowed - but an interface holding
    # no address cannot be the one carrying traffic.
    links = metrics.wan_links_from([], [{"type": "udm",
        "wan1": {"up": False, "ip": ""},
        "wan3": {"up": True, "ifname": "gre1", "ip": ""}}])
    assert [link.active for link in links] == [False, False]


def test_a_healthy_wan_still_resolves_an_active_link_without_an_ip_match():
    links = metrics.wan_links_from(
        [{"subsystem": "wan", "status": "ok"}],
        [{"type": "udm", "wan1": {"up": True, "ip": "192.0.2.20"}}],
    )
    assert links[0].active is True


def test_a_stale_wan_address_does_not_keep_a_dead_link_active():
    # Reported from hardware, second round: with both uplinks pulled, WAN 1
    # stayed green. A DHCP lease outlives the cable, so the controller was
    # still reporting the old wan_ip and the interface still carried it - and
    # the IP-match branch neither checked the WAN's reported health nor whether
    # the interface was up.
    health = [{"subsystem": "wan", "status": "error", "wan_ip": "10.199.99.155"},
              {"subsystem": "www", "status": "error"}]
    devices = [{"type": "udm",
                "wan1": {"up": False, "ifname": "eth4", "ip": "10.199.99.155"},
                "wan3": {"up": True, "ifname": "gre1", "type": "wireless_5g", "ip": ""}}]

    assert [link.active for link in metrics.wan_links_from(health, devices)] == [False, False]

    # Same, but the port has not noticed yet either.
    devices[0]["wan1"]["up"] = True
    assert [link.active for link in metrics.wan_links_from(health, devices)] == [False, False]


def test_a_down_interface_is_never_active_even_with_a_matching_address():
    # The controller says the WAN is fine and names an address the down
    # interface still holds. Being down beats the address match.
    links = metrics.wan_links_from(
        [{"subsystem": "wan", "status": "ok", "wan_ip": "10.199.99.155"}],
        [{"type": "udm",
          "wan1": {"up": False, "ip": "10.199.99.155"},
          "wan3": {"up": True, "ifname": "gre1", "ip": "198.51.100.7"}}],
    )
    assert links[0].active is False
    assert links[1].active is True      # the one that is actually up


# --- the gateway's own vitals ----------------------------------------------

def test_temperature_from_a_named_sensor_list_prefers_the_cpu():
    # Newer firmware publishes several sensors. The CPU one is the number worth
    # putting on a panel, not whichever happens to come first.
    device = [{"type": "udm", "temperatures": [
        {"name": "PHY", "type": "phy", "value": 55.0},
        {"name": "CPU", "type": "cpu", "value": 62.5},
        {"name": "System", "type": "board", "value": 48.0},
    ]}]
    assert metrics.gateway_temperature(device[0]) == (62.5, "CPU")
    assert metrics.gateway_health_from(device).temperature_c == 62.5


def test_without_a_cpu_sensor_the_hottest_one_wins():
    device = {"type": "udm", "temperatures": [
        {"name": "PHY", "value": 51.0}, {"name": "Board", "value": 63.0},
    ]}
    assert metrics.gateway_temperature(device) == (63.0, "Board")


def test_older_firmware_reports_a_single_value():
    assert metrics.gateway_temperature({"general_temperature": 57}) == (57.0, "system")


def test_a_gateway_with_no_sensor_reports_nothing_rather_than_zero():
    # Not every model has one, and a fabricated 0 would read as a fault.
    assert metrics.gateway_temperature({"type": "udm", "name": "gw"}) == (None, None)
    assert metrics.gateway_health_from([{"type": "udm"}]).temperature_c is None
    assert metrics.gateway_health_from([]).temperature_c is None


def test_gateway_health_carries_the_overheating_flag_and_system_stats():
    health = metrics.gateway_health_from([{
        "type": "udm", "name": "UCG", "model": "UCGMAX",
        "general_temperature": 91, "overheating": True,
        "system-stats": {"cpu": "12.4", "mem": "38.1"},
    }])
    assert health.overheating is True
    assert health.model == "UCGMAX"
    assert health.cpu_pct == 12.4 and health.mem_pct == 38.1
