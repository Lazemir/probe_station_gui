"""Route-meter selection and per-run settings."""

from __future__ import annotations

from dataclasses import dataclass, field

from probe_station_measure import OHMMETER_RANGE_MANUAL

from probe_station_gui.route.payload_parsing import (
    payload_bool as _payload_bool,
    payload_float as _payload_float,
    payload_optional_float as _payload_optional_float,
)


ROUTE_METER_GWINSTEK = "gwinstek_lcr_76200"
ROUTE_METER_KEITHLEY_2400 = "keithley_2400"
ROUTE_METER_KEITHLEY = "keithley_2400_2182a"
ROUTE_METER_KEITHLEY_TYPES: tuple[str, ...] = (
    ROUTE_METER_KEITHLEY_2400,
    ROUTE_METER_KEITHLEY,
)
ROUTE_METER_TYPES: tuple[str, ...] = (
    ROUTE_METER_GWINSTEK,
    ROUTE_METER_KEITHLEY_2400,
    ROUTE_METER_KEITHLEY,
)
ROUTE_METER_LABELS: dict[str, str] = {
    ROUTE_METER_GWINSTEK: "GW Instek LCR-76200",
    ROUTE_METER_KEITHLEY_2400: "Keithley 2400",
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
    """Per-run resistance settings for Keithley 2400-based measurements."""

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

        if self.meter_type in ROUTE_METER_KEITHLEY_TYPES:
            return f"{float(self.keithley.nplc):g}"
        return ""

    def measurement_type_label(self) -> str:
        """Return a concise CSV label for the route measurement mode."""

        if self.meter_type == ROUTE_METER_KEITHLEY_2400:
            voltage = float(self.keithley.measurement_voltage_v)
            return f"Keithley 2400 voltage sweep +/-{voltage:g} V"
        if self.meter_type == ROUTE_METER_KEITHLEY:
            voltage = float(self.keithley.measurement_voltage_v)
            return f"Keithley voltage sweep +/-{voltage:g} V"
        function = str(self.gwinstek.measurement_function or "").strip()
        if function.upper() == "DCR":
            return "GW Instek DCR"
        return f"GW Instek {function or 'measurement'}"


@dataclass(frozen=True)
class _KeithleyVoltageSettings:
    measurement_voltage_v: float
    range_mode: str
    voltage_range_v: float | None
    source_voltage_range_v: float
    voltmeter_range_v: float


@dataclass(frozen=True)
class _KeithleyResistanceSettings:
    expected_resistance_ohm: float | None
    minimum_resistance_ohm: float | None
    maximum_current_a: float | None


@dataclass(frozen=True)
class _KeithleyRangeSettings:
    current_range_a: float
    compliance_current_a: float
    range_voltage_headroom: float
    range_current_headroom: float


@dataclass(frozen=True)
class _KeithleyTimingSettings:
    nplc: float
    terminals: str
    trigger_delay_s: float


def route_meter_type_from_payload(value: object) -> str | None:
    """Normalize route-meter type aliases from API payloads."""

    if value is None:
        return None
    text = str(value).strip().lower()
    if text in {"", "current", "configured"}:
        return None
    if text in {"keithley_2400", "2400", "source_meter", "source-meter"}:
        return ROUTE_METER_KEITHLEY_2400
    if text in {"keithley", "keithley_2400_2182a", "2400_2182a"}:
        return ROUTE_METER_KEITHLEY
    if text in {"gwinstek", "lcr", "gwinstek_lcr_76200"}:
        return ROUTE_METER_GWINSTEK
    return text


def route_meter_configuration_from_payload(
    payload: object,
    *,
    voltages_v: list[float] | None,
    current_meter_type: str | None,
    default_gwinstek_resource_name: str | None,
) -> RouteMeterConfiguration:
    """Build a per-run meter configuration from an API route payload."""

    meter_payload = payload if isinstance(payload, dict) else {}
    meter_type = route_meter_type_from_payload(
        meter_payload.get("meter_type", meter_payload.get("type"))
    )
    if meter_type is None:
        meter_type = current_meter_type
    if meter_type in ROUTE_METER_KEITHLEY_TYPES:
        keithley_payload = meter_payload.get("keithley")
        if not isinstance(keithley_payload, dict):
            keithley_payload = meter_payload
        return RouteMeterConfiguration(
            meter_type=meter_type,
            keithley=_keithley_settings_from_payload(
                keithley_payload,
                voltages_v=voltages_v,
            ),
        )
    if meter_type == ROUTE_METER_GWINSTEK:
        gw_payload = meter_payload.get("gwinstek")
        if not isinstance(gw_payload, dict):
            gw_payload = meter_payload
        return RouteMeterConfiguration(
            meter_type=ROUTE_METER_GWINSTEK,
            gwinstek=_gwinstek_settings_from_payload(
                gw_payload,
                default_resource_name=default_gwinstek_resource_name,
            ),
        )
    raise ValueError(f"Unsupported meter_type: {meter_type!r}")


def _keithley_settings_from_payload(
    keithley_payload: dict[str, object],
    *,
    voltages_v: list[float] | None,
) -> KeithleyRouteMeterSettings:
    defaults = KeithleyRouteMeterSettings()
    range_payload = _keithley_range_payload(keithley_payload)
    voltage = _keithley_voltage_settings(
        range_payload,
        voltages_v=voltages_v,
        defaults=defaults,
    )
    resistance = _keithley_resistance_settings(range_payload)
    ranges = _keithley_range_settings(range_payload, defaults=defaults)
    timing = _keithley_timing_settings(keithley_payload, defaults=defaults)
    return KeithleyRouteMeterSettings(
        measurement_voltage_v=voltage.measurement_voltage_v,
        range_mode=voltage.range_mode,
        expected_resistance_ohm=resistance.expected_resistance_ohm,
        minimum_resistance_ohm=resistance.minimum_resistance_ohm,
        maximum_current_a=resistance.maximum_current_a,
        voltage_range_v=voltage.voltage_range_v,
        source_voltage_range_v=voltage.source_voltage_range_v,
        voltmeter_range_v=voltage.voltmeter_range_v,
        current_range_a=ranges.current_range_a,
        compliance_current_a=ranges.compliance_current_a,
        range_voltage_headroom=ranges.range_voltage_headroom,
        range_current_headroom=ranges.range_current_headroom,
        nplc=timing.nplc,
        terminals=timing.terminals,
        trigger_delay_s=timing.trigger_delay_s,
    )


def _keithley_range_payload(
    keithley_payload: dict[str, object],
) -> dict[str, object]:
    range_payload = dict(keithley_payload)
    nested_ranges = keithley_payload.get("ranges")
    if isinstance(nested_ranges, dict):
        range_payload.update(nested_ranges)
    return range_payload


def _keithley_voltage_settings(
    range_payload: dict[str, object],
    *,
    voltages_v: list[float] | None,
    defaults: KeithleyRouteMeterSettings,
) -> _KeithleyVoltageSettings:
    default_voltage = defaults.measurement_voltage_v
    max_voltage = _max_abs_voltage_or_default(voltages_v, default=default_voltage)
    measurement_voltage = _payload_float(
        range_payload,
        "measurement_voltage_v",
        "voltage_v",
        default=max(max_voltage, 1e-12),
        minimum=1e-12,
    )
    range_mode = str(
        range_payload.get(
            "range_mode",
            range_payload.get("mode", defaults.range_mode),
        )
    )
    voltage_range, source_voltage_range, voltmeter_range = (
        _keithley_voltage_range_values(range_payload)
    )
    voltage_range, source_voltage_range, voltmeter_range = (
        _resolved_keithley_voltage_ranges(
            voltage_range=voltage_range,
            source_voltage_range=source_voltage_range,
            voltmeter_range=voltmeter_range,
            range_mode=range_mode,
            measurement_voltage=measurement_voltage,
            max_voltage=max_voltage,
        )
    )
    return _KeithleyVoltageSettings(
        measurement_voltage_v=measurement_voltage,
        range_mode=range_mode,
        voltage_range_v=voltage_range,
        source_voltage_range_v=(
            source_voltage_range
            if source_voltage_range is not None
            else defaults.source_voltage_range_v
        ),
        voltmeter_range_v=(
            voltmeter_range
            if voltmeter_range is not None
            else defaults.voltmeter_range_v
        ),
    )


def _keithley_voltage_range_values(
    range_payload: dict[str, object],
) -> tuple[float | None, float | None, float | None]:
    voltage_range = _payload_optional_float(
        range_payload,
        "voltage_range_v",
        minimum=1e-12,
    )
    source_voltage_range = _payload_optional_float(
        range_payload,
        "source_voltage_range_v",
        "source_range_v",
        "keithley_source_voltage_range_v",
        minimum=1e-12,
    )
    voltmeter_range = _payload_optional_float(
        range_payload,
        "voltmeter_range_v",
        "meter_voltage_range_v",
        "nanovoltmeter_range_v",
        "keithley_voltmeter_range_v",
        minimum=1e-12,
    )
    return voltage_range, source_voltage_range, voltmeter_range


def _resolved_keithley_voltage_ranges(
    *,
    voltage_range: float | None,
    source_voltage_range: float | None,
    voltmeter_range: float | None,
    range_mode: str,
    measurement_voltage: float,
    max_voltage: float,
) -> tuple[float | None, float | None, float | None]:
    if voltage_range is not None:
        return voltage_range, voltage_range, voltage_range
    if source_voltage_range is not None or voltmeter_range is not None:
        return voltage_range, source_voltage_range, voltmeter_range
    if range_mode.strip().lower() in {
        "code_auto",
        "auto",
        "software_auto",
        "computed_auto",
    }:
        return voltage_range, source_voltage_range, voltmeter_range
    voltage_range = max(measurement_voltage, max_voltage)
    return voltage_range, voltage_range, voltage_range


def _max_abs_voltage_or_default(
    voltages_v: list[float] | None,
    *,
    default: float,
) -> float:
    return max(abs(float(value)) for value in voltages_v) if voltages_v else default


def _keithley_resistance_settings(
    range_payload: dict[str, object],
) -> _KeithleyResistanceSettings:
    return _KeithleyResistanceSettings(
        expected_resistance_ohm=_payload_optional_float(
            range_payload,
            "expected_resistance_ohm",
            "resistance_ohm",
            minimum=1e-12,
        ),
        minimum_resistance_ohm=_payload_optional_float(
            range_payload,
            "minimum_resistance_ohm",
            "min_resistance_ohm",
            "resistance_floor_ohm",
            minimum=1e-12,
        ),
        maximum_current_a=_payload_optional_float(
            range_payload,
            "maximum_current_a",
            "max_current_a",
            "current_limit_a",
            minimum=1e-12,
        ),
    )


def _keithley_range_settings(
    range_payload: dict[str, object],
    *,
    defaults: KeithleyRouteMeterSettings,
) -> _KeithleyRangeSettings:
    return _KeithleyRangeSettings(
        current_range_a=_payload_float(
            range_payload,
            "current_range_a",
            default=defaults.current_range_a,
            minimum=1e-12,
        ),
        compliance_current_a=_payload_float(
            range_payload,
            "compliance_current_a",
            "current_limit_a",
            "max_current_a",
            default=defaults.compliance_current_a,
            minimum=1e-12,
        ),
        range_voltage_headroom=_payload_float(
            range_payload,
            "range_voltage_headroom",
            "voltage_headroom",
            default=defaults.range_voltage_headroom,
            minimum=1.0,
        ),
        range_current_headroom=_payload_float(
            range_payload,
            "range_current_headroom",
            "current_headroom",
            default=defaults.range_current_headroom,
            minimum=1.0,
        ),
    )


def _keithley_timing_settings(
    keithley_payload: dict[str, object],
    *,
    defaults: KeithleyRouteMeterSettings,
) -> _KeithleyTimingSettings:
    return _KeithleyTimingSettings(
        nplc=_payload_float(
            keithley_payload,
            "nplc",
            default=defaults.nplc,
            minimum=0.01,
        ),
        terminals=str(keithley_payload.get("terminals", defaults.terminals)),
        trigger_delay_s=_payload_float(
            keithley_payload,
            "trigger_delay_s",
            "delay_s",
            default=defaults.trigger_delay_s,
            minimum=0.0,
        ),
    )


def _gwinstek_settings_from_payload(
    gw_payload: dict[str, object],
    *,
    default_resource_name: str | None,
) -> GWInstekRouteMeterSettings:
    defaults = (
        GWInstekRouteMeterSettings(resource_name=default_resource_name)
        if default_resource_name is not None
        else GWInstekRouteMeterSettings()
    )
    return GWInstekRouteMeterSettings(
        resource_name=str(gw_payload.get("resource_name", defaults.resource_name)),
        measurement_function=str(
            gw_payload.get("measurement_function", defaults.measurement_function)
        ),
        range_mode=str(gw_payload.get("range_mode", defaults.range_mode)),
        impedance_range=int(
            _payload_float(
                gw_payload,
                "impedance_range",
                default=defaults.impedance_range,
            )
        ),
        dcr_range=int(
            _payload_float(gw_payload, "dcr_range", default=defaults.dcr_range)
        ),
        frequency_hz=_payload_float(
            gw_payload,
            "frequency_hz",
            default=defaults.frequency_hz,
            minimum=10.0,
        ),
        level_mode=str(gw_payload.get("level_mode", defaults.level_mode)),
        voltage_level_v=_payload_float(
            gw_payload,
            "voltage_level_v",
            default=defaults.voltage_level_v,
            minimum=0.0,
        ),
        current_level_a=_payload_float(
            gw_payload,
            "current_level_a",
            default=defaults.current_level_a,
            minimum=0.0,
        ),
        source_resistance_ohm=int(
            _payload_float(
                gw_payload,
                "source_resistance_ohm",
                default=defaults.source_resistance_ohm,
            )
        ),
        aperture_rate=str(gw_payload.get("aperture_rate", defaults.aperture_rate)),
        aperture_averages=int(
            _payload_float(
                gw_payload,
                "aperture_averages",
                default=defaults.aperture_averages,
                minimum=1.0,
            )
        ),
        trigger_delay_s=_payload_float(
            gw_payload,
            "trigger_delay_s",
            default=defaults.trigger_delay_s,
            minimum=0.0,
        ),
        bias_enabled=_payload_bool(
            gw_payload, "bias_enabled", default=defaults.bias_enabled
        ),
        bias_level_v=_payload_float(
            gw_payload,
            "bias_level_v",
            default=defaults.bias_level_v,
        ),
        monitor1=str(gw_payload.get("monitor1", defaults.monitor1)),
        monitor2=str(gw_payload.get("monitor2", defaults.monitor2)),
        alc_enabled=_payload_bool(
            gw_payload, "alc_enabled", default=defaults.alc_enabled
        ),
    )


__all__ = [
    "GWInstekRouteMeterSettings",
    "KeithleyRouteMeterSettings",
    "ROUTE_METER_GWINSTEK",
    "ROUTE_METER_KEITHLEY",
    "ROUTE_METER_KEITHLEY_2400",
    "ROUTE_METER_KEITHLEY_TYPES",
    "ROUTE_METER_LABELS",
    "ROUTE_METER_TYPES",
    "RouteMeterConfiguration",
    "route_meter_configuration_from_payload",
    "route_meter_type_from_payload",
]
