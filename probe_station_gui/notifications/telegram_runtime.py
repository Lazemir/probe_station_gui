"""Telegram command and route-notification runtime."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable

from PySide6.QtCore import QObject

from probe_station_gui.notifications.telegram import (
    TelegramBotCommandService,
    TelegramBotRequest,
    TelegramBotResponse,
    resolved_bot_token,
    send_telegram_alert_for_settings,
    send_telegram_bot_message_for_settings,
    telegram_inline_keyboard,
)
from probe_station_gui.notifications import telegram_commands
from probe_station_gui.notifications.telegram_commands import TelegramStatusSnapshot
from probe_station_gui.route.telegram_adapter import (
    RouteTelegramPhotoState,
    TelegramPhoto,
)


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TelegramCommandSnapshot:
    route_active: bool
    route_waiting: bool
    runner_available: bool
    photo_enabled: bool
    measure_enabled: bool


class TelegramCommandRuntime(QObject):
    """Own Telegram command handling and notification lifecycle."""

    def __init__(
        self,
        *,
        request_publisher: Callable[[TelegramBotRequest], None],
        command_snapshot_provider: Callable[[], TelegramCommandSnapshot],
        status_snapshot_provider: Callable[[], TelegramStatusSnapshot],
        latest_camera_photo_provider: Callable[[], TelegramPhoto | None],
        route_action_submitter: Callable[[str], None],
        api_route_confirmation_provider: Callable[[], bool],
        settings_provider: Callable[[], object],
    ) -> None:
        super().__init__()
        self._request_publisher = request_publisher
        self._command_snapshot_provider = command_snapshot_provider
        self._status_snapshot_provider = status_snapshot_provider
        self._latest_camera_photo_provider = latest_camera_photo_provider
        self._route_action_submitter = route_action_submitter
        self._api_route_confirmation_provider = api_route_confirmation_provider
        self._settings_provider = settings_provider
        self._bot_service: TelegramBotCommandService | None = None
        self._bot_signature: tuple[str, str] | None = None
        self.route_photos = RouteTelegramPhotoState()

    def configure(self, settings: object) -> None:
        bot_token = resolved_bot_token(settings)
        chat_id = str(getattr(settings, "chat_id", "") or "").strip()
        signature = (bot_token, chat_id)
        should_run = bool(getattr(settings, "enabled", False) and bot_token and chat_id)
        if (
            should_run
            and self._bot_service is not None
            and self._bot_signature == signature
            and self._bot_service.is_running()
        ):
            return
        if self._bot_service is not None:
            self.stop()
        if not should_run:
            return
        try:
            service = TelegramBotCommandService(
                bot_token=bot_token,
                chat_id=chat_id,
                request_handler=self.submit_from_worker,
            )
            service.start()
        except Exception as exc:
            logger.warning("Telegram command bot was not started: %s", exc)
            self._bot_service = None
            self._bot_signature = None
            return
        self._bot_service = service
        self._bot_signature = signature
        logger.info("Telegram command bot started for chat %s.", chat_id)

    def stop(self) -> None:
        service = self._bot_service
        if service is not None:
            service.stop()
        self._bot_service = None
        self._bot_signature = None

    def submit_from_worker(
        self, request: TelegramBotRequest
    ) -> TelegramBotResponse | None:
        self._request_publisher(request)
        response = request.wait_for_response(15.0)
        if response is None:
            return TelegramBotResponse(
                "Telegram command timed out in the GUI thread.",
                callback_answer="Command timed out.",
            )
        return response

    def handle_on_gui(self, request: TelegramBotRequest) -> None:
        try:
            response = self._response_for_request(request)
        except Exception as exc:
            logger.exception("Telegram command failed.")
            response = TelegramBotResponse(
                f"Telegram command failed: {exc}",
                callback_answer="Command failed.",
            )
        request.set_response(response)

    def _response_for_request(
        self,
        request: TelegramBotRequest,
    ) -> TelegramBotResponse | None:
        if request.kind == "callback":
            route = telegram_commands.route_callback(request.callback_data)
        else:
            route = telegram_commands.route_message_command(request.text)
        if route is None:
            return None
        snapshot = self._command_snapshot_provider()
        if route.kind == "status":
            photo = self._latest_camera_photo_provider()
            return TelegramBotResponse(
                telegram_commands.status_text(self._status_snapshot_provider()),
                photo_bytes=photo[0] if photo is not None else None,
                photo_name=photo[1] if photo is not None else "microscope.jpg",
                reply_markup=self.default_markup(route_waiting=snapshot.route_waiting),
                callback_answer="Status sent.",
            )
        if route.kind in {"route_photo", "contact_photo"}:
            plan = (
                telegram_commands.next_route_photo_response(
                    route_active=snapshot.route_active,
                    structure_photos_enabled=snapshot.photo_enabled,
                )
                if route.kind == "route_photo"
                else telegram_commands.next_contact_photo_response(
                    route_active=snapshot.route_active,
                    contact_measurement_enabled=snapshot.measure_enabled,
                )
            )
            if plan.request_photo:
                request_photo = (
                    self.route_photos.request_route_photo
                    if route.kind == "route_photo"
                    else self.route_photos.request_contact_photo
                )
                request_photo()
            return self._text_response(plan.text, plan.callback_answer, snapshot)
        if route.kind == "route_action":
            api_accepts_confirmation = False
            if (
                telegram_commands.is_route_action(route.action)
                and snapshot.route_waiting
                and not snapshot.runner_available
            ):
                api_accepts_confirmation = bool(self._api_route_confirmation_provider())
            plan = telegram_commands.route_action_response(
                route.action,
                route_waiting=snapshot.route_waiting,
                runner_available=snapshot.runner_available,
                api_route_control_accepts_confirmation=api_accepts_confirmation,
            )
            if plan.submit_action is not None:
                self._route_action_submitter(plan.submit_action)
            return self._text_response(plan.text, plan.callback_answer, snapshot)
        return TelegramBotResponse(
            route.text,
            callback_answer=route.callback_answer,
            reply_markup=self.default_markup(route_waiting=snapshot.route_waiting),
        )

    def _text_response(
        self,
        text: str,
        callback_answer: str,
        snapshot: TelegramCommandSnapshot,
    ) -> TelegramBotResponse:
        return TelegramBotResponse(
            text,
            reply_markup=self.default_markup(route_waiting=snapshot.route_waiting),
            callback_answer=callback_answer,
        )

    @staticmethod
    def default_markup(*, route_waiting: bool) -> object | None:
        return telegram_inline_keyboard(
            telegram_commands.default_markup_rows(route_waiting)
        )

    @staticmethod
    def route_actions_markup() -> object | None:
        return telegram_inline_keyboard(telegram_commands.route_action_markup_rows())

    def send_alert(
        self,
        alert_key: str,
        message: str,
        *,
        attach_photo: bool = False,
        photo: TelegramPhoto | None = None,
        document_path: object | None = None,
        reply_markup: object | None = None,
    ) -> None:
        send_telegram_alert_for_settings(
            self._settings_provider(),
            alert_key,
            message,
            attach_photo=attach_photo,
            photo=photo,
            document_path=document_path,
            reply_markup=reply_markup,
            latest_camera_frame_photo=self._latest_camera_photo_provider,
        )

    def send_bot_message(
        self,
        message: str,
        *,
        photo: TelegramPhoto | None = None,
        document_path: object | None = None,
        reply_markup: object | None = None,
    ) -> bool:
        return send_telegram_bot_message_for_settings(
            self._settings_provider(),
            message,
            photo=photo,
            document_path=document_path,
            reply_markup=reply_markup,
        )
