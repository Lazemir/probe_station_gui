"""Telegram notification helpers for the probe station GUI."""

from __future__ import annotations

import asyncio
import ctypes
from io import BytesIO
import json
import logging
import os
import platform
import re
import secrets
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Sequence


logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN_ENV = "PROBE_STATION_TELEGRAM_BOT_TOKEN"
GLOBAL_TELEGRAM_TOKEN_FILENAME = "telegram-bot.json"
START_PAYLOAD_PREFIX = "probe_"
TELEGRAM_PHOTO_CAPTION_LIMIT = 1024

try:  # pragma: no cover - exercised when optional dependency is installed
    from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
except Exception:  # pragma: no cover - keeps the GUI importable without extras
    Bot = None  # type: ignore[assignment]
    InlineKeyboardButton = None  # type: ignore[assignment]
    InlineKeyboardMarkup = None  # type: ignore[assignment]


@dataclass(frozen=True)
class LinkedTelegramChat:
    """A Telegram private chat linked to the local GUI settings."""

    chat_id: str
    chat_title: str
    bot_username: str
    linked_at_utc: str


class TelegramNotificationError(RuntimeError):
    """Raised for user-visible Telegram configuration or API failures."""


@dataclass(frozen=True)
class TelegramBotResponse:
    """A response that the Telegram bot can send back to the linked chat."""

    text: str
    photo_bytes: bytes | None = None
    photo_name: str = "microscope.jpg"
    document_path: str | Path | None = None
    reply_markup: object | None = None
    callback_answer: str = ""


class TelegramBotRequest:
    """A thread-safe request object handed from the bot worker to the GUI."""

    def __init__(
        self,
        *,
        kind: str,
        chat_id: str,
        text: str = "",
        callback_data: str = "",
        callback_query_id: str = "",
    ) -> None:
        self.kind = str(kind)
        self.chat_id = str(chat_id)
        self.text = str(text)
        self.callback_data = str(callback_data)
        self.callback_query_id = str(callback_query_id)
        self.response: TelegramBotResponse | None = None
        self._response_ready = threading.Event()

    def set_response(self, response: TelegramBotResponse | None) -> None:
        self.response = response
        self._response_ready.set()

    def wait_for_response(self, timeout_s: float) -> TelegramBotResponse | None:
        if not self._response_ready.wait(max(0.1, float(timeout_s))):
            return None
        return self.response


class TelegramBotCommandService:
    """Long-poll the linked Telegram chat and dispatch commands to the GUI."""

    def __init__(
        self,
        *,
        bot_token: str,
        chat_id: str,
        request_handler: Callable[[TelegramBotRequest], TelegramBotResponse | None],
        request_timeout_s: float = 15.0,
    ) -> None:
        self._bot_token = str(bot_token or "").strip()
        self._chat_id = str(chat_id or "").strip()
        self._request_handler = request_handler
        self._request_timeout_s = max(1.0, float(request_timeout_s))
        self._stop_requested = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def signature(self) -> tuple[str, str]:
        return self._bot_token, self._chat_id

    def is_running(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        _ensure_dependency()
        if not self._bot_token:
            raise TelegramNotificationError("Telegram bot token is empty.")
        if not self._chat_id:
            raise TelegramNotificationError("Telegram chat is not linked.")
        self._stop_requested.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="TelegramBotCommandService",
            daemon=True,
        )
        self._thread.start()

    def stop(self, timeout_s: float = 3.0) -> None:
        self._stop_requested.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=max(0.0, float(timeout_s)))
        self._thread = None

    def _run(self) -> None:
        while not self._stop_requested.is_set():
            try:
                asyncio.run(self._run_async())
                return
            except Exception:  # pragma: no cover - network dependent
                if self._stop_requested.is_set():
                    return
                logger.exception("Telegram command service crashed; restarting.")
                self._stop_requested.wait(5.0)

    async def _run_async(self) -> None:
        offset: int | None = None
        async with Bot(token=self._bot_token) as bot:  # type: ignore[misc,operator]
            while not self._stop_requested.is_set():
                try:
                    updates = await bot.get_updates(
                        offset=offset,
                        limit=20,
                        timeout=2,
                        allowed_updates=["message", "callback_query"],
                    )
                except Exception as exc:  # pragma: no cover - network dependent
                    if self._stop_requested.is_set():
                        return
                    logger.warning("Telegram command polling failed: %s", exc)
                    await asyncio.sleep(5.0)
                    continue
                for update in updates:
                    update_id = getattr(update, "update_id", None)
                    if isinstance(update_id, int):
                        offset = update_id + 1
                    try:
                        await self._handle_update(bot, update)
                    except Exception:  # pragma: no cover - network dependent
                        if self._stop_requested.is_set():
                            return
                        logger.exception("Telegram command update handling failed.")

    async def _handle_update(self, bot: object, update: object) -> None:
        callback_query = getattr(update, "callback_query", None)
        if callback_query is not None:
            await self._handle_callback_query(bot, callback_query)
            return
        message = getattr(update, "effective_message", None)
        if message is None:
            message = getattr(update, "message", None)
        if message is None:
            return
        text = str(getattr(message, "text", "") or "").strip()
        if not text:
            return
        chat_id = _chat_id_from_message(message)
        if chat_id != self._chat_id:
            return
        request = TelegramBotRequest(kind="message", chat_id=chat_id, text=text)
        logger.info("Telegram command received: %s", _telegram_command_label(text))
        response = self._dispatch_request(request)
        if response is not None:
            await _send_bot_response(bot, chat_id, response)

    async def _handle_callback_query(self, bot: object, callback_query: object) -> None:
        data = str(getattr(callback_query, "data", "") or "").strip()
        if not data:
            return
        message = getattr(callback_query, "message", None)
        chat_id = _chat_id_from_message(message)
        if chat_id != self._chat_id:
            return
        callback_query_id = str(getattr(callback_query, "id", "") or "")
        request = TelegramBotRequest(
            kind="callback",
            chat_id=chat_id,
            callback_data=data,
            callback_query_id=callback_query_id,
        )
        if callback_query_id:
            try:
                # Telegram clients keep inline buttons in a loading state until this
                # answer arrives. A GUI-thread command or photo send can take longer
                # than Telegram's callback timeout, so acknowledge first.
                await bot.answer_callback_query(  # type: ignore[attr-defined]
                    callback_query_id=callback_query_id,
                    text="Accepted.",
                )
            except Exception as exc:  # pragma: no cover - network dependent
                logger.warning("Telegram callback answer failed: %s", exc)
        logger.info("Telegram callback received: %s", data)
        response = self._dispatch_request(request)
        if response is not None and response.text:
            await _send_bot_response(bot, chat_id, response)

    def _dispatch_request(
        self,
        request: TelegramBotRequest,
    ) -> TelegramBotResponse | None:
        try:
            return self._request_handler(request)
        except Exception as exc:  # pragma: no cover - GUI handler dependent
            logger.exception("Telegram command handler failed.")
            return TelegramBotResponse(
                f"Telegram command failed: {exc}",
                callback_answer="Command failed.",
            )


def telegram_dependency_available() -> bool:
    """Return whether the python-telegram-bot package is importable."""

    return Bot is not None


def global_telegram_token_path() -> Path:
    """Return the machine-wide Telegram bot token file path."""

    system = platform.system()
    if system == "Windows":
        base = os.environ.get("PROGRAMDATA")
        if base:
            return Path(base) / "ProbeStationGUI" / GLOBAL_TELEGRAM_TOKEN_FILENAME
        return (
            Path.home()
            / "AppData"
            / "Local"
            / "ProbeStationGUI"
            / GLOBAL_TELEGRAM_TOKEN_FILENAME
        )
    if system == "Darwin":
        return (
            Path("/Library")
            / "Application Support"
            / "ProbeStationGUI"
            / GLOBAL_TELEGRAM_TOKEN_FILENAME
        )
    return Path("/etc") / "probe-station-gui" / GLOBAL_TELEGRAM_TOKEN_FILENAME


def load_global_bot_token() -> str:
    """Load the machine-wide Telegram bot token if configured."""

    path = global_telegram_token_path()
    try:
        with path.open("r", encoding="utf-8-sig") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return ""
    if isinstance(data, dict):
        return str(data.get("bot_token", "") or "").strip()
    if isinstance(data, str):
        return data.strip()
    return ""


def can_manage_global_bot_token() -> bool:
    """Return whether the current process may change the machine-wide token."""

    system = platform.system()
    if system == "Windows":
        try:
            return bool(
                ctypes.windll.shell32.IsUserAnAdmin()  # type: ignore[attr-defined]
            )
        except Exception:
            return False
    geteuid = getattr(os, "geteuid", None)
    if callable(geteuid):
        return geteuid() == 0
    return False


def save_global_bot_token(bot_token: str) -> Path:
    """Save or clear the machine-wide Telegram bot token."""

    if not can_manage_global_bot_token():
        raise PermissionError(
            "Machine-wide Telegram bot token can be changed only by an administrator."
        )
    path = global_telegram_token_path()
    token = str(bot_token or "").strip()
    if not token:
        clear_global_bot_token()
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump({"bot_token": token}, handle, indent=2, ensure_ascii=False)
    _harden_global_bot_token_permissions(path)
    return path


def clear_global_bot_token() -> None:
    """Remove the machine-wide Telegram bot token file if it exists."""

    if not can_manage_global_bot_token():
        raise PermissionError(
            "Machine-wide Telegram bot token can be changed only by an administrator."
        )
    try:
        global_telegram_token_path().unlink()
    except FileNotFoundError:
        return


def _harden_global_bot_token_permissions(path: Path) -> None:
    """Best-effort Windows ACL hardening for the machine-wide token file."""

    if platform.system() != "Windows":
        return
    try:
        subprocess.run(
            [
                "icacls",
                str(path),
                "/inheritance:r",
                "/grant:r",
                "*S-1-5-32-544:F",
                "*S-1-5-18:F",
                "*S-1-5-11:R",
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        logger.warning("Failed to harden Telegram bot token ACL: %s", exc)


def resolved_bot_token(settings: object | None = None) -> str:
    """Return the bot token, preferring env then machine-wide storage."""

    env_value = os.environ.get(TELEGRAM_BOT_TOKEN_ENV, "").strip()
    if env_value:
        return env_value
    global_value = load_global_bot_token()
    if global_value:
        return global_value
    if settings is None:
        return ""
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
    reply_markup: object | None = None,
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
            reply_markup=reply_markup,
        )


async def send_telegram_photo(
    bot_token: str,
    chat_id: str,
    photo_bytes: bytes,
    caption: str,
    *,
    photo_name: str = "microscope.jpg",
    disable_notification: bool = False,
    reply_markup: object | None = None,
) -> None:
    """Send one Telegram photo with a short caption."""

    _ensure_dependency()
    token = str(bot_token or "").strip()
    if not token:
        raise TelegramNotificationError("Telegram bot token is empty.")
    chat_id_text = str(chat_id or "").strip()
    if not chat_id_text:
        raise TelegramNotificationError("Telegram chat is not linked.")
    if not photo_bytes:
        raise TelegramNotificationError("Telegram photo is empty.")
    photo = BytesIO(photo_bytes)
    photo.name = str(photo_name or "microscope.jpg")
    async with Bot(token=token) as bot:  # type: ignore[misc,operator]
        await bot.send_photo(
            chat_id=_telegram_chat_id_value(chat_id_text),
            photo=photo,
            caption=_telegram_photo_caption(caption),
            disable_notification=disable_notification,
            reply_markup=reply_markup,
        )


async def send_telegram_document(
    bot_token: str,
    chat_id: str,
    document_path: str | Path,
    caption: str,
    *,
    disable_notification: bool = False,
    reply_markup: object | None = None,
) -> None:
    """Send one Telegram document with a short caption."""

    _ensure_dependency()
    token = str(bot_token or "").strip()
    if not token:
        raise TelegramNotificationError("Telegram bot token is empty.")
    chat_id_text = str(chat_id or "").strip()
    if not chat_id_text:
        raise TelegramNotificationError("Telegram chat is not linked.")
    path = Path(document_path).expanduser()
    if not path.exists() or not path.is_file():
        raise TelegramNotificationError(f"Telegram document does not exist: {path}")
    with path.open("rb") as document:
        async with Bot(token=token) as bot:  # type: ignore[misc,operator]
            await bot.send_document(
                chat_id=_telegram_chat_id_value(chat_id_text),
                document=document,
                caption=_telegram_photo_caption(caption),
                disable_notification=disable_notification,
                reply_markup=reply_markup,
            )


def send_telegram_message_in_thread(
    *,
    bot_token: str,
    chat_id: str,
    text: str,
    photo_bytes: bytes | None = None,
    photo_name: str = "microscope.jpg",
    document_path: str | Path | None = None,
    reply_markup: object | None = None,
    done_callback: Callable[[bool, str], None] | None = None,
) -> bool:
    """Send a Telegram message, photo, or document on a short-lived daemon thread."""

    if not telegram_dependency_available():
        message = _missing_dependency_message()
        if done_callback is not None:
            done_callback(False, message)
        logger.warning(message)
        return False

    def _worker() -> None:
        try:
            if document_path is not None:
                asyncio.run(
                    send_telegram_document(
                        bot_token,
                        chat_id,
                        document_path,
                        text,
                        reply_markup=reply_markup,
                    )
                )
            elif photo_bytes:
                asyncio.run(
                    send_telegram_photo(
                        bot_token,
                        chat_id,
                        photo_bytes,
                        text,
                        photo_name=photo_name,
                        reply_markup=reply_markup,
                    )
                )
            else:
                asyncio.run(
                    send_telegram_message(
                        bot_token,
                        chat_id,
                        text,
                        reply_markup=reply_markup,
                    )
                )
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


def telegram_inline_keyboard(
    rows: Sequence[Sequence[tuple[str, str]]],
) -> object | None:
    """Build a Telegram inline keyboard from ``(label, callback_data)`` rows."""

    if not telegram_dependency_available():
        return None
    button_rows = [
        [
            InlineKeyboardButton(  # type: ignore[operator]
                str(label),
                callback_data=str(callback_data),
            )
            for label, callback_data in row
        ]
        for row in rows
    ]
    return InlineKeyboardMarkup(button_rows)  # type: ignore[operator]


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


def _telegram_photo_caption(text: str) -> str:
    caption = str(text or "").strip()
    if len(caption) <= TELEGRAM_PHOTO_CAPTION_LIMIT:
        return caption
    return caption[: TELEGRAM_PHOTO_CAPTION_LIMIT - 3].rstrip() + "..."


async def _send_bot_response(
    bot: object,
    chat_id: str,
    response: TelegramBotResponse,
) -> None:
    document_path = (
        Path(response.document_path).expanduser()
        if response.document_path is not None
        else None
    )
    if document_path is not None:
        if not document_path.exists() or not document_path.is_file():
            await bot.send_message(  # type: ignore[attr-defined]
                chat_id=_telegram_chat_id_value(chat_id),
                text=f"Telegram document does not exist: {document_path}",
            )
            return
        with document_path.open("rb") as document:
            await bot.send_document(  # type: ignore[attr-defined]
                chat_id=_telegram_chat_id_value(chat_id),
                document=document,
                caption=_telegram_photo_caption(response.text),
                reply_markup=response.reply_markup,
            )
        return
    if response.photo_bytes:
        photo = BytesIO(response.photo_bytes)
        photo.name = str(response.photo_name or "microscope.jpg")
        await bot.send_photo(  # type: ignore[attr-defined]
            chat_id=_telegram_chat_id_value(chat_id),
            photo=photo,
            caption=_telegram_photo_caption(response.text),
            reply_markup=response.reply_markup,
        )
        return
    await bot.send_message(  # type: ignore[attr-defined]
        chat_id=_telegram_chat_id_value(chat_id),
        text=str(response.text),
        reply_markup=response.reply_markup,
    )


def _chat_id_from_message(message: object) -> str:
    chat = getattr(message, "chat", None)
    return str(getattr(chat, "id", "") or "").strip()


def _telegram_command_label(text: str) -> str:
    stripped = str(text or "").strip()
    if not stripped:
        return "<empty>"
    return stripped.split(maxsplit=1)[0][:80]


def _ensure_dependency() -> None:
    if not telegram_dependency_available():
        raise TelegramNotificationError(_missing_dependency_message())


def _missing_dependency_message() -> str:
    return "python-telegram-bot is not installed."
