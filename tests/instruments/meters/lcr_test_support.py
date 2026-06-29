import sys
import types

_ORIGINAL_PYSIDE6 = {
    name: module
    for name, module in sys.modules.items()
    if name == "PySide6" or name.startswith("PySide6.")
}


def _module_names(root: str) -> list[str]:
    dotted = f"{root}."
    return [
        name
        for name in list(sys.modules)
        if name == root or name.startswith(dotted)
    ]


def _delete_modules(root: str) -> None:
    for name in _module_names(root):
        del sys.modules[name]


def _restore_real_imports() -> None:
    _delete_modules("PySide6")
    package = sys.modules.get("probe_station_gui")
    if package is not None and not getattr(package, "__file__", ""):
        _delete_modules("probe_station_gui")


def _restore_pyside6_modules() -> None:
    _delete_modules("PySide6")
    sys.modules.update(_ORIGINAL_PYSIDE6)


def _install_pyside6_stubs_if_missing() -> None:
    try:
        __import__("PySide6.QtCore")
        return
    except ModuleNotFoundError:
        pass

    qtcore = types.ModuleType("PySide6.QtCore")

    class QObject:
        def __init__(self, *_args, **_kwargs) -> None:
            super().__init__()

    class Signal:
        def __init__(self, *_args, **_kwargs) -> None:
            self.emissions = []

        def connect(self, *_args, **_kwargs) -> None:
            return None

        def emit(self, *args) -> None:
            self.emissions.append(args)

    qtcore.QObject = QObject
    qtcore.Signal = Signal
    qtcore.Qt = types.SimpleNamespace(
        ConnectionType=types.SimpleNamespace(DirectConnection=object())
    )
    pyside6 = types.ModuleType("PySide6")
    sys.modules["PySide6"] = pyside6
    sys.modules["PySide6.QtCore"] = qtcore


_restore_real_imports()
_install_pyside6_stubs_if_missing()

import probe_station_gui.instruments.meters.lcr as lcr_module
from PySide6.QtCore import Qt
from probe_station_gui.instruments.meters.lcr import (
    GWInstekRouteMeterSettings,
    KeithleyRouteMeterSettings,
    LCRMeterError,
    LCRMeterController,
    ROUTE_METER_GWINSTEK,
    ROUTE_METER_KEITHLEY,
    RouteMeter,
    RouteMeterConfiguration,
    _LCRSession,
)

_restore_pyside6_modules()


def _connect_direct(signal, slot) -> None:
    signal.connect(slot, Qt.ConnectionType.DirectConnection)


class _FakeInstrument:
    def __init__(self) -> None:
        self.trigger_fetch_called = False
        self.fetch_main_called = False

    def trigger_fetch(self):
        self.trigger_fetch_called = True
        return types.SimpleNamespace(primary=12.5)

    def fetch_main(self):
        self.fetch_main_called = True
        return types.SimpleNamespace(primary=7.5)


class _ConfiguringFakeInstrument:
    def __init__(self, responses: dict[str, str]) -> None:
        self.responses = dict(responses)
        self.operations: list[tuple[str, str]] = []

    def write(self, command: str) -> None:
        self.operations.append(("write", command))

    def ask(self, query: str) -> str:
        self.operations.append(("ask", query))
        return self.responses[query]


class _FakeSession:
    backend_name = "fake"

    def __init__(self) -> None:
        self.read_triggers: list[bool] = []
        self.voltage_lists: list[list[float]] = []
        self.configurations: list[dict] = []
        self.abort_count = 0

    def read_primary_value(self, *, trigger: bool = False) -> float:
        self.read_triggers.append(bool(trigger))
        return 42.0

    def measure_voltage_list(
        self,
        voltages_v: list[float] | tuple[float, ...],
    ) -> list[dict[str, object]]:
        voltages = [float(value) for value in voltages_v]
        self.voltage_lists.append(voltages)
        return [
            {
                "source_voltage_v": voltage,
                "measured_voltage_v": voltage,
                "current_a": voltage / 12.0 if voltage else 0.0,
                "resistance_ohm": 12.0,
                "compliance_hit": False,
            }
            for voltage in voltages
        ]

    def configure_measurement(self, **kwargs) -> None:
        self.configurations.append(dict(kwargs))

    def abort_measurement(self) -> None:
        self.abort_count += 1


class _FakeVisaHandle:
    def __init__(self) -> None:
        self.timeout = 0
        self.read_termination = ""
        self.write_termination = ""
        self.writes: list[str] = []
        self.queries: list[str] = []
        self.cleared = False
        self.raw_data = b"raw-response"
        self.read_text = "text-response"

    def write(self, command: str) -> None:
        self.writes.append(command)

    def query(self, command: str) -> str:
        self.queries.append(command)
        return "fake-response"

    def read(self) -> str:
        return self.read_text

    def read_raw(self) -> bytes:
        return self.raw_data

    def clear(self) -> None:
        self.cleared = True


class _AskOnlyVisaHandle:
    def __init__(self) -> None:
        self.asks: list[str] = []

    def ask(self, command: str) -> str:
        self.asks.append(command)
        return "ask-response"


class _DeviceClearVisaHandle:
    def __init__(self) -> None:
        self.device_cleared = False

    def device_clear(self) -> None:
        self.device_cleared = True


class _TextOnlyVisaHandle:
    def __init__(self) -> None:
        self.read_text = "raw-as-text"

    def read(self) -> str:
        return self.read_text


class _FakeLCRSession(_LCRSession):
    backend_name = "fake-lcr"

    def __init__(self) -> None:
        self.configurations: list[dict] = []
        self.closed = False

    def read_primary_value(self, *, trigger: bool = False) -> float:
        return 42.0

    def configure_measurement(self, **kwargs) -> None:
        self.configurations.append(dict(kwargs))

    def close(self) -> None:
        self.closed = True


class _FakeKeithleySession:
    backend_name = "fake-keithley"

    def __init__(self) -> None:
        self.configurations: list[dict] = []
        self.read_triggers: list[bool] = []
        self.prepared_batches: list[tuple[int, int | None]] = []
        self.route_batches: list[tuple[int, bool, bool]] = []
        self.output_events: list[bool] = []
        self.output_active = False
        self.source_handle = _FakeVisaHandle()
        self.voltmeter_handle = _FakeVisaHandle()
        self.closed = False

    def output(self, enabled: bool = True):
        session = self
        requested = bool(enabled)

        class _OutputContext:
            def __enter__(self):
                session.output_active = requested
                session.output_events.append(requested)
                return session

            def __exit__(self, _exc_type, _exc, _tb) -> None:
                session.output_active = False
                session.output_events.append(False)

        return _OutputContext()

    def read_primary_value(self, *, trigger: bool = False) -> float:
        self.read_triggers.append(bool(trigger))
        return 42.0

    def read_route_measurements(
        self,
        count: int,
        *,
        trigger: bool = False,
        after_measurement=None,
    ) -> list[dict[str, object]]:
        self.route_batches.append((int(count), bool(trigger), after_measurement is not None))
        if after_measurement is not None:
            after_measurement()
        return [
            {"differential_resistance_ohm": 42.0}
            for _index in range(int(count))
        ]

    def prepare_route_measurements(
        self,
        count: int,
        *,
        source_list_count: int | None = None,
    ) -> None:
        self.prepared_batches.append((int(count), source_list_count))

    def configure_measurement(self, **kwargs) -> None:
        self.configurations.append(dict(kwargs))

    def identify(self) -> str:
        return "2400 KEITHLEY INSTRUMENTS INC.,MODEL 2400; 2182A fake"

    def visa_resource_roles(self) -> dict[str, dict[str, object]]:
        return {
            "meter.source": {"role": "meter.source"},
            "meter.voltmeter": {"role": "meter.voltmeter"},
        }

    def visa_handle_for_role(self, role: str):
        if role == "meter.source":
            return self.source_handle
        if role == "meter.voltmeter":
            return self.voltmeter_handle
        raise KeyError(role)

    def close(self) -> None:
        self.closed = True


class _StopDuringReadSession(_FakeSession):
    def __init__(self, stop_callback) -> None:
        super().__init__()
        self._stop_callback = stop_callback

    def read_primary_value(self, *, trigger: bool = False) -> float:
        self.read_triggers.append(bool(trigger))
        self._stop_callback()
        return 42.0


class _ExplodingReadSession(_FakeSession):
    def __init__(self) -> None:
        super().__init__()
        self.closed = False

    def read_primary_value(self, *, trigger: bool = False) -> float:
        self.read_triggers.append(bool(trigger))
        raise RuntimeError("visa boom")

    def close(self) -> None:
        self.closed = True


class _DeletedSignalSource:
    def emit(self, *_args) -> None:
        raise RuntimeError("Signal source has been deleted")


class _FailingConfigureKeithleySession(_FakeKeithleySession):
    def configure_measurement(self, **kwargs) -> None:
        self.configurations.append(dict(kwargs))
        raise RuntimeError("visa boom")


class _SourceSilentKeithleySession(_FakeKeithleySession):
    def identify(self) -> str:
        return "2182A KEITHLEY INSTRUMENTS INC.,MODEL 2182A"
