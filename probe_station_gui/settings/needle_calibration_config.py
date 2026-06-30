"""Pure needle calibration settings normalisation helpers."""

from __future__ import annotations

from dataclasses import dataclass, field, replace

from probe_station_gui.settings.value_parsing import coerce_bool as _coerce_bool
from probe_station_gui.settings.value_parsing import coerce_float as _coerce_float
from probe_station_gui.settings.value_parsing import coerce_int as _coerce_int
from probe_station_gui.settings.value_parsing import normalise_choice as _normalise_choice


LCR_MEASUREMENT_FUNCTIONS: tuple[str, ...] = (
    "Cs-Rs",
    "Cs-D",
    "Cp-Rp",
    "Cp-D",
    "Lp-Rp",
    "Lp-Q",
    "Ls-Rs",
    "Ls-Q",
    "Rs-Q",
    "Rp-Q",
    "R-X",
    "DCR",
    "Z-thr",
    "Z-thd",
    "Z-D",
    "Z-Q",
)
LCR_RANGE_MODES: tuple[str, ...] = ("HOLD", "AUTO")
LCR_LEVEL_MODES: tuple[str, ...] = ("VOLTAGE", "CURRENT")
LCR_APERTURE_RATES: tuple[str, ...] = ("FAST", "MED", "SLOW")
LCR_TRIGGER_SOURCES: tuple[str, ...] = ("INT", "MAN", "EXT", "BUS")
LCR_SOURCE_RESISTANCES_OHM: tuple[int, ...] = (30, 50, 100)
LCR_MONITOR_PARAMETERS: tuple[str, ...] = (
    "OFF",
    "Z",
    "D",
    "Q",
    "THR",
    "THD",
    "R",
    "X",
    "G",
    "B",
    "Y",
    "ABS",
    "PER",
    "VAC",
    "IAC",
)
LCR_METER_TYPE_GWINSTEK = "gwinstek_lcr_76200"
LCR_METER_TYPE_KEITHLEY = "keithley_2400_2182a"
LCR_METER_TYPES: tuple[str, ...] = (
    LCR_METER_TYPE_GWINSTEK,
    LCR_METER_TYPE_KEITHLEY,
)
LCR_METER_TYPE_LABELS: dict[str, str] = {
    LCR_METER_TYPE_GWINSTEK: "GW Instek LCR-76200",
    LCR_METER_TYPE_KEITHLEY: "Keithley 2400 + 2182A",
}


@dataclass(frozen=True)
class SavedStagePositionConfig:
    """Normalised XYZ bookmark used by calibration workflows."""

    x_mm: float = 0.0
    y_mm: float = 0.0
    z_mm: float = 0.0
    configured: bool = False


@dataclass
class SavedStagePositionSettings:
    """A persisted XYZ bookmark used by calibration workflows."""

    x_mm: float = 0.0
    y_mm: float = 0.0
    z_mm: float = 0.0
    configured: bool = False

    def clone(self) -> "SavedStagePositionSettings":
        """Return a copy of the saved XYZ bookmark."""

        return SavedStagePositionSettings(
            x_mm=self.x_mm,
            y_mm=self.y_mm,
            z_mm=self.z_mm,
            configured=self.configured,
        )

    def to_dict(self) -> dict[str, float | bool]:
        """Serialize the saved XYZ bookmark."""

        return {
            "x_mm": self.x_mm,
            "y_mm": self.y_mm,
            "z_mm": self.z_mm,
            "configured": self.configured,
        }


@dataclass
class NeedleCalibrationSettings:
    """Configuration for needle control and the external measurement instrument."""

    meter_type: str = LCR_METER_TYPE_GWINSTEK
    visa_resource: str = "COM4"
    keithley_source_resource: str = "GPIB2::1::INSTR"
    keithley_voltmeter_resource: str = "GPIB2::2::INSTR"
    measurement_function: str = "R-X"
    range_mode: str = "AUTO"
    auto_range_enabled: bool = True
    impedance_range: int = 3
    dcr_range: int = 4
    frequency_hz: float = 50.0
    level_mode: str = "VOLTAGE"
    voltage_level_v: float = 0.01
    current_level_a: float = 0.0001
    source_resistance_ohm: int = 100
    aperture_rate: str = "SLOW"
    aperture_averages: int = 1
    trigger_source: str = "INT"
    trigger_delay_s: float = 0.0
    bias_enabled: bool = False
    bias_level_v: float = 0.0
    monitor1: str = "OFF"
    monitor2: str = "OFF"
    alc_enabled: bool = False
    short_threshold_ohm: float = 10.0
    poll_interval_ms: int = 250
    feedrate_mm_min: float = 1.0
    contact_zone_mm: float = 0.05
    raise_position_mm: float = 0.0
    raise_position_configured: bool = False
    down_position_mm: float = 0.0
    down_position_configured: bool = False
    chip_position: SavedStagePositionSettings = field(
        default_factory=SavedStagePositionSettings
    )
    stone_position: SavedStagePositionSettings = field(
        default_factory=SavedStagePositionSettings
    )

    def clone(self) -> "NeedleCalibrationSettings":
        """Return a copy of the needle calibration settings."""

        return NeedleCalibrationSettings(
            meter_type=self.meter_type,
            visa_resource=self.visa_resource,
            keithley_source_resource=self.keithley_source_resource,
            keithley_voltmeter_resource=self.keithley_voltmeter_resource,
            measurement_function=self.measurement_function,
            range_mode=self.range_mode,
            auto_range_enabled=self.auto_range_enabled,
            impedance_range=self.impedance_range,
            dcr_range=self.dcr_range,
            frequency_hz=self.frequency_hz,
            level_mode=self.level_mode,
            voltage_level_v=self.voltage_level_v,
            current_level_a=self.current_level_a,
            source_resistance_ohm=self.source_resistance_ohm,
            aperture_rate=self.aperture_rate,
            aperture_averages=self.aperture_averages,
            trigger_source=self.trigger_source,
            trigger_delay_s=self.trigger_delay_s,
            bias_enabled=self.bias_enabled,
            bias_level_v=self.bias_level_v,
            monitor1=self.monitor1,
            monitor2=self.monitor2,
            alc_enabled=self.alc_enabled,
            short_threshold_ohm=self.short_threshold_ohm,
            poll_interval_ms=self.poll_interval_ms,
            feedrate_mm_min=self.feedrate_mm_min,
            contact_zone_mm=self.contact_zone_mm,
            raise_position_mm=self.raise_position_mm,
            raise_position_configured=self.raise_position_configured,
            down_position_mm=self.down_position_mm,
            down_position_configured=self.down_position_configured,
            chip_position=self.chip_position.clone(),
            stone_position=self.stone_position.clone(),
        )

    def to_dict(self) -> dict[str, float | int | str | bool | dict[str, float | bool]]:
        """Serialize the needle calibration preferences."""

        return {
            "meter_type": self.meter_type,
            "visa_resource": self.visa_resource,
            "keithley_source_resource": self.keithley_source_resource,
            "keithley_voltmeter_resource": self.keithley_voltmeter_resource,
            "measurement_function": self.measurement_function,
            "range_mode": self.range_mode,
            "auto_range_enabled": self.auto_range_enabled,
            "impedance_range": self.impedance_range,
            "dcr_range": self.dcr_range,
            "frequency_hz": self.frequency_hz,
            "level_mode": self.level_mode,
            "voltage_level_v": self.voltage_level_v,
            "current_level_a": self.current_level_a,
            "source_resistance_ohm": self.source_resistance_ohm,
            "aperture_rate": self.aperture_rate,
            "aperture_averages": self.aperture_averages,
            "trigger_source": self.trigger_source,
            "trigger_delay_s": self.trigger_delay_s,
            "bias_enabled": self.bias_enabled,
            "bias_level_v": self.bias_level_v,
            "monitor1": self.monitor1,
            "monitor2": self.monitor2,
            "alc_enabled": self.alc_enabled,
            "short_threshold_ohm": self.short_threshold_ohm,
            "poll_interval_ms": self.poll_interval_ms,
            "feedrate_mm_min": self.feedrate_mm_min,
            "contact_zone_mm": self.contact_zone_mm,
            "raise_position_mm": self.raise_position_mm,
            "raise_position_configured": self.raise_position_configured,
            "down_position_mm": self.down_position_mm,
            "down_position_configured": self.down_position_configured,
            "chip_position": self.chip_position.to_dict(),
            "stone_position": self.stone_position.to_dict(),
        }


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

    config = _default_needle_calibration_config(defaults)
    if isinstance(raw_needle_calibration, dict):
        config = _needle_calibration_config_from_raw(
            raw_needle_calibration,
            config,
            defaults,
        )
    return _validated_needle_calibration_config(config, defaults)


def _default_needle_calibration_config(
    defaults: NeedleCalibrationDefaults,
) -> NeedleCalibrationConfig:
    return NeedleCalibrationConfig(
        meter_type=defaults.meter_type,
        visa_resource=defaults.visa_resource,
        keithley_source_resource=defaults.keithley_source_resource,
        keithley_voltmeter_resource=defaults.keithley_voltmeter_resource,
        measurement_function=defaults.measurement_function,
        range_mode=defaults.range_mode,
        auto_range_enabled=defaults.auto_range_enabled,
        impedance_range=defaults.impedance_range,
        dcr_range=defaults.dcr_range,
        frequency_hz=defaults.frequency_hz,
        level_mode=defaults.level_mode,
        voltage_level_v=defaults.voltage_level_v,
        current_level_a=defaults.current_level_a,
        source_resistance_ohm=defaults.source_resistance_ohm,
        aperture_rate=defaults.aperture_rate,
        aperture_averages=defaults.aperture_averages,
        trigger_source=defaults.trigger_source,
        trigger_delay_s=defaults.trigger_delay_s,
        bias_enabled=defaults.bias_enabled,
        bias_level_v=defaults.bias_level_v,
        monitor1=defaults.monitor,
        monitor2=defaults.monitor,
        alc_enabled=defaults.alc_enabled,
        short_threshold_ohm=defaults.short_threshold_ohm,
        poll_interval_ms=defaults.poll_interval_ms,
        feedrate_mm_min=defaults.feedrate_mm_min,
        contact_zone_mm=defaults.contact_zone_mm,
        raise_position_mm=0.0,
        raise_position_configured=False,
        down_position_mm=0.0,
        down_position_configured=False,
        chip_position=SavedStagePositionConfig(),
        stone_position=SavedStagePositionConfig(),
    )


def _needle_calibration_config_from_raw(
    raw_needle_calibration: dict,
    config: NeedleCalibrationConfig,
    defaults: NeedleCalibrationDefaults,
) -> NeedleCalibrationConfig:
    return NeedleCalibrationConfig(
        meter_type=_normalise_choice(
            raw_needle_calibration.get("meter_type", config.meter_type),
            choices=defaults.meter_types,
            default=config.meter_type,
        ),
        visa_resource=_strip_string(
            raw_needle_calibration.get("visa_resource", config.visa_resource),
            default=config.visa_resource,
        ),
        keithley_source_resource=_strip_string(
            raw_needle_calibration.get(
                "keithley_source_resource",
                config.keithley_source_resource,
            ),
            default=config.keithley_source_resource,
        ),
        keithley_voltmeter_resource=_strip_string(
            raw_needle_calibration.get(
                "keithley_voltmeter_resource",
                config.keithley_voltmeter_resource,
            ),
            default=config.keithley_voltmeter_resource,
        ),
        measurement_function=_normalise_choice(
            raw_needle_calibration.get(
                "measurement_function",
                config.measurement_function,
            ),
            choices=defaults.measurement_functions,
            default=config.measurement_function,
        ),
        range_mode=_normalise_choice(
            raw_needle_calibration.get("range_mode", config.range_mode),
            choices=defaults.range_modes,
            default=config.range_mode,
        ),
        auto_range_enabled=_coerce_bool(
            raw_needle_calibration.get(
                "auto_range_enabled",
                config.auto_range_enabled,
            ),
            default=defaults.auto_range_enabled,
        ),
        impedance_range=_coerce_int(
            raw_needle_calibration.get("impedance_range", config.impedance_range),
            default=defaults.impedance_range,
        ),
        dcr_range=_coerce_int(
            raw_needle_calibration.get("dcr_range", config.dcr_range),
            default=defaults.dcr_range,
        ),
        frequency_hz=_coerce_float(
            raw_needle_calibration.get("frequency_hz", config.frequency_hz),
            default=defaults.frequency_hz,
        ),
        level_mode=_normalise_choice(
            raw_needle_calibration.get("level_mode", config.level_mode),
            choices=defaults.level_modes,
            default=config.level_mode,
        ),
        voltage_level_v=_coerce_float(
            raw_needle_calibration.get("voltage_level_v", config.voltage_level_v),
            default=defaults.voltage_level_v,
        ),
        current_level_a=_coerce_float(
            raw_needle_calibration.get("current_level_a", config.current_level_a),
            default=defaults.current_level_a,
        ),
        source_resistance_ohm=_coerce_int(
            raw_needle_calibration.get(
                "source_resistance_ohm",
                config.source_resistance_ohm,
            ),
            default=defaults.source_resistance_ohm,
        ),
        aperture_rate=_normalise_choice(
            raw_needle_calibration.get("aperture_rate", config.aperture_rate),
            choices=defaults.aperture_rates,
            default=config.aperture_rate,
        ),
        aperture_averages=_coerce_int(
            raw_needle_calibration.get(
                "aperture_averages",
                config.aperture_averages,
            ),
            default=defaults.aperture_averages,
        ),
        trigger_source=_normalise_choice(
            raw_needle_calibration.get("trigger_source", config.trigger_source),
            choices=defaults.trigger_sources,
            default=config.trigger_source,
        ),
        trigger_delay_s=_coerce_float(
            raw_needle_calibration.get("trigger_delay_s", config.trigger_delay_s),
            default=defaults.trigger_delay_s,
        ),
        bias_enabled=_coerce_bool(
            raw_needle_calibration.get("bias_enabled", config.bias_enabled),
            default=defaults.bias_enabled,
        ),
        bias_level_v=_coerce_float(
            raw_needle_calibration.get("bias_level_v", config.bias_level_v),
            default=defaults.bias_level_v,
        ),
        monitor1=_normalise_choice(
            raw_needle_calibration.get("monitor1", config.monitor1),
            choices=defaults.monitor_parameters,
            default=config.monitor1,
        ),
        monitor2=_normalise_choice(
            raw_needle_calibration.get("monitor2", config.monitor2),
            choices=defaults.monitor_parameters,
            default=config.monitor2,
        ),
        alc_enabled=_coerce_bool(
            raw_needle_calibration.get("alc_enabled", config.alc_enabled),
            default=defaults.alc_enabled,
        ),
        short_threshold_ohm=_coerce_float(
            raw_needle_calibration.get(
                "short_threshold_ohm",
                config.short_threshold_ohm,
            ),
            default=defaults.short_threshold_ohm,
        ),
        poll_interval_ms=_coerce_int(
            raw_needle_calibration.get("poll_interval_ms", config.poll_interval_ms),
            default=defaults.poll_interval_ms,
        ),
        feedrate_mm_min=_coerce_float(
            raw_needle_calibration.get("feedrate_mm_min", config.feedrate_mm_min),
            default=defaults.feedrate_mm_min,
        ),
        contact_zone_mm=_coerce_float(
            raw_needle_calibration.get("contact_zone_mm", config.contact_zone_mm),
            default=defaults.contact_zone_mm,
        ),
        raise_position_mm=_coerce_float(
            raw_needle_calibration.get("raise_position_mm", config.raise_position_mm),
            default=0.0,
        ),
        raise_position_configured=bool(
            raw_needle_calibration.get(
                "raise_position_configured",
                config.raise_position_configured,
            )
        ),
        down_position_mm=_coerce_float(
            raw_needle_calibration.get("down_position_mm", config.down_position_mm),
            default=0.0,
        ),
        down_position_configured=bool(
            raw_needle_calibration.get(
                "down_position_configured",
                config.down_position_configured,
            )
        ),
        chip_position=parse_saved_stage_position(
            raw_needle_calibration.get("chip_position")
        ),
        stone_position=parse_saved_stage_position(
            raw_needle_calibration.get("stone_position")
        ),
    )


def _validated_needle_calibration_config(
    config: NeedleCalibrationConfig,
    defaults: NeedleCalibrationDefaults,
) -> NeedleCalibrationConfig:
    config = _validated_lcr_range_config(config, defaults)
    config = _validated_timing_and_threshold_config(config, defaults)
    return _validated_needle_motion_config(config, defaults)


def _validated_lcr_range_config(
    config: NeedleCalibrationConfig,
    defaults: NeedleCalibrationDefaults,
) -> NeedleCalibrationConfig:
    impedance_range = config.impedance_range
    dcr_range = config.dcr_range
    frequency_hz = config.frequency_hz
    voltage_level_v = config.voltage_level_v
    current_level_a = config.current_level_a
    source_resistance_ohm = config.source_resistance_ohm
    aperture_averages = config.aperture_averages
    if _outside(impedance_range, 0, 8):
        impedance_range = defaults.impedance_range
    if _outside(dcr_range, 0, 8):
        dcr_range = defaults.dcr_range
    if frequency_hz < 10:
        frequency_hz = defaults.frequency_hz
    if _outside(voltage_level_v, 0.01, 2.0):
        voltage_level_v = defaults.voltage_level_v
    if _outside(current_level_a, 0.0001, 0.02):
        current_level_a = defaults.current_level_a
    if source_resistance_ohm not in defaults.source_resistances_ohm:
        source_resistance_ohm = defaults.source_resistance_ohm
    if _outside(aperture_averages, 1, 256):
        aperture_averages = defaults.aperture_averages
    return replace(
        config,
        impedance_range=impedance_range,
        dcr_range=dcr_range,
        frequency_hz=frequency_hz,
        voltage_level_v=voltage_level_v,
        current_level_a=current_level_a,
        source_resistance_ohm=source_resistance_ohm,
        aperture_averages=aperture_averages,
    )


def _validated_timing_and_threshold_config(
    config: NeedleCalibrationConfig,
    defaults: NeedleCalibrationDefaults,
) -> NeedleCalibrationConfig:
    trigger_delay_s = config.trigger_delay_s
    bias_level_v = config.bias_level_v
    short_threshold_ohm = config.short_threshold_ohm
    poll_interval_ms = config.poll_interval_ms
    if _outside(trigger_delay_s, 0, 60):
        trigger_delay_s = defaults.trigger_delay_s
    if _outside(bias_level_v, -2.5, 2.5):
        bias_level_v = defaults.bias_level_v
    if short_threshold_ohm < 0:
        short_threshold_ohm = defaults.short_threshold_ohm
    if poll_interval_ms < 50:
        poll_interval_ms = defaults.poll_interval_ms
    return replace(
        config,
        trigger_delay_s=trigger_delay_s,
        bias_level_v=bias_level_v,
        short_threshold_ohm=short_threshold_ohm,
        poll_interval_ms=poll_interval_ms,
    )


def _validated_needle_motion_config(
    config: NeedleCalibrationConfig,
    defaults: NeedleCalibrationDefaults,
) -> NeedleCalibrationConfig:
    feedrate_mm_min = config.feedrate_mm_min
    contact_zone_mm = config.contact_zone_mm
    raise_position_mm = config.raise_position_mm
    raise_position_configured = config.raise_position_configured
    if feedrate_mm_min <= 0:
        feedrate_mm_min = defaults.feedrate_mm_min
    feedrate_mm_min = max(defaults.min_feedrate_mm_min, feedrate_mm_min)
    if contact_zone_mm < 0:
        contact_zone_mm = defaults.contact_zone_mm
    if not raise_position_configured and config.down_position_configured:
        raise_position_mm = config.down_position_mm
        raise_position_configured = True
    return replace(
        config,
        auto_range_enabled=config.range_mode == "AUTO",
        feedrate_mm_min=feedrate_mm_min,
        contact_zone_mm=contact_zone_mm,
        raise_position_mm=raise_position_mm,
        raise_position_configured=raise_position_configured,
    )


def _outside(value: float, lower: float, upper: float) -> bool:
    return value < lower or value > upper


def parse_needle_calibration_preferences(
    raw_needle_calibration: object,
    *,
    min_feedrate_mm_min: float,
) -> NeedleCalibrationSettings:
    """Normalise persisted needle calibration settings for the GUI settings model."""

    config = parse_needle_calibration_settings(
        raw_needle_calibration,
        default_needle_calibration_values(
            min_feedrate_mm_min=min_feedrate_mm_min,
        ),
    )
    return needle_calibration_settings_from_config(config)


def default_needle_calibration_values(
    *,
    min_feedrate_mm_min: float,
) -> NeedleCalibrationDefaults:
    """Return default values and constraints for needle calibration parsing."""

    defaults = NeedleCalibrationSettings()
    return NeedleCalibrationDefaults(
        meter_type=defaults.meter_type,
        visa_resource=defaults.visa_resource,
        keithley_source_resource=defaults.keithley_source_resource,
        keithley_voltmeter_resource=defaults.keithley_voltmeter_resource,
        measurement_function=defaults.measurement_function,
        range_mode=defaults.range_mode,
        auto_range_enabled=defaults.auto_range_enabled,
        impedance_range=defaults.impedance_range,
        dcr_range=defaults.dcr_range,
        frequency_hz=defaults.frequency_hz,
        level_mode=defaults.level_mode,
        voltage_level_v=defaults.voltage_level_v,
        current_level_a=defaults.current_level_a,
        source_resistance_ohm=defaults.source_resistance_ohm,
        aperture_rate=defaults.aperture_rate,
        aperture_averages=defaults.aperture_averages,
        trigger_source=defaults.trigger_source,
        trigger_delay_s=defaults.trigger_delay_s,
        bias_enabled=defaults.bias_enabled,
        bias_level_v=defaults.bias_level_v,
        monitor=defaults.monitor1,
        alc_enabled=defaults.alc_enabled,
        short_threshold_ohm=defaults.short_threshold_ohm,
        poll_interval_ms=defaults.poll_interval_ms,
        feedrate_mm_min=defaults.feedrate_mm_min,
        contact_zone_mm=defaults.contact_zone_mm,
        min_feedrate_mm_min=min_feedrate_mm_min,
        meter_types=LCR_METER_TYPES,
        measurement_functions=LCR_MEASUREMENT_FUNCTIONS,
        range_modes=LCR_RANGE_MODES,
        level_modes=LCR_LEVEL_MODES,
        source_resistances_ohm=LCR_SOURCE_RESISTANCES_OHM,
        aperture_rates=LCR_APERTURE_RATES,
        trigger_sources=LCR_TRIGGER_SOURCES,
        monitor_parameters=LCR_MONITOR_PARAMETERS,
    )


def needle_calibration_settings_from_config(
    config: NeedleCalibrationConfig,
) -> NeedleCalibrationSettings:
    """Convert normalised needle calibration data to mutable settings."""

    return NeedleCalibrationSettings(
        meter_type=config.meter_type,
        visa_resource=config.visa_resource,
        keithley_source_resource=config.keithley_source_resource,
        keithley_voltmeter_resource=config.keithley_voltmeter_resource,
        measurement_function=config.measurement_function,
        range_mode=config.range_mode,
        auto_range_enabled=config.auto_range_enabled,
        impedance_range=config.impedance_range,
        dcr_range=config.dcr_range,
        frequency_hz=config.frequency_hz,
        level_mode=config.level_mode,
        voltage_level_v=config.voltage_level_v,
        current_level_a=config.current_level_a,
        source_resistance_ohm=config.source_resistance_ohm,
        aperture_rate=config.aperture_rate,
        aperture_averages=config.aperture_averages,
        trigger_source=config.trigger_source,
        trigger_delay_s=config.trigger_delay_s,
        bias_enabled=config.bias_enabled,
        bias_level_v=config.bias_level_v,
        monitor1=config.monitor1,
        monitor2=config.monitor2,
        alc_enabled=config.alc_enabled,
        short_threshold_ohm=config.short_threshold_ohm,
        poll_interval_ms=config.poll_interval_ms,
        feedrate_mm_min=config.feedrate_mm_min,
        contact_zone_mm=config.contact_zone_mm,
        raise_position_mm=config.raise_position_mm,
        raise_position_configured=config.raise_position_configured,
        down_position_mm=config.down_position_mm,
        down_position_configured=config.down_position_configured,
        chip_position=saved_stage_position_settings_from_config(
            config.chip_position
        ),
        stone_position=saved_stage_position_settings_from_config(
            config.stone_position
        ),
    )


def saved_stage_position_settings_from_config(
    config: SavedStagePositionConfig,
) -> SavedStagePositionSettings:
    """Convert a normalised saved stage position to mutable settings."""

    return SavedStagePositionSettings(
        x_mm=config.x_mm,
        y_mm=config.y_mm,
        z_mm=config.z_mm,
        configured=config.configured,
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
