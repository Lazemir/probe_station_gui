from __future__ import annotations

from probe_station_gui.route_measurement_config import (
    RouteMeasurementRunConfiguration,
    route_measurement_count_profile,
)
from probe_station_gui.route_meter_config import RouteMeterConfiguration


def _configuration(
    *,
    initial_measurement_count: int,
    followup_measurement_count: int,
    current_point: int = 1,
) -> RouteMeasurementRunConfiguration:
    return RouteMeasurementRunConfiguration(
        csv_path="route.csv",
        previous_csv_path="",
        operation_mode="measure",
        photo_output_dir="",
        photo_settle_s=0.0,
        photo_autofocus_enabled=False,
        photo_autofocus_range_mm=0.0,
        initial_measurement_count=initial_measurement_count,
        followup_measurement_count=followup_measurement_count,
        current_point=current_point,
        max_relative_rms=0.0,
        contact_settle_s=0.0,
        contact_seek_range_mm=0.0,
        contact_seek_step_mm=0.0,
        previous_ok_only=False,
        meter=RouteMeterConfiguration(),
    )


def test_measurement_count_combines_initial_and_followup_with_minimums() -> None:
    assert (
        _configuration(
            initial_measurement_count=10,
            followup_measurement_count=240,
        ).measurement_count
        == 250
    )
    assert (
        _configuration(
            initial_measurement_count=0,
            followup_measurement_count=-3,
        ).measurement_count
        == 1
    )


def test_start_point_alias_uses_current_point() -> None:
    assert (
        _configuration(
            initial_measurement_count=1,
            followup_measurement_count=0,
            current_point=7,
        ).start_point
        == 7
    )


def test_route_measurement_count_profile_prefers_split_fields() -> None:
    assert route_measurement_count_profile(
        {
            "measurement_count": 99,
            "initial_measurement_count": "2.4",
            "followup_measurement_count": "3.6",
        },
        default_initial_count=10,
    ) == (2, 4)


def test_route_measurement_count_profile_splits_legacy_total() -> None:
    assert route_measurement_count_profile(
        {"measurement_count": 6},
        default_initial_count=10,
    ) == (6, 0)
    assert route_measurement_count_profile(
        {"measurement_count": 25},
        default_initial_count=10,
    ) == (10, 15)


def test_route_measurement_count_profile_ignores_invalid_values() -> None:
    assert route_measurement_count_profile(
        {
            "measurement_count": 0,
            "initial_measurement_count": -1,
            "followup_measurement_count": -1,
        },
        default_initial_count=10,
    ) == (None, None)
