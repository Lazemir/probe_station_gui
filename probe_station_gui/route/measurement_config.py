"""Route measurement run configuration models."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping

from probe_station_gui.route.measurement import RouteContactQualityLimits
from probe_station_gui.route.meter_config import RouteMeterConfiguration


@dataclass(frozen=True)
class RouteMeasurementRunConfiguration:
    """Complete per-run route measurement configuration from the dialog."""

    csv_path: str
    previous_csv_path: str
    operation_mode: str
    photo_output_dir: str
    photo_settle_s: float
    photo_autofocus_enabled: bool
    photo_autofocus_range_mm: float
    initial_measurement_count: int
    followup_measurement_count: int
    current_point: int
    max_relative_rms: float
    contact_settle_s: float
    contact_seek_range_mm: float
    contact_seek_step_mm: float
    previous_ok_only: bool
    meter: RouteMeterConfiguration
    contact_quality_limits: RouteContactQualityLimits = field(
        default_factory=RouteContactQualityLimits
    )

    @property
    def measurement_count(self) -> int:
        """Maximum readings per point after both measurement phases."""

        return max(1, int(self.initial_measurement_count)) + max(
            0,
            int(self.followup_measurement_count),
        )

    @property
    def start_point(self) -> int:
        """Backward-compatible alias for older callers."""

        return self.current_point


def route_measurement_count_profile(
    data: Mapping[str, object],
    *,
    default_initial_count: int,
) -> tuple[int | None, int | None]:
    initial = _positive_int_or_none(data.get("initial_measurement_count"))
    followup = _nonnegative_int_or_none(data.get("followup_measurement_count"))
    total = _positive_int_or_none(data.get("measurement_count"))
    if initial is None and followup is None and total is not None:
        initial = min(total, int(default_initial_count))
        followup = max(0, total - initial)
    return initial, followup


def _positive_int_or_none(value: object) -> int | None:
    try:
        numeric = int(round(float(value)))
    except (TypeError, ValueError):
        return None
    return numeric if numeric > 0 else None


def _nonnegative_int_or_none(value: object) -> int | None:
    try:
        numeric = int(round(float(value)))
    except (TypeError, ValueError):
        return None
    return numeric if numeric >= 0 else None
