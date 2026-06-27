from __future__ import annotations

import sys
from types import SimpleNamespace


def _restore_real_imports_for_main() -> None:
    for name in list(sys.modules):
        if name == "PySide6" or name.startswith("PySide6."):
            del sys.modules[name]
    serial_module = sys.modules.get("serial")
    if serial_module is not None and not hasattr(serial_module, "__path__"):
        for name in list(sys.modules):
            if name == "serial" or name.startswith("serial."):
                del sys.modules[name]
    package = sys.modules.get("probe_station_gui")
    if package is not None and not hasattr(package, "__path__"):
        for name in list(sys.modules):
            if name == "probe_station_gui" or name.startswith("probe_station_gui."):
                del sys.modules[name]


_restore_real_imports_for_main()

from main import Main
from probe_station_gui.notifications import telegram_commands
from probe_station_gui.notifications.telegram import TelegramBotRequest


class Alive:
    def is_alive(self) -> bool:
        return True


class CountingTelegramAdapter:
    def __init__(self) -> None:
        self.route_photo_requests = 0
        self.contact_photo_requests = 0

    def request_route_photo(self) -> None:
        self.route_photo_requests += 1

    def request_contact_photo(self) -> None:
        self.contact_photo_requests += 1


def test_handle_telegram_bot_request_help_returns_markup_and_callback_answer() -> None:
    window = Main.__new__(Main)
    window._route_measurement_waiting = False
    window._telegram_default_markup = lambda: "markup"
    request = TelegramBotRequest(kind="message", chat_id="1", text="/help")

    response = Main._handle_telegram_bot_request(window, request)

    assert response.text == telegram_commands.help_text()
    assert response.reply_markup == "markup"
    assert response.callback_answer == "Commands."


def test_handle_telegram_callback_unknown_returns_default_response() -> None:
    window = Main.__new__(Main)
    window._route_measurement_waiting = False
    window._telegram_default_markup = lambda: "markup"

    response = Main._handle_telegram_callback(window, "unknown")

    assert response.text == "Unknown Telegram action."
    assert response.callback_answer == "Unknown action."
    assert response.reply_markup == "markup"


def test_telegram_request_next_route_photo_response_requests_once_on_success() -> None:
    window = Main.__new__(Main)
    adapter = CountingTelegramAdapter()
    window._route_measurement_thread = Alive()
    window._route_measurement_photo_enabled = True
    window._telegram_default_markup = lambda: "markup"
    window._route_telegram_adapter = lambda: adapter

    response = Main._telegram_request_next_route_photo_response(window)

    assert adapter.route_photo_requests == 1
    assert adapter.contact_photo_requests == 0
    assert response.text == "The next route structure photo will be sent here."
    assert response.callback_answer == "Waiting for route photo."
    assert response.reply_markup == "markup"


def test_telegram_request_next_contact_photo_response_requests_once_on_success() -> None:
    window = Main.__new__(Main)
    adapter = CountingTelegramAdapter()
    window._route_measurement_thread = Alive()
    window._route_measurement_measure_enabled = True
    window._telegram_default_markup = lambda: "markup"
    window._route_telegram_adapter = lambda: adapter

    response = Main._telegram_request_next_contact_photo_response(window)

    assert adapter.route_photo_requests == 0
    assert adapter.contact_photo_requests == 1
    assert response.text == "The next route contact attempt photo will be sent here."
    assert response.callback_answer == "Waiting for contact photo."
    assert response.reply_markup == "markup"


def test_telegram_route_action_response_submits_runner_confirmation() -> None:
    window = Main.__new__(Main)
    submitted: list[str] = []
    window._route_measurement_waiting = True
    window._route_measurement_runner = object()
    window._telegram_default_markup = lambda: "markup"
    window._submit_route_measurement_confirmation = submitted.append

    response = Main._telegram_route_action_response(window, "measure")

    assert submitted == ["measure"]
    assert response.text == "Route measurement action submitted: measure."
    assert response.callback_answer == "measure submitted."
    assert response.reply_markup == "markup"


def test_telegram_route_action_response_submits_api_route_control_confirmation() -> None:
    window = Main.__new__(Main)
    submitted: list[str] = []
    window._route_measurement_waiting = True
    window._route_measurement_runner = None
    window._telegram_default_markup = lambda: "markup"
    window._submit_route_measurement_confirmation = submitted.append
    window._api_route_control_state_snapshot = lambda: SimpleNamespace(
        accepts_route_confirmation=True
    )

    response = Main._telegram_route_action_response(window, "skip")

    assert submitted == ["skip"]
    assert response.text == "API route control action submitted: skip."
    assert response.callback_answer == "skip submitted."
    assert response.reply_markup == "markup"


def test_telegram_status_response_attaches_latest_camera_photo() -> None:
    window = Main.__new__(Main)
    window._latest_camera_frame_photo = lambda: (b"jpeg", "latest.jpg")
    window._telegram_status_text = lambda: "status text"
    window._telegram_default_markup = lambda: "markup"

    response = Main._telegram_status_response(window)

    assert response.text == "status text"
    assert response.photo_bytes == b"jpeg"
    assert response.photo_name == "latest.jpg"
    assert response.reply_markup == "markup"
    assert response.callback_answer == "Status sent."
