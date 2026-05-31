from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace

from probe_station_gui import logging_config
from probe_station_gui import telegram_notifications


def test_global_bot_token_store_round_trip(tmp_path, monkeypatch) -> None:
    token_path = tmp_path / "telegram-bot.json"
    monkeypatch.setattr(
        telegram_notifications,
        "global_telegram_token_path",
        lambda: token_path,
    )
    monkeypatch.setattr(
        telegram_notifications,
        "can_manage_global_bot_token",
        lambda: True,
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
    monkeypatch.setattr(
        telegram_notifications,
        "can_manage_global_bot_token",
        lambda: True,
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


def test_global_bot_token_store_requires_admin(tmp_path, monkeypatch) -> None:
    token_path = tmp_path / "telegram-bot.json"
    monkeypatch.setattr(
        telegram_notifications,
        "global_telegram_token_path",
        lambda: token_path,
    )
    monkeypatch.setattr(
        telegram_notifications,
        "can_manage_global_bot_token",
        lambda: False,
    )

    try:
        telegram_notifications.save_global_bot_token("123:abc")
    except PermissionError:
        pass
    else:
        raise AssertionError("Expected PermissionError")

    assert not token_path.exists()


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


def test_command_service_continues_after_update_handler_timeout(monkeypatch) -> None:
    class FakeBot:
        def __init__(self) -> None:
            self.polls = 0

        async def __aenter__(self) -> "FakeBot":
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def get_updates(self, **_kwargs: object) -> list[SimpleNamespace]:
            self.polls += 1
            if self.polls == 1:
                return [SimpleNamespace(update_id=10)]
            if self.polls == 2:
                return [SimpleNamespace(update_id=11)]
            raise AssertionError("polling should have stopped")

    fake_bot = FakeBot()
    monkeypatch.setattr(telegram_notifications, "Bot", lambda **_kwargs: fake_bot)
    service = telegram_notifications.TelegramBotCommandService(
        bot_token="token",
        chat_id="123",
        request_handler=lambda _request: None,
    )
    handled: list[int] = []

    async def handle_update(_bot: object, update: object) -> None:
        update_id = int(getattr(update, "update_id"))
        handled.append(update_id)
        if update_id == 10:
            raise TimeoutError("Timed out")
        service._stop_requested.set()

    monkeypatch.setattr(service, "_handle_update", handle_update)

    asyncio.run(service._run_async())

    assert handled == [10, 11]


def test_command_service_reports_running_thread() -> None:
    service = telegram_notifications.TelegramBotCommandService(
        bot_token="token",
        chat_id="123",
        request_handler=lambda _request: None,
    )

    assert not service.is_running()

    service._thread = SimpleNamespace(is_alive=lambda: True)  # type: ignore[assignment]

    assert service.is_running()
