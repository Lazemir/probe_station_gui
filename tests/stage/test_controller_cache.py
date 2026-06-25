import math

from probe_station_gui.stage.controller_cache import (
    parse_cached_axis_limits,
    parse_cached_axis_max_feedrates,
    parse_cached_controller_session_marker,
    parse_cached_coordinate_offsets,
)


AXES = ("X", "Y", "Z", "A", "B", "C")
WORK_COORDINATE_SYSTEMS = ("G54", "G55")


def test_parse_cached_controller_session_marker_requires_positive_int() -> None:
    assert parse_cached_controller_session_marker({"controller_session_marker": "321"}) == 321
    assert parse_cached_controller_session_marker({"controller_session_marker": 0}) is None
    assert parse_cached_controller_session_marker({"controller_session_marker": "bad"}) is None
    assert parse_cached_controller_session_marker({}) is None


def test_parse_cached_axis_limits_filters_invalid_axes_and_ranges() -> None:
    limits = parse_cached_axis_limits(
        {
            "x": [0, 64],
            "B": [-45, 45],
            "Z": [10, 0],
            "A": [0, math.nan],
            "Q": [0, 1],
        },
        axis_names=AXES,
    )

    assert limits == {"X": (0.0, 64.0)}


def test_parse_cached_axis_max_feedrates_filters_and_clamps_rates() -> None:
    feedrates = parse_cached_axis_max_feedrates(
        {
            "x": 0.5,
            "Z": 100.0,
            "A": -1,
            "Q": 200,
            "Y": math.inf,
        },
        axis_names=AXES,
        min_feedrate=1.0,
    )

    assert feedrates == {"X": 1.0, "Z": 100.0}


def test_parse_cached_coordinate_offsets_filters_invalid_systems_and_values() -> None:
    offsets = parse_cached_coordinate_offsets(
        {
            "g54": [32.0, 32.0, 0.0, -2.885],
            "G55": [1, 2],
            "G56": [1, 2, 3],
            "G54.1": [1, 2, 3],
        },
        coordinate_systems=WORK_COORDINATE_SYSTEMS,
    )

    assert offsets == {"G54": (32.0, 32.0, 0.0, -2.885)}
