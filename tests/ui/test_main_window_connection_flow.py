from __future__ import annotations

from types import SimpleNamespace

from probe_station_gui.views import main_window_connection_flow as connection_flow


class _Serial:
    def __init__(self, events: list[object], *, port: str = "COM7") -> None:
        self.events = events
        self.port = port
        self.baudrate = "115200"
        self.is_open = True

    def close(self) -> None:
        self.events.append(("serial_close", self.port))
        self.is_open = False


class _StageController:
    def __init__(self, events: list[object]) -> None:
        self.events = events
        self.owner = None
        self._axis_rates = {"X": 1200.0}

    def set_serial(self, serial_connection: object | None) -> None:
        assert self.owner._controller_state_persistence_suspended is True
        port = getattr(serial_connection, "port", None)
        self.events.append(("stage_set_serial", port))

    def request_stop_oscillation(self) -> None:
        self.events.append(("stop_oscillation",))

    def force_jog_stop(self, *, timeout: float) -> None:
        self.events.append(("force_jog_stop", timeout))

    def request_startup_sync(self, **kwargs: object) -> None:
        self.events.append(("startup_sync", kwargs))

    def apply_axis_max_feedrates(self, rates: object) -> None:
        self.events.append(("apply_rates", rates))
        self._axis_rates = dict(rates)

    def axis_max_feedrates(self) -> dict[str, float]:
        return dict(self._axis_rates)


class _RestoreStageController:
    def __init__(self, events: list[object], *, current: bool) -> None:
        self.events = events
        self.current = current

    def cached_controller_session_is_current(self, cached_state: dict) -> bool:
        self.events.append(("session_current", cached_state))
        return self.current

    def clear_cached_controller_state(self) -> None:
        self.events.append(("clear_stage_cache",))

    def import_cached_controller_state(self, cached_state: dict) -> None:
        self.events.append(("import_cache", cached_state))

    def apply_axis_max_feedrates(self, rates: object) -> None:
        self.events.append(("apply_rates", rates))

    def axis_max_feedrates(self) -> dict[str, float]:
        return {"X": 900.0}


class _Settings:
    def __init__(self, events: list[object], cached_state: dict | None = None) -> None:
        self.events = events
        self.cached_state = cached_state or {"cached": True}
        self.serial_auto = False
        self.meter_auto = False

    def load_controller_state(self) -> dict:
        self.events.append(("load_controller_state",))
        return dict(self.cached_state)

    def serial_auto_connect_enabled(self) -> bool:
        return self.serial_auto

    def meter_auto_connect_enabled(self) -> bool:
        return self.meter_auto

    def save_serial_connection_state(
        self,
        connected: bool,
        *,
        port: str,
        baud_rate: int,
    ) -> None:
        self.events.append(("save_serial", connected, port, baud_rate))

    def save_meter_connection_state(
        self,
        connected: bool,
        *,
        meter_type: str,
        description: str,
    ) -> None:
        self.events.append(("save_meter", connected, meter_type, description))


class _Panel:
    def __init__(self, events: list[object], name: str) -> None:
        self.events = events
        self.name = name

    def set_serial(self, serial_connection: object | None) -> None:
        self.events.append((self.name, getattr(serial_connection, "port", None)))

    def handle_external_disconnect(self, *, auto_retry: bool) -> None:
        self.events.append((self.name, "external_disconnect", auto_retry))

    def auto_connect(self) -> None:
        self.events.append((self.name, "auto_connect"))

    def stop_jog(self) -> None:
        self.events.append(("stop_jog", "serial disconnect"))

    def set_current_stage_position(self, value: object) -> None:
        self.events.append((self.name, "stage_position", value))

    def set_current_needle_lowering(self, value: object) -> None:
        self.events.append((self.name, "needle_lowering", value))

    def set_axis_feedrate_limits(self, rates: dict[str, float]) -> None:
        self.events.append((self.name, "axis_rates", rates))


class _Dock:
    def __init__(self, events: list[object]) -> None:
        self.events = events

    def setVisible(self, visible: bool) -> None:  # noqa: N802 - Qt naming
        self.events.append(("dock_visible", visible))

    def raise_(self) -> None:
        self.events.append(("dock_raise",))

    def isFloating(self) -> bool:  # noqa: N802 - Qt naming
        return True

    def activateWindow(self) -> None:  # noqa: N802 - Qt naming
        self.events.append(("dock_activate",))


def _owner(events: list[object]) -> SimpleNamespace:
    stage_controller = _StageController(events)
    owner = SimpleNamespace(
        serial_connection=None,
        serial_port_name="",
        serial_baud_rate=0,
        settings_manager=_Settings(events),
        stage_controller=stage_controller,
        joystick_panel=_Panel(events, "joystick"),
        joystick_dock=_Dock(events),
        serial_terminal_panel=_Panel(events, "terminal"),
        serial_connection_panel=_Panel(events, "serial_panel"),
        contact_calibration_window=_Panel(events, "contact"),
        oscillation_panel=SimpleNamespace(
            set_running=lambda running, axis: events.append(("oscillation", running, axis))
        ),
        _stage_unhomed_display_origins={"X": 1.0},
        _last_reported_b_position=12.0,
        _controller_state_persistence_suspended=False,
        _controller_reboot_recovery_scheduled=False,
        _pending_persisted_design_state={"design": True},
        _pending_persisted_design_position=(1.0, 2.0, 3.0),
        _pending_homing_axes=[],
        _homing_active_key=None,
        _stage_motion_axes=set(),
        _stage_motion_blink_dimmed=False,
        _stage_motion_blink_timer=SimpleNamespace(isActive=lambda: False),
    )
    stage_controller.owner = owner
    owner._restore_persisted_controller_state = (
        lambda state, **kwargs: events.append(("restore", state, kwargs))
    )
    owner._run_serial_startup_sync = lambda: events.append(("startup_callback",))
    owner._run_controller_reboot_recovery = lambda: events.append(
        ("reboot_recovery_callback",)
    )
    owner._refresh_design_position = lambda: events.append(("refresh_design",))
    owner._persist_serial_connection_state = (
        lambda connected: events.append(
            ("persist_serial_wrapper", connected, owner.serial_connection)
        )
    )
    owner._manual_jog_timer = SimpleNamespace(
        stop=lambda: events.append(("manual_timer_stop",))
    )
    owner._manual_jog_prediction = SimpleNamespace(
        reset_tracking=lambda: events.append(("manual_prediction_reset",))
    )
    owner._clear_coordinate_move_tracking = (
        lambda **kwargs: events.append(("clear_coordinate_tracking", kwargs))
    )
    owner._clear_planned_move_prediction = (
        lambda **kwargs: events.append(("clear_prediction", kwargs))
    )
    owner._update_stage_coordinate_apply_state = lambda: events.append(("apply_state",))
    owner._update_stage_position_display = lambda value: events.append(
        ("stage_display", value)
    )
    owner.sender = lambda: object()
    owner._reset_manual_alignment = lambda **kwargs: events.append(
        ("reset_alignment", kwargs)
    )
    owner._invalidate_design_registration = lambda message: events.append(
        ("invalidate_design", message)
    )
    owner._update_design_position = lambda value: events.append(
        ("update_design_position", value)
    )
    owner._schedule_cancel_state_refresh = lambda: events.append(("schedule_refresh",))
    owner._apply_axis_feedrate_limits = lambda rates: connection_flow.apply_axis_feedrate_limits(
        owner,
        rates,
    )
    owner._apply_joystick_feedrate_preferences = lambda: events.append(
        ("apply_joystick_preferences",)
    )
    return owner


def test_on_serial_connected_preserves_attach_order(monkeypatch) -> None:
    events: list[object] = []
    timer_calls: list[tuple[int, object]] = []
    monkeypatch.setattr(
        connection_flow,
        "QTimer",
        SimpleNamespace(singleShot=lambda delay, callback: timer_calls.append((delay, callback))),
    )
    monkeypatch.setattr(
        connection_flow,
        "restore_persisted_controller_state",
        lambda _owner, state, **kwargs: events.append(("restore", state, kwargs)),
    )
    owner = _owner(events)
    owner.serial_connection = _Serial(events, port="OLD")
    serial_port = _Serial(events, port="COM9")

    connection_flow.on_serial_connected(owner, serial_port)

    assert events[:4] == [
        ("serial_close", "OLD"),
        ("load_controller_state",),
        ("stage_set_serial", "COM9"),
        ("restore", {"cached": True}, {"cache_already_loaded": True}),
    ]
    assert owner.serial_connection is serial_port
    assert owner.serial_port_name == "COM9"
    assert owner.serial_baud_rate == 115200
    assert ("joystick", "COM9") in events
    assert ("terminal", "COM9") in events
    assert ("refresh_design",) in events
    assert len(timer_calls) == 1
    assert timer_calls[0][0] == 0
    assert callable(timer_calls[0][1])
    assert owner._controller_state_persistence_suspended is False


def test_on_serial_disconnected_preserves_detach_cleanup_order() -> None:
    events: list[object] = []
    owner = _owner(events)
    owner.serial_connection = _Serial(events, port="COM9")
    original_clear = connection_flow.stage_move_lifecycle.clear_coordinate_move_tracking
    original_display = connection_flow.stage_position_panel.update_stage_position_display
    connection_flow.stage_move_lifecycle.clear_coordinate_move_tracking = (
        lambda _owner, **kwargs: events.append(("clear_coordinate_tracking", kwargs))
    )
    connection_flow.stage_position_panel.update_stage_position_display = (
        lambda _owner, value: events.append(("stage_display", value))
    )

    try:
        connection_flow.on_serial_disconnected(owner)
    finally:
        connection_flow.stage_move_lifecycle.clear_coordinate_move_tracking = (
            original_clear
        )
        connection_flow.stage_position_panel.update_stage_position_display = (
            original_display
        )

    assert events[:4] == [
        ("stop_jog", "serial disconnect"),
        ("force_jog_stop", 0.8),
        ("serial_close", "COM9"),
        ("save_serial", False, "", 0),
    ]
    assert ("manual_timer_stop",) in events
    assert ("stage_set_serial", None) in events
    assert ("serial_panel", "external_disconnect", True) in events
    assert ("joystick", None) in events
    assert ("terminal", None) in events
    assert ("contact", "stage_position", None) in events
    assert ("contact", "needle_lowering", None) in events
    assert ("oscillation", False, "") in events
    assert (
        "invalidate_design",
        "Design registration cleared after serial disconnect.",
    ) in events
    assert owner.serial_connection is None
    assert owner._controller_state_persistence_suspended is False


def test_startup_sync_and_reboot_recovery_are_guarded(monkeypatch) -> None:
    events: list[object] = []
    timer_calls: list[tuple[int, object]] = []
    monkeypatch.setattr(
        connection_flow,
        "QTimer",
        SimpleNamespace(singleShot=lambda delay, callback: timer_calls.append((delay, callback))),
    )
    owner = _owner(events)

    owner.serial_connection = SimpleNamespace(is_open=False)
    connection_flow.run_serial_startup_sync(owner)
    assert events == []

    owner.serial_connection = SimpleNamespace(is_open=True)
    connection_flow.run_serial_startup_sync(owner)
    assert events == [
        (
            "startup_sync",
            {"auto_home_a": True, "clear_unverified_state": False},
        ),
        ("schedule_refresh",),
    ]

    connection_flow.on_controller_reboot_ready(owner)
    connection_flow.on_controller_reboot_ready(owner)
    assert owner._controller_reboot_recovery_scheduled is True
    assert len(timer_calls) == 1

    connection_flow.run_controller_reboot_recovery(owner)
    assert owner._controller_reboot_recovery_scheduled is False
    assert events.count(
        (
            "startup_sync",
            {"auto_home_a": True, "clear_unverified_state": False},
        )
    ) == 2


def test_feedrate_limit_and_auto_connect_decisions() -> None:
    events: list[object] = []
    owner = _owner(events)

    connection_flow.apply_axis_feedrate_limits(owner, object())
    assert events == []

    connection_flow.apply_axis_feedrate_limits(owner, {"X": 900.0})
    assert ("apply_rates", {"X": 900.0}) in events
    assert ("joystick", "axis_rates", {"X": 900.0}) in events

    owner.settings_manager.serial_auto = True
    owner.settings_manager.meter_auto = True
    owner.serial_connection = None
    owner.lcr_controller = SimpleNamespace(
        is_connected=lambda: False,
        request_connect=lambda: events.append(("lcr", "request_connect")),
    )
    connection_flow.auto_connect_if_possible(owner)

    assert ("serial_panel", "auto_connect") in events
    assert ("lcr", "request_connect") in events


def test_request_lcr_disconnect_persists_before_disconnect() -> None:
    events: list[object] = []
    owner = SimpleNamespace(
        settings_manager=SimpleNamespace(
            save_meter_connection_state=lambda connected, **kwargs: events.append(
                ("save_meter", connected, kwargs)
            )
        ),
        lcr_controller=SimpleNamespace(
            meter_type=lambda: "gwinstek",
            connection_label=lambda: "LCR",
            request_disconnect=lambda: events.append(("disconnect_lcr",))
        ),
    )

    connection_flow.request_lcr_disconnect(owner)

    assert events == [
        ("save_meter", False, {"meter_type": "gwinstek", "description": "LCR"}),
        ("disconnect_lcr",),
    ]


def test_restore_persisted_controller_state_clears_stale_cache() -> None:
    events: list[object] = []
    owner = SimpleNamespace(
        serial_connection=SimpleNamespace(is_open=True),
        settings_manager=SimpleNamespace(
            clear_controller_state=lambda: events.append(("clear_settings_cache",))
        ),
        stage_controller=_RestoreStageController(events, current=False),
        _pending_persisted_design_state={"design": True},
        _pending_persisted_design_position=(1.0, 2.0, 3.0),
        _show_status=lambda message: events.append(("status", message)),
    )

    connection_flow.restore_persisted_controller_state(
        owner,
        {"session": "old"},
        cache_already_loaded=True,
    )

    assert events == [
        ("session_current", {"session": "old"}),
        ("clear_settings_cache",),
        ("clear_stage_cache",),
        ("status", "Controller session changed. Cleared cached homing state."),
    ]
    assert owner._pending_persisted_design_state is None
    assert owner._pending_persisted_design_position is None


def test_restore_persisted_controller_state_imports_current_cache(monkeypatch) -> None:
    events: list[object] = []
    monkeypatch.setattr(
        connection_flow,
        "prepare_persisted_design_restore",
        lambda _owner, state: events.append(("prepare_design_restore", state)),
    )
    owner = SimpleNamespace(
        serial_connection=SimpleNamespace(is_open=True),
        settings_manager=SimpleNamespace(load_controller_state=lambda: {"session": "ok"}),
        stage_controller=_RestoreStageController(events, current=True),
        joystick_panel=SimpleNamespace(
            set_axis_feedrate_limits=lambda rates: events.append(
                ("joystick_rates", rates)
            )
        ),
        _show_status=lambda message: events.append(("status", message)),
    )

    connection_flow.restore_persisted_controller_state(owner)

    assert events == [
        ("session_current", {"session": "ok"}),
        ("prepare_design_restore", {"session": "ok"}),
        ("import_cache", {"session": "ok"}),
        ("apply_rates", {"X": 900.0}),
        ("joystick_rates", {"X": 900.0}),
        ("status", "Restored cached homing state; reading live coordinates."),
    ]
