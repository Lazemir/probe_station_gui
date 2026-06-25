"""Resistance-meter integrations used by calibration and route measurements."""

from __future__ import annotations

import atexit
import logging
import math
import queue
import re
import threading
import time
import weakref
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Callable, Iterator, Optional

from PySide6.QtCore import QObject, Signal

from probe_station_measure import OHMMETER_RANGE_MANUAL
from probe_station_gui.lcr_meter_helpers import (
    callable_accepts_keyword as _callable_accepts_keyword,
    normalize_visa_role as _normalize_visa_role,
    prepare_route_measurement_batch as _prepare_route_measurement_batch,
    read_route_measurement_batch as _read_route_measurement_batch,
    session_visa_resource_roles as _session_visa_resource_roles,
    voltage_sweep_point_to_dict as _voltage_sweep_point_to_dict,
)


logger = logging.getLogger(__name__)

_LCR_METER_CONTROLLERS: "weakref.WeakSet[LCRMeterController]" = weakref.WeakSet()


def _shutdown_lcr_meter_controllers() -> None:
    for controller in list(_LCR_METER_CONTROLLERS):
        try:
            controller.shutdown()
        except Exception:
            logger.exception("Failed to shut down measurement instrument controller")


atexit.register(_shutdown_lcr_meter_controllers)


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
DEFAULT_METER_TIMEOUT_MS = 10000
KEITHLEY_LIVE_MEASUREMENT_VOLTAGE_V = 0.03
KEITHLEY_LIVE_SOURCE_VOLTAGE_RANGE_V = 0.21
KEITHLEY_LIVE_VOLTMETER_RANGE_V = 1.0
KEITHLEY_LIVE_CURRENT_RANGE_A = 10e-6
KEITHLEY_LIVE_COMPLIANCE_CURRENT_A = 9.5e-6
KEITHLEY_LIVE_NPLC = 1.0
COM_RESOURCE_PATTERN = re.compile(r"^COM(?P<port>\d+)$", re.IGNORECASE)
GPIB_RESOURCE_PATTERN = re.compile(r"^GPIB(?P<board>\d*)::", re.IGNORECASE)


@dataclass
class _MeterWorkerCall:
    func: Callable[[], object]
    done: threading.Event | None = None
    result: object = None
    error: BaseException | None = None


def normalize_resource_name(resource_name: str) -> str:
    """Translate ``COM4``-style names into VISA ASRL resources."""

    candidate = (resource_name or "").strip()
    match = COM_RESOURCE_PATTERN.fullmatch(candidate)
    if match:
        return f"ASRL{int(match.group('port'))}::INSTR"
    return candidate


def _gpib_interface_resources_for(
    resources: tuple[str | None, ...],
) -> tuple[str, ...]:
    interfaces: list[str] = []
    seen: set[str] = set()
    for resource in resources:
        normalized = normalize_resource_name(resource or "")
        match = GPIB_RESOURCE_PATTERN.match(normalized)
        if match is None:
            continue
        board = match.group("board")
        interface = f"GPIB{board}::INTFC" if board else "GPIB::INTFC"
        key = interface.upper()
        if key in seen:
            continue
        seen.add(key)
        interfaces.append(interface)
    return tuple(interfaces)


def _reset_gpib_interfaces_for_resources(*resources: str | None) -> None:
    interfaces = _gpib_interface_resources_for(tuple(resources))
    if not interfaces:
        return
    try:
        import pyvisa
    except ImportError:
        logger.debug("PyVISA is unavailable; skipping GPIB interface reset.")
        return
    try:
        resource_manager = pyvisa.ResourceManager()
    except Exception as exc:  # pragma: no cover - backend specific failures
        logger.warning("Unable to create VISA resource manager for GPIB reset: %s", exc)
        return
    reset_any = False
    for interface_name in interfaces:
        try:
            interface = resource_manager.open_resource(interface_name)
        except Exception as exc:  # pragma: no cover - backend specific failures
            logger.warning("Unable to open %s for GPIB reset: %s", interface_name, exc)
            continue
        try:
            send_ifc = getattr(interface, "send_ifc", None)
            if callable(send_ifc):
                send_ifc()
                reset_any = True
                logger.info("Sent GPIB IFC on %s before Keithley connect.", interface_name)
        except Exception as exc:  # pragma: no cover - backend specific failures
            logger.warning("GPIB interface reset failed on %s: %s", interface_name, exc)
        finally:
            try:
                interface.close()
            except Exception:
                pass
    if reset_any:
        time.sleep(0.25)


def format_source_level_value(value: float) -> str:
    """Format source levels in the form accepted by the LCR-76200 firmware."""

    numeric = float(value)
    if numeric == 0.0 or abs(numeric) >= 0.1:
        return f"{numeric:.12g}"
    for scale, suffix in ((1e3, "m"), (1e6, "u"), (1e9, "n")):
        scaled = numeric * scale
        if 1.0 <= abs(scaled) < 1000.0:
            return f"{scaled:.12g}{suffix}"
    return f"{numeric:.12g}"


def _session_visa_operation(
    session: object | None,
    role: str,
    operation: str,
    *,
    command: str | None,
    timeout_ms: int | None,
    read_termination: str | None,
    write_termination: str | None,
) -> object:
    if session is None:
        raise LCRMeterError("Measurement instrument is not connected.")
    resolver = getattr(session, "visa_handle_for_role", None)
    if not callable(resolver):
        raise LCRMeterError("Measurement instrument does not expose VISA roles.")
    try:
        handle = resolver(role)
    except KeyError as exc:
        raise LCRMeterError(str(exc)) from exc

    previous: dict[str, object] = {}
    try:
        _set_temporary_visa_attribute(handle, previous, "timeout", timeout_ms)
        _set_temporary_visa_attribute(
            handle,
            previous,
            "read_termination",
            read_termination,
        )
        _set_temporary_visa_attribute(
            handle,
            previous,
            "write_termination",
            write_termination,
        )
        normalized = str(operation or "").strip().lower().replace("-", "_")
        if normalized == "write":
            if command is None:
                raise LCRMeterError("VISA write requires a command.")
            handle.write(str(command))
            return None
        if normalized in {"query", "ask"}:
            if command is None:
                raise LCRMeterError("VISA query requires a command.")
            query = getattr(handle, "query", None)
            if callable(query):
                return str(query(str(command))).strip()
            ask = getattr(handle, "ask", None)
            if callable(ask):
                return str(ask(str(command))).strip()
            raise LCRMeterError("VISA handle cannot run queries.")
        if normalized == "read":
            reader = getattr(handle, "read", None)
            if not callable(reader):
                raise LCRMeterError("VISA handle cannot read text.")
            return str(reader())
        if normalized == "read_raw":
            reader = getattr(handle, "read_raw", None)
            if callable(reader):
                data = reader()
            else:
                text_reader = getattr(handle, "read", None)
                if not callable(text_reader):
                    raise LCRMeterError("VISA handle cannot read raw bytes.")
                data = str(text_reader()).encode("utf-8")
            return bytes(data)
        if normalized == "clear":
            clearer = getattr(handle, "clear", None)
            if not callable(clearer):
                clearer = getattr(handle, "device_clear", None)
            if not callable(clearer):
                visa_handle = getattr(handle, "visa_handle", None)
                clearer = getattr(visa_handle, "clear", None)
            if callable(clearer):
                clearer()
            return None
    finally:
        for name, value in previous.items():
            try:
                setattr(handle, name, value)
            except Exception:
                logger.debug("Failed to restore VISA attribute %s", name, exc_info=True)
    raise LCRMeterError(f"Unsupported VISA operation: {operation}")


def _set_temporary_visa_attribute(
    handle: object,
    previous: dict[str, object],
    name: str,
    value: object,
) -> None:
    if value is None or not hasattr(handle, name):
        return
    try:
        previous[name] = getattr(handle, name)
        setattr(handle, name, value)
    except Exception:
        logger.debug("VISA handle does not accept %s=%r", name, value, exc_info=True)


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

    source_resource: str = "GPIB0::1::INSTR"
    voltmeter_resource: str = "GPIB0::2::INSTR"
    measurement_voltage_v: float = 0.03
    range_mode: str = OHMMETER_RANGE_MANUAL
    expected_resistance_ohm: float | None = None
    minimum_resistance_ohm: float | None = None
    maximum_current_a: float | None = None
    voltage_range_v: float | None = None
    source_voltage_range_v: float = 0.21
    voltmeter_range_v: float = 0.1
    current_range_a: float = 10e-6
    compliance_current_a: float = 10e-6
    range_voltage_headroom: float = 1.2
    range_current_headroom: float = 2.0
    nplc: float = 1.0
    terminals: str = "rear"
    trigger_delay_s: float = 0.0
    use_buffer: bool = True
    use_trigger_link: bool = True


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

    def measurement_type_label(self) -> str:
        """Return a concise CSV label for the route measurement mode."""

        if self.meter_type == ROUTE_METER_KEITHLEY:
            voltage = float(self.keithley.measurement_voltage_v)
            return f"Keithley voltage sweep +/-{voltage:g} V"
        function = str(self.gwinstek.measurement_function or "").strip()
        if function.upper() == "DCR":
            return "GW Instek DCR"
        return f"GW Instek {function or 'measurement'}"


class LCRMeterError(RuntimeError):
    """Raised when the LCR meter backend cannot complete the request."""


def _open_keithley_session(
    source_resource: str,
    voltmeter_resource: str | None,
    timeout_ms: int,
) -> object:
    try:
        from probe_station_measure import Keithley2400With2182A
    except ImportError as exc:
        raise LCRMeterError(
            "Keithley route measurements require optional dependency "
            "'probe-station-measure'. Install with `pip install .[lcr]`."
        ) from exc
    source = normalize_resource_name(source_resource)
    voltmeter = normalize_resource_name(voltmeter_resource or "")
    try:
        _reset_gpib_interfaces_for_resources(source, voltmeter)
        return Keithley2400With2182A(
            source,
            voltmeter or None,
            timeout_ms=timeout_ms,
        )
    except Exception as exc:  # pragma: no cover - backend specific failures
        raise LCRMeterError(
            "Unable to open Keithley resources "
            f"{source_resource!r}, {voltmeter_resource!r}: {exc}"
        ) from exc


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

    def visa_resource_roles(self) -> dict[str, dict[str, object]]:
        return {
            "meter.source": {
                "role": "meter.source",
                "kind": "source_meter",
                "model": "GW Instek LCR-76200",
                "required": True,
            }
        }

    def visa_handle_for_role(self, role: str):
        normalized = _normalize_visa_role(role)
        if normalized not in {"meter.source", "source", "source_meter", "meter"}:
            raise KeyError(f"Unsupported GW Instek VISA role: {role}")
        return getattr(self._instrument, "visa_handle", self._instrument)

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

    def abort_measurement(self) -> None:
        writer = getattr(self._instrument, "write", None)
        if writer is None:
            writer = getattr(self._instrument, "write_raw", None)
        if callable(writer):
            try:
                writer("ABOR")
            except Exception:
                logger.debug("GW Instek abort command failed", exc_info=True)

    def close(self) -> None:
        self._instrument.close()


class RouteMeter:
    """Open, configure, and read one per-run route measurement backend."""

    def __init__(
        self,
        configuration: RouteMeterConfiguration,
        timeout_ms: int = DEFAULT_METER_TIMEOUT_MS,
    ) -> None:
        self._configuration = configuration
        self._timeout_ms = int(timeout_ms)
        self._session: object | None = None

    @property
    def backend_name(self) -> str:
        session = self._session
        if session is not None:
            return str(getattr(session, "backend_name", session.__class__.__name__))
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
            session = _open_keithley_session(
                settings.source_resource,
                settings.voltmeter_resource,
                self._timeout_ms,
            )
            try:
                identify = getattr(session, "identify", None)
                configure = getattr(session, "configure_measurement")
                if callable(identify):
                    identify()
                configure(
                    keithley_measurement_voltage_v=settings.measurement_voltage_v,
                    keithley_range_mode=settings.range_mode,
                    keithley_expected_resistance_ohm=settings.expected_resistance_ohm,
                    keithley_minimum_resistance_ohm=settings.minimum_resistance_ohm,
                    keithley_maximum_current_a=settings.maximum_current_a,
                    keithley_voltage_range_v=settings.voltage_range_v,
                    keithley_source_voltage_range_v=settings.source_voltage_range_v,
                    keithley_voltmeter_range_v=settings.voltmeter_range_v,
                    keithley_current_range_a=settings.current_range_a,
                    keithley_compliance_current_a=settings.compliance_current_a,
                    keithley_range_voltage_headroom=settings.range_voltage_headroom,
                    keithley_range_current_headroom=settings.range_current_headroom,
                    keithley_nplc=settings.nplc,
                    keithley_terminals=settings.terminals,
                    keithley_trigger_delay_s=settings.trigger_delay_s,
                    keithley_use_buffer=settings.use_buffer,
                    keithley_use_trigger_link=settings.use_trigger_link,
                )
            except Exception:
                closer = getattr(session, "close", None)
                if callable(closer):
                    closer()
                raise
            self._session = session
            return
        raise LCRMeterError(
            f"Unsupported route measurement instrument: {self._configuration.meter_type}"
        )

    def apply_route_meter_configuration(
        self,
        configuration: RouteMeterConfiguration,
    ) -> None:
        """Apply updated route measurement settings to the per-run backend."""

        if self._session is None:
            self._configuration = configuration
            return
        if configuration.meter_type != self._configuration.meter_type:
            configured_label = ROUTE_METER_LABELS.get(
                self._configuration.meter_type,
                self._configuration.meter_type,
            )
            requested_label = ROUTE_METER_LABELS.get(
                configuration.meter_type,
                configuration.meter_type,
            )
            raise LCRMeterError(
                f"Open route instrument is {configured_label}; route requested {requested_label}."
            )
        if configuration.meter_type == ROUTE_METER_GWINSTEK:
            if not isinstance(self._session, _LCRSession):
                raise LCRMeterError("Open route instrument is not a GW Instek LCR.")
            settings = configuration.gwinstek
            self._session.configure_measurement(
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
            self._configuration = configuration
            return
        if configuration.meter_type == ROUTE_METER_KEITHLEY:
            if isinstance(self._session, _LCRSession):
                raise LCRMeterError("Open route instrument is not a Keithley pair.")
            settings = configuration.keithley
            configure = getattr(self._session, "configure_measurement", None)
            if not callable(configure):
                raise LCRMeterError("Open route instrument is not a Keithley pair.")
            configure(
                keithley_measurement_voltage_v=settings.measurement_voltage_v,
                keithley_range_mode=settings.range_mode,
                keithley_expected_resistance_ohm=settings.expected_resistance_ohm,
                keithley_minimum_resistance_ohm=settings.minimum_resistance_ohm,
                keithley_maximum_current_a=settings.maximum_current_a,
                keithley_voltage_range_v=settings.voltage_range_v,
                keithley_source_voltage_range_v=settings.source_voltage_range_v,
                keithley_voltmeter_range_v=settings.voltmeter_range_v,
                keithley_current_range_a=settings.current_range_a,
                keithley_compliance_current_a=settings.compliance_current_a,
                keithley_range_voltage_headroom=settings.range_voltage_headroom,
                keithley_range_current_headroom=settings.range_current_headroom,
                keithley_nplc=settings.nplc,
                keithley_terminals=settings.terminals,
                keithley_trigger_delay_s=settings.trigger_delay_s,
                keithley_use_buffer=settings.use_buffer,
                keithley_use_trigger_link=settings.use_trigger_link,
            )
            self._configuration = configuration
            return
        raise LCRMeterError(
            f"Unsupported route measurement instrument: {configuration.meter_type}"
        )

    def read_primary_value_now(self, *, restart_polling: bool = False) -> float:
        if self._session is None:
            self.open()
        if self._session is None:
            raise LCRMeterError("Route measurement instrument is not open.")
        reader = getattr(self._session, "read_primary_value", None)
        if not callable(reader):
            raise LCRMeterError("Route measurement instrument cannot read values.")
        return float(reader(trigger=True))

    def read_route_measurement_now(self, *, restart_polling: bool = False) -> dict[str, object]:
        _ = restart_polling
        if self._session is None:
            self.open()
        if self._session is None:
            raise LCRMeterError("Route measurement instrument is not open.")
        reader = getattr(self._session, "read_route_measurement", None)
        if callable(reader):
            return dict(reader(trigger=True))
        primary_reader = getattr(self._session, "read_primary_value", None)
        if not callable(primary_reader):
            raise LCRMeterError("Route measurement instrument cannot read values.")
        value = primary_reader(trigger=True)
        return {"differential_resistance_ohm": value}

    def read_route_measurement_batch_now(
        self,
        count: int,
        *,
        restart_polling: bool = False,
        after_measurement: object | None = None,
    ) -> list[dict[str, object]]:
        _ = restart_polling
        if self._session is None:
            self.open()
        if self._session is None:
            raise LCRMeterError("Route measurement instrument is not open.")
        count = max(1, int(count))
        batch_reader = getattr(self._session, "read_route_measurements", None)
        if callable(batch_reader):
            return [
                dict(item)
                for item in _read_route_measurement_batch(
                    batch_reader,
                    count,
                    after_measurement=after_measurement,
                )
            ]
        return [self.read_route_measurement_now() for _index in range(count)]

    def prepare_route_measurement_batch_now(
        self,
        count: int,
        *,
        source_list_count: int | None = None,
    ) -> None:
        if self._session is None:
            self.open()
        if self._session is None:
            raise LCRMeterError("Route measurement instrument is not open.")
        preparer = getattr(self._session, "prepare_route_measurements", None)
        _prepare_route_measurement_batch(
            preparer,
            max(1, int(count)),
            source_list_count=source_list_count,
        )

    @contextmanager
    def output(self, enabled: bool = True) -> Iterator["RouteMeter"]:
        if self._session is None:
            self.open()
        session = self._session
        output = getattr(session, "output", None)
        if not callable(output):
            yield self
            return
        with output(bool(enabled)):
            yield self

    def abort_current_measurement(self) -> None:
        session = self._session
        abort = getattr(session, "abort_measurement", None)
        if callable(abort):
            abort()

    def visa_resource_roles(self) -> dict[str, dict[str, object]]:
        if self._session is None:
            self.open()
        return _session_visa_resource_roles(
            self._session,
            meter_type=self._configuration.meter_type,
        )

    def visa_operation(
        self,
        role: str,
        operation: str,
        *,
        command: str | None = None,
        timeout_ms: int | None = None,
        read_termination: str | None = None,
        write_termination: str | None = None,
    ) -> object:
        if self._session is None:
            self.open()
        return _session_visa_operation(
            self._session,
            role,
            operation,
            command=command,
            timeout_ms=timeout_ms,
            read_termination=read_termination,
            write_termination=write_termination,
        )

    def close(self) -> None:
        session = self._session
        self._session = None
        if session is not None:
            closer = getattr(session, "close", None)
            if callable(closer):
                closer()


class LCRMeterController(QObject):
    """Manage connection and polling for the external measurement instrument."""

    connection_changed: Signal = Signal(bool, str, str)
    reading_started: Signal = Signal(int)
    reading_summary_updated: Signal = Signal(float, bool, int)
    reading_updated: Signal = Signal(float, bool)
    status_message: Signal = Signal(str)

    DEFAULT_TIMEOUT_MS = DEFAULT_METER_TIMEOUT_MS
    TASK_WAIT_TIMEOUT_S = 45.0

    def __init__(self) -> None:
        super().__init__()
        self._meter_type = ROUTE_METER_GWINSTEK
        self._resource_name = ""
        self._connected_resource_name = ""
        self._keithley_source_resource = ""
        self._keithley_voltmeter_resource = ""
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
        self._session: Optional[object] = None
        self._stop_polling = threading.Event()
        self._live_polling_enabled = True
        self._pending_route_meter_configuration: RouteMeterConfiguration | None = None
        self._worker_queue: queue.Queue[_MeterWorkerCall | None] = queue.Queue()
        self._worker_thread: threading.Thread | None = None
        self._worker_start_lock = threading.Lock()
        self._worker_shutdown = threading.Event()
        self._shutdown_started = False
        self._worker_state = threading.Condition()
        self._worker_pending_calls = 0
        self._worker_running_call = False
        self._live_output_context: object | None = None
        _LCR_METER_CONTROLLERS.add(self)

    def apply_configuration(
        self,
        *,
        meter_type: str = ROUTE_METER_GWINSTEK,
        resource_name: str,
        keithley_source_resource: str = "",
        keithley_voltmeter_resource: str = "",
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

        meter_type = str(meter_type).strip() or ROUTE_METER_GWINSTEK
        if meter_type not in ROUTE_METER_TYPES:
            meter_type = ROUTE_METER_GWINSTEK
        self._meter_type = meter_type
        self._resource_name = resource_name.strip()
        self._keithley_source_resource = keithley_source_resource.strip()
        self._keithley_voltmeter_resource = keithley_voltmeter_resource.strip()
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
        """Return True when the measurement backend is connected."""

        return self._session is not None

    def live_polling_enabled(self) -> bool:
        """Return True when standby resistance polling is enabled."""

        return self._live_polling_enabled

    def set_live_polling_enabled(self, enabled: bool) -> None:
        """Enable or disable standby resistance polling."""

        self._live_polling_enabled = bool(enabled)
        if not self._live_polling_enabled:
            self._pause_live_polling()
            return
        self._resume_live_polling()

    def wait_until_idle(self, timeout_s: float | None = None) -> bool:
        """Wait until the current background meter task finishes."""

        worker_thread = self._worker_thread
        if worker_thread is not None and threading.current_thread() is worker_thread:
            return True
        timeout = (
            self.TASK_WAIT_TIMEOUT_S
            if timeout_s is None
            else max(0.0, float(timeout_s))
        )
        deadline = time.monotonic() + timeout
        with self._worker_state:
            while True:
                if self._worker_pending_calls <= 0 and not self._worker_running_call:
                    return True
                remaining = deadline - time.monotonic()
                if remaining <= 0.0:
                    return False
                self._worker_state.wait(timeout=min(0.05, remaining))

    def _ensure_worker_started(self) -> None:
        with self._worker_start_lock:
            thread = self._worker_thread
            if thread is not None and thread.is_alive():
                return
            self._worker_shutdown.clear()
            thread = threading.Thread(
                target=self._meter_worker_loop,
                name="LCRMeterWorker",
                daemon=True,
            )
            self._worker_thread = thread
            thread.start()

    def _wake_meter_worker(self) -> None:
        self._ensure_worker_started()
        self._worker_queue.put(None)

    def _run_on_meter_worker(self, func: Callable[[], object]) -> object:
        worker_thread = self._worker_thread
        if worker_thread is not None and threading.current_thread() is worker_thread:
            return func()
        done = threading.Event()
        call = _MeterWorkerCall(func=func, done=done)
        with self._worker_state:
            self._worker_pending_calls += 1
        self._worker_queue.put(call)
        self._ensure_worker_started()
        done.wait()
        if call.error is not None:
            raise call.error
        return call.result

    def _submit_meter_worker_call(self, func: Callable[[], object]) -> bool:
        with self._worker_state:
            if self._worker_pending_calls > 0 or self._worker_running_call:
                return False
            self._worker_pending_calls += 1
        self._worker_queue.put(_MeterWorkerCall(func=func))
        self._ensure_worker_started()
        return True

    def _meter_worker_loop(self) -> None:
        while not self._worker_shutdown.is_set():
            timeout = self._meter_worker_poll_timeout()
            try:
                call = self._worker_queue.get(timeout=timeout)
            except queue.Empty:
                with self._worker_state:
                    self._worker_running_call = True
                try:
                    self._run_meter_poll_once()
                finally:
                    with self._worker_state:
                        self._worker_running_call = False
                        self._worker_state.notify_all()
                continue
            if call is None:
                continue
            with self._worker_state:
                self._worker_running_call = True
            try:
                call.result = call.func()
            except BaseException as exc:
                call.error = exc
                if call.done is None:
                    logger.exception("Measurement instrument worker task failed.")
            finally:
                if call.done is not None:
                    call.done.set()
                with self._worker_state:
                    self._worker_running_call = False
                    self._worker_pending_calls = max(0, self._worker_pending_calls - 1)
                    self._worker_state.notify_all()

    def _meter_worker_poll_timeout(self) -> float | None:
        if (
            not self._live_polling_enabled
            or self._stop_polling.is_set()
            or self._session is None
        ):
            return None
        return max(0.05, self._poll_interval_ms / 1000.0)

    def meter_type(self) -> str:
        """Return the currently configured meter type."""

        return self._meter_type

    def connection_label(self) -> str:
        """Return a human readable connection target for the configured meter."""

        if self._meter_type == ROUTE_METER_KEITHLEY:
            source = self._keithley_source_resource or "source not configured"
            if self._keithley_voltmeter_resource:
                return (
                    f"Keithley 2400 {source}; "
                    f"2182A {self._keithley_voltmeter_resource}"
                )
            return f"Keithley 2400 {source}"
        return self._resource_name

    def apply_route_meter_configuration(
        self,
        configuration: RouteMeterConfiguration,
    ) -> None:
        """Apply per-run measurement settings to the connected meter."""

        if configuration.meter_type != self._meter_type:
            configured_label = ROUTE_METER_LABELS.get(
                self._meter_type, self._meter_type
            )
            requested_label = ROUTE_METER_LABELS.get(
                configuration.meter_type, configuration.meter_type
            )
            raise LCRMeterError(
                f"Connected instrument is {configured_label}; route requested {requested_label}."
            )
        self._pending_route_meter_configuration = configuration
        self._run_on_meter_worker(
            lambda: self._apply_route_meter_configuration_on_worker(configuration)
        )

    def _apply_route_meter_configuration_on_worker(
        self,
        configuration: RouteMeterConfiguration,
    ) -> None:
        session = self._session
        if session is None:
            raise LCRMeterError("Measurement instrument is not connected.")
        self._stop_polling_session()
        try:
            if session is not self._session:
                raise LCRMeterError("Measurement instrument is not connected.")
            self._apply_route_meter_configuration_to_session(
                session,
                configuration,
            )
        except Exception as exc:
            failure: BaseException = exc
            if (
                configuration.meter_type == ROUTE_METER_KEITHLEY
                and bool(self._connection_key())
            ):
                try:
                    self.status_message.emit(
                        "Instrument setup failed; reconnecting Keithley."
                    )
                    self._disconnect_session()
                    self._connect_configured_session(resume_polling=False)
                    replacement = self._session
                    if replacement is None:
                        raise LCRMeterError("Measurement instrument is not connected.")
                    self._apply_route_meter_configuration_to_session(
                        replacement,
                        configuration,
                    )
                    return
                except Exception as retry_exc:
                    failure = retry_exc
            error = self._lcr_error(failure)
            self._disconnect_session()
            self.connection_changed.emit(False, "", str(error))
            self.status_message.emit(f"Instrument setup failed: {error}")
            raise error from failure

    def _apply_route_meter_configuration_to_session(
        self,
        session: object,
        configuration: RouteMeterConfiguration,
    ) -> None:
        if configuration.meter_type == ROUTE_METER_GWINSTEK:
            if not isinstance(session, _LCRSession):
                raise LCRMeterError("Connected instrument is not a GW Instek LCR.")
            settings = configuration.gwinstek
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
            return
        if configuration.meter_type == ROUTE_METER_KEITHLEY:
            if isinstance(session, _LCRSession):
                raise LCRMeterError("Connected instrument is not a Keithley pair.")
            settings = configuration.keithley
            configure = getattr(session, "configure_measurement", None)
            if not callable(configure):
                raise LCRMeterError("Connected instrument is not a Keithley pair.")
            configure(
                keithley_measurement_voltage_v=settings.measurement_voltage_v,
                keithley_range_mode=settings.range_mode,
                keithley_expected_resistance_ohm=settings.expected_resistance_ohm,
                keithley_minimum_resistance_ohm=settings.minimum_resistance_ohm,
                keithley_maximum_current_a=settings.maximum_current_a,
                keithley_voltage_range_v=settings.voltage_range_v,
                keithley_source_voltage_range_v=settings.source_voltage_range_v,
                keithley_voltmeter_range_v=settings.voltmeter_range_v,
                keithley_current_range_a=settings.current_range_a,
                keithley_compliance_current_a=settings.compliance_current_a,
                keithley_range_voltage_headroom=settings.range_voltage_headroom,
                keithley_range_current_headroom=settings.range_current_headroom,
                keithley_nplc=settings.nplc,
                keithley_terminals=settings.terminals,
                keithley_trigger_delay_s=settings.trigger_delay_s,
                keithley_use_buffer=settings.use_buffer,
                keithley_use_trigger_link=settings.use_trigger_link,
            )
            return
        raise LCRMeterError(
            f"Unsupported route measurement instrument: {configuration.meter_type}"
        )

    def apply_route_meter_runtime_configuration(
        self,
        configuration: RouteMeterConfiguration,
    ) -> None:
        """Store route-meter connection settings for a future connection."""

        self._pending_route_meter_configuration = configuration
        if configuration.meter_type == ROUTE_METER_GWINSTEK:
            settings = configuration.gwinstek
            self._meter_type = ROUTE_METER_GWINSTEK
            self._resource_name = str(settings.resource_name).strip()
            self._keithley_source_resource = ""
            self._keithley_voltmeter_resource = ""
            self._measurement_function = (
                str(settings.measurement_function).strip() or "DCR"
            )
            self._range_mode = str(settings.range_mode).strip().upper() or "HOLD"
            self._auto_range_enabled = self._range_mode == "AUTO"
            self._impedance_range = max(0, min(8, int(settings.impedance_range)))
            self._dcr_range = max(0, min(8, int(settings.dcr_range)))
            self._frequency_hz = max(10.0, float(settings.frequency_hz))
            self._level_mode = str(settings.level_mode).strip().upper() or "VOLTAGE"
            self._voltage_level_v = max(0.0, float(settings.voltage_level_v))
            self._current_level_a = max(0.0, float(settings.current_level_a))
            self._source_resistance_ohm = int(settings.source_resistance_ohm)
            self._aperture_rate = (
                str(settings.aperture_rate).strip().upper() or "FAST"
            )
            self._aperture_averages = max(
                1, min(256, int(settings.aperture_averages))
            )
            self._trigger_source = "BUS"
            self._trigger_delay_s = max(0.0, float(settings.trigger_delay_s))
            self._bias_enabled = bool(settings.bias_enabled)
            self._bias_level_v = max(-2.5, min(2.5, float(settings.bias_level_v)))
            self._monitor1 = str(settings.monitor1).strip().upper() or "OFF"
            self._monitor2 = str(settings.monitor2).strip().upper() or "OFF"
            self._alc_enabled = bool(settings.alc_enabled)
            return
        if configuration.meter_type == ROUTE_METER_KEITHLEY:
            settings = configuration.keithley
            self._meter_type = ROUTE_METER_KEITHLEY
            self._resource_name = ""
            self._keithley_source_resource = str(settings.source_resource).strip()
            self._keithley_voltmeter_resource = str(
                settings.voltmeter_resource
            ).strip()
            self._measurement_function = "DCR"
            self._range_mode = str(settings.range_mode).strip().upper() or "AUTO"
            self._auto_range_enabled = self._range_mode == "AUTO"
            return

    def open(self) -> None:
        """Open the configured instrument for route-runner compatibility."""

        self.connect_now()
        configuration = self._pending_route_meter_configuration
        if configuration is not None:
            self.apply_route_meter_configuration(configuration)

    def is_short_reading(self, primary_value: float) -> bool:
        """Return True when a primary reading satisfies the configured short threshold."""

        return self._is_short_reading(float(primary_value))

    def short_threshold_ohm(self) -> float:
        """Return the configured short-circuit resistance threshold."""

        return self._short_threshold_ohm

    def read_primary_value_now(self, *, restart_polling: bool = False) -> float:
        """Synchronously run one BUS-triggered measurement and return its value."""

        measurement = self.read_route_measurement_now(restart_polling=restart_polling)
        return float(measurement["differential_resistance_ohm"])

    def read_route_measurement_now(
        self,
        *,
        restart_polling: bool = False,
    ) -> dict[str, object]:
        """Synchronously run one route measurement and return raw readings."""

        return dict(
            self._run_on_meter_worker(
                lambda: self._read_route_measurement_now_on_worker(restart_polling)
            )
        )

    def _read_route_measurement_now_on_worker(
        self,
        restart_polling: bool,
    ) -> dict[str, object]:
        session = self._session
        if session is None:
            raise LCRMeterError("Measurement instrument is not connected.")
        self._stop_polling_session()
        self.reading_started.emit(1)
        try:
            measurement = self._read_route_measurement_from_session(session)
        except Exception as exc:
            error = self._lcr_error(exc)
            self._disconnect_session()
            self.connection_changed.emit(False, "", str(error))
            self.status_message.emit(f"Instrument read failed: {error}")
            raise error from exc
        finally:
            if restart_polling and self._session is session:
                self._stop_polling.clear()
                self._start_polling_thread()
        primary_value = float(measurement["differential_resistance_ohm"])
        self._emit_reading_summary(primary_value, 1)
        return measurement

    def read_voltage_sweep_now(
        self,
        voltages_v: list[float] | tuple[float, ...],
        *,
        restart_polling: bool = False,
    ) -> dict[str, object]:
        """Synchronously run a raw source-voltage sweep and return V/I points."""

        return dict(
            self._run_on_meter_worker(
                lambda: self._read_voltage_sweep_now_on_worker(
                    voltages_v,
                    restart_polling,
                )
            )
        )

    def _read_voltage_sweep_now_on_worker(
        self,
        voltages_v: list[float] | tuple[float, ...],
        restart_polling: bool,
    ) -> dict[str, object]:
        session = self._session
        if session is None:
            raise LCRMeterError("Measurement instrument is not connected.")
        voltage_list_reader = getattr(session, "measure_voltage_list", None)
        if not callable(voltage_list_reader):
            raise LCRMeterError(
                "Raw voltage sweeps require a Keithley 2400 + 2182A instrument."
            )
        self._stop_polling_session()
        self.reading_started.emit(max(1, len(voltages_v)))
        try:
            points = [
                _voltage_sweep_point_to_dict(point)
                for point in voltage_list_reader(voltages_v)
            ]
            measurement = {
                "source_voltages_v": [float(value) for value in voltages_v],
                "points": points,
            }
        except Exception as exc:
            error = self._lcr_error(exc)
            self._disconnect_session()
            self.connection_changed.emit(False, "", str(error))
            self.status_message.emit(f"Instrument read failed: {error}")
            raise error from exc
        finally:
            if restart_polling and self._session is session:
                self._stop_polling.clear()
                self._start_polling_thread()
        primary_value = self._mean_resistance_from_measurements(points)
        self._emit_reading_summary(primary_value, len(points))
        return measurement

    def read_route_measurement_batch_now(
        self,
        count: int,
        *,
        restart_polling: bool = False,
        after_measurement: object | None = None,
    ) -> list[dict[str, object]]:
        """Synchronously run a batch of route measurements when supported."""

        count = max(1, int(count))
        return list(
            self._run_on_meter_worker(
                lambda: self._read_route_measurement_batch_now_on_worker(
                    count,
                    restart_polling,
                    after_measurement,
                )
            )
        )

    def _read_route_measurement_batch_now_on_worker(
        self,
        count: int,
        restart_polling: bool,
        after_measurement: object | None,
    ) -> list[dict[str, object]]:
        session = self._session
        if session is None:
            raise LCRMeterError("Measurement instrument is not connected.")
        self._stop_polling_session()
        self.reading_started.emit(count)
        try:
            batch_reader = getattr(session, "read_route_measurements", None)
            if callable(batch_reader):
                measurements = [
                    dict(item)
                    for item in _read_route_measurement_batch(
                        batch_reader,
                        count,
                        after_measurement=after_measurement,
                    )
                ]
            else:
                measurements = [
                    self._read_route_measurement_from_session(session)
                    for _index in range(count)
                ]
        except Exception as exc:
            error = self._lcr_error(exc)
            self._disconnect_session()
            self.connection_changed.emit(False, "", str(error))
            self.status_message.emit(f"Instrument read failed: {error}")
            raise error from exc
        finally:
            if restart_polling and self._session is session:
                self._stop_polling.clear()
                self._start_polling_thread()
        primary_value = self._mean_resistance_from_measurements(measurements)
        self._emit_reading_summary(primary_value, len(measurements))
        return measurements

    def prepare_route_measurement_batch_now(
        self,
        count: int,
        *,
        source_list_count: int | None = None,
    ) -> None:
        """Prepare a route measurement batch when the backend supports it."""

        count = max(1, int(count))
        self._run_on_meter_worker(
            lambda: self._prepare_route_measurement_batch_now_on_worker(
                count,
                source_list_count,
            )
        )

    def _prepare_route_measurement_batch_now_on_worker(
        self,
        count: int,
        source_list_count: int | None,
    ) -> None:
        session = self._session
        if session is None:
            raise LCRMeterError("Measurement instrument is not connected.")
        self._stop_polling_session()
        try:
            preparer = getattr(session, "prepare_route_measurements", None)
            _prepare_route_measurement_batch(
                preparer,
                count,
                source_list_count=source_list_count,
            )
        except Exception as exc:
            error = self._lcr_error(exc)
            self._disconnect_session()
            self.connection_changed.emit(False, "", str(error))
            self.status_message.emit(f"Instrument preparation failed: {error}")
            raise error from exc

    def _read_route_measurement_from_session(self, session: object) -> dict[str, object]:
        reader = getattr(session, "read_route_measurement", None)
        if callable(reader):
            return dict(reader(trigger=True))
        primary_reader = getattr(session, "read_primary_value", None)
        if not callable(primary_reader):
            raise LCRMeterError("Measurement instrument cannot read route values.")
        primary_value = primary_reader(trigger=True)
        return {"differential_resistance_ohm": primary_value}

    def _emit_reading_summary(self, primary_value: float, sample_count: int) -> None:
        count = max(1, int(sample_count))
        is_short = self._is_short_reading(primary_value)
        self.reading_updated.emit(primary_value, is_short)
        self.reading_summary_updated.emit(primary_value, is_short, count)

    @staticmethod
    def _mean_resistance_from_measurements(measurements: object) -> float:
        finite_values: list[float] = []
        for item in list(measurements) if measurements is not None else []:
            value = LCRMeterController._resistance_value_from_measurement(item)
            if math.isfinite(value):
                finite_values.append(value)
        if not finite_values:
            return math.nan
        return sum(finite_values) / len(finite_values)

    @staticmethod
    def _resistance_value_from_measurement(item: object) -> float:
        if isinstance(item, dict):
            for key in (
                "differential_resistance_ohm",
                "resistance_ohm",
                "v_over_i_ohm",
            ):
                if key not in item:
                    continue
                try:
                    return float(item[key])
                except (TypeError, ValueError):
                    continue
            return math.nan
        try:
            return float(item)
        except (TypeError, ValueError):
            return math.nan

    def abort_current_measurement(self) -> None:
        """Best-effort cancellation for a blocking route measurement read."""

        session = self._session
        abort = getattr(session, "abort_measurement", None)
        if callable(abort):
            abort()

    def visa_resource_roles(self) -> dict[str, dict[str, object]]:
        """Return station-owned VISA roles for the connected measurement backend."""

        session = self._session
        if session is None:
            return {}
        return _session_visa_resource_roles(session, meter_type=self._meter_type)

    def visa_operation(
        self,
        role: str,
        operation: str,
        *,
        command: str | None = None,
        timeout_ms: int | None = None,
        read_termination: str | None = None,
        write_termination: str | None = None,
    ) -> object:
        """Run one serialized VISA-like operation on the connected backend."""

        return self._run_on_meter_worker(
            lambda: self._visa_operation_on_worker(
                role,
                operation,
                command=command,
                timeout_ms=timeout_ms,
                read_termination=read_termination,
                write_termination=write_termination,
            )
        )

    def _visa_operation_on_worker(
        self,
        role: str,
        operation: str,
        *,
        command: str | None = None,
        timeout_ms: int | None = None,
        read_termination: str | None = None,
        write_termination: str | None = None,
    ) -> object:
        session = self._session
        if session is None:
            raise LCRMeterError("Measurement instrument is not connected.")
        self._stop_polling_session()
        try:
            return _session_visa_operation(
                session,
                role,
                operation,
                command=command,
                timeout_ms=timeout_ms,
                read_termination=read_termination,
                write_termination=write_termination,
            )
        except Exception as exc:
            error = self._lcr_error(exc)
            self._disconnect_session()
            self.connection_changed.emit(False, "", str(error))
            self.status_message.emit(f"Instrument VISA operation failed: {error}")
            raise error from exc

    def request_connect(self) -> None:
        """Open the configured measurement instrument in a background thread."""

        if not self._submit_meter_worker_call(self._run_connect):
            self.status_message.emit("Measurement instrument task already running.")

    def connect_now(self) -> None:
        """Open the configured measurement instrument in the current thread."""

        self._run_on_meter_worker(self._connect_now_on_worker)

    def _connect_now_on_worker(self) -> None:
        if self._session is not None:
            return
        try:
            self._connect_configured_session()
        except Exception as exc:
            error = self._lcr_error(exc)
            self._disconnect_session()
            self.connection_changed.emit(False, "", str(error))
            self.status_message.emit(f"Instrument connection failed: {error}")
            raise error from exc

    def request_disconnect(self) -> None:
        """Close the current measurement instrument and stop polling."""

        if not self._submit_meter_worker_call(self._run_disconnect):
            self.status_message.emit("Measurement instrument task already running.")

    def request_reconfigure(self) -> None:
        """Apply the current configuration to an already connected meter."""

        if self._session is None:
            return
        if not self._submit_meter_worker_call(self._run_reconfigure):
            self.status_message.emit("Measurement instrument task already running.")

    def shutdown(self) -> None:
        """Stop background work before application exit."""

        if self._shutdown_started:
            return
        self._shutdown_started = True
        _LCR_METER_CONTROLLERS.discard(self)
        self._stop_polling.set()
        try:
            self._run_on_meter_worker(self._disconnect_session)
        except Exception:  # pragma: no cover - best effort shutdown
            logger.exception("Failed to close measurement instrument session during shutdown")
        self._worker_shutdown.set()
        self._worker_queue.put(None)
        worker_thread = self._worker_thread
        if (
            worker_thread is not None
            and worker_thread.is_alive()
            and worker_thread is not threading.current_thread()
        ):
            worker_thread.join(timeout=2.0)

    def _run_connect(self) -> None:
        try:
            self._connect_configured_session()
        except Exception as exc:
            error = self._lcr_error(exc)
            self._disconnect_session()
            self.connection_changed.emit(False, "", str(error))
            self.status_message.emit(f"Instrument connection failed: {error}")

    def _run_reconfigure(self) -> None:
        try:
            session = self._session
            if session is None:
                return
            desired_resource = self._connection_key()
            connected_resource = self._connected_resource_name
            if not desired_resource:
                raise LCRMeterError(
                    "Measurement instrument resource is empty. Set it in Settings."
                )
            if desired_resource != connected_resource:
                self.status_message.emit(
                    "Instrument connection settings changed. Reconnecting."
                )
                self._disconnect_session()
                replacement = self._open_configured_session()
                identify = getattr(replacement, "identify", None)
                instrument_id = str(identify()) if callable(identify) else ""
                logger.info(
                    "Reconnected to measurement instrument %s (%s)",
                    self.connection_label(),
                    instrument_id or "IDN unavailable",
                )
                session = replacement
                self._session = session
                self._connected_resource_name = desired_resource
                backend_name = str(
                    getattr(session, "backend_name", session.__class__.__name__)
                )
                self.connection_changed.emit(
                    True, backend_name, self.connection_label()
                )
            else:
                self._stop_polling_session()
            if self._meter_type == ROUTE_METER_GWINSTEK:
                self._configure_active_session(session)
            self._stop_polling.clear()
            self.status_message.emit("Instrument settings applied.")
            self._resume_live_polling()
        except LCRMeterError as exc:
            self._disconnect_session()
            self.connection_changed.emit(False, "", str(exc))
            self.status_message.emit(f"Instrument reconfiguration failed: {exc}")

    def _run_disconnect(self) -> None:
        self._disconnect_session()
        self.connection_changed.emit(False, "", "Disconnected")
        self.status_message.emit("Measurement instrument disconnected.")

    def _connect_configured_session(self, *, resume_polling: bool = True) -> None:
        if self._meter_type == ROUTE_METER_GWINSTEK and not self._resource_name:
            raise LCRMeterError("GW Instek resource is empty. Set it in Settings.")
        if self._meter_type == ROUTE_METER_KEITHLEY:
            if not self._keithley_source_resource:
                raise LCRMeterError(
                    "Keithley 2400 resource is empty. Set it in Settings."
                )
        self._disconnect_session()
        session = self._open_configured_session()
        try:
            identify = getattr(session, "identify", None)
            instrument_id = str(identify()) if callable(identify) else ""
            self._validate_connected_session_identity(instrument_id)
            backend_name = str(
                getattr(session, "backend_name", session.__class__.__name__)
            )
            logger.info(
                "Connected to measurement instrument %s (%s)",
                self.connection_label(),
                instrument_id or "IDN unavailable",
            )
            if self._meter_type == ROUTE_METER_GWINSTEK:
                self._configure_session(session)
            elif self._meter_type == ROUTE_METER_KEITHLEY:
                self._configure_keithley_live_session(session)
        except Exception:
            closer = getattr(session, "close", None)
            if callable(closer):
                try:
                    closer()
                except Exception:  # pragma: no cover - best effort cleanup
                    logger.exception(
                        "Failed to close rejected measurement instrument session"
                    )
            raise
        self._session = session
        self._connected_resource_name = self._connection_key()
        if resume_polling:
            self._stop_polling.clear()
        self.connection_changed.emit(True, backend_name, self.connection_label())
        self.status_message.emit(
            f"Measurement instrument connected via {backend_name}."
        )
        if resume_polling:
            self._resume_live_polling()

    def _validate_connected_session_identity(self, instrument_id: str) -> None:
        if self._meter_type != ROUTE_METER_KEITHLEY:
            return
        normalized = str(instrument_id or "")
        if normalized.startswith("2400 ") or "; 2400 " in normalized:
            return
        raise LCRMeterError(
            "Keithley 2400 did not respond to *IDN?. "
            f"Check {self._keithley_source_resource or 'the source resource'} "
            "or power-cycle the source meter."
        )

    def _configure_active_session(self, session: _LCRSession) -> None:
        self._configure_session(session)

    def _configure_session(self, session: _LCRSession) -> None:
        if not isinstance(session, _LCRSession):
            return
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

    def _configure_keithley_live_session(self, session: object) -> None:
        configure = getattr(session, "configure_measurement", None)
        if not callable(configure):
            raise LCRMeterError("Connected instrument is not a Keithley pair.")
        configure(
            keithley_measurement_voltage_v=KEITHLEY_LIVE_MEASUREMENT_VOLTAGE_V,
            keithley_range_mode=OHMMETER_RANGE_MANUAL,
            keithley_source_voltage_range_v=KEITHLEY_LIVE_SOURCE_VOLTAGE_RANGE_V,
            keithley_voltmeter_range_v=KEITHLEY_LIVE_VOLTMETER_RANGE_V,
            keithley_current_range_a=KEITHLEY_LIVE_CURRENT_RANGE_A,
            keithley_compliance_current_a=KEITHLEY_LIVE_COMPLIANCE_CURRENT_A,
            keithley_nplc=KEITHLEY_LIVE_NPLC,
            keithley_terminals="rear",
            keithley_use_buffer=False,
            keithley_use_trigger_link=False,
        )

    def _open_configured_session(self) -> object:
        if self._meter_type == ROUTE_METER_KEITHLEY:
            return _open_keithley_session(
                self._keithley_source_resource,
                self._keithley_voltmeter_resource,
                self.DEFAULT_TIMEOUT_MS,
            )
        return _LCRSession(self._resource_name, self.DEFAULT_TIMEOUT_MS)

    def _connection_key(self) -> str:
        if self._meter_type == ROUTE_METER_KEITHLEY:
            source = normalize_resource_name(self._keithley_source_resource)
            voltmeter = normalize_resource_name(self._keithley_voltmeter_resource)
            if not source:
                return ""
            return f"{ROUTE_METER_KEITHLEY}|{source}|{voltmeter}"
        resource = normalize_resource_name(self._resource_name)
        if not resource:
            return ""
        return f"{ROUTE_METER_GWINSTEK}|{resource}"

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
        if self._live_output_context is None:
            return
        worker_thread = self._worker_thread
        if (
            worker_thread is not None
            and worker_thread.is_alive()
            and worker_thread is not threading.current_thread()
        ):
            self._run_on_meter_worker(self._close_live_output_context_on_worker)
        else:
            self._close_live_output_context_on_worker()

    def _pause_live_polling(self) -> None:
        self._stop_polling_session()

    def _disconnect_session(self) -> None:
        self._stop_polling_session()
        session = self._session
        self._session = None
        self._connected_resource_name = ""
        if session is not None:
            try:
                closer = getattr(session, "close", None)
                if callable(closer):
                    closer()
            except Exception:  # pragma: no cover - best effort cleanup
                logger.exception(
                    "Failed to close measurement instrument session cleanly"
                )

    def _start_polling_thread(self) -> None:
        self._wake_meter_worker()

    def _resume_live_polling(self) -> None:
        if not self._live_polling_enabled or self._session is None:
            return
        self._stop_polling.clear()
        self._start_polling_thread()

    def _poll_readings(self) -> None:
        while not self._stop_polling.is_set():
            if not self._run_meter_poll_once():
                return
            time.sleep(self._poll_interval_ms / 1000.0)

    def _run_meter_poll_once(self) -> bool:
        session = self._session
        if self._stop_polling.is_set() or session is None:
            return False
        try:
            if self._stop_polling.is_set() or session is not self._session:
                return False
            self._ensure_live_output_context_on_worker(session)
            reader = getattr(session, "read_primary_value", None)
            if not callable(reader):
                raise LCRMeterError("Measurement instrument cannot read values.")
            primary_value = float(reader(trigger=True))
        except Exception as exc:
            error = self._lcr_error(exc)
            self.status_message.emit(f"Instrument read failed: {error}")
            self.connection_changed.emit(False, "", str(error))
            self._disconnect_session()
            return False
        if self._stop_polling.is_set() or session is not self._session:
            return False
        self._emit_reading_summary(primary_value, 1)
        return True

    @contextmanager
    def output(self, enabled: bool = True) -> Iterator["LCRMeterController"]:
        context = self._run_on_meter_worker(
            lambda: self._enter_output_context_on_worker(bool(enabled))
        )
        try:
            yield self
        finally:
            if context is not None:
                self._run_on_meter_worker(
                    lambda: self._exit_output_context_on_worker(context)
                )

    def _enter_output_context_on_worker(self, enabled: bool) -> object | None:
        session = self._session
        if session is None:
            raise LCRMeterError("Measurement instrument is not connected.")
        self._stop_polling.set()
        self._close_live_output_context_on_worker()
        output = getattr(session, "output", None)
        if not callable(output):
            return None
        context = output(bool(enabled))
        enter = getattr(context, "__enter__", None)
        if not callable(enter):
            return None
        enter()
        return context

    @staticmethod
    def _exit_output_context_on_worker(context: object) -> None:
        exit_method = getattr(context, "__exit__", None)
        if callable(exit_method):
            exit_method(None, None, None)

    def _ensure_live_output_context_on_worker(self, session: object) -> None:
        if self._live_output_context is not None:
            return
        output = getattr(session, "output", None)
        if not callable(output):
            return
        context = output(True)
        enter = getattr(context, "__enter__", None)
        if not callable(enter):
            return
        enter()
        self._live_output_context = context

    def _close_live_output_context_on_worker(self) -> None:
        context = self._live_output_context
        self._live_output_context = None
        if context is not None:
            self._exit_output_context_on_worker(context)

    @staticmethod
    def _lcr_error(exc: BaseException) -> LCRMeterError:
        if isinstance(exc, LCRMeterError):
            return exc
        message = str(exc).strip() or exc.__class__.__name__
        return LCRMeterError(message)
