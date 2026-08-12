from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

from probe_station_gui.stage.controller import StageController
from probe_station_gui.views.joystick_window import JoystickWindow


class _SignalRecorder:
    def __init__(self) -> None:
        self.values: list[tuple[object, ...]] = []

    def emit(self, *args: object) -> None:
        self.values.append(args)


def _jog_widget(stage_controller: object) -> tuple[JoystickWindow, list[object]]:
    sent: list[object] = []
    widget = JoystickWindow.__new__(JoystickWindow)
    widget.serial_connection = SimpleNamespace(is_open=True)
    widget.stage_controller = stage_controller
    widget._active_axes = None
    widget._active_jog_projection_lease = None
    widget._relative_motion_projector = None
    widget._linear_jog_distance_mm = 25.0
    widget._rotary_jog_distance_deg = 5.0
    widget._manual_axis_distance_mm = 1.0
    widget._jog_stop_resend_generation = 0
    widget._move_safety_check = lambda: True
    widget._feedrate_for_axes = lambda _axes: 20.0
    widget._distance_for_axis = lambda _axis: 25.0
    widget._show_warning = lambda message: (_ for _ in ()).throw(
        AssertionError(f"unexpected warning: {message}")
    )
    widget.send_command = lambda command: sent.append(command) or True
    widget.jog_command_changed = _SignalRecorder()
    return widget, sent


def test_apply_axes_with_missing_cache_sends_jog_without_status_io() -> None:
    class _ExplodingSerial:
        is_open = True

        def __getattr__(self, name: str) -> object:
            raise AssertionError(f"unexpected GUI-thread serial access: {name}")

    controller = StageController()
    controller._serial = _ExplodingSerial()
    controller._last_stage_position = None
    controller._query_status = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("status query reached from GUI jog")
    )
    widget, sent = _jog_widget(controller)

    try:
        JoystickWindow._apply_axes(widget, (("X", 1),))
    finally:
        controller._serial = None
        controller.shutdown()

    assert sent == ["$J=G91 G21 X25.000 F20.0\n"]


def test_apply_axes_uses_known_cached_constraint_result_exactly() -> None:
    controller = StageController()
    controller._position_reporting_mode = "machine"
    controller._axis_limits = {"X": (0.0, 10.0)}
    controller._homed_axes = {"X"}
    controller._last_stage_position = (9.5, 0.0, 0.0, 0.0, 0.0, 0.0)
    widget, sent = _jog_widget(controller)

    try:
        JoystickWindow._apply_axes(widget, (("X", 1),))
    finally:
        controller.shutdown()

    assert sent == ["$J=G91 G21 X0.500 F20.0\n"]


def test_missing_cached_a_position_never_calls_blocking_read() -> None:
    warnings: list[str] = []

    class _CachedOnlyController:
        def latest_a_position(self) -> None:
            return None

        def last_a_position_read_failure(self) -> str:
            return ""

        def current_a_position(self) -> float:
            raise AssertionError("blocking A read reached from GUI")

    widget = JoystickWindow.__new__(JoystickWindow)
    widget.stage_controller = _CachedOnlyController()
    widget._show_warning = lambda message: warnings.append(str(message))

    result = JoystickWindow._current_needle_contact_a_coordinate(widget)

    assert result is None
    assert warnings == ["Unable to read current A coordinate."]


def test_cached_a_position_is_converted_for_display() -> None:
    class _CachedController:
        def latest_a_position(self) -> float:
            return -2.5

        def calibrated_axis_display_value(self, axis: str, value: float) -> float:
            assert (axis, value) == ("A", -2.5)
            return 3.75

    widget = JoystickWindow.__new__(JoystickWindow)
    widget.stage_controller = _CachedController()
    widget._show_warning = lambda message: (_ for _ in ()).throw(
        AssertionError(f"unexpected warning: {message}")
    )

    assert JoystickWindow._current_needle_contact_a_coordinate(widget) == 3.75


def test_gui_owner_has_no_blocking_current_a_position_call() -> None:
    source_path = Path(JoystickWindow.__module__.replace(".", "/") + ".py")
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    owner = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "JoystickWindow"
    )

    assert not any(
        isinstance(node, ast.Attribute) and node.attr == "current_a_position"
        for node in ast.walk(owner)
    )


def test_stage_jog_owner_has_no_synchronous_cache_refresh_helper() -> None:
    assert not hasattr(StageController, "_refresh_cached_jog_status_if_missing")
