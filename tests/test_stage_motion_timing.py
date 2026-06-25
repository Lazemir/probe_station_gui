from probe_station_gui.stage_motion_timing import (
    absolute_move_distance_for_timeout,
    idle_timeout_for_distance,
    move_distance_for_timeout,
)
from probe_station_gui.stage_types import MoveVector


def test_absolute_move_distance_uses_current_values_when_available() -> None:
    assert absolute_move_distance_for_timeout(
        {"X": 4.0, "Z": 6.0},
        {"X": 1.0, "Z": 2.0},
    ) == 5.0


def test_absolute_move_distance_falls_back_to_target_magnitude() -> None:
    assert absolute_move_distance_for_timeout({"X": 3.0, "Y": 4.0}, {}) == 5.0


def test_move_distance_for_timeout_uses_all_axes() -> None:
    assert move_distance_for_timeout(MoveVector(x=3.0, z=4.0)) == 5.0


def test_idle_timeout_for_distance_applies_margin_and_bounds() -> None:
    assert idle_timeout_for_distance(
        0.25,
        1.0,
        min_feedrate=1.0,
        margin_s=5.0,
        min_timeout_s=10.0,
        max_timeout_s=3600.0,
    ) == 20.0
    assert idle_timeout_for_distance(
        0.001,
        1000.0,
        min_feedrate=1.0,
        margin_s=5.0,
        min_timeout_s=10.0,
        max_timeout_s=3600.0,
    ) == 10.0
    assert idle_timeout_for_distance(
        10_000.0,
        1.0,
        min_feedrate=1.0,
        margin_s=5.0,
        min_timeout_s=10.0,
        max_timeout_s=3600.0,
    ) == 3600.0
