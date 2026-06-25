"""Telegram notification settings and alert parsing."""

from __future__ import annotations

from dataclasses import dataclass, field

from probe_station_gui.settings_value_parsing import coerce_bool


TELEGRAM_ALERT_TYPES: tuple[tuple[str, str], ...] = (
    ("route_attention", "Route needs attention"),
    ("route_started", "Route measurement started"),
    ("route_completed", "Route measurement complete"),
    ("route_failed", "Route measurement stopped or failed"),
    ("contact_seek_failed", "Contact seek failed"),
    ("camera_error", "Camera error"),
)


def default_telegram_alerts() -> dict[str, bool]:
    """Return default Telegram alert selections."""

    return {key: True for key, _label in TELEGRAM_ALERT_TYPES}


def parse_telegram_alerts(raw_alerts: object) -> dict[str, bool]:
    alerts = default_telegram_alerts()
    if isinstance(raw_alerts, dict):
        for key, _label in TELEGRAM_ALERT_TYPES:
            if key in raw_alerts:
                alerts[key] = coerce_bool(
                    raw_alerts.get(key),
                    default=alerts[key],
                )
    return alerts


@dataclass
class TelegramSettings:
    """Configuration for Telegram notifications."""

    enabled: bool = False
    bot_token: str = ""
    bot_username: str = ""
    chat_id: str = ""
    chat_title: str = ""
    linked_at_utc: str = ""
    alerts: dict[str, bool] = field(default_factory=default_telegram_alerts)

    def clone(self) -> "TelegramSettings":
        """Return a copy of the Telegram notification preferences."""

        return TelegramSettings(
            enabled=self.enabled,
            bot_token=self.bot_token,
            bot_username=self.bot_username,
            chat_id=self.chat_id,
            chat_title=self.chat_title,
            linked_at_utc=self.linked_at_utc,
            alerts=dict(self.alerts),
        )

    def to_dict(self) -> dict[str, bool | str | dict[str, bool]]:
        """Serialize Telegram preferences without the machine-wide bot token."""

        return {
            "enabled": self.enabled,
            "bot_username": self.bot_username,
            "chat_id": self.chat_id,
            "chat_title": self.chat_title,
            "linked_at_utc": self.linked_at_utc,
            "alerts": dict(self.alerts),
        }

    def alert_enabled(self, alert_key: str) -> bool:
        """Return whether a notification type is enabled."""

        defaults = default_telegram_alerts()
        return bool(self.alerts.get(alert_key, defaults.get(alert_key, False)))
