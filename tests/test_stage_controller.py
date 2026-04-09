import importlib.util
import sys
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

    class QImage:  # noqa: N801 - mimic Qt type name
        pass

    qtcore.QObject = QObject
    qtcore.Signal = Signal
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


StageController = _load_stage_controller().StageController


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


class StageControllerAbsoluteMoveTest(unittest.TestCase):
    def test_absolute_xy_move_uses_relative_delta(self) -> None:
        controller = StageController()
        controller._serial = _FakeSerial()

        sent_moves = []
        statuses = [
            types.SimpleNamespace(state="Idle", position=(10.0, 20.0, 0.0), homed_axes={"X", "Y"}),
            types.SimpleNamespace(state="Idle", position=(15.0, 26.0, 0.0), homed_axes={"X", "Y"}),
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


if __name__ == "__main__":
    unittest.main()
