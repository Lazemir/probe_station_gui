import types
import unittest
import sys


def _restore_real_imports() -> None:
    for name in list(sys.modules):
        if name == "PySide6" or name.startswith("PySide6."):
            del sys.modules[name]
    package = sys.modules.get("probe_station_gui")
    if package is not None and not getattr(package, "__file__", ""):
        for name in list(sys.modules):
            if name == "probe_station_gui" or name.startswith("probe_station_gui."):
                del sys.modules[name]


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
    pyside6 = types.ModuleType("PySide6")
    sys.modules["PySide6"] = pyside6
    sys.modules["PySide6.QtCore"] = qtcore


_restore_real_imports()
_install_pyside6_stubs_if_missing()

from probe_station_gui.lcr_meter import (
    KeithleyRouteMeterSettings,
    LCRMeterController,
    ROUTE_METER_KEITHLEY,
    RouteMeterConfiguration,
    _Keithley2400With2182ASession,
    _LCRSession,
)


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


class _FakeSession:
    backend_name = "fake"

    def __init__(self) -> None:
        self.read_triggers: list[bool] = []
        self.configurations: list[dict] = []

    def read_primary_value(self, *, trigger: bool = False) -> float:
        self.read_triggers.append(bool(trigger))
        return 42.0

    def configure_measurement(self, **kwargs) -> None:
        self.configurations.append(dict(kwargs))


class _FakeVisaHandle:
    def __init__(self, responses: dict[str, list[str]]) -> None:
        self.responses = {key: list(value) for key, value in responses.items()}
        self.writes: list[str] = []
        self.closed = False

    def write(self, command: str) -> None:
        self.writes.append(command)

    def query(self, query: str) -> str:
        values = self.responses.get(query, [])
        if values:
            return values.pop(0)
        return ""

    def close(self) -> None:
        self.closed = True


class LCRMeterTest(unittest.TestCase):
    def test_session_trigger_read_uses_completed_trigger_fetch(self) -> None:
        session = _LCRSession.__new__(_LCRSession)
        instrument = _FakeInstrument()
        session._instrument = instrument

        value = session.read_primary_value(trigger=True)

        self.assertEqual(value, 12.5)
        self.assertTrue(instrument.trigger_fetch_called)
        self.assertFalse(instrument.fetch_main_called)

    def test_controller_single_read_is_triggered_and_does_not_restart_polling(self) -> None:
        controller = LCRMeterController()
        session = _FakeSession()
        controller._session = session
        stopped = []
        started = []
        controller._stop_polling_session = lambda: stopped.append(True)
        controller._start_polling_thread = lambda: started.append(True)

        value = controller.read_primary_value_now()

        self.assertEqual(value, 42.0)
        self.assertEqual(session.read_triggers, [True])
        self.assertEqual(stopped, [True])
        self.assertEqual(started, [])

    def test_controller_forces_bus_trigger_source_when_configuring(self) -> None:
        controller = LCRMeterController()
        controller.apply_configuration(
            resource_name="COM4",
            measurement_function="R-X",
            range_mode="AUTO",
            auto_range_enabled=True,
            impedance_range=3,
            dcr_range=4,
            frequency_hz=50.0,
            level_mode="VOLTAGE",
            voltage_level_v=0.01,
            current_level_a=0.0001,
            source_resistance_ohm=100,
            aperture_rate="SLOW",
            aperture_averages=1,
            trigger_source="INT",
            trigger_delay_s=0.0,
            bias_enabled=False,
            bias_level_v=0.0,
            monitor1="OFF",
            monitor2="OFF",
            alc_enabled=False,
            short_threshold_ohm=10.0,
            poll_interval_ms=250,
        )
        session = _FakeSession()

        controller._configure_session(session)

        self.assertEqual(session.configurations[-1]["trigger_source"], "BUS")

    def test_keithley_session_reads_four_wire_resistance_from_two_biases(self) -> None:
        session = _Keithley2400With2182ASession.__new__(
            _Keithley2400With2182ASession
        )
        source = _FakeVisaHandle(
            {
                "FETC?": ["-0.03,-0.001", "0.03,0.001"],
                ":SENS:CURR:PROT:TRIP?": ["0"],
            }
        )
        voltmeter = _FakeVisaHandle({"FETC?": ["-0.029", "0.031"]})
        session._source = source
        session._voltmeter = voltmeter
        session._measurement_voltage_v = 0.03
        session._trigger_delay_s = 0.0
        session._compliance_current_a = 0.5

        value = session.read_primary_value(trigger=True)

        self.assertAlmostEqual(value, 30.0)
        self.assertIn(":SOUR:VOLT -0.03", source.writes)
        self.assertIn(":SOUR:VOLT 0.03", source.writes)
        self.assertEqual(source.writes[-1], ":SOUR:VOLT 0")

    def test_route_meter_configuration_reports_keithley_nplc_label(self) -> None:
        config = RouteMeterConfiguration(
            meter_type=ROUTE_METER_KEITHLEY,
            keithley=KeithleyRouteMeterSettings(nplc=7.5),
        )

        self.assertEqual(config.nplc_label(), "7.5")


if __name__ == "__main__":
    unittest.main()
