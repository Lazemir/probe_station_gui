"""Qt-free configuration and lazy backend ownership for LCR sessions."""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, replace
from typing import Callable

from probe_station_measure import OHMMETER_RANGE_MANUAL

from probe_station_gui.instruments.meters.gwinstek_session import (
    GWInstekLCRSession,
)
from probe_station_gui.instruments.meters.lcr_helpers import (
    gpib_interface_resources_for,
    normalize_resource_name,
    session_visa_resource_roles,
)
from probe_station_gui.instruments.meters.lcr_visa import (
    VisaOperationError,
    session_visa_operation as run_session_visa_operation,
)
from probe_station_gui.route.meter_config import (
    ROUTE_METER_GWINSTEK,
    ROUTE_METER_KEITHLEY,
    ROUTE_METER_KEITHLEY_2400,
    ROUTE_METER_KEITHLEY_TYPES,
    ROUTE_METER_LABELS,
    ROUTE_METER_TYPES,
    RouteMeterConfiguration,
)


logger = logging.getLogger(__name__)

DEFAULT_METER_TIMEOUT_MS = 10000
KEITHLEY_LIVE_MEASUREMENT_VOLTAGE_V = 0.03
KEITHLEY_LIVE_SOURCE_VOLTAGE_RANGE_V = 0.21
KEITHLEY_LIVE_VOLTMETER_RANGE_V = 1.0
KEITHLEY_LIVE_CURRENT_RANGE_A = 10e-6
KEITHLEY_LIVE_COMPLIANCE_CURRENT_A = 9.5e-6
KEITHLEY_LIVE_NPLC = 1.0


class LCRMeterError(RuntimeError):
    """Raised when an LCR meter backend cannot complete an operation."""


GWInstekLCRSession.error_type = LCRMeterError


@dataclass(frozen=True, slots=True)
class LCRSessionConfiguration:
    """Normalized immutable configuration for one live meter session."""

    meter_type: str
    resource_name: str
    keithley_source_resource: str
    keithley_voltmeter_resource: str
    measurement_function: str
    range_mode: str
    auto_range_enabled: bool
    impedance_range: int
    dcr_range: int
    frequency_hz: float
    level_mode: str
    voltage_level_v: float
    current_level_a: float
    source_resistance_ohm: int
    aperture_rate: str
    aperture_averages: int
    trigger_source: str
    trigger_delay_s: float
    bias_enabled: bool
    bias_level_v: float
    monitor1: str
    monitor2: str
    alc_enabled: bool
    short_threshold_ohm: float
    poll_interval_ms: int

    @classmethod
    def normalized(
        cls,
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
    ) -> LCRSessionConfiguration:
        normalized_type = str(meter_type).strip() or ROUTE_METER_GWINSTEK
        if normalized_type not in ROUTE_METER_TYPES:
            normalized_type = ROUTE_METER_GWINSTEK
        voltmeter_resource = (
            str(keithley_voltmeter_resource or "").strip()
            if normalized_type == ROUTE_METER_KEITHLEY
            else ""
        )
        return cls(
            meter_type=normalized_type,
            resource_name=str(resource_name).strip(),
            keithley_source_resource=str(keithley_source_resource).strip(),
            keithley_voltmeter_resource=voltmeter_resource,
            measurement_function=str(measurement_function).strip() or "DCR",
            range_mode=str(range_mode).strip().upper() or "HOLD",
            auto_range_enabled=bool(auto_range_enabled),
            impedance_range=max(0, min(8, int(impedance_range))),
            dcr_range=max(0, min(8, int(dcr_range))),
            frequency_hz=max(10.0, float(frequency_hz)),
            level_mode=str(level_mode).strip().upper() or "VOLTAGE",
            voltage_level_v=max(0.0, float(voltage_level_v)),
            current_level_a=max(0.0, float(current_level_a)),
            source_resistance_ohm=int(source_resistance_ohm),
            aperture_rate=str(aperture_rate).strip().upper() or "FAST",
            aperture_averages=max(1, min(256, int(aperture_averages))),
            trigger_source=str(trigger_source).strip().upper() or "INT",
            trigger_delay_s=max(0.0, float(trigger_delay_s)),
            bias_enabled=bool(bias_enabled),
            bias_level_v=max(-2.5, min(2.5, float(bias_level_v))),
            monitor1=str(monitor1).strip().upper() or "OFF",
            monitor2=str(monitor2).strip().upper() or "OFF",
            alc_enabled=bool(alc_enabled),
            short_threshold_ohm=max(0.0, float(short_threshold_ohm)),
            poll_interval_ms=max(50, int(poll_interval_ms)),
        )

    def with_route_configuration(
        self,
        configuration: RouteMeterConfiguration,
    ) -> LCRSessionConfiguration:
        if configuration.meter_type == ROUTE_METER_GWINSTEK:
            settings = configuration.gwinstek
            return replace(
                self,
                meter_type=ROUTE_METER_GWINSTEK,
                resource_name=str(settings.resource_name).strip(),
                keithley_source_resource="",
                keithley_voltmeter_resource="",
                measurement_function=(
                    str(settings.measurement_function).strip() or "DCR"
                ),
                range_mode=str(settings.range_mode).strip().upper() or "HOLD",
                auto_range_enabled=(str(settings.range_mode).strip().upper() or "HOLD")
                == "AUTO",
                impedance_range=max(0, min(8, int(settings.impedance_range))),
                dcr_range=max(0, min(8, int(settings.dcr_range))),
                frequency_hz=max(10.0, float(settings.frequency_hz)),
                level_mode=(str(settings.level_mode).strip().upper() or "VOLTAGE"),
                voltage_level_v=max(0.0, float(settings.voltage_level_v)),
                current_level_a=max(0.0, float(settings.current_level_a)),
                source_resistance_ohm=int(settings.source_resistance_ohm),
                aperture_rate=(str(settings.aperture_rate).strip().upper() or "FAST"),
                aperture_averages=max(
                    1,
                    min(256, int(settings.aperture_averages)),
                ),
                trigger_source="BUS",
                trigger_delay_s=max(0.0, float(settings.trigger_delay_s)),
                bias_enabled=bool(settings.bias_enabled),
                bias_level_v=max(
                    -2.5,
                    min(2.5, float(settings.bias_level_v)),
                ),
                monitor1=str(settings.monitor1).strip().upper() or "OFF",
                monitor2=str(settings.monitor2).strip().upper() or "OFF",
                alc_enabled=bool(settings.alc_enabled),
            )
        if configuration.meter_type in ROUTE_METER_KEITHLEY_TYPES:
            settings = configuration.keithley
            return replace(
                self,
                meter_type=configuration.meter_type,
                resource_name="",
                keithley_source_resource=str(settings.source_resource).strip(),
                keithley_voltmeter_resource=(
                    str(settings.voltmeter_resource).strip()
                    if configuration.meter_type == ROUTE_METER_KEITHLEY
                    else ""
                ),
                measurement_function="DCR",
                range_mode=str(settings.range_mode).strip().upper() or "AUTO",
                auto_range_enabled=(str(settings.range_mode).strip().upper() or "AUTO")
                == "AUTO",
            )
        return self

    @property
    def connection_key(self) -> str:
        if self.meter_type in ROUTE_METER_KEITHLEY_TYPES:
            source = normalize_resource_name(self.keithley_source_resource)
            voltmeter = normalize_resource_name(self.keithley_voltmeter_resource)
            if not source:
                return ""
            if self.meter_type == ROUTE_METER_KEITHLEY_2400:
                return f"{ROUTE_METER_KEITHLEY_2400}|{source}"
            return f"{ROUTE_METER_KEITHLEY}|{source}|{voltmeter}"
        resource = normalize_resource_name(self.resource_name)
        if not resource:
            return ""
        return f"{ROUTE_METER_GWINSTEK}|{resource}"

    @property
    def connection_label(self) -> str:
        if self.meter_type in ROUTE_METER_KEITHLEY_TYPES:
            source = self.keithley_source_resource or "source not configured"
            if self.keithley_voltmeter_resource:
                return (
                    f"Keithley 2400 {source}; 2182A {self.keithley_voltmeter_resource}"
                )
            return f"Keithley 2400 {source}"
        return self.resource_name

    @property
    def uses_bus_trigger(self) -> bool:
        return self.trigger_source.upper() == "BUS"

    def is_short_reading(self, primary_value: float) -> bool:
        return (
            self.measurement_function.upper() == "DCR"
            and math.isfinite(primary_value)
            and primary_value <= self.short_threshold_ohm
        )


def validate_required_resources(configuration: LCRSessionConfiguration) -> None:
    if (
        configuration.meter_type == ROUTE_METER_GWINSTEK
        and not configuration.resource_name
    ):
        raise LCRMeterError("GW Instek resource is empty. Set it in Settings.")
    if (
        configuration.meter_type in ROUTE_METER_KEITHLEY_TYPES
        and not configuration.keithley_source_resource
    ):
        raise LCRMeterError("Keithley 2400 resource is empty. Set it in Settings.")


def open_configured_session(
    configuration: LCRSessionConfiguration,
    *,
    timeout_ms: int = DEFAULT_METER_TIMEOUT_MS,
    gwinstek_session_type: type = GWInstekLCRSession,
    keithley_session_opener: Callable[[str, str, int], object] | None = None,
) -> object:
    if configuration.meter_type in ROUTE_METER_KEITHLEY_TYPES:
        opener = keithley_session_opener or open_keithley_session
        return opener(
            configuration.keithley_source_resource,
            configuration.keithley_voltmeter_resource,
            int(timeout_ms),
        )
    return gwinstek_session_type(configuration.resource_name, int(timeout_ms))


def open_keithley_session(
    source_resource: str,
    voltmeter_resource: str | None,
    timeout_ms: int,
) -> object:
    try:
        from probe_station_measure import Keithley2400SourceMeter, Keithley2400With2182A
    except ImportError as exc:
        raise LCRMeterError(
            "Keithley route measurements require optional dependency "
            "'probe-station-measure'. Install with `pip install .[lcr]`."
        ) from exc
    source = normalize_resource_name(source_resource)
    voltmeter = normalize_resource_name(voltmeter_resource or "")
    try:
        reset_gpib_interfaces_for_resources(source, voltmeter)
        if not voltmeter:
            return Keithley2400SourceMeter(source, timeout_ms=timeout_ms)
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


def reset_gpib_interfaces_for_resources(*resources: str | None) -> None:
    interfaces = gpib_interface_resources_for(tuple(resources))
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
                logger.info(
                    "Sent GPIB IFC on %s before Keithley connect.", interface_name
                )
        except Exception as exc:  # pragma: no cover - backend specific failures
            logger.warning("GPIB interface reset failed on %s: %s", interface_name, exc)
        finally:
            try:
                interface.close()
            except Exception:
                pass
    if reset_any:
        time.sleep(0.25)


def validate_session_identity(
    session: object,
    configuration: LCRSessionConfiguration,
) -> tuple[str, str]:
    identify = getattr(session, "identify", None)
    instrument_id = str(identify()) if callable(identify) else ""
    if configuration.meter_type in ROUTE_METER_KEITHLEY_TYPES:
        if not (instrument_id.startswith("2400 ") or "; 2400 " in instrument_id):
            raise LCRMeterError(
                "Keithley 2400 did not respond to *IDN?. "
                f"Check {configuration.keithley_source_resource or 'the source resource'} "
                "or power-cycle the source meter."
            )
    backend_name = str(getattr(session, "backend_name", session.__class__.__name__))
    return backend_name, instrument_id


def configure_live_session(
    session: object,
    configuration: LCRSessionConfiguration,
    *,
    gwinstek_session_type: type = GWInstekLCRSession,
) -> None:
    if configuration.meter_type == ROUTE_METER_GWINSTEK:
        if not isinstance(session, gwinstek_session_type):
            return
        session.configure_measurement(
            measurement_function=configuration.measurement_function,
            range_mode=configuration.range_mode,
            impedance_range=configuration.impedance_range,
            dcr_range=configuration.dcr_range,
            frequency_hz=configuration.frequency_hz,
            level_mode=configuration.level_mode,
            voltage_level_v=configuration.voltage_level_v,
            current_level_a=configuration.current_level_a,
            source_resistance_ohm=configuration.source_resistance_ohm,
            aperture_rate=configuration.aperture_rate,
            aperture_averages=configuration.aperture_averages,
            trigger_source="BUS",
            trigger_delay_s=configuration.trigger_delay_s,
            bias_enabled=configuration.bias_enabled,
            bias_level_v=configuration.bias_level_v,
            monitor1=configuration.monitor1,
            monitor2=configuration.monitor2,
            alc_enabled=configuration.alc_enabled,
        )
        return
    configure = getattr(session, "configure_measurement", None)
    if not callable(configure):
        raise LCRMeterError("Connected instrument is not a Keithley 2400.")
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


def session_roles(
    session: object | None,
    configuration: LCRSessionConfiguration,
) -> dict[str, dict[str, object]]:
    return session_visa_resource_roles(
        session,
        meter_type=configuration.meter_type,
    )


def session_visa_operation(
    session: object | None,
    role: str,
    operation: str,
    *,
    command: str | None,
    timeout_ms: int | None,
    read_termination: str | None,
    write_termination: str | None,
) -> object:
    try:
        return run_session_visa_operation(
            session,
            role,
            operation,
            command=command,
            timeout_ms=timeout_ms,
            read_termination=read_termination,
            write_termination=write_termination,
        )
    except VisaOperationError as exc:
        raise LCRMeterError(str(exc)) from exc


def coerce_lcr_error(exc: BaseException) -> LCRMeterError:
    if isinstance(exc, LCRMeterError):
        return exc
    message = str(exc).strip() or exc.__class__.__name__
    return LCRMeterError(message)


def route_meter_label(meter_type: str) -> str:
    return ROUTE_METER_LABELS.get(meter_type, meter_type)


__all__ = [
    "DEFAULT_METER_TIMEOUT_MS",
    "LCRMeterError",
    "LCRSessionConfiguration",
    "configure_live_session",
    "open_configured_session",
    "session_visa_operation",
    "validate_session_identity",
]
