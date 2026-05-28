"""Telegram notification helpers for the probe station GUI."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import secrets
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable


logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN_ENV = "PROBE_STATION_TELEGRAM_BOT_TOKEN"
START_PAYLOAD_PREFIX = "probe_"

try:  # pragma: no cover - exercised when optional dependency is installed
    from telegram import Bot
except Exception:  # pragma: no cover - keeps the GUI importable without extras
    Bot = None  # type: ignore[assignment]


@dataclass(frozen=True)
class LinkedTelegramChat:
    """A Telegram private chat linked to the local GUI settings."""

    chat_id: str
    chat_title: str
    bot_username: str
    linked_at_utc: str


class TelegramNotificationError(RuntimeError):
    """Raised for user-visible Telegram configuration or API failures."""


def telegram_dependency_available() -> bool:
    """Return whether the python-telegram-bot package is importable."""

    return Bot is not None


def resolved_bot_token(settings: object) -> str:
    """Return the bot token, preferring the environment override."""

    env_value = os.environ.get(TELEGRAM_BOT_TOKEN_ENV, "").strip()
    if env_value:
        return env_value
    return str(getattr(settings, "bot_token", "") or "").strip()


def create_start_payload() -> str:
    """Create a Telegram deep-link payload within Bot API constraints."""

    return f"{START_PAYLOAD_PREFIX}{secrets.token_urlsafe(18)}"


def telegram_deep_link(bot_username: str, payload: str) -> str:
    """Build a t.me deep link for a private bot chat."""

    username = str(bot_username or "").strip().lstrip("@")
    if not username:
        raise TelegramNotificationError("Telegram bot username is unavailable.")
    return f"https://t.me/{username}?start={payload}"


async def prepare_telegram_link(bot_token: str, payload: str) -> tuple[str, str]:
    """Validate the bot token and return ``(bot_username, deep_link)``."""

    _ensure_dependency()
    token = str(bot_token or "").strip()
    if not token:
        raise TelegramNotificationError("Telegram bot token is empty.")
    async with Bot(token=token) as bot:  # type: ignore[misc,operator]
        me = await bot.get_me()
    username = str(getattr(me, "username", "") or "").strip()
    if not username:
        raise TelegramNotificationError("Telegram bot has no public username.")
    return username, telegram_deep_link(username, payload)


async def wait_for_telegram_link(
    bot_token: str,
    payload: str,
    *,
    timeout_s: float = 120.0,
    poll_timeout_s: float = 4.0,
    should_cancel: Callable[[], bool] | None = None,
) -> LinkedTelegramChat:
    """Wait until a private chat sends ``/start <payload>`` to the bot."""

    _ensure_dependency()
    token = str(bot_token or "").strip()
    if not token:
        raise TelegramNotificationError("Telegram bot token is empty.")
    deadline = time.monotonic() + max(1.0, float(timeout_s))
    offset: int | None = None
    async with Bot(token=token) as bot:  # type: ignore[misc,operator]
        me = await bot.get_me()
        bot_username = str(getattr(me, "username", "") or "").strip()
        while time.monotonic() < deadline:
            if should_cancel is not None and should_cancel():
                raise TelegramNotificationError("Telegram link cancelled.")
            remaining = max(1.0, deadline - time.monotonic())
            updates = await bot.get_updates(
                offset=offset,
                limit=100,
                timeout=int(min(max(1.0, poll_timeout_s), remaining)),
                allowed_updates=["message"],
            )
            for update in updates:
                if should_cancel is not None and should_cancel():
                    raise TelegramNotificationError("Telegram link cancelled.")
                update_id = getattr(update, "update_id", None)
                if isinstance(update_id, int):
                    offset = update_id + 1
                linked = _linked_chat_from_update(update, payload, bot_username)
                if linked is None:
                    continue
                await bot.send_message(
                    chat_id=_telegram_chat_id_value(linked.chat_id),
                    text="Probe Station GUI notifications are linked.",
                )
                return linked
    raise TelegramNotificationError("Telegram link timed out.")


async def send_telegram_message(
    bot_token: str,
    chat_id: str,
    text: str,
    *,
    disable_notification: bool = False,
) -> None:
    """Send one Telegram text message."""

    _ensure_dependency()
    token = str(bot_token or "").strip()
    if not token:
        raise TelegramNotificationError("Telegram bot token is empty.")
    chat_id_text = str(chat_id or "").strip()
    if not chat_id_text:
        raise TelegramNotificationError("Telegram chat is not linked.")
    async with Bot(token=token) as bot:  # type: ignore[misc,operator]
        await bot.send_message(
            chat_id=_telegram_chat_id_value(chat_id_text),
            text=str(text),
            disable_notification=disable_notification,
        )


def send_telegram_message_in_thread(
    *,
    bot_token: str,
    chat_id: str,
    text: str,
    done_callback: Callable[[bool, str], None] | None = None,
) -> bool:
    """Send a Telegram message on a short-lived daemon thread."""

    if not telegram_dependency_available():
        message = _missing_dependency_message()
        if done_callback is not None:
            done_callback(False, message)
        logger.warning(message)
        return False

    def _worker() -> None:
        try:
            asyncio.run(send_telegram_message(bot_token, chat_id, text))
        except Exception as exc:  # pragma: no cover - network dependent
            logger.warning("Telegram notification failed: %s", exc)
            if done_callback is not None:
                done_callback(False, str(exc))
        else:
            if done_callback is not None:
                done_callback(True, "Telegram notification sent.")

    thread = threading.Thread(
        target=_worker,
        name="TelegramNotification",
        daemon=True,
    )
    thread.start()
    return True


def _linked_chat_from_update(
    update: object,
    expected_payload: str,
    bot_username: str,
) -> LinkedTelegramChat | None:
    message = getattr(update, "effective_message", None)
    if message is None:
        message = getattr(update, "message", None)
    text = str(getattr(message, "text", "") or "").strip()
    if not _start_payload_matches(text, expected_payload):
        return None
    chat = getattr(message, "chat", None)
    if str(getattr(chat, "type", "") or "").lower() != "private":
        return None
    chat_id = str(getattr(chat, "id", "") or "").strip()
    if not chat_id:
        return None
    user = getattr(message, "from_user", None)
    title = _private_chat_title(user)
    return LinkedTelegramChat(
        chat_id=chat_id,
        chat_title=title,
        bot_username=bot_username,
        linked_at_utc=datetime.now(timezone.utc).isoformat(),
    )


def _start_payload_matches(text: str, expected_payload: str) -> bool:
    if not text:
        return False
    parts = text.split(maxsplit=1)
    command = parts[0].strip().lower()
    if not command.startswith("/start"):
        return False
    if len(parts) < 2:
        return False
    payload = parts[1].strip()
    return payload == expected_payload


def _private_chat_title(user: object) -> str:
    first_name = str(getattr(user, "first_name", "") or "").strip()
    last_name = str(getattr(user, "last_name", "") or "").strip()
    username = str(getattr(user, "username", "") or "").strip()
    name = " ".join(part for part in (first_name, last_name) if part).strip()
    if username:
        suffix = f"@{username}"
        return f"{name} ({suffix})" if name else suffix
    return name or "Telegram user"


def _telegram_chat_id_value(chat_id: str) -> int | str:
    chat_id_text = str(chat_id).strip()
    if re.fullmatch(r"-?\d+", chat_id_text):
        return int(chat_id_text)
    return chat_id_text


def _ensure_dependency() -> None:
    if not telegram_dependency_available():
        raise TelegramNotificationError(_missing_dependency_message())


def _missing_dependency_message() -> str:
    return "python-telegram-bot is not installed."
