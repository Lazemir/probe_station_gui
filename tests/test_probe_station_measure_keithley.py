import threading
import unittest

from probe_station_measure import (
    AbstractOhmmeter,
    OHMMETER_RANGE_CODE_AUTO,
    Keithley2400With2182A,
    Keithley2400With2182AConfig,
    evaluate_contact_quality,
    summarize_contact_quality,
)


class _FakeHandle:
    def __init__(self, responses: dict[str, list[str]] | None = None) -> None:
        self.responses = {key: list(value) for key, value in (responses or {}).items()}
        self.writes: list[str] = []
        self.queries: list[str] = []
        self.closed = False
        self._timeout = 0
        self.timeout_history: list[int] = []

    @property
    def timeout(self) -> int:
        return self._timeout

    @timeout.setter
    def timeout(self, value: int) -> None:
        self._timeout = int(value)
        self.timeout_history.append(self._timeout)

    def write(self, command: str) -> None:
        self.writes.append(command)

    def query(self, query: str) -> str:
        self.queries.append(query)
        values = self.responses.get(query, [])
        if values:
            return values.pop(0)
        if query == "SYST:ERR?":
            return '0,"No error"'
        return ""

    def close(self) -> None:
        self.closed = True

    def clear(self) -> None:
        self.writes.append("<CLEAR>")


class _FakeResourceManager:
    def __init__(self, source: _FakeHandle, voltmeter: _FakeHandle) -> None:
        self.source = source
        self.voltmeter = voltmeter
        self.opened: list[str] = []
        self.closed = False

    def open_resource(self, address: str) -> _FakeHandle:
        self.opened.append(address)
        if len(self.opened) == 1:
            return self.source
        return self.voltmeter

    def close(self) -> None:
        self.closed = True


class _BarrierTraceHandle(_FakeHandle):
    def __init__(
        self,
        responses: dict[str, list[str]],
        barrier: threading.Barrier,
    ) -> None:
        super().__init__(responses)
        self.barrier = barrier

    def query(self, query: str) -> str:
        if query == "TRAC:DATA?":
            self.barrier.wait(timeout=2.0)
        return super().query(query)


class KeithleyDriverTest(unittest.TestCase):
    def test_composite_driver_implements_abstract_ohmmeter(self) -> None:
        meter = Keithley2400With2182A(
            "GPIB0::1::INSTR",
            "GPIB0::2::INSTR",
            resource_manager=_FakeResourceManager(_FakeHandle(), _FakeHandle()),
        )

        self.assertIsInstance(meter, AbstractOhmmeter)

    def test_normalized_config_uses_documented_buffer_limits(self) -> None:
        config = Keithley2400With2182AConfig(
            max_buffer_points_per_chunk=500
        ).normalized()

        self.assertEqual(config.max_buffer_points_per_chunk, 500)

        huge_config = Keithley2400With2182AConfig(
            max_buffer_points_per_chunk=9999
        ).normalized()

        self.assertEqual(huge_config.max_buffer_points_per_chunk, 1024)

    def test_code_auto_selects_fixed_ranges_from_external_parameters(self) -> None:
        config = Keithley2400With2182AConfig(
            measurement_voltage_v=0.03,
            range_mode=OHMMETER_RANGE_CODE_AUTO,
            expected_resistance_ohm=100_000.0,
        ).normalized()

        self.assertEqual(config.range_mode, OHMMETER_RANGE_CODE_AUTO)
        self.assertEqual(config.source_voltage_range_v, 0.21)
        self.assertEqual(config.voltmeter_range_v, 0.21)
        self.assertEqual(config.current_range_a, 1e-6)
        self.assertEqual(config.compliance_current_a, 1e-6)

    def test_code_auto_writes_fixed_ranges_not_device_autorange(self) -> None:
        source = _FakeHandle()
        voltmeter = _FakeHandle()
        meter = Keithley2400With2182A(
            "GPIB0::1::INSTR",
            "GPIB0::2::INSTR",
            resource_manager=_FakeResourceManager(source, voltmeter),
        )

        meter.configure_measurement(
            keithley_range_mode=OHMMETER_RANGE_CODE_AUTO,
            keithley_measurement_voltage_v=0.03,
            keithley_expected_resistance_ohm=100_000.0,
        )

        self.assertIn(":SOUR:VOLT:RANG 0.21", source.writes)
        self.assertIn(":SENS:CURR:RANG 1e-06", source.writes)
        self.assertIn(":SENS:CURR:PROT 1e-06", source.writes)
        self.assertIn("SENS:VOLT:RANG 0.21", voltmeter.writes)
        self.assertFalse(
            any("RANG:AUTO" in command.upper() for command in source.writes)
        )
        self.assertFalse(
            any("RANG:AUTO" in command.upper() for command in voltmeter.writes)
        )

    def test_neutral_configure_aliases_apply_common_ranges_and_nplc(self) -> None:
        source = _FakeHandle()
        voltmeter = _FakeHandle()
        meter = Keithley2400With2182A(
            "GPIB0::1::INSTR",
            "GPIB0::2::INSTR",
            resource_manager=_FakeResourceManager(source, voltmeter),
        )

        meter.configure_measurement(
            measurement_voltage_v=0.03,
            voltage_range_v=1.0,
            current_range_a=0.5,
            compliance_current_a=0.25,
            nplc=2.0,
        )

        self.assertIn(":SOUR:VOLT:RANG 1", source.writes)
        self.assertIn(":SENS:CURR:RANG 0.5", source.writes)
        self.assertIn(":SENS:CURR:PROT 0.25", source.writes)
        self.assertIn(":SENS:CURR:NPLC 2", source.writes)
        self.assertIn("SENS:VOLT:RANG 1", voltmeter.writes)
        self.assertIn("SENS:VOLT:NPLC 2", voltmeter.writes)

    def test_source_only_keithley_uses_source_meter_voltage_readback(self) -> None:
        source = _FakeHandle(
            {
                "FETC?": ["-0.029,-0.001", "0.031,0.001"],
            }
        )
        manager = _FakeResourceManager(source, _FakeHandle())
        meter = Keithley2400With2182A(
            "GPIB0::1::INSTR",
            None,
            resource_manager=manager,
        )

        readings = meter.measure_voltage_list(
            [-0.03, 0.03],
            Keithley2400With2182AConfig(
                use_buffer=True,
                use_trigger_link=True,
                compliance_current_a=0.5,
                current_range_a=0.5,
            ),
        )

        self.assertEqual(manager.opened, ["GPIB0::1::INSTR"])
        self.assertEqual(len(readings), 2)
        self.assertAlmostEqual(readings[0].measured_voltage_v, -0.029)
        self.assertAlmostEqual(readings[1].measured_voltage_v, 0.031)
        self.assertAlmostEqual(readings[0].resistance_ohm, 29.0)
        self.assertNotIn(":TRIG:SOUR TLIN", source.writes)

    def test_visa_roles_reflect_optional_voltmeter(self) -> None:
        source_only = Keithley2400With2182A(
            "GPIB0::1::INSTR",
            None,
            resource_manager=_FakeResourceManager(_FakeHandle(), _FakeHandle()),
        )
        pair = Keithley2400With2182A(
            "GPIB0::1::INSTR",
            "GPIB0::2::INSTR",
            resource_manager=_FakeResourceManager(_FakeHandle(), _FakeHandle()),
        )

        self.assertEqual(set(source_only.visa_resource_roles()), {"meter.source"})
        self.assertEqual(
            set(pair.visa_resource_roles()),
            {"meter.source", "meter.voltmeter"},
        )

    def test_software_loop_reads_differential_resistance(self) -> None:
        source = _FakeHandle(
            {
                "FETC?": ["-0.03,-0.001", "0.03,0.001"],
            }
        )
        voltmeter = _FakeHandle({"READ?": ["-0.029", "0.031"]})
        meter = Keithley2400With2182A(
            "GPIB0::1::INSTR",
            "GPIB0::2::INSTR",
            resource_manager=_FakeResourceManager(source, voltmeter),
        )

        reading = meter.measure_pair(
            Keithley2400With2182AConfig(
                use_buffer=False,
                use_trigger_link=False,
                compliance_current_a=0.5,
                current_range_a=0.5,
            )
        )

        self.assertAlmostEqual(reading.differential_resistance_ohm, 30.0)
        self.assertIn(":SENS:CURR:RANG 0.5", source.writes)
        self.assertIn(":SOUR:VOLT -0.03", source.writes)
        self.assertIn(":SOUR:VOLT 0.03", source.writes)
        self.assertNotIn(":INIT:CONT OFF", source.writes)
        self.assertIn(":TRIG:SOUR IMM", source.writes)
        self.assertIn(":OUTP OFF", source.writes)
        self.assertEqual(reading.negative.resistance_ohm, 29.0)
        self.assertEqual(reading.positive.resistance_ohm, 31.0)

    def test_buffered_trigger_link_uses_vmc_sequence_and_trace_data(self) -> None:
        source = _FakeHandle(
            {
                "*OPC?": ["1"],
                "TRAC:DATA?": [
                    "-0.03,-0.001,0.03,0.001,-0.03,-0.0011,0.03,0.0011"
                ],
            }
        )
        voltmeter = _FakeHandle({"TRAC:DATA?": ["-0.029,0.031,-0.030,0.032"]})
        meter = Keithley2400With2182A(
            "GPIB0::1::INSTR",
            "GPIB0::2::INSTR",
            resource_manager=_FakeResourceManager(source, voltmeter),
        )

        readings = meter.measure_pairs(
            2,
            Keithley2400With2182AConfig(
                use_buffer=True,
                use_trigger_link=True,
                compliance_current_a=0.5,
                current_range_a=0.5,
            ),
        )

        self.assertEqual(len(readings), 2)
        self.assertAlmostEqual(readings[0].differential_resistance_ohm, 30.0)
        self.assertAlmostEqual(
            readings[1].differential_resistance_ohm,
            0.062 / 0.0022,
        )
        self.assertIn(":TRIG:CLE", source.writes)
        self.assertIn(":ARM:SOUR IMM", source.writes)
        self.assertIn(":ARM:COUN 1", source.writes)
        self.assertIn(":TRIG:SOUR IMM", source.writes)
        self.assertIn(":TRIG:OUTP NONE", source.writes)
        self.assertIn(":TRIG:SOUR TLIN", source.writes)
        self.assertIn(":TRIG:ILIN 1", source.writes)
        self.assertIn(":TRIG:OLIN 2", source.writes)
        self.assertIn("TRIG:SOUR EXT", voltmeter.writes)
        self.assertIn("INIT:CONT OFF", voltmeter.writes)
        self.assertIn("TRIG:SOUR IMM", voltmeter.writes)
        self.assertIn("TRAC:POIN 4", voltmeter.writes)
        self.assertEqual(source.timeout, 10000)
        self.assertEqual(voltmeter.timeout, 10000)
        self.assertGreater(max(source.timeout_history), 10000)
        self.assertEqual(voltmeter.timeout_history, [10000])

    def test_prepared_route_batch_reuses_covering_source_list(self) -> None:
        source = _FakeHandle(
            {
                "*OPC?": ["1", "1"],
                "TRAC:DATA?": [
                    "-0.03,-0.001,0.03,0.001",
                    "-0.03,-0.001,0.03,0.001,-0.03,-0.001,0.03,0.001,-0.03,-0.001,0.03,0.001",
                ],
            }
        )
        voltmeter = _FakeHandle(
            {
                "TRAC:DATA?": [
                    "-0.029,0.031",
                    "-0.029,0.031,-0.029,0.031,-0.029,0.031",
                ]
            }
        )
        meter = Keithley2400With2182A(
            "GPIB0::1::INSTR",
            "GPIB0::2::INSTR",
            resource_manager=_FakeResourceManager(source, voltmeter),
        )

        meter.configure(
            Keithley2400With2182AConfig(
                use_buffer=True,
                use_trigger_link=True,
                compliance_current_a=0.5,
                current_range_a=0.5,
            )
        )
        meter.prepare_route_measurements(1, source_list_count=3)
        first = meter.read_route_measurements(1)
        meter.prepare_route_measurements(3, source_list_count=3)
        second = meter.read_route_measurements(3)

        source_list_commands = [
            command
            for command in source.writes
            if command.startswith(":SOUR:LIST:VOLT")
        ]
        self.assertEqual(len(first), 1)
        self.assertEqual(len(second), 3)
        self.assertEqual(len(source_list_commands), 1)
        self.assertIn("TRAC:POIN 2", source.writes)
        self.assertIn("TRAC:POIN 6", source.writes)

    def test_buffered_trace_download_queries_instruments_concurrently(self) -> None:
        barrier = threading.Barrier(2)
        source = _BarrierTraceHandle(
            {
                "*OPC?": ["1"],
                "TRAC:DATA?": ["-0.03,-0.001,0.03,0.001"],
            },
            barrier,
        )
        voltmeter = _BarrierTraceHandle(
            {"TRAC:DATA?": ["-0.029,0.031"]},
            barrier,
        )
        meter = Keithley2400With2182A(
            "GPIB0::1::INSTR",
            "GPIB0::2::INSTR",
            resource_manager=_FakeResourceManager(source, voltmeter),
        )

        readings = meter.measure_pairs(
            1,
            Keithley2400With2182AConfig(
                use_buffer=True,
                use_trigger_link=True,
                compliance_current_a=0.5,
                current_range_a=0.5,
            ),
        )

        self.assertEqual(len(readings), 1)
        self.assertFalse(barrier.broken)

    def test_measure_voltage_list_returns_one_reading_per_source_point(self) -> None:
        source = _FakeHandle(
            {
                "*OPC?": ["1"],
                "TRAC:DATA?": ["-0.02,-0.002,0.01,0.001,0.03,0.003"],
            }
        )
        voltmeter = _FakeHandle({"TRAC:DATA?": ["-0.04,0.02,0.06"]})
        meter = Keithley2400With2182A(
            "GPIB0::1::INSTR",
            "GPIB0::2::INSTR",
            resource_manager=_FakeResourceManager(source, voltmeter),
        )

        readings = meter.measure_voltage_list(
            [-0.02, 0.01, 0.03],
            Keithley2400With2182AConfig(
                use_buffer=True,
                use_trigger_link=True,
                compliance_current_a=0.5,
                current_range_a=0.5,
            ),
        )

        self.assertEqual(len(readings), 3)
        self.assertEqual([reading.source_voltage_v for reading in readings], [-0.02, 0.01, 0.03])
        self.assertAlmostEqual(readings[0].resistance_ohm, 20.0)
        self.assertAlmostEqual(readings[2].resistance_ohm, 20.0)
        self.assertIn(":SOUR:LIST:VOLT -0.02,0.01,0.03", source.writes)
        self.assertIn("TRAC:POIN 3", source.writes)
        self.assertIn("TRAC:POIN 3", voltmeter.writes)

    def test_measure_repeated_voltage_list_splits_arbitrary_pattern(self) -> None:
        source = _FakeHandle(
            {
                "*OPC?": ["1"],
                "TRAC:DATA?": ["-0.02,-0.002,0,0.001,0.02,0.002,-0.02,-0.002,0,0.001,0.02,0.002"],
            }
        )
        voltmeter = _FakeHandle({"TRAC:DATA?": ["-0.04,0,0.04,-0.041,0.001,0.041"]})
        meter = Keithley2400With2182A(
            "GPIB0::1::INSTR",
            "GPIB0::2::INSTR",
            resource_manager=_FakeResourceManager(source, voltmeter),
        )

        sweeps = meter.measure_repeated_voltage_list(
            [-0.02, 0.0, 0.02],
            2,
            Keithley2400With2182AConfig(
                use_buffer=True,
                use_trigger_link=True,
                compliance_current_a=0.5,
                current_range_a=0.5,
            ),
        )

        self.assertEqual(len(sweeps), 2)
        self.assertEqual(len(sweeps[0]), 3)
        self.assertEqual([reading.source_voltage_v for reading in sweeps[1]], [-0.02, 0.0, 0.02])
        self.assertIn(":SOUR:LIST:VOLT -0.02,0,0.02,-0.02,0,0.02", source.writes)

    def test_trace_status_queries_documented_buffer_commands(self) -> None:
        source = _FakeHandle(
            {
                "TRAC:POIN?": ["100"],
                "TRAC:POIN:ACT?": ["42"],
                "TRAC:FREE?": ["57600,2400"],
                "TRAC:FEED?": ["SENS1"],
                "TRAC:FEED:CONT?": ["NEV"],
            }
        )
        voltmeter = _FakeHandle(
            {
                "TRAC:POIN?": ["100"],
                "TRAC:FREE?": ["16632,1800"],
                "TRAC:FEED?": ["SENS1"],
                "TRAC:FEED:CONT?": ["NEV"],
            }
        )
        meter = Keithley2400With2182A(
            "GPIB0::1::INSTR",
            "GPIB0::2::INSTR",
            resource_manager=_FakeResourceManager(source, voltmeter),
        )

        status = meter.trace_status()

        self.assertEqual(status["source"].points, 100)
        self.assertEqual(status["source"].actual_points, 42)
        self.assertEqual(status["source"].free_bytes, 57600)
        self.assertEqual(status["source"].reserved_bytes, 2400)
        self.assertEqual(status["voltmeter"].actual_points, None)
        self.assertEqual(status["voltmeter"].free_bytes, 16632)
        self.assertNotIn("TRAC:POIN:ACT?", voltmeter.queries)

    def test_large_buffered_batch_is_split_into_chunks(self) -> None:
        source = _FakeHandle(
            {
                "*OPC?": ["1", "1", "1"],
                "TRAC:DATA?": [
                    "-0.03,-0.001,0.03,0.001",
                    "-0.03,-0.001,0.03,0.001",
                    "-0.03,-0.001,0.03,0.001",
                ],
            }
        )
        voltmeter = _FakeHandle(
            {"TRAC:DATA?": ["-0.029,0.031", "-0.029,0.031", "-0.029,0.031"]}
        )
        meter = Keithley2400With2182A(
            "GPIB0::1::INSTR",
            "GPIB0::2::INSTR",
            resource_manager=_FakeResourceManager(source, voltmeter),
        )

        readings = meter.measure_pairs(
            3,
            Keithley2400With2182AConfig(
                use_buffer=True,
                use_trigger_link=True,
                max_buffer_points_per_chunk=2,
            ),
        )

        self.assertEqual(len(readings), 3)
        self.assertEqual(source.writes.count("TRAC:POIN 2"), 3)

    def test_buffered_trigger_link_appends_source_list_after_100_points(self) -> None:
        source_trace = ",".join(
            f"{-0.03 if index % 2 == 0 else 0.03},"
            f"{-0.001 if index % 2 == 0 else 0.001}"
            for index in range(102)
        )
        voltmeter_trace = ",".join(
            "-0.029" if index % 2 == 0 else "0.031" for index in range(102)
        )
        source = _FakeHandle(
            {
                "*OPC?": ["1"],
                "TRAC:DATA?": [source_trace],
            }
        )
        voltmeter = _FakeHandle({"TRAC:DATA?": [voltmeter_trace]})
        meter = Keithley2400With2182A(
            "GPIB0::1::INSTR",
            "GPIB0::2::INSTR",
            resource_manager=_FakeResourceManager(source, voltmeter),
        )

        readings = meter.measure_pairs(
            51,
            Keithley2400With2182AConfig(use_buffer=True, use_trigger_link=True),
        )

        source_list_commands = [
            command
            for command in source.writes
            if command.startswith(":SOUR:LIST:VOLT")
        ]
        self.assertEqual(len(readings), 51)
        self.assertEqual(len(source_list_commands), 2)
        self.assertTrue(source_list_commands[0].startswith(":SOUR:LIST:VOLT "))
        self.assertTrue(
            source_list_commands[1].startswith(":SOUR:LIST:VOLT:APPend ")
        )
        self.assertEqual(source.writes.count("TRAC:POIN 102"), 1)
        self.assertNotIn(":SOUR:LIST:VOLT:POIN?", source.queries)

    def test_contact_quality_check_classifies_stable_contact(self) -> None:
        source = _FakeHandle(
            {
                "*OPC?": ["1"],
                "TRAC:DATA?": [
                    ",".join(
                        "-0.03,-0.001,0.03,0.001"
                        for _index in range(25)
                    )
                ],
            }
        )
        voltmeter = _FakeHandle(
            {
                "TRAC:DATA?": [
                    ",".join("-16.8,16.8" for _index in range(25))
                ],
            }
        )
        meter = Keithley2400With2182A(
            "GPIB0::1::INSTR",
            "GPIB0::2::INSTR",
            resource_manager=_FakeResourceManager(source, voltmeter),
        )

        check = meter.check_contact_quality(
            25,
            Keithley2400With2182AConfig(
                measurement_voltage_v=0.03,
                use_buffer=True,
                use_trigger_link=True,
                compliance_current_a=0.5,
                current_range_a=0.5,
            ),
        )

        self.assertTrue(check.good)
        self.assertEqual(check.status, "good")
        self.assertEqual(check.reasons, ())

    def test_contact_quality_check_reports_noisy_open_contact(self) -> None:
        readings = []
        for index, value in enumerate((520000.0, 610000.0, 480000.0, 570000.0)):
            current = 0.001 if index % 2 else -0.001
            readings.append(
                type(
                    "Reading",
                    (),
                    {
                        "differential_resistance_ohm": value,
                        "compliance_hit": False,
                        "negative": type("Polarity", (), {"current_a": -abs(current)})(),
                        "positive": type("Polarity", (), {"current_a": abs(current)})(),
                    },
                )()
            )

        check = evaluate_contact_quality(summarize_contact_quality(readings))

        self.assertFalse(check.good)
        self.assertEqual(check.status, "bad_contact")
        self.assertIn("median_out_of_range", check.reasons)
        self.assertIn("mad_sigma_too_high", check.reasons)


if __name__ == "__main__":
    unittest.main()
