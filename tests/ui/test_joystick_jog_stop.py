from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

import pytest
from PySide6.QtCore import Qt

from probe_station_gui.views.joystick import jog_runtime as joystick_module
from probe_station_gui.views.joystick_window import JoystickWindow


class _SignalRecorder:
    def __init__(self) -> None:
        self.values: list[tuple[object, ...]] = []

    def emit(self, *args: object) -> None:
        self.values.append(args)


class _Timer:
    def __init__(self) -> None:
        self.active = False

    def isActive(self) -> bool:
        return self.active

    def stop(self) -> None:
        self.active = False


class _Label:
    def __init__(self) -> None:
        self.text = ""

    def setText(self, text: str) -> None:
        self.text = str(text)


class _Serial:
    def __init__(
        self, name: str, timeline: list[tuple[str, object]] | None = None
    ) -> None:
        self.is_open = True
        self.port = name
        self.baudrate = 115200
        self.writes: list[bytes] = []
        self.flush_count = 0
        self._timeline = timeline

    def write(self, payload: bytes) -> None:
        value = bytes(payload)
        self.writes.append(value)
        if self._timeline is not None:
            self._timeline.append(("write", value))

    def flush(self) -> None:
        self.flush_count += 1


class _KeyRelease:
    def __init__(self) -> None:
        self.accepted = False

    def isAutoRepeat(self) -> bool:
        return False

    def key(self) -> int:
        return int(Qt.Key_W)

    def nativeScanCode(self) -> int:
        return 17

    def text(self) -> str:
        return "w"

    def modifiers(self) -> Qt.KeyboardModifiers:
        return Qt.KeyboardModifiers()

    def accept(self) -> None:
        self.accepted = True

    def ignore(self) -> None:
        self.accepted = False


@dataclass(frozen=True)
class _ScheduledCall:
    delay_ms: int
    callback: object


def _capture_single_shots(
    monkeypatch: pytest.MonkeyPatch,
    timeline: list[tuple[str, object]] | None = None,
) -> list[_ScheduledCall]:
    scheduled: list[_ScheduledCall] = []

    def single_shot(delay_ms: int, callback: object) -> None:
        scheduled.append(_ScheduledCall(int(delay_ms), callback))
        if timeline is not None:
            timeline.append(("timer", int(delay_ms)))

    monkeypatch.setattr(
        joystick_module,
        "QTimer",
        SimpleNamespace(singleShot=single_shot),
    )
    return scheduled


def _widget(serial_connection: _Serial | None = None) -> JoystickWindow:
    widget = JoystickWindow.__new__(JoystickWindow)
    widget.serial_connection = serial_connection or _Serial("COM-OLD")
    widget.stage_controller = None
    widget._active_axes = (("X", 1),)
    widget._active_jog_projection_lease = None
    widget._pending_jog_axes = None
    widget._key_stack = []
    widget._key_press_times = {}
    widget._pending_key_activations = {}
    widget._jog_stop_resend_generation = 0
    widget._jog_state_sync_timer = _Timer()
    widget._axis_a_ready = True
    widget._motion_safety_disabled = False
    widget._control_mode = JoystickWindow.MODE_JOG
    widget._relative_motion_projector = None
    widget._linear_jog_distance_mm = 25.0
    widget._rotary_jog_distance_deg = 5.0
    widget._manual_axis_distance_mm = 1.0
    widget._move_safety_check = lambda: True
    widget._feedrate_for_axes = lambda _axes: 20.0
    widget._sync_physical_key_watchdog = lambda: None
    widget._clear_pending_key_activations = lambda: None
    widget._stop_homing_animation = lambda _axis: None
    widget._update_enabled_state = lambda: None
    widget._show_warning = lambda _message: None
    widget._pending_homing_axes = set()
    widget.status_label = _Label()
    widget.jog_command_changed = _SignalRecorder()
    widget.jog_stopped = _SignalRecorder()
    return widget


def _run(call: _ScheduledCall) -> None:
    callback = call.callback
    assert callable(callback)
    callback()


def test_final_key_release_sends_immediate_stop_and_one_120_ms_resend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    timeline: list[tuple[str, object]] = []
    serial_connection = _Serial("COM-OLD", timeline)
    widget = _widget(serial_connection)
    identifier = ("scan", (17, 0))
    widget._key_stack = [identifier]
    widget._key_press_times = {identifier: 1.0}
    widget._mapping_from_event = lambda _event: (identifier, ("X", 1))
    scheduled = _capture_single_shots(monkeypatch, timeline)
    event = _KeyRelease()

    handled = JoystickWindow._handle_key_release_event(widget, event)

    assert handled is True
    assert event.accepted is True
    assert serial_connection.writes == [b"\x85"]
    assert [call.delay_ms for call in scheduled] == [120]
    assert timeline == [("write", b"\x85"), ("timer", 120)]

    _run(scheduled[0])

    assert serial_connection.writes == [b"\x85", b"\x85"]


def test_stop_resend_contract_has_one_named_120_ms_delay() -> None:
    assert JoystickWindow.JOG_STOP_RESEND_DELAY_MS == 120
    assert not hasattr(JoystickWindow, "JOG_STOP_RESEND_DELAYS_MS")


def test_old_stop_callback_is_obsolete_after_a_new_jog_is_accepted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    serial_connection = _Serial("COM-OLD")
    widget = _widget(serial_connection)
    scheduled = _capture_single_shots(monkeypatch)
    JoystickWindow.stop_jog(widget)
    assert serial_connection.writes == [b"\x85"]

    JoystickWindow._apply_axes(widget, (("X", 1),))
    assert serial_connection.writes[-1].startswith(b"$J=")
    widget._active_axes = None
    widget._pending_jog_axes = None
    _run(scheduled[0])

    assert serial_connection.writes.count(b"\x85") == 1


def test_old_stop_callback_cannot_target_a_replacement_serial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = _Serial("COM-OLD")
    replacement = _Serial("COM-NEW")
    widget = _widget(original)
    scheduled = _capture_single_shots(monkeypatch)
    JoystickWindow.stop_jog(widget)

    JoystickWindow.set_serial(widget, replacement)
    _run(scheduled[0])

    assert original.writes == [b"\x85"]
    assert replacement.writes == []


def test_stop_callback_requires_the_exact_serial_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = _Serial("COM-OLD")
    replacement = _Serial("COM-NEW")
    widget = _widget(original)
    scheduled = _capture_single_shots(monkeypatch)
    JoystickWindow.stop_jog(widget)
    widget.serial_connection = replacement

    _run(scheduled[0])

    assert original.writes == [b"\x85"]
    assert replacement.writes == []


def test_reenabling_a_axis_invalidates_the_disabled_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    serial_connection = _Serial("COM-OLD")
    widget = _widget(serial_connection)
    scheduled = _capture_single_shots(monkeypatch)
    JoystickWindow.stop_jog(widget)

    JoystickWindow.set_axis_a_ready(widget, False)
    JoystickWindow.set_axis_a_ready(widget, True)
    _run(scheduled[0])

    assert serial_connection.writes == [b"\x85"]


@pytest.mark.parametrize(
    "pending_kind", ["held", "pending_axes", "pending_key", "active"]
)
def test_resend_requires_no_held_pending_or_active_input(
    monkeypatch: pytest.MonkeyPatch,
    pending_kind: str,
) -> None:
    serial_connection = _Serial("COM-OLD")
    widget = _widget(serial_connection)
    widget._active_axes = None
    scheduled = _capture_single_shots(monkeypatch)
    JoystickWindow._schedule_jog_stop_resend(widget)
    if pending_kind == "held":
        widget._key_stack = [("scan", (17, 0))]
    elif pending_kind == "pending_axes":
        widget._pending_jog_axes = (("X", 1),)
    elif pending_kind == "pending_key":
        widget._pending_key_activations = {("scan", (17, 0)): object()}
    else:
        widget._active_axes = (("X", 1),)

    _run(scheduled[0])

    assert serial_connection.writes == []


def test_newest_stop_supersedes_the_previous_resend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    serial_connection = _Serial("COM-OLD")
    widget = _widget(serial_connection)
    widget._active_axes = None
    scheduled = _capture_single_shots(monkeypatch)

    JoystickWindow._schedule_jog_stop_resend(widget)
    JoystickWindow._schedule_jog_stop_resend(widget)
    _run(scheduled[0])
    _run(scheduled[1])

    assert serial_connection.writes == [b"\x85"]


def test_stop_without_active_jog_sends_and_schedules_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    serial_connection = _Serial("COM-OLD")
    widget = _widget(serial_connection)
    widget._active_axes = None
    scheduled = _capture_single_shots(monkeypatch)

    JoystickWindow.stop_jog(widget)

    assert serial_connection.writes == []
    assert scheduled == []


def test_failed_immediate_stop_write_detaches_without_scheduling_resend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _FailingSerial(_Serial):
        def write(self, payload: bytes) -> None:
            self.writes.append(bytes(payload))
            raise joystick_module.serial.SerialException("connection lost")

    serial_connection = _FailingSerial("COM-OLD")
    widget = _widget(serial_connection)
    scheduled = _capture_single_shots(monkeypatch)

    JoystickWindow.stop_jog(widget)

    assert serial_connection.writes == [b"\x85"]
    assert widget.serial_connection is None
    assert scheduled == []


def test_stop_path_routes_only_realtime_bytes_without_status_io(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Controller:
        def __init__(self) -> None:
            self.commands: list[tuple[object, str]] = []

        def queue_outbound_command(
            self,
            command: object,
            *,
            source: str = "unknown",
        ) -> bool:
            self.commands.append((command, source))
            return True

        def __getattr__(self, name: str) -> object:
            raise AssertionError(f"unexpected controller status access: {name}")

    serial_connection = _Serial("COM-OLD")
    widget = _widget(serial_connection)
    controller = _Controller()
    widget.stage_controller = controller
    scheduled = _capture_single_shots(monkeypatch)

    JoystickWindow.stop_jog(widget)
    _run(scheduled[0])

    assert controller.commands == [
        (b"\x85", "joystick_reset_button"),
        (b"\x85", "joystick_reset_button"),
    ]
    assert serial_connection.writes == []
