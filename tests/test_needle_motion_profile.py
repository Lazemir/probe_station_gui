from probe_station_gui.needle_motion_profile import (
    build_needle_motion_profile_segments,
)


def test_lower_moves_fast_to_boundary_then_slow_inside_contact_zone() -> None:
    segments = build_needle_motion_profile_segments(
        current_lowering=0.0,
        target_lowering=1.0,
        boundary_lowering=0.95,
        fast_feedrate=500.0,
        slow_feedrate=80.0,
        min_feedrate=1.0,
    )

    assert segments == [(0.95, 500.0, False), (1.0, 80.0, True)]


def test_raise_moves_slow_out_of_contact_zone_then_fast_up() -> None:
    segments = build_needle_motion_profile_segments(
        current_lowering=1.0,
        target_lowering=0.0,
        boundary_lowering=0.95,
        fast_feedrate=500.0,
        slow_feedrate=70.0,
        min_feedrate=1.0,
    )

    assert segments == [(0.95, 70.0, True), (0.0, 500.0, False)]


def test_lift_from_above_boundary_uses_fast_feedrate() -> None:
    segments = build_needle_motion_profile_segments(
        current_lowering=0.5,
        target_lowering=0.9,
        boundary_lowering=0.9,
        fast_feedrate=500.0,
        slow_feedrate=70.0,
        min_feedrate=1.0,
    )

    assert segments == [(0.9, 500.0, False)]


def test_missing_boundary_uses_single_fast_segment() -> None:
    segments = build_needle_motion_profile_segments(
        current_lowering=0.0,
        target_lowering=1.0,
        boundary_lowering=None,
        fast_feedrate=500.0,
        slow_feedrate=80.0,
        min_feedrate=1.0,
    )

    assert segments == [(1.0, 500.0, False)]


def test_feedrates_are_clamped_to_minimum() -> None:
    segments = build_needle_motion_profile_segments(
        current_lowering=0.0,
        target_lowering=1.0,
        boundary_lowering=0.5,
        fast_feedrate=0.1,
        slow_feedrate=0.2,
        min_feedrate=1.0,
    )

    assert segments == [(0.5, 1.0, False), (1.0, 1.0, True)]
