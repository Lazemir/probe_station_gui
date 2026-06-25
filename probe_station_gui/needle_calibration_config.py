"""Pure needle calibration settings normalisation helpers."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SavedStagePositionConfig:
    """Normalised XYZ bookmark used by calibration workflows."""

    x_mm: float = 0.0
    y_mm: float = 0.0
    z_mm: float = 0.0
    configured: bool = False


@dataclass(frozen=True)
class NeedleCalibrationDefaults:
    """Default values and constraints for needle calibration settings."""

    meter_type: str
    visa_resource: str
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
    monitor: str
    alc_enabled: bool
    short_threshold_ohm: float
    poll_interval_ms: int
    feedrate_mm_min: float
    contact_zone_mm: float
    min_feedrate_mm_min: float
    meter_types: tuple[str, ...]
    measurement_functions: tuple[str, ...]
    range_modes: tuple[str, ...]
    level_modes: tuple[str, ...]
    source_resistances_ohm: tuple[int, ...]
    aperture_rates: tuple[str, ...]
    trigger_sources: tuple[str, ...]
    monitor_parameters: tuple[str, ...]


@dataclass
class NeedleCalibrationConfig:
    """Normalised needle calibration and external meter settings."""

    meter_type: str
    visa_resource: str
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
    feedrate_mm_min: float
    contact_zone_mm: float
    raise_position_mm: float
    raise_position_configured: bool
    down_position_mm: float
    down_position_configured: bool
    chip_position: SavedStagePositionConfig
    stone_position: SavedStagePositionConfig


def parse_needle_calibration_settings(
    raw_needle_calibration: object,
    defaults: NeedleCalibrationDefaults,
) -> NeedleCalibrationConfig:
    """Normalise persisted needle calibration settings."""

    meter_type = defaults.meter_type
    visa_resource = defaults.visa_resource
    keithley_source_resource = defaults.keithley_source_resource
    keithley_voltmeter_resource = defaults.keithley_voltmeter_resource
    measurement_function = defaults.measurement_function
    range_mode = defaults.range_mode
    auto_range_enabled = defaults.auto_range_enabled
    impedance_range = defaults.impedance_range
    dcr_range = defaults.dcr_range
    frequency_hz = defaults.frequency_hz
    level_mode = defaults.level_mode
    voltage_level_v = defaults.voltage_level_v
    current_level_a = defaults.current_level_a
    source_resistance_ohm = defaults.source_resistance_ohm
    aperture_rate = defaults.aperture_rate
    aperture_averages = defaults.aperture_averages
    trigger_source = defaults.trigger_source
    trigger_delay_s = defaults.trigger_delay_s
    bias_enabled = defaults.bias_enabled
    bias_level_v = defaults.bias_level_v
    monitor1 = defaults.monitor
    monitor2 = defaults.monitor
    alc_enabled = defaults.alc_enabled
    short_threshold_ohm = defaults.short_threshold_ohm
    poll_interval_ms = defaults.poll_interval_ms
    feedrate_mm_min = defaults.feedrate_mm_min
    contact_zone_mm = defaults.contact_zone_mm
    raise_position_mm = 0.0
    raise_position_configured = False
    down_position_mm = 0.0
    down_position_configured = False
    chip_position = SavedStagePositionConfig()
    stone_position = SavedStagePositionConfig()

    if isinstance(raw_needle_calibration, dict):
        meter_type = _normalise_choice(
            raw_needle_calibration.get("meter_type", meter_type),
            choices=defaults.meter_types,
            default=meter_type,
        )
        visa_resource = _strip_string(
            raw_needle_calibration.get("visa_resource", visa_resource),
            default=visa_resource,
        )
        keithley_source_resource = _strip_string(
            raw_needle_calibration.get(
                "keithley_source_resource",
                keithley_source_resource,
            ),
            default=keithley_source_resource,
        )
        keithley_voltmeter_resource = _strip_string(
            raw_needle_calibration.get(
                "keithley_voltmeter_resource",
                keithley_voltmeter_resource,
            ),
            default=keithley_voltmeter_resource,
        )
        measurement_function = _normalise_choice(
            raw_needle_calibration.get(
                "measurement_function",
                measurement_function,
            ),
            choices=defaults.measurement_functions,
            default=measurement_function,
        )
        range_mode = _normalise_choice(
            raw_needle_calibration.get("range_mode", range_mode),
            choices=defaults.range_modes,
            default=range_mode,
        )
        auto_range_enabled = _coerce_bool(
            raw_needle_calibration.get("auto_range_enabled", auto_range_enabled),
            default=defaults.auto_range_enabled,
        )
        impedance_range = _coerce_int(
            raw_needle_calibration.get("impedance_range", impedance_range),
            default=defaults.impedance_range,
        )
        dcr_range = _coerce_int(
            raw_needle_calibration.get("dcr_range", dcr_range),
            default=defaults.dcr_range,
        )
        frequency_hz = _coerce_float(
            raw_needle_calibration.get("frequency_hz", frequency_hz),
            default=defaults.frequency_hz,
        )
        level_mode = _normalise_choice(
            raw_needle_calibration.get("level_mode", level_mode),
            choices=defaults.level_modes,
            default=level_mode,
        )
        voltage_level_v = _coerce_float(
            raw_needle_calibration.get("voltage_level_v", voltage_level_v),
            default=defaults.voltage_level_v,
        )
        current_level_a = _coerce_float(
            raw_needle_calibration.get("current_level_a", current_level_a),
            default=defaults.current_level_a,
        )
        source_resistance_ohm = _coerce_int(
            raw_needle_calibration.get(
                "source_resistance_ohm",
                source_resistance_ohm,
            ),
            default=defaults.source_resistance_ohm,
        )
        aperture_rate = _normalise_choice(
            raw_needle_calibration.get("aperture_rate", aperture_rate),
            choices=defaults.aperture_rates,
            default=aperture_rate,
        )
        aperture_averages = _coerce_int(
            raw_needle_calibration.get("aperture_averages", aperture_averages),
            default=defaults.aperture_averages,
        )
        trigger_source = _normalise_choice(
            raw_needle_calibration.get("trigger_source", trigger_source),
            choices=defaults.trigger_sources,
            default=trigger_source,
        )
        trigger_delay_s = _coerce_float(
            raw_needle_calibration.get("trigger_delay_s", trigger_delay_s),
            default=defaults.trigger_delay_s,
        )
        bias_enabled = _coerce_bool(
            raw_needle_calibration.get("bias_enabled", bias_enabled),
            default=defaults.bias_enabled,
        )
        bias_level_v = _coerce_float(
            raw_needle_calibration.get("bias_level_v", bias_level_v),
            default=defaults.bias_level_v,
        )
        monitor1 = _normalise_choice(
            raw_needle_calibration.get("monitor1", monitor1),
            choices=defaults.monitor_parameters,
            default=monitor1,
        )
        monitor2 = _normalise_choice(
            raw_needle_calibration.get("monitor2", monitor2),
            choices=defaults.monitor_parameters,
            default=monitor2,
        )
        alc_enabled = _coerce_bool(
            raw_needle_calibration.get("alc_enabled", alc_enabled),
            default=defaults.alc_enabled,
        )
        short_threshold_ohm = _coerce_float(
            raw_needle_calibration.get(
                "short_threshold_ohm",
                short_threshold_ohm,
            ),
            default=defaults.short_threshold_ohm,
        )
        poll_interval_ms = _coerce_int(
            raw_needle_calibration.get("poll_interval_ms", poll_interval_ms),
            default=defaults.poll_interval_ms,
        )
        feedrate_mm_min = _coerce_float(
            raw_needle_calibration.get("feedrate_mm_min", feedrate_mm_min),
            default=defaults.feedrate_mm_min,
        )
        contact_zone_mm = _coerce_float(
            raw_needle_calibration.get("contact_zone_mm", contact_zone_mm),
            default=defaults.contact_zone_mm,
        )
        raise_position_mm = _coerce_float(
            raw_needle_calibration.get("raise_position_mm", raise_position_mm),
            default=0.0,
        )
        raise_position_configured = bool(
            raw_needle_calibration.get(
                "raise_position_configured",
                raise_position_configured,
            )
        )
        down_position_mm = _coerce_float(
            raw_needle_calibration.get("down_position_mm", down_position_mm),
            default=0.0,
        )
        down_position_configured = bool(
            raw_needle_calibration.get(
                "down_position_configured",
                down_position_configured,
            )
        )
        chip_position = parse_saved_stage_position(
            raw_needle_calibration.get("chip_position")
        )
        stone_position = parse_saved_stage_position(
            raw_needle_calibration.get("stone_position")
        )

    if impedance_range < 0 or impedance_range > 8:
        impedance_range = defaults.impedance_range
    if dcr_range < 0 or dcr_range > 8:
        dcr_range = defaults.dcr_range
    if frequency_hz < 10:
        frequency_hz = defaults.frequency_hz
    if voltage_level_v < 0.01 or voltage_level_v > 2.0:
        voltage_level_v = defaults.voltage_level_v
    if current_level_a < 0.0001 or current_level_a > 0.02:
        current_level_a = defaults.current_level_a
    if source_resistance_ohm not in defaults.source_resistances_ohm:
        source_resistance_ohm = defaults.source_resistance_ohm
    if aperture_averages < 1 or aperture_averages > 256:
        aperture_averages = defaults.aperture_averages
    if trigger_delay_s < 0 or trigger_delay_s > 60:
        trigger_delay_s = defaults.trigger_delay_s
    if bias_level_v < -2.5 or bias_level_v > 2.5:
        bias_level_v = defaults.bias_level_v
    if short_threshold_ohm < 0:
        short_threshold_ohm = defaults.short_threshold_ohm
    if poll_interval_ms < 50:
        poll_interval_ms = defaults.poll_interval_ms
    if feedrate_mm_min <= 0:
        feedrate_mm_min = defaults.feedrate_mm_min
    feedrate_mm_min = max(defaults.min_feedrate_mm_min, feedrate_mm_min)
    if contact_zone_mm < 0:
        contact_zone_mm = defaults.contact_zone_mm
    auto_range_enabled = range_mode == "AUTO"
    if not raise_position_configured and down_position_configured:
        raise_position_mm = down_position_mm
        raise_position_configured = True

    return NeedleCalibrationConfig(
        meter_type=meter_type,
        visa_resource=visa_resource,
        keithley_source_resource=keithley_source_resource,
        keithley_voltmeter_resource=keithley_voltmeter_resource,
        measurement_function=measurement_function,
        range_mode=range_mode,
        auto_range_enabled=auto_range_enabled,
        impedance_range=impedance_range,
        dcr_range=dcr_range,
        frequency_hz=frequency_hz,
        level_mode=level_mode,
        voltage_level_v=voltage_level_v,
        current_level_a=current_level_a,
        source_resistance_ohm=source_resistance_ohm,
        aperture_rate=aperture_rate,
        aperture_averages=aperture_averages,
        trigger_source=trigger_source,
        trigger_delay_s=trigger_delay_s,
        bias_enabled=bias_enabled,
        bias_level_v=bias_level_v,
        monitor1=monitor1,
        monitor2=monitor2,
        alc_enabled=alc_enabled,
        short_threshold_ohm=short_threshold_ohm,
        poll_interval_ms=poll_interval_ms,
        feedrate_mm_min=feedrate_mm_min,
        contact_zone_mm=contact_zone_mm,
        raise_position_mm=raise_position_mm,
        raise_position_configured=raise_position_configured,
        down_position_mm=down_position_mm,
        down_position_configured=down_position_configured,
        chip_position=chip_position,
        stone_position=stone_position,
    )


def parse_saved_stage_position(raw_position: object) -> SavedStagePositionConfig:
    """Normalise a persisted XYZ bookmark used by calibration workflows."""

    x_mm = 0.0
    y_mm = 0.0
    z_mm = 0.0
    configured = False
    if isinstance(raw_position, dict):
        x_mm = _coerce_float(raw_position.get("x_mm", x_mm), default=0.0)
        y_mm = _coerce_float(raw_position.get("y_mm", y_mm), default=0.0)
        z_mm = _coerce_float(raw_position.get("z_mm", z_mm), default=0.0)
        configured = bool(raw_position.get("configured", configured))
    return SavedStagePositionConfig(
        x_mm=x_mm,
        y_mm=y_mm,
        z_mm=z_mm,
        configured=configured,
    )


def _strip_string(value: object, *, default: str) -> str:
    if isinstance(value, str):
        return value.strip()
    return default


def _normalise_choice(value: object, *, choices: tuple, default: str) -> str:
    if isinstance(value, str):
        candidate = value.strip()
        for choice in choices:
            if candidate.upper() == str(choice).upper():
                return str(choice)
    return default


def _coerce_bool(value: object, *, default: bool) -> bool:
    if isinstance(value, str):
        return value.strip().lower() not in {"", "0", "false", "off", "no"}
    if value is None:
        return default
    return bool(value)


def _coerce_float(value: object, *, default: float) -> float:
    try:
        if isinstance(value, (int, float, str)):
            return float(value)
    except (TypeError, ValueError):
        pass
    return default


def _coerce_int(value: object, *, default: int) -> int:
    try:
        if isinstance(value, (int, float, str)):
            return int(float(value))
    except (TypeError, ValueError):
        pass
    return default
