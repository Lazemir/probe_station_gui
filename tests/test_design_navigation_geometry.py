import math

import pytest

from probe_station_gui.design_navigation_geometry import (
    count_from_endpoint,
    first_segment_length,
    format_bounds,
    format_mark_label,
    format_point,
    route_arrow_segments,
    route_arrow_tip_fractions,
    route_pick_label_text,
    vector_from_length_angle,
    vector_length_angle,
)


def test_route_arrow_helpers_skip_empty_and_place_default_single_tip() -> None:
    assert first_segment_length([(0.0, 0.0), (0.0, 0.0)]) == 0.0
    assert route_arrow_tip_fractions(10.0, 10.0, 1.0) == [0.58]

    x_values, y_values = route_arrow_segments(
        [(0.0, 0.0), (10.0, 0.0)],
        pixel_size=0.1,
    )

    assert len(x_values) == 8
    assert len(y_values) == 8
    assert x_values[1] == pytest.approx(5.8)
    assert y_values[1] == pytest.approx(0.0)
    assert math.isnan(x_values[3])
    assert route_arrow_segments(
        [(0.0, 0.0), (10.0, 0.0)],
        pixel_size=0.1,
        deferred=True,
    ) == ([], [])


def test_vector_length_angle_round_trips_cardinal_direction() -> None:
    vector = vector_from_length_angle(2.0, 90.0)

    assert vector[0] == pytest.approx(0.0, abs=1e-12)
    assert vector[1] == pytest.approx(2.0)
    assert vector_length_angle(vector) == pytest.approx((2.0, 90.0))


def test_count_from_endpoint_projects_to_nearest_count() -> None:
    assert count_from_endpoint((0.0, 0.0), (2.0, 0.0), (5.1, 0.0)) == 4
    assert count_from_endpoint((0.0, 0.0), (0.0, 0.0), (5.1, 0.0)) == 1


def test_route_labels_and_coordinate_formatting() -> None:
    assert route_pick_label_text("array_dir1") == "direction 1 vector start"
    assert route_pick_label_text("unknown") == "route point"
    assert format_bounds((1.0, 2.0, 4.0, 6.0)) == "3.000 x 4.000 (diag 5.000)"
    assert format_mark_label("Mark", None) == "Mark: not set"
    assert format_mark_label("Mark", (1.23456, 7.0)) == "Mark: X=1.235, Y=7.000"
    assert format_point((1.23456, 7.0)) == "X=1.235, Y=7.000"
