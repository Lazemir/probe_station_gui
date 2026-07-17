from __future__ import annotations

import pytest

from probe_station_gui.settings.precision_approach import PrecisionApproachProfile
from probe_station_gui.stage.coordinate_confidence import AxisCoordinateConfidence
from probe_station_gui.stage.precision_approach import (
    PrecisionApproachPlanner,
    resolve_relative_targets,
)


def _profile(
    *, enabled: bool = True, backlash: float = 0.1, direction: int = 1
) -> PrecisionApproachProfile:
    return PrecisionApproachProfile(enabled, backlash, direction)


@pytest.mark.parametrize(
    ("profile", "confidence", "current", "target"),
    [
        (_profile(enabled=False), AxisCoordinateConfidence(), 0.0, -0.01),
        (_profile(backlash=0.0), AxisCoordinateConfidence(), 0.0, -0.01),
        (
            _profile(direction=1),
            AxisCoordinateConfidence(exact=True, loaded_direction=1),
            0.0,
            0.01,
        ),
        (_profile(direction=1), AxisCoordinateConfidence(), 0.0, 0.1),
        (
            _profile(direction=1),
            AxisCoordinateConfidence(
                exact=False,
                loaded_direction=1,
                takeup_travel=0.09,
            ),
            0.0,
            0.01,
        ),
        (_profile(direction=-1), AxisCoordinateConfidence(), 1.0, 0.9),
    ],
)
def test_planner_uses_one_direct_final_target_when_take_up_is_already_safe(
    profile: PrecisionApproachProfile,
    confidence: AxisCoordinateConfidence,
    current: float,
    target: float,
) -> None:
    validated: list[dict[str, float]] = []

    plan = PrecisionApproachPlanner().plan(
        current={"Z": current},
        target={"Z": target},
        profiles={"Z": profile},
        confidence={"Z": confidence},
        validate_target=lambda values: validated.append(dict(values)),
    )

    assert plan.preparation_target is None
    assert plan.final_target == {"Z": target}
    assert plan.prepared_axes == frozenset()
    assert validated == [{"Z": target}]


@pytest.mark.parametrize(
    ("confidence", "current", "target", "expected_preparation"),
    [
        (AxisCoordinateConfidence(), 0.0, 0.05, -0.05),
        (
            AxisCoordinateConfidence(exact=True, loaded_direction=1),
            1.0,
            0.95,
            0.85,
        ),
        (
            AxisCoordinateConfidence(
                exact=False,
                loaded_direction=1,
                takeup_travel=0.09,
            ),
            0.0,
            0.005,
            -0.095,
        ),
    ],
)
def test_planner_prepares_when_final_direction_is_not_confirmed(
    confidence: AxisCoordinateConfidence,
    current: float,
    target: float,
    expected_preparation: float,
) -> None:
    plan = PrecisionApproachPlanner().plan(
        current={"Z": current},
        target={"Z": target},
        profiles={"Z": _profile(direction=1)},
        confidence={"Z": confidence},
        validate_target=lambda _values: None,
    )

    assert plan.preparation_target == {"Z": pytest.approx(expected_preparation)}
    assert plan.final_target == {"Z": target}
    assert plan.prepared_axes == frozenset({"Z"})


def test_planner_holds_other_target_axes_during_mixed_axis_preparation() -> None:
    plan = PrecisionApproachPlanner().plan(
        current={"X": 0.0, "Z": 5.0, "B": 1.0},
        target={"X": 10.0, "Z": 5.01, "B": 0.5},
        profiles={
            "X": _profile(enabled=False),
            "Z": _profile(backlash=0.03, direction=1),
            "B": _profile(backlash=0.2, direction=-1),
        },
        confidence={
            axis: AxisCoordinateConfidence() for axis in ("X", "Z", "B")
        },
        validate_target=lambda _values: None,
    )

    assert plan.preparation_target == {
        "X": pytest.approx(0.0),
        "Z": pytest.approx(4.98),
        "B": pytest.approx(1.0),
    }
    assert plan.final_target == {"X": 10.0, "Z": 5.01, "B": 0.5}
    assert plan.prepared_axes == frozenset({"Z"})


def test_planner_validates_every_segment_before_returning() -> None:
    validated: list[dict[str, float]] = []

    PrecisionApproachPlanner().plan(
        current={"Z": 0.0},
        target={"Z": 0.01},
        profiles={"Z": _profile()},
        confidence={"Z": AxisCoordinateConfidence()},
        validate_target=lambda values: validated.append(dict(values)),
    )

    assert validated == [{"Z": 0.01}, {"Z": pytest.approx(-0.09)}]


def test_planner_rejects_entire_plan_when_preparation_is_out_of_limits() -> None:
    validated: list[dict[str, float]] = []

    def validate(values: dict[str, float]) -> None:
        validated.append(dict(values))
        if values["Z"] < 0.0:
            raise ValueError("Z preparation is outside limits")

    with pytest.raises(ValueError, match="outside limits"):
        PrecisionApproachPlanner().plan(
            current={"Z": 0.05},
            target={"Z": 0.05},
            profiles={"Z": _profile(backlash=0.1)},
            confidence={"Z": AxisCoordinateConfidence()},
            validate_target=validate,
        )

    assert validated == [{"Z": 0.05}, {"Z": pytest.approx(-0.05)}]


def test_planner_rejects_targets_without_current_coordinates() -> None:
    with pytest.raises(ValueError, match="current coordinate for Z"):
        PrecisionApproachPlanner().plan(
            current={},
            target={"Z": 1.0},
            profiles={"Z": _profile()},
            confidence={},
            validate_target=lambda _values: None,
        )


def test_relative_targets_are_resolved_before_planning() -> None:
    assert resolve_relative_targets(
        current={"X": 10.0, "Z": -2.0},
        deltas={"X": 0.5, "Z": -0.25},
    ) == {"X": 10.5, "Z": -2.25}
