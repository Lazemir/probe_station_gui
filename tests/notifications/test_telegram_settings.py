from __future__ import annotations

from probe_station_gui.notifications.telegram_settings import (
    TELEGRAM_ALERT_TYPES,
    TelegramSettings,
    default_telegram_alerts,
    parse_telegram_alerts,
)


def test_telegram_settings_round_trip_omits_bot_token() -> None:
    settings = TelegramSettings(
        enabled=True,
        bot_token="123:abc",
        bot_username="probe_station_bot",
        chat_id="456",
        chat_title="Lab User",
        linked_at_utc="2026-05-28T12:00:00+00:00",
        alerts={"route_attention": False},
    )

    serialized = settings.to_dict()
    restored = TelegramSettings(**serialized)

    assert "bot_token" not in serialized
    assert restored.bot_token == ""
    assert restored.enabled is True
    assert restored.bot_username == "probe_station_bot"
    assert restored.alerts == {"route_attention": False}


def test_default_telegram_alerts_cover_declared_types() -> None:
    assert set(default_telegram_alerts()) == {
        key for key, _label in TELEGRAM_ALERT_TYPES
    }
    assert all(default_telegram_alerts().values())


def test_parse_telegram_alerts_preserves_legacy_bool_rules() -> None:
    parsed = parse_telegram_alerts(
        {
            "route_attention": "false",
            "route_started": "",
            "route_completed": "yes",
            "unknown": False,
        }
    )

    assert parsed["route_attention"] is False
    assert parsed["route_started"] is False
    assert parsed["route_completed"] is True
    assert "unknown" not in parsed
