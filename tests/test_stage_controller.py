import importlib.util
import math
import sys
import threading
import time
import types
import unittest
from pathlib import Path

import numpy as np


def _install_pyside6_stubs() -> None:
    qtcore = types.ModuleType("PySide6.QtCore")
    qtgui = types.ModuleType("PySide6.QtGui")

    class QObject:  # noqa: N801 - mimic Qt type name
        pass

    class Signal:  # noqa: N801 - mimic Qt type name
        def __init__(self, *args, **kwargs) -> None:
            pass

        def emit(self, *args, **kwargs) -> None:
            pass

    class Qt:  # noqa: N801 - mimic Qt namespace
        KeyboardModifier = int
        KeyboardModifiers = int
        Key_Left = 16777234
        Key_Up = 16777235
        Key_Right = 16777236
        Key_Down = 16777237
        Key_Space = 32
        Key_Tab = 16777217
        Key_Return = 16777220
        Key_Enter = 16777221

    class QImage:  # noqa: N801 - mimic Qt type name
        pass

    qtcore.QObject = QObject
    qtcore.Signal = Signal
    qtcore.Qt = Qt
    qtgui.QImage = QImage

    pyside6 = types.ModuleType("PySide6")
    sys.modules["PySide6"] = pyside6
    sys.modules["PySide6.QtCore"] = qtcore
    sys.modules["PySide6.QtGui"] = qtgui

    if "cv2" not in sys.modules:
        cv2_stub = types.ModuleType("cv2")
        cv2_stub.CV_64F = 0

        def _laplacian(*_args, **_kwargs):  # pragma: no cover - stub
            return 0

        cv2_stub.Laplacian = _laplacian
        sys.modules["cv2"] = cv2_stub

    if "serial" not in sys.modules:
        serial_stub = types.ModuleType("serial")
        serial_stub.Serial = object
        serial_stub.SerialException = Exception
        sys.modules["serial"] = serial_stub


def _load_stage_controller():
    _install_pyside6_stubs()
    module_path = Path(__file__).resolve().parents[1] / "probe_station_gui" / "stage_controller.py"
    spec = importlib.util.spec_from_file_location("stage_controller_test", module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _load_settings_manager():
    module_path = Path(__file__).resolve().parents[1] / "probe_station_gui" / "settings_manager.py"
    spec = importlib.util.spec_from_file_location("settings_manager_stage_test", module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_stage_controller_module = _load_stage_controller()
_settings_manager_module = _load_settings_manager()
StageController = _stage_controller_module.StageController
StageControllerError = _stage_controller_module.StageControllerError
QueuedSerialWrite = _stage_controller_module._QueuedSerialWrite
MoveVector = _stage_controller_module.MoveVector
AxisACalibrationSettings = _settings_manager_module.AxisACalibrationSettings
AxisZCalibrationSettings = _settings_manager_module.AxisZCalibrationSettings
FocusSweepResult = _stage_controller_module._FocusSweepResult
AutofocusContext = _stage_controller_module._AutofocusContext


class StageControllerStartupLimitsTest(unittest.TestCase):
    def test_parse_startup_limits(self) -> None:
        lines = [
            "?<Idle|MPos:0.000,0.000,9.520,0.000,0.000|FS:0,0|WCO:0.000,0.000,0.000,0.000,0.000>",
            "[0xc]$Build/Info",
            "[VER:4.0 FluidNC v4.0.1:]",
            "[MSG:INFO: Axis X (0.000,64.000)]",
            "[MSG:INFO: Axis Y (0.000,64.000)]",
            "[MSG:INFO: Axis Z (0.000,20.000)]",
            "[MSG:INFO: Axis A (-0.100,0.000)]",
            "[MSG:INFO: Axis B (-1000.000,0.000)]",
            "ok",
        ]

        limits = StageController._parse_startup_limits(lines)

        self.assertEqual(limits.get("X"), (0.0, 64.0))
        self.assertEqual(limits.get("Y"), (0.0, 64.0))
        self.assertEqual(limits.get("Z"), (0.0, 20.0))
        self.assertEqual(limits.get("A"), (-0.1, 0.0))
        self.assertEqual(limits.get("B"), (-1000.0, 0.0))

    def test_relative_software_limits_apply_per_homed_axis(self) -> None:
        controller = StageController()
        controller._axis_limits = {"X": (0.0, 10.0), "Y": (0.0, 10.0)}
        controller._query_status = lambda _serial: types.SimpleNamespace(
            state="Idle",
            position=None,
            display_position=(9.0, 0.0, 0.0),
            work_position=(9.0, 0.0, 0.0),
            work_offset=(0.0, 0.0, 0.0),
            homed_axes={"X"},
        )
        controller._ensure_b_axis_zero_reference = lambda _status: None

        controller._check_relative_move_limits(
            _FakeSerial(), MoveVector(y=15.0), allow_relative=True
        )

        with self.assertRaises(StageControllerError):
            controller._check_relative_move_limits(
                _FakeSerial(), MoveVector(x=5.0), allow_relative=True
            )

    def test_constrain_jog_distances_clips_homed_axis_to_soft_limit(self) -> None:
        controller = StageController()
        controller._position_reporting_mode = "machine"
        controller._axis_limits = {"Z": (0.0, 20.0)}
        controller._last_stage_position = (0.0, 0.0, 1.0)
        controller._homed_axes = {"Z"}
        messages = []
        controller.status_message = types.SimpleNamespace(
            emit=lambda message: messages.append(message)
        )

        constrained = controller.constrain_jog_distances((("Z", -25.0),))

        self.assertEqual(constrained, (("Z", -1.0),))
        self.assertTrue(any("soft limit" in message for message in messages))

    def test_queue_jog_command_rejects_unclipped_soft_limit_move(self) -> None:
        controller = StageController()
        controller._position_reporting_mode = "machine"
        controller._axis_limits = {"Z": (0.0, 20.0)}
        controller._last_stage_position = (0.0, 0.0, 0.0)
        controller._homed_axes = {"Z"}

        with self.assertRaises(StageControllerError):
            controller.queue_jog_command("$J=G91 G21 Z-25.000 F10")

        self.assertFalse(controller._jog_motion_active)

    def test_constrain_jog_distances_does_not_blanket_block_unknown_position(self) -> None:
        controller = StageController()
        controller._position_reporting_mode = "machine"
        controller._axis_limits = {"Z": (0.0, 20.0)}
        controller._last_stage_position = None
        controller._homed_axes = {"Z"}

        constrained = controller.constrain_jog_distances((("Z", 250.0),))

        self.assertEqual(constrained, (("Z", 250.0),))

    def test_queue_jog_command_rewrites_known_relative_jog_to_absolute_target(self) -> None:
        controller = StageController()
        serial_connection = _WritableFakeSerial()
        try:
            controller._serial = serial_connection
            controller._position_reporting_mode = "machine"
            controller._axis_limits = {"Z": (0.0, 20.0)}
            controller._last_stage_position = (0.0, 0.0, 1.0)
            controller._homed_axes = {"Z"}

            controller.queue_jog_command("$J=G91 G21 Z5.000 F10")
            controller._async_write_queue.join()

            self.assertEqual(
                serial_connection.writes,
                [b"$J=G90 G21 G53 Z6.0000 F10\n"],
            )
        finally:
            controller.shutdown()

    def test_queue_jog_command_allows_unknown_position_without_blanket_block(self) -> None:
        controller = StageController()
        try:
            controller._position_reporting_mode = "machine"
            controller._axis_limits = {"Z": (0.0, 20.0)}
            controller._last_stage_position = None
            controller._homed_axes = {"Z"}

            controller.queue_jog_command("$J=G91 G21 Z250.000 F10")

            self.assertTrue(controller._jog_motion_active)
        finally:
            controller.shutdown()

    def test_absolute_axis_move_respects_homed_axis_soft_limit(self) -> None:
        controller = StageController()
        controller._serial = _FakeSerial()
        controller._position_reporting_mode = "machine"
        controller._axis_limits = {"Z": (0.0, 20.0), "X": (0.0, 64.0)}
        controller._ensure_axis_limits = lambda _serial, **_kwargs: None
        controller._query_status = lambda _serial: types.SimpleNamespace(
            state="Idle",
            position=(0.0, 0.0, 1.0),
            work_position=(0.0, 0.0, 1.0),
            display_position=(0.0, 0.0, 1.0),
            work_offset=(0.0, 0.0, 0.0),
            homed_axes={"Z"},
        )
        commands = []
        controller._write_command = lambda _serial, command: commands.append(command)
        controller._wait_for_ok = lambda *_args, **_kwargs: None
        controller._wait_for_idle = lambda *_args, **_kwargs: None

        with self.assertRaises(StageControllerError):
            controller._send_absolute_axis_move(
                controller._serial,
                "Z",
                -0.1,
                ignore_needle_safety=True,
                allow_unhomed=True,
            )

        self.assertFalse(any(command.startswith("G1 Z") for command in commands))

    def test_feed_override_payload_uses_realtime_grbl_steps(self) -> None:
        payload, applied = StageController._feed_override_payload_for_percent_change(
            100,
            137,
        )

        self.assertEqual(applied, 137)
        self.assertEqual(payload, b"\x91\x91\x91" + b"\x93" * 7)

        payload, applied = StageController._feed_override_payload_for_percent_change(
            137,
            82,
        )

        self.assertEqual(applied, 82)
        self.assertEqual(payload, b"\x92" * 5 + b"\x94" * 5)

    def test_feed_override_percent_is_clamped_to_controller_range(self) -> None:
        self.assertEqual(
            StageController.feed_override_percent_for_feedrates(100.0, 250.0),
            200,
        )
        self.assertEqual(
            StageController.feed_override_percent_for_feedrates(100.0, 1.0),
            10,
        )

    def test_active_needles_feedrate_queues_realtime_override(self) -> None:
        controller = StageController()
        serial_connection = _WritableFakeSerial()
        try:
            controller._serial = serial_connection
            controller._active_needles_action = "lower"
            controller._active_needles_programmed_feedrate = 100.0
            controller._active_feed_override_percent = 100

            applied = controller.queue_active_needles_feedrate(150.0)
            controller._async_write_queue.join()

            self.assertEqual(applied, 150)
            self.assertEqual(serial_connection.writes, [b"\x91" * 5])
        finally:
            controller.shutdown()


class _FakeSerial:
    def __init__(self) -> None:
        self.is_open = True
        self.probe_station_reboot_detected = False


class _WritableFakeSerial(_FakeSerial):
    def __init__(self) -> None:
        super().__init__()
        self.writes: list[bytes] = []

    def write(self, payload: bytes) -> None:
        self.writes.append(payload)

    def flush(self) -> None:
        return None


class _LineFakeSerial(_WritableFakeSerial):
    def __init__(self, lines: list[bytes]) -> None:
        super().__init__()
        self.lines = list(lines)

    def readline(self) -> bytes:
        if self.lines:
            return self.lines.pop(0)
        time.sleep(0.01)
        return b""


class _TrackingLock:
    def __init__(self) -> None:
        self.depth = 0
        self.max_depth = 0

    @property
    def held(self) -> bool:
        return self.depth > 0

    def acquire(self, blocking: bool = True, timeout: float = -1.0) -> bool:
        self.depth += 1
        self.max_depth = max(self.max_depth, self.depth)
        return True

    def release(self) -> None:
        if self.depth <= 0:
            raise RuntimeError("lock released while not held")
        self.depth -= 1

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *_args) -> None:
        self.release()


class _LockedLineFakeSerial(_LineFakeSerial):
    def __init__(self, lines: list[bytes], lock: _TrackingLock) -> None:
        super().__init__(lines)
        self.lock = lock

    def write(self, payload: bytes) -> None:
        if not self.lock.held:
            raise AssertionError("serial write without session lock")
        super().write(payload)

    def readline(self) -> bytes:
        if not self.lock.held:
            raise AssertionError("serial read without session lock")
        return super().readline()


class _BufferedFakeSerial(_WritableFakeSerial):
    def __init__(self, data: bytes) -> None:
        super().__init__()
        self.data = bytearray(data)

    @property
    def in_waiting(self) -> int:
        return len(self.data)

    def read(self, size: int) -> bytes:
        chunk = bytes(self.data[:size])
        del self.data[:size]
        return chunk


class StageControllerAbsoluteMoveTest(unittest.TestCase):
    def test_relative_move_callback_runs_after_g1_is_accepted(self) -> None:
        controller = StageController()
        serial_connection = _LineFakeSerial([b"ok\n", b"ok\n", b"ok\n", b"ok\n"])
        callback_writes: list[list[bytes]] = []

        controller._move_safety_check = lambda: None
        controller._ensure_axis_limits = lambda _serial, **_kwargs: None
        controller._check_relative_move_limits = lambda *_args, **_kwargs: None
        controller._reset_feed_override_for_serial = lambda _serial: None

        controller._send_relative_move(
            serial_connection,
            MoveVector(x=0.1),
            wait_for_completion=False,
            motion_started_callback=lambda _move, _feedrate: callback_writes.append(
                list(serial_connection.writes)
            ),
        )

        self.assertEqual(len(callback_writes), 1)
        self.assertTrue(callback_writes[0][-1].startswith(b"G1 X0.1000"))
        self.assertFalse(any(command == b"G90\n" for command in callback_writes[0]))
        self.assertEqual(serial_connection.writes[-1], b"G90\n")

    def test_absolute_xy_move_uses_relative_delta(self) -> None:
        controller = StageController()
        controller._serial = _FakeSerial()
        controller._position_reporting_mode = "machine"

        sent_moves = []
        statuses = [
            types.SimpleNamespace(
                state="Idle",
                position=(10.0, 20.0, 0.0),
                display_position=(10.0, 20.0, 0.0),
                homed_axes={"X", "Y"},
            ),
            types.SimpleNamespace(
                state="Idle",
                position=(15.0, 26.0, 0.0),
                display_position=(15.0, 26.0, 0.0),
                homed_axes={"X", "Y"},
            ),
        ]

        controller._move_safety_check = lambda: None
        controller._wait_for_idle = lambda _serial: None
        controller._query_status = lambda _serial: statuses.pop(0)
        started_moves = []

        def send_relative_move(_serial, move, **kwargs) -> None:
            sent_moves.append(move)
            callback = kwargs.get("motion_started_callback")
            if callback is not None:
                callback(move, 600.0)

        controller._send_relative_move = send_relative_move
        controller.absolute_xy_move_started = types.SimpleNamespace(
            emit=lambda *args: started_moves.append(args)
        )
        controller.movement_started = types.SimpleNamespace(emit=lambda *args, **kwargs: None)
        controller.status_message = types.SimpleNamespace(emit=lambda *args, **kwargs: None)
        movement_results = []
        controller.movement_finished = types.SimpleNamespace(
            emit=lambda success, message: movement_results.append((success, message))
        )
        controller.stage_position_changed = types.SimpleNamespace(
            emit=lambda *args, **kwargs: None
        )

        controller._run_move_to_xy(15.0, 26.0)

        self.assertEqual(len(sent_moves), 1)
        self.assertAlmostEqual(sent_moves[0].x, 5.0)
        self.assertAlmostEqual(sent_moves[0].y, 6.0)
        self.assertEqual(started_moves, [(15.0, 26.0, 600.0)])
        self.assertEqual(movement_results[-1][0], True)
        self.assertIn("Arrived", movement_results[-1][1])

    def test_absolute_xy_move_uses_work_basis_and_ignores_machine_position(self) -> None:
        controller = StageController()
        controller._serial = _FakeSerial()
        controller._position_reporting_mode = "work"
        controller._active_work_coordinate_system = "G54"
        controller._controller_coordinate_offsets["G54"] = (
            32.0,
            32.0,
            0.0,
            0.0,
            0.0,
        )

        sent_moves = []
        statuses = [
            types.SimpleNamespace(
                state="Idle",
                position=(999.0, 999.0, 9.244, 0.0, 2.17),
                display_position=(4.984, 3.705, 9.244, 0.0, 2.17),
                work_position=(4.984, 3.705, 9.244, 0.0, 2.17),
                work_offset=(32.0, 32.0, 0.0, 0.0, 0.0),
                coordinate_system="G54",
                homed_axes={"X", "Y"},
            ),
            types.SimpleNamespace(
                state="Idle",
                position=(888.0, 888.0, 9.244, 0.0, 2.17),
                display_position=(29.887, 26.689, 9.244, 0.0, 2.17),
                work_position=(29.887, 26.689, 9.244, 0.0, 2.17),
                work_offset=(32.0, 32.0, 0.0, 0.0, 0.0),
                coordinate_system="G54",
                homed_axes={"X", "Y"},
            ),
        ]

        controller._move_safety_check = lambda: None
        refresh_calls = []
        controller._refresh_coordinate_system_state = (
            lambda _serial, apply_preference=True: refresh_calls.append(
                apply_preference
            )
        )
        controller._wait_for_idle = lambda _serial: None
        controller._query_status = lambda _serial: statuses.pop(0)
        started_moves = []

        def send_relative_move(_serial, move, **kwargs) -> None:
            sent_moves.append(move)
            callback = kwargs.get("motion_started_callback")
            if callback is not None:
                callback(move, 600.0)

        controller._send_relative_move = send_relative_move
        controller.absolute_xy_move_started = types.SimpleNamespace(
            emit=lambda *args: started_moves.append(args)
        )
        controller.movement_started = types.SimpleNamespace(emit=lambda *args, **kwargs: None)
        controller.status_message = types.SimpleNamespace(emit=lambda *args, **kwargs: None)
        movement_results = []
        controller.movement_finished = types.SimpleNamespace(
            emit=lambda success, message: movement_results.append((success, message))
        )

        controller._run_move_to_xy(29.887, 26.689)

        self.assertEqual(len(sent_moves), 1)
        self.assertAlmostEqual(sent_moves[0].x, 24.903)
        self.assertAlmostEqual(sent_moves[0].y, 22.984)
        self.assertEqual(started_moves, [(29.887, 26.689, 600.0)])
        self.assertEqual(movement_results[-1][0], True)
        self.assertEqual(refresh_calls, [])

    def test_modal_state_query_waits_for_payload_when_ok_arrives_first(self) -> None:
        controller = StageController()
        serial_connection = _LineFakeSerial(
            [
                b"ok\n",
                b"[GC:G1 G54 G17 G21 G90]\n",
            ]
        )

        tokens = controller._query_modal_state_tokens(
            serial_connection,
            timeout=0.2,
        )

        self.assertIn("G54", tokens)
        self.assertEqual(serial_connection.writes, [b"$G\n"])

    def test_work_offset_query_ignores_stale_modal_response(self) -> None:
        controller = StageController()
        serial_connection = _LineFakeSerial(
            [
                b"[GC:G1 G54 G17 G21 G90]\n",
                b"ok\n",
                b"[G54:32.000,32.000,0.000,-2.908,0.000]\n",
                b"[G55:0.000,0.000,0.000,0.000,0.000]\n",
                b"ok\n",
            ]
        )

        offsets = controller._query_work_coordinate_offsets(
            serial_connection,
            timeout=0.5,
        )

        self.assertEqual(serial_connection.writes, [b"$#\n"])
        self.assertIn("G54", offsets)
        self.assertEqual(offsets["G54"], (32.0, 32.0, 0.0, -2.908, 0.0))

    def test_set_current_a_work_coordinate_zeroes_active_wcs(self) -> None:
        controller = StageController()
        serial_connection = _LineFakeSerial(
            [
                b"<Idle|WPos:0.000,0.000,0.000,-1.000,0.000|WCO:0.000,0.000,0.000,0.000,0.000|H:A>\n",
                b"ok\n",
                b"[G54:0.000,0.000,0.000,-1.000,0.000]\n",
                b"ok\n",
                b"<Idle|WPos:0.000,0.000,0.000,0.000,0.000|WCO:0.000,0.000,0.000,-1.000,0.000|H:A>\n",
            ]
        )
        controller._serial = serial_connection
        controller._current_status_report_mask = 2
        controller._position_reporting_mode = "work"
        controller._active_work_coordinate_system = "G54"

        controller.set_current_axis_work_coordinate("A", 0.0)

        self.assertIn(b"G10 L20 P1 A0\n", serial_connection.writes)
        self.assertEqual(
            controller._controller_coordinate_offsets["G54"],
            (0.0, 0.0, 0.0, -1.0, 0.0),
        )
        self.assertAlmostEqual(controller.latest_a_position(), 0.0)

    def test_query_axis_max_feedrates_reads_controller_config_dump(self) -> None:
        controller = StageController()
        serial_connection = _LineFakeSerial(
            [
                b"axes:\n",
                b"  x:\n",
                b"    max_rate_mm_per_min: 500\n",
                b"  a:\n",
                b"    max_rate_mm_per_min: 80\n",
                b"ok\n",
            ]
        )
        controller._serial = serial_connection

        rates = controller.query_axis_max_feedrates()

        self.assertEqual(rates, {"X": 500.0, "A": 80.0})
        self.assertEqual(controller._axis_max_feedrates, rates)
        self.assertEqual(serial_connection.writes, [b"$CD\n"])

    def test_query_axis_max_feedrates_ignores_leading_ok(self) -> None:
        controller = StageController()
        serial_connection = _LineFakeSerial(
            [
                b"ok\n",
                b"axes:\n",
                b"  x:\n",
                b"    max_rate_mm_per_min: 500\n",
                b"  z:\n",
                b"    max_rate_mm_per_min: 100\n",
                b"  a:\n",
                b"    max_rate_mm_per_min: 80\n",
                b"ok\n",
            ]
        )
        controller._serial = serial_connection

        rates = controller.query_axis_max_feedrates()

        self.assertEqual(rates, {"X": 500.0, "Z": 100.0, "A": 80.0})

    def test_max_feedrate_for_axes_uses_limiting_axis(self) -> None:
        controller = StageController()
        controller.apply_axis_max_feedrates({"X": 500.0, "Y": 400.0, "Z": 100.0})

        self.assertEqual(controller.max_feedrate_for_axes(("X", "Y")), 400.0)
        self.assertEqual(controller.max_feedrate_for_axes(("Z",)), 100.0)
        self.assertEqual(
            controller.max_feedrate_for_axes(("C",)),
            controller.DEFAULT_FEEDRATE,
        )

    def test_ensure_axis_limits_refreshes_partial_cache(self) -> None:
        controller = StageController()
        controller._axis_limits = {"X": (0.0, 64.0)}
        serial_connection = _LineFakeSerial(
            [
                b"[MSG:INFO: Axis X (0.000,64.000)]\n",
                b"[MSG:INFO: Axis Y (0.000,64.000)]\n",
                b"[MSG:INFO: Axis Z (0.000,23.000)]\n",
                b"[MSG:INFO: Axis A (-5.500,0.000)]\n",
                b"ok\n",
            ]
        )

        controller._ensure_axis_limits(
            serial_connection, required_axes=controller.CONTROLLER_LIMIT_AXES
        )

        self.assertEqual(controller._axis_limits["Z"], (0.0, 23.0))
        self.assertEqual(controller._axis_limits["A"], (-5.5, 0.0))
        self.assertEqual(serial_connection.writes, [b"$Startup/Show\n"])

    def test_absolute_xy_move_requires_serial(self) -> None:
        controller = StageController()
        controller._serial = None
        controller.movement_started = types.SimpleNamespace(emit=lambda *args, **kwargs: None)
        controller.status_message = types.SimpleNamespace(emit=lambda *args, **kwargs: None)
        movement_results = []
        controller.movement_finished = types.SimpleNamespace(
            emit=lambda success, message: movement_results.append((success, message))
        )

        controller._run_move_to_xy(1.0, 1.0)

        self.assertEqual(movement_results[-1][0], False)
        self.assertIn("Serial connection is not available", movement_results[-1][1])

    def test_absolute_xyz_move_uses_safe_transfer_z_before_xy(self) -> None:
        controller = StageController()
        controller._serial = _FakeSerial()
        controller._position_reporting_mode = "machine"

        sent_moves = []
        statuses = [
            types.SimpleNamespace(
                state="Idle",
                position=(10.0, 20.0, 8.0, 0.0),
                display_position=(10.0, 20.0, 8.0, 0.0),
                homed_axes={"X", "Y", "Z"},
            ),
            types.SimpleNamespace(
                state="Idle",
                position=(30.0, 40.0, 6.0, 0.0),
                display_position=(30.0, 40.0, 6.0, 0.0),
                homed_axes={"X", "Y", "Z"},
            ),
        ]

        controller._move_safety_check = lambda: None
        controller._wait_for_idle = lambda _serial: None
        controller._query_status = lambda _serial: statuses.pop(0)
        controller._send_relative_move = (
            lambda _serial, move, **_kwargs: sent_moves.append(move)
        )
        controller.movement_started = types.SimpleNamespace(emit=lambda *args, **kwargs: None)
        controller.status_message = types.SimpleNamespace(emit=lambda *args, **kwargs: None)
        movement_results = []
        controller.movement_finished = types.SimpleNamespace(
            emit=lambda success, message: movement_results.append((success, message))
        )

        controller._run_move_to_xyz(30.0, 40.0, 6.0, 3.0, "stone position")

        self.assertEqual(len(sent_moves), 3)
        self.assertAlmostEqual(sent_moves[0].z, -5.0)
        self.assertAlmostEqual(sent_moves[1].x, 20.0)
        self.assertAlmostEqual(sent_moves[1].y, 20.0)
        self.assertAlmostEqual(sent_moves[2].z, 3.0)
        self.assertEqual(movement_results[-1][0], True)
        self.assertIn("Arrived at stone position", movement_results[-1][1])

    def test_click_to_move_holds_serial_lock(self) -> None:
        controller = StageController()
        controller._serial = _FakeSerial()
        controller._pixels_to_mm = _stage_controller_module.np.eye(2) * 0.1
        controller._move_safety_check = lambda: None
        controller._ensure_calibration = lambda _serial, target_pixels=None: (
            False,
            None,
        )
        controller._get_frame_snapshot = lambda timeout=3.0: (object(), 1)
        controller._wait_for_new_frame = (
            lambda frame_counter, timeout=4.0: (object(), frame_counter + 1)
        )
        controller.movement_started = types.SimpleNamespace(emit=lambda *args, **kwargs: None)
        controller.status_message = types.SimpleNamespace(emit=lambda *args, **kwargs: None)
        controller.stage_position_changed = types.SimpleNamespace(
            emit=lambda *args, **kwargs: None
        )
        movement_results = []
        controller.movement_finished = types.SimpleNamespace(
            emit=lambda success, message: movement_results.append((success, message))
        )
        observed = []

        def _send_relative_move(_serial, _move, **_kwargs) -> None:
            def _probe() -> None:
                acquired = controller._serial_session_lock.acquire(blocking=False)
                observed.append(acquired)
                if acquired:
                    controller._serial_session_lock.release()

            thread = threading.Thread(target=_probe)
            thread.start()
            thread.join()

        controller._send_relative_move = _send_relative_move

        controller._run_move(10.0, -5.0)

        self.assertEqual(observed, [False])
        self.assertEqual(movement_results[-1], (True, "Move complete."))


class StageControllerStatusParsingTest(unittest.TestCase):
    def test_work_mode_requests_wpos_status_reports(self) -> None:
        controller = StageController()

        self.assertEqual(controller._desired_status_report_mask_for_mode("work"), 2)
        self.assertEqual(controller._desired_status_report_mask_for_mode("machine"), 3)

    def test_parse_status_line_uses_native_work_position(self) -> None:
        controller = StageController()
        controller._active_work_coordinate_system = "G54"
        controller._controller_coordinate_offsets["G54"] = (
            32.0,
            32.0,
            0.0,
            0.0,
            0.0,
        )

        status = controller._parse_status_line(
            "<Idle|WPos:-6.894,-6.599,9.207,0.000,2.170|Bf:15,127|FS:0,0>"
        )

        self.assertIsNotNone(status)
        assert status is not None
        self.assertIsNone(status.position)
        self.assertEqual(status.work_offset[:2], (32.0, 32.0))
        self.assertAlmostEqual(status.work_position[0], -6.894)
        self.assertAlmostEqual(status.work_position[1], -6.599)

    def test_parse_status_line_captures_limit_pins(self) -> None:
        controller = StageController()

        status = controller._parse_status_line(
            "<Alarm|WPos:0.000,10.000,1.000,0.000|Pn:XY|FS:0,0>"
        )

        self.assertIsNotNone(status)
        assert status is not None
        self.assertEqual(status.pins, {"X", "Y"})

    def test_limit_axes_update_from_status_pins(self) -> None:
        controller = StageController()
        emitted = []
        controller.limit_axes_changed = types.SimpleNamespace(
            emit=lambda axes: emitted.append(set(axes))
        )
        status = controller._parse_status_line(
            "<Alarm|WPos:0.000,10.000,1.000,0.000|Pn:Y|FS:0,0>"
        )

        assert status is not None
        controller._update_limit_axes_from_status(status)

        self.assertEqual(controller._limit_axes, {"Y"})
        self.assertEqual(emitted, [{"Y"}])

    def test_soft_limit_message_marks_axis_limited(self) -> None:
        controller = StageController()
        emitted = []
        controller.limit_axes_changed = types.SimpleNamespace(
            emit=lambda axes: emitted.append(set(axes))
        )

        controller._handle_limit_line("[MSG:INFO: Soft limit on X target:-8.000]")

        self.assertEqual(controller._limit_axes, {"X"})
        self.assertEqual(emitted, [{"X"}])

    def test_work_mode_ignores_machine_position_status_reports(self) -> None:
        controller = StageController()
        controller._position_reporting_mode = "work"
        controller._active_work_coordinate_system = "G54"
        controller._controller_coordinate_offsets["G54"] = (
            32.0,
            32.0,
            0.0,
            0.0,
            0.0,
        )

        status = controller._parse_status_line(
            "<Idle|MPos:25.106,25.401,9.207,0.000,2.170|Bf:15,127|FS:0,0>"
        )

        self.assertIsNone(status)

    def test_parse_status_line_rejects_truncated_machine_position(self) -> None:
        controller = StageController()

        status = controller._parse_status_line("<Idle|MPos:29.459,31.4|FS:0,0>")

        self.assertIsNone(status)


class StageControllerAutofocusTest(unittest.TestCase):
    def test_autofocus_sweep_feedrate_uses_live_frame_rate(self) -> None:
        controller = StageController()
        start = time.monotonic() - 0.25
        with controller._frame_condition:
            for index in range(6):
                controller._frame_history.append(
                    (
                        index + 1,
                        start + index * 0.05,
                        np.zeros((2, 2), dtype=np.uint8),
                    )
                )

        feedrate = controller._autofocus_sweep_feedrate_mm_min(0.002)

        self.assertAlmostEqual(feedrate, 2.4, places=5)

    def test_autofocus_holds_serial_lock_and_refreshes_work_offsets(self) -> None:
        controller = StageController()
        lock = _TrackingLock()
        controller._serial_session_lock = lock
        serial_connection = _LockedLineFakeSerial(
            [
                b"ok\n",
                b"[GC:G54 G21 G90]\n",
                b"ok\n",
                b"[G54:32.000,32.000,0.000,0.000,0.000]\n",
                b"ok\n",
                b"<Idle|WPos:-0.842,-2.953,9.520,0.000,0.000|Bf:15,127|FS:0,0>\n",
            ],
            lock,
        )
        controller._serial = serial_connection
        controller._needles_known = True
        controller._needles_up = True
        controller._homed_axes = {"Z"}
        controller._axis_limits = {"Z": (0.0, 20.0)}
        controller._position_reporting_mode = "work"
        controller._current_status_report_mask = 2
        controller._active_work_coordinate_system = None
        controller._controller_coordinate_offsets.clear()
        controller.AUTOFOCUS_SAMPLES = 1
        frame = np.array([[1.0, 2.0], [3.0, 4.0]])
        controller._get_frame_snapshot = lambda timeout=3.0: (frame, 1)
        controller._focus_metric = lambda _frame: 10.0
        controller._autofocus_sweep_feedrate_mm_min = lambda _fine_step: 12.0
        controller.status_message = types.SimpleNamespace(
            emit=lambda *args, **kwargs: None
        )
        controller.movement_started = types.SimpleNamespace(
            emit=lambda *args, **kwargs: None
        )
        finished = []
        controller.autofocus_finished = types.SimpleNamespace(
            emit=lambda success, message: finished.append((success, message))
        )

        controller._run_focus_sweep_locked = (
            lambda *_args, **_kwargs: FocusSweepResult(
                best_z=9.520,
                best_score=10.0,
                sample_count=5,
                edge_peak=False,
            )
        )
        controller._run_static_focus_refinement_locked = (
            lambda *_args, **_kwargs: FocusSweepResult(
                best_z=9.515,
                best_score=12.0,
                sample_count=9,
                edge_peak=False,
            )
        )
        controller._approach_z_from_below_locked = lambda *_args, **_kwargs: None
        try:
            controller._run_autofocus()
        finally:
            controller.shutdown()

        self.assertTrue(finished[-1][0])
        self.assertEqual(controller._active_work_coordinate_system, "G54")
        self.assertIn("G54", controller._controller_coordinate_offsets)
        self.assertGreaterEqual(lock.max_depth, 1)
        self.assertEqual(
            serial_connection.writes[:3],
            [b"$G\n", b"$#\n", b"?\n"],
        )

    def test_static_autofocus_refinement_corrects_sweep_z_bias(self) -> None:
        controller = StageController()
        serial_connection = _FakeSerial()
        current_z = [10.040]
        true_focus_z = 9.980

        def _status() -> types.SimpleNamespace:
            position = (0.0, 0.0, current_z[0])
            return types.SimpleNamespace(
                state="Idle",
                position=position,
                work_position=position,
                display_position=position,
                work_offset=(0.0, 0.0, 0.0),
                homed_axes={"Z"},
            )

        def _send_relative_move(_serial, move, **_kwargs) -> None:
            current_z[0] += move.z

        frame_counter = [0]

        def _wait_for_new_frame(previous_counter, timeout=2.0):
            frame_counter[0] = max(frame_counter[0], previous_counter) + 1
            return np.array([[current_z[0]]], dtype=np.float32), frame_counter[0]

        controller._objective_autofocus_fine_step_mm = 0.01
        controller._query_status = lambda _serial: _status()
        controller._send_relative_move = _send_relative_move
        controller._wait_for_new_frame = _wait_for_new_frame
        controller._focus_metric = (
            lambda frame: 1.0 - (float(frame[0, 0]) - true_focus_z) ** 2
        )

        result = controller._run_static_focus_refinement_locked(
            serial_connection,
            10.000,
            min_z=0.0,
            max_z=20.0,
            step_mm=0.01,
        )

        self.assertLess(result.best_z, 10.000)
        self.assertAlmostEqual(result.best_z, true_focus_z, places=5)
        self.assertEqual(
            result.sample_count,
            controller.AUTOFOCUS_STATIC_REFINEMENT_POINTS,
        )
        self.assertFalse(result.edge_peak)

    def test_static_autofocus_refinement_expands_from_edge_peak(self) -> None:
        controller = StageController()
        serial_connection = _FakeSerial()
        current_z = [10.000]
        true_focus_z = 10.060

        def _status() -> types.SimpleNamespace:
            position = (0.0, 0.0, current_z[0])
            return types.SimpleNamespace(
                state="Idle",
                position=position,
                work_position=position,
                display_position=position,
                work_offset=(0.0, 0.0, 0.0),
                homed_axes={"Z"},
            )

        def _send_relative_move(_serial, move, **_kwargs) -> None:
            current_z[0] += move.z

        frame_counter = [0]

        def _wait_for_new_frame(previous_counter, timeout=2.0):
            frame_counter[0] = max(frame_counter[0], previous_counter) + 1
            return np.array([[current_z[0]]], dtype=np.float32), frame_counter[0]

        controller._query_status = lambda _serial: _status()
        controller._send_relative_move = _send_relative_move
        controller._wait_for_new_frame = _wait_for_new_frame
        controller._focus_metric = (
            lambda frame: 1.0 - (float(frame[0, 0]) - true_focus_z) ** 2
        )

        result = controller._run_static_focus_refinement_locked(
            serial_connection,
            10.000,
            min_z=0.0,
            max_z=20.0,
            step_mm=0.01,
        )

        self.assertAlmostEqual(result.best_z, true_focus_z, places=5)
        self.assertGreater(
            result.sample_count,
            controller.AUTOFOCUS_STATIC_REFINEMENT_POINTS,
        )
        self.assertFalse(result.edge_peak)

    def test_external_local_autofocus_uses_static_refinement_window(self) -> None:
        controller = StageController()
        serial_connection = _FakeSerial()
        controller._serial = serial_connection
        calls: list[tuple[object, ...]] = []
        finished: list[tuple[bool, str]] = []
        controller.autofocus_finished = types.SimpleNamespace(
            emit=lambda success, message: finished.append((success, message))
        )

        def _prepare(_serial, *, range_mm, step_mm):
            calls.append(("prepare", range_mm, step_mm))
            return AutofocusContext(
                objective_name="X20",
                start_z=10.000,
                min_z=0.0,
                max_z=20.0,
                lower_z=9.970,
                upper_z=10.030,
                local_range_mm=0.030,
                fine_step_mm=0.010,
            )

        def _static(_serial, center_z, *, min_z, max_z, step_mm):
            calls.append(("static", center_z, min_z, max_z, step_mm))
            return FocusSweepResult(
                best_z=10.012,
                best_score=2.5,
                sample_count=5,
                edge_peak=False,
            )

        def _approach(_serial, target_z, *, min_z, fine_step_mm):
            calls.append(("approach", target_z, min_z, fine_step_mm))

        controller._prepare_autofocus_context_locked = _prepare
        controller._run_static_focus_refinement_locked = _static
        controller._approach_z_from_below_locked = _approach

        result = controller.run_external_local_autofocus(range_mm=0.030)

        self.assertIn("local complete", result.summary())
        self.assertAlmostEqual(result.best_z_mm, 10.012)
        self.assertAlmostEqual(result.delta_um, 12.0)
        self.assertEqual(result.to_dict()["focus_best_z_mm"], 10.012)
        self.assertEqual(finished[-1][0], True)
        self.assertEqual(
            calls,
            [
                ("prepare", 0.030, None),
                ("static", 10.000, 9.970, 10.030, 0.010),
                ("approach", 10.012, 0.0, 0.010),
            ],
        )


class StageControllerObjectiveTest(unittest.TestCase):
    def test_verification_moves_directly_to_click_target(self) -> None:
        controller = StageController()
        serial_connection = _FakeSerial()
        current = [0.0, 0.0, 0.0]
        moves: list[MoveVector] = []
        controller._pixels_to_mm = np.array(
            [[0.001, 0.0], [0.0, 0.001]],
            dtype=float,
        )
        controller._frame_counter = 10

        def _status() -> types.SimpleNamespace:
            position = tuple(current)
            return types.SimpleNamespace(
                state="Idle",
                position=position,
                work_position=position,
                display_position=position,
                work_offset=(0.0, 0.0, 0.0),
                homed_axes={"X", "Y"},
            )

        def _send_relative_move(_serial, move, **_kwargs) -> None:
            moves.append(move)
            current[0] += move.x
            current[1] += move.y

        controller._query_status = lambda _serial: _status()
        controller._send_relative_move = _send_relative_move
        controller._get_frame_snapshot = (
            lambda timeout=3.0: (np.zeros((8, 8), dtype=np.uint8), 10)
        )
        controller._wait_for_new_frame = (
            lambda frame_counter, timeout=2.0: (
                np.zeros((8, 8), dtype=np.uint8),
                frame_counter + 1,
            )
        )
        controller._estimate_shift = lambda *_args: (40.0, 0.0)
        controller.status_message = types.SimpleNamespace(
            emit=lambda *args, **kwargs: None
        )

        handled, before_counter = controller._verify_active_objective_calibration(
            serial_connection,
            target_pixels=np.array([10.0, -5.0], dtype=float),
        )

        self.assertTrue(handled)
        self.assertEqual(before_counter, 10)
        self.assertEqual(len(moves), 2)
        self.assertAlmostEqual(moves[0].x, 0.04)
        self.assertAlmostEqual(moves[0].y, 0.0)
        self.assertAlmostEqual(moves[1].x, -0.05)
        self.assertAlmostEqual(moves[1].y, 0.005)
        self.assertAlmostEqual(current[0], -0.01)
        self.assertAlmostEqual(current[1], 0.005)

    def test_fresh_calibration_moves_directly_to_click_target(self) -> None:
        controller = StageController()
        serial_connection = _FakeSerial()
        current = [0.0, 0.0, 0.0]
        moves: list[MoveVector] = []
        axis_calls: list[str] = []
        controller._frame_counter = 12

        def _status() -> types.SimpleNamespace:
            position = tuple(current)
            return types.SimpleNamespace(
                state="Idle",
                position=position,
                work_position=position,
                display_position=position,
                work_offset=(0.0, 0.0, 0.0),
                homed_axes={"X", "Y"},
            )

        def _send_relative_move(_serial, move, **_kwargs) -> None:
            moves.append(move)
            current[0] += move.x
            current[1] += move.y

        def _calibrate_axis_series(_serial, _frame, _origin, axis: str):
            axis_calls.append(axis)
            if axis == "Y":
                current[0] = 0.02
                current[1] = 0.03
            return [
                (
                    np.array([1.0, 0.0], dtype=float),
                    np.array([1000.0, 0.0], dtype=float),
                ),
                (
                    np.array([0.0, 1.0], dtype=float),
                    np.array([0.0, 1000.0], dtype=float),
                ),
            ]

        controller._query_status = lambda _serial: _status()
        controller._send_relative_move = _send_relative_move
        controller._get_frame_snapshot = (
            lambda timeout=3.0: (np.zeros((8, 8), dtype=np.uint8), 12)
        )
        controller._calibrate_axis_series = _calibrate_axis_series
        controller._calibration_matrix_from_observations = (
            lambda _observations: np.eye(2, dtype=float) * 1000.0
        )
        controller.calibration_changed = types.SimpleNamespace(
            emit=lambda *args, **kwargs: None
        )
        controller.objective_calibration_updated = types.SimpleNamespace(
            emit=lambda *args, **kwargs: None
        )
        controller.status_message = types.SimpleNamespace(
            emit=lambda *args, **kwargs: None
        )

        handled, before_counter = controller._ensure_calibration(
            serial_connection,
            target_pixels=np.array([10.0, -5.0], dtype=float),
        )

        self.assertTrue(handled)
        self.assertEqual(before_counter, 12)
        self.assertEqual(axis_calls, ["X", "Y"])
        self.assertEqual(len(moves), 1)
        self.assertAlmostEqual(moves[0].x, -0.03)
        self.assertAlmostEqual(moves[0].y, -0.025)
        self.assertAlmostEqual(current[0], -0.01)
        self.assertAlmostEqual(current[1], 0.005)

    def test_axis_calibration_uses_axis_reference_position(self) -> None:
        controller = StageController()
        controller.CALIBRATION_MAX_OBSERVATIONS_PER_AXIS = 4
        controller.CALIBRATION_MIN_OBSERVATIONS = 4
        controller.CALIBRATION_PROBE_STEP_MM = 0.1
        controller.CALIBRATION_MAX_ADAPTIVE_STEP_MM = 0.1
        controller.CALIBRATION_MAX_UNVERIFIED_STEP_MM = 0.1
        controller._objective_calibration_target_pixels = 1000.0
        current = [0.4, 0.0, 0.0]
        serial_connection = _FakeSerial()

        def _status() -> types.SimpleNamespace:
            position = tuple(current)
            return types.SimpleNamespace(
                state="Idle",
                position=position,
                work_position=position,
                display_position=position,
                work_offset=(0.0, 0.0, 0.0),
                homed_axes={"X", "Y"},
            )

        def _send_relative_move(_serial, move) -> None:
            current[0] += move.x
            current[1] += move.y

        controller._query_status = lambda _serial: _status()
        controller._send_relative_move = _send_relative_move
        controller._wait_for_new_frame = (
            lambda frame_counter, timeout=2.0: (
                np.zeros((8, 8), dtype=np.uint8),
                frame_counter + 1,
            )
        )
        controller._estimate_shift_with_response = lambda *_args: (
            0.0,
            current[1] * 1000.0,
            1.0,
        )

        observations = controller._calibrate_axis_series(
            serial_connection,
            np.zeros((8, 8), dtype=np.uint8),
            (0.0, 0.0, 0.0),
            axis="Y",
        )

        self.assertEqual(len(observations), 4)
        for index, (mm_vector, _pixel_vector) in enumerate(observations, start=1):
            self.assertAlmostEqual(float(mm_vector[0]), 0.0)
            self.assertAlmostEqual(float(mm_vector[1]), 0.1 * index)

    def test_axis_calibration_adapts_probe_step_from_measured_pixels(self) -> None:
        controller = StageController()
        controller.CALIBRATION_MAX_OBSERVATIONS_PER_AXIS = 4
        controller.CALIBRATION_MIN_OBSERVATIONS = 4
        controller.CALIBRATION_PROBE_STEP_MM = 0.005
        controller.CALIBRATION_MAX_ADAPTIVE_STEP_MM = 0.08
        controller._objective_calibration_target_pixels = 120.0
        current = [0.0, 0.0, 0.0]
        moves: list[float] = []
        serial_connection = _FakeSerial()

        def _status() -> types.SimpleNamespace:
            position = tuple(current)
            return types.SimpleNamespace(
                state="Idle",
                position=position,
                work_position=position,
                display_position=position,
                work_offset=(0.0, 0.0, 0.0),
                homed_axes={"X", "Y"},
            )

        def _send_relative_move(_serial, move) -> None:
            moves.append(move.x)
            current[0] += move.x

        controller._query_status = lambda _serial: _status()
        controller._send_relative_move = _send_relative_move
        controller._wait_for_new_frame = (
            lambda frame_counter, timeout=2.0: (
                np.zeros((8, 8), dtype=np.uint8),
                frame_counter + 1,
            )
        )
        controller._estimate_shift_with_response = lambda *_args: (
            current[0] * 600.0,
            0.0,
            1.0,
        )

        observations = controller._calibrate_axis_series(
            serial_connection,
            np.zeros((8, 8), dtype=np.uint8),
            (0.0, 0.0, 0.0),
            axis="X",
        )

        self.assertEqual(len(observations), 4)
        self.assertAlmostEqual(moves[0], 0.005)
        self.assertTrue(all(move < 0.1 for move in moves))
        self.assertNotIn(1.0, moves)

    def test_calibration_verify_step_uses_saved_matrix_scale(self) -> None:
        controller = StageController()
        controller.CALIBRATION_VERIFY_TARGET_PIXELS = 40.0
        controller.CALIBRATION_VERIFY_STEP_MM = 0.05
        controller.CALIBRATION_PROBE_STEP_MM = 0.005
        controller._pixels_to_mm = np.array(
            [[0.001, 0.0], [0.0, 0.001]],
            dtype=float,
        )

        self.assertAlmostEqual(controller._calibration_verify_step_mm(), 0.04)

    def test_best_objective_for_measurement_uses_saved_candidate_matrices(self) -> None:
        controller = StageController()
        controller._objective_matrices = {
            "X5": np.array([[0.002, 0.0], [0.0, 0.002]], dtype=float),
            "X20": np.array([[0.0005, 0.0], [0.0, 0.0005]], dtype=float),
        }

        best = controller._best_objective_for_measurement(
            np.array([100.0, 0.0]),
            np.array([0.05, 0.0]),
            tolerance=0.005,
        )

        self.assertEqual(best, "X20")


class StageControllerJogQueueTest(unittest.TestCase):
    def test_jog_stop_writes_immediately_when_serial_lock_is_free(self) -> None:
        controller = StageController()
        serial_connection = _WritableFakeSerial()
        try:
            controller._serial = serial_connection

            controller.queue_jog_stop()

            self.assertEqual(serial_connection.writes, [b"\x85"])
            self.assertTrue(controller._async_write_queue.empty())
        finally:
            controller.shutdown()

    def test_force_jog_stop_writes_synchronously_before_serial_shutdown(self) -> None:
        controller = StageController()
        serial_connection = _WritableFakeSerial()
        try:
            controller._serial = serial_connection
            controller._jog_motion_active = True

            written = controller.force_jog_stop(timeout=0.1)

            self.assertTrue(written)
            self.assertEqual(serial_connection.writes, [b"\x85"])
            self.assertFalse(controller._jog_motion_active)
            self.assertTrue(controller._async_write_queue.empty())
        finally:
            controller.shutdown()

    def test_jog_command_superseded_while_waiting_for_serial_lock_is_not_written(
        self,
    ) -> None:
        controller = StageController()
        controller.SERIAL_JOG_COMMAND_SETTLE_S = 0.0
        serial_connection = _WritableFakeSerial()
        try:
            controller._serial = serial_connection
            controller._serial_session_lock.acquire()
            try:
                controller.queue_jog_command("$J=G91 G21 X250.000 F10")
                deadline = time.monotonic() + 1.0
                while (
                    not controller._async_write_queue.empty()
                    and time.monotonic() < deadline
                ):
                    time.sleep(0.005)
                controller.queue_jog_stop()
            finally:
                controller._serial_session_lock.release()

            controller._async_write_queue.join()

            self.assertEqual(serial_connection.writes, [b"\x85"])
        finally:
            controller.shutdown()

    def test_absolute_jog_reissue_queues_stop_before_new_feedrate_command(self) -> None:
        controller = StageController()
        controller.SERIAL_JOG_COMMAND_SETTLE_S = 0.0
        serial_connection = _WritableFakeSerial()
        try:
            controller._serial = serial_connection
            lock_acquired = threading.Event()
            release_lock = threading.Event()

            def _hold_serial_lock() -> None:
                with controller._serial_session_lock:
                    lock_acquired.set()
                    release_lock.wait(timeout=1.0)

            holder = threading.Thread(target=_hold_serial_lock)
            holder.start()
            self.assertTrue(lock_acquired.wait(timeout=1.0))
            accepted = controller.queue_absolute_axis_targets_jog(
                {"Y": -2.0, "X": 1.5},
                feedrate=180.0,
            )
            self.assertTrue(accepted)
            self.assertFalse(controller._jog_motion_active)
            self.assertEqual(serial_connection.writes, [])
            release_lock.set()
            holder.join(timeout=1.0)

            controller._async_write_queue.join()

            self.assertEqual(
                serial_connection.writes,
                [b"\x85", b"$J=G90 G21 X1.5000 Y-2.0000 F180\n"],
            )
        finally:
            controller.shutdown()

    def test_superseded_jog_command_is_dropped_before_write(self) -> None:
        controller = StageController()
        controller.SERIAL_JOG_COMMAND_SETTLE_S = 0.0
        controller._queued_jog_generation = 2
        job = QueuedSerialWrite(
            priority=controller.SERIAL_PRIORITY_JOG_COMMAND,
            sequence=1,
            kind="jog_command",
            payload=b"$J=G91 G21 X250.000 F10.0\n",
            description="$J=G91 G21 X250.000 F10.0",
            generation=1,
        )

        ready = controller._await_current_jog_command(job)

        self.assertFalse(ready)

    def test_current_jog_command_survives_settle_window(self) -> None:
        controller = StageController()
        controller.SERIAL_JOG_COMMAND_SETTLE_S = 0.0
        controller._queued_jog_generation = 3
        job = QueuedSerialWrite(
            priority=controller.SERIAL_PRIORITY_JOG_COMMAND,
            sequence=1,
            kind="jog_command",
            payload=b"$J=G91 G21 Y250.000 F10.0\n",
            description="$J=G91 G21 Y250.000 F10.0",
            generation=3,
        )

        ready = controller._await_current_jog_command(job)

        self.assertTrue(ready)


class StageControllerMotionSafetyBypassTest(unittest.TestCase):
    def test_disabled_motion_safety_bypasses_needle_safety_and_axis_limits(self) -> None:
        controller = StageController()
        controller.set_motion_safety_disabled(True)
        controller._serial = _FakeSerial()
        commands = []
        controller._ensure_axis_limits = (
            lambda _serial: (_ for _ in ()).throw(AssertionError("limits checked"))
        )
        controller._check_relative_move_limits = (
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("relative limits checked")
            )
        )
        controller._write_command = lambda _serial, command: commands.append(command)
        controller._wait_for_ok = lambda *_args, **_kwargs: None
        controller._wait_for_idle = lambda *_args, **_kwargs: None

        controller._move_safety_check()
        controller._send_relative_move(controller._serial, MoveVector(a=0.25))

        self.assertIn("G1 A0.2500 F600", commands)

    def test_disabled_motion_safety_allows_absolute_manual_axis_move(self) -> None:
        controller = StageController()
        controller.set_motion_safety_disabled(True)
        controller._serial = _FakeSerial()
        commands = []
        controller._ensure_axis_limits = (
            lambda _serial: (_ for _ in ()).throw(AssertionError("limits checked"))
        )
        controller._query_status = (
            lambda _serial: (_ for _ in ()).throw(AssertionError("status queried"))
        )
        controller._write_command = lambda _serial, command: commands.append(command)
        controller._wait_for_ok = lambda *_args, **_kwargs: None
        controller._wait_for_idle = lambda *_args, **_kwargs: None

        controller._send_absolute_axis_move(
            controller._serial,
            "B",
            0.25,
            ignore_needle_safety=True,
            feedrate=123.4,
        )

        self.assertIn("G90", commands)
        self.assertIn("G1 B0.2500 F123.4", commands)

    def test_multi_axis_absolute_move_uses_single_g90_command(self) -> None:
        controller = StageController()
        controller._serial = _FakeSerial()
        controller._needles_known = True
        controller._needles_up = True
        controller._axis_limits = {"X": (0.0, 64.0), "Y": (0.0, 64.0)}
        controller._active_work_coordinate_system = "G54"
        commands = []
        controller._ensure_axis_limits = lambda _serial, **_kwargs: None
        controller._query_status = lambda _serial: types.SimpleNamespace(
            state="Idle",
            position=None,
            work_position=(0.0, 0.0, 0.0),
            display_position=(0.0, 0.0, 0.0),
            work_offset=(32.0, 32.0, 0.0),
            coordinate_system="G54",
            homed_axes={"X", "Y"},
        )
        controller._write_command = lambda _serial, command: commands.append(command)
        controller._wait_for_ok = lambda *_args, **_kwargs: None
        controller._wait_for_idle = lambda *_args, **_kwargs: None
        controller._reset_feed_override_for_serial = lambda _serial: None

        controller._send_absolute_axis_targets_move(
            controller._serial,
            {"X": 10.0, "Y": -5.0},
            feedrate=123.4,
        )

        self.assertIn("G90", commands)
        self.assertIn("G1 X10.0000 Y-5.0000 F123.4", commands)

    def test_coordinate_task_uses_cancelable_absolute_jog(self) -> None:
        controller = StageController()
        controller.set_motion_safety_disabled(True)
        controller._serial = _FakeSerial()
        controller.movement_started = types.SimpleNamespace(
            emit=lambda *_args, **_kwargs: None
        )
        movement_results = []
        controller.movement_finished = types.SimpleNamespace(
            emit=lambda success, message: movement_results.append((success, message))
        )
        controller.status_message = types.SimpleNamespace(
            emit=lambda *_args, **_kwargs: None
        )
        commands = []
        controller._write_command = lambda _serial, command: commands.append(command)
        controller._wait_for_ok = lambda *_args, **_kwargs: None
        controller._wait_for_idle = lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("waited for idle")
        )

        controller._run_absolute_axis_targets_move(
            {"X": 1.5, "Y": -2.0},
            25.0,
        )

        self.assertIn("$J=G90 G21 X1.5000 Y-2.0000 F25", commands)
        self.assertFalse(any(command.startswith("G1 ") for command in commands))
        self.assertEqual(movement_results[-1][0], True)

    def test_external_absolute_targets_waits_for_completion(self) -> None:
        controller = StageController()
        controller.set_motion_safety_disabled(True)
        controller._serial = _FakeSerial()
        movement_results = []
        idle_calls = []
        commands = []
        controller.movement_started = types.SimpleNamespace(
            emit=lambda *_args, **_kwargs: None
        )
        controller.movement_finished = types.SimpleNamespace(
            emit=lambda success, message: movement_results.append((success, message))
        )
        controller.status_message = types.SimpleNamespace(
            emit=lambda *_args, **_kwargs: None
        )
        controller._write_command = lambda _serial, command: commands.append(command)
        controller._wait_for_ok = lambda *_args, **_kwargs: None
        controller._wait_for_idle = lambda *_args, **_kwargs: idle_calls.append(_args)
        controller._query_status = lambda _serial: None
        try:
            message = controller.run_external_absolute_axis_targets_move(
                {"z": 3.25},
                feedrate=12.5,
            )
        finally:
            controller.shutdown()

        self.assertIn("$J=G90 G21 Z3.2500 F12.5", commands)
        self.assertTrue(idle_calls)
        self.assertIn("Z+3.250", message)
        self.assertEqual(movement_results[-1][0], True)

    def test_machine_coordinate_jog_uses_g53(self) -> None:
        controller = StageController()
        controller.set_motion_safety_disabled(True)
        controller._position_reporting_mode = "machine"
        controller._serial = _FakeSerial()
        commands = []
        controller._write_command = lambda _serial, command: commands.append(command)
        controller._wait_for_ok = lambda *_args, **_kwargs: None
        controller._wait_for_idle = lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("waited for idle")
        )

        controller._send_absolute_axis_targets_move(
            controller._serial,
            {"X": 4.0},
            ignore_needle_safety=True,
            feedrate=50.0,
            wait_for_completion=False,
            as_jog=True,
        )

        self.assertIn("$J=G90 G21 G53 X4.0000 F50", commands)

    def test_homed_work_xy_target_uses_reported_wco_limits(self) -> None:
        controller = StageController()
        controller._serial = _FakeSerial()
        controller._needles_known = True
        controller._needles_up = True
        controller._axis_limits = {"X": (0.0, 64.0), "Y": (0.0, 64.0)}
        controller._position_reporting_mode = "work"
        controller._ensure_axis_limits = lambda _serial, **_kwargs: None
        controller._query_status = lambda _serial: types.SimpleNamespace(
            state="Idle",
            position=None,
            work_position=(0.0, 0.0, 0.0),
            display_position=(0.0, 0.0, 0.0),
            work_offset=(32.0, 32.0, 0.0),
            coordinate_system="G54",
            homed_axes={"X", "Y"},
        )

        with self.assertRaises(StageControllerError):
            controller._send_absolute_axis_targets_move(
                controller._serial,
                {"X": 37.0},
                feedrate=100.0,
            )

    def test_unhomed_work_xy_target_does_not_apply_software_limits(self) -> None:
        controller = StageController()
        controller._serial = _FakeSerial()
        controller._needles_known = True
        controller._needles_up = True
        controller._axis_limits = {"X": (0.0, 64.0)}
        commands = []
        controller._position_reporting_mode = "work"
        controller._ensure_axis_limits = lambda _serial, **_kwargs: None
        controller._query_status = lambda _serial: types.SimpleNamespace(
            state="Idle",
            position=None,
            work_position=(0.0, 0.0, 0.0),
            display_position=(0.0, 0.0, 0.0),
            work_offset=None,
            coordinate_system=None,
            homed_axes=set(),
        )
        controller._write_command = lambda _serial, command: commands.append(command)
        controller._wait_for_ok = lambda *_args, **_kwargs: None
        controller._wait_for_idle = lambda *_args, **_kwargs: None
        controller._reset_feed_override_for_serial = lambda _serial: None

        controller._send_absolute_axis_targets_move(
            controller._serial,
            {"X": 37.0},
            feedrate=100.0,
            allow_unhomed=True,
        )

        self.assertIn("G1 X37.0000 F100", commands)

    def test_relative_manual_axis_move_is_resolved_to_absolute_g90(
        self,
    ) -> None:
        controller = StageController()
        controller.set_motion_safety_disabled(True)
        controller._serial = _FakeSerial()
        controller.movement_started = types.SimpleNamespace(
            emit=lambda *_args, **_kwargs: None
        )
        movement_results = []
        controller.movement_finished = types.SimpleNamespace(
            emit=lambda success, message: movement_results.append((success, message))
        )
        controller.status_message = types.SimpleNamespace(
            emit=lambda *_args, **_kwargs: None
        )
        commands = []
        controller._query_status = lambda _serial: types.SimpleNamespace(
            state="Idle",
            position=(0.0, 0.0, 0.0, -0.1),
            work_position=(0.0, 0.0, 0.0, -0.1),
            display_position=(0.0, 0.0, 0.0, -0.1),
            homed_axes={"A"},
        )
        controller._write_command = lambda _serial, command: commands.append(command)
        controller._wait_for_ok = lambda *_args, **_kwargs: None
        controller._wait_for_idle = lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("waited for idle")
        )

        controller._run_manual_axis_move("A", -0.02, "G91", 1.0)

        self.assertEqual(commands, ["$J=G90 G21 A-0.1200 F1"])
        self.assertEqual(movement_results[-1][0], True)
        self.assertIn("accepted", movement_results[-1][1])

    def test_relative_manual_axis_move_can_use_unhomed_current_position(self) -> None:
        controller = StageController()
        controller._serial = _FakeSerial()
        controller._needles_known = True
        controller._needles_up = True
        controller.movement_started = types.SimpleNamespace(
            emit=lambda *_args, **_kwargs: None
        )
        movement_results = []
        controller.movement_finished = types.SimpleNamespace(
            emit=lambda success, message: movement_results.append((success, message))
        )
        controller.status_message = types.SimpleNamespace(
            emit=lambda *_args, **_kwargs: None
        )
        commands = []
        controller._ensure_axis_limits = lambda _serial, **_kwargs: None
        controller._query_status = lambda _serial: types.SimpleNamespace(
            state="Idle",
            position=(1.0, 0.0, 0.0),
            work_position=(1.0, 0.0, 0.0),
            display_position=(1.0, 0.0, 0.0),
            homed_axes=set(),
        )
        controller._write_command = lambda _serial, command: commands.append(command)
        controller._wait_for_ok = lambda *_args, **_kwargs: None
        controller._wait_for_idle = lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("waited for idle")
        )

        controller._run_manual_axis_move("X", 0.25, "G91", 10.0)

        self.assertEqual(commands, ["$J=G90 G21 X1.2500 F10"])
        self.assertEqual(movement_results[-1][0], True)

    def test_absolute_manual_axis_zero_target_is_sent(self) -> None:
        controller = StageController()
        controller.set_motion_safety_disabled(True)
        controller._serial = _FakeSerial()
        controller.movement_started = types.SimpleNamespace(
            emit=lambda *_args, **_kwargs: None
        )
        movement_results = []
        controller.movement_finished = types.SimpleNamespace(
            emit=lambda success, message: movement_results.append((success, message))
        )
        controller.status_message = types.SimpleNamespace(
            emit=lambda *_args, **_kwargs: None
        )
        commands = []
        controller._query_status = lambda _serial: (_ for _ in ()).throw(
            AssertionError("status queried")
        )
        controller._write_command = lambda _serial, command: commands.append(command)
        controller._wait_for_ok = lambda *_args, **_kwargs: None
        controller._wait_for_idle = lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("waited for idle")
        )

        controller._run_manual_axis_move("A", 0.0, "G90", 5.0)

        self.assertEqual(commands, ["$J=G90 G21 A0.0000 F5"])
        self.assertEqual(movement_results[-1][0], True)
        self.assertIn("accepted", movement_results[-1][1])

    def test_idle_timeout_scales_with_slow_feedrate(self) -> None:
        controller = StageController()

        self.assertEqual(controller._idle_timeout_for_distance(0.25, 1.0), 20.0)


class StageControllerAxisACalibrationTest(unittest.TestCase):
    def test_axis_a_calibration_maps_physical_lowering_to_absolute_gcode(self) -> None:
        controller = StageController()
        try:
            controller.apply_axis_a_calibration(
                types.SimpleNamespace(
                    configured=True,
                    model="cosine_displacement",
                    steps_per_mm=2500.0,
                    commanded_lowering_min_mm=0.0,
                    commanded_lowering_max_mm=6.0,
                    offset_mm=-0.00013272701600556085,
                    amplitude_mm=4.29496757977153,
                    angular_frequency_rad_per_mm=0.24349261926759336,
                    phase_rad=0.8994441869661569,
                )
            )

            target_a = controller.axis_a_gcode_coordinate_for_lowering(0.02)

            self.assertLess(target_a, 0.0)
            self.assertAlmostEqual(target_a, -0.0243676184, places=6)
            self.assertAlmostEqual(
                controller.axis_a_lowering_for_gcode_coordinate(0.0),
                0.0,
                places=6,
            )
        finally:
            controller.shutdown()

    def test_axis_a_calibration_falls_back_when_disabled(self) -> None:
        controller = StageController()
        try:
            controller.apply_axis_a_calibration(
                types.SimpleNamespace(configured=False)
            )

            target_a = controller.axis_a_gcode_coordinate_for_lowering(0.02)
            lowering = controller.axis_a_lowering_for_gcode_coordinate(-1.0)

            self.assertEqual(target_a, -0.02)
            self.assertEqual(lowering, 1.0)
        finally:
            controller.shutdown()

    def test_needle_adjust_sends_absolute_calibrated_a_target(self) -> None:
        controller = StageController()
        try:
            controller._serial = _FakeSerial()
            controller.apply_axis_a_calibration(
                types.SimpleNamespace(
                    configured=True,
                    model="cosine_displacement",
                    steps_per_mm=2500.0,
                    commanded_lowering_min_mm=0.0,
                    commanded_lowering_max_mm=6.0,
                    offset_mm=-0.00013272701600556085,
                    amplitude_mm=4.29496757977153,
                    angular_frequency_rad_per_mm=0.24349261926759336,
                    phase_rad=0.8994441869661569,
                )
            )
            controller._query_status = lambda _serial: types.SimpleNamespace(
                state="Idle",
                position=(0.0, 0.0, 0.0, 0.0),
                work_position=(0.0, 0.0, 0.0, 0.0),
                display_position=(0.0, 0.0, 0.0, 0.0),
                homed_axes={"A"},
            )
            targets = []
            controller._send_absolute_axis_move = (
                lambda _serial, axis, value, **_kwargs: targets.append((axis, value))
            )
            controller._read_current_a_position = lambda _serial: targets[-1][1]
            controller.needles_action_finished = types.SimpleNamespace(
                emit=lambda *_args, **_kwargs: None
            )
            controller.needle_height_changed = types.SimpleNamespace(
                emit=lambda *_args, **_kwargs: None
            )
            controller.needles_state_changed = types.SimpleNamespace(
                emit=lambda *_args, **_kwargs: None
            )
            controller.axis_a_ready_changed = types.SimpleNamespace(
                emit=lambda *_args, **_kwargs: None
            )

            controller._run_needles_adjust(-0.02)

            self.assertEqual(targets[0][0], "A")
            self.assertAlmostEqual(targets[0][1], -0.0243676184, places=6)
        finally:
            controller.shutdown()

    def test_needles_lower_uses_requested_feedrate_while_active(self) -> None:
        controller = StageController()
        try:
            controller._serial = _FakeSerial()
            controller.apply_axis_max_feedrates({"A": 500.0})
            controller.apply_needle_calibration(down_position_mm=1.0)
            controller._query_status = lambda _serial: types.SimpleNamespace(
                state="Idle",
                position=(0.0, 0.0, 0.0, 0.0),
                work_position=(0.0, 0.0, 0.0, 0.0),
                display_position=(0.0, 0.0, 0.0, 0.0),
                homed_axes={"A"},
            )
            observed = []

            def _send_absolute_axis_move(_serial, axis, value, **kwargs) -> None:
                observed.append(
                    (
                        axis,
                        value,
                        kwargs.get("feedrate"),
                        controller._active_needles_action,
                        controller._active_needles_programmed_feedrate,
                    )
                )

            controller._send_absolute_axis_move = _send_absolute_axis_move
            controller.needles_action_started = types.SimpleNamespace(
                emit=lambda *_args, **_kwargs: None
            )
            controller.needles_action_finished = types.SimpleNamespace(
                emit=lambda *_args, **_kwargs: None
            )
            controller.needle_height_changed = types.SimpleNamespace(
                emit=lambda *_args, **_kwargs: None
            )
            controller.needles_state_changed = types.SimpleNamespace(
                emit=lambda *_args, **_kwargs: None
            )
            controller.axis_a_ready_changed = types.SimpleNamespace(
                emit=lambda *_args, **_kwargs: None
            )

            controller._run_needles_action("lower", feedrate=80.0)

            self.assertEqual(observed[0][0], "A")
            self.assertAlmostEqual(observed[0][1], -0.95)
            self.assertEqual(observed[0][2:], (500.0, None, None))
            self.assertEqual(observed[1][0], "A")
            self.assertAlmostEqual(observed[1][1], -1.0)
            self.assertEqual(observed[1][2:], (80.0, "lower", 80.0))
            self.assertIsNone(controller._active_needles_action)
            self.assertIsNone(controller._active_needles_programmed_feedrate)
        finally:
            controller.shutdown()

    def test_needles_raise_moves_slow_out_of_contact_zone_then_full_up(self) -> None:
        controller = StageController()
        try:
            controller._serial = _FakeSerial()
            controller.apply_axis_max_feedrates({"A": 500.0})
            controller.apply_needle_calibration(
                raise_position_mm=0.5,
                down_position_mm=1.0,
            )
            controller._query_status = lambda _serial: types.SimpleNamespace(
                state="Idle",
                position=(0.0, 0.0, 0.0, -1.0),
                work_position=(0.0, 0.0, 0.0, -1.0),
                display_position=(0.0, 0.0, 0.0, -1.0),
                homed_axes={"A"},
            )
            observed = []
            states = []

            def _send_absolute_axis_move(_serial, axis, value, **kwargs) -> None:
                observed.append(
                    (
                        axis,
                        value,
                        kwargs.get("feedrate"),
                        controller._active_needles_action,
                        controller._active_needles_programmed_feedrate,
                    )
                )

            controller._send_absolute_axis_move = _send_absolute_axis_move
            controller.needles_action_started = types.SimpleNamespace(
                emit=lambda *_args, **_kwargs: None
            )
            controller.needles_action_finished = types.SimpleNamespace(
                emit=lambda *_args, **_kwargs: None
            )
            controller.needle_height_changed = types.SimpleNamespace(
                emit=lambda *_args, **_kwargs: None
            )
            controller.needles_state_changed = types.SimpleNamespace(
                emit=lambda raised, known: states.append((raised, known))
            )
            controller.axis_a_ready_changed = types.SimpleNamespace(
                emit=lambda *_args, **_kwargs: None
            )

            controller._run_needles_action("raise", feedrate=70.0)

            self.assertEqual(observed[0][0], "A")
            self.assertAlmostEqual(observed[0][1], -0.95)
            self.assertEqual(observed[0][2:], (70.0, "raise", 70.0))
            self.assertEqual(observed[1][0], "A")
            self.assertAlmostEqual(observed[1][1], 0.0)
            self.assertEqual(observed[1][2:], (500.0, None, None))
            self.assertEqual(states[-1], (True, True))
        finally:
            controller.shutdown()

    def test_needles_lift_moves_only_to_contact_zone_boundary(self) -> None:
        controller = StageController()
        try:
            controller._serial = _FakeSerial()
            controller.apply_axis_max_feedrates({"A": 500.0})
            controller.apply_needle_calibration(
                raise_position_mm=0.5,
                down_position_mm=1.0,
                contact_zone_mm=0.1,
            )
            controller._query_status = lambda _serial: types.SimpleNamespace(
                state="Idle",
                position=(0.0, 0.0, 0.0, -1.0),
                work_position=(0.0, 0.0, 0.0, -1.0),
                display_position=(0.0, 0.0, 0.0, -1.0),
                homed_axes={"A"},
            )
            observed = []

            def _send_absolute_axis_move(_serial, axis, value, **kwargs) -> None:
                observed.append(
                    (
                        axis,
                        value,
                        kwargs.get("feedrate"),
                        controller._active_needles_action,
                        controller._active_needles_programmed_feedrate,
                    )
                )

            controller._send_absolute_axis_move = _send_absolute_axis_move
            controller.needles_action_started = types.SimpleNamespace(
                emit=lambda *_args, **_kwargs: None
            )
            controller.needles_action_finished = types.SimpleNamespace(
                emit=lambda *_args, **_kwargs: None
            )
            controller.needle_height_changed = types.SimpleNamespace(
                emit=lambda *_args, **_kwargs: None
            )
            controller.needles_state_changed = types.SimpleNamespace(
                emit=lambda *_args, **_kwargs: None
            )
            controller.axis_a_ready_changed = types.SimpleNamespace(
                emit=lambda *_args, **_kwargs: None
            )

            controller._run_needles_action("lift", feedrate=70.0)

            self.assertEqual(observed, [("A", -0.9, 70.0, "lift", 70.0)])
            self.assertIsNone(controller._active_needles_action)
            self.assertIsNone(controller._active_needles_programmed_feedrate)
        finally:
            controller.shutdown()

    def test_needles_lift_moves_to_contact_zone_boundary_from_above(self) -> None:
        controller = StageController()
        try:
            controller._serial = _FakeSerial()
            controller.apply_axis_max_feedrates({"A": 500.0})
            controller.apply_needle_calibration(
                raise_position_mm=0.5,
                down_position_mm=1.0,
                contact_zone_mm=0.1,
            )
            controller._query_status = lambda _serial: types.SimpleNamespace(
                state="Idle",
                position=(0.0, 0.0, 0.0, -0.5),
                work_position=(0.0, 0.0, 0.0, -0.5),
                display_position=(0.0, 0.0, 0.0, -0.5),
                homed_axes={"A"},
            )
            observed = []
            messages = []

            def _send_absolute_axis_move(_serial, axis, value, **kwargs) -> None:
                observed.append(
                    (
                        axis,
                        value,
                        kwargs.get("feedrate"),
                        controller._active_needles_action,
                        controller._active_needles_programmed_feedrate,
                    )
                )

            controller._send_absolute_axis_move = _send_absolute_axis_move
            controller.needles_action_started = types.SimpleNamespace(
                emit=lambda *_args, **_kwargs: None
            )
            controller.needles_action_finished = types.SimpleNamespace(
                emit=lambda success, message, action: messages.append(
                    (success, message, action)
                )
            )
            controller.needle_height_changed = types.SimpleNamespace(
                emit=lambda *_args, **_kwargs: None
            )
            controller.needles_state_changed = types.SimpleNamespace(
                emit=lambda *_args, **_kwargs: None
            )
            controller.axis_a_ready_changed = types.SimpleNamespace(
                emit=lambda *_args, **_kwargs: None
            )

            controller._run_needles_action("lift", feedrate=70.0)

            self.assertEqual(observed, [("A", -0.9, 500.0, None, None)])
            self.assertEqual(messages[-1], (True, "Needles lifted.", "lift"))
        finally:
            controller.shutdown()

    def test_needles_lower_targets_contact_zero_after_a_work_offset(self) -> None:
        controller = StageController()
        try:
            controller._serial = _FakeSerial()
            controller._position_reporting_mode = "work"
            controller._active_work_coordinate_system = "G54"
            controller._controller_coordinate_offsets["G54"] = (
                0.0,
                0.0,
                0.0,
                -1.0,
                0.0,
            )
            controller.apply_axis_max_feedrates({"A": 500.0})
            controller.apply_needle_calibration(down_position_mm=1.0)
            controller._query_status = lambda _serial: types.SimpleNamespace(
                state="Idle",
                position=None,
                work_position=(0.0, 0.0, 0.0, 1.0),
                display_position=(0.0, 0.0, 0.0, 1.0),
                work_offset=(0.0, 0.0, 0.0, -1.0, 0.0),
                coordinate_system="G54",
                homed_axes={"A"},
            )
            observed = []

            def _send_absolute_axis_move(_serial, axis, value, **kwargs) -> None:
                observed.append((axis, value, kwargs.get("feedrate")))

            controller._send_absolute_axis_move = _send_absolute_axis_move
            controller.needles_action_started = types.SimpleNamespace(
                emit=lambda *_args, **_kwargs: None
            )
            controller.needles_action_finished = types.SimpleNamespace(
                emit=lambda *_args, **_kwargs: None
            )
            controller.needle_height_changed = types.SimpleNamespace(
                emit=lambda *_args, **_kwargs: None
            )
            controller.needles_state_changed = types.SimpleNamespace(
                emit=lambda *_args, **_kwargs: None
            )
            controller.axis_a_ready_changed = types.SimpleNamespace(
                emit=lambda *_args, **_kwargs: None
            )

            controller._run_needles_action("lower", feedrate=80.0)

            self.assertEqual(observed[0][0], "A")
            self.assertAlmostEqual(observed[0][1], 0.05)
            self.assertEqual(observed[0][2], 500.0)
            self.assertEqual(observed[1][0], "A")
            self.assertAlmostEqual(observed[1][1], 0.0)
            self.assertEqual(observed[1][2], 80.0)
        finally:
            controller.shutdown()

    def test_needles_lower_to_depth_below_down_goes_directly_to_target(self) -> None:
        controller = StageController()
        try:
            controller._serial = _FakeSerial()
            controller._position_reporting_mode = "machine"
            controller.apply_axis_max_feedrates({"A": 500.0})
            controller.apply_needle_calibration(down_position_mm=1.0)
            controller._query_status = lambda _serial: types.SimpleNamespace(
                state="Idle",
                position=(0.0, 0.0, 0.0, -0.95),
                work_position=(0.0, 0.0, 0.0, -0.95),
                display_position=(0.0, 0.0, 0.0, -0.95),
                homed_axes={"A"},
            )
            observed = []

            def _send_absolute_axis_move(_serial, axis, value, **kwargs) -> None:
                observed.append((axis, value, kwargs.get("feedrate")))

            controller._send_absolute_axis_move = _send_absolute_axis_move
            controller.needles_action_started = types.SimpleNamespace(
                emit=lambda *_args, **_kwargs: None
            )
            controller.needles_action_finished = types.SimpleNamespace(
                emit=lambda *_args, **_kwargs: None
            )
            controller.needle_height_changed = types.SimpleNamespace(
                emit=lambda *_args, **_kwargs: None
            )
            controller.needles_state_changed = types.SimpleNamespace(
                emit=lambda *_args, **_kwargs: None
            )
            controller.axis_a_ready_changed = types.SimpleNamespace(
                emit=lambda *_args, **_kwargs: None
            )

            message = controller.run_external_needles_lower_to_depth_below_down(
                0.001,
                feedrate=80.0,
            )

            self.assertEqual(message, "Needles lowered to 0.0010 mm below saved down.")
            self.assertEqual(len(observed), 1)
            self.assertEqual(observed[0][0], "A")
            self.assertAlmostEqual(observed[0][1], -1.001)
            self.assertEqual(observed[0][2], 80.0)
        finally:
            controller.shutdown()

    def test_manual_axis_a_relative_move_keeps_raw_gcode_sign(self) -> None:
        controller = StageController()
        try:
            controller.apply_axis_a_calibration(
                types.SimpleNamespace(
                    configured=True,
                    model="cosine_displacement",
                    steps_per_mm=2500.0,
                    commanded_lowering_min_mm=0.0,
                    commanded_lowering_max_mm=6.0,
                    offset_mm=-0.00013272701600556085,
                    amplitude_mm=4.29496757977153,
                    angular_frequency_rad_per_mm=0.24349261926759336,
                    phase_rad=0.8994441869661569,
                )
            )
            controller._query_status = lambda _serial: types.SimpleNamespace(
                state="Idle",
                position=(0.0, 0.0, 0.0, 0.0),
                work_position=(0.0, 0.0, 0.0, 0.0),
                display_position=(0.0, 0.0, 0.0, 0.0),
                homed_axes={"A"},
            )

            target = controller._manual_axis_absolute_target(
                _FakeSerial(),
                "A",
                -0.02,
                "G91",
            )

            self.assertEqual(target, -0.02)
        finally:
            controller.shutdown()

    def test_calibrated_axis_a_display_keeps_gcode_sign(self) -> None:
        controller = StageController()
        try:
            controller.apply_axis_a_calibration(
                types.SimpleNamespace(
                    configured=True,
                    model="cosine_displacement",
                    steps_per_mm=2500.0,
                    commanded_lowering_min_mm=0.0,
                    commanded_lowering_max_mm=6.0,
                    offset_mm=-0.00013272701600556085,
                    amplitude_mm=4.29496757977153,
                    angular_frequency_rad_per_mm=0.24349261926759336,
                    phase_rad=0.8994441869661569,
                )
            )

            display = controller.calibrated_axis_display_value("A", -1.0)
            raw = controller.calibrated_axis_raw_value("A", display)

            self.assertLess(display, 0.0)
            self.assertAlmostEqual(raw, -1.0, places=6)
        finally:
            controller.shutdown()

    def test_calibrated_axis_a_zero_display_is_positive_zero(self) -> None:
        controller = StageController()
        try:
            controller.apply_axis_a_calibration(
                AxisACalibrationSettings(configured=True)
            )

            display = controller.calibrated_axis_display_value("A", 0.0)

            self.assertEqual(display, 0.0)
            self.assertEqual(math.copysign(1.0, display), 1.0)
        finally:
            controller.shutdown()

    def test_legacy_negative_saved_a_position_is_treated_as_raw_coordinate(self) -> None:
        controller = StageController()
        try:
            controller.apply_axis_a_calibration(
                types.SimpleNamespace(
                    configured=True,
                    model="cosine_displacement",
                    steps_per_mm=2500.0,
                    commanded_lowering_min_mm=0.0,
                    commanded_lowering_max_mm=6.0,
                    offset_mm=-0.00013272701600556085,
                    amplitude_mm=4.29496757977153,
                    angular_frequency_rad_per_mm=0.24349261926759336,
                    phase_rad=0.8994441869661569,
                )
            )

            controller.apply_needle_calibration(down_position_mm=-1.0)

            self.assertGreater(controller._needle_down_lowering_mm, 0.0)
            self.assertAlmostEqual(
                controller.axis_a_gcode_coordinate_for_lowering(
                    controller._needle_down_lowering_mm
                ),
                -1.0,
                places=6,
            )
        finally:
            controller.shutdown()


class StageControllerAxisMotionFitTest(unittest.TestCase):
    CALIBRATIONS = Path(__file__).resolve().parents[1] / "calibrations"

    @staticmethod
    def _rmse(values: np.ndarray) -> float:
        return float(np.sqrt(np.mean(values * values)))

    @staticmethod
    def _averaged_curve(gcode: np.ndarray, indicator: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        buckets: dict[float, list[float]] = {}
        for gcode_value, indicator_value in zip(np.round(gcode, 4), indicator):
            buckets.setdefault(float(gcode_value), []).append(float(indicator_value))
        keys = np.array(sorted(buckets), dtype=float)
        values = np.array([np.mean(buckets[float(key)]) for key in keys], dtype=float)
        return keys, values

    def test_default_axis_a_sine_fit_matches_measured_curves(self) -> None:
        controller = StageController()
        try:
            controller.apply_axis_a_calibration(
                AxisACalibrationSettings(configured=True)
            )
            datasets = [
                "axis_a_spm2600_pulloff0p25_start0p230_to-lowerlimit_step0p01_settle1p0_feed30_oneshot_20260504_223257.npz",
                "axis_a_spm2600_pulloff0p25_reverse_startm5p730_to0p230_step0p01_settle1p0_feed30_oneshot_nozero_20260504_225516.npz",
            ]
            residuals: list[np.ndarray] = []
            for filename in datasets:
                data = np.load(self.CALIBRATIONS / filename)
                commanded = -data["gcode"]
                predicted = np.array(
                    [
                        controller._axis_a_model_lowering_for_commanded(float(value))
                        for value in commanded
                    ],
                    dtype=float,
                )
                residuals.append(predicted - data["indicator"])
            residual = np.concatenate(residuals)

            self.assertLess(self._rmse(residual), 0.035)
            self.assertLess(float(np.percentile(np.abs(residual), 95)), 0.043)
            self.assertLess(float(np.max(np.abs(residual))), 0.05)
        finally:
            controller.shutdown()

    def test_default_axis_z_polynomial_fit_matches_stitched_center_curve(self) -> None:
        controller = StageController()
        settings = AxisZCalibrationSettings(configured=True)
        try:
            controller.apply_axis_z_calibration(settings)
            up_gcode, up_indicator, down_gcode, down_indicator = (
                self._stitched_z_indicator_curves(settings)
            )
            mask = (up_gcode >= 0.05) & (up_gcode <= 23.35)
            gcode = up_gcode[mask]
            center = (
                up_indicator[mask]
                + np.interp(gcode, down_gcode, down_indicator)
            ) * 0.5
            predicted = np.array(
                [
                    controller.calibrated_axis_display_value("Z", float(value))
                    for value in gcode
                ],
                dtype=float,
            )
            residual = predicted - center

            self.assertLess(self._rmse(residual), 0.007)
            self.assertLess(float(np.percentile(np.abs(residual), 95)), 0.013)
            self.assertLess(float(np.max(np.abs(residual))), 0.022)
        finally:
            controller.shutdown()

    def test_calibrated_axis_targets_round_trip(self) -> None:
        controller = StageController()
        try:
            controller.apply_axis_a_calibration(
                AxisACalibrationSettings(configured=True)
            )
            controller.apply_axis_z_calibration(
                AxisZCalibrationSettings(configured=True)
            )

            a_raw = controller.calibrated_axis_raw_value("A", -1.25)
            self.assertLess(a_raw, 0.0)
            self.assertAlmostEqual(
                controller.calibrated_axis_display_value("A", a_raw),
                -1.25,
                places=6,
            )

            z_display = controller.calibrated_axis_display_value(
                "Z",
                18.0,
            )
            z_raw = controller.calibrated_axis_raw_value(
                "Z",
                z_display,
            )
            self.assertAlmostEqual(z_raw, 18.0, places=6)
        finally:
            controller.shutdown()

    def _stitched_z_indicator_curves(
        self,
        settings: AxisZCalibrationSettings,
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        s1_up = np.load(
            self.CALIBRATIONS
            / "axis_z_spm6335_nozero_up_from0p020_until-ind9p95_step0p01_settle1p0_feed50_20260504_233107.npz"
        )
        s1_down = np.load(
            self.CALIBRATIONS
            / "axis_z_spm6335_nozero_down_to0p020_from-up-end_step0p01_settle1p0_feed50_20260504_233107.npz"
        )
        s2_up = np.load(
            self.CALIBRATIONS
            / "axis_z_spm6335_section2_nozero_up_step0p005_settle2p0_feed50_20260505_010155.npz"
        )
        s2_down = np.load(
            self.CALIBRATIONS
            / "axis_z_spm6335_section2_nozero_down_step0p005_settle2p0_feed50_20260505_010155.npz"
        )
        s3 = np.load(
            self.CALIBRATIONS
            / "axis_z_spm6335_section3_precise_start16p5_top23p4_step0p0025_settle2p0_feed1_transition10_20260505_175825.npz"
        )

        s1_up_g, s1_up_i = self._averaged_curve(s1_up["gcode"], s1_up["indicator"])
        s1_down_g, s1_down_i = self._averaged_curve(
            s1_down["gcode"],
            s1_down["indicator"],
        )
        s2_up_g, s2_up_i = self._averaged_curve(s2_up["gcode"], s2_up["indicator"])
        s2_down_g, s2_down_i = self._averaged_curve(
            s2_down["gcode"],
            s2_down["indicator"],
        )
        s2_up_i = s2_up_i + settings.section2_indicator_offset_mm
        s2_down_i = s2_down_i + settings.section2_indicator_offset_mm

        s3_up_mask = s3["direction"] > 0
        s3_down_mask = s3["direction"] < 0
        s3_up_g, s3_up_i = self._averaged_curve(
            s3["gcode"][s3_up_mask],
            s3["indicator"][s3_up_mask],
        )
        s3_down_g, s3_down_i = self._averaged_curve(
            s3["gcode"][s3_down_mask],
            s3["indicator"][s3_down_mask],
        )
        s3_up_i = s3_up_i + settings.section3_indicator_offset_mm
        s3_down_i = s3_down_i + settings.section3_indicator_offset_mm

        up_gcode = np.concatenate(
            [
                s1_up_g[s1_up_g < 12.0],
                s2_up_g[(s2_up_g >= 12.0) & (s2_up_g <= 20.214)],
                s3_up_g[s3_up_g > 20.214],
            ]
        )
        up_indicator = np.concatenate(
            [
                s1_up_i[s1_up_g < 12.0],
                s2_up_i[(s2_up_g >= 12.0) & (s2_up_g <= 20.214)],
                s3_up_i[s3_up_g > 20.214],
            ]
        )
        down_gcode = np.concatenate(
            [
                s1_down_g[s1_down_g < 12.0],
                s2_down_g[(s2_down_g >= 12.0) & (s2_down_g <= 20.214)],
                s3_down_g[s3_down_g > 20.214],
            ]
        )
        down_indicator = np.concatenate(
            [
                s1_down_i[s1_down_g < 12.0],
                s2_down_i[(s2_down_g >= 12.0) & (s2_down_g <= 20.214)],
                s3_down_i[s3_down_g > 20.214],
            ]
        )
        order = np.argsort(down_gcode)
        return up_gcode, up_indicator, down_gcode[order], down_indicator[order]


class StageControllerNeedlesStateTest(unittest.TestCase):
    def test_status_without_a_homing_keeps_needles_unknown(self) -> None:
        controller = StageController()
        emitted = []
        controller.needles_state_changed = types.SimpleNamespace(
            emit=lambda raised, known: emitted.append((raised, known))
        )
        controller.axis_a_ready_changed = types.SimpleNamespace(
            emit=lambda *_args, **_kwargs: None
        )
        controller.needle_height_changed = types.SimpleNamespace(
            emit=lambda *_args, **_kwargs: None
        )

        controller._update_needles_from_status(
            types.SimpleNamespace(
                state="Idle",
                position=None,
                display_position=(0.0, 0.0, 0.0, 0.0),
                work_position=(0.0, 0.0, 0.0, 0.0),
                homed_axes=None,
            )
        )

        self.assertEqual(emitted, [])
        self.assertFalse(controller._needles_known)
        self.assertFalse(controller._needles_up)

    def test_status_with_a_homing_marks_needles_up_when_a_is_zero(self) -> None:
        controller = StageController()
        emitted = []
        controller.needles_state_changed = types.SimpleNamespace(
            emit=lambda raised, known: emitted.append((raised, known))
        )
        controller.axis_a_ready_changed = types.SimpleNamespace(
            emit=lambda *_args, **_kwargs: None
        )
        controller.needle_height_changed = types.SimpleNamespace(
            emit=lambda *_args, **_kwargs: None
        )

        controller._update_needles_from_status(
            types.SimpleNamespace(
                state="Idle",
                position=None,
                display_position=(0.0, 0.0, 0.0, 0.0),
                work_position=(0.0, 0.0, 0.0, 0.0),
                homed_axes={"A"},
            )
        )

        self.assertEqual(emitted[-1], (True, True))

    def test_status_marks_needles_unknown_inside_contact_zone(self) -> None:
        controller = StageController()
        emitted = []
        controller.apply_needle_calibration(
            raise_position_mm=0.5,
            down_position_mm=1.0,
        )
        controller._needles_up = True
        controller._needles_known = True
        controller.needles_state_changed = types.SimpleNamespace(
            emit=lambda raised, known: emitted.append((raised, known))
        )
        controller.axis_a_ready_changed = types.SimpleNamespace(
            emit=lambda *_args, **_kwargs: None
        )
        controller.needle_height_changed = types.SimpleNamespace(
            emit=lambda *_args, **_kwargs: None
        )

        controller._update_needles_from_status(
            types.SimpleNamespace(
                state="Idle",
                position=None,
                display_position=(0.0, 0.0, 0.0, -0.975),
                work_position=(0.0, 0.0, 0.0, -0.975),
                homed_axes={"A"},
            )
        )

        self.assertEqual(emitted[-1], (False, False))

    def test_status_marks_needles_lifted_above_contact_zone(self) -> None:
        controller = StageController()
        emitted = []
        zones = []
        controller.apply_needle_calibration(
            raise_position_mm=0.5,
            down_position_mm=1.0,
        )
        controller.needles_state_changed = types.SimpleNamespace(
            emit=lambda raised, known: emitted.append((raised, known))
        )
        controller.needles_zone_changed = types.SimpleNamespace(
            emit=lambda zone: zones.append(zone)
        )
        controller.axis_a_ready_changed = types.SimpleNamespace(
            emit=lambda *_args, **_kwargs: None
        )
        controller.needle_height_changed = types.SimpleNamespace(
            emit=lambda *_args, **_kwargs: None
        )

        controller._update_needles_from_status(
            types.SimpleNamespace(
                state="Idle",
                position=None,
                display_position=(0.0, 0.0, 0.0, -0.75),
                work_position=(0.0, 0.0, 0.0, -0.75),
                homed_axes={"A"},
            )
        )

        self.assertEqual(emitted[-1], (False, True))
        self.assertEqual(zones[-1], "lift")

    def test_status_marks_needles_down_at_or_below_saved_lower(self) -> None:
        controller = StageController()
        emitted = []
        controller.apply_needle_calibration(
            raise_position_mm=0.5,
            down_position_mm=1.0,
        )
        controller.needles_state_changed = types.SimpleNamespace(
            emit=lambda raised, known: emitted.append((raised, known))
        )
        controller.axis_a_ready_changed = types.SimpleNamespace(
            emit=lambda *_args, **_kwargs: None
        )
        controller.needle_height_changed = types.SimpleNamespace(
            emit=lambda *_args, **_kwargs: None
        )

        controller._update_needles_from_status(
            types.SimpleNamespace(
                state="Idle",
                position=None,
                display_position=(0.0, 0.0, 0.0, -1.1),
                work_position=(0.0, 0.0, 0.0, -1.1),
                homed_axes={"A"},
            )
        )

        self.assertEqual(emitted[-1], (False, True))

    def test_latest_a_position_reads_cached_stage_position(self) -> None:
        controller = StageController()
        controller._last_stage_position = (1.0, 2.0, 3.0, -0.25)

        self.assertEqual(controller.latest_a_position(), -0.25)


class StageControllerPriorityNeedlesActionTest(unittest.TestCase):
    def test_cancel_active_motion_sends_jog_cancel_without_invalidating_state(self) -> None:
        controller = StageController()
        controller._serial = _WritableFakeSerial()
        controller._homed_axes = {"X", "Y", "A"}
        controller._needles_up = True
        controller._needles_known = True
        messages = []
        controller.status_message = types.SimpleNamespace(
            emit=lambda message: messages.append(message)
        )

        controller.cancel_active_motion("Coordinate move cancel requested.")

        self.assertIn(b"\x85", controller._serial.writes)
        self.assertEqual(controller._homed_axes, {"X", "Y", "A"})
        self.assertTrue(controller._needles_up)
        self.assertTrue(controller._needles_known)
        self.assertEqual(messages[-1], "Coordinate move cancel requested.")

    def test_cancel_active_task_preserves_homing_without_soft_reset(self) -> None:
        controller = StageController()
        controller._serial = _WritableFakeSerial()
        controller._homed_axes = {"X", "Y", "A"}
        controller._needles_up = False
        controller._needles_known = True
        controller._active_needles_action = "lower"
        controller._active_needles_programmed_feedrate = 80.0
        controller._queued_needles_actions.append(("raise", None, None))
        controller._oscillation_needles_actions.append(("lower", None, None))
        messages = []
        controller.status_message = types.SimpleNamespace(
            emit=lambda message: messages.append(message)
        )
        try:
            controller.cancel_active_task("Needle move cancel requested.")
            controller._async_write_queue.join()

            self.assertTrue(controller._cancel_event.is_set())
            self.assertIn(b"\x85", controller._serial.writes)
            self.assertNotIn(b"\x18", controller._serial.writes)
            self.assertEqual(controller._homed_axes, {"X", "Y", "A"})
            self.assertFalse(controller._needles_known)
            self.assertIsNone(controller._active_needles_action)
            self.assertIsNone(controller._active_needles_programmed_feedrate)
            self.assertEqual(list(controller._queued_needles_actions), [])
            self.assertEqual(list(controller._oscillation_needles_actions), [])
            self.assertEqual(messages[0], "Needle move cancel requested.")
        finally:
            controller.shutdown()

    def test_cancel_active_task_without_needles_preserves_needle_state(self) -> None:
        controller = StageController()
        controller._serial = _WritableFakeSerial()
        controller._homed_axes = {"X", "Y", "Z", "A"}
        controller._needles_up = True
        controller._needles_known = True
        messages = []
        controller.status_message = types.SimpleNamespace(
            emit=lambda message: messages.append(message)
        )
        try:
            controller.cancel_active_task("Autofocus cancel requested.")
            controller._async_write_queue.join()

            self.assertTrue(controller._cancel_event.is_set())
            self.assertEqual(controller._serial.writes, [b"\x85"])
            self.assertEqual(controller._homed_axes, {"X", "Y", "Z", "A"})
            self.assertTrue(controller._needles_up)
            self.assertTrue(controller._needles_known)
            self.assertEqual(messages[-1], "Autofocus cancel requested.")
        finally:
            controller.shutdown()

    def test_reset_controller_soft_resets_and_clears_unverified_state(self) -> None:
        controller = StageController()
        controller._serial = _WritableFakeSerial()
        controller._homed_axes = {"X", "Y", "A"}
        controller._needles_up = True
        controller._needles_known = True
        messages = []
        controller.status_message = types.SimpleNamespace(
            emit=lambda message: messages.append(message)
        )
        try:
            controller.reset_controller(source="test", reason="Reset requested.")
            controller._async_write_queue.join()

            self.assertTrue(controller._cancel_event.is_set())
            self.assertIn(b"\x18", controller._serial.writes)
            self.assertEqual(controller._homed_axes, set())
            self.assertFalse(controller._needles_known)
            self.assertEqual(messages[0], "Reset requested.")
        finally:
            controller.shutdown()

    def test_relative_move_can_be_sent_as_cancelable_jog(self) -> None:
        controller = StageController()
        serial_connection = _LineFakeSerial([b"ok\n"])
        controller.set_motion_safety_disabled(True)
        controller._wait_for_idle = lambda *_args, **_kwargs: None

        controller._send_relative_move(
            serial_connection,
            MoveVector(x=0.5, z=-0.1),
            feedrate=12.3,
            as_jog=True,
        )

        self.assertEqual(
            serial_connection.writes,
            [b"\x90", b"$J=G91 G21 X0.5000 Z-0.1000 F12.3\n"],
        )

    def test_needles_lower_queues_during_oscillation(self) -> None:
        controller = StageController()
        controller._oscillation_active = True
        controller._active_thread = types.SimpleNamespace(is_alive=lambda: True)
        messages = []
        controller.needles_action_started = types.SimpleNamespace(
            emit=lambda *_args, **_kwargs: None
        )
        controller.status_message = types.SimpleNamespace(
            emit=lambda message: messages.append(message)
        )

        controller.request_needles_lower()

        self.assertFalse(controller._cancel_event.is_set())
        self.assertEqual(
            list(controller._oscillation_needles_actions), [("lower", None, None)]
        )
        self.assertIn("queued during oscillation", messages[-1])


class StageControllerStatusRefreshTest(unittest.TestCase):
    def test_status_refresh_is_suppressed_while_jog_is_active(self) -> None:
        controller = StageController()
        controller._serial = _FakeSerial()
        controller.status_message = types.SimpleNamespace(
            emit=lambda *_args, **_kwargs: None
        )
        started = []
        controller._run_status_refresh = lambda: started.append(True)

        controller._jog_motion_active = True
        controller.request_status_refresh()

        self.assertEqual(started, [])

        controller._jog_motion_active = False
        controller.request_status_refresh()
        thread = controller._status_refresh_thread
        if thread is not None:
            thread.join(timeout=1.0)

        self.assertEqual(started, [True])


class StageControllerStartupSyncTest(unittest.TestCase):
    def test_startup_sync_homes_a_when_not_reported_homed(self) -> None:
        controller = StageController()
        controller._serial = _FakeSerial()
        controller._refresh_coordinate_system_state = (
            lambda _serial, apply_preference=True: None
        )
        controller._ensure_axis_limits = lambda _serial, **_kwargs: None
        controller._ensure_controller_session_marker = lambda _serial: None
        controller._query_status = lambda _serial: types.SimpleNamespace(
            state="Idle",
            position=(0.0, 0.0, 0.0, 0.0),
            homed_axes={"X", "Y"},
        )
        performed = []
        controller._perform_home_command = (
            lambda _serial, command: performed.append(command)
        )
        controller.status_message = types.SimpleNamespace(
            emit=lambda *_args, **_kwargs: None
        )
        controller.movement_started = types.SimpleNamespace(
            emit=lambda *_args, **_kwargs: None
        )
        controller.movement_finished = types.SimpleNamespace(
            emit=lambda *_args, **_kwargs: None
        )
        controller.homing_action_started = types.SimpleNamespace(
            emit=lambda *_args, **_kwargs: None
        )
        controller.homing_action_finished = types.SimpleNamespace(
            emit=lambda *_args, **_kwargs: None
        )

        controller._run_startup_sync(auto_home_a=True)

        self.assertEqual(performed, ["$HA"])

    def test_startup_sync_skips_a_homing_when_axis_is_homed(self) -> None:
        controller = StageController()
        controller._serial = _FakeSerial()
        controller._refresh_coordinate_system_state = (
            lambda _serial, apply_preference=True: None
        )
        controller._ensure_axis_limits = lambda _serial, **_kwargs: None
        controller._ensure_controller_session_marker = lambda _serial: None
        controller._query_status = lambda _serial: types.SimpleNamespace(
            state="Idle",
            position=(0.0, 0.0, 0.0, 0.0),
            homed_axes={"X", "Y", "A"},
        )
        performed = []
        controller._perform_home_command = (
            lambda _serial, command: performed.append(command)
        )
        controller.status_message = types.SimpleNamespace(
            emit=lambda *_args, **_kwargs: None
        )
        controller.movement_started = types.SimpleNamespace(
            emit=lambda *_args, **_kwargs: None
        )
        controller.movement_finished = types.SimpleNamespace(
            emit=lambda *_args, **_kwargs: None
        )
        controller.homing_action_started = types.SimpleNamespace(
            emit=lambda *_args, **_kwargs: None
        )
        controller.homing_action_finished = types.SimpleNamespace(
            emit=lambda *_args, **_kwargs: None
        )

        controller._run_startup_sync(auto_home_a=True)

        self.assertEqual(performed, [])

    def test_startup_sync_uses_cached_homing_when_status_does_not_report_it(self) -> None:
        controller = StageController()
        controller._serial = _FakeSerial()
        controller._homed_axes = {"X", "Y", "A"}
        controller._refresh_coordinate_system_state = (
            lambda _serial, apply_preference=True: None
        )
        controller._ensure_axis_limits = lambda _serial, **_kwargs: None
        controller._ensure_controller_session_marker = lambda _serial: None
        controller._query_status = lambda _serial: types.SimpleNamespace(
            state="Idle",
            position=(1.0, 2.0, 3.0, 0.0),
            work_position=(1.0, 2.0, 3.0, 0.0),
            display_position=(1.0, 2.0, 3.0, 0.0),
            homed_axes=None,
            coordinate_system="G54",
        )
        performed = []
        controller._perform_home_command = (
            lambda _serial, command: performed.append(command)
        )
        controller.status_message = types.SimpleNamespace(
            emit=lambda *_args, **_kwargs: None
        )
        controller.movement_started = types.SimpleNamespace(
            emit=lambda *_args, **_kwargs: None
        )
        controller.movement_finished = types.SimpleNamespace(
            emit=lambda *_args, **_kwargs: None
        )
        controller.homing_action_started = types.SimpleNamespace(
            emit=lambda *_args, **_kwargs: None
        )
        controller.homing_action_finished = types.SimpleNamespace(
            emit=lambda *_args, **_kwargs: None
        )

        controller._run_startup_sync(auto_home_a=True)

        self.assertEqual(performed, [])

    def test_startup_sync_uses_cached_axis_feedrates_without_config_dump(self) -> None:
        controller = StageController()
        controller._serial = _FakeSerial()
        controller._axis_max_feedrates = {"X": 500.0, "Z": 100.0, "A": 80.0}
        controller._refresh_coordinate_system_state = (
            lambda _serial, apply_preference=True: None
        )
        controller._ensure_axis_limits = lambda _serial, **_kwargs: None
        controller._ensure_controller_session_marker = lambda _serial: None
        controller._query_axis_max_feedrates_locked = (
            lambda _serial: (_ for _ in ()).throw(AssertionError("$CD should be skipped"))
        )
        controller._query_status = lambda _serial: types.SimpleNamespace(
            state="Idle",
            position=(1.0, 2.0, 3.0, 0.0),
            work_position=(1.0, 2.0, 3.0, 0.0),
            display_position=(1.0, 2.0, 3.0, 0.0),
            homed_axes={"X", "Y", "Z", "A"},
            coordinate_system="G54",
        )
        emitted_feedrates = []
        controller.status_message = types.SimpleNamespace(
            emit=lambda *_args, **_kwargs: None
        )
        controller.axis_max_feedrates_changed = types.SimpleNamespace(
            emit=lambda rates: emitted_feedrates.append(dict(rates))
        )
        controller.stage_position_changed = types.SimpleNamespace(
            emit=lambda *_args, **_kwargs: None
        )

        controller._run_startup_sync(auto_home_a=True)

        self.assertEqual(
            emitted_feedrates,
            [{"X": 500.0, "Z": 100.0, "A": 80.0}],
        )


class StageControllerReconnectStateTest(unittest.TestCase):
    def test_disconnect_clears_unverified_controller_state(self) -> None:
        controller = StageController()
        controller._last_stage_position = (1.0, 2.0, 3.0, 0.0)
        controller._homed_axes = {"X", "Y", "A"}
        controller._needles_up = True
        controller._needles_known = True
        controller._axis_a_ready = True
        axis_ready = []
        controller.axis_a_ready_changed = types.SimpleNamespace(
            emit=lambda ready: axis_ready.append(bool(ready))
        )

        controller.set_serial(None)

        self.assertIsNone(controller._last_stage_position)
        self.assertEqual(controller._homed_axes, set())
        self.assertFalse(controller._needles_up)
        self.assertFalse(controller._needles_known)
        self.assertTrue(controller._controller_state_stale)
        self.assertFalse(controller._axis_a_ready)
        self.assertEqual(axis_ready[-1], False)

    def test_reconnect_without_reboot_clears_unverified_state_until_sync(self) -> None:
        controller = StageController()
        controller._last_stage_position = (1.0, 2.0, 3.0, 0.0)
        controller._homed_axes = {"X", "Y", "A"}
        controller._needles_up = True
        controller._needles_known = True
        fake_serial = _FakeSerial()

        controller.set_serial(fake_serial)

        self.assertIsNone(controller._last_stage_position)
        self.assertEqual(controller._homed_axes, set())
        self.assertFalse(controller._needles_up)
        self.assertFalse(controller._needles_known)
        self.assertTrue(controller._controller_state_stale)
        self.assertFalse(controller._axis_a_ready)

    def test_set_serial_allows_position_signal_slot_to_query_busy(self) -> None:
        controller = StageController()
        busy_values = []
        controller.stage_position_changed = types.SimpleNamespace(
            emit=lambda _position: busy_values.append(controller.is_busy())
        )

        controller.set_serial(_FakeSerial())

        self.assertEqual(busy_values, [False])

    def test_reconnect_with_reboot_clears_cached_state(self) -> None:
        controller = StageController()
        controller._last_stage_position = (1.0, 2.0, 3.0, 0.0)
        controller._homed_axes = {"X", "Y", "A"}
        controller._needles_up = True
        controller._needles_known = True
        state_changes = []
        controller.needles_state_changed = types.SimpleNamespace(
            emit=lambda raised, known: state_changes.append((raised, known))
        )
        controller.homing_status_changed = types.SimpleNamespace(
            emit=lambda *_args, **_kwargs: None
        )
        controller.axis_a_ready_changed = types.SimpleNamespace(
            emit=lambda *_args, **_kwargs: None
        )
        fake_serial = _FakeSerial()
        fake_serial.probe_station_reboot_detected = True

        controller.set_serial(fake_serial)

        self.assertIsNone(controller._last_stage_position)
        self.assertEqual(controller._homed_axes, set())
        self.assertFalse(controller._needles_up)
        self.assertFalse(controller._needles_known)
        self.assertTrue(controller._controller_state_stale)
        self.assertEqual(state_changes[-1], (False, False))

    def test_import_cached_state_restores_homing_but_not_coordinates(self) -> None:
        controller = StageController()
        positions = []
        controller.stage_position_changed = types.SimpleNamespace(
            emit=lambda position: positions.append(position)
        )
        controller.homing_status_changed = types.SimpleNamespace(
            emit=lambda *_args, **_kwargs: None
        )
        controller.needles_state_changed = types.SimpleNamespace(
            emit=lambda *_args, **_kwargs: None
        )
        controller.axis_a_ready_changed = types.SimpleNamespace(
            emit=lambda *_args, **_kwargs: None
        )

        controller.import_cached_controller_state(
            {
                "last_stage_position": [1.0, 2.0, 3.0, 0.0],
                "last_stage_state": "Idle",
                "active_work_coordinate_system": "G54",
                "controller_coordinate_offsets": {
                    "G54": [32.0, 32.0, 0.0, -2.885, 0.0],
                },
                "homed_axes": ["X", "Y", "A"],
                "needles_up": True,
                "needles_known": True,
                "controller_session_marker": 321,
                "axis_limits": {
                    "X": [0.0, 64.0],
                    "Y": [0.0, 64.0],
                    "Z": [0.0, 23.0],
                    "A": [-5.5, 0.0],
                },
                "axis_max_feedrates": {"X": 500.0, "Z": 100.0, "A": 80.0},
            }
        )

        self.assertIsNone(controller._last_stage_position)
        self.assertEqual(controller._homed_axes, {"X", "Y", "A"})
        self.assertTrue(controller._needles_up)
        self.assertTrue(controller._needles_known)
        self.assertEqual(controller._controller_session_marker, 321)
        self.assertTrue(controller._controller_state_stale)
        self.assertFalse(controller._axis_a_ready)
        self.assertEqual(positions, [])
        self.assertEqual(controller._axis_limits["Z"], (0.0, 23.0))
        self.assertEqual(controller._axis_max_feedrates["X"], 500.0)
        self.assertEqual(controller._active_work_coordinate_system, "G54")
        self.assertEqual(
            controller._controller_coordinate_offsets["G54"],
            (32.0, 32.0, 0.0, -2.885, 0.0),
        )

    def test_status_reader_detects_live_controller_reboot_and_clears_homing(self) -> None:
        controller = StageController()
        controller._homed_axes = {"A", "Y"}
        controller._needles_up = True
        controller._needles_known = True
        controller._controller_state_stale = False
        controller._controller_session_marker = 321
        controller._last_stage_position = (1.0, 2.0, 3.0, 0.0)
        controller._axis_limits = {"Z": (0.0, 23.0)}
        controller._axis_max_feedrates = {"Z": 100.0}
        positions = []
        reboot_events = []
        controller.stage_position_changed = types.SimpleNamespace(
            emit=lambda position: positions.append(position)
        )
        controller.controller_reboot_detected = types.SimpleNamespace(
            emit=lambda: reboot_events.append(True)
        )
        serial_connection = _LineFakeSerial(
            [b"rst:0x1 (POWERON_RESET),boot:0x13 (SPI_FAST_FLASH_BOOT)\n"]
        )

        with self.assertRaises(StageControllerError):
            controller._read_status_frame(serial_connection, timeout=0.1)

        self.assertEqual(controller._homed_axes, set())
        self.assertFalse(controller._needles_up)
        self.assertFalse(controller._needles_known)
        self.assertTrue(controller._controller_state_stale)
        self.assertIsNone(controller._controller_session_marker)
        self.assertEqual(controller._axis_limits, {})
        self.assertEqual(controller._axis_max_feedrates, {})
        self.assertEqual(positions[-1], None)
        self.assertEqual(reboot_events, [True])

    def test_repeated_live_controller_reboot_lines_emit_one_recovery_event(self) -> None:
        controller = StageController()
        reboot_events = []
        controller.controller_reboot_detected = types.SimpleNamespace(
            emit=lambda: reboot_events.append(True)
        )
        serial_connection = _LineFakeSerial(
            [
                b"rst:0x1 (POWERON_RESET),boot:0x13 (SPI_FAST_FLASH_BOOT)\n",
                b"[MSG:RST]\n",
            ]
        )

        for _ in range(2):
            with self.assertRaises(StageControllerError):
                controller._read_status_frame(serial_connection, timeout=0.1)

        self.assertEqual(reboot_events, [True])

    def test_valid_status_after_live_reboot_emits_recovery_ready_once(self) -> None:
        controller = StageController()
        controller._controller_reboot_recovery_pending = True
        controller._current_status_report_mask = 2
        ready_events = []
        controller.controller_reboot_ready = types.SimpleNamespace(
            emit=lambda: ready_events.append(True)
        )
        serial_connection = _LineFakeSerial(
            [
                b"<Idle|WPos:0.000,0.000,0.000,0.000,0.000|Bf:15,127|FS:0,0>\n",
                b"<Idle|WPos:0.000,0.000,0.000,0.000,0.000|Bf:15,127|FS:0,0>\n",
            ]
        )

        self.assertIsNotNone(controller._query_status(serial_connection))
        self.assertIsNotNone(controller._query_status(serial_connection))

        self.assertEqual(ready_events, [True])

    def test_terminal_pending_read_detects_live_controller_reboot(self) -> None:
        controller = StageController()
        serial_connection = _BufferedFakeSerial(
            b"rst:0x1 (POWERON_RESET),boot:0x13 (SPI_FAST_FLASH_BOOT)\r\n"
        )
        controller._serial = serial_connection
        controller._homed_axes = {"A"}
        controller._needles_up = True
        controller._needles_known = True
        controller._controller_state_stale = False
        controller._controller_session_marker = 321
        positions = []
        controller.stage_position_changed = types.SimpleNamespace(
            emit=lambda position: positions.append(position)
        )

        data = controller.read_pending_serial_output()

        self.assertIn(b"POWERON_RESET", data)
        self.assertEqual(controller._homed_axes, set())
        self.assertFalse(controller._needles_known)
        self.assertIsNone(controller._controller_session_marker)
        self.assertEqual(positions[-1], None)

    def test_perform_home_command_marks_a_ready_and_updates_homing(self) -> None:
        controller = StageController()
        controller._serial = _FakeSerial()
        controller._controller_state_stale = True
        controller.status_message = types.SimpleNamespace(
            emit=lambda *_args, **_kwargs: None
        )
        controller.axis_a_ready_changed = types.SimpleNamespace(
            emit=lambda *_args, **_kwargs: None
        )
        controller.needles_state_changed = types.SimpleNamespace(
            emit=lambda *_args, **_kwargs: None
        )
        controller.homing_status_changed = types.SimpleNamespace(
            emit=lambda *_args, **_kwargs: None
        )
        controller._write_command = lambda *_args, **_kwargs: None
        controller._wait_for_ok = lambda *_args, **_kwargs: None
        controller._wait_for_idle = lambda *_args, **_kwargs: None

        controller.set_serial(_FakeSerial())
        controller._perform_home_command(controller._serial, "$HA")

        self.assertIn("A", controller._homed_axes)
        self.assertFalse(controller._controller_state_stale)
        self.assertTrue(controller._needles_up)
        self.assertTrue(controller._needles_known)
        self.assertTrue(controller._axis_a_ready)
        self.assertIsNotNone(controller._controller_session_marker)


if __name__ == "__main__":
    unittest.main()
