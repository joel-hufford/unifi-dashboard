"""Configuration loading.

Values come from a TOML file; secrets may be overridden by the environment so
that a systemd unit can keep them out of a world-readable file.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path

SEARCH_PATHS = (
    Path("config.toml"),
    Path("~/.config/unifi-dashboard/config.toml").expanduser(),
    Path("/etc/unifi-dashboard/config.toml"),
)


@dataclass
class UniFiConfig:
    host: str = "https://192.168.1.1"
    site: str = "default"
    api_key: str = ""
    username: str = ""
    password: str = ""
    verify_ssl: bool | str = False
    timeout: float = 10.0

    @property
    def auth_mode(self) -> str:
        if self.api_key:
            return "api_key"
        if self.username and self.password:
            return "local_admin"
        return "none"


@dataclass
class PingConfig:
    target: str = "8.8.8.8"
    count: int = 3
    interval: float = 0.25
    timeout: float = 1.0


@dataclass
class ServerConfig:
    host: str = "127.0.0.1"
    port: int = 8787


@dataclass
class HistoryConfig:
    window_minutes: int = 60
    retention_minutes: int = 180
    db_path: str = "~/.local/share/unifi-dashboard/history.db"

    def resolved_path(self) -> Path:
        return Path(self.db_path).expanduser()


@dataclass
class WlanConfig:
    weak_signal_dbm: int = -70


@dataclass
class Config:
    poll_interval: float = 10.0
    demo: bool = False
    unifi: UniFiConfig = field(default_factory=UniFiConfig)
    ping: PingConfig = field(default_factory=PingConfig)
    server: ServerConfig = field(default_factory=ServerConfig)
    history: HistoryConfig = field(default_factory=HistoryConfig)
    wlan: WlanConfig = field(default_factory=WlanConfig)

    @classmethod
    def load(cls, path: str | Path | None = None) -> "Config":
        data: dict = {}
        if path is not None:
            data = _read_toml(Path(path).expanduser())
        else:
            for candidate in SEARCH_PATHS:
                if candidate.is_file():
                    data = _read_toml(candidate)
                    break
        cfg = _from_mapping(cls, data)
        cfg._apply_env()
        return cfg

    def _apply_env(self) -> None:
        env = os.environ
        if v := env.get("UNIFI_DASHBOARD_HOST"):
            self.unifi.host = v
        if v := env.get("UNIFI_DASHBOARD_SITE"):
            self.unifi.site = v
        if v := env.get("UNIFI_DASHBOARD_API_KEY"):
            self.unifi.api_key = v
        if v := env.get("UNIFI_DASHBOARD_USERNAME"):
            self.unifi.username = v
        if v := env.get("UNIFI_DASHBOARD_PASSWORD"):
            self.unifi.password = v
        if v := env.get("UNIFI_DASHBOARD_DEMO"):
            self.demo = v.strip().lower() in {"1", "true", "yes", "on"}


def _read_toml(path: Path) -> dict:
    with path.open("rb") as fh:
        return tomllib.load(fh)


def _from_mapping(cls, data: dict):
    """Build a dataclass from a mapping, ignoring unknown keys."""
    kwargs = {}
    for f in fields(cls):
        if f.name not in data:
            continue
        value = data[f.name]
        if is_dataclass(f.type) or (isinstance(value, dict) and _nested_type(cls, f.name)):
            kwargs[f.name] = _from_mapping(_nested_type(cls, f.name), value)
        else:
            kwargs[f.name] = value
    return cls(**kwargs)


_NESTED = {
    "unifi": UniFiConfig,
    "ping": PingConfig,
    "server": ServerConfig,
    "history": HistoryConfig,
    "wlan": WlanConfig,
}


def _nested_type(cls, name):
    return _NESTED.get(name)
