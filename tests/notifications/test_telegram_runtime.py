from __future__ import annotations

from dataclasses import FrozenInstanceError, replace

import pytest
from PySide6.QtCore import QObject

from probe_station_gui.notifications import telegram_runtime
from probe_station_gui.notifications.telegram import TelegramBotRequest
from probe_station_gui.notifications.telegram_commands import TelegramStatusSnapshot
from probe_station_gui.notifications.telegram_runtime import (
    TelegramCommandRuntime,
    TelegramCommandSnapshot,
)
from probe_station_gui.notifications.telegram_settings import TelegramSettings


def _status_snapshot() -> TelegramStatusSnapshot:
    return TelegramStatusSnapshot(
        latest_status_message="idle",
        route_thread_active=False,
        route_waiting=False,
        route_session_active=False,
        route_current_point=None,
        api_route_control_status_text="",
        stage_status={},
        microscope_scan_active=False,
        contact_seek_active=False,
        camera_frame_available=False,
        stage_axis_names=("X", "Y"),
    )


def _command_snapshot(**changes: object) -> TelegramCommandSnapshot:
    snapshot = TelegramCommandSnapshot(
        route_active=False,
        route_waiting=False,
        runner_available=False,
        photo_enabled=False,
        measure_enabled=False,
    )
    return replace(snapshot, **changes)


def _runtime(
    *,
    request_publisher=lambda _request: None,
    command_snapshot_provider=lambda: _command_snapshot(),
    status_snapshot_provider=_status_snapshot,
    latest_camera_photo_provider=lambda: None,
    route_action_submitter=lambda _action: None,
    api_route_confirmation_provider=lambda: False,
    settings_provider=TelegramSettings,
) -> TelegramCommandRuntime:
    return TelegramCommandRuntime(
        request_publisher=request_publisher,
        command_snapshot_provider=command_snapshot_provider,
        status_snapshot_provider=status_snapshot_provider,
        latest_camera_photo_provider=latest_camera_photo_provider,
        route_action_submitter=route_action_submitter,
        api_route_confirmation_provider=api_route_confirmation_provider,
        settings_provider=settings_provider,
    )


def test_telegram_command_runtime_is_the_canonical_qobject_owner() -> None:
    assert issubclass(TelegramCommandRuntime, QObject)


def test_telegram_command_snapshot_is_immutable() -> None:
    snapshot = TelegramCommandSnapshot(
        route_active=False,
        route_waiting=False,
        runner_available=False,
        photo_enabled=False,
        measure_enabled=False,
    )

    with pytest.raises(FrozenInstanceError):
        snapshot.route_waiting = True


def test_stop_retries_same_service_after_stop_failure(monkeypatch) -> None:
    stops: list[str] = []

    class Service:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def start(self) -> None:
            pass

        def is_running(self) -> bool:
            return True

        def stop(self) -> None:
            stops.append("stop")
            if len(stops) == 1:
                raise RuntimeError("stop failed")

    monkeypatch.setattr(telegram_runtime, "TelegramBotCommandService", Service)
    monkeypatch.setattr(
        telegram_runtime, "resolved_bot_token", lambda settings: settings.bot_token
    )
    runtime = _runtime()
    runtime.configure(TelegramSettings(enabled=True, bot_token="token", chat_id="42"))

    with pytest.raises(RuntimeError, match="stop failed"):
        runtime.stop()
    runtime.stop()

    assert stops == ["stop", "stop"]


def test_configure_starts_once_for_the_same_running_signature(monkeypatch) -> None:
    services: list[object] = []

    class Service:
        def __init__(self, **kwargs: object) -> None:
            self.kwargs = kwargs
            self.started = 0
            self.running = False
            services.append(self)

        def start(self) -> None:
            self.started += 1
            self.running = True

        def is_running(self) -> bool:
            return self.running

        def stop(self) -> None:
            self.running = False

    monkeypatch.setattr(telegram_runtime, "TelegramBotCommandService", Service)
    monkeypatch.setattr(
        telegram_runtime,
        "resolved_bot_token",
        lambda settings: settings.bot_token,
    )
    runtime = _runtime()
    settings = TelegramSettings(enabled=True, bot_token="token", chat_id="42")

    runtime.configure(settings)
    runtime.configure(settings)

    assert len(services) == 1
    assert services[0].started == 1
    assert services[0].kwargs == {
        "bot_token": "token",
        "chat_id": "42",
        "request_handler": runtime.submit_from_worker,
    }


def test_configure_restarts_a_dead_same_signature_service(monkeypatch) -> None:
    events: list[str] = []
    services: list[object] = []

    class Service:
        def __init__(self, **_kwargs: object) -> None:
            self.running = False
            self.name = f"service-{len(services) + 1}"
            services.append(self)

        def start(self) -> None:
            events.append(f"start:{self.name}")
            self.running = True

        def is_running(self) -> bool:
            return self.running

        def stop(self) -> None:
            events.append(f"stop:{self.name}")
            self.running = False

    monkeypatch.setattr(telegram_runtime, "TelegramBotCommandService", Service)
    monkeypatch.setattr(
        telegram_runtime, "resolved_bot_token", lambda settings: settings.bot_token
    )
    runtime = _runtime()
    settings = TelegramSettings(enabled=True, bot_token="token", chat_id="42")

    runtime.configure(settings)
    services[0].running = False
    runtime.configure(settings)

    assert events == ["start:service-1", "stop:service-1", "start:service-2"]


def test_configure_stops_replaced_service_before_starting_new_one(monkeypatch) -> None:
    events: list[str] = []

    class Service:
        def __init__(self, *, chat_id: str, **_kwargs: object) -> None:
            self.chat_id = chat_id

        def start(self) -> None:
            events.append(f"start:{self.chat_id}")

        def is_running(self) -> bool:
            return True

        def stop(self) -> None:
            events.append(f"stop:{self.chat_id}")

    monkeypatch.setattr(telegram_runtime, "TelegramBotCommandService", Service)
    monkeypatch.setattr(
        telegram_runtime, "resolved_bot_token", lambda settings: settings.bot_token
    )
    runtime = _runtime()

    runtime.configure(TelegramSettings(enabled=True, bot_token="one", chat_id="1"))
    runtime.configure(TelegramSettings(enabled=True, bot_token="two", chat_id="2"))

    assert events == ["start:1", "stop:1", "start:2"]


def test_disabled_or_invalid_configuration_stops_and_clears(monkeypatch) -> None:
    events: list[str] = []

    class Service:
        def __init__(self, *, chat_id: str, **_kwargs: object) -> None:
            self.chat_id = chat_id

        def start(self) -> None:
            events.append(f"start:{self.chat_id}")

        def is_running(self) -> bool:
            return True

        def stop(self) -> None:
            events.append(f"stop:{self.chat_id}")

    monkeypatch.setattr(telegram_runtime, "TelegramBotCommandService", Service)
    monkeypatch.setattr(
        telegram_runtime, "resolved_bot_token", lambda settings: settings.bot_token
    )
    runtime = _runtime()
    enabled = TelegramSettings(enabled=True, bot_token="token", chat_id="42")

    runtime.configure(enabled)
    runtime.configure(TelegramSettings(enabled=False, bot_token="token", chat_id="42"))
    runtime.configure(enabled)
    runtime.configure(TelegramSettings(enabled=True, bot_token="", chat_id="42"))
    runtime.configure(enabled)

    assert events == [
        "start:42",
        "stop:42",
        "start:42",
        "stop:42",
        "start:42",
    ]


def test_service_start_failure_leaves_runtime_reconfigurable(monkeypatch) -> None:
    events: list[str] = []
    attempts = 0

    class Service:
        def __init__(self, **_kwargs: object) -> None:
            nonlocal attempts
            attempts += 1
            self.attempt = attempts

        def start(self) -> None:
            events.append(f"start:{self.attempt}")
            if self.attempt == 1:
                raise RuntimeError("start failed")

        def is_running(self) -> bool:
            return True

        def stop(self) -> None:
            events.append(f"stop:{self.attempt}")

    monkeypatch.setattr(telegram_runtime, "TelegramBotCommandService", Service)
    monkeypatch.setattr(
        telegram_runtime, "resolved_bot_token", lambda settings: settings.bot_token
    )
    runtime = _runtime()
    settings = TelegramSettings(enabled=True, bot_token="token", chat_id="42")

    runtime.configure(settings)
    runtime.configure(settings)

    assert events == ["start:1", "start:2"]


def test_service_construction_failure_leaves_runtime_reconfigurable(
    monkeypatch,
) -> None:
    events: list[str] = []
    attempts = 0

    class Service:
        def __init__(self, **_kwargs: object) -> None:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("construction failed")

        def start(self) -> None:
            events.append("start")

        def is_running(self) -> bool:
            return True

        def stop(self) -> None:
            events.append("stop")

    monkeypatch.setattr(telegram_runtime, "TelegramBotCommandService", Service)
    monkeypatch.setattr(
        telegram_runtime, "resolved_bot_token", lambda settings: settings.bot_token
    )
    runtime = _runtime()
    settings = TelegramSettings(enabled=True, bot_token="token", chat_id="42")

    runtime.configure(settings)
    runtime.configure(settings)

    assert attempts == 2
    assert events == ["start"]


def test_submit_from_worker_publishes_once_and_waits_fifteen_seconds() -> None:
    published: list[object] = []
    response = object()

    class Request:
        def __init__(self) -> None:
            self.waits: list[float] = []

        def wait_for_response(self, timeout_s: float) -> object:
            self.waits.append(timeout_s)
            return response

    request = Request()
    runtime = _runtime(request_publisher=published.append)

    result = runtime.submit_from_worker(request)

    assert published == [request]
    assert request.waits == [15.0]
    assert result is response


def test_submit_from_worker_returns_existing_timeout_response_on_miss() -> None:
    class Request:
        def wait_for_response(self, _timeout_s: float) -> None:
            return None

    response = _runtime().submit_from_worker(Request())

    assert response is not None
    assert response.text == "Telegram command timed out in the GUI thread."
    assert response.callback_answer == "Command timed out."


def test_handle_on_gui_normalizes_errors_and_sets_one_response() -> None:
    responses: list[object] = []

    class Request:
        @property
        def kind(self) -> str:
            raise RuntimeError("boom")

        def set_response(self, response: object) -> None:
            responses.append(response)

    _runtime().handle_on_gui(Request())

    assert len(responses) == 1
    response = responses[0]
    assert response.text == "Telegram command failed: boom"
    assert response.callback_answer == "Command failed."


def test_handle_on_gui_uses_one_snapshot_for_help_response(monkeypatch) -> None:
    snapshots: list[str] = []
    snapshot = _command_snapshot()
    monkeypatch.setattr(telegram_runtime, "telegram_inline_keyboard", lambda rows: rows)

    def provide_snapshot() -> TelegramCommandSnapshot:
        snapshots.append("snapshot")
        return snapshot

    runtime = _runtime(command_snapshot_provider=provide_snapshot)
    request = TelegramBotRequest(kind="message", chat_id="1", text="/help")

    runtime.handle_on_gui(request)

    assert snapshots == ["snapshot"]
    assert request.response is not None
    assert request.response.text == telegram_runtime.telegram_commands.help_text()
    assert request.response.callback_answer == "Commands."
    assert request.response.reply_markup == [
        [("Status", "status")],
        [("Next photo", "watch:photo"), ("Next contact", "watch:contact")],
    ]


def test_unknown_message_uses_no_state_provider() -> None:
    route_states: list[str] = []
    statuses: list[str] = []
    api_checks: list[str] = []
    runtime = _runtime(
        command_snapshot_provider=lambda: (
            route_states.append("route") or _command_snapshot()
        ),
        status_snapshot_provider=lambda: (
            statuses.append("status") or _status_snapshot()
        ),
        api_route_confirmation_provider=lambda: api_checks.append("api") or True,
    )
    request = TelegramBotRequest(kind="message", chat_id="1", text="/unknown")

    runtime.handle_on_gui(request)

    assert request.response is None
    assert route_states == []
    assert statuses == []
    assert api_checks == []


@pytest.mark.parametrize(
    ("kind", "text", "callback_data"),
    [
        ("message", "/help", ""),
        ("callback", "", "unknown"),
    ],
)
def test_help_and_unknown_callback_only_read_route_presentation_state(
    monkeypatch,
    kind: str,
    text: str,
    callback_data: str,
) -> None:
    route_states: list[str] = []
    statuses: list[str] = []
    api_checks: list[str] = []
    monkeypatch.setattr(
        telegram_runtime, "telegram_inline_keyboard", lambda _rows: "markup"
    )
    runtime = _runtime(
        command_snapshot_provider=lambda: (
            route_states.append("route") or _command_snapshot()
        ),
        status_snapshot_provider=lambda: (
            statuses.append("status") or _status_snapshot()
        ),
        api_route_confirmation_provider=lambda: api_checks.append("api") or True,
    )
    request = TelegramBotRequest(
        kind=kind,
        chat_id="1",
        text=text,
        callback_data=callback_data,
    )

    runtime.handle_on_gui(request)

    assert request.response is not None
    assert route_states == ["route"]
    assert statuses == []
    assert api_checks == []


def test_status_response_uses_snapshot_text_and_latest_photo(monkeypatch) -> None:
    monkeypatch.setattr(
        telegram_runtime, "telegram_inline_keyboard", lambda _rows: "markup"
    )
    runtime = _runtime(
        command_snapshot_provider=lambda: _command_snapshot(),
        status_snapshot_provider=_status_snapshot,
        latest_camera_photo_provider=lambda: (b"jpeg", "latest.jpg"),
    )
    request = TelegramBotRequest(kind="message", chat_id="1", text="/status")

    runtime.handle_on_gui(request)

    assert request.response is not None
    assert request.response.text.startswith("Probe Station status\n")
    assert request.response.photo_bytes == b"jpeg"
    assert request.response.photo_name == "latest.jpg"
    assert request.response.reply_markup == "markup"
    assert request.response.callback_answer == "Status sent."


def test_route_photo_request_uses_owned_route_photo_state(monkeypatch) -> None:
    monkeypatch.setattr(
        telegram_runtime, "telegram_inline_keyboard", lambda _rows: "markup"
    )
    runtime = _runtime(
        command_snapshot_provider=lambda: _command_snapshot(
            route_active=True,
            photo_enabled=True,
        )
    )
    request = TelegramBotRequest(kind="message", chat_id="1", text="/next_photo")

    runtime.handle_on_gui(request)

    assert runtime.route_photos.consume_route_photo_request() is True
    assert runtime.route_photos.consume_route_photo_request() is False
    assert request.response is not None
    assert request.response.text == "The next route structure photo will be sent here."
    assert request.response.callback_answer == "Waiting for route photo."


def test_known_route_action_uses_snapshot_policy_and_submitter(monkeypatch) -> None:
    submitted: list[str] = []
    api_checks: list[str] = []
    monkeypatch.setattr(
        telegram_runtime, "telegram_inline_keyboard", lambda _rows: "markup"
    )
    runtime = _runtime(
        command_snapshot_provider=lambda: _command_snapshot(
            route_waiting=True,
            runner_available=False,
        ),
        route_action_submitter=submitted.append,
        api_route_confirmation_provider=lambda: api_checks.append("checked") or True,
    )
    request = TelegramBotRequest(
        kind="callback", chat_id="1", callback_data="route:skip"
    )

    runtime.handle_on_gui(request)

    assert submitted == ["skip"]
    assert api_checks == ["checked"]
    assert request.response is not None
    assert request.response.text == "API route control action submitted: skip."
    assert request.response.callback_answer == "skip submitted."


@pytest.mark.parametrize(
    ("callback_data", "route_waiting", "runner_available"),
    [
        ("route:remeasure", True, False),
        ("route:skip", False, False),
        ("route:measure", True, True),
    ],
)
def test_route_action_avoids_api_confirmation_when_not_needed(
    monkeypatch,
    callback_data: str,
    route_waiting: bool,
    runner_available: bool,
) -> None:
    api_checks: list[str] = []
    monkeypatch.setattr(
        telegram_runtime, "telegram_inline_keyboard", lambda _rows: "markup"
    )
    runtime = _runtime(
        command_snapshot_provider=lambda: _command_snapshot(
            route_waiting=route_waiting,
            runner_available=runner_available,
        ),
        api_route_confirmation_provider=lambda: api_checks.append("checked") or True,
    )
    request = TelegramBotRequest(
        kind="callback", chat_id="1", callback_data=callback_data
    )

    runtime.handle_on_gui(request)

    assert api_checks == []


def test_send_bot_message_uses_current_settings_and_payload(monkeypatch) -> None:
    settings = TelegramSettings(enabled=True, chat_id="42")
    sent: list[tuple[object, str, dict[str, object]]] = []
    monkeypatch.setattr(
        telegram_runtime,
        "send_telegram_bot_message_for_settings",
        lambda current, message, **kwargs: (
            sent.append((current, message, kwargs)) or True
        ),
    )
    runtime = _runtime(settings_provider=lambda: settings)

    accepted = runtime.send_bot_message(
        "hello", photo=(b"jpeg", "photo.jpg"), document_path="run.csv"
    )

    assert accepted is True
    assert sent == [
        (
            settings,
            "hello",
            {
                "photo": (b"jpeg", "photo.jpg"),
                "document_path": "run.csv",
                "reply_markup": None,
            },
        )
    ]


def test_send_alert_uses_current_settings_and_latest_photo_port(monkeypatch) -> None:
    settings = TelegramSettings(enabled=True, chat_id="42")
    sent: list[tuple[object, str, str, dict[str, object]]] = []

    def latest() -> tuple[bytes, str]:
        return b"jpeg", "latest.jpg"

    monkeypatch.setattr(
        telegram_runtime,
        "send_telegram_alert_for_settings",
        lambda current, key, message, **kwargs: sent.append(
            (current, key, message, kwargs)
        ),
    )
    runtime = _runtime(
        settings_provider=lambda: settings,
        latest_camera_photo_provider=latest,
    )

    runtime.send_alert("camera_error", "camera failed", attach_photo=True)

    assert sent == [
        (
            settings,
            "camera_error",
            "camera failed",
            {
                "attach_photo": True,
                "photo": None,
                "document_path": None,
                "reply_markup": None,
                "latest_camera_frame_photo": latest,
            },
        )
    ]


def test_markup_methods_convert_command_rows(monkeypatch) -> None:
    rows_seen: list[object] = []
    monkeypatch.setattr(
        telegram_runtime,
        "telegram_inline_keyboard",
        lambda rows: rows_seen.append(rows) or ("markup", rows),
    )
    runtime = _runtime()

    default = runtime.default_markup(route_waiting=True)
    actions = runtime.route_actions_markup()

    assert default == (
        "markup",
        telegram_runtime.telegram_commands.default_markup_rows(True),
    )
    assert actions == (
        "markup",
        telegram_runtime.telegram_commands.route_action_markup_rows(),
    )
    assert rows_seen == [
        telegram_runtime.telegram_commands.default_markup_rows(True),
        telegram_runtime.telegram_commands.route_action_markup_rows(),
    ]
