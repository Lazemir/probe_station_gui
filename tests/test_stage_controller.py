import importlib.util
import sys
import threading
import time
import types
import unittest
from pathlib import Path


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


_stage_controller_module = _load_stage_controller()
StageController = _stage_controller_module.StageController
StageControllerError = _stage_controller_module.StageControllerError
QueuedSerialWrite = _stage_controller_module._QueuedSerialWrite
MoveVector = _stage_controller_module.MoveVector


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

    def test_absolute_axis_move_respects_homed_axis_soft_limit(self) -> None:
        controller = StageController()
        controller._serial = _FakeSerial()
        controller._position_reporting_mode = "machine"
        controller._axis_limits = {"Z": (0.0, 20.0), "X": (0.0, 64.0)}
        controller._ensure_axis_limits = lambda _serial: None
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
        controller._send_relative_move = lambda _serial, move: sent_moves.append(move)
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
        controller._refresh_coordinate_system_state = (
            lambda _serial, apply_preference=True: None
        )
        controller._wait_for_idle = lambda _serial: None
        controller._query_status = lambda _serial: statuses.pop(0)
        controller._send_relative_move = lambda _serial, move: sent_moves.append(move)
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
        self.assertEqual(movement_results[-1][0], True)

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
        controller._send_relative_move = lambda _serial, move: sent_moves.append(move)
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
        controller._ensure_calibration = lambda _serial: None
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

        def _send_relative_move(_serial, _move) -> None:
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

        self.assertIn("G90", commands)
        self.assertNotIn("G91", commands)
        self.assertIn("G1 A-0.1200 F1", commands)
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
        controller._ensure_axis_limits = lambda _serial: None
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

        self.assertIn("G90", commands)
        self.assertNotIn("G91", commands)
        self.assertIn("G1 X1.2500 F10", commands)
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

        self.assertIn("G90", commands)
        self.assertIn("G1 A0.0000 F5", commands)
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
                    commanded_lowering_max_mm=5.0,
                    offset_mm=0.006879563812405575,
                    amplitude_mm=4.175160198502771,
                    angular_frequency_rad_per_mm=0.25075568892433536,
                    phase_rad=0.8855481310064558,
                )
            )

            target_a = controller.axis_a_gcode_coordinate_for_lowering(0.02)

            self.assertLess(target_a, 0.0)
            self.assertAlmostEqual(target_a, -0.0246108657, places=6)
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
                    commanded_lowering_max_mm=5.0,
                    offset_mm=0.006879563812405575,
                    amplitude_mm=4.175160198502771,
                    angular_frequency_rad_per_mm=0.25075568892433536,
                    phase_rad=0.8855481310064558,
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
            self.assertAlmostEqual(targets[0][1], -0.0246108657, places=6)
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
                    commanded_lowering_max_mm=5.0,
                    offset_mm=0.006879563812405575,
                    amplitude_mm=4.175160198502771,
                    angular_frequency_rad_per_mm=0.25075568892433536,
                    phase_rad=0.8855481310064558,
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

    def test_legacy_negative_saved_a_position_is_treated_as_raw_coordinate(self) -> None:
        controller = StageController()
        try:
            controller.apply_axis_a_calibration(
                types.SimpleNamespace(
                    configured=True,
                    model="cosine_displacement",
                    steps_per_mm=2500.0,
                    commanded_lowering_min_mm=0.0,
                    commanded_lowering_max_mm=5.0,
                    offset_mm=0.006879563812405575,
                    amplitude_mm=4.175160198502771,
                    angular_frequency_rad_per_mm=0.25075568892433536,
                    phase_rad=0.8855481310064558,
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

    def test_latest_a_position_reads_cached_stage_position(self) -> None:
        controller = StageController()
        controller._last_stage_position = (1.0, 2.0, 3.0, -0.25)

        self.assertEqual(controller.latest_a_position(), -0.25)


class StageControllerPriorityNeedlesActionTest(unittest.TestCase):
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
            list(controller._oscillation_needles_actions), [("lower", None)]
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
        controller._ensure_axis_limits = lambda _serial: None
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
        controller._ensure_axis_limits = lambda _serial: None
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
        controller._ensure_axis_limits = lambda _serial: None
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
                "homed_axes": ["X", "Y", "A"],
                "needles_up": True,
                "needles_known": True,
                "controller_session_marker": 321,
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

    def test_status_reader_detects_live_controller_reboot_and_clears_homing(self) -> None:
        controller = StageController()
        controller._homed_axes = {"A", "Y"}
        controller._needles_up = True
        controller._needles_known = True
        controller._controller_state_stale = False
        controller._controller_session_marker = 321
        controller._last_stage_position = (1.0, 2.0, 3.0, 0.0)
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
