from unifi_dashboard.alarm import CRITICAL, OK, WARNING, evaluate
from unifi_dashboard.config import AlarmConfig

HEALTHY = dict(
    controller_ok=True, wan_up=True, internet_reachable=True,
    dns_ok=True, loss_pct=0.0, latency_ms=12.0,
)


def check(**overrides):
    return evaluate(AlarmConfig(), **{**HEALTHY, **overrides})


def test_a_healthy_network_is_quiet():
    alarm = check()
    assert alarm.level == OK
    assert alarm.reasons == []
    assert alarm.headline is None


def test_wan_down_is_critical():
    assert check(wan_up=False).level == CRITICAL


def test_dns_failure_is_critical_on_its_own():
    # Routing works, names do not: the failure users notice first.
    alarm = check(dns_ok=False)
    assert alarm.level == CRITICAL
    assert alarm.headline == "DNS is not resolving"


def test_loss_crosses_warning_then_critical():
    assert check(loss_pct=1.0).level == OK
    assert check(loss_pct=2.0).level == WARNING
    assert check(loss_pct=10.0).level == CRITICAL


def test_latency_crosses_warning_then_critical():
    assert check(latency_ms=149).level == OK
    assert check(latency_ms=150).level == WARNING
    assert check(latency_ms=400).level == CRITICAL


def test_backup_wan_is_a_warning_by_default_and_configurable():
    assert check(on_backup=True).level == WARNING
    loud = evaluate(AlarmConfig(failover_is_critical=True), **HEALTHY, on_backup=True)
    assert loud.level == CRITICAL


def test_an_unreachable_controller_never_claims_an_outage():
    # We cannot see the WAN, which is not the same as the WAN being down.
    alarm = evaluate(
        AlarmConfig(), controller_ok=False, wan_up=None, internet_reachable=None,
        dns_ok=None, loss_pct=None, latency_ms=None,
    )
    assert alarm.level == WARNING
    assert alarm.reasons == ["Controller unreachable"]


def test_the_headline_is_the_most_fundamental_failure():
    alarm = check(wan_up=False, internet_reachable=False, dns_ok=False, loss_pct=100.0)
    assert alarm.headline == "WAN is down"
    assert alarm.level == CRITICAL


HOT = dict(temp_warning_c=80.0, temp_critical_c=90.0)


def test_gateway_heat_crosses_warning_then_critical():
    assert check(temperature_c=79.0, **HOT).level == OK
    assert check(temperature_c=80.0, **HOT).level == WARNING
    assert check(temperature_c=90.0, **HOT).level == CRITICAL


def test_the_controllers_own_overheating_flag_wins():
    # UniFi knows the per-model limit better than a threshold we picked, so a
    # device that says it is overheating is critical whatever the number says.
    alarm = check(temperature_c=61.0, overheating=True, **HOT)
    assert alarm.level == CRITICAL
    assert alarm.headline == "Gateway overheating"


def test_heat_is_reported_in_the_unit_on_screen():
    assert check(temperature_c=95.0, temperature_unit="F", **HOT).headline == "Gateway at 203°F"
    assert check(temperature_c=95.0, **HOT).headline == "Gateway at 95°C"


def test_a_missing_temperature_is_not_an_alarm():
    # Not every model reports one, and silence is not heat.
    assert check(temperature_c=None, **HOT).level == OK


def test_an_outage_still_leads_over_a_hot_gateway():
    alarm = check(wan_up=False, temperature_c=95.0, **HOT)
    assert alarm.headline == "WAN is down"
    assert "Gateway at 95°C" in alarm.reasons
