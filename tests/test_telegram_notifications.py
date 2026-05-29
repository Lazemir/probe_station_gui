from __future__ import annotations

import logging
from pathlib import Path

from probe_station_gui import logging_config
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


def test_logging_redacts_telegram_bot_tokens(tmp_path) -> None:
    log_path = tmp_path / "probe-station-gui.log"
    secret = "123456789:AAExample_token-value_1234567890"

    logging_config.configure_logging(log_path, "DEBUG")
    logging.getLogger("probe_station_gui.test").warning(
        "Telegram URL: %s",
        f"https://api.telegram.org/bot{secret}/sendMessage",
    )
    logging_config._stop_listener()

    text = log_path.read_text(encoding="utf-8")
    assert secret not in text
    assert "bot<redacted-token>" in text


def test_telegram_transport_loggers_stay_at_warning(tmp_path) -> None:
    logging_config.configure_logging(tmp_path / "probe-station-gui.log", "DEBUG")
    try:
        assert logging.getLogger("telegram").level == logging.WARNING
        assert logging.getLogger("httpx").level == logging.WARNING
        assert logging.getLogger("httpcore").level == logging.WARNING
    finally:
        logging_config._stop_listener()
