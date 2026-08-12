from __future__ import annotations

import sys
import types
import unittest
from unittest.mock import patch

from probe_station_measure import (
    Keithley2400With2182A,
    Keithley2400With2182AConfig,
)
from tests.instruments.test_probe_station_measure_keithley import _FakeHandle


class _FailingSecondOpenResourceManager:
    def __init__(self, source: _FakeHandle) -> None:
        self.source = source
        self.opened: list[str] = []
        self.closed = False

    def open_resource(self, address: str) -> _FakeHandle:
        self.opened.append(address)
        if len(self.opened) == 1:
            return self.source
        raise RuntimeError("voltmeter open failed")

    def close(self) -> None:
        self.closed = True


class _TimeoutFailingHandle(_FakeHandle):
    @property
    def timeout(self) -> int:
        return self._timeout

    @timeout.setter
    def timeout(self, value: int) -> None:
        raise RuntimeError("timeout setup failed")


class _TimeoutResourceManager:
    def __init__(self, *handles: _FakeHandle) -> None:
        self.handles = list(handles)
        self.opened: list[str] = []
        self.closed = False

    def open_resource(self, address: str) -> _FakeHandle:
        self.opened.append(address)
        return self.handles[len(self.opened) - 1]

    def close(self) -> None:
        self.closed = True


class _EventHandle(_FakeHandle):
    def __init__(
        self,
        name: str,
        events: list[tuple[str, str, str]],
        responses: dict[str, list[str]] | None = None,
        *,
        fail_query: str | None = None,
        fail_write_prefix: str | None = None,
        scpi_errors_after_write_failure: list[str] | None = None,
    ) -> None:
        super().__init__(responses)
        self.name = name
        self.events = events
        self.fail_query = fail_query
        self.fail_write_prefix = fail_write_prefix
        self.scpi_errors_after_write_failure = list(
            scpi_errors_after_write_failure or []
        )
        self.write_failed = False

    def write(self, command: str) -> None:
        self.events.append((self.name, "write", command))
        if self.fail_write_prefix and command.startswith(self.fail_write_prefix):
            self.write_failed = True
            raise RuntimeError("source-list transport failed")
        super().write(command)

    def query(self, query: str) -> str:
        self.events.append((self.name, "query", query))
        if query == self.fail_query:
            raise RuntimeError(f"{query} failed")
        if (
            query == "SYST:ERR?"
            and self.write_failed
            and self.scpi_errors_after_write_failure
        ):
            return self.scpi_errors_after_write_failure.pop(0)
        return super().query(query)

    def clear(self) -> None:
        self.events.append((self.name, "clear", ""))
        super().clear()

    def close(self) -> None:
        self.events.append((self.name, "close", ""))
        super().close()


class _RecordingResourceManager:
    def __init__(
        self,
        source: _EventHandle,
        voltmeter: _EventHandle | None,
        events: list[tuple[str, str, str]],
    ) -> None:
        self.source = source
        self.voltmeter = voltmeter
        self.events = events
        self.opened: list[str] = []

    def open_resource(self, address: str) -> _EventHandle:
        self.opened.append(address)
        self.events.append(("manager", "open", address))
        if len(self.opened) == 1:
            return self.source
        if self.voltmeter is None:
            raise AssertionError("Unexpected second resource open")
        return self.voltmeter


def _meter_with_events(
    source: _EventHandle,
    voltmeter: _EventHandle | None,
    events: list[tuple[str, str, str]],
) -> Keithley2400With2182A:
    return Keithley2400With2182A(
        "GPIB0::1::INSTR",
        "GPIB0::2::INSTR" if voltmeter is not None else None,
        resource_manager=_RecordingResourceManager(source, voltmeter, events),
    )


class KeithleyDriverFailureTest(unittest.TestCase):
    def test_injected_manager_closes_source_when_timeout_setup_fails(self) -> None:
        source = _TimeoutFailingHandle()
        resource_manager = _TimeoutResourceManager(source)

        with self.assertRaisesRegex(RuntimeError, "timeout setup failed"):
            Keithley2400With2182A(
                "GPIB0::1::INSTR",
                resource_manager=resource_manager,
            )

        self.assertTrue(source.closed)
        self.assertFalse(resource_manager.closed)

    def test_injected_manager_closes_both_handles_when_second_timeout_fails(
        self,
    ) -> None:
        source = _FakeHandle()
        voltmeter = _TimeoutFailingHandle()
        resource_manager = _TimeoutResourceManager(source, voltmeter)

        with self.assertRaisesRegex(RuntimeError, "timeout setup failed"):
            Keithley2400With2182A(
                "GPIB0::1::INSTR",
                "GPIB0::2::INSTR",
                resource_manager=resource_manager,
            )

        self.assertTrue(source.closed)
        self.assertTrue(voltmeter.closed)
        self.assertFalse(resource_manager.closed)

    def test_owned_manager_closes_source_and_manager_when_timeout_fails(self) -> None:
        source = _TimeoutFailingHandle()
        resource_manager = _TimeoutResourceManager(source)
        pyvisa = types.ModuleType("pyvisa")
        pyvisa.ResourceManager = lambda: resource_manager

        with patch.dict(sys.modules, {"pyvisa": pyvisa}):
            with self.assertRaisesRegex(RuntimeError, "timeout setup failed"):
                Keithley2400With2182A("GPIB0::1::INSTR")

        self.assertTrue(source.closed)
        self.assertTrue(resource_manager.closed)

    def test_owned_manager_closes_both_handles_when_second_timeout_fails(self) -> None:
        source = _FakeHandle()
        voltmeter = _TimeoutFailingHandle()
        resource_manager = _TimeoutResourceManager(source, voltmeter)
        pyvisa = types.ModuleType("pyvisa")
        pyvisa.ResourceManager = lambda: resource_manager

        with patch.dict(sys.modules, {"pyvisa": pyvisa}):
            with self.assertRaisesRegex(RuntimeError, "timeout setup failed"):
                Keithley2400With2182A(
                    "GPIB0::1::INSTR",
                    "GPIB0::2::INSTR",
                )

        self.assertTrue(source.closed)
        self.assertTrue(voltmeter.closed)
        self.assertTrue(resource_manager.closed)

    def test_partial_owned_resource_open_failure_closes_source_and_manager(
        self,
    ) -> None:
        source = _FakeHandle()
        resource_manager = _FailingSecondOpenResourceManager(source)
        pyvisa = types.ModuleType("pyvisa")
        pyvisa.ResourceManager = lambda: resource_manager

        with patch.dict(sys.modules, {"pyvisa": pyvisa}):
            with self.assertRaisesRegex(RuntimeError, "voltmeter open failed"):
                Keithley2400With2182A(
                    "GPIB0::1::INSTR",
                    "GPIB0::2::INSTR",
                )

        self.assertEqual(
            resource_manager.opened,
            ["GPIB0::1::INSTR", "GPIB0::2::INSTR"],
        )
        self.assertTrue(source.closed)
        self.assertTrue(resource_manager.closed)

    def test_software_read_failure_restores_zero_volts_with_output_on(self) -> None:
        events: list[tuple[str, str, str]] = []
        source = _EventHandle("source", events, fail_query="FETC?")
        voltmeter = _EventHandle(
            "voltmeter",
            events,
            {"READ?": ["-0.029"]},
        )
        meter = _meter_with_events(source, voltmeter, events)

        with self.assertRaisesRegex(RuntimeError, "FETC\\? failed"):
            meter.measure_pair(
                Keithley2400With2182AConfig(
                    use_buffer=False,
                    use_trigger_link=False,
                )
            )

        self.assertEqual(source.writes[-2:], [":SOUR:VOLT 0", ":OUTP ON"])
        self.assertNotIn(":OUTP OFF", source.writes)

    def test_buffered_opc_failure_aborts_and_restores_timeout_and_output(self) -> None:
        events: list[tuple[str, str, str]] = []
        source = _EventHandle("source", events, fail_query="*OPC?")
        voltmeter = _EventHandle("voltmeter", events)
        meter = _meter_with_events(source, voltmeter, events)

        with self.assertRaisesRegex(
            RuntimeError,
            "buffered Trigger Link sequence did not complete",
        ):
            meter.measure_voltage_list(
                [-0.03, 0.03],
                Keithley2400With2182AConfig(
                    use_buffer=True,
                    use_trigger_link=True,
                ),
            )

        opc_index = events.index(("source", "query", "*OPC?"))
        self.assertEqual(
            events[opc_index + 1 :],
            [
                ("source", "clear", ""),
                ("source", "write", ":ABOR"),
                ("source", "write", ":TRIG:CLE"),
                ("voltmeter", "write", "ABOR"),
                ("voltmeter", "write", "TRIG:SOUR IMM"),
                ("voltmeter", "write", "TRIG:COUN 1"),
                ("voltmeter", "write", "SAMP:COUN 1"),
                ("source", "write", ":SOUR:VOLT:MODE FIX"),
                ("source", "write", ":SOUR:VOLT 0"),
                ("source", "write", ":OUTP ON"),
            ],
        )
        self.assertEqual(source.timeout, 10_000)
        self.assertGreater(max(source.timeout_history), 10_000)
        self.assertEqual(source.timeout_history[-1], 10_000)

    def test_source_list_write_failure_reports_chunk_and_scpi_error(self) -> None:
        events: list[tuple[str, str, str]] = []
        source = _EventHandle(
            "source",
            events,
            fail_write_prefix=":SOUR:LIST:VOLT:APPend",
            scpi_errors_after_write_failure=[
                '-113,"Undefined header"',
                '0,"No error"',
            ],
        )
        voltmeter = _EventHandle("voltmeter", events)
        meter = _meter_with_events(source, voltmeter, events)

        with self.assertRaisesRegex(RuntimeError, "source-list write failed") as caught:
            meter.measure_voltage_list(
                [0.001 * index for index in range(101)],
                Keithley2400With2182AConfig(
                    use_buffer=True,
                    use_trigger_link=True,
                ),
            )

        message = str(caught.exception)
        self.assertIn("101 points total", message)
        self.assertIn("chunk offset 100", message)
        self.assertIn("1 points", message)
        self.assertIn('-113,"Undefined header"', message)
        source_list_events = [
            event
            for event in events
            if event[:2] == ("source", "write")
            and event[2].startswith(":SOUR:LIST:VOLT")
        ]
        self.assertEqual(len(source_list_events), 2)
        self.assertTrue(source_list_events[0][2].startswith(":SOUR:LIST:VOLT "))
        self.assertTrue(source_list_events[1][2].startswith(":SOUR:LIST:VOLT:APPend "))
        self.assertIn(f"{len(source_list_events[1][2])} characters", message)

    def test_common_configuration_propagates_source_scpi_errors(self) -> None:
        events: list[tuple[str, str, str]] = []
        source = _EventHandle(
            "source",
            events,
            {
                "SYST:ERR?": [
                    '-222,"Data out of range"',
                    '0,"No error"',
                ]
            },
        )
        meter = _meter_with_events(source, None, events)

        with self.assertRaisesRegex(RuntimeError, "2400 source SCPI error") as caught:
            meter.configure()

        self.assertEqual(
            str(caught.exception),
            '2400 source SCPI error after configuration: -222,"Data out of range"',
        )

    def test_short_trace_errors_name_the_incomplete_instrument(self) -> None:
        messages: list[str] = []
        cases = (
            (
                "source",
                "-0.03,-0.001",
                "-0.029,0.031",
            ),
            (
                "voltmeter",
                "-0.03,-0.001,0.03,0.001",
                "-0.029",
            ),
        )
        for label, source_trace, voltmeter_trace in cases:
            with self.subTest(label=label):
                events: list[tuple[str, str, str]] = []
                source = _EventHandle(
                    "source",
                    events,
                    {"*OPC?": ["1"], "TRAC:DATA?": [source_trace]},
                )
                voltmeter = _EventHandle(
                    "voltmeter",
                    events,
                    {"TRAC:DATA?": [voltmeter_trace]},
                )
                meter = _meter_with_events(source, voltmeter, events)

                with self.assertRaises(RuntimeError) as caught:
                    meter.measure_voltage_list([-0.03, 0.03])
                messages.append(str(caught.exception))

        self.assertEqual(
            messages,
            [
                "2400 buffer returned 2 values for 2 points",
                "2182A buffer returned 1 values for 2 points",
            ],
        )

    def test_abort_and_close_are_repeatable_with_exact_safe_output_order(self) -> None:
        events: list[tuple[str, str, str]] = []
        source = _EventHandle("source", events)
        voltmeter = _EventHandle("voltmeter", events)
        meter = _meter_with_events(source, voltmeter, events)
        events.clear()

        meter.abort()
        meter.abort()
        meter.close()
        meter.close()
        meter.abort()

        self.assertEqual(
            events,
            [
                ("voltmeter", "write", "ABOR"),
                ("source", "write", ":ABOR"),
                ("source", "write", ":SOUR:VOLT 0"),
                ("source", "write", ":OUTP OFF"),
                ("voltmeter", "write", "ABOR"),
                ("source", "write", ":ABOR"),
                ("source", "write", ":SOUR:VOLT 0"),
                ("source", "write", ":OUTP OFF"),
                ("source", "write", ":SOUR:VOLT:MODE FIX"),
                ("source", "write", ":SOUR:VOLT 0"),
                ("source", "write", ":OUTP ON"),
                ("source", "close", ""),
                ("voltmeter", "close", ""),
            ],
        )

    def test_callback_timing_and_software_omission_remain_exact(self) -> None:
        events: list[tuple[str, str, str]] = []
        source = _EventHandle(
            "source",
            events,
            {
                "*OPC?": ["1"],
                "TRAC:DATA?": ["-0.03,-0.001,0.03,0.001"],
            },
        )
        voltmeter = _EventHandle(
            "voltmeter",
            events,
            {"TRAC:DATA?": ["-0.029,0.031"]},
        )
        meter = _meter_with_events(source, voltmeter, events)
        callback_event = ("callback", "called", "")

        meter.measure_voltage_list(
            [-0.03, 0.03],
            after_measurement=lambda: events.append(callback_event),
        )

        callback_index = events.index(callback_event)
        trace_index = events.index(("source", "query", "TRAC:DATA?"))
        cleanup_index = max(
            index
            for index, event in enumerate(events[:callback_index])
            if event == ("source", "write", ":OUTP ON")
        )
        self.assertLess(cleanup_index, callback_index)
        self.assertLess(callback_index, trace_index)

        software_callbacks: list[bool] = []
        software_events: list[tuple[str, str, str]] = []
        software_source = _EventHandle(
            "source",
            software_events,
            {"FETC?": ["-0.03,-0.001", "0.03,0.001"]},
        )
        software_voltmeter = _EventHandle(
            "voltmeter",
            software_events,
            {"READ?": ["-0.029", "0.031"]},
        )
        software_meter = _meter_with_events(
            software_source,
            software_voltmeter,
            software_events,
        )
        software_meter.measure_voltage_list(
            [-0.03, 0.03],
            Keithley2400With2182AConfig(use_buffer=False),
            after_measurement=lambda: software_callbacks.append(True),
        )

        source_only_callbacks: list[bool] = []
        source_only_events: list[tuple[str, str, str]] = []
        source_only = _meter_with_events(
            _EventHandle(
                "source",
                source_only_events,
                {"FETC?": ["-0.03,-0.001", "0.03,0.001"]},
            ),
            None,
            source_only_events,
        )
        source_only.measure_voltage_list(
            [-0.03, 0.03],
            after_measurement=lambda: source_only_callbacks.append(True),
        )

        self.assertEqual(software_callbacks, [])
        self.assertEqual(source_only_callbacks, [])

    def test_raw_visa_output_mutation_does_not_invalidate_cached_state(self) -> None:
        events: list[tuple[str, str, str]] = []
        source = _EventHandle("source", events)
        meter = _meter_with_events(source, None, events)
        meter.configure()
        events.clear()

        raw_source = meter.visa_handle_for_role("meter.source")
        raw_source.write(":OUTP OFF")
        with meter.output(True):
            pass

        self.assertEqual(
            events,
            [
                ("source", "write", ":OUTP OFF"),
                ("source", "write", ":SOUR:VOLT:MODE FIX"),
                ("source", "write", ":SOUR:VOLT -0.03"),
            ],
        )


if __name__ == "__main__":
    unittest.main()
