import sys
import threading
import time
import types
import unittest

try:
    from .lcr_test_support import (
        KeithleyRouteMeterSettings,
        LCRMeterController,
        LCRMeterError,
        ROUTE_METER_GWINSTEK,
        ROUTE_METER_KEITHLEY,
        ROUTE_METER_KEITHLEY_2400,
        RouteMeterConfiguration,
        _ConfiguringFakeInstrument,
        _DeletedSignalSource,
        _ExplodingReadSession,
        _FailingConfigureKeithleySession,
        _FakeInstrument,
        _FakeKeithleySession,
        _FakeLCRSession,
        _FakeSession,
        _LCRSession,
        _StopDuringReadSession,
        _connect_direct,
        lcr_module,
    )
except ImportError:
    from lcr_test_support import (
        KeithleyRouteMeterSettings,
        LCRMeterController,
        LCRMeterError,
        ROUTE_METER_GWINSTEK,
        ROUTE_METER_KEITHLEY,
        ROUTE_METER_KEITHLEY_2400,
        RouteMeterConfiguration,
        _ConfiguringFakeInstrument,
        _DeletedSignalSource,
        _ExplodingReadSession,
        _FailingConfigureKeithleySession,
        _FakeInstrument,
        _FakeKeithleySession,
        _FakeLCRSession,
        _FakeSession,
        _LCRSession,
        _StopDuringReadSession,
        _connect_direct,
        lcr_module,
    )


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

    def test_session_configures_impedance_measurement_command_sequence(self) -> None:
        session = _LCRSession.__new__(_LCRSession)
        session.CONFIG_COMMAND_DELAY_S = 0
        session.CONFIG_VERIFY_DELAY_S = 0
        session.POST_CONFIG_SETTLE_S = 0
        instrument = _ConfiguringFakeInstrument(
            {
                "FUNC?": "CpD",
                "TRIG:SOUR?": "BUS",
                "FUNC:RANG:AUTO?": "HOLD",
                "APER:RATE?": "MED",
                "APER:AVG?": "4",
                "TRIG:DLY?": "0.25",
                "FUNC:IMP:RANG?": "7",
                "FREQ?": "1000.0",
                "LEV:SRES?": "100",
                "FUNC:MON1?": "Z",
                "FUNC:MON2?": "TH",
                "LEV:ALC?": "ON",
                "LEV:CURR?": "0.0015",
                "BIAS?": "1.2",
            }
        )
        session._instrument = instrument

        session.configure_measurement(
            measurement_function="CpD",
            range_mode="HOLD",
            impedance_range=7,
            dcr_range=3,
            frequency_hz=1000.0,
            level_mode="CURRENT",
            voltage_level_v=0.2,
            current_level_a=0.0015,
            source_resistance_ohm=100,
            aperture_rate="MED",
            aperture_averages=4,
            trigger_source="BUS",
            trigger_delay_s=0.25,
            bias_enabled=True,
            bias_level_v=1.2,
            monitor1="Z",
            monitor2="TH",
            alc_enabled=True,
        )

        self.assertEqual(
            instrument.operations,
            [
                ("write", "FUNC CpD"),
                ("ask", "FUNC?"),
                ("write", "TRIG:SOUR BUS"),
                ("ask", "TRIG:SOUR?"),
                ("write", "FUNC:RANG:AUTO HOLD"),
                ("ask", "FUNC:RANG:AUTO?"),
                ("write", "APER MED"),
                ("ask", "APER:RATE?"),
                ("write", "APER 4"),
                ("ask", "APER:AVG?"),
                ("write", "TRIG:DLY 0.25"),
                ("ask", "TRIG:DLY?"),
                ("write", "FUNC:IMP:RANG 7"),
                ("ask", "FUNC:IMP:RANG?"),
                ("write", "FREQ 1000.0"),
                ("ask", "FREQ?"),
                ("write", "LEV:SRES 100"),
                ("ask", "LEV:SRES?"),
                ("write", "FUNC:MON1 Z"),
                ("ask", "FUNC:MON1?"),
                ("write", "FUNC:MON2 TH"),
                ("ask", "FUNC:MON2?"),
                ("write", "LEV:ALC ON"),
                ("ask", "LEV:ALC?"),
                ("write", "LEV:CURR 1.5m"),
                ("ask", "LEV:CURR?"),
                ("write", "BIAS 1.2"),
                ("ask", "BIAS?"),
            ],
        )

    def test_session_configures_dcr_measurement_command_sequence(self) -> None:
        session = _LCRSession.__new__(_LCRSession)
        session.CONFIG_COMMAND_DELAY_S = 0
        session.CONFIG_VERIFY_DELAY_S = 0
        session.POST_CONFIG_SETTLE_S = 0
        instrument = _ConfiguringFakeInstrument(
            {
                "FUNC?": "DCR",
                "TRIG:SOUR?": "BUS",
                "FUNC:RANG:AUTO?": "HOLD",
                "APER:RATE?": "FAST",
                "APER:AVG?": "1",
                "TRIG:DLY?": "0.0",
                "FUNC:DCR:RANG?": "3",
                "BIAS?": "OFF",
            }
        )
        session._instrument = instrument

        session.configure_measurement(
            measurement_function="DCR",
            range_mode="HOLD",
            impedance_range=7,
            dcr_range=3,
            frequency_hz=1000.0,
            level_mode="VOLTAGE",
            voltage_level_v=0.2,
            current_level_a=0.0015,
            source_resistance_ohm=100,
            aperture_rate="FAST",
            aperture_averages=1,
            trigger_source="BUS",
            trigger_delay_s=0.0,
            bias_enabled=True,
            bias_level_v=1.2,
            monitor1="Z",
            monitor2="TH",
            alc_enabled=True,
        )

        self.assertEqual(
            instrument.operations,
            [
                ("write", "FUNC DCR"),
                ("ask", "FUNC?"),
                ("write", "TRIG:SOUR BUS"),
                ("ask", "TRIG:SOUR?"),
                ("write", "FUNC:RANG:AUTO HOLD"),
                ("ask", "FUNC:RANG:AUTO?"),
                ("write", "APER FAST"),
                ("ask", "APER:RATE?"),
                ("write", "APER 1"),
                ("ask", "APER:AVG?"),
                ("write", "TRIG:DLY 0.0"),
                ("ask", "TRIG:DLY?"),
                ("write", "FUNC:DCR:RANG 3"),
                ("ask", "FUNC:DCR:RANG?"),
                ("write", "BIAS OFF"),
                ("ask", "BIAS?"),
            ],
        )

    def test_session_configuration_failure_raises_lcr_meter_error(self) -> None:
        session = _LCRSession.__new__(_LCRSession)
        session.CONFIG_COMMAND_DELAY_S = 0
        session.CONFIG_VERIFY_DELAY_S = 0
        session.POST_CONFIG_SETTLE_S = 0
        session.CONFIG_VERIFY_RETRIES = 1
        session._instrument = _ConfiguringFakeInstrument({"FUNC?": "DCR"})

        with self.assertRaisesRegex(
            LCRMeterError,
            "Unable to configure LCR measurement",
        ):
            session.configure_measurement(
                measurement_function="CpD",
                range_mode="AUTO",
                impedance_range=7,
                dcr_range=3,
                frequency_hz=1000.0,
                level_mode="VOLTAGE",
                voltage_level_v=0.2,
                current_level_a=0.0015,
                source_resistance_ohm=100,
                aperture_rate="FAST",
                aperture_averages=1,
                trigger_source="BUS",
                trigger_delay_s=0.0,
                bias_enabled=False,
                bias_level_v=0.0,
                monitor1="Z",
                monitor2="TH",
                alc_enabled=False,
            )

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

    def test_controller_live_polling_toggle_controls_worker_polling(self) -> None:
        controller = LCRMeterController()
        controller._session = _FakeSession()
        paused = []
        started = []
        controller._pause_live_polling = lambda: paused.append(True)
        controller._start_polling_thread = lambda: started.append(True)

        self.assertTrue(controller.live_polling_enabled())

        controller.set_live_polling_enabled(False)
        controller.set_live_polling_enabled(True)

        self.assertTrue(controller.live_polling_enabled())
        self.assertEqual(paused, [True])
        self.assertEqual(started, [True])

    def test_controller_enables_live_polling_for_keithley(self) -> None:
        controller = LCRMeterController()
        controller._meter_type = ROUTE_METER_KEITHLEY

        self.assertTrue(controller.live_polling_enabled())

        controller.set_live_polling_enabled(False)
        self.assertFalse(controller.live_polling_enabled())
        controller.set_live_polling_enabled(True)

        self.assertTrue(controller.live_polling_enabled())

    def test_controller_worker_polls_keithley_when_idle(self) -> None:
        controller = LCRMeterController()
        controller._meter_type = ROUTE_METER_KEITHLEY
        session = _FakeKeithleySession()
        controller._session = session
        controller._stop_polling.clear()
        summaries: list[tuple[float, bool, int]] = []
        _connect_direct(
            controller.reading_summary_updated,
            lambda value, is_short, count: summaries.append(
                (float(value), bool(is_short), int(count))
            ),
        )

        self.assertIsNotNone(controller._meter_worker_poll_timeout())
        polled = controller._run_meter_poll_once()

        self.assertTrue(polled)
        self.assertEqual(session.read_triggers, [True])
        self.assertEqual(summaries, [(42.0, False, 1)])

    def test_controller_live_polling_keeps_keithley_output_context(self) -> None:
        controller = LCRMeterController()
        controller._meter_type = ROUTE_METER_KEITHLEY
        session = _FakeKeithleySession()
        controller._session = session
        controller._stop_polling.clear()

        self.assertTrue(controller._run_meter_poll_once())
        self.assertTrue(controller._run_meter_poll_once())
        controller._stop_polling_session()

        self.assertEqual(session.read_triggers, [True, True])
        self.assertEqual(session.output_events, [True, False])

    def test_controller_output_context_proxies_keithley_session(self) -> None:
        controller = LCRMeterController()
        session = _FakeKeithleySession()
        controller._session = session

        try:
            with controller.output(True):
                self.assertTrue(session.output_active)
        finally:
            controller.shutdown()

        self.assertFalse(session.output_active)
        self.assertEqual(session.output_events, [True, False])

    def test_gpib_interface_reset_uses_unique_boards(self) -> None:
        calls: list[tuple[str, str]] = []

        class _FakeInterface:
            def __init__(self, name: str) -> None:
                self.name = name

            def send_ifc(self) -> None:
                calls.append(("send_ifc", self.name))

            def close(self) -> None:
                calls.append(("close", self.name))

        class _FakeResourceManager:
            def open_resource(self, name: str) -> _FakeInterface:
                calls.append(("open", name))
                return _FakeInterface(name)

        fake_pyvisa = types.SimpleNamespace(
            ResourceManager=lambda: _FakeResourceManager()
        )
        original_pyvisa = sys.modules.get("pyvisa")
        original_sleep = lcr_module.time.sleep
        sys.modules["pyvisa"] = fake_pyvisa
        lcr_module.time.sleep = lambda _seconds: None
        try:
            lcr_module._reset_gpib_interfaces_for_resources(
                "GPIB0::1::INSTR",
                "GPIB0::2::INSTR",
                "GPIB2::7::INSTR",
                "ASRL4::INSTR",
            )
        finally:
            lcr_module.time.sleep = original_sleep
            if original_pyvisa is None:
                sys.modules.pop("pyvisa", None)
            else:
                sys.modules["pyvisa"] = original_pyvisa

        self.assertEqual(
            calls,
            [
                ("open", "GPIB0::INTFC"),
                ("send_ifc", "GPIB0::INTFC"),
                ("close", "GPIB0::INTFC"),
                ("open", "GPIB2::INTFC"),
                ("send_ifc", "GPIB2::INTFC"),
                ("close", "GPIB2::INTFC"),
            ],
        )

    def test_controller_open_applies_pending_route_configuration(self) -> None:
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
            aperture_rate="FAST",
            aperture_averages=1,
            trigger_source="BUS",
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
        controller._start_polling_thread = lambda: None

        def fake_open(_source: str, _voltmeter: str, _timeout_ms: int):
            return session

        configuration = RouteMeterConfiguration(
            meter_type=ROUTE_METER_KEITHLEY,
            keithley=KeithleyRouteMeterSettings(nplc=5.0),
        )
        controller.apply_route_meter_runtime_configuration(configuration)
        original = lcr_module._open_keithley_session
        lcr_module._open_keithley_session = fake_open
        try:
            controller.open()
        finally:
            lcr_module._open_keithley_session = original

        self.assertTrue(controller.is_connected())
        self.assertEqual(session.configurations[-1]["keithley_nplc"], 5.0)

    def test_controller_batch_reads_are_serialized_by_worker(self) -> None:
        class _BlockingKeithleySession(_FakeKeithleySession):
            def __init__(self) -> None:
                super().__init__()
                self.first_entered = threading.Event()
                self.release_first = threading.Event()
                self.active_reads = 0
                self.max_active_reads = 0

            def read_route_measurements(
                self,
                count: int,
                *,
                trigger: bool = False,
                after_measurement=None,
            ) -> list[dict[str, object]]:
                self.active_reads += 1
                self.max_active_reads = max(self.max_active_reads, self.active_reads)
                self.route_batches.append(
                    (int(count), bool(trigger), after_measurement is not None)
                )
                if len(self.route_batches) == 1:
                    self.first_entered.set()
                    self.release_first.wait(timeout=1.0)
                self.active_reads -= 1
                return [
                    {"differential_resistance_ohm": 42.0}
                    for _index in range(int(count))
                ]

        controller = LCRMeterController()
        session = _BlockingKeithleySession()
        controller._session = session
        failures: list[BaseException] = []

        def run_batch() -> None:
            try:
                controller.read_route_measurement_batch_now(1)
            except BaseException as exc:
                failures.append(exc)

        first_thread = threading.Thread(target=run_batch)
        second_thread = threading.Thread(target=run_batch)
        first_thread.start()
        self.assertTrue(session.first_entered.wait(timeout=1.0))

        second_thread.start()
        time.sleep(0.05)
        self.assertEqual(session.route_batches, [(1, True, False)])
        self.assertEqual(session.max_active_reads, 1)

        session.release_first.set()
        first_thread.join(timeout=1.0)
        second_thread.join(timeout=1.0)

        self.assertFalse(first_thread.is_alive())
        self.assertFalse(second_thread.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(
            session.route_batches,
            [(1, True, False), (1, True, False)],
        )

    def test_controller_polling_drops_reading_after_stop(self) -> None:
        controller = LCRMeterController()
        session = _StopDuringReadSession(controller._stop_polling.set)
        controller._session = session
        updates: list[tuple[float, bool]] = []
        summaries: list[tuple[float, bool, int]] = []
        _connect_direct(
            controller.reading_updated,
            lambda value, is_short: updates.append((float(value), bool(is_short)))
        )
        _connect_direct(
            controller.reading_summary_updated,
            lambda value, is_short, count: summaries.append(
                (float(value), bool(is_short), int(count))
            )
        )

        controller._poll_readings()

        self.assertEqual(session.read_triggers, [True])
        self.assertEqual(updates, [])
        self.assertEqual(summaries, [])

    def test_deleted_qt_signal_source_stops_worker_polling(self) -> None:
        controller = LCRMeterController()
        controller.reading_updated = _DeletedSignalSource()
        controller._stop_polling.clear()
        controller._worker_runtime.shutdown_event.clear()

        controller._emit_reading_summary(42.0, 1)

        self.assertTrue(controller._shutdown_started)
        self.assertTrue(controller._stop_polling.is_set())
        self.assertTrue(controller._worker_runtime.shutdown_requested)

    def test_controller_polling_disconnects_on_unexpected_driver_error(self) -> None:
        controller = LCRMeterController()
        session = _ExplodingReadSession()
        controller._session = session
        status_messages: list[str] = []
        connection_events: list[tuple[bool, str]] = []
        _connect_direct(controller.status_message, status_messages.append)
        _connect_direct(
            controller.connection_changed,
            lambda connected, _backend, message: connection_events.append(
                (bool(connected), str(message))
            )
        )

        controller._poll_readings()

        self.assertEqual(session.read_triggers, [True])
        self.assertTrue(session.closed)
        self.assertFalse(controller.is_connected())
        self.assertIn("Instrument read failed: visa boom", status_messages)
        self.assertIn((False, "visa boom"), connection_events)

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

    def test_controller_connection_label_uses_keithley_2400_source_only(self) -> None:
        controller = LCRMeterController()
        controller.apply_configuration(
            meter_type=ROUTE_METER_KEITHLEY_2400,
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

        self.assertEqual(label, "Keithley 2400 GPIB2::7::INSTR")

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

    def test_controller_keithley_route_config_failure_disconnects_session(self) -> None:
        controller = LCRMeterController()
        controller._meter_type = ROUTE_METER_KEITHLEY
        session = _FailingConfigureKeithleySession()
        controller._session = session
        status_messages: list[str] = []
        connection_events: list[tuple[bool, str]] = []
        _connect_direct(controller.status_message, status_messages.append)
        _connect_direct(
            controller.connection_changed,
            lambda connected, _backend, message: connection_events.append(
                (bool(connected), str(message))
            )
        )

        with self.assertRaisesRegex(LCRMeterError, "visa boom"):
            controller.apply_route_meter_configuration(
                RouteMeterConfiguration(
                    meter_type=ROUTE_METER_KEITHLEY,
                    keithley=KeithleyRouteMeterSettings(),
                )
            )

        self.assertTrue(session.closed)
        self.assertFalse(controller.is_connected())
        self.assertIn("Instrument setup failed: visa boom", status_messages)
        self.assertIn((False, "visa boom"), connection_events)

    def test_controller_keithley_route_config_retries_after_reconnect(self) -> None:
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
        failing_session = _FailingConfigureKeithleySession()
        replacement_session = _FakeKeithleySession()
        controller._session = failing_session
        controller._connected_resource_name = controller._connection_key()
        controller._open_configured_session = lambda: replacement_session
        started: list[bool] = []
        status_messages: list[str] = []
        controller._start_polling_thread = lambda: started.append(True)
        _connect_direct(controller.status_message, status_messages.append)

        controller.apply_route_meter_configuration(
            RouteMeterConfiguration(
                meter_type=ROUTE_METER_KEITHLEY,
                keithley=KeithleyRouteMeterSettings(nplc=2.0),
            )
        )

        self.assertTrue(failing_session.closed)
        self.assertTrue(controller.is_connected())
        self.assertIs(controller._session, replacement_session)
        self.assertEqual(replacement_session.configurations[-1]["keithley_nplc"], 2.0)
        self.assertEqual(started, [])
        self.assertIn("Instrument setup failed; reconnecting Keithley.", status_messages)

    def test_controller_prepares_and_reads_keithley_batch_callbacks(self) -> None:
        controller = LCRMeterController()
        session = _FakeKeithleySession()
        controller._session = session
        stopped = []
        controller._stop_polling_session = lambda: stopped.append(True)
        callbacks: list[bool] = []
        started: list[int] = []
        summaries: list[tuple[float, bool, int]] = []
        _connect_direct(
            controller.reading_started,
            lambda count: started.append(int(count)),
        )
        _connect_direct(
            controller.reading_summary_updated,
            lambda value, is_short, count: summaries.append(
                (float(value), bool(is_short), int(count))
            )
        )

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
        self.assertEqual(started, [2])
        self.assertEqual(summaries, [(42.0, False, 2)])

if __name__ == "__main__":
    unittest.main()
