"""Turn raw controller JSON into the handful of numbers the dashboard shows.

Everything here is a pure function over decoded JSON so it can be tested
against captured fixtures without a controller. UniFi's payloads vary between
firmware generations, so each extractor walks a list of candidate locations and
takes the first one that is actually present.
"""

from __future__ import annotations

import re
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
class WanLink:
    """One WAN interface. A UniFi gateway usually has two: the primary and a
    backup, often cellular."""

    key: str                              # "wan1", "wan2"
    label: str                            # "WAN 1", or the network group name
    up: bool = False
    active: bool = False                  # the link currently carrying traffic
    cellular: bool = False
    rat: str | None = None                # "5G", "LTE" - cellular links only
    signal_pct: float | None = None
    rsrp: float | None = None             # dBm, reference signal power
    sinr: float | None = None             # dB, signal to interference+noise
    bands: str | None = None              # "n71 + n41"
    ip: str | None = None
    prefix: int | None = None             # netmask as a CIDR length
    mac: str | None = None                # venues often want this to register a port
    speed_mbps: float | None = None       # negotiated
    max_speed_mbps: float | None = None   # what the port could do
    isp: str | None = None
    uptime_s: float | None = None
    latency_ms: float | None = None
    rx_bps: float | None = None
    tx_bps: float | None = None
    rx_bytes: float | None = None
    tx_bytes: float | None = None


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


# WAN slots are discovered rather than assumed: numbering is not contiguous
# in the wild - a gateway with a cellular backup commonly reports wan1 and
# wan3 with no wan2 at all.
_WAN_KEY = re.compile(r"^wan(\d+)$")

# Mobile-broadband keys. Their presence is definitive: a UniFi cellular backup
# reports an "mbb" block whatever interface it happens to be tunnelled over
# (a UCG-Max presents its cellular uplink as gre1, not wwan0).
_MBB_KEYS = ("mbb", "mbb_state", "mbb_device_mac")

# Interface *types* that mean cellular. Matched as whole words against `type`
# only - never as substrings across arbitrary fields, because "2.5GE" media on
# an ordinary ethernet WAN contains "5g".
_CELLULAR_TYPES = {"wireless_5g", "wireless_lte", "wireless_4g", "cellular", "lte", "wwan", "modem"}
_CELLULAR_IFNAME_PREFIXES = ("wwan", "ppp", "mbim", "qmi", "cdc-wdm")


def is_cellular(iface: dict | None) -> bool:
    if not isinstance(iface, dict):
        return False
    if any(key in iface for key in _MBB_KEYS):
        return True
    kind = iface.get("type")
    if isinstance(kind, str) and kind.strip().lower() in _CELLULAR_TYPES:
        return True
    ifname = iface.get("ifname")
    if isinstance(ifname, str) and ifname.lower().startswith(_CELLULAR_IFNAME_PREFIXES):
        return True
    return False


def cellular_signal(iface: dict | None) -> dict:
    """Radio detail from an mbb block.

    5G-capable modems report both LTE and NR measurements; which set is
    meaningful depends on what the radio is actually camped on, so the
    reported access technology picks the family.
    """
    mbb = (iface or {}).get("mbb")
    if not isinstance(mbb, dict):
        return {}
    rat = mbb.get("rat") if isinstance(mbb.get("rat"), str) else None
    nr = bool(rat and "5g" in rat.lower()) and _num(mbb.get("nr_rsrp")) is not None
    prefix = "nr_" if nr else "lte_"
    carriers = mbb.get(f"{prefix}ca")
    bands = None
    if isinstance(carriers, list) and carriers:
        letter = "n" if nr else "b"
        names = [
            f"{letter}{entry['band']}"
            for entry in carriers
            if isinstance(entry, dict) and entry.get("band") is not None
        ]
        if names:
            bands = " + ".join(names)
    return {
        "rat": rat,
        "signal_pct": _num(mbb.get("signal_pct")),
        "rsrp": _num(mbb.get(f"{prefix}rsrp")),
        "sinr": _num(mbb.get(f"{prefix}sinr")),
        "bands": bands,
    }


def prefix_length(netmask: str | None) -> int | None:
    """A dotted netmask as a CIDR length: what you tell venue IT."""
    if not isinstance(netmask, str) or not netmask.strip():
        return None
    try:
        import ipaddress
        return ipaddress.IPv4Network(f"0.0.0.0/{netmask.strip()}").prefixlen
    except ValueError:
        return None


def wan_links_from(health: list[dict], devices: list[dict]) -> list[WanLink]:
    """Every WAN the gateway exposes, with the active one flagged.

    ``stat/health`` only ever describes the uplink currently in use, so the
    per-interface detail has to come off the gateway device itself.
    """
    gateway = find_gateway(devices)
    subsystems = index_health(health)
    active_ip = _first_str(subsystems.get("wan", {}), "wan_ip")

    slots: list[tuple[int, str]] = []
    if isinstance(gateway, dict):
        for key in gateway:
            match = _WAN_KEY.match(key)
            if match and isinstance(gateway.get(key), dict) and gateway[key]:
                slots.append((int(match.group(1)), key))
    slots.sort()

    links: list[WanLink] = []
    for number, key in slots:
        iface = gateway[key]
        radio = cellular_signal(iface)
        links.append(
            WanLink(
                key=key,
                # Only a network *group* name is a label. `name` is the
                # interface name on real firmware ("eth4", "gre1"), which is
                # not what anyone wants to read on a wall panel.
                label=_first_str(iface, "wan_networkgroup") or f"WAN {number}",
                up=bool(iface.get("up")),
                cellular=is_cellular(iface),
                rat=radio.get("rat"),
                signal_pct=radio.get("signal_pct"),
                rsrp=radio.get("rsrp"),
                sinr=radio.get("sinr"),
                bands=radio.get("bands"),
                ip=_first_str(iface, "ip"),
                prefix=prefix_length(_first_str(iface, "netmask")),
                mac=_first_str(iface, "mac"),
                speed_mbps=_first_num(iface, "speed"),
                max_speed_mbps=_first_num(iface, "max_speed"),
                isp=_first_str(iface, "isp_name", "isp_organization"),
                uptime_s=_first_num(iface, "uptime"),
                latency_ms=_first_num(iface, "latency"),
                rx_bps=_first_num(iface, "rx_bytes-r"),
                tx_bps=_first_num(iface, "tx_bytes-r"),
                rx_bytes=_first_num(iface, "rx_bytes"),
                tx_bytes=_first_num(iface, "tx_bytes"),
            )
        )

    if not links:
        return links

    # The active link is the one whose address the controller reports as *the*
    # WAN address; failing that, the first one that is up.
    chosen = next((l for l in links if active_ip and l.ip == active_ip), None)
    if chosen is None:
        chosen = next((l for l in links if l.up), links[0])
    chosen.active = True

    # The health subsystems describe the active uplink only, and carry detail
    # the interface object does not: the ISP name lives there, and per-link
    # uptime is not reported at all.
    www = subsystems.get("www", {})
    if chosen.isp is None:
        chosen.isp = _first_str(subsystems.get("wan", {}), "isp_name") or _first_str(
            www, "isp_name", "isp_organization"
        )
    if chosen.uptime_s is None:
        chosen.uptime_s = _first_num(www, "uptime")
    if chosen.latency_ms is None:
        chosen.latency_ms = _first_num(www, "latency")

    return links


def active_link(links: list[WanLink]) -> WanLink | None:
    return next((link for link in links if link.active), links[0] if links else None)


# --------------------------------------------------------------------------
# extractors


def wan_from(health: list[dict], devices: list[dict]) -> WanStatus:
    """The active WAN, flattened.

    Interface detail comes from whichever link ``wan_links_from`` considers
    active, so this stays correct when the gateway fails over to a backup -
    including one in a non-contiguous slot such as wan3.
    """
    subsystems = index_health(health)
    wan = subsystems.get("wan", {})
    www = subsystems.get("www", {})
    links = wan_links_from(health, devices)
    current = active_link(links)

    status = WanStatus()

    wan_state = wan.get("status") or www.get("status")
    if wan_state:
        status.online = str(wan_state).lower() == "ok"
    elif current is not None:
        status.online = current.up

    status.ip = _first_str(wan, "wan_ip") or (current.ip if current else None) or _first_str(www, "wan_ip")
    status.isp = (
        _first_str(www, "isp_name", "isp_organization")
        or (current.isp if current else None)
    )
    status.uptime_s = (
        _first_num(www, "uptime")
        or _first_num(wan, "uptime")
        or (current.uptime_s if current else None)
    )
    status.gateway_latency_ms = _first_num(www, "latency", "speedtest_ping")

    # Instantaneous rates, if the firmware reports them.
    status.rx_bps = (current.rx_bps if current else None) or _first_num(wan, "rx_bytes-r") or _first_num(www, "rx_bytes-r")
    status.tx_bps = (current.tx_bps if current else None) or _first_num(wan, "tx_bytes-r") or _first_num(www, "tx_bytes-r")

    # Cumulative counters, so the poller can difference them when it has to.
    status.rx_bytes = (current.rx_bytes if current else None) or _first_num(wan, "rx_bytes")
    status.tx_bytes = (current.tx_bytes if current else None) or _first_num(wan, "tx_bytes")

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
