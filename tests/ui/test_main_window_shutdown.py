from __future__ import annotations

from types import SimpleNamespace

from probe_station_gui.views import main_window_shutdown as shutdown_ui


class _Serial:
    def __init__(self, events: list[object]) -> None:
        self.events = events
        self.is_open = True

    def close(self) -> None:
        self.events.append(("serial_close",))
        self.is_open = False


class _AliveThread:
    def __init__(self, events: list[object], name: str) -> None:
        self.events = events
        self.name = name
        self.joined = False

    def is_alive(self) -> bool:
        return not self.joined

    def join(self, timeout: float | None = None) -> None:
        self.events.append((self.name, "join", timeout))
        self.joined = True


def test_shutdown_waits_for_calibration_and_outer_session_close() -> None:
    events: list[object] = []
    calibration_thread = _AliveThread(events, "calibration_thread")
    close_thread = _AliveThread(events, "session_close_thread")
    owner = SimpleNamespace(
        _flat_field_calibration_thread=calibration_thread,
        _lens_distortion_thread=None,
        _optical_calibration_outer_close_thread=None,
        _optical_calibration_outer_lease=object(),
        _cancel_optical_calibration_wizard=lambda: events.append(("cancel",)),
    )

    def schedule_close() -> None:
        events.append(("schedule_close",))
        owner._optical_calibration_outer_close_thread = close_thread

    owner._schedule_optical_calibration_outer_close = schedule_close

    shutdown_ui._stop_optical_calibration(owner)

    assert events == [
        ("cancel",),
        ("calibration_thread", "join", 2.0),
        ("schedule_close",),
        ("session_close_thread", "join", 2.0),
    ]


def test_close_event_preserves_shutdown_order(monkeypatch) -> None:
    events: list[object] = []
    monkeypatch.setattr(
        shutdown_ui.connection_flow,
        "persist_serial_connection_state",
        lambda _owner, connected: events.append(("persist_serial", connected)),
    )
    monkeypatch.setattr(
        shutdown_ui.connection_flow,
        "persist_lcr_connection_state",
        lambda _owner, connected: events.append(("persist_lcr", connected)),
    )
    monkeypatch.setattr(
        shutdown_ui.connection_flow,
        "persist_controller_state",
        lambda _owner: events.append(("persist_controller",)),
    )
    serial = _Serial(events)
    route_thread = _AliveThread(events, "route_thread")
    scan_thread = _AliveThread(events, "scan_thread")
    owner = SimpleNamespace(
        serial_connection=serial,
        lcr_controller=SimpleNamespace(
            is_connected=lambda: True,
            shutdown=lambda: events.append(("lcr_shutdown",)),
        ),
        _api_server=SimpleNamespace(stop=lambda: events.append(("api_stop",))),
        _design_position_timer=SimpleNamespace(
            stop=lambda: events.append(("design_timer_stop",))
        ),
        _manual_jog_timer=SimpleNamespace(
            stop=lambda: events.append(("manual_timer_stop",))
        ),
        _stage_motion_blink_timer=SimpleNamespace(
            stop=lambda: events.append(("blink_timer_stop",))
        ),
        _linear_feedrate_save_timer=SimpleNamespace(
            isActive=lambda: True,
            stop=lambda: events.append(("feedrate_timer_stop",)),
        ),
        _route_measurement_runner=SimpleNamespace(
            stop=lambda: events.append(("route_runner_stop",))
        ),
        _route_measurement_thread=route_thread,
        _microscope_scan_thread=scan_thread,
        _microscope_scan_stop_requested=SimpleNamespace(
            set=lambda: events.append(("scan_stop_requested",))
        ),
        _exposure_policy_adapter=SimpleNamespace(
            shutdown=lambda **kwargs: events.append(
                ("exposure_adapter_shutdown", kwargs)
            )
        ),
        _exposure_policy_controller=SimpleNamespace(
            shutdown=lambda **kwargs: events.append(
                ("exposure_controller_shutdown", kwargs)
            )
        ),
        grabber=SimpleNamespace(stop=lambda: events.append(("grabber_stop",))),
        _live_camera_frame_processor=SimpleNamespace(
            shutdown=lambda **kwargs: events.append(("frame_processor_shutdown", kwargs))
        ),
        thread=SimpleNamespace(
            quit=lambda: events.append(("camera_thread_quit",)),
            wait=lambda: events.append(("camera_thread_wait",)),
        ),
        joystick_panel=SimpleNamespace(
            stop_jog=lambda: events.append(("stop_jog", "application shutdown", serial.is_open)),
            set_serial=lambda serial_connection: events.append(
                ("joystick_serial", serial_connection)
            )
        ),
        serial_terminal_panel=SimpleNamespace(
            set_serial=lambda serial_connection: events.append(
                ("terminal_serial", serial_connection)
            )
        ),
        serial_connection_panel=SimpleNamespace(
            shutdown=lambda: events.append(("serial_panel_shutdown",))
        ),
        stage_controller=SimpleNamespace(
            force_jog_stop=lambda **kwargs: events.append(("force_jog_stop", kwargs)),
            request_stop_oscillation=lambda: events.append(("stop_oscillation",)),
            shutdown=lambda: events.append(("stage_shutdown",)),
        ),
        _stop_telegram_bot_service=lambda: events.append(("telegram_stop",)),
        _save_pending_linear_feedrate_default=lambda: events.append(
            ("save_feedrate",)
        ),
        _stop_design_markup_store=lambda: events.append(("markup_store_stop",)),
        _route_measurement_dialog=SimpleNamespace(
            close=lambda: events.append(("route_close",))
        ),
        _route_runtime_presenter=lambda: SimpleNamespace(
            set_running=lambda running: events.append(("running", running))
        ),
        design_layout_window=None,
        contact_calibration_window=None,
        surface_map_window=None,
        microscope_scan_dialog=None,
        serial_connection_dialog=None,
    )
    event = SimpleNamespace(accept=lambda: events.append(("event_accept",)))

    shutdown_ui.close_event(owner, event)

    assert events == [
        ("persist_serial", True),
        ("persist_lcr", True),
        ("persist_controller",),
        ("api_stop",),
        ("telegram_stop",),
        ("design_timer_stop",),
        ("manual_timer_stop",),
        ("blink_timer_stop",),
        ("feedrate_timer_stop",),
        ("save_feedrate",),
        ("markup_store_stop",),
        ("route_runner_stop",),
        ("route_thread", "join", 2.0),
        ("scan_stop_requested",),
        ("scan_thread", "join", 2.0),
        ("stop_jog", "application shutdown", True),
        ("force_jog_stop", {"timeout": 0.8}),
        ("exposure_adapter_shutdown", {"timeout_s": 2.0}),
        ("exposure_controller_shutdown", {"timeout_s": 2.0}),
        ("frame_processor_shutdown", {"timeout_s": 2.0}),
        ("grabber_stop",),
        ("camera_thread_quit",),
        ("camera_thread_wait",),
        ("serial_close",),
        ("joystick_serial", None),
        ("terminal_serial", None),
        ("stop_oscillation",),
        ("stage_shutdown",),
        ("lcr_shutdown",),
        ("running", False),
        ("route_close",),
        ("serial_panel_shutdown",),
        ("event_accept",),
    ]


def test_exposure_shutdown_timeout_blocks_camera_teardown_and_close(
    monkeypatch,
) -> None:
    events: list[object] = []
    monkeypatch.setattr(
        shutdown_ui,
        "_persist_shutdown_state",
        lambda *_args, **_kwargs: events.append(("persist",)),
    )
    monkeypatch.setattr(
        shutdown_ui,
        "_stop_services_and_timers",
        lambda _owner: events.append(("services",)),
    )
    monkeypatch.setattr(
        shutdown_ui,
        "_stop_route_worker",
        lambda _owner: events.append(("route",)),
    )
    monkeypatch.setattr(
        shutdown_ui,
        "_stop_microscope_scan",
        lambda _owner: events.append(("scan",)),
    )

    def fail_adapter_shutdown(**kwargs) -> None:
        events.append(("exposure_adapter_shutdown", kwargs))
        raise RuntimeError("Camera exposure adapter did not stop active command.")

    owner = SimpleNamespace(
        serial_connection=SimpleNamespace(is_open=False),
        lcr_controller=SimpleNamespace(is_connected=lambda: False),
        _exposure_policy_adapter=SimpleNamespace(shutdown=fail_adapter_shutdown),
        _exposure_policy_controller=SimpleNamespace(
            shutdown=lambda **kwargs: events.append(
                ("exposure_controller_shutdown", kwargs)
            )
        ),
        _live_camera_frame_processor=SimpleNamespace(
            shutdown=lambda **kwargs: events.append(("frame_processor_shutdown", kwargs))
        ),
        grabber=SimpleNamespace(stop=lambda: events.append(("grabber_stop",))),
        thread=SimpleNamespace(
            quit=lambda: events.append(("camera_thread_quit",)),
            wait=lambda: events.append(("camera_thread_wait",)),
        ),
        joystick_panel=None,
        stage_controller=SimpleNamespace(force_jog_stop=lambda **_kwargs: None),
        serial_terminal_panel=None,
        serial_connection_panel=None,
        _show_status=lambda message, timeout: events.append(
            ("status", message, timeout)
        ),
    )
    event = SimpleNamespace(
        accept=lambda: events.append(("event_accept",)),
        ignore=lambda: events.append(("event_ignore",)),
    )

    shutdown_ui.close_event(owner, event)

    assert events == [
        ("persist",),
        ("services",),
        ("route",),
        ("scan",),
        ("exposure_adapter_shutdown", {"timeout_s": 2.0}),
        (
            "status",
            "Camera shutdown blocked: Camera exposure adapter did not stop active command.",
            10000,
        ),
        ("event_ignore",),
    ]


def test_close_auxiliary_windows_forces_route_presenter_before_dialog_close() -> None:
    events: list[object] = []
    owner = SimpleNamespace(
        _route_measurement_dialog=SimpleNamespace(
            close=lambda: events.append(("route_close",))
        ),
        _route_runtime_presenter=lambda: SimpleNamespace(
            set_running=lambda running: events.append(("running", running))
        ),
        design_layout_window=SimpleNamespace(
            close=lambda: events.append(("design_close",))
        ),
        contact_calibration_window=None,
        surface_map_window=SimpleNamespace(
            close=lambda: events.append(("surface_close",))
        ),
        microscope_scan_dialog=None,
        serial_connection_dialog=SimpleNamespace(
            close=lambda: events.append(("serial_dialog_close",))
        ),
    )

    shutdown_ui.close_auxiliary_windows(owner, force_route_dialog=True)

    assert events == [
        ("running", False),
        ("route_close",),
        ("design_close",),
        ("surface_close",),
        ("serial_dialog_close",),
    ]


def test_stop_jog_before_serial_close_noops_when_serial_is_closed() -> None:
    events: list[object] = []
    owner = SimpleNamespace(
        serial_connection=SimpleNamespace(is_open=False),
        joystick_panel=SimpleNamespace(stop_jog=lambda: events.append(("stop_jog",))),
        stage_controller=SimpleNamespace(
            force_jog_stop=lambda **_kwargs: events.append(("force_stop",))
        ),
    )

    shutdown_ui.stop_jog_before_serial_close(owner, "test")

    assert events == []
