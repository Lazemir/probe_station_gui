"""Exercise dirty joystick key transitions without moving hardware.

The test drives JoystickWindow through its normal key handling path while using a
fake serial connection. It verifies that noisy opposite-direction key events do
not emit jog commands in the wrong direction.
"""

from __future__ import annotations

import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from probe_station_gui.settings_manager import KeyBinding
from probe_station_gui.views.joystick_window import JoystickWindow


KEYS = {
    "W": {"key": 87, "scan": 17, "text": "w"},
    "S": {"key": 83, "scan": 31, "text": "s"},
    "A": {"key": 65, "scan": 30, "text": "a"},
    "D": {"key": 68, "scan": 32, "text": "d"},
}
SCAN_TO_KEY = {int(value["scan"]): name for name, value in KEYS.items()}
QT_KEY_TO_KEY = {int(value["key"]): name for name, value in KEYS.items()}

CONTROLS = {
    "move_y_positive": [
        KeyBinding(qt_key=87, modifiers=0, native_scan_code=17, text="w")
    ],
    "move_y_negative": [
        KeyBinding(qt_key=83, modifiers=0, native_scan_code=31, text="s")
    ],
    "move_x_negative": [
        KeyBinding(qt_key=65, modifiers=0, native_scan_code=30, text="a")
    ],
    "move_x_positive": [
        KeyBinding(qt_key=68, modifiers=0, native_scan_code=32, text="d")
    ],
}


class FakeSerial:
    def __init__(self) -> None:
        self.is_open = True
        self.port = "FAKE"
        self.baudrate = 115200
        self.writes: list[bytes] = []

    def write(self, payload: bytes) -> None:
        self.writes.append(bytes(payload))

    def flush(self) -> None:
        return None


class FakeKeyEvent:
    def __init__(self, name: str, *, auto_repeat: bool = False) -> None:
        key = KEYS[name]
        self._key = int(key["key"])
        self._scan = int(key["scan"])
        self._text = str(key["text"])
        self._auto_repeat = bool(auto_repeat)
        self.accepted = False

    def key(self) -> int:
        return self._key

    def nativeScanCode(self) -> int:
        return self._scan

    def text(self) -> str:
        return self._text

    def modifiers(self) -> Qt.KeyboardModifiers:
        return Qt.KeyboardModifiers()

    def isAutoRepeat(self) -> bool:
        return self._auto_repeat

    def accept(self) -> None:
        self.accepted = True

    def ignore(self) -> None:
        self.accepted = False


@dataclass
class Harness:
    app: QApplication
    joystick: JoystickWindow
    serial: FakeSerial
    physical_keys: set[str]

    @classmethod
    def create(cls) -> "Harness":
        app = QApplication.instance() or QApplication([])
        joystick = JoystickWindow()
        serial = FakeSerial()
        joystick.set_serial(serial)
        joystick.apply_control_bindings(CONTROLS)
        joystick.apply_jog_settings(
            linear_distance_mm=25.0,
            rotary_distance_deg=5.0,
            motion_safety_disabled=True,
        )
        harness = cls(app=app, joystick=joystick, serial=serial, physical_keys=set())

        def physical_key_is_down(identifier) -> bool | None:
            kind, value = identifier
            if not isinstance(value, tuple) or not value:
                return None
            if kind == "scan":
                return SCAN_TO_KEY.get(int(value[0] or 0)) in harness.physical_keys
            if kind == "key":
                return QT_KEY_TO_KEY.get(int(value[0] or 0)) in harness.physical_keys
            return None

        joystick._physical_key_is_down = physical_key_is_down  # type: ignore[method-assign]
        return harness

    def reset(self) -> None:
        self.joystick._jog_state_sync_timer.stop()
        self.joystick._physical_key_watchdog_timer.stop()
        self.joystick._pending_jog_axes = None
        self.joystick._key_stack.clear()
        self.joystick._key_press_times.clear()
        self.joystick._clear_pending_key_activations()
        self.joystick._active_axes = None
        self.joystick._jog_stop_resend_generation += 1
        self.physical_keys.clear()
        self.serial.writes.clear()
        self.pump(20)

    def press(self, key: str, *, auto_repeat: bool = False) -> None:
        self.physical_keys.add(key)
        self.joystick._handle_key_press_event(
            FakeKeyEvent(key, auto_repeat=auto_repeat)
        )

    def release(self, key: str, *, auto_repeat: bool = False) -> None:
        if not auto_repeat:
            self.physical_keys.discard(key)
        self.joystick._handle_key_release_event(
            FakeKeyEvent(key, auto_repeat=auto_repeat)
        )

    def physical_release_without_event(self, key: str) -> None:
        self.physical_keys.discard(key)

    def pump(self, ms: int) -> None:
        deadline = time.monotonic() + (ms / 1000.0)
        while time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.002)
        self.app.processEvents()

    def jog_commands(self) -> list[str]:
        commands: list[str] = []
        for payload in self.serial.writes:
            if payload == b"\x85":
                continue
            try:
                command = payload.decode("ascii").strip()
            except UnicodeDecodeError:
                continue
            if command.startswith("$J="):
                commands.append(command)
        return commands

    def jog_commands_since(self, start_index: int) -> list[str]:
        return self.jog_commands()[start_index:]

    def stop_commands(self) -> list[bytes]:
        return [payload for payload in self.serial.writes if payload == b"\x85"]


def command_axes(command: str) -> dict[str, float]:
    axes: dict[str, float] = {}
    for axis, value in re.findall(r"\b([XYZABC])(-?\d+(?:\.\d+)?)", command):
        axes[axis] = float(value)
    return axes


def assert_no_wrong_x(commands: list[str], *, label: str) -> None:
    wrong = [
        command
        for command in commands
        if command_axes(command).get("X", 0.0) < 0.0
    ]
    if wrong:
        raise AssertionError(f"{label}: emitted wrong left jog command: {wrong}")


def assert_contains(commands: list[str], expected_axes: dict[str, float], *, label: str) -> None:
    for command in commands:
        axes = command_axes(command)
        if all(abs(axes.get(axis, 0.0) - value) <= 1e-9 for axis, value in expected_axes.items()):
            return
    raise AssertionError(f"{label}: expected axes {expected_axes}, got {commands}")


def validate_commands_against_pressed_keys(
    commands: list[str],
    pressed: set[str],
    active_signs: dict[str, int],
    *,
    label: str,
) -> None:
    for command in commands:
        axes = command_axes(command)
        for axis, value in axes.items():
            sign = 1 if value > 0 else -1 if value < 0 else 0
            if sign:
                active_signs[axis] = sign
        for axis in list(active_signs):
            if axis not in axes:
                active_signs.pop(axis)

        x_sign = active_signs.get("X")
        y_sign = active_signs.get("Y")
        if "D" in pressed and "A" not in pressed and x_sign is not None and x_sign < 0:
            raise AssertionError(f"{label}: D held but command moved left: {command}")
        if "A" in pressed and "D" not in pressed and x_sign is not None and x_sign > 0:
            raise AssertionError(f"{label}: A held but command moved right: {command}")
        if "W" in pressed and "S" not in pressed and y_sign is not None and y_sign < 0:
            raise AssertionError(f"{label}: W held but command moved down: {command}")
        if "S" in pressed and "W" not in pressed and y_sign is not None and y_sign > 0:
            raise AssertionError(f"{label}: S held but command moved up: {command}")


def run_replay(
    harness: Harness,
    events: list[tuple[int, str, str]],
    *,
    label: str,
) -> list[str]:
    harness.reset()
    pressed: set[str] = set()
    active_signs: dict[str, int] = {}
    command_index = len(harness.jog_commands())
    previous_at_ms = 0

    for at_ms, action, key in events:
        delay = max(0, int(at_ms) - previous_at_ms)
        if delay:
            harness.pump(delay)
            validate_commands_against_pressed_keys(
                harness.jog_commands_since(command_index),
                pressed,
                active_signs,
                label=label,
            )
            command_index = len(harness.jog_commands())
        previous_at_ms = int(at_ms)

        if action == "press":
            pressed.add(key)
            harness.press(key)
        elif action == "release":
            pressed.discard(key)
            harness.release(key)
        else:
            raise ValueError(f"Unknown replay action: {action}")

        validate_commands_against_pressed_keys(
            harness.jog_commands_since(command_index),
            pressed,
            active_signs,
            label=label,
        )
        command_index = len(harness.jog_commands())

    harness.pump(320)
    validate_commands_against_pressed_keys(
        harness.jog_commands_since(command_index),
        pressed,
        active_signs,
        label=label,
    )
    return harness.jog_commands()


def run_dirty_wd_with_ghost_a(harness: Harness) -> None:
    harness.reset()
    harness.press("W")
    harness.press("D")
    harness.pump(140)
    harness.press("A")
    harness.pump(180)
    harness.release("A")
    harness.pump(280)
    harness.release("W")
    harness.release("D")
    harness.pump(80)
    commands = harness.jog_commands()
    assert_contains(commands, {"X": 25.0, "Y": 25.0}, label="WD baseline")
    assert_no_wrong_x(commands, label="WD with ghost A")


def run_dirty_wd_auto_repeat(harness: Harness) -> None:
    harness.reset()
    harness.press("W")
    harness.press("D")
    harness.pump(140)
    for _ in range(8):
        harness.release("D", auto_repeat=True)
        harness.press("D", auto_repeat=True)
        harness.release("W", auto_repeat=True)
        harness.press("W", auto_repeat=True)
        harness.pump(25)
    harness.release("W")
    harness.release("D")
    harness.pump(80)
    commands = harness.jog_commands()
    assert_contains(commands, {"X": 25.0, "Y": 25.0}, label="WD auto-repeat")
    assert_no_wrong_x(commands, label="WD auto-repeat")


def run_dirty_wd_to_wa_to_wd(harness: Harness) -> None:
    harness.reset()
    harness.press("W")
    harness.press("D")
    harness.pump(140)
    harness.press("A")
    harness.pump(120)
    harness.release("D")
    harness.pump(280)
    harness.press("D")
    harness.pump(120)
    harness.release("A")
    harness.pump(280)
    harness.release("W")
    harness.release("D")
    harness.pump(80)
    commands = harness.jog_commands()
    assert_contains(commands, {"X": 25.0, "Y": 25.0}, label="WD restored")


def run_log_replay_1648(harness: Harness) -> None:
    events = [
        (0, "press", "D"),
        (223, "press", "S"),
        (256, "release", "D"),
        (542, "press", "A"),
        (598, "release", "A"),
        (1295, "press", "A"),
        (1422, "release", "S"),
        (1858, "press", "W"),
        (1939, "release", "A"),
        (2219, "press", "D"),
        (2310, "release", "W"),
        (2715, "press", "S"),
        (2814, "release", "D"),
        (3114, "press", "A"),
        (3166, "release", "S"),
        (3414, "press", "W"),
        (3454, "release", "A"),
        (3710, "press", "D"),
        (3867, "release", "W"),
        (4294, "press", "S"),
        (4334, "release", "D"),
        (4438, "press", "A"),
        (4486, "release", "S"),
        (4866, "press", "D"),
        (4950, "release", "A"),
        (5486, "press", "A"),
        (5590, "release", "D"),
        (5686, "press", "W"),
        (5866, "release", "A"),
        (5918, "press", "D"),
        (10394, "release", "W"),
        (10403, "release", "D"),
    ]
    commands = run_replay(harness, events, label="16:48 log replay")
    assert_contains(commands, {"X": 25.0, "Y": 25.0}, label="16:48 log replay")


def run_log_replay_1447_lost_d_release(harness: Harness) -> None:
    harness.reset()
    harness.press("W")
    harness.press("D")
    harness.pump(220)
    assert_contains(
        harness.jog_commands(),
        {"X": 25.0, "Y": 25.0},
        label="14:47 diagonal setup",
    )

    command_index = len(harness.jog_commands())
    stop_index = len(harness.stop_commands())
    harness.physical_release_without_event("D")
    harness.release("W")
    harness.pump(420)

    commands_after_release = harness.jog_commands_since(command_index)
    if commands_after_release:
        raise AssertionError(
            "14:47 lost D release: emitted jog command after all physical keys "
            f"were released: {commands_after_release}"
        )
    if len(harness.stop_commands()) <= stop_index:
        raise AssertionError("14:47 lost D release: no jog stop was emitted")
    if harness.joystick._key_stack:
        raise AssertionError(
            f"14:47 lost D release: stale key stack {harness.joystick._key_stack}"
        )
    if harness.joystick._active_axes is not None:
        raise AssertionError(
            f"14:47 lost D release: active axes remained {harness.joystick._active_axes}"
        )


def main() -> int:
    harness = Harness.create()
    scenarios = (
        ("WD with ghost A", run_dirty_wd_with_ghost_a),
        ("WD auto-repeat", run_dirty_wd_auto_repeat),
        ("WD -> WA -> WD", run_dirty_wd_to_wa_to_wd),
        ("16:48 log replay", run_log_replay_1648),
        ("14:47 lost D release replay", run_log_replay_1447_lost_d_release),
    )
    for label, scenario in scenarios:
        scenario(harness)
        print(f"PASS {label}")
    print("PASS all dirty jog transition checks from zero key state")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
