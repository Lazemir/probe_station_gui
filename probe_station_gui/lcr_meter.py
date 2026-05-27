"""Resistance-meter integrations used by calibration and route measurements."""

from __future__ import annotations

import logging
import math
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

from PySide6.QtCore import QObject, Signal

from probe_station_gui.gwinstek_lcr_76200 import (
    format_source_level_value,
    normalize_resource_name,
)


logger = logging.getLogger(__name__)


ROUTE_METER_GWINSTEK = "gwinstek_lcr_76200"
ROUTE_METER_KEITHLEY = "keithley_2400_2182a"
ROUTE_METER_TYPES: tuple[str, ...] = (
    ROUTE_METER_GWINSTEK,
    ROUTE_METER_KEITHLEY,
)
ROUTE_METER_LABELS: dict[str, str] = {
    ROUTE_METER_GWINSTEK: "GW Instek LCR-76200",
    ROUTE_METER_KEITHLEY: "Keithley 2400 + 2182A",
}


@dataclass(frozen=True)
class GWInstekRouteMeterSettings:
    """Per-run GW Instek LCR settings for route measurements."""

    resource_name: str = "COM4"
    measurement_function: str = "DCR"
    range_mode: str = "AUTO"
    impedance_range: int = 3
    dcr_range: int = 4
    frequency_hz: float = 50.0
    level_mode: str = "VOLTAGE"
    voltage_level_v: float = 0.03
    current_level_a: float = 0.0001
    source_resistance_ohm: int = 100
    aperture_rate: str = "SLOW"
    aperture_averages: int = 1
    trigger_delay_s: float = 0.0
    bias_enabled: bool = False
    bias_level_v: float = 0.0
    monitor1: str = "OFF"
    monitor2: str = "OFF"
    alc_enabled: bool = False


@dataclass(frozen=True)
class KeithleyRouteMeterSettings:
    """Per-run four-wire resistance settings for a Keithley 2400 and 2182A."""

    source_resource: str = "GPIB2::1::INSTR"
    voltmeter_resource: str = "GPIB2::2::INSTR"
    measurement_voltage_v: float = 0.03
    source_voltage_range_v: float = 0.21
    compliance_current_a: float = 500e-6
    nplc: float = 10.0
    terminals: str = "rear"
    trigger_delay_s: float = 0.01


@dataclass(frozen=True)
class RouteMeterConfiguration:
    """Per-run meter selection and settings from the route measurement dialog."""

    meter_type: str = ROUTE_METER_KEITHLEY
    gwinstek: GWInstekRouteMeterSettings = field(
        default_factory=GWInstekRouteMeterSettings
    )
    keithley: KeithleyRouteMeterSettings = field(
        default_factory=KeithleyRouteMeterSettings
    )

    def nplc_label(self) -> str:
        """Return the CSV integration-time label for this meter."""

        if self.meter_type == ROUTE_METER_KEITHLEY:
            return f"{float(self.keithley.nplc):g}"
        return ""


class LCRMeterError(RuntimeError):
    """Raised when the LCR meter backend cannot complete the request."""


class _LCRSession:
    """Thin wrapper around the GW Instek LCR driver."""

    backend_name = "qcodes"
    CONFIG_COMMAND_DELAY_S = 0.2
    CONFIG_VERIFY_RETRIES = 3
    CONFIG_VERIFY_DELAY_S = 0.15
    POST_CONFIG_SETTLE_S = 0.2
    OVERLOAD_RESISTANCE_OHM = 9.9e19

    def __init__(self, address: str, timeout_ms: int) -> None:
        from probe_station_gui.gwinstek_lcr_76200 import (
            GWInstekLCR76200,
        )

        normalized_address = normalize_resource_name(address)
        try:
            self._instrument = GWInstekLCR76200(
                name="gwinstek_lcr76200",
                address=normalized_address,
                timeout=max(0.1, timeout_ms / 1000.0),
            )
        except ImportError as exc:
            raise LCRMeterError(
                "QCoDeS LCR driver is unavailable. Install optional dependencies "
                "with `pip install .[lcr]`."
            ) from exc
        except Exception as exc:  # pragma: no cover - backend specific failures
            raise LCRMeterError(
                f"Unable to open LCR resource {normalized_address}: {exc}"
            ) from exc

    def identify(self) -> str:
        try:
            idn = self._instrument.get_idn()
        except Exception as exc:  # pragma: no cover - backend specific failures
            raise LCRMeterError(f"LCR identify query failed: {exc}") from exc
        parts = (
            idn.get("model"),
            idn.get("serial"),
            idn.get("firmware"),
            idn.get("vendor"),
        )
        return ", ".join(part for part in parts if part)

    def configure_measurement(
        self,
        *,
        measurement_function: str,
        range_mode: str,
        impedance_range: int,
        dcr_range: int,
        frequency_hz: float,
        level_mode: str,
        voltage_level_v: float,
        current_level_a: float,
        source_resistance_ohm: int,
        aperture_rate: str,
        aperture_averages: int,
        trigger_source: str,
        trigger_delay_s: float,
        bias_enabled: bool,
        bias_level_v: float,
        monitor1: str,
        monitor2: str,
        alc_enabled: bool,
        **_ignored: object,
    ) -> None:
        try:
            measurement_function = str(measurement_function).strip() or "DCR"
            range_mode = str(range_mode).strip().upper() or "HOLD"
            dcr_mode = measurement_function.upper() == "DCR"
            configuration_steps = [
                (f"FUNC {measurement_function}", "FUNC?", measurement_function),
                (f"TRIG:SOUR {trigger_source}", "TRIG:SOUR?", trigger_source),
                (f"FUNC:RANG:AUTO {range_mode}", "FUNC:RANG:AUTO?", range_mode),
                (f"APER {aperture_rate}", "APER:RATE?", aperture_rate),
                (f"APER {int(aperture_averages)}", "APER:AVG?", str(int(aperture_averages))),
                (f"TRIG:DLY {float(trigger_delay_s)}", "TRIG:DLY?", str(float(trigger_delay_s))),
            ]
            if range_mode == "HOLD":
                if dcr_mode:
                    configuration_steps.append(
                        (
                            f"FUNC:DCR:RANG {int(dcr_range)}",
                            "FUNC:DCR:RANG?",
                            str(int(dcr_range)),
                        )
                    )
                else:
                    configuration_steps.append(
                        (
                            f"FUNC:IMP:RANG {int(impedance_range)}",
                            "FUNC:IMP:RANG?",
                            str(int(impedance_range)),
                        )
                    )
            if dcr_mode:
                configuration_steps.append(
                    ("BIAS OFF", "BIAS?", "OFF")
                )
            else:
                configuration_steps.extend(
                    (
                        (f"FREQ {float(frequency_hz)}", "FREQ?", str(float(frequency_hz))),
                        (
                            f"LEV:SRES {int(source_resistance_ohm)}",
                            "LEV:SRES?",
                            str(int(source_resistance_ohm)),
                        ),
                        (f"FUNC:MON1 {monitor1}", "FUNC:MON1?", monitor1),
                        (f"FUNC:MON2 {monitor2}", "FUNC:MON2?", monitor2),
                        (
                            f"LEV:ALC {'ON' if alc_enabled else 'OFF'}",
                            "LEV:ALC?",
                            "ON" if alc_enabled else "OFF",
                        ),
                    )
                )
                if level_mode.upper() == "CURRENT":
                    configuration_steps.append(
                        (
                            f"LEV:CURR {format_source_level_value(current_level_a)}",
                            "LEV:CURR?",
                            str(float(current_level_a)),
                        )
                    )
                else:
                    configuration_steps.append(
                        (
                            f"LEV:VOLT {format_source_level_value(voltage_level_v)}",
                            "LEV:VOLT?",
                            str(float(voltage_level_v)),
                        )
                    )
                if bias_enabled:
                    configuration_steps.append(
                        (
                            f"BIAS {float(bias_level_v)}",
                            "BIAS?",
                            str(float(bias_level_v)),
                        )
                    )
                else:
                    configuration_steps.append(
                        ("BIAS OFF", "BIAS?", "OFF")
                    )
            for command, query, expected in configuration_steps:
                self._write_and_verify(command, query, expected)
            logger.info(
                "Configured LCR: function=%s range_mode=%s impedance_range=%s dcr_range=%s frequency=%s level_mode=%s voltage=%s current=%s aperture=%s avg=%s trigger=%s",
                measurement_function,
                range_mode,
                int(impedance_range),
                int(dcr_range),
                float(frequency_hz),
                level_mode,
                float(voltage_level_v),
                float(current_level_a),
                aperture_rate,
                int(aperture_averages),
                trigger_source,
            )
            time.sleep(self.POST_CONFIG_SETTLE_S)
        except Exception as exc:  # pragma: no cover - backend specific failures
            raise LCRMeterError(f"Unable to configure LCR measurement: {exc}") from exc

    def configure_for_resistance(
        self, dcr_range: int, auto_range_enabled: bool
    ) -> None:
        try:
            configuration_steps = [
                ("FUNC DCR", "FUNC?", "DCR"),
                ("TRIG:SOUR INT", "TRIG:SOUR?", "INT"),
                ("BIAS OFF", "BIAS?", "OFF"),
            ]
            if auto_range_enabled:
                configuration_steps.append(
                    ("FUNC:RANG:AUTO AUTO", "FUNC:RANG:AUTO?", "AUTO")
                )
            else:
                configuration_steps.extend(
                    (
                        ("FUNC:RANG:AUTO HOLD", "FUNC:RANG:AUTO?", "HOLD"),
                        (
                            f"FUNC:DCR:RANG {int(dcr_range)}",
                            "FUNC:DCR:RANG?",
                            str(int(dcr_range)),
                        ),
                    )
                )
            configuration_steps.append(("APER FAST", "APER?", "FAST"))
            for command, query, expected in configuration_steps:
                self._write_and_verify(command, query, expected)
            logger.info(
                "Configured LCR for DCR measurement: auto_range=%s range=%s aperture=FAST trigger=INT",
                auto_range_enabled,
                int(dcr_range),
            )
            time.sleep(self.POST_CONFIG_SETTLE_S)
        except Exception as exc:  # pragma: no cover - backend specific failures
            raise LCRMeterError(f"Unable to configure resistance mode: {exc}") from exc

    def _write_and_verify(self, command: str, query: str, expected_token: str) -> None:
        last_response = ""
        normalized_expected = expected_token.strip().upper()
        for attempt in range(self.CONFIG_VERIFY_RETRIES):
            self._instrument.write(command)
            time.sleep(self.CONFIG_COMMAND_DELAY_S)
            response = str(self._instrument.ask(query)).strip()
            normalized_response = response.split(",", 1)[0].strip().upper()
            if self._configuration_response_matches(
                normalized_response, normalized_expected
            ):
                return
            last_response = response
            logger.warning(
                "LCR config verification mismatch after %s: expected %s from %s, got %s (attempt %s/%s)",
                command,
                expected_token,
                query,
                response,
                attempt + 1,
                self.CONFIG_VERIFY_RETRIES,
            )
            time.sleep(self.CONFIG_VERIFY_DELAY_S)
        raise LCRMeterError(
            f"LCR rejected configuration command {command!r}: "
            f"{query} returned {last_response!r}, expected {expected_token!r}"
        )

    def set_trigger_source(self, trigger_source: str) -> None:
        source = str(trigger_source).strip().upper() or "INT"
        self._write_and_verify(f"TRIG:SOUR {source}", "TRIG:SOUR?", source)

    @staticmethod
    def _configuration_response_matches(response: str, expected: str) -> bool:
        if response == expected:
            return True
        if response in {"ON", "1"} and expected in {"ON", "1"}:
            return True
        if response in {"OFF", "0"} and expected in {"OFF", "0"}:
            return True
        try:
            return math.isclose(
                _LCRSession._parse_numeric_response(response),
                float(expected),
                rel_tol=1e-6,
                abs_tol=1e-9,
            )
        except ValueError:
            return False

    @staticmethod
    def _parse_numeric_response(response: str) -> float:
        value = response.strip().upper()
        for suffix in ("OHM", "MS", "S", "V", "A"):
            if value.endswith(suffix):
                value = value[: -len(suffix)]
                break
        return float(value.strip())

    def read_primary_value(self, *, trigger: bool = False) -> float:
        try:
            if trigger:
                reading = self._instrument.trigger_fetch()
            else:
                reading = self._instrument.fetch_main()
            primary_value = reading.primary
            if primary_value is None:
                raise ValueError("LCR read did not return a primary value")
            primary_value = float(primary_value)
        except Exception as exc:  # pragma: no cover - backend specific failures
            raise LCRMeterError(f"LCR fetch failed: {exc}") from exc
        if (
            not math.isfinite(primary_value)
            or abs(primary_value) >= self.OVERLOAD_RESISTANCE_OHM
        ):
            return math.inf
        return primary_value

    def read_resistance_ohm(self) -> float:
        return self.read_primary_value()

    def close(self) -> None:
        self._instrument.close()


class _Keithley2400With2182ASession:
    """Direct PyVISA driver for the two-Keithley four-wire route setup."""

    backend_name = "pyvisa-keithley"
    OVERLOAD_RESISTANCE_OHM = 9.9e19

    def __init__(
        self,
        source_address: str,
        voltmeter_address: str,
        timeout_ms: int,
    ) -> None:
        try:
            import pyvisa
        except ImportError as exc:
            raise LCRMeterError(
                "Keithley route measurements require optional dependency "
                "'pyvisa'. Install with `pip install .[lcr]`."
            ) from exc

        self._resource_manager = pyvisa.ResourceManager()
        self._source = None
        self._voltmeter = None
        self._measurement_voltage_v = 0.03
        self._trigger_delay_s = 0.01
        self._compliance_current_a = 500e-6
        try:
            self._source = self._open_resource(source_address, timeout_ms)
            self._voltmeter = self._open_resource(voltmeter_address, timeout_ms)
        except Exception as exc:  # pragma: no cover - backend specific failures
            self.close()
            raise LCRMeterError(
                "Unable to open Keithley resources "
                f"{source_address!r}, {voltmeter_address!r}: {exc}"
            ) from exc

    def _open_resource(self, address: str, timeout_ms: int):
        normalized_address = normalize_resource_name(address)
        handle = self._resource_manager.open_resource(normalized_address)
        handle.timeout = int(timeout_ms)
        for attribute, value in (
            ("read_termination", "\n"),
            ("write_termination", "\n"),
        ):
            try:
                setattr(handle, attribute, value)
            except Exception:
                logger.debug(
                    "Keithley VISA handle does not accept %s=%r",
                    attribute,
                    value,
                    exc_info=True,
                )
        return handle

    def identify(self) -> str:
        source_id = self._safe_query(self._source, "*IDN?")
        voltmeter_id = self._safe_query(self._voltmeter, "*IDN?")
        parts = []
        if source_id:
            parts.append(f"2400 {source_id}")
        if voltmeter_id:
            parts.append(f"2182A {voltmeter_id}")
        return "; ".join(parts)

    def configure_measurement(
        self,
        *,
        keithley_measurement_voltage_v: float = 0.03,
        keithley_source_voltage_range_v: float = 0.21,
        keithley_compliance_current_a: float = 500e-6,
        keithley_nplc: float = 10.0,
        keithley_terminals: str = "rear",
        keithley_trigger_delay_s: float = 0.01,
        **_ignored: object,
    ) -> None:
        try:
            measurement_voltage_v = abs(float(keithley_measurement_voltage_v))
            if not math.isfinite(measurement_voltage_v) or measurement_voltage_v <= 0:
                measurement_voltage_v = 0.03
            source_voltage_range_v = abs(float(keithley_source_voltage_range_v))
            if (
                not math.isfinite(source_voltage_range_v)
                or source_voltage_range_v < measurement_voltage_v
            ):
                source_voltage_range_v = max(0.21, measurement_voltage_v)
            compliance_current_a = abs(float(keithley_compliance_current_a))
            if not math.isfinite(compliance_current_a) or compliance_current_a <= 0:
                compliance_current_a = 500e-6
            nplc = float(keithley_nplc)
            if not math.isfinite(nplc):
                nplc = 10.0
            nplc = max(0.01, min(50.0, nplc))
            terminal_value = str(keithley_terminals).strip().lower()
            terminal_scpi = "FRON" if terminal_value == "front" else "REAR"
            trigger_delay_s = max(0.0, float(keithley_trigger_delay_s))
            if not math.isfinite(trigger_delay_s):
                trigger_delay_s = 0.01

            source = self._require_source()
            voltmeter = self._require_voltmeter()
            self._measurement_voltage_v = measurement_voltage_v
            self._trigger_delay_s = trigger_delay_s
            self._compliance_current_a = compliance_current_a

            self._write(source, "*CLS")
            self._write(voltmeter, "*CLS")
            self._write(voltmeter, "CONF:VOLT")
            self._write(voltmeter, "SENS:CHAN 1")
            self._try_write(voltmeter, f"SENS:VOLT:NPLC {nplc:.12g}")
            self._try_write(voltmeter, "SENS:VOLT:DFIL:STAT ON")
            self._try_write(voltmeter, "SENS:VOLT:DFIL:COUNT 1")
            self._try_write(voltmeter, "TRIG:SOUR IMM")
            self._try_write(voltmeter, "TRIG:COUN 1")

            self._try_write(source, f":ROUT:TERM {terminal_scpi}")
            self._try_write(source, ":SENS:RES:MODE MAN")
            self._write(source, ":SOUR:FUNC VOLT")
            self._try_write(source, ":SOUR:VOLT:MODE FIX")
            self._write(source, f":SOUR:VOLT:RANG {source_voltage_range_v:.12g}")
            self._write(source, f":SENS:CURR:PROT {compliance_current_a:.12g}")
            self._try_write(source, f":SENS:VOLT:NPLC {nplc:.12g}")
            self._try_write(source, f":SENS:CURR:NPLC {nplc:.12g}")
            self._try_write(source, ":FORM:ELEM VOLT,CURR")
            self._write(source, ":SOUR:VOLT 0")
            self._write(source, ":OUTP ON")
            logger.info(
                "Configured Keithley route meter: source=%s voltmeter=%s bias=+-%s V range=%s V compliance=%s A nplc=%s terminals=%s",
                self._safe_query(source, "*IDN?") or "unknown",
                self._safe_query(voltmeter, "*IDN?") or "unknown",
                measurement_voltage_v,
                source_voltage_range_v,
                compliance_current_a,
                nplc,
                terminal_value or "rear",
            )
        except Exception as exc:  # pragma: no cover - backend specific failures
            raise LCRMeterError(
                f"Unable to configure Keithley route measurement: {exc}"
            ) from exc

    def read_primary_value(self, *, trigger: bool = False) -> float:
        source = self._require_source()
        voltmeter = self._require_voltmeter()
        measured_voltage: list[float] = []
        measured_current: list[float] = []
        try:
            for voltage in (-self._measurement_voltage_v, self._measurement_voltage_v):
                self._write(source, f":SOUR:VOLT {voltage:.12g}")
                self._write(source, "INIT")
                if self._trigger_delay_s > 0:
                    time.sleep(self._trigger_delay_s)
                self._write(voltmeter, "INIT")
                measured_voltage.append(float(self._ask(voltmeter, "FETC?")))
                source_values = self._parse_source_fetch(self._ask(source, "FETC?"))
                measured_current.append(source_values[1])
            resistance = self._resistance_from_two_points(
                measured_voltage,
                measured_current,
            )
            if self._compliance_tripped(measured_current):
                logger.warning(
                    "Keithley current compliance was reached during route measurement"
                )
        except Exception as exc:  # pragma: no cover - backend specific failures
            raise LCRMeterError(f"Keithley fetch failed: {exc}") from exc
        finally:
            self._try_write(source, ":SOUR:VOLT 0")
        if (
            not math.isfinite(resistance)
            or abs(resistance) >= self.OVERLOAD_RESISTANCE_OHM
        ):
            return math.inf
        return resistance

    def read_resistance_ohm(self) -> float:
        return self.read_primary_value(trigger=True)

    def close(self) -> None:
        source = self._source
        voltmeter = self._voltmeter
        self._source = None
        self._voltmeter = None
        if source is not None:
            self._try_write(source, ":SOUR:VOLT 0")
            self._try_write(source, ":OUTP OFF")
            self._close_handle(source)
        if voltmeter is not None:
            self._close_handle(voltmeter)
        resource_manager = getattr(self, "_resource_manager", None)
        if resource_manager is not None:
            try:
                resource_manager.close()
            except Exception:
                logger.debug("Failed to close VISA resource manager", exc_info=True)

    def _require_source(self):
        if self._source is None:
            raise LCRMeterError("Keithley 2400 source is not open.")
        return self._source

    def _require_voltmeter(self):
        if self._voltmeter is None:
            raise LCRMeterError("Keithley 2182A voltmeter is not open.")
        return self._voltmeter

    @staticmethod
    def _write(handle, command: str) -> None:
        handle.write(command)

    @staticmethod
    def _ask(handle, query: str) -> str:
        if hasattr(handle, "query"):
            return str(handle.query(query)).strip()
        return str(handle.ask(query)).strip()

    def _try_write(self, handle, command: str) -> None:
        try:
            self._write(handle, command)
        except Exception:
            logger.debug("Keithley command failed: %s", command, exc_info=True)

    def _safe_query(self, handle, query: str) -> str:
        if handle is None:
            return ""
        try:
            return self._ask(handle, query).strip()
        except Exception:
            logger.debug("Keithley query failed: %s", query, exc_info=True)
            return ""

    @staticmethod
    def _close_handle(handle) -> None:
        try:
            handle.close()
        except Exception:
            logger.debug("Failed to close VISA handle", exc_info=True)

    @staticmethod
    def _parse_source_fetch(response: str) -> tuple[float, float]:
        values = [part.strip() for part in str(response).split(",")]
        if len(values) < 2:
            raise ValueError(f"Keithley 2400 FETC? returned {response!r}")
        return float(values[0]), float(values[1])

    @staticmethod
    def _resistance_from_two_points(
        measured_voltage: list[float],
        measured_current: list[float],
    ) -> float:
        if len(measured_voltage) != 2 or len(measured_current) != 2:
            raise ValueError("Keithley resistance measurement needs two bias points.")
        delta_v = measured_voltage[1] - measured_voltage[0]
        delta_i = measured_current[1] - measured_current[0]
        if delta_i == 0:
            return math.inf
        return float(delta_v / delta_i)

    def _compliance_tripped(self, measured_current: list[float]) -> bool:
        threshold = abs(self._compliance_current_a) * 0.99
        by_current = any(abs(value) >= threshold for value in measured_current)
        trip_response = self._safe_query(self._source, ":SENS:CURR:PROT:TRIP?")
        try:
            by_trip = bool(int(float(trip_response))) if trip_response else False
        except ValueError:
            by_trip = False
        return by_current or by_trip


class RouteMeter:
    """Open, configure, and read one per-run route measurement backend."""

    def __init__(
        self,
        configuration: RouteMeterConfiguration,
        timeout_ms: int = 10000,
    ) -> None:
        self._configuration = configuration
        self._timeout_ms = int(timeout_ms)
        self._session: _LCRSession | _Keithley2400With2182ASession | None = None

    @property
    def backend_name(self) -> str:
        session = self._session
        if session is not None:
            return session.backend_name
        return ROUTE_METER_LABELS.get(
            self._configuration.meter_type, self._configuration.meter_type
        )

    def open(self) -> None:
        if self._session is not None:
            return
        if self._configuration.meter_type == ROUTE_METER_GWINSTEK:
            settings = self._configuration.gwinstek
            session = _LCRSession(settings.resource_name, self._timeout_ms)
            try:
                session.identify()
                session.configure_measurement(
                    measurement_function=settings.measurement_function,
                    range_mode=settings.range_mode,
                    impedance_range=settings.impedance_range,
                    dcr_range=settings.dcr_range,
                    frequency_hz=settings.frequency_hz,
                    level_mode=settings.level_mode,
                    voltage_level_v=settings.voltage_level_v,
                    current_level_a=settings.current_level_a,
                    source_resistance_ohm=settings.source_resistance_ohm,
                    aperture_rate=settings.aperture_rate,
                    aperture_averages=settings.aperture_averages,
                    trigger_source="BUS",
                    trigger_delay_s=settings.trigger_delay_s,
                    bias_enabled=settings.bias_enabled,
                    bias_level_v=settings.bias_level_v,
                    monitor1=settings.monitor1,
                    monitor2=settings.monitor2,
                    alc_enabled=settings.alc_enabled,
                )
            except Exception:
                session.close()
                raise
            self._session = session
            return
        if self._configuration.meter_type == ROUTE_METER_KEITHLEY:
            settings = self._configuration.keithley
            session = _Keithley2400With2182ASession(
                settings.source_resource,
                settings.voltmeter_resource,
                self._timeout_ms,
            )
            try:
                session.identify()
                session.configure_measurement(
                    keithley_measurement_voltage_v=settings.measurement_voltage_v,
                    keithley_source_voltage_range_v=settings.source_voltage_range_v,
                    keithley_compliance_current_a=settings.compliance_current_a,
                    keithley_nplc=settings.nplc,
                    keithley_terminals=settings.terminals,
                    keithley_trigger_delay_s=settings.trigger_delay_s,
                )
            except Exception:
                session.close()
                raise
            self._session = session
            return
        raise LCRMeterError(
            f"Unsupported route measurement meter: {self._configuration.meter_type}"
        )

    def read_primary_value_now(self, *, restart_polling: bool = False) -> float:
        if self._session is None:
            self.open()
        if self._session is None:
            raise LCRMeterError("Route measurement meter is not open.")
        return self._session.read_primary_value(trigger=True)

    def close(self) -> None:
        session = self._session
        self._session = None
        if session is not None:
            session.close()


class LCRMeterController(QObject):
    """Manage connection and polling for the external LCR meter."""

    connection_changed: Signal = Signal(bool, str, str)
    reading_updated: Signal = Signal(float, bool)
    status_message: Signal = Signal(str)

    DEFAULT_TIMEOUT_MS = 10000

    def __init__(self) -> None:
        super().__init__()
        self._resource_name = ""
        self._connected_resource_name = ""
        self._measurement_function = "DCR"
        self._range_mode = "HOLD"
        self._auto_range_enabled = False
        self._impedance_range = 3
        self._dcr_range = 3
        self._frequency_hz = 1000.0
        self._level_mode = "VOLTAGE"
        self._voltage_level_v = 0.01
        self._current_level_a = 0.0001
        self._source_resistance_ohm = 30
        self._aperture_rate = "FAST"
        self._aperture_averages = 1
        self._trigger_source = "INT"
        self._trigger_delay_s = 0.0
        self._bias_enabled = False
        self._bias_level_v = 0.0
        self._monitor1 = "OFF"
        self._monitor2 = "OFF"
        self._alc_enabled = False
        self._short_threshold_ohm = 10.0
        self._poll_interval_ms = 250
        self._session: Optional[_LCRSession] = None
        self._task_lock = threading.Lock()
        self._active_thread: Optional[threading.Thread] = None
        self._poll_thread: Optional[threading.Thread] = None
        self._stop_polling = threading.Event()

    def apply_configuration(
        self,
        *,
        resource_name: str,
        measurement_function: str,
        range_mode: str,
        auto_range_enabled: bool,
        impedance_range: int,
        dcr_range: int,
        frequency_hz: float,
        level_mode: str,
        voltage_level_v: float,
        current_level_a: float,
        source_resistance_ohm: int,
        aperture_rate: str,
        aperture_averages: int,
        trigger_source: str,
        trigger_delay_s: float,
        bias_enabled: bool,
        bias_level_v: float,
        monitor1: str,
        monitor2: str,
        alc_enabled: bool,
        short_threshold_ohm: float,
        poll_interval_ms: int,
    ) -> None:
        """Store the runtime configuration used by future connections."""

        self._resource_name = resource_name.strip()
        self._measurement_function = str(measurement_function).strip() or "DCR"
        self._range_mode = str(range_mode).strip().upper() or "HOLD"
        self._auto_range_enabled = bool(auto_range_enabled)
        self._impedance_range = max(0, min(8, int(impedance_range)))
        self._dcr_range = max(0, min(8, int(dcr_range)))
        self._frequency_hz = max(10.0, float(frequency_hz))
        self._level_mode = str(level_mode).strip().upper() or "VOLTAGE"
        self._voltage_level_v = max(0.0, float(voltage_level_v))
        self._current_level_a = max(0.0, float(current_level_a))
        self._source_resistance_ohm = int(source_resistance_ohm)
        self._aperture_rate = str(aperture_rate).strip().upper() or "FAST"
        self._aperture_averages = max(1, min(256, int(aperture_averages)))
        self._trigger_source = str(trigger_source).strip().upper() or "INT"
        self._trigger_delay_s = max(0.0, float(trigger_delay_s))
        self._bias_enabled = bool(bias_enabled)
        self._bias_level_v = max(-2.5, min(2.5, float(bias_level_v)))
        self._monitor1 = str(monitor1).strip().upper() or "OFF"
        self._monitor2 = str(monitor2).strip().upper() or "OFF"
        self._alc_enabled = bool(alc_enabled)
        self._short_threshold_ohm = max(0.0, float(short_threshold_ohm))
        self._poll_interval_ms = max(50, int(poll_interval_ms))

    def is_connected(self) -> bool:
        """Return True when the LCR backend is connected."""

        return self._session is not None

    def is_short_reading(self, primary_value: float) -> bool:
        """Return True when a primary reading satisfies the configured short threshold."""

        return self._is_short_reading(float(primary_value))

    def read_primary_value_now(self, *, restart_polling: bool = False) -> float:
        """Synchronously run one BUS-triggered measurement and return its value."""

        with self._task_lock:
            if self._active_thread and self._active_thread.is_alive():
                raise LCRMeterError("LCR meter task already running.")
            session = self._session
        if session is None:
            raise LCRMeterError("LCR meter is not connected.")
        self._stop_polling_session()
        try:
            primary_value = session.read_primary_value(trigger=True)
        except LCRMeterError:
            self._disconnect_session()
            self.connection_changed.emit(False, "", "LCR read failed.")
            raise
        finally:
            if restart_polling and self._session is session:
                self._stop_polling.clear()
                self._start_polling_thread()
        self.reading_updated.emit(
            primary_value,
            self._is_short_reading(primary_value),
        )
        return primary_value

    def request_connect(self) -> None:
        """Open the configured LCR resource in a background thread."""

        with self._task_lock:
            if self._active_thread and self._active_thread.is_alive():
                self.status_message.emit("LCR meter task already running.")
                return
            thread = threading.Thread(target=self._run_connect, daemon=True)
            self._active_thread = thread
            thread.start()

    def request_disconnect(self) -> None:
        """Close the current LCR resource and stop polling."""

        with self._task_lock:
            if self._active_thread and self._active_thread.is_alive():
                self.status_message.emit("LCR meter task already running.")
                return
            thread = threading.Thread(target=self._run_disconnect, daemon=True)
            self._active_thread = thread
            thread.start()

    def request_reconfigure(self) -> None:
        """Apply the current configuration to an already connected meter."""

        with self._task_lock:
            if self._session is None:
                return
            if self._active_thread and self._active_thread.is_alive():
                self.status_message.emit("LCR meter task already running.")
                return
            thread = threading.Thread(target=self._run_reconfigure, daemon=True)
            self._active_thread = thread
            thread.start()

    def shutdown(self) -> None:
        """Stop background work before application exit."""

        self._stop_polling.set()
        poll_thread = self._poll_thread
        if poll_thread and poll_thread.is_alive():
            poll_thread.join(timeout=2.0)
        session = self._session
        self._session = None
        if session is not None:
            try:
                session.close()
            except Exception:  # pragma: no cover - best effort shutdown
                logger.exception("Failed to close LCR session during shutdown")

    def _run_connect(self) -> None:
        try:
            if not self._resource_name:
                raise LCRMeterError("LCR resource is empty. Set it in Settings.")
            self._disconnect_session()
            session = _LCRSession(self._resource_name, self.DEFAULT_TIMEOUT_MS)
            instrument_id = session.identify()
            logger.info(
                "Connected to LCR resource %s (%s)",
                self._resource_name,
                instrument_id or "IDN unavailable",
            )
            self._configure_session(session)
            self._session = session
            self._connected_resource_name = self._resource_name
            self._stop_polling.clear()
            self.connection_changed.emit(True, session.backend_name, self._resource_name)
            self.status_message.emit(
                f"LCR connected via {session.backend_name}; waiting for explicit triggers."
            )
        except LCRMeterError as exc:
            self._disconnect_session()
            self.connection_changed.emit(False, "", str(exc))
            self.status_message.emit(f"LCR connection failed: {exc}")
        finally:
            with self._task_lock:
                self._active_thread = None

    def _run_reconfigure(self) -> None:
        try:
            session = self._session
            if session is None:
                return
            desired_resource = self._resource_name.strip()
            connected_resource = self._connected_resource_name.strip()
            if not desired_resource:
                raise LCRMeterError("LCR resource is empty. Set it in Settings.")
            if normalize_resource_name(desired_resource) != normalize_resource_name(
                connected_resource
            ):
                self.status_message.emit("LCR resource changed. Reconnecting meter.")
                self._disconnect_session()
                replacement = _LCRSession(desired_resource, self.DEFAULT_TIMEOUT_MS)
                instrument_id = replacement.identify()
                logger.info(
                    "Reconnected to LCR resource %s (%s)",
                    desired_resource,
                    instrument_id or "IDN unavailable",
                )
                session = replacement
                self._session = session
                self._connected_resource_name = desired_resource
                self.connection_changed.emit(True, session.backend_name, desired_resource)
            else:
                self._stop_polling_session()
            self._configure_active_session(session)
            self._stop_polling.clear()
            self.status_message.emit("LCR settings applied; waiting for explicit triggers.")
        except LCRMeterError as exc:
            self._disconnect_session()
            self.connection_changed.emit(False, "", str(exc))
            self.status_message.emit(f"LCR reconfiguration failed: {exc}")
        finally:
            with self._task_lock:
                self._active_thread = None

    def _run_disconnect(self) -> None:
        try:
            self._disconnect_session()
            self.connection_changed.emit(False, "", "Disconnected")
            self.status_message.emit("LCR disconnected.")
        finally:
            with self._task_lock:
                self._active_thread = None

    def _configure_active_session(self, session: _LCRSession) -> None:
        self._configure_session(session)

    def _configure_session(self, session: _LCRSession) -> None:
        session.configure_measurement(
            measurement_function=self._measurement_function,
            range_mode=self._range_mode,
            impedance_range=self._impedance_range,
            dcr_range=self._dcr_range,
            frequency_hz=self._frequency_hz,
            level_mode=self._level_mode,
            voltage_level_v=self._voltage_level_v,
            current_level_a=self._current_level_a,
            source_resistance_ohm=self._source_resistance_ohm,
            aperture_rate=self._aperture_rate,
            aperture_averages=self._aperture_averages,
            trigger_source="BUS",
            trigger_delay_s=self._trigger_delay_s,
            bias_enabled=self._bias_enabled,
            bias_level_v=self._bias_level_v,
            monitor1=self._monitor1,
            monitor2=self._monitor2,
            alc_enabled=self._alc_enabled,
        )

    def _is_short_reading(self, primary_value: float) -> bool:
        return (
            self._measurement_function.upper() == "DCR"
            and math.isfinite(primary_value)
            and primary_value <= self._short_threshold_ohm
        )

    def _uses_bus_trigger(self) -> bool:
        return self._trigger_source.upper() == "BUS"

    def _stop_polling_session(self) -> None:
        self._stop_polling.set()
        poll_thread = self._poll_thread
        self._poll_thread = None
        if (
            poll_thread
            and poll_thread.is_alive()
            and poll_thread is not threading.current_thread()
        ):
            poll_thread.join(timeout=2.0)

    def _disconnect_session(self) -> None:
        self._stop_polling_session()
        session = self._session
        self._session = None
        self._connected_resource_name = ""
        if session is not None:
            try:
                session.close()
            except Exception:  # pragma: no cover - best effort cleanup
                logger.exception("Failed to close LCR session cleanly")

    def _start_polling_thread(self) -> None:
        if self._poll_thread and self._poll_thread.is_alive():
            return
        thread = threading.Thread(target=self._poll_readings, daemon=True)
        self._poll_thread = thread
        thread.start()

    def _poll_readings(self) -> None:
        while not self._stop_polling.is_set():
            session = self._session
            if session is None:
                return
            try:
                primary_value = session.read_primary_value(trigger=True)
            except LCRMeterError as exc:
                self.status_message.emit(f"LCR read failed: {exc}")
                self.connection_changed.emit(False, "", str(exc))
                self._disconnect_session()
                return
            self.reading_updated.emit(
                primary_value, self._is_short_reading(primary_value)
            )
            time.sleep(self._poll_interval_ms / 1000.0)
