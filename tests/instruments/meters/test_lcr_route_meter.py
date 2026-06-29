import unittest

try:
    from .lcr_test_support import (
        GWInstekRouteMeterSettings,
        KeithleyRouteMeterSettings,
        LCRMeterController,
        LCRMeterError,
        ROUTE_METER_GWINSTEK,
        ROUTE_METER_KEITHLEY,
        RouteMeter,
        RouteMeterConfiguration,
        _AskOnlyVisaHandle,
        _DeviceClearVisaHandle,
        _FakeKeithleySession,
        _FakeLCRSession,
        _SourceSilentKeithleySession,
        _TextOnlyVisaHandle,
        lcr_module,
    )
except ImportError:
    from lcr_test_support import (
        GWInstekRouteMeterSettings,
        KeithleyRouteMeterSettings,
        LCRMeterController,
        LCRMeterError,
        ROUTE_METER_GWINSTEK,
        ROUTE_METER_KEITHLEY,
        RouteMeter,
        RouteMeterConfiguration,
        _AskOnlyVisaHandle,
        _DeviceClearVisaHandle,
        _FakeKeithleySession,
        _FakeLCRSession,
        _SourceSilentKeithleySession,
        _TextOnlyVisaHandle,
        lcr_module,
    )


class LCRRouteMeterTest(unittest.TestCase):
    def test_controller_exposes_station_owned_visa_roles(self) -> None:
        controller = LCRMeterController()
        session = _FakeKeithleySession()
        controller._meter_type = ROUTE_METER_KEITHLEY
        controller._session = session
        stopped = []
        controller._stop_polling_session = lambda: stopped.append(True)

        roles = controller.visa_resource_roles()
        response = controller.visa_operation(
            "meter.source",
            "query",
            command="*IDN?",
            timeout_ms=1234,
        )
        controller.visa_operation("meter.voltmeter", "clear")

        self.assertEqual(set(roles), {"meter.source", "meter.voltmeter"})
        self.assertEqual(roles["meter.source"]["meter_type"], ROUTE_METER_KEITHLEY)
        self.assertEqual(response, "fake-response")
        self.assertEqual(session.source_handle.queries, ["*IDN?"])
        self.assertEqual(session.source_handle.timeout, 0)
        self.assertTrue(session.voltmeter_handle.cleared)
        self.assertEqual(stopped, [True, True])

    def test_route_meter_visa_operation_supports_write_read_and_read_raw(self) -> None:
        session = _FakeKeithleySession()
        meter = RouteMeter(RouteMeterConfiguration(meter_type=ROUTE_METER_KEITHLEY))
        meter._session = session

        self.assertIsNone(
            meter.visa_operation(
                "meter.source",
                "write",
                command=":SOUR:VOLT 0.03",
                timeout_ms=1234,
                read_termination="\n",
                write_termination="\n",
            )
        )
        self.assertEqual(
            meter.visa_operation("meter.source", "read"),
            "text-response",
        )
        self.assertEqual(
            meter.visa_operation("meter.source", "read_raw"),
            b"raw-response",
        )

        self.assertEqual(session.source_handle.writes, [":SOUR:VOLT 0.03"])
        self.assertEqual(session.source_handle.timeout, 0)
        self.assertEqual(session.source_handle.read_termination, "")
        self.assertEqual(session.source_handle.write_termination, "")

    def test_route_meter_visa_query_uses_ask_when_query_is_missing(self) -> None:
        handle = _AskOnlyVisaHandle()

        class _AskOnlySession(_FakeKeithleySession):
            def visa_handle_for_role(self, role: str):
                if role == "meter.source":
                    return handle
                return super().visa_handle_for_role(role)

        meter = RouteMeter(RouteMeterConfiguration(meter_type=ROUTE_METER_KEITHLEY))
        meter._session = _AskOnlySession()

        response = meter.visa_operation("meter.source", "query", command="*IDN?")

        self.assertEqual(response, "ask-response")
        self.assertEqual(handle.asks, ["*IDN?"])

    def test_route_meter_visa_read_raw_falls_back_to_text_read(self) -> None:
        handle = _TextOnlyVisaHandle()

        class _TextOnlySession(_FakeKeithleySession):
            def visa_handle_for_role(self, role: str):
                if role == "meter.source":
                    return handle
                return super().visa_handle_for_role(role)

        meter = RouteMeter(RouteMeterConfiguration(meter_type=ROUTE_METER_KEITHLEY))
        meter._session = _TextOnlySession()

        self.assertEqual(
            meter.visa_operation("meter.source", "read_raw"),
            b"raw-as-text",
        )

    def test_route_meter_visa_clear_uses_device_clear_fallback(self) -> None:
        handle = _DeviceClearVisaHandle()

        class _DeviceClearSession(_FakeKeithleySession):
            def visa_handle_for_role(self, role: str):
                if role == "meter.source":
                    return handle
                return super().visa_handle_for_role(role)

        meter = RouteMeter(RouteMeterConfiguration(meter_type=ROUTE_METER_KEITHLEY))
        meter._session = _DeviceClearSession()

        self.assertIsNone(meter.visa_operation("meter.source", "clear"))

        self.assertTrue(handle.device_cleared)

    def test_route_meter_visa_operation_rejects_unsupported_operation(self) -> None:
        meter = RouteMeter(RouteMeterConfiguration(meter_type=ROUTE_METER_KEITHLEY))
        meter._session = _FakeKeithleySession()

        with self.assertRaisesRegex(LCRMeterError, "Unsupported VISA operation"):
            meter.visa_operation("meter.source", "flash")

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

    def test_route_meter_opens_applies_and_reads_gwinstek_session(self) -> None:
        created: list[tuple[str, int]] = []

        class _OpeningFakeLCRSession(_FakeLCRSession):
            def __init__(self, address: str, timeout_ms: int) -> None:
                super().__init__()
                created.append((address, timeout_ms))
                self.identify_count = 0

            def identify(self) -> str:
                self.identify_count += 1
                return "fake-lcr"

        original = lcr_module._LCRSession
        lcr_module._LCRSession = _OpeningFakeLCRSession
        try:
            meter = RouteMeter(
                RouteMeterConfiguration(
                    meter_type=ROUTE_METER_GWINSTEK,
                    gwinstek=GWInstekRouteMeterSettings(
                        resource_name="COM4",
                        aperture_averages=3,
                    ),
                ),
                timeout_ms=4321,
            )

            value = meter.read_primary_value_now()
            meter.apply_route_meter_configuration(
                RouteMeterConfiguration(
                    meter_type=ROUTE_METER_GWINSTEK,
                    gwinstek=GWInstekRouteMeterSettings(
                        resource_name="COM4",
                        aperture_averages=5,
                    ),
                )
            )
        finally:
            lcr_module._LCRSession = original

        session = meter._session
        self.assertIsInstance(session, _OpeningFakeLCRSession)
        self.assertEqual(created, [("COM4", 4321)])
        self.assertEqual(value, 42.0)
        self.assertEqual(session.identify_count, 1)
        self.assertEqual(session.configurations[0]["trigger_source"], "BUS")
        self.assertEqual(session.configurations[0]["aperture_averages"], 3)
        self.assertEqual(session.configurations[1]["aperture_averages"], 5)

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

    def test_controller_connect_now_starts_live_polling_for_keithley(self) -> None:
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
        started = []
        controller._start_polling_thread = lambda: started.append(True)
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
        self.assertEqual(started, [True])

    def test_controller_connect_now_rejects_keithley_without_source_idn(self) -> None:
        session = _SourceSilentKeithleySession()

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
        controller._open_configured_session = lambda: session

        with self.assertRaisesRegex(LCRMeterError, "Keithley 2400 did not respond"):
            controller.connect_now()

        self.assertTrue(session.closed)
        self.assertFalse(controller.is_connected())

    def test_controller_connect_now_keeps_live_polling_for_gwinstek(self) -> None:
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
        session = _FakeLCRSession()
        session.identify = lambda: "fake-lcr"
        started = []
        controller._open_configured_session = lambda: session
        controller._start_polling_thread = lambda: started.append(True)

        controller.connect_now()

        self.assertTrue(controller.is_connected())
        self.assertIs(controller._session, session)
        self.assertEqual(started, [True])

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
