"""Prepared source lists and buffered Trigger-Link acquisition."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import math
import time
from typing import Any

from .Keithley_2400_2182A_session import KeithleySession, _parse_float_list


class TriggerLinkBatch:
    """Own one two-instrument source-list protocol and its prepared cache."""

    def __init__(
        self,
        session: KeithleySession,
        *,
        max_source_list_points: int,
        max_source_list_points_per_command: int,
        max_trace_points: int,
        default_timeout_ms: int,
        max_timeout_ms: int,
        base_timeout_s: float,
        point_overhead_s: float,
        nplc_point_s: float,
    ) -> None:
        self._session = session
        self._max_source_list_points = max_source_list_points
        self._max_source_list_points_per_command = max_source_list_points_per_command
        self._max_trace_points = max_trace_points
        self._default_timeout_ms = default_timeout_ms
        self._max_timeout_ms = max_timeout_ms
        self._base_timeout_s = base_timeout_s
        self._point_overhead_s = point_overhead_s
        self._nplc_point_s = nplc_point_s
        self._source_list_cache: tuple[float, ...] = ()

    def measure(
        self,
        source_voltages: Sequence[float],
        config: Any,
        reading_factory: Callable[..., Any],
        *,
        after_measurement: Callable[[], object] | None = None,
    ) -> list[Any]:
        source = self._session._require_source()
        voltmeter = self._session._require_voltmeter()
        points = len(source_voltages)
        if points < 1:
            raise ValueError("Source voltage list cannot be empty")
        if points > self._max_source_list_points:
            raise ValueError(
                f"2400 trace buffer accepts at most "
                f"{self._max_source_list_points} readings"
            )
        if points > self._max_trace_points:
            raise ValueError(
                f"2182A trace buffer accepts at most {self._max_trace_points} readings"
            )
        requested_voltages = tuple(float(value) for value in source_voltages)
        if not self._prepared_voltage_list_matches(
            requested_voltages,
            config,
            points,
        ):
            self.prepare(
                requested_voltages,
                config,
                measure_points=points,
            )

        try:
            self._session._write(voltmeter, "INIT")
            time.sleep(0.1)
            self._session._set_source_output_enabled(True)
            self._session._write(source, ":INIT")
            try:
                with _temporary_timeout(
                    source,
                    self._measurement_timeout_ms(points, config),
                ):
                    self._session._query(source, "*OPC?")
            except Exception as exc:
                self._session._try_clear(source)
                self._session._try_write(source, ":ABOR")
                self._session._try_write(source, ":TRIG:CLE")
                self._session._try_write(voltmeter, "ABOR")
                self._session._try_write(voltmeter, "TRIG:SOUR IMM")
                self._session._try_write(voltmeter, "TRIG:COUN 1")
                self._session._try_write(voltmeter, "SAMP:COUN 1")
                raise RuntimeError(
                    "2400 buffered Trigger Link sequence did not complete "
                    f"for {points} source points. Check Trigger Link line "
                    "mapping and the 2182A external-trigger state."
                ) from exc
        finally:
            self._session._try_write(source, ":SOUR:VOLT:MODE FIX")
            if self._session.output_depth > 0:
                self._session.source_output_enabled = True
            else:
                self._session._try_write(source, ":SOUR:VOLT 0")
                self._session._try_write(source, ":OUTP ON")
                self._session.source_output_enabled = True
            self._session.prepared_key = None

        if after_measurement is not None:
            after_measurement()

        source_trace, voltmeter_trace = self._read_trace_buffers_parallel()
        self._session._try_write(source, "TRAC:FEED:CONT NEV")
        self._session._try_write(voltmeter, "TRAC:FEED:CONT NEV")
        self._session._try_write(voltmeter, "TRIG:SOUR IMM")
        self._session._try_write(voltmeter, "TRIG:COUN 1")
        self._session._try_write(voltmeter, "SAMP:COUN 1")
        self._session._try_write(source, ":SOUR:VOLT:MODE FIX")
        self._session._raise_scpi_errors(
            source,
            "2400 source",
            "buffered measurement",
        )
        self._session._raise_scpi_errors(
            voltmeter,
            "2182A voltmeter",
            "buffered measurement",
        )

        if len(source_trace) < points * 2:
            raise RuntimeError(
                f"2400 buffer returned {len(source_trace)} values for {points} points"
            )
        if len(voltmeter_trace) < points:
            raise RuntimeError(
                f"2182A buffer returned {len(voltmeter_trace)} values for {points} points"
            )
        return [
            reading_factory(
                source_voltage_v=float(source_voltage),
                measured_voltage_v=float(voltmeter_trace[index]),
                current_a=float(source_trace[index * 2 + 1]),
                compliance_current_a=config.compliance_current_a,
            )
            for index, source_voltage in enumerate(source_voltages)
        ]

    def prepare(
        self,
        source_voltages: Sequence[float],
        config: Any,
        *,
        measure_points: int | None = None,
    ) -> None:
        source = self._session._require_source()
        voltmeter = self._session._require_voltmeter()
        source_list = tuple(float(value) for value in source_voltages)
        points = len(source_list) if measure_points is None else int(measure_points)
        if points < 1:
            raise ValueError("Source voltage list cannot be empty")
        if points > len(source_list):
            raise ValueError("Prepared source list is shorter than trigger count")
        if len(source_list) > self._max_source_list_points:
            raise ValueError(
                f"2400 source list accepts at most "
                f"{self._max_source_list_points} readings"
            )
        if points > self._max_trace_points:
            raise ValueError(
                f"2182A trace buffer accepts at most {self._max_trace_points} readings"
            )

        self._session.configure_common(config)
        self._session._try_write(source, ":ABOR")
        self._session._try_write(voltmeter, "ABOR")
        self._session._try_write(source, "TRAC:FEED:CONT NEV")
        self._session._try_write(voltmeter, "TRAC:FEED:CONT NEV")
        self._session._try_write(source, "*CLS")
        self._session._try_write(voltmeter, "*CLS")
        self._session._try_write(source, ":TRIG:CLE")
        self._session._write(source, ":SOUR:VOLT:MODE LIST")
        if not self._source_list_covers(source_list):
            self._write_source_voltage_list(source_list)
            self._source_list_cache = source_list
        self._session._write(source, f":TRIG:COUN {points}")
        self._session._write(source, ":TRIG:SOUR TLIN")
        self._session._write(source, ":TRIG:DIR SOUR")
        self._session._write(source, ":TRIG:INP SOUR")
        self._session._write(source, ":TRIG:ILIN 1")
        self._session._write(source, ":TRIG:OLIN 2")
        self._session._write(source, ":TRIG:OUTP SOUR")
        self._session._write(source, "TRIG:DEL 0")
        self._session._write(source, ":SOUR:DEL 0")
        self._session._write(source, "TRAC:CLE")
        self._session._write(source, f"TRAC:POIN {points}")
        self._session._write(source, "TRAC:FEED SENS")
        self._session._raise_scpi_errors(source, "2400 source", "source list setup")

        self._session._write(voltmeter, "TRIG:SOUR EXT")
        self._session._write(voltmeter, "TRIG:DEL 0")
        self._session._write(voltmeter, f"TRIG:COUN {points}")
        self._session._write(voltmeter, "SAMP:COUN 1")
        self._session._write(voltmeter, "TRAC:CLE")
        self._session._write(voltmeter, f"TRAC:POIN {points}")
        self._session._write(voltmeter, "TRAC:FEED SENS")
        self._session._raise_scpi_errors(
            voltmeter,
            "2182A voltmeter",
            "buffer setup",
        )

        self._session._write(voltmeter, "TRAC:FEED:CONT NEXT")
        self._session._write(source, "TRAC:FEED:CONT NEXT")
        self._session.prepared_key = (
            config,
            source_list[:points],
            points,
        )

    def _prepared_voltage_list_matches(
        self,
        requested_voltages: tuple[float, ...],
        config: Any,
        points: int,
    ) -> bool:
        return self._session.prepared_key == (
            config,
            requested_voltages[:points],
            points,
        )

    def _source_list_covers(self, requested_voltages: tuple[float, ...]) -> bool:
        cached = self._source_list_cache
        return (
            len(cached) >= len(requested_voltages)
            and cached[: len(requested_voltages)] == requested_voltages
        )

    def _read_trace_buffers_parallel(self) -> tuple[list[float], list[float]]:
        source = self._session._require_source()
        voltmeter = self._session._require_voltmeter()
        with ThreadPoolExecutor(max_workers=2) as executor:
            source_future = executor.submit(
                lambda: _parse_float_list(self._session._query(source, "TRAC:DATA?"))
            )
            voltmeter_future = executor.submit(
                lambda: _parse_float_list(self._session._query(voltmeter, "TRAC:DATA?"))
            )
            return source_future.result(), voltmeter_future.result()

    def _write_source_voltage_list(self, values: Sequence[float]) -> None:
        source = self._session._require_source()
        values = [float(value) for value in values]
        if not values:
            raise ValueError("Source voltage list cannot be empty")
        if len(values) > self._max_source_list_points:
            raise ValueError(
                "2400 source list accepts at most "
                f"{self._max_source_list_points} points"
            )
        step = self._max_source_list_points_per_command
        for offset in range(0, len(values), step):
            chunk = values[offset : offset + step]
            command_name = (
                ":SOUR:LIST:VOLT" if offset == 0 else ":SOUR:LIST:VOLT:APPend"
            )
            command = f"{command_name} " + ",".join(f"{value:.12g}" for value in chunk)
            try:
                self._session._write(source, command)
            except Exception as exc:
                details = "; ".join(self._session._read_scpi_errors(source))
                suffix = f" SCPI errors: {details}" if details else ""
                raise RuntimeError(
                    "2400 source-list write failed "
                    f"({len(values)} points total, chunk offset {offset}, "
                    f"{len(chunk)} points, {len(command)} characters)."
                    f"{suffix}"
                ) from exc
            self._session._raise_scpi_errors(
                source,
                "2400 source",
                "source list setup",
            )

    def _measurement_timeout_ms(
        self,
        points: int,
        config: Any,
    ) -> int:
        point_count = max(1, int(points))
        nplc = max(0.01, _finite_float(config.nplc, 1.0))
        trigger_delay_s = max(0.0, _finite_float(config.trigger_delay_s, 0.0))
        timeout_s = self._base_timeout_s + point_count * (
            self._point_overhead_s + self._nplc_point_s * nplc + trigger_delay_s
        )
        return max(
            self._default_timeout_ms,
            min(
                self._max_timeout_ms,
                int(math.ceil(timeout_s * 1000.0)),
            ),
        )


@contextmanager
def _temporary_timeout(handle, timeout_ms: int) -> Iterator[None]:
    previous_timeout = getattr(handle, "timeout", None)
    if previous_timeout is None:
        yield
        return
    handle.timeout = int(timeout_ms)
    try:
        yield
    finally:
        handle.timeout = previous_timeout


def _finite_float(value: object, default: float) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return float(default)
    if not math.isfinite(result):
        return float(default)
    return result
