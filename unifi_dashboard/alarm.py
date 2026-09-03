"""Turning a snapshot into one severity the screen can shout with.

The dashboard hangs on a wall and is read from across the room, so the most
important thing it renders is not a number - it is whether anything is wrong.
That decision lives here, in one place, so it is testable and so the rules are
visible rather than scattered through the view.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .config import AlarmConfig

OK = "ok"
WARNING = "warning"
CRITICAL = "critical"

_RANK = {OK: 0, WARNING: 1, CRITICAL: 2}


@dataclass
class Alarm:
    level: str = OK
    reasons: list[str] = field(default_factory=list)

    def raise_to(self, level: str, reason: str) -> None:
        self.reasons.append(reason)
        if _RANK[level] > _RANK[self.level]:
            self.level = level

    @property
    def headline(self) -> str | None:
        return self.reasons[0] if self.reasons else None


def _temperature_label(celsius: float, unit: str) -> str:
    """Match the unit the panel is displaying, so the banner and the readout
    below it do not disagree about how hot the gateway is."""
    if unit.upper() == "F":
        return f"{round(celsius * 9 / 5 + 32)}\u00b0F"
    return f"{round(celsius)}\u00b0C"


def evaluate(
    cfg: AlarmConfig,
    *,
    controller_ok: bool,
    wan_up: bool | None,
    internet_reachable: bool | None,
    dns_ok: bool | None,
    loss_pct: float | None,
    latency_ms: float | None,
    on_backup: bool = False,
    temperature_c: float | None = None,
    overheating: bool = False,
    temp_warning_c: float | None = None,
    temp_critical_c: float | None = None,
    temperature_unit: str = "C",
) -> Alarm:
    """Order matters: the first reason recorded becomes the headline, so the
    most fundamental failure is checked first."""
    alarm = Alarm()

    if not controller_ok:
        alarm.raise_to(WARNING, "Controller unreachable")

    if wan_up is False:
        alarm.raise_to(CRITICAL, "WAN is down")
    if internet_reachable is False:
        alarm.raise_to(CRITICAL, "No route to the internet")
    if dns_ok is False:
        alarm.raise_to(CRITICAL, "DNS is not resolving")

    # The gateway's own heat. Not a WAN fault, but the frame exists to make
    # someone walk to the rack, and a gateway cooking itself is worth the walk
    # - often before it becomes a WAN fault a few minutes later.
    if overheating:
        alarm.raise_to(CRITICAL, "Gateway overheating")
    elif temperature_c is not None:
        reading = _temperature_label(temperature_c, temperature_unit)
        if temp_critical_c is not None and temperature_c >= temp_critical_c:
            alarm.raise_to(CRITICAL, f"Gateway at {reading}")
        elif temp_warning_c is not None and temperature_c >= temp_warning_c:
            alarm.raise_to(WARNING, f"Gateway at {reading}")

    if loss_pct is not None:
        if loss_pct >= cfg.loss_pct_critical:
            alarm.raise_to(CRITICAL, f"Packet loss {loss_pct:.0f}%")
        elif loss_pct >= cfg.loss_pct_warning:
            alarm.raise_to(WARNING, f"Packet loss {loss_pct:.1f}%")

    if latency_ms is not None:
        if latency_ms >= cfg.latency_ms_critical:
            alarm.raise_to(CRITICAL, f"Latency {latency_ms:.0f} ms")
        elif latency_ms >= cfg.latency_ms_warning:
            alarm.raise_to(WARNING, f"Latency {latency_ms:.0f} ms")

    if on_backup:
        alarm.raise_to(
            CRITICAL if cfg.failover_is_critical else WARNING,
            "Running on the backup WAN",
        )

    return alarm
