"""Turn raw controller JSON into the handful of numbers the dashboard shows.

Everything here is a pure function over decoded JSON so it can be tested
against captured fixtures without a controller. UniFi's payloads vary between
firmware generations, so each extractor walks a list of candidate locations and
takes the first one that is actually present.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

# A client at or above this signal is as good as it gets; at or below the floor
# it is unusable. Between them the score is linear.
_SIGNAL_CEILING_DBM = -50.0
_SIGNAL_FLOOR_DBM = -85.0

# Three bands, because the dashboard paints them with the reserved status
# colours (good / warning / critical) and there is no fourth status colour that
# would not misread.
QUALITY_BANDS = (
    ("good", 70.0),
    ("fair", 45.0),
    ("poor", 0.0),
)


# --------------------------------------------------------------------------
# helpers


def _num(value) -> float | None:
    """Coerce a JSON value to a float, treating UniFi's -1 sentinels as absent."""
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out


def _first_num(source: dict | None, *keys: str) -> float | None:
    if not isinstance(source, dict):
        return None
    for key in keys:
        value = _num(source.get(key))
        if value is not None:
            return value
    return None


def _first_str(source: dict | None, *keys: str) -> str | None:
    if not isinstance(source, dict):
        return None
    for key in keys:
        value = source.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def index_health(health: list[dict]) -> dict[str, dict]:
    """The health endpoint returns a list; key it by subsystem name."""
    out: dict[str, dict] = {}
    for entry in health or []:
        name = entry.get("subsystem")
        if isinstance(name, str):
            out[name] = entry
    return out


def find_gateway(devices: list[dict]) -> dict | None:
    """The gateway is whichever adopted device owns a WAN port."""
    gateway_types = {"ugw", "uxg", "udm", "ubb", "uck"}
    for device in devices or []:
        if device.get("type") in gateway_types:
            return device
    for device in devices or []:
        if isinstance(device.get("wan1"), dict) or device.get("is_gateway"):
            return device
    return None


def wan_interface(gateway: dict | None) -> dict | None:
    """The active WAN interface sub-object, whichever slot it lives in."""
    if not isinstance(gateway, dict):
        return None
    for key in ("wan1", "wan2", "uplink"):
        candidate = gateway.get(key)
        if isinstance(candidate, dict) and candidate:
            # wan2 only counts when wan1 was absent; a failed-over link still
            # reports up, so prefer whichever one says it is up.
            if candidate.get("up") is not False:
                return candidate
    for key in ("wan1", "wan2", "uplink"):
        candidate = gateway.get(key)
        if isinstance(candidate, dict) and candidate:
            return candidate
    return None


# --------------------------------------------------------------------------
# dataclasses


@dataclass
class WanStatus:
    online: bool = False
    ip: str | None = None
    isp: str | None = None
    uptime_s: float | None = None
    gateway_latency_ms: float | None = None
    rx_bps: float | None = None          # download, bytes/second
    tx_bps: float | None = None          # upload, bytes/second
    rx_bytes: float | None = None        # cumulative counters, for rate fallback
    tx_bytes: float | None = None
    speedtest_down_mbps: float | None = None
    speedtest_up_mbps: float | None = None
    speedtest_ping_ms: float | None = None
    speedtest_ts: float | None = None


@dataclass
class ClientCounts:
    total: int = 0
    wireless: int = 0
    wired: int = 0
    guest: int = 0


@dataclass
class DeviceCounts:
    total: int = 0
    online: int = 0
    offline: int = 0
    upgradable: int = 0


@dataclass
class ClientQuality:
    name: str
    mac: str
    score: float
    signal_dbm: float | None
    band: str | None
    ssid: str | None
    ap: str | None
    tx_rate_mbps: float | None
    rx_rate_mbps: float | None


@dataclass
class WlanQuality:
    score: float | None = None
    rated: int = 0
    weak: int = 0
    mean_signal_dbm: float | None = None
    bands: dict[str, int] = field(default_factory=dict)
    histogram: dict[str, int] = field(default_factory=dict)
    worst: list[ClientQuality] = field(default_factory=list)


# --------------------------------------------------------------------------
# extractors


def wan_from(health: list[dict], devices: list[dict]) -> WanStatus:
    subsystems = index_health(health)
    wan = subsystems.get("wan", {})
    www = subsystems.get("www", {})
    gateway = find_gateway(devices)
    iface = wan_interface(gateway)

    status = WanStatus()

    wan_state = wan.get("status") or www.get("status")
    if wan_state:
        status.online = str(wan_state).lower() == "ok"
    elif iface is not None:
        status.online = bool(iface.get("up"))

    status.ip = _first_str(wan, "wan_ip") or _first_str(iface, "ip") or _first_str(www, "wan_ip")
    status.isp = (
        _first_str(www, "isp_name", "isp_organization")
        or _first_str(iface, "isp_name", "isp_organization")
    )
    status.uptime_s = _first_num(www, "uptime") or _first_num(wan, "uptime") or _first_num(iface, "uptime")
    status.gateway_latency_ms = _first_num(www, "latency", "speedtest_ping")

    # Instantaneous rates, if the firmware reports them.
    status.rx_bps = _first_num(iface, "rx_bytes-r") or _first_num(wan, "rx_bytes-r") or _first_num(www, "rx_bytes-r")
    status.tx_bps = _first_num(iface, "tx_bytes-r") or _first_num(wan, "tx_bytes-r") or _first_num(www, "tx_bytes-r")

    # Cumulative counters, so the poller can difference them when it has to.
    status.rx_bytes = _first_num(iface, "rx_bytes") or _first_num(wan, "rx_bytes")
    status.tx_bytes = _first_num(iface, "tx_bytes") or _first_num(wan, "tx_bytes")

    status.speedtest_down_mbps = _first_num(www, "xput_down")
    status.speedtest_up_mbps = _first_num(www, "xput_up")
    status.speedtest_ping_ms = _first_num(www, "speedtest_ping")
    status.speedtest_ts = _first_num(www, "speedtest_lastrun")
    return status


def clients_from(clients: list[dict]) -> ClientCounts:
    counts = ClientCounts()
    for client in clients or []:
        counts.total += 1
        if client.get("is_wired"):
            counts.wired += 1
        else:
            counts.wireless += 1
        if client.get("is_guest"):
            counts.guest += 1
    return counts


def devices_from(devices: list[dict]) -> DeviceCounts:
    counts = DeviceCounts()
    for device in devices or []:
        counts.total += 1
        if device.get("state") == 1:
            counts.online += 1
        else:
            counts.offline += 1
        if device.get("upgradable"):
            counts.upgradable += 1
    return counts


def signal_of(client: dict) -> float | None:
    """Client signal in dBm.

    ``signal`` is already dBm. ``rssi`` is a 0-95 scale relative to the noise
    floor, which converts by subtracting 96.
    """
    signal = _num(client.get("signal"))
    if signal is not None and signal < 0:
        return signal
    rssi = _num(client.get("rssi"))
    if rssi is not None:
        return rssi - 96 if rssi >= 0 else rssi
    return None


def score_from_signal(signal_dbm: float | None) -> float | None:
    if signal_dbm is None:
        return None
    span = _SIGNAL_CEILING_DBM - _SIGNAL_FLOOR_DBM
    ratio = (signal_dbm - _SIGNAL_FLOOR_DBM) / span
    return round(max(0.0, min(1.0, ratio)) * 100.0, 1)


def client_score(client: dict) -> float | None:
    """Prefer the controller's own satisfaction score, fall back to signal.

    ``satisfaction`` is the controller's blend of signal, retries and airtime,
    but it is absent on wired clients and reported as -1 before it has enough
    samples.
    """
    satisfaction = _num(client.get("satisfaction"))
    if satisfaction is not None and 0 <= satisfaction <= 100:
        return float(satisfaction)
    return score_from_signal(signal_of(client))


def band_of(client: dict) -> str | None:
    radio = client.get("radio")
    mapping = {"ng": "2.4 GHz", "na": "5 GHz", "6e": "6 GHz", "ax": "6 GHz"}
    if isinstance(radio, str) and radio in mapping:
        return mapping[radio]
    channel = _num(client.get("channel"))
    if channel is None:
        return None
    if channel <= 14:
        return "2.4 GHz"
    if channel >= 33:
        return "6 GHz" if channel > 177 else "5 GHz"
    return None


def band_label(score: float | None) -> str:
    if score is None:
        return "unknown"
    for name, floor in QUALITY_BANDS:
        if score >= floor:
            return name
    return "poor"


def wlan_quality_from(clients: list[dict], *, weak_signal_dbm: int = -70, worst_n: int = 5) -> WlanQuality:
    quality = WlanQuality(histogram={name: 0 for name, _ in QUALITY_BANDS})
    scores: list[float] = []
    signals: list[float] = []
    rated: list[ClientQuality] = []

    for client in clients or []:
        if client.get("is_wired"):
            continue
        score = client_score(client)
        if score is None:
            continue
        signal = signal_of(client)
        scores.append(score)
        quality.histogram[band_label(score)] += 1
        if signal is not None:
            signals.append(signal)
            if signal <= weak_signal_dbm:
                quality.weak += 1

        band = band_of(client)
        if band:
            quality.bands[band] = quality.bands.get(band, 0) + 1

        rated.append(
            ClientQuality(
                name=_first_str(client, "name", "hostname", "display_name") or client.get("mac", "unknown"),
                mac=client.get("mac", ""),
                score=score,
                signal_dbm=signal,
                band=band,
                ssid=_first_str(client, "essid"),
                ap=_first_str(client, "ap_displayname", "ap_name") or client.get("ap_mac"),
                tx_rate_mbps=_kbps_to_mbps(_num(client.get("tx_rate"))),
                rx_rate_mbps=_kbps_to_mbps(_num(client.get("rx_rate"))),
            )
        )

    quality.rated = len(scores)
    if scores:
        quality.score = round(sum(scores) / len(scores), 1)
    if signals:
        quality.mean_signal_dbm = round(sum(signals) / len(signals), 1)
    quality.worst = sorted(rated, key=lambda c: c.score)[:worst_n]
    return quality


def _kbps_to_mbps(value: float | None) -> float | None:
    if value is None or value <= 0:
        return None
    return round(value / 1000.0, 1)


def as_dict(obj) -> dict:
    return asdict(obj)
