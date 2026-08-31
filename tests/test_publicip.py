import time

import httpx
import pytest

from unifi_dashboard.config import PublicIpConfig
from unifi_dashboard.publicip import PublicIpProbe, _first_address


def transport(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def responder(body, status=200):
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(status, text=body)

    return handler, calls


def test_parses_bare_and_json_bodies():
    assert _first_address("203.0.113.9\n") == "203.0.113.9"
    assert _first_address('{"ip": "203.0.113.9"}') == "203.0.113.9"
    assert _first_address('{"origin": "203.0.113.9"}') == "203.0.113.9"
    assert _first_address("2001:db8::1") == "2001:db8::1"


def test_rejects_anything_that_is_not_an_address():
    # A captive portal answering with HTML must not become the "public IP".
    assert _first_address("<html>Sign in to continue</html>") is None
    assert _first_address("") is None
    assert _first_address('{"nope": 1}') is None


@pytest.mark.asyncio
async def test_result_is_cached_between_polls():
    handler, calls = responder("203.0.113.9")
    probe = PublicIpProbe(PublicIpConfig(interval_minutes=15))

    async with transport(handler) as client:
        first = await probe.get("192.0.2.20", client=client)
        second = await probe.get("192.0.2.20", client=client)

    assert first.address == "203.0.113.9"
    assert second.address == "203.0.113.9"
    assert len(calls) == 1          # one lookup, not one per poll


@pytest.mark.asyncio
async def test_a_changed_wan_address_forces_a_fresh_lookup():
    handler, calls = responder("203.0.113.9")
    probe = PublicIpProbe(PublicIpConfig(interval_minutes=60))

    async with transport(handler) as client:
        await probe.get("192.0.2.20", client=client)
        await probe.get("198.51.100.4", client=client)   # moved networks

    assert len(calls) == 2


@pytest.mark.asyncio
async def test_a_failure_keeps_the_last_known_address_and_records_why():
    def handler(request):
        return httpx.Response(500, text="nope")

    probe = PublicIpProbe(PublicIpConfig())
    probe._result.address = "203.0.113.9"
    probe._result.checked_at = time.time() - 3600

    async with transport(handler) as client:
        result = await probe.get("192.0.2.20", client=client)

    assert result.address == "203.0.113.9"   # stale, but better than nothing
    assert result.error


@pytest.mark.asyncio
async def test_disabled_probe_makes_no_request():
    handler, calls = responder("203.0.113.9")
    probe = PublicIpProbe(PublicIpConfig(enabled=False))

    async with transport(handler) as client:
        result = await probe.get("192.0.2.20", client=client)

    assert result.enabled is False
    assert result.address is None
    assert calls == []


# --- the one write the dashboard makes -------------------------------------

@pytest.mark.asyncio
async def test_a_read_only_credential_gets_an_explanatory_refusal():
    from unifi_dashboard.config import UniFiConfig
    from unifi_dashboard.unifi_client import UniFiAuthError, UniFiClient

    client = UniFiClient(UniFiConfig(api_key="k"))
    client._prefix = "/proxy/network"
    client._authenticated = True
    client._http = httpx.AsyncClient(
        base_url="https://192.0.2.1",
        transport=httpx.MockTransport(lambda request: httpx.Response(403, text="forbidden")),
    )

    with pytest.raises(UniFiAuthError) as caught:
        await client.start_speedtest()
    # Starting a test is the only write, so a read-only key fails here alone -
    # the message has to say that rather than "auth failed".
    assert "read-only" in str(caught.value)
    await client.aclose()
