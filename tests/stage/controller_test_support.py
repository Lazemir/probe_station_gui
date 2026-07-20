import importlib.util
import sys
import time
import types
from pathlib import Path

_ORIGINAL_PYSIDE6 = {
    name: module
    for name, module in sys.modules.items()
    if name == "PySide6" or name.startswith("PySide6.")
}
_MISSING_MODULE = object()
_ORIGINAL_STUBBED_MODULES = {
    name: sys.modules.get(name, _MISSING_MODULE) for name in ("cv2", "serial")
}


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


def _restore_pyside6_modules() -> None:
    for name in list(sys.modules):
        if name == "PySide6" or name.startswith("PySide6."):
            del sys.modules[name]
    sys.modules.update(_ORIGINAL_PYSIDE6)
    for name, original in _ORIGINAL_STUBBED_MODULES.items():
        if original is _MISSING_MODULE:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = original


def _load_stage_controller():
    _install_pyside6_stubs()
    module_path = (
        Path(__file__).resolve().parents[2]
        / "probe_station_gui"
        / "stage"
        / "controller.py"
    )
    spec = importlib.util.spec_from_file_location("stage_controller_test", module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _load_settings_manager():
    module_path = (
        Path(__file__).resolve().parents[2]
        / "probe_station_gui"
        / "settings"
        / "manager.py"
    )
    spec = importlib.util.spec_from_file_location(
        "settings_manager_stage_test", module_path
    )
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_stage_controller_module = _load_stage_controller()
_settings_manager_module = _load_settings_manager()
_restore_pyside6_modules()
StageController = _stage_controller_module.StageController
StageControllerError = _stage_controller_module.StageControllerError
QueuedSerialWrite = _stage_controller_module._QueuedSerialWrite
MoveVector = _stage_controller_module.MoveVector
AxisCalibrationSettings = _settings_manager_module.AxisCalibrationSettings
FocusSweepResult = _stage_controller_module._FocusSweepResult
AutofocusContext = _stage_controller_module._AutofocusContext


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


class _SwappingLineFakeSerial(_LineFakeSerial):
    def __init__(self, lines: list[bytes], on_write) -> None:
        super().__init__(lines)
        self._on_write = on_write

    def write(self, payload: bytes) -> None:
        super().write(payload)
        self._on_write(payload)


class _RejectingSerial(_WritableFakeSerial):
    def write(self, payload: bytes) -> None:
        self.writes.append(payload)
        raise AssertionError(f"replacement serial used for {payload!r}")

    def readline(self) -> bytes:
        raise AssertionError("replacement serial read")


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


class _SwapOnFirstAcquireLock(_TrackingLock):
    def __init__(self, on_acquire) -> None:
        super().__init__()
        self._on_acquire = on_acquire
        self._swapped = False

    def acquire(self, blocking: bool = True, timeout: float = -1.0) -> bool:
        acquired = super().acquire(blocking=blocking, timeout=timeout)
        if acquired and not self._swapped:
            self._swapped = True
            self._on_acquire()
        return acquired


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


class _BufferedLineFakeSerial(_LineFakeSerial):
    def __init__(self, data: bytes, lines: list[bytes]) -> None:
        super().__init__(lines)
        self.data = bytearray(data)
        self.discarded = bytearray()

    @property
    def in_waiting(self) -> int:
        return len(self.data)

    def read(self, size: int) -> bytes:
        chunk = bytes(self.data[:size])
        del self.data[:size]
        self.discarded.extend(chunk)
        return chunk


class _ResetTrackingLineFakeSerial(_LineFakeSerial):
    def __init__(self, lines: list[bytes]) -> None:
        super().__init__(lines)
        self.reset_input_buffer_calls = 0

    def reset_input_buffer(self) -> None:
        self.reset_input_buffer_calls += 1
