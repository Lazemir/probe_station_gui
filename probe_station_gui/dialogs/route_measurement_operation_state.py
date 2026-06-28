"""Enablement policy for route measurement operation controls."""

from __future__ import annotations

from dataclasses import dataclass

from probe_station_gui.route.operation_modes import (
    route_operation_measure_enabled,
    route_operation_photo_enabled,
)


@dataclass(frozen=True)
class RouteMeasurementOperationState:
    photo_controls_enabled: bool
    photo_autofocus_enabled: bool
    photo_autofocus_range_enabled: bool
    measurement_controls_enabled: bool
    previous_csv_enabled: bool


def route_measurement_operation_state(
    *,
    mode: str,
    running: bool,
    waiting: bool,
    autofocus_checked: bool,
    previous_ok_only_checked: bool,
) -> RouteMeasurementOperationState:
    photo_enabled = route_operation_photo_enabled(mode)
    measure_enabled = route_operation_measure_enabled(mode)
    route_active = photo_enabled or measure_enabled
    can_edit = (not running) or waiting
    return RouteMeasurementOperationState(
        photo_controls_enabled=photo_enabled and can_edit,
        photo_autofocus_enabled=route_active and can_edit,
        photo_autofocus_range_enabled=route_active
        and autofocus_checked
        and can_edit,
        measurement_controls_enabled=measure_enabled and can_edit,
        previous_csv_enabled=measure_enabled
        and previous_ok_only_checked
        and can_edit,
    )
