from __future__ import annotations

import pytest

from probe_station_gui.settings.precision_approach import PrecisionApproachProfile
from probe_station_gui.stage.coordinate_confidence import AxisCoordinateConfidence


PROFILE = PrecisionApproachProfile(True, 0.1, 1)


def test_completed_precision_final_marks_coordinate_exact() -> None:
    state = AxisCoordinateConfidence().after_precision_final(
        coordinate=2.5,
        final_direction=1,
    )

    assert state == AxisCoordinateConfidence(
        exact=True,
        loaded_direction=1,
        confirmed_machine_coordinate=2.5,
        takeup_travel=0.0,
    )


def test_same_direction_small_jog_preserves_exact_coordinate() -> None:
    state = AxisCoordinateConfidence(
        exact=True,
        loaded_direction=1,
        confirmed_machine_coordinate=2.5,
    )

    moved = state.after_confirmed_motion(coordinate=2.51, profile=PROFILE)

    assert moved.exact
    assert moved.loaded_direction == 1
    assert moved.confirmed_machine_coordinate == pytest.approx(2.51)


def test_reversal_immediately_makes_exact_coordinate_approximate() -> None:
    state = AxisCoordinateConfidence(
        exact=True,
        loaded_direction=1,
        confirmed_machine_coordinate=2.5,
    )

    moved = state.after_confirmed_motion(coordinate=2.49, profile=PROFILE)

    assert not moved.exact
    assert moved.loaded_direction == -1
    assert moved.takeup_travel == pytest.approx(0.01)


def test_full_final_direction_takeup_restores_exact_coordinate() -> None:
    state = AxisCoordinateConfidence(
        exact=False,
        loaded_direction=-1,
        confirmed_machine_coordinate=2.49,
        takeup_travel=0.02,
    )

    first = state.after_confirmed_motion(coordinate=2.54, profile=PROFILE)
    second = first.after_confirmed_motion(coordinate=2.59, profile=PROFILE)

    assert not first.exact
    assert first.loaded_direction == 1
    assert first.takeup_travel == pytest.approx(0.05)
    assert second.exact
    assert second.loaded_direction == 1
    assert second.takeup_travel == 0.0


def test_direction_change_restarts_approximate_takeup_distance() -> None:
    state = AxisCoordinateConfidence(
        exact=False,
        loaded_direction=1,
        confirmed_machine_coordinate=1.0,
        takeup_travel=0.08,
    )

    moved = state.after_confirmed_motion(coordinate=0.97, profile=PROFILE)

    assert not moved.exact
    assert moved.loaded_direction == -1
    assert moved.takeup_travel == pytest.approx(0.03)


@pytest.mark.parametrize("reason", ["homing", "reset", "failed motion", "profile change"])
def test_invalidation_makes_coordinate_approximate(reason: str) -> None:
    state = AxisCoordinateConfidence(
        exact=True,
        loaded_direction=1,
        confirmed_machine_coordinate=3.0,
    )

    invalid = state.invalidate(reason=reason, coordinate=3.1)

    assert not invalid.exact
    assert invalid.loaded_direction is None
    assert invalid.confirmed_machine_coordinate == pytest.approx(3.1)
    assert invalid.takeup_travel == 0.0


def test_first_confirmed_position_does_not_claim_motion_or_exactness() -> None:
    state = AxisCoordinateConfidence().after_confirmed_motion(
        coordinate=4.0,
        profile=PROFILE,
    )

    assert state == AxisCoordinateConfidence(confirmed_machine_coordinate=4.0)
