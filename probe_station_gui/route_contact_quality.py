"""Contact-quality rules for route measurements."""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from typing import Collection


def _finite_nonnegative_or_default(value: object, default: float) -> float:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return float(default)
    if math.isfinite(numeric) and numeric >= 0.0:
        return numeric
    return float(default)


@dataclass(frozen=True)
class RouteContactQualityLimits:
    """Configurable thresholds used to classify repeated contact samples."""

    max_mad_sigma_ohm: float = 300.0
    max_p95_abs_step_ohm: float = 1_000.0
    max_relative_mad_sigma: float = 0.02
    max_relative_p95_abs_step: float = 0.05

    def normalized(self) -> "RouteContactQualityLimits":
        defaults = type(self)()
        return type(self)(
            max_mad_sigma_ohm=_finite_nonnegative_or_default(
                self.max_mad_sigma_ohm,
                defaults.max_mad_sigma_ohm,
            ),
            max_p95_abs_step_ohm=_finite_nonnegative_or_default(
                self.max_p95_abs_step_ohm,
                defaults.max_p95_abs_step_ohm,
            ),
            max_relative_mad_sigma=_finite_nonnegative_or_default(
                self.max_relative_mad_sigma,
                defaults.max_relative_mad_sigma,
            ),
            max_relative_p95_abs_step=_finite_nonnegative_or_default(
                self.max_relative_p95_abs_step,
                defaults.max_relative_p95_abs_step,
            ),
        )

    def as_dict(self) -> dict[str, float]:
        normalized = self.normalized()
        return {
            "max_mad_sigma_ohm": float(normalized.max_mad_sigma_ohm),
            "max_p95_abs_step_ohm": float(normalized.max_p95_abs_step_ohm),
            "max_relative_mad_sigma": float(normalized.max_relative_mad_sigma),
            "max_relative_p95_abs_step": float(
                normalized.max_relative_p95_abs_step
            ),
        }


@dataclass(frozen=True)
class RouteContactQuality:
    """Contact quality metrics calculated from repeated route samples."""

    assessed: bool
    good: bool | None
    status: str
    median_ohm: float = math.nan
    mad_sigma_ohm: float = math.nan
    p95_abs_step_ohm: float = math.nan
    span_ohm: float = math.nan
    compliance_hits: int = 0
    polarity_sign_mismatch_count: int = 0
    reasons: tuple[str, ...] = ()
    failure_criteria: tuple[str, ...] = ()


@dataclass(frozen=True)
class RouteMeasurementSample:
    """One repeated raw route measurement, including optional polarity details."""

    sample_index: int
    differential_resistance_ohm: float
    compliance_hit: bool = False
    negative_source_voltage_v: float | None = None
    negative_measured_voltage_v: float | None = None
    negative_current_a: float | None = None
    negative_resistance_ohm: float | None = None
    positive_source_voltage_v: float | None = None
    positive_measured_voltage_v: float | None = None
    positive_current_a: float | None = None
    positive_resistance_ohm: float | None = None


def _format_percent(value: float) -> str:
    if not math.isfinite(value):
        return "nan%"
    return f"{float(value) * 100.0:.3g}%"


def _format_ohm(value: float) -> str:
    if not math.isfinite(value):
        return "nan Ohm"
    abs_value = abs(value)
    for scale, unit in (
        (1e9, "GOhm"),
        (1e6, "MOhm"),
        (1e3, "kOhm"),
        (1.0, "Ohm"),
        (1e-3, "mOhm"),
        (1e-6, "uOhm"),
    ):
        if abs_value >= scale:
            return f"{value / scale:.3g} {unit}"
    return f"{value:.3g} Ohm"


def _contact_quality_failure_parts(
    *,
    median_ohm: float,
    mad_sigma_ohm: float,
    p95_abs_step_ohm: float,
    reasons: Collection[str],
    limits: RouteContactQualityLimits,
) -> list[str]:
    normalized_limits = limits.normalized()
    scale_ohm = max(abs(float(median_ohm)), 1.0)
    parts: list[str] = []
    reason_set = {str(reason) for reason in reasons}
    if "mad_sigma_too_high" in reason_set:
        relative = float(mad_sigma_ohm) / scale_ohm
        parts.append(
            "MAD sigma "
            f"{_format_ohm(float(mad_sigma_ohm))} > "
            f"{_format_ohm(normalized_limits.max_mad_sigma_ohm)} "
            "and relative MAD "
            f"{_format_percent(relative)} > "
            f"{_format_percent(normalized_limits.max_relative_mad_sigma)}"
        )
    if "step_noise_too_high" in reason_set:
        relative = float(p95_abs_step_ohm) / scale_ohm
        parts.append(
            "p95 step "
            f"{_format_ohm(float(p95_abs_step_ohm))} > "
            f"{_format_ohm(normalized_limits.max_p95_abs_step_ohm)} "
            "and relative p95 step "
            f"{_format_percent(relative)} > "
            f"{_format_percent(normalized_limits.max_relative_p95_abs_step)}"
        )
    return parts


def _format_contact_quality_failure(
    quality: RouteContactQuality,
    limits: RouteContactQualityLimits,
) -> str:
    if quality.good is not False:
        return ""
    parts = list(quality.failure_criteria)
    if not parts:
        parts = _contact_quality_failure_parts(
            median_ohm=float(quality.median_ohm),
            mad_sigma_ohm=float(quality.mad_sigma_ohm),
            p95_abs_step_ohm=float(quality.p95_abs_step_ohm),
            reasons=quality.reasons,
            limits=limits,
        )
    return "; ".join(parts)


def _contact_quality_from_samples(
    samples: list[RouteMeasurementSample] | tuple[RouteMeasurementSample, ...],
    *,
    contact_quality_limits: RouteContactQualityLimits | None = None,
) -> RouteContactQuality:
    limits = (contact_quality_limits or RouteContactQualityLimits()).normalized()
    finite_values = [
        float(sample.differential_resistance_ohm)
        for sample in samples
        if math.isfinite(float(sample.differential_resistance_ohm))
    ]
    compliance_hits = sum(1 for sample in samples if sample.compliance_hit)
    polarity_mismatches = sum(
        1 for sample in samples if _sample_has_polarity_sign_mismatch(sample)
    )
    if len(finite_values) < 2:
        return RouteContactQuality(
            assessed=False,
            good=None,
            status="unchecked",
            compliance_hits=compliance_hits,
            polarity_sign_mismatch_count=polarity_mismatches,
            reasons=("too_few_readings",),
        )

    sorted_values = sorted(finite_values)
    median = _percentile(sorted_values, 50.0)
    abs_deviations = sorted(abs(value - median) for value in finite_values)
    mad_sigma = 1.4826 * _percentile(abs_deviations, 50.0)
    abs_steps = sorted(
        abs(finite_values[index] - finite_values[index - 1])
        for index in range(1, len(finite_values))
    )
    p95_abs_step = _percentile(abs_steps, 95.0) if abs_steps else 0.0
    span = max(finite_values) - min(finite_values)

    reasons: list[str] = []
    scale_ohm = max(abs(median), 1.0)
    relative_mad_sigma = mad_sigma / scale_ohm
    relative_p95_abs_step = p95_abs_step / scale_ohm

    if (
        mad_sigma > limits.max_mad_sigma_ohm
        and relative_mad_sigma > limits.max_relative_mad_sigma
    ):
        reasons.append("mad_sigma_too_high")
    if (
        p95_abs_step > limits.max_p95_abs_step_ohm
        and relative_p95_abs_step > limits.max_relative_p95_abs_step
    ):
        reasons.append("step_noise_too_high")
    failure_criteria = tuple(
        _contact_quality_failure_parts(
            median_ohm=median,
            mad_sigma_ohm=mad_sigma,
            p95_abs_step_ohm=p95_abs_step,
            reasons=reasons,
            limits=limits,
        )
    )

    good = not reasons
    return RouteContactQuality(
        assessed=True,
        good=good,
        status="good" if good else "bad_contact",
        median_ohm=median,
        mad_sigma_ohm=mad_sigma,
        p95_abs_step_ohm=p95_abs_step,
        span_ohm=span,
        compliance_hits=compliance_hits,
        polarity_sign_mismatch_count=polarity_mismatches,
        reasons=tuple(reasons),
        failure_criteria=failure_criteria,
    )


def _sample_has_polarity_sign_mismatch(sample: RouteMeasurementSample) -> bool:
    negative_current = sample.negative_current_a
    positive_current = sample.positive_current_a
    if negative_current is None or positive_current is None:
        return False
    if not math.isfinite(negative_current) or not math.isfinite(positive_current):
        return False
    return negative_current * positive_current >= 0.0


def _percentile(sorted_values: list[float], percentile: float) -> float:
    if not sorted_values:
        return math.nan
    if len(sorted_values) == 1:
        return float(sorted_values[0])
    bounded = max(0.0, min(100.0, float(percentile)))
    position = (len(sorted_values) - 1) * bounded / 100.0
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return float(sorted_values[lower])
    fraction = position - lower
    return float(
        sorted_values[lower]
        + (sorted_values[upper] - sorted_values[lower]) * fraction
    )


def _measurement_sample_from_raw(
    raw: object,
    sample_index: int,
) -> RouteMeasurementSample:
    if isinstance(raw, RouteMeasurementSample):
        return replace(raw, sample_index=sample_index)
    if isinstance(raw, dict):
        negative = raw.get("negative")
        positive = raw.get("positive")
        differential = _raw_float(
            raw,
            "differential_resistance_ohm",
            "primary_value",
            default=math.nan,
        )
        negative_data = negative if isinstance(negative, dict) else {}
        positive_data = positive if isinstance(positive, dict) else {}
        return RouteMeasurementSample(
            sample_index=sample_index,
            differential_resistance_ohm=differential,
            compliance_hit=_raw_bool(
                raw,
                "compliance_hit",
                "short_detected",
                default=False,
            ),
            negative_source_voltage_v=_raw_float_or_none(
                negative_data,
                "source_voltage_v",
                "bias_voltage_v",
            ),
            negative_measured_voltage_v=_raw_float_or_none(
                negative_data,
                "measured_voltage_v",
                "voltage_v",
            ),
            negative_current_a=_raw_float_or_none(
                negative_data,
                "current_a",
                "measured_current_a",
            ),
            negative_resistance_ohm=_raw_float_or_none(
                negative_data,
                "resistance_ohm",
                "v_over_i_ohm",
            ),
            positive_source_voltage_v=_raw_float_or_none(
                positive_data,
                "source_voltage_v",
                "bias_voltage_v",
            ),
            positive_measured_voltage_v=_raw_float_or_none(
                positive_data,
                "measured_voltage_v",
                "voltage_v",
            ),
            positive_current_a=_raw_float_or_none(
                positive_data,
                "current_a",
                "measured_current_a",
            ),
            positive_resistance_ohm=_raw_float_or_none(
                positive_data,
                "resistance_ohm",
                "v_over_i_ohm",
            ),
        )
    return RouteMeasurementSample(
        sample_index=sample_index,
        differential_resistance_ohm=float(raw),
    )


def route_measurement_sample_from_raw(
    raw: object,
    sample_index: int,
) -> RouteMeasurementSample:
    """Convert one backend measurement payload into a route sample."""

    return _measurement_sample_from_raw(raw, sample_index)


def summarize_route_contact_quality(
    samples: list[RouteMeasurementSample] | tuple[RouteMeasurementSample, ...],
    *,
    contact_quality_limits: RouteContactQualityLimits | None = None,
) -> RouteContactQuality:
    """Evaluate the contact-quality metrics used by route workflows."""

    return _contact_quality_from_samples(
        samples,
        contact_quality_limits=contact_quality_limits,
    )


def _raw_bool(
    data: dict[str, object],
    *keys: str,
    default: bool,
) -> bool:
    for key in keys:
        if key not in data:
            continue
        value = data.get(key)
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        if isinstance(value, str):
            text = value.strip().lower()
            if text in {"1", "true", "yes", "y", "on"}:
                return True
            if text in {"0", "false", "no", "n", "off"}:
                return False
    return default


def _raw_float(
    data: dict[str, object],
    *keys: str,
    default: float,
) -> float:
    value = _raw_float_or_none(data, *keys)
    return default if value is None else value


def _raw_float_or_none(data: dict[str, object], *keys: str) -> float | None:
    for key in keys:
        if key not in data:
            continue
        value = data.get(key)
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
    return None


__all__ = [
    "RouteContactQuality",
    "RouteContactQualityLimits",
    "RouteMeasurementSample",
    "route_measurement_sample_from_raw",
    "summarize_route_contact_quality",
]
