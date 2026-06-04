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

import probe_station_gui.lcr_meter as lcr_module
from probe_station_gui.lcr_meter import (
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


class _FakeLCRSession(_LCRSession):
    backend_name = "fake-lcr"

    def __init__(self) -> None:
        self.configurations: list[dict] = []

    def read_primary_value(self, *, trigger: bool = False) -> float:
        return 42.0

    def configure_measurement(self, **kwargs) -> None:
        self.configurations.append(dict(kwargs))


class _FakeKeithleySession:
    backend_name = "fake-keithley"

    def __init__(self) -> None:
        self.configurations: list[dict] = []
        self.read_triggers: list[bool] = []
        self.prepared_batches: list[tuple[int, int | None]] = []
        self.route_batches: list[tuple[int, bool, bool]] = []
        self.closed = False

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

    def close(self) -> None:
        self.closed = True


class LCRMeterTest(unittest.TestCase):
    def test_route_meter_configuration_describes_keithley_voltage_sweep(self) -> None:
        configuration = RouteMeterConfiguration(
            meter_type=ROUTE_METER_KEITHLEY,
            keithley=KeithleyRouteMeterSettings(
                measurement_voltage_v=0.03,
                nplc=1.0,
            ),
        )

        self.assertEqual(configuration.nplc_label(), "1")
        self.assertEqual(
            configuration.measurement_type_label(),
            "Keithley voltage sweep +/-0.03 V",
        )

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

    def test_controller_raw_voltage_sweep_uses_keithley_voltage_list(self) -> None:
        controller = LCRMeterController()
        session = _FakeSession()
        controller._session = session
        stopped = []
        started = []
        controller._stop_polling_session = lambda: stopped.append(True)
        controller._start_polling_thread = lambda: started.append(True)

        measurement = controller.read_voltage_sweep_now(
            [-0.03, 0.03],
            restart_polling=True,
        )

        self.assertEqual(measurement["source_voltages_v"], [-0.03, 0.03])
        self.assertEqual(
            measurement["points"],
            [
                {
                    "source_voltage_v": -0.03,
                    "measured_voltage_v": -0.03,
                    "current_a": -0.0025,
                    "resistance_ohm": 12.0,
                    "compliance_hit": False,
                },
                {
                    "source_voltage_v": 0.03,
                    "measured_voltage_v": 0.03,
                    "current_a": 0.0025,
                    "resistance_ohm": 12.0,
                    "compliance_hit": False,
                },
            ],
        )
        self.assertEqual(session.voltage_lists, [[-0.03, 0.03]])
        self.assertEqual(stopped, [True])
        self.assertEqual(started, [True])

    def test_controller_abort_delegates_to_active_session(self) -> None:
        controller = LCRMeterController()
        session = _FakeSession()
        controller._session = session

        controller.abort_current_measurement()

        self.assertEqual(session.abort_count, 1)

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
        session = _FakeLCRSession()

        controller._configure_session(session)

        self.assertEqual(session.configurations[-1]["trigger_source"], "BUS")

    def test_controller_connection_label_uses_keithley_resources(self) -> None:
        controller = LCRMeterController()
        controller.apply_configuration(
            meter_type=ROUTE_METER_KEITHLEY,
            resource_name="COM4",
            keithley_source_resource="GPIB2::7::INSTR",
            keithley_voltmeter_resource="GPIB2::8::INSTR",
            measurement_function="DCR",
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

        label = controller.connection_label()

        self.assertIn("Keithley 2400 GPIB2::7::INSTR", label)
        self.assertIn("2182A GPIB2::8::INSTR", label)

    def test_controller_applies_connected_keithley_route_settings(self) -> None:
        controller = LCRMeterController()
        controller.apply_configuration(
            meter_type=ROUTE_METER_KEITHLEY,
            resource_name="COM4",
            keithley_source_resource="GPIB2::1::INSTR",
            keithley_voltmeter_resource="GPIB2::2::INSTR",
            measurement_function="DCR",
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
        session = _FakeKeithleySession()
        controller._session = session

        controller.apply_route_meter_configuration(
            RouteMeterConfiguration(
                meter_type=ROUTE_METER_KEITHLEY,
                keithley=KeithleyRouteMeterSettings(
                    measurement_voltage_v=0.03,
                    range_mode="code_auto",
                    expected_resistance_ohm=100_000.0,
                    maximum_current_a=10e-6,
                    nplc=7.5,
                ),
            )
        )

        self.assertEqual(
            session.configurations[-1]["keithley_measurement_voltage_v"],
            0.03,
        )
        self.assertEqual(session.configurations[-1]["keithley_range_mode"], "code_auto")
        self.assertEqual(
            session.configurations[-1]["keithley_expected_resistance_ohm"],
            100_000.0,
        )
        self.assertEqual(
            session.configurations[-1]["keithley_maximum_current_a"],
            10e-6,
        )
        self.assertEqual(session.configurations[-1]["keithley_nplc"], 7.5)
        self.assertEqual(session.configurations[-1]["keithley_current_range_a"], 10e-6)
        self.assertEqual(session.configurations[-1]["keithley_use_buffer"], True)
        self.assertEqual(session.configurations[-1]["keithley_use_trigger_link"], True)

    def test_controller_prepares_and_reads_keithley_batch_callbacks(self) -> None:
        controller = LCRMeterController()
        session = _FakeKeithleySession()
        controller._session = session
        stopped = []
        controller._stop_polling_session = lambda: stopped.append(True)
        callbacks: list[bool] = []

        controller.prepare_route_measurement_batch_now(10, source_list_count=240)
        measurements = controller.read_route_measurement_batch_now(
            2,
            after_measurement=lambda: callbacks.append(True),
        )

        self.assertEqual(session.prepared_batches, [(10, 240)])
        self.assertEqual(session.route_batches, [(2, True, True)])
        self.assertEqual(callbacks, [True])
        self.assertEqual(len(measurements), 2)
        self.assertEqual(stopped, [True, True])

    def test_route_meter_opens_keithley_through_external_driver_factory(self) -> None:
        created: list[tuple[str, str, int]] = []
        session = _FakeKeithleySession()

        def fake_open(source: str, voltmeter: str, timeout_ms: int):
            created.append((source, voltmeter, timeout_ms))
            return session

        original = lcr_module._open_keithley_session
        lcr_module._open_keithley_session = fake_open
        try:
            meter = RouteMeter(
                RouteMeterConfiguration(
                    meter_type=ROUTE_METER_KEITHLEY,
                    keithley=KeithleyRouteMeterSettings(
                        source_resource="GPIB0::1::INSTR",
                        voltmeter_resource="GPIB0::2::INSTR",
                        nplc=1.0,
                        use_buffer=True,
                        use_trigger_link=True,
                    ),
                ),
                timeout_ms=1234,
            )

            value = meter.read_primary_value_now()
        finally:
            lcr_module._open_keithley_session = original

        self.assertEqual(value, 42.0)
        self.assertEqual(created, [("GPIB0::1::INSTR", "GPIB0::2::INSTR", 1234)])
        self.assertEqual(session.read_triggers, [True])
        self.assertEqual(session.configurations[-1]["keithley_nplc"], 1.0)
        self.assertEqual(session.configurations[-1]["keithley_use_buffer"], True)

    def test_route_meter_prepares_and_reads_keithley_batch_callbacks(self) -> None:
        session = _FakeKeithleySession()
        meter = RouteMeter(
            RouteMeterConfiguration(
                meter_type=ROUTE_METER_KEITHLEY,
                keithley=KeithleyRouteMeterSettings(),
            )
        )
        meter._session = session
        callbacks: list[bool] = []

        meter.prepare_route_measurement_batch_now(10, source_list_count=240)
        measurements = meter.read_route_measurement_batch_now(
            2,
            after_measurement=lambda: callbacks.append(True),
        )

        self.assertEqual(session.prepared_batches, [(10, 240)])
        self.assertEqual(session.route_batches, [(2, True, True)])
        self.assertEqual(callbacks, [True])
        self.assertEqual(len(measurements), 2)

    def test_controller_opens_keithley_with_default_timeout(self) -> None:
        created: list[tuple[str, str, int]] = []
        session = _FakeKeithleySession()

        def fake_open(source: str, voltmeter: str, timeout_ms: int):
            created.append((source, voltmeter, timeout_ms))
            return session

        controller = LCRMeterController()
        controller.apply_configuration(
            meter_type=ROUTE_METER_KEITHLEY,
            resource_name="COM4",
            keithley_source_resource="GPIB0::1::INSTR",
            keithley_voltmeter_resource="GPIB0::2::INSTR",
            measurement_function="DCR",
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
        original = lcr_module._open_keithley_session
        lcr_module._open_keithley_session = fake_open
        try:
            opened = controller._open_configured_session()
        finally:
            lcr_module._open_keithley_session = original

        self.assertIs(opened, session)
        self.assertEqual(
            created,
            [
                (
                    "GPIB0::1::INSTR",
                    "GPIB0::2::INSTR",
                    LCRMeterController.DEFAULT_TIMEOUT_MS,
                )
            ],
        )

    def test_controller_connect_now_opens_configured_keithley(self) -> None:
        created: list[tuple[str, str, int]] = []
        session = _FakeKeithleySession()

        def fake_open(source: str, voltmeter: str, timeout_ms: int):
            created.append((source, voltmeter, timeout_ms))
            return session

        controller = LCRMeterController()
        controller.apply_configuration(
            meter_type=ROUTE_METER_KEITHLEY,
            resource_name="COM4",
            keithley_source_resource="GPIB0::1::INSTR",
            keithley_voltmeter_resource="GPIB0::2::INSTR",
            measurement_function="DCR",
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
        original = lcr_module._open_keithley_session
        lcr_module._open_keithley_session = fake_open
        try:
            controller.connect_now()
        finally:
            lcr_module._open_keithley_session = original

        self.assertTrue(controller.is_connected())
        self.assertEqual(
            created,
            [
                (
                    "GPIB0::1::INSTR",
                    "GPIB0::2::INSTR",
                    LCRMeterController.DEFAULT_TIMEOUT_MS,
                )
            ],
        )

    def test_controller_rejects_route_meter_type_mismatch(self) -> None:
        controller = LCRMeterController()
        controller.apply_configuration(
            meter_type=ROUTE_METER_GWINSTEK,
            resource_name="COM4",
            measurement_function="DCR",
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
        controller._session = _FakeLCRSession()

        with self.assertRaises(LCRMeterError):
            controller.apply_route_meter_configuration(
                RouteMeterConfiguration(
                    meter_type=ROUTE_METER_KEITHLEY,
                    gwinstek=GWInstekRouteMeterSettings(),
                )
            )

    def test_route_meter_configuration_reports_keithley_nplc_label(self) -> None:
        config = RouteMeterConfiguration(
            meter_type=ROUTE_METER_KEITHLEY,
            keithley=KeithleyRouteMeterSettings(nplc=7.5),
        )

        self.assertEqual(config.nplc_label(), "7.5")


if __name__ == "__main__":
    unittest.main()
