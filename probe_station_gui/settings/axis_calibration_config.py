"""Pure axis calibration settings normalisation helpers."""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass, field

from probe_station_gui.settings.axis_calibration_npz import (
    LINEAR_INTERPOLATION_MODEL,
)
from probe_station_gui.settings.value_parsing import coerce_bool as _coerce_bool
from probe_station_gui.settings.value_parsing import coerce_float as _coerce_float


@dataclass
class AxisACalibrationConfig:
    """Normalised compact A-axis nonlinear calibration model."""

    configured: bool = False
    model: str = "cosine_displacement"
    calibration_file: str = ""
    interpolation_gcode_mm: list[float] = field(default_factory=list)
    interpolation_display_mm: list[float] = field(default_factory=list)
    interpolation_direction: int | None = None
    steps_per_mm: float = 2600.0
    commanded_lowering_min_mm: float = 0.0
    commanded_lowering_max_mm: float = 5.5
    offset_mm: float = -0.18025492860701603
    amplitude_mm: float = -4.256281153779931
    angular_frequency_rad_per_mm: float = 0.2560331555269034
    phase_rad: float = 0.9304927419233507
    fit_rmse_mm: float = 0.03390421874833405
    fit_max_abs_error_mm: float = 0.044862806662900656
    source: str = ""
    created_at: str = "2026-05-06T00:00:00+03:00"


@dataclass
class AxisZCalibrationConfig:
    """Normalised smooth Z-axis calibration model."""

    configured: bool = False
    model: str = "quintic_polynomial"
    calibration_file: str = ""
    interpolation_gcode_mm: list[float] = field(default_factory=list)
    interpolation_display_mm: list[float] = field(default_factory=list)
    interpolation_direction: int | None = None
    steps_per_mm: float = 6335.0
    gcode_min_mm: float = 0.02
    gcode_max_mm: float = 23.4
    coefficients_mm: list[float] = field(
        default_factory=lambda: [
            -1.1689194871855767e-06,
            6.252947738309964e-05,
            -0.0006221022578588869,
            0.015449946058775076,
            0.5416754463041403,
            0.00910614542389841,
        ]
    )
    fit_rmse_mm: float = 0.006126947872349345
    fit_max_abs_error_mm: float = 0.020464954405667868
    section2_indicator_offset_mm: float = 8.661368914604154
    section3_indicator_offset_mm: float = 13.56547962940159
    source: str = ""
    created_at: str = "2026-05-06T00:00:00+03:00"


@dataclass
class AxisACalibrationSettings(AxisACalibrationConfig):
    """Compact signed calibrated model for the nonlinear A-axis linkage."""

    def clone(self) -> "AxisACalibrationSettings":
        """Return a copy of the A-axis calibration model."""

        return AxisACalibrationSettings(**self.to_dict())

    def to_dict(self) -> dict[str, object]:
        """Serialize the A-axis calibration model."""

        data = dict(self.__dict__)
        data["interpolation_gcode_mm"] = list(self.interpolation_gcode_mm)
        data["interpolation_display_mm"] = list(self.interpolation_display_mm)
        return data


@dataclass
class AxisZCalibrationSettings(AxisZCalibrationConfig):
    """Smooth calibrated model for the measured Z-axis branches."""

    def clone(self) -> "AxisZCalibrationSettings":
        """Return a copy of the Z-axis calibration model."""

        return AxisZCalibrationSettings(**self.to_dict())

    def to_dict(self) -> dict[str, object]:
        """Serialize the Z-axis calibration model."""

        data = dict(self.__dict__)
        data["coefficients_mm"] = list(self.coefficients_mm)
        data["interpolation_gcode_mm"] = list(self.interpolation_gcode_mm)
        data["interpolation_display_mm"] = list(self.interpolation_display_mm)
        return data


def parse_axis_a_calibration(
    raw_calibration: object,
    defaults: AxisACalibrationConfig,
    *,
    expected_model: str,
) -> AxisACalibrationConfig:
    """Normalise the compact A-axis nonlinear calibration model."""

    if not isinstance(raw_calibration, dict):
        return _clone_axis_a(defaults)

    model = _strip_string(
        raw_calibration.get("model", defaults.model),
        default=defaults.model,
    )
    if model not in {expected_model, LINEAR_INTERPOLATION_MODEL}:
        model = expected_model

    interpolation_gcode_mm, interpolation_display_mm = _interpolation_points(
        raw_calibration.get(
            "interpolation_gcode_mm",
            defaults.interpolation_gcode_mm,
        ),
        raw_calibration.get(
            "interpolation_display_mm",
            defaults.interpolation_display_mm,
        ),
    )

    calibration = AxisACalibrationConfig(
        configured=_coerce_bool(
            raw_calibration.get("configured", defaults.configured),
            default=defaults.configured,
        ),
        model=model,
        calibration_file=_strip_string(
            raw_calibration.get("calibration_file", defaults.calibration_file),
            default=defaults.calibration_file,
        ),
        interpolation_gcode_mm=interpolation_gcode_mm,
        interpolation_display_mm=interpolation_display_mm,
        interpolation_direction=_interpolation_direction(
            raw_calibration.get(
                "interpolation_direction",
                defaults.interpolation_direction,
            )
        ),
        steps_per_mm=_coerce_float(
            raw_calibration.get("steps_per_mm", defaults.steps_per_mm),
            default=defaults.steps_per_mm,
        ),
        commanded_lowering_min_mm=_coerce_float(
            raw_calibration.get(
                "commanded_lowering_min_mm",
                defaults.commanded_lowering_min_mm,
            ),
            default=defaults.commanded_lowering_min_mm,
        ),
        commanded_lowering_max_mm=_coerce_float(
            raw_calibration.get(
                "commanded_lowering_max_mm",
                defaults.commanded_lowering_max_mm,
            ),
            default=defaults.commanded_lowering_max_mm,
        ),
        offset_mm=-abs(
            _coerce_float(
                raw_calibration.get("offset_mm", defaults.offset_mm),
                default=defaults.offset_mm,
            )
        ),
        amplitude_mm=-abs(
            _coerce_float(
                raw_calibration.get("amplitude_mm", defaults.amplitude_mm),
                default=defaults.amplitude_mm,
            )
        ),
        angular_frequency_rad_per_mm=_coerce_float(
            raw_calibration.get(
                "angular_frequency_rad_per_mm",
                defaults.angular_frequency_rad_per_mm,
            ),
            default=defaults.angular_frequency_rad_per_mm,
        ),
        phase_rad=_coerce_float(
            raw_calibration.get("phase_rad", defaults.phase_rad),
            default=defaults.phase_rad,
        ),
        fit_rmse_mm=_coerce_float(
            raw_calibration.get("fit_rmse_mm", defaults.fit_rmse_mm),
            default=defaults.fit_rmse_mm,
        ),
        fit_max_abs_error_mm=_coerce_float(
            raw_calibration.get(
                "fit_max_abs_error_mm",
                defaults.fit_max_abs_error_mm,
            ),
            default=defaults.fit_max_abs_error_mm,
        ),
        source=_normalized_source(
            raw_calibration.get("source", defaults.source),
            default=defaults.source,
        ),
        created_at=_strip_string(
            raw_calibration.get("created_at", defaults.created_at),
            default=defaults.created_at,
        ),
    )
    if calibration.model == LINEAR_INTERPOLATION_MODEL:
        if len(calibration.interpolation_gcode_mm) < 2:
            calibration.configured = False
    elif (
        calibration.steps_per_mm <= 0
        or calibration.commanded_lowering_max_mm
        <= calibration.commanded_lowering_min_mm
        or abs(calibration.amplitude_mm) <= 1e-12
        or calibration.angular_frequency_rad_per_mm <= 0
    ):
        calibration.configured = False
    return calibration


def parse_axis_z_calibration(
    raw_calibration: object,
    defaults: AxisZCalibrationConfig,
) -> AxisZCalibrationConfig:
    """Normalise the compact Z-axis nonlinear calibration model."""

    if not isinstance(raw_calibration, dict):
        return _clone_axis_z(defaults)

    model = _strip_string(
        raw_calibration.get("model", defaults.model),
        default=defaults.model,
    )
    if model not in {defaults.model, LINEAR_INTERPOLATION_MODEL}:
        model = defaults.model

    interpolation_gcode_mm, interpolation_display_mm = _interpolation_points(
        raw_calibration.get(
            "interpolation_gcode_mm",
            defaults.interpolation_gcode_mm,
        ),
        raw_calibration.get(
            "interpolation_display_mm",
            defaults.interpolation_display_mm,
        ),
    )

    calibration = AxisZCalibrationConfig(
        configured=_coerce_bool(
            raw_calibration.get("configured", defaults.configured),
            default=defaults.configured,
        ),
        model=model,
        calibration_file=_strip_string(
            raw_calibration.get("calibration_file", defaults.calibration_file),
            default=defaults.calibration_file,
        ),
        interpolation_gcode_mm=interpolation_gcode_mm,
        interpolation_display_mm=interpolation_display_mm,
        interpolation_direction=_interpolation_direction(
            raw_calibration.get(
                "interpolation_direction",
                defaults.interpolation_direction,
            )
        ),
        steps_per_mm=_coerce_float(
            raw_calibration.get("steps_per_mm", defaults.steps_per_mm),
            default=defaults.steps_per_mm,
        ),
        gcode_min_mm=_coerce_float(
            raw_calibration.get("gcode_min_mm", defaults.gcode_min_mm),
            default=defaults.gcode_min_mm,
        ),
        gcode_max_mm=_coerce_float(
            raw_calibration.get("gcode_max_mm", defaults.gcode_max_mm),
            default=defaults.gcode_max_mm,
        ),
        coefficients_mm=_coefficients(
            raw_calibration.get("coefficients_mm", defaults.coefficients_mm),
            fallback=defaults.coefficients_mm,
        ),
        fit_rmse_mm=_coerce_float(
            raw_calibration.get("fit_rmse_mm", defaults.fit_rmse_mm),
            default=defaults.fit_rmse_mm,
        ),
        fit_max_abs_error_mm=_coerce_float(
            raw_calibration.get(
                "fit_max_abs_error_mm",
                defaults.fit_max_abs_error_mm,
            ),
            default=defaults.fit_max_abs_error_mm,
        ),
        section2_indicator_offset_mm=_coerce_float(
            raw_calibration.get(
                "section2_indicator_offset_mm",
                defaults.section2_indicator_offset_mm,
            ),
            default=defaults.section2_indicator_offset_mm,
        ),
        section3_indicator_offset_mm=_coerce_float(
            raw_calibration.get(
                "section3_indicator_offset_mm",
                defaults.section3_indicator_offset_mm,
            ),
            default=defaults.section3_indicator_offset_mm,
        ),
        source=_normalized_source(
            raw_calibration.get("source", defaults.source),
            default=defaults.source,
        ),
        created_at=_strip_string(
            raw_calibration.get("created_at", defaults.created_at),
            default=defaults.created_at,
        ),
    )
    if calibration.model == LINEAR_INTERPOLATION_MODEL:
        if len(calibration.interpolation_gcode_mm) < 2:
            calibration.configured = False
    elif (
        calibration.steps_per_mm <= 0
        or calibration.gcode_max_mm <= calibration.gcode_min_mm
    ):
        calibration.configured = False
    return calibration


def _coefficients(raw_values: object, *, fallback: list[float]) -> list[float]:
    if not isinstance(raw_values, Iterable) or isinstance(raw_values, (str, bytes)):
        return list(fallback)
    values: list[float] = []
    for raw_value in raw_values:
        try:
            value = float(raw_value)
        except (TypeError, ValueError):
            return list(fallback)
        if not math.isfinite(value):
            return list(fallback)
        values.append(value)
    if len(values) != 6:
        return list(fallback)
    return values


def _interpolation_points(
    raw_gcode: object,
    raw_display: object,
) -> tuple[list[float], list[float]]:
    gcode = _finite_float_list(raw_gcode)
    display = _finite_float_list(raw_display)
    if (
        len(gcode) < 2
        or len(gcode) != len(display)
        or any(right <= left for left, right in zip(gcode, gcode[1:]))
        or any(right <= left for left, right in zip(display, display[1:]))
    ):
        return [], []
    return gcode, display


def _finite_float_list(raw_values: object) -> list[float]:
    if not isinstance(raw_values, Iterable) or isinstance(raw_values, (str, bytes)):
        return []
    values: list[float] = []
    for raw_value in raw_values:
        try:
            value = float(raw_value)
        except (TypeError, ValueError):
            return []
        if not math.isfinite(value):
            return []
        values.append(value)
    return values


def _interpolation_direction(raw_direction: object) -> int | None:
    if isinstance(raw_direction, bool) or raw_direction not in (-1, 1):
        return None
    return int(raw_direction)


def _clone_axis_a(defaults: AxisACalibrationConfig) -> AxisACalibrationConfig:
    data = dict(defaults.__dict__)
    data["interpolation_gcode_mm"] = list(defaults.interpolation_gcode_mm)
    data["interpolation_display_mm"] = list(defaults.interpolation_display_mm)
    return AxisACalibrationConfig(**data)


def _clone_axis_z(defaults: AxisZCalibrationConfig) -> AxisZCalibrationConfig:
    data = dict(defaults.__dict__)
    data["coefficients_mm"] = list(defaults.coefficients_mm)
    data["interpolation_gcode_mm"] = list(defaults.interpolation_gcode_mm)
    data["interpolation_display_mm"] = list(defaults.interpolation_display_mm)
    return AxisZCalibrationConfig(**data)


def _strip_string(value: object, *, default: str) -> str:
    if isinstance(value, str):
        return value.strip()
    return default


def _normalized_source(value: object, *, default: str) -> str:
    source = _strip_string(value, default=default)
    return "" if source.lower().endswith(".png") else source
