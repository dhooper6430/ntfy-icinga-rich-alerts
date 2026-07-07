"""Normalise the Icinga2 runtime macros (passed as environment variables by the
NotificationCommand) into a single AlertEvent object the rest of the dispatcher uses.

The env var names below are produced by icinga2/ntfy-commands.conf. Host notifications and
service notifications populate different macros; `object_type` selects which.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping

# Host states in Icinga are UP / DOWN; service states are OK / WARNING / CRITICAL / UNKNOWN.
PROBLEM_TYPES = {"PROBLEM"}
RECOVERY_STATES = {"OK", "UP"}

# Icinga's $service.state$/$host.state$ normally render as text, but normalise numeric
# values too so we are robust to either form.
_SERVICE_STATE_NUM = {"0": "OK", "1": "WARNING", "2": "CRITICAL", "3": "UNKNOWN"}
_HOST_STATE_NUM = {"0": "UP", "1": "DOWN"}


def _norm_state(object_type: str, value: str) -> str:
    value = (value or "").strip()
    table = _SERVICE_STATE_NUM if object_type == "service" else _HOST_STATE_NUM
    return table.get(value, value.upper())


@dataclass
class AlertEvent:
    object_type: str           # "host" | "service"
    notification_type: str     # PROBLEM | RECOVERY | ACKNOWLEDGEMENT | CUSTOM | FLAPPING* | DOWNTIME*
    host_name: str
    host_display: str
    host_address: str
    state: str                 # effective state (service state for services, host state for hosts)
    output: str                # effective plugin output
    display: str               # effective display name (service display, or host display)
    service_name: str = ""
    service_display: str = ""
    check_command: str = ""
    long_date_time: str = ""
    author: str = ""
    comment: str = ""
    ntfy_topic: str = ""
    user_name: str = ""
    extra: Mapping[str, str] = field(default_factory=dict)

    @property
    def is_service(self) -> bool:
        return self.object_type == "service"

    @property
    def key(self) -> str:
        """Stable suppression key: host for host alerts, host!service for service alerts."""
        return f"{self.host_name}!{self.service_name}" if self.is_service else self.host_name

    @property
    def is_problem(self) -> bool:
        return self.notification_type in PROBLEM_TYPES

    @property
    def is_recovery(self) -> bool:
        return self.notification_type == "RECOVERY" or self.state in RECOVERY_STATES

    @classmethod
    def from_env(cls, object_type: str, environ: Mapping[str, str]) -> "AlertEvent":
        g = lambda k, d="": environ.get(k, d).strip()  # noqa: E731
        host_name = g("HOSTNAME")
        host_display = g("HOSTDISPLAYNAME") or host_name
        common = dict(
            object_type=object_type,
            notification_type=g("NOTIFICATIONTYPE", "PROBLEM").upper(),
            host_name=host_name,
            host_display=host_display,
            host_address=g("HOSTADDRESS"),
            long_date_time=g("LONGDATETIME"),
            author=g("NOTIFICATIONAUTHORNAME"),
            comment=g("NOTIFICATIONCOMMENT"),
            ntfy_topic=g("NTFY_TOPIC"),
            user_name=g("NTFY_USERNAME"),
        )
        if object_type == "service":
            service_name = g("SERVICENAME")
            return cls(
                state=_norm_state("service", g("SERVICESTATE", "UNKNOWN")),
                output=g("SERVICEOUTPUT"),
                display=g("SERVICEDISPLAYNAME") or service_name,
                service_name=service_name,
                service_display=g("SERVICEDISPLAYNAME") or service_name,
                check_command=g("SERVICECHECKCOMMAND"),
                **common,
            )
        return cls(
            state=_norm_state("host", g("HOSTSTATE", "DOWN")),
            output=g("HOSTOUTPUT"),
            display=host_display,
            check_command=g("HOSTCHECKCOMMAND"),
            **common,
        )
