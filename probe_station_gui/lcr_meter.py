"""Resistance-meter integrations used by calibration and route measurements."""

from __future__ import annotations

import inspect
import logging
import math
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

from PySide6.QtCore import QObject, Signal

from probe_station_measure import OHMMETER_RANGE_MANUAL


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
DEFAULT_METER_TIMEOUT_MS = 10000
COM_RESOURCE_PATTERN = re.compile(r"^COM(?P<port>\d+)$", re.IGNORECASE)


def normalize_resource_name(resource_name: str) -> str:
    """Translate ``COM4``-style names into VISA ASRL resources."""

    candidate = (resource_name or "").strip()
    match = COM_RESOURCE_PATTERN.fullmatch(candidate)
    if match:
        return f"ASRL{int(match.group('port'))}::INSTR"
    return candidate


def _callable_accepts_keyword(function: object, name: str) -> bool:
    try:
        signature = inspect.signature(function)
    except (TypeError, ValueError):
        return False
    for parameter in signature.parameters.values():
        if parameter.kind is inspect.Parameter.VAR_KEYWORD:
            return True
        if (
            parameter.name == name
            and parameter.kind
            in {
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                inspect.Parameter.KEYWORD_ONLY,
            }
        ):
            return True
    return False


def _prepare_route_measurement_batch(
    preparer: object,
    count: int,
    *,
    source_list_count: int | None,
) -> None:
    if not callable(preparer):
        return
    if (
        source_list_count is not None
        and _callable_accepts_keyword(preparer, "source_list_count")
    ):
        preparer(count, source_list_count=source_list_count)
        return
    preparer(count)


def _read_route_measurement_batch(
    batch_reader: object,
    count: int,
    *,
    after_measurement: object | None,
) -> list[object]:
    if not callable(batch_reader):
        return []
    kwargs: dict[str, object] = {}
    if _callable_accepts_keyword(batch_reader, "trigger"):
        kwargs["trigger"] = True
    if (
        after_measurement is not None
        and _callable_accepts_keyword(batch_reader, "after_measurement")
    ):
        kwargs["after_measurement"] = after_measurement
    return list(batch_reader(count, **kwargs))


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


def _voltage_sweep_point_to_dict(point: object) -> dict[str, object]:
    as_dict = getattr(point, "as_dict", None)
    if callable(as_dict):
        return dict(as_dict())
    if isinstance(point, dict):
        return dict(point)
    values: dict[str, object] = {}
    for name in (
        "source_voltage_v",
        "measured_voltage_v",
        "current_a",
        "resistance_ohm",
        "compliance_hit",
    ):
        if hasattr(point, name):
            values[name] = getattr(point, name)
    if values:
        return values
    return {"value": point}


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
    voltmeter_resource: str,
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
    voltmeter = normalize_resource_name(voltmeter_resource)
    try:
        return Keithley2400With2182A(
            source,
            voltmeter,
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

    def abort_current_measurement(self) -> None:
        session = self._session
        abort = getattr(session, "abort_measurement", None)
        if callable(abort):
            abort()

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
    reading_updated: Signal = Signal(float, bool)
    status_message: Signal = Signal(str)

    DEFAULT_TIMEOUT_MS = DEFAULT_METER_TIMEOUT_MS

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
        self._task_lock = threading.Lock()
        self._active_thread: Optional[threading.Thread] = None
        self._poll_thread: Optional[threading.Thread] = None
        self._stop_polling = threading.Event()

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

    def meter_type(self) -> str:
        """Return the currently configured meter type."""

        return self._meter_type

    def connection_label(self) -> str:
        """Return a human readable connection target for the configured meter."""

        if self._meter_type == ROUTE_METER_KEITHLEY:
            source = self._keithley_source_resource or "source not configured"
            voltmeter = (
                self._keithley_voltmeter_resource or "voltmeter not configured"
            )
            return f"Keithley 2400 {source}; 2182A {voltmeter}"
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
        session = self._session
        if session is None:
            raise LCRMeterError("Measurement instrument is not connected.")
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

        with self._task_lock:
            if self._active_thread and self._active_thread.is_alive():
                raise LCRMeterError("Measurement instrument task already running.")
            session = self._session
        if session is None:
            raise LCRMeterError("Measurement instrument is not connected.")
        self._stop_polling_session()
        try:
            reader = getattr(session, "read_route_measurement", None)
            if callable(reader):
                measurement = dict(reader(trigger=True))
            else:
                primary_reader = getattr(session, "read_primary_value", None)
                if not callable(primary_reader):
                    raise LCRMeterError(
                        "Measurement instrument cannot read route values."
                    )
                primary_value = primary_reader(trigger=True)
                measurement = {"differential_resistance_ohm": primary_value}
        except LCRMeterError:
            self._disconnect_session()
            self.connection_changed.emit(False, "", "Instrument read failed.")
            raise
        finally:
            if restart_polling and self._session is session:
                self._stop_polling.clear()
                self._start_polling_thread()
        primary_value = float(measurement["differential_resistance_ohm"])
        self.reading_updated.emit(
            primary_value,
            self._is_short_reading(primary_value),
        )
        return measurement

    def read_voltage_sweep_now(
        self,
        voltages_v: list[float] | tuple[float, ...],
        *,
        restart_polling: bool = False,
    ) -> dict[str, object]:
        """Synchronously run a raw source-voltage sweep and return V/I points."""

        with self._task_lock:
            if self._active_thread and self._active_thread.is_alive():
                raise LCRMeterError("Measurement instrument task already running.")
            session = self._session
        if session is None:
            raise LCRMeterError("Measurement instrument is not connected.")
        voltage_list_reader = getattr(session, "measure_voltage_list", None)
        if not callable(voltage_list_reader):
            raise LCRMeterError(
                "Raw voltage sweeps require a Keithley 2400 + 2182A instrument."
            )
        self._stop_polling_session()
        try:
            points = [
                _voltage_sweep_point_to_dict(point)
                for point in voltage_list_reader(voltages_v)
            ]
            measurement = {
                "source_voltages_v": [float(value) for value in voltages_v],
                "points": points,
            }
        except LCRMeterError:
            self._disconnect_session()
            self.connection_changed.emit(False, "", "Instrument read failed.")
            raise
        finally:
            if restart_polling and self._session is session:
                self._stop_polling.clear()
                self._start_polling_thread()
        primary_value = float(measurement.get("differential_resistance_ohm", math.nan))
        self.reading_updated.emit(
            primary_value,
            self._is_short_reading(primary_value),
        )
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
        with self._task_lock:
            if self._active_thread and self._active_thread.is_alive():
                raise LCRMeterError("Measurement instrument task already running.")
            session = self._session
        if session is None:
            raise LCRMeterError("Measurement instrument is not connected.")
        self._stop_polling_session()
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
                    self.read_route_measurement_now()
                    for _index in range(count)
                ]
        except LCRMeterError:
            self._disconnect_session()
            self.connection_changed.emit(False, "", "Instrument read failed.")
            raise
        finally:
            if restart_polling and self._session is session:
                self._stop_polling.clear()
                self._start_polling_thread()
        finite_values: list[float] = []
        for item in measurements:
            try:
                value = float(item.get("differential_resistance_ohm", math.nan))
            except (TypeError, ValueError):
                continue
            if math.isfinite(value):
                finite_values.append(value)
        if finite_values:
            primary_value = sum(finite_values) / len(finite_values)
            self.reading_updated.emit(
                primary_value,
                self._is_short_reading(primary_value),
            )
        return measurements

    def prepare_route_measurement_batch_now(
        self,
        count: int,
        *,
        source_list_count: int | None = None,
    ) -> None:
        """Prepare a route measurement batch when the backend supports it."""

        count = max(1, int(count))
        current_thread = threading.current_thread()
        with self._task_lock:
            if (
                self._active_thread
                and self._active_thread.is_alive()
                and self._active_thread is not current_thread
            ):
                raise LCRMeterError("Measurement instrument task already running.")
            session = self._session
            self._active_thread = current_thread
        if session is None:
            with self._task_lock:
                if self._active_thread is current_thread:
                    self._active_thread = None
            raise LCRMeterError("Measurement instrument is not connected.")
        self._stop_polling_session()
        try:
            preparer = getattr(session, "prepare_route_measurements", None)
            _prepare_route_measurement_batch(
                preparer,
                count,
                source_list_count=source_list_count,
            )
        finally:
            with self._task_lock:
                if self._active_thread is current_thread:
                    self._active_thread = None

    def abort_current_measurement(self) -> None:
        """Best-effort cancellation for a blocking route measurement read."""

        session = self._session
        abort = getattr(session, "abort_measurement", None)
        if callable(abort):
            abort()

    def request_connect(self) -> None:
        """Open the configured measurement instrument in a background thread."""

        with self._task_lock:
            if self._active_thread and self._active_thread.is_alive():
                self.status_message.emit(
                    "Measurement instrument task already running."
                )
                return
            thread = threading.Thread(target=self._run_connect, daemon=True)
            self._active_thread = thread
            thread.start()

    def connect_now(self) -> None:
        """Open the configured measurement instrument in the current thread."""

        with self._task_lock:
            if self._session is not None:
                return
            if self._active_thread and self._active_thread.is_alive():
                raise LCRMeterError("Measurement instrument task already running.")
            self._active_thread = threading.current_thread()
        try:
            self._connect_configured_session()
        except LCRMeterError as exc:
            self._disconnect_session()
            self.connection_changed.emit(False, "", str(exc))
            self.status_message.emit(f"Instrument connection failed: {exc}")
            raise
        finally:
            with self._task_lock:
                self._active_thread = None

    def request_disconnect(self) -> None:
        """Close the current measurement instrument and stop polling."""

        with self._task_lock:
            if self._active_thread and self._active_thread.is_alive():
                self.status_message.emit(
                    "Measurement instrument task already running."
                )
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
                self.status_message.emit(
                    "Measurement instrument task already running."
                )
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
                closer = getattr(session, "close", None)
                if callable(closer):
                    closer()
            except Exception:  # pragma: no cover - best effort shutdown
                logger.exception(
                    "Failed to close measurement instrument session during shutdown"
                )

    def _run_connect(self) -> None:
        try:
            self._connect_configured_session()
        except LCRMeterError as exc:
            self._disconnect_session()
            self.connection_changed.emit(False, "", str(exc))
            self.status_message.emit(f"Instrument connection failed: {exc}")
        finally:
            with self._task_lock:
                self._active_thread = None

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
        except LCRMeterError as exc:
            self._disconnect_session()
            self.connection_changed.emit(False, "", str(exc))
            self.status_message.emit(f"Instrument reconfiguration failed: {exc}")
        finally:
            with self._task_lock:
                self._active_thread = None

    def _run_disconnect(self) -> None:
        try:
            self._disconnect_session()
            self.connection_changed.emit(False, "", "Disconnected")
            self.status_message.emit("Measurement instrument disconnected.")
        finally:
            with self._task_lock:
                self._active_thread = None

    def _connect_configured_session(self) -> None:
        if self._meter_type == ROUTE_METER_GWINSTEK and not self._resource_name:
            raise LCRMeterError("GW Instek resource is empty. Set it in Settings.")
        if self._meter_type == ROUTE_METER_KEITHLEY:
            if not self._keithley_source_resource:
                raise LCRMeterError(
                    "Keithley 2400 resource is empty. Set it in Settings."
                )
            if not self._keithley_voltmeter_resource:
                raise LCRMeterError(
                    "Keithley 2182A resource is empty. Set it in Settings."
                )
        self._disconnect_session()
        session = self._open_configured_session()
        identify = getattr(session, "identify", None)
        instrument_id = str(identify()) if callable(identify) else ""
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
        self._session = session
        self._connected_resource_name = self._connection_key()
        self._stop_polling.clear()
        self.connection_changed.emit(True, backend_name, self.connection_label())
        self.status_message.emit(
            f"Measurement instrument connected via {backend_name}."
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
            if not source or not voltmeter:
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
                closer = getattr(session, "close", None)
                if callable(closer):
                    closer()
            except Exception:  # pragma: no cover - best effort cleanup
                logger.exception(
                    "Failed to close measurement instrument session cleanly"
                )

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
                reader = getattr(session, "read_primary_value", None)
                if not callable(reader):
                    raise LCRMeterError(
                        "Measurement instrument cannot read values."
                    )
                primary_value = float(reader(trigger=True))
            except LCRMeterError as exc:
                self.status_message.emit(f"Instrument read failed: {exc}")
                self.connection_changed.emit(False, "", str(exc))
                self._disconnect_session()
                return
            self.reading_updated.emit(
                primary_value, self._is_short_reading(primary_value)
            )
            time.sleep(self._poll_interval_ms / 1000.0)
