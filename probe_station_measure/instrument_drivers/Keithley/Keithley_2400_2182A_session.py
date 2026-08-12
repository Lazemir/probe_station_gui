"""VISA session and safe-output ownership for the Keithley ohmmeter."""

from __future__ import annotations

from contextlib import contextmanager
import logging
import math
import re
import time
from collections.abc import Callable, Iterator, Sequence
from typing import Any


logger = logging.getLogger(__name__)


class KeithleySession:
    """Own both VISA handles, common SCPI state, and safe output cleanup."""

    def __init__(
        self,
        source_resource: str,
        voltmeter_resource: str | None,
        *,
        timeout_ms: int,
        resource_manager: object | None = None,
    ) -> None:
        if resource_manager is None:
            try:
                import pyvisa
            except ImportError as exc:  # pragma: no cover - environment specific
                raise RuntimeError(
                    "Keithley measurements require pyvisa. "
                    "Install probe-station-measure with the 'visa' extra."
                ) from exc
            resource_manager = pyvisa.ResourceManager()
            self._owns_resource_manager = True
        else:
            self._owns_resource_manager = False
        self._resource_manager = resource_manager
        self._source = None
        self._voltmeter = None
        self.configured: object | None = None
        self.prepared_key: object | None = None
        self.source_output_enabled = False
        self.output_depth = 0
        try:
            self._source = self._open_resource(source_resource, timeout_ms)
            if voltmeter_resource is not None and str(voltmeter_resource).strip():
                self._voltmeter = self._open_resource(voltmeter_resource, timeout_ms)
        except Exception:
            if self._source is not None:
                _close_handle(self._source)
                self._source = None
            if self._owns_resource_manager:
                _close_handle(self._resource_manager)
            raise

    def _open_resource(self, address: str, timeout_ms: int):
        handle = self._resource_manager.open_resource(str(address).strip())
        try:
            handle.timeout = int(timeout_ms)
        except Exception:
            _close_handle(handle)
            raise
        for attribute, value in (
            ("read_termination", "\n"),
            ("write_termination", "\n"),
        ):
            try:
                setattr(handle, attribute, value)
            except Exception:
                logger.debug(
                    "VISA handle does not accept %s=%r",
                    attribute,
                    value,
                    exc_info=True,
                )
        return handle

    def identify(self) -> str:
        parts = []
        source_id = self._safe_query(self._source, "*IDN?")
        voltmeter_id = self._safe_query(self._voltmeter, "*IDN?")
        if source_id:
            parts.append(f"2400 {source_id}")
        if voltmeter_id:
            parts.append(f"2182A {voltmeter_id}")
        return "; ".join(parts)

    @contextmanager
    def output(
        self,
        config: Any,
        enabled: bool = True,
        *,
        prime_source: bool = True,
    ) -> Iterator["KeithleySession"]:
        """Keep the 2400 output relay in one state across several operations."""

        requested_enabled = bool(enabled)
        previous_enabled = bool(self.source_output_enabled)
        if requested_enabled:
            self.output_depth += 1
        try:
            if requested_enabled and self.configured is None:
                self._configure_common(config)
            if requested_enabled and prime_source and self._source is not None:
                voltage = -abs(float(config.measurement_voltage_v))
                self._write(self._source, ":SOUR:VOLT:MODE FIX")
                self._write(self._source, f":SOUR:VOLT {voltage:.12g}")
            self._set_source_output_enabled(requested_enabled)
            yield self
        finally:
            if requested_enabled:
                self.output_depth = max(
                    0,
                    self.output_depth - 1,
                )
                if previous_enabled:
                    self._set_source_output_enabled(
                        True,
                        best_effort=True,
                        zero_voltage=False,
                    )
                else:
                    source = self._source
                    if source is not None:
                        self._try_write(source, ":SOUR:VOLT:MODE FIX")
                        self._try_write(source, ":SOUR:VOLT 0")
                        self._try_write(source, ":OUTP ON")
                    self.source_output_enabled = True
            else:
                self._set_source_output_enabled(
                    previous_enabled,
                    best_effort=True,
                    zero_voltage=True,
                )

    def configure_common(
        self,
        config: Any,
        *,
        force: bool = False,
    ) -> None:
        if not force and self.configured == config:
            return
        self.prepared_key = None
        source = self._require_source()
        voltmeter = self._voltmeter
        terminal_scpi = "FRON" if config.terminals == "front" else "REAR"

        self._try_clear(source)
        if voltmeter is not None:
            self._try_clear(voltmeter)
        self._write(source, "*CLS")
        if voltmeter is not None:
            self._write(voltmeter, "*CLS")
            self._try_write(voltmeter, "INIT:CONT OFF")
        self._try_write(source, ":ABOR")
        if voltmeter is not None:
            self._try_write(voltmeter, "ABOR")
        self._try_write(source, ":TRIG:CLE")
        self._try_write(source, f":ROUT:TERM {terminal_scpi}")

        self._write(source, ":ARM:SOUR IMM")
        self._write(source, ":ARM:COUN 1")
        self._write(source, ":ARM:DIR ACC")
        self._write(source, ":ARM:OUTP NONE")
        self._write(source, ":TRIG:SOUR IMM")
        self._write(source, ":TRIG:COUN 1")
        self._write(source, ":TRIG:DIR ACC")
        self._write(source, ":TRIG:INP NONE")
        self._write(source, ":TRIG:OUTP NONE")
        self._write(source, ":TRIG:DEL 0")

        if voltmeter is not None:
            self._write(voltmeter, "CONF:VOLT")
            self._write(voltmeter, "SENS:CHAN 1")
            self._write(
                voltmeter,
                f"SENS:VOLT:RANG {config.voltmeter_range_v:.12g}",
            )
            self._write(voltmeter, f"SENS:VOLT:NPLC {config.nplc:.12g}")
            self._write(voltmeter, "TRIG:SOUR IMM")
            self._write(voltmeter, "TRIG:COUN 1")
            self._write(voltmeter, "SAMP:COUN 1")
            self._write(voltmeter, "TRIG:DEL 0")
            self._try_write(voltmeter, "TRIG:DEL:AUTO OFF")
            self._try_write(voltmeter, "SENS:VOLT:DFIL:STAT OFF")
            self._try_write(voltmeter, "FORM:ELEM READ")
            self._try_write(voltmeter, "TRAC:CLE")

        if voltmeter is None:
            self._write(source, ":SENS:FUNC:CONC ON")
            self._write(source, ':SENS:FUNC:ON "VOLT:DC"')
            self._write(source, ':SENS:FUNC:ON "CURR:DC"')
        else:
            self._try_write(source, ":SENS:FUNC:CONC OFF")
            self._write(source, ':SENS:FUNC "CURR:DC"')
        self._write(source, ":SOUR:FUNC VOLT")
        self._write(source, ":SOUR:VOLT:MODE FIX")
        self._write(
            source,
            f":SOUR:VOLT:RANG {config.source_voltage_range_v:.12g}",
        )
        self._write(source, f":SENS:CURR:PROT {config.compliance_current_a:.12g}")
        self._write(source, f":SENS:CURR:RANG {config.current_range_a:.12g}")
        self._write(source, f":SENS:CURR:NPLC {config.nplc:.12g}")
        if voltmeter is None:
            self._write(source, ":FORM:ELEM VOLT,CURR")
        else:
            self._try_write(source, ":FORM:ELEM VOLT,CURR")
        if self.output_depth == 0:
            self._write(source, ":SOUR:VOLT 0")
            self._write(source, ":OUTP ON")
            self.source_output_enabled = True
        self._raise_scpi_errors(source, "2400 source", "configuration")
        if voltmeter is not None:
            self._raise_scpi_errors(voltmeter, "2182A voltmeter", "configuration")
        self.configured = config

    def measure_software(
        self,
        source_voltages: Sequence[float],
        config: Any,
        reading_factory: Callable[..., Any],
    ) -> list[Any]:
        self.configure_common(
            config,
            force=self.output_depth == 0,
        )
        source = self._require_source()
        voltmeter = self._voltmeter
        readings: list[Any] = []
        try:
            self._set_source_output_enabled(True)
            for voltage in source_voltages:
                self._write(source, f":SOUR:VOLT {voltage:.12g}")
                if config.trigger_delay_s > 0:
                    time.sleep(config.trigger_delay_s)
                self._write(source, "INIT")
                voltage_reading = math.nan
                if voltmeter is not None:
                    voltage_reading = float(self._query(voltmeter, "READ?"))
                source_values = _parse_float_list(self._query(source, "FETC?"))
                if len(source_values) < 2:
                    raise RuntimeError("2400 FETC? did not return voltage,current")
                if voltmeter is None:
                    voltage_reading = float(source_values[0])
                readings.append(
                    reading_factory(
                        source_voltage_v=float(voltage),
                        measured_voltage_v=voltage_reading,
                        current_a=float(source_values[1]),
                        compliance_current_a=config.compliance_current_a,
                    )
                )
        finally:
            if self.output_depth > 0:
                self.source_output_enabled = True
            else:
                self._try_write(source, ":SOUR:VOLT 0")
                self._try_write(source, ":OUTP ON")
                self.source_output_enabled = True
        self._raise_scpi_errors(source, "2400 source", "software measurement")
        if voltmeter is not None:
            self._raise_scpi_errors(
                voltmeter,
                "2182A voltmeter",
                "software measurement",
            )
        return readings

    def abort(self) -> None:
        if self._voltmeter is not None:
            self._try_write(self._voltmeter, "ABOR")
        if self._source is not None:
            self._try_write(self._source, ":ABOR")
            self._set_source_output_enabled(
                False,
                best_effort=True,
                force=True,
            )

    def trace_status(self, status_factory: Callable[..., Any]) -> dict[str, Any]:
        status = {
            "source": self._trace_status(
                self._require_source(),
                status_factory,
                supports_actual_points=True,
            ),
        }
        if self._voltmeter is not None:
            status["voltmeter"] = self._trace_status(
                self._require_voltmeter(),
                status_factory,
                supports_actual_points=False,
            )
        return status

    def visa_resource_roles(self) -> dict[str, dict[str, object]]:
        roles: dict[str, dict[str, object]] = {}
        if self._source is not None:
            roles["meter.source"] = {
                "role": "meter.source",
                "kind": "source_meter",
                "model": "Keithley 2400",
                "required": True,
            }
        if self._voltmeter is not None:
            roles["meter.voltmeter"] = {
                "role": "meter.voltmeter",
                "kind": "voltmeter",
                "model": "Keithley 2182A",
                "required": False,
            }
        return roles

    def visa_handle_for_role(self, role: str):
        normalized = _normalize_visa_role(role)
        if normalized in {"meter.source", "source", "source_meter", "meter"}:
            return self._require_source()
        if normalized in {"meter.voltmeter", "voltmeter", "meter.voltage"}:
            return self._require_voltmeter()
        raise KeyError(f"Unsupported Keithley VISA role: {role}")

    def close(self) -> None:
        source = self._source
        voltmeter = self._voltmeter
        if source is not None:
            self._try_write(source, ":SOUR:VOLT:MODE FIX")
            self._try_write(source, ":SOUR:VOLT 0")
            self._try_write(source, ":OUTP ON")
            self.source_output_enabled = True
            _close_handle(source)
        self._source = None
        self._voltmeter = None
        if voltmeter is not None:
            _close_handle(voltmeter)
        if self._owns_resource_manager:
            _close_handle(self._resource_manager)

    def _set_source_output_enabled(
        self,
        enabled: bool,
        *,
        best_effort: bool = False,
        force: bool = False,
        zero_voltage: bool = True,
    ) -> None:
        source = self._source
        if source is None:
            if best_effort:
                return
            source = self._require_source()
        write = self._try_write if best_effort else self._write
        enabled = bool(enabled)
        if not enabled and zero_voltage:
            write(source, ":SOUR:VOLT 0")
        if force or self.source_output_enabled != enabled:
            write(source, f":OUTP {'ON' if enabled else 'OFF'}")
        self.source_output_enabled = enabled

    def _require_source(self):
        if self._source is None:
            raise RuntimeError("Keithley 2400 source is not open")
        return self._source

    def _require_voltmeter(self):
        if self._voltmeter is None:
            raise RuntimeError("Keithley 2182A voltmeter is not open")
        return self._voltmeter

    @staticmethod
    def _write(handle, command: str) -> None:
        handle.write(command)

    @staticmethod
    def _query(handle, query: str) -> str:
        if hasattr(handle, "query"):
            return str(handle.query(query)).strip()
        return str(handle.ask(query)).strip()

    def _try_write(self, handle, command: str) -> None:
        try:
            self._write(handle, command)
        except Exception:
            logger.debug("Keithley command failed: %s", command, exc_info=True)

    def _try_clear(self, handle) -> None:
        clearer = getattr(handle, "clear", None)
        if not callable(clearer):
            return
        try:
            clearer()
        except Exception:
            logger.debug("Keithley device clear failed", exc_info=True)

    def _safe_query(self, handle, query: str) -> str:
        if handle is None:
            return ""
        try:
            return self._query(handle, query).strip()
        except Exception:
            logger.debug("Keithley query failed: %s", query, exc_info=True)
            return ""

    def _trace_status(
        self,
        handle,
        status_factory: Callable[..., Any],
        *,
        supports_actual_points: bool,
    ) -> Any:
        points = _optional_int(self._safe_query(handle, "TRAC:POIN?"))
        actual_points = (
            _optional_int(self._safe_query(handle, "TRAC:POIN:ACT?"))
            if supports_actual_points
            else None
        )
        free_response = self._safe_query(handle, "TRAC:FREE?")
        free_bytes, reserved_bytes = _optional_int_pair(free_response)
        feed = self._safe_query(handle, "TRAC:FEED?") or None
        control = self._safe_query(handle, "TRAC:FEED:CONT?") or None
        return status_factory(
            points=points,
            actual_points=actual_points,
            free_bytes=free_bytes,
            reserved_bytes=reserved_bytes,
            feed=feed,
            control=control,
        )

    def _raise_scpi_errors(self, handle, label: str, context: str) -> None:
        errors = self._read_scpi_errors(handle)
        if errors:
            details = "; ".join(errors)
            raise RuntimeError(f"{label} SCPI error after {context}: {details}")

    def _read_scpi_errors(self, handle) -> list[str]:
        errors: list[str] = []
        for _index in range(8):
            response = self._safe_query(handle, "SYST:ERR?")
            if not response:
                break
            code = _scpi_error_code(response)
            if code == 0:
                break
            errors.append(response)
        return errors


def _parse_float_list(response: str) -> list[float]:
    values: list[float] = []
    for token in re.split(r"[\s,]+", str(response).strip()):
        if not token:
            continue
        values.append(float(token))
    return values


def _normalize_visa_role(role: object) -> str:
    return str(role or "").strip().lower().replace("-", "_")


def _optional_int(response: str) -> int | None:
    try:
        return int(float(str(response).strip()))
    except (TypeError, ValueError):
        return None


def _optional_int_pair(response: str) -> tuple[int | None, int | None]:
    values = [item.strip() for item in str(response).split(",", 1)]
    if len(values) != 2:
        return None, None
    return _optional_int(values[0]), _optional_int(values[1])


def _scpi_error_code(response: str) -> int | None:
    match = re.match(r"\s*([+-]?\d+)", str(response))
    if match is None:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def _close_handle(handle) -> None:
    try:
        handle.close()
    except Exception:
        logger.debug("Failed to close handle", exc_info=True)
