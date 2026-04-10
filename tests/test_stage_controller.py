import importlib.util
import sys
import threading
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
QueuedSerialWrite = _stage_controller_module._QueuedSerialWrite


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


class _FakeSerial:
    def __init__(self) -> None:
        self.is_open = True
        self.probe_station_reboot_detected = False


class StageControllerAbsoluteMoveTest(unittest.TestCase):
    def test_absolute_xy_move_uses_relative_delta(self) -> None:
        controller = StageController()
        controller._serial = _FakeSerial()

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
    def test_parse_status_line_uses_cached_work_offset(self) -> None:
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
            "<Idle|MPos:25.106,25.401,9.207,0.000,2.170|Bf:15,127|FS:0,0>"
        )

        self.assertIsNotNone(status)
        assert status is not None
        self.assertEqual(status.position[:2], (25.106, 25.401))
        self.assertEqual(status.work_offset[:2], (32.0, 32.0))
        self.assertAlmostEqual(status.work_position[0], -6.894)
        self.assertAlmostEqual(status.work_position[1], -6.599)


class StageControllerJogQueueTest(unittest.TestCase):
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
                position=(0.0, 0.0, 0.0, 0.0),
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
                position=(0.0, 0.0, 0.0, 0.0),
                homed_axes={"A"},
            )
        )

        self.assertEqual(emitted[-1], (True, True))


class StageControllerStartupSyncTest(unittest.TestCase):
    def test_startup_sync_homes_a_when_not_reported_homed(self) -> None:
        controller = StageController()
        controller._serial = _FakeSerial()
        controller._refresh_coordinate_system_state = (
            lambda _serial, apply_preference=True: None
        )
        controller._ensure_axis_limits = lambda _serial: None
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


class StageControllerReconnectStateTest(unittest.TestCase):
    def test_disconnect_preserves_cached_state_but_marks_it_stale(self) -> None:
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

        self.assertEqual(controller._last_stage_position, (1.0, 2.0, 3.0, 0.0))
        self.assertEqual(controller._homed_axes, {"X", "Y", "A"})
        self.assertTrue(controller._needles_up)
        self.assertTrue(controller._needles_known)
        self.assertTrue(controller._controller_state_stale)
        self.assertFalse(controller._axis_a_ready)
        self.assertEqual(axis_ready[-1], False)

    def test_reconnect_without_reboot_keeps_cached_state_until_sync(self) -> None:
        controller = StageController()
        controller._last_stage_position = (1.0, 2.0, 3.0, 0.0)
        controller._homed_axes = {"X", "Y", "A"}
        controller._needles_up = True
        controller._needles_known = True
        fake_serial = _FakeSerial()

        controller.set_serial(fake_serial)

        self.assertEqual(controller._last_stage_position, (1.0, 2.0, 3.0, 0.0))
        self.assertEqual(controller._homed_axes, {"X", "Y", "A"})
        self.assertTrue(controller._needles_up)
        self.assertTrue(controller._needles_known)
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

    def test_import_cached_state_restores_state_but_keeps_it_stale(self) -> None:
        controller = StageController()
        positions = []
        controller.stage_position_changed = types.SimpleNamespace(
            emit=lambda position: positions.append(tuple(position))
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
            }
        )

        self.assertEqual(controller._last_stage_position, (1.0, 2.0, 3.0, 0.0))
        self.assertEqual(controller._homed_axes, {"X", "Y", "A"})
        self.assertTrue(controller._needles_up)
        self.assertTrue(controller._needles_known)
        self.assertTrue(controller._controller_state_stale)
        self.assertFalse(controller._axis_a_ready)
        self.assertEqual(positions[-1], (1.0, 2.0, 3.0, 0.0))

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


if __name__ == "__main__":
    unittest.main()
