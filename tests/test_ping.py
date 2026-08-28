from unifi_dashboard.ping import parse_ping_output

IPUTILS = """PING 8.8.8.8 (8.8.8.8) 56(84) bytes of data.
64 bytes from 8.8.8.8: icmp_seq=1 ttl=116 time=12.3 ms
64 bytes from 8.8.8.8: icmp_seq=3 ttl=116 time=14.5 ms

--- 8.8.8.8 ping statistics ---
3 packets transmitted, 2 received, 33.3333% packet loss, time 502ms
rtt min/avg/max/mdev = 12.300/13.400/14.500/1.100 ms
"""

BSD = """--- 8.8.8.8 ping statistics ---
3 packets transmitted, 3 packets received, 0.0% packet loss
round-trip min/avg/max/stddev = 11.100/12.200/13.300/0.500 ms
"""

TOTAL_LOSS = """PING 8.8.8.8 (8.8.8.8) 56(84) bytes of data.

--- 8.8.8.8 ping statistics ---
3 packets transmitted, 0 received, 100% packet loss, time 2039ms
"""


def test_parses_iputils_summary():
    result = parse_ping_output(IPUTILS, expected=3)
    assert (result.sent, result.received) == (3, 2)
    assert result.avg_ms == 13.4
    assert result.min_ms == 12.3
    assert result.max_ms == 14.5
    assert round(result.loss_pct, 1) == 33.3
    assert result.reachable is True


def test_parses_bsd_summary():
    result = parse_ping_output(BSD, expected=3)
    assert (result.sent, result.received) == (3, 3)
    assert result.avg_ms == 12.2
    assert result.loss_pct == 0.0


def test_total_loss_has_no_latency():
    result = parse_ping_output(TOTAL_LOSS, expected=3)
    assert result.received == 0
    assert result.avg_ms is None
    assert result.loss_pct == 100.0
    assert result.reachable is False


def test_unparseable_output_counts_as_total_loss():
    result = parse_ping_output("ping: command failed", expected=3)
    assert (result.sent, result.received) == (3, 0)
    assert result.loss_pct == 100.0
