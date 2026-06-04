"""Generic ohmmeter contracts and range selection helpers."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import math
from typing import Any


OHMMETER_RANGE_MANUAL = "manual"
OHMMETER_RANGE_CODE_AUTO = "code_auto"
OHMMETER_RANGE_DEVICE_AUTO = "device_auto"
OHMMETER_RANGE_MODES = (
    OHMMETER_RANGE_MANUAL,
    OHMMETER_RANGE_CODE_AUTO,
    OHMMETER_RANGE_DEVICE_AUTO,
)

DEFAULT_VOLTAGE_RANGE_HEADROOM = 1.2
DEFAULT_CURRENT_RANGE_HEADROOM = 2.0


class AbstractOhmmeter(ABC):
    """Minimal instrument contract used by the probe-station workflow."""

    backend_name = "ohmmeter"

    @abstractmethod
    def configure_measurement(self, **settings: Any) -> None:
        """Apply one externally selected measurement configuration."""

    @abstractmethod
    def read_route_measurement(self, *, trigger: bool = False) -> Mapping[str, object]:
        """Read one route-measurement result."""

    @abstractmethod
    def close(self) -> None:
        """Release instrument resources."""

    def read_primary_value(self, *, trigger: bool = False) -> float:
        """Return the primary route resistance value."""

        result = self.read_route_measurement(trigger=trigger)
        return float(result["differential_resistance_ohm"])

    def read_route_measurements(
        self,
        count: int,
        *,
        trigger: bool = False,
        after_measurement: object | None = None,
    ) -> list[Mapping[str, object]]:
        """Read a small batch using the single-reading method by default."""

        _ = after_measurement
        return [
            self.read_route_measurement(trigger=trigger)
            for _index in range(max(1, int(count)))
        ]

    def prepare_route_measurements(
        self,
        count: int,
        *,
        source_list_count: int | None = None,
    ) -> None:
        """Optional pre-measurement hook for instruments with trace buffers."""

        _ = count, source_list_count

    def abort_measurement(self) -> None:
        """Best-effort cancellation hook."""


@dataclass(frozen=True)
class OhmmeterRangeRequest:
    """Device-neutral range request for one ohmmeter configuration.

    ``code_auto`` means the driver code chooses fixed instrument ranges from the
    external measurement parameters before acquisition starts. It is deliberately
    not instrument autorange during the actual measurement.
    """

    mode: str = OHMMETER_RANGE_MANUAL
    expected_resistance_ohm: float | None = None
    minimum_resistance_ohm: float | None = None
    maximum_current_a: float | None = None
    voltage_range_v: float | None = None
    current_range_a: float | None = None
    compliance_current_a: float | None = None
    voltage_headroom: float = DEFAULT_VOLTAGE_RANGE_HEADROOM
    current_headroom: float = DEFAULT_CURRENT_RANGE_HEADROOM

    @classmethod
    def from_mapping(cls, values: Mapping[str, object] | None) -> "OhmmeterRangeRequest":
        data = dict(values or {})
        return cls(
            mode=normalize_range_mode(_first(data, "mode", "range_mode")),
            expected_resistance_ohm=_positive_float_or_none(
                _first(data, "expected_resistance_ohm", "resistance_ohm")
            ),
            minimum_resistance_ohm=_positive_float_or_none(
                _first(
                    data,
                    "minimum_resistance_ohm",
                    "min_resistance_ohm",
                    "resistance_floor_ohm",
                )
            ),
            maximum_current_a=_positive_float_or_none(
                _first(
                    data,
                    "maximum_current_a",
                    "max_current_a",
                    "current_limit_a",
                    "compliance_current_a",
                )
            ),
            voltage_range_v=_positive_float_or_none(
                _first(data, "voltage_range_v")
            ),
            current_range_a=_positive_float_or_none(
                _first(data, "current_range_a")
            ),
            compliance_current_a=_positive_float_or_none(
                _first(data, "compliance_current_a")
            ),
            voltage_headroom=_positive_float(
                _first(data, "voltage_headroom"),
                DEFAULT_VOLTAGE_RANGE_HEADROOM,
            ),
            current_headroom=_positive_float(
                _first(data, "current_headroom"),
                DEFAULT_CURRENT_RANGE_HEADROOM,
            ),
        ).normalized()

    def normalized(self) -> "OhmmeterRangeRequest":
        return OhmmeterRangeRequest(
            mode=normalize_range_mode(self.mode),
            expected_resistance_ohm=_positive_float_or_none(
                self.expected_resistance_ohm
            ),
            minimum_resistance_ohm=_positive_float_or_none(
                self.minimum_resistance_ohm
            ),
            maximum_current_a=_positive_float_or_none(self.maximum_current_a),
            voltage_range_v=_positive_float_or_none(self.voltage_range_v),
            current_range_a=_positive_float_or_none(self.current_range_a),
            compliance_current_a=_positive_float_or_none(
                self.compliance_current_a
            ),
            voltage_headroom=max(
                1.0,
                _positive_float(
                    self.voltage_headroom,
                    DEFAULT_VOLTAGE_RANGE_HEADROOM,
                ),
            ),
            current_headroom=max(
                1.0,
                _positive_float(
                    self.current_headroom,
                    DEFAULT_CURRENT_RANGE_HEADROOM,
                ),
            ),
        )


@dataclass(frozen=True)
class OhmmeterRangeDefaults:
    """Fallback fixed ranges for an ohmmeter implementation."""

    source_voltage_range_v: float
    voltmeter_range_v: float
    current_range_a: float
    compliance_current_a: float


@dataclass(frozen=True)
class OhmmeterRangeCapabilities:
    """Discrete ranges supported by one implementation."""

    source_voltage_ranges_v: tuple[float, ...] = ()
    voltmeter_ranges_v: tuple[float, ...] = ()
    current_ranges_a: tuple[float, ...] = ()


@dataclass(frozen=True)
class ResolvedOhmmeterRanges:
    """Concrete ranges to write to an instrument."""

    mode: str
    source_voltage_range_v: float
    voltmeter_range_v: float
    current_range_a: float
    compliance_current_a: float


def normalize_range_mode(value: object) -> str:
    text = str(value or "").strip().lower().replace("-", "_")
    if text in {"", "manual", "fixed", "hold"}:
        return OHMMETER_RANGE_MANUAL
    if text in {"code_auto", "software_auto", "computed_auto", "auto"}:
        return OHMMETER_RANGE_CODE_AUTO
    if text in {"device_auto", "instrument_auto"}:
        return OHMMETER_RANGE_DEVICE_AUTO
    return text


def resolve_ohmmeter_ranges(
    *,
    measurement_voltage_v: float,
    request: OhmmeterRangeRequest,
    defaults: OhmmeterRangeDefaults,
    capabilities: OhmmeterRangeCapabilities,
) -> ResolvedOhmmeterRanges:
    """Resolve a neutral range request into fixed instrument ranges."""

    normalized = request.normalized()
    voltage = abs(_positive_float(measurement_voltage_v, 0.03))
    if normalized.mode == OHMMETER_RANGE_DEVICE_AUTO:
        raise ValueError("Device autorange is not supported for route measurements.")
    if normalized.mode != OHMMETER_RANGE_CODE_AUTO:
        voltage_range = _positive_float(
            normalized.voltage_range_v,
            max(defaults.source_voltage_range_v, defaults.voltmeter_range_v),
        )
        current_range = _positive_float(
            normalized.current_range_a,
            defaults.current_range_a,
        )
        compliance = _positive_float(
            normalized.compliance_current_a,
            defaults.compliance_current_a,
        )
        return ResolvedOhmmeterRanges(
            mode=OHMMETER_RANGE_MANUAL,
            source_voltage_range_v=max(voltage_range, voltage),
            voltmeter_range_v=max(voltage_range, voltage),
            current_range_a=current_range,
            compliance_current_a=compliance,
        )

    requested_common_voltage_range = _positive_float(
        normalized.voltage_range_v,
        0.0,
    )
    voltage_required = max(
        voltage,
        requested_common_voltage_range,
    ) * normalized.voltage_headroom
    current_limit = _positive_float(
        normalized.maximum_current_a,
        _positive_float(
            normalized.compliance_current_a,
            defaults.compliance_current_a,
        ),
    )
    target_current = _target_current_a(
        voltage,
        normalized.minimum_resistance_ohm or normalized.expected_resistance_ohm,
        fallback=defaults.current_range_a,
    )
    current_required = min(
        current_limit,
        max(0.0, target_current * normalized.current_headroom),
    )
    if current_required <= 0.0:
        current_required = min(current_limit, defaults.current_range_a)
    current_range = _pick_supported_range(
        current_required,
        capabilities.current_ranges_a,
        default=defaults.current_range_a,
    )
    compliance = min(current_limit, current_range)
    voltage_range = max(
        _pick_supported_range(
            voltage_required,
            capabilities.source_voltage_ranges_v,
            default=defaults.source_voltage_range_v,
        ),
        _pick_supported_range(
            voltage_required,
            capabilities.voltmeter_ranges_v,
            default=defaults.voltmeter_range_v,
        ),
    )
    return ResolvedOhmmeterRanges(
        mode=OHMMETER_RANGE_CODE_AUTO,
        source_voltage_range_v=voltage_range,
        voltmeter_range_v=voltage_range,
        current_range_a=current_range,
        compliance_current_a=compliance,
    )


def _target_current_a(
    voltage_v: float,
    resistance_ohm: float | None,
    *,
    fallback: float,
) -> float:
    if resistance_ohm is None or resistance_ohm <= 0.0:
        return _positive_float(fallback, 0.0)
    return abs(float(voltage_v)) / float(resistance_ohm)


def _pick_supported_range(
    required: float,
    supported: Sequence[float],
    *,
    default: float,
) -> float:
    required_value = _positive_float(required, default)
    values = sorted(
        value
        for value in (_positive_float(item, math.nan) for item in supported)
        if math.isfinite(value) and value > 0.0
    )
    if not values:
        return max(required_value, _positive_float(default, required_value))
    for value in values:
        if value >= required_value or math.isclose(value, required_value, rel_tol=1e-12):
            return value
    return values[-1]


def _first(values: Mapping[str, object], *names: str) -> object | None:
    for name in names:
        if name in values:
            return values[name]
    return None


def _positive_float_or_none(value: object) -> float | None:
    result = _positive_float(value, math.nan)
    return result if math.isfinite(result) and result > 0.0 else None


def _positive_float(value: object, default: float) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return float(default)
    if not math.isfinite(result) or result <= 0.0:
        return float(default)
    return result
