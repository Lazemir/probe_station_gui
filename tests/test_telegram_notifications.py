from __future__ import annotations

from pathlib import Path

from probe_station_gui import telegram_notifications


def test_global_bot_token_store_round_trip(tmp_path, monkeypatch) -> None:
    token_path = tmp_path / "telegram-bot.json"
    monkeypatch.setattr(
        telegram_notifications,
        "global_telegram_token_path",
        lambda: token_path,
    )

    assert telegram_notifications.load_global_bot_token() == ""

    saved_path = telegram_notifications.save_global_bot_token(" 123:abc ")

    assert saved_path == token_path
    assert telegram_notifications.load_global_bot_token() == "123:abc"

    telegram_notifications.clear_global_bot_token()

    assert not token_path.exists()
    assert telegram_notifications.load_global_bot_token() == ""


def test_resolved_bot_token_prefers_env_then_global_then_legacy(
    tmp_path,
    monkeypatch,
) -> None:
    token_path = tmp_path / "telegram-bot.json"
    monkeypatch.setattr(
        telegram_notifications,
        "global_telegram_token_path",
        lambda: token_path,
    )
    monkeypatch.delenv(telegram_notifications.TELEGRAM_BOT_TOKEN_ENV, raising=False)

    class LegacySettings:
        bot_token = "legacy-token"

    assert telegram_notifications.resolved_bot_token(LegacySettings()) == "legacy-token"

    telegram_notifications.save_global_bot_token("global-token")

    assert telegram_notifications.resolved_bot_token(LegacySettings()) == "global-token"

    monkeypatch.setenv(
        telegram_notifications.TELEGRAM_BOT_TOKEN_ENV,
        "env-token",
    )

    assert telegram_notifications.resolved_bot_token(LegacySettings()) == "env-token"
