import math

from probe_station_gui.stage.api_moves import (
    ApiCoordinateMovePlan,
    api_axis_value_map,
    api_coordinate_move_busy_response,
    api_coordinate_move_plan,
    api_coordinate_move_start_failed_response,
    api_coordinate_move_success_response,
    api_move_feedrate,
    normalize_api_coordinate_input_mode,
)


AXIS_NAMES = ("X", "Y", "Z", "A", "B", "C")


def _resolve_axis_target(
    axis: str,
    display_target: float,
    input_mode: str,
) -> tuple[float | None, float]:
    if axis == "A":
        return None, display_target
    raw_target = display_target + (100.0 if input_mode == "G91" else 0.0)
    return raw_target, display_target + 0.25


def _axis_target_limit_error(axis: str, display_target: float) -> str | None:
    if axis == "Y":
        return f"{axis} target {display_target:.3f} exceeds limits."
    if axis == "Z":
        return f"{axis} target {display_target:.3f} exceeds limits."
    return None


def test_api_axis_value_map_returns_none_for_non_positions_and_skips_invalid_values() -> None:
    assert api_axis_value_map(None, axis_names=AXIS_NAMES) is None
    assert api_axis_value_map("X,Y,Z", axis_names=AXIS_NAMES) is None
    assert api_axis_value_map((1, "bad", 3.5), axis_names=AXIS_NAMES) == {
        "X": 1.0,
        "Z": 3.5,
    }


def test_normalize_api_coordinate_input_mode_supports_aliases_and_rejects_unknown() -> None:
    for value in (None, "", "absolute", "abs", "g90", " G90 "):
        assert normalize_api_coordinate_input_mode(value) == "G90"
    for value in ("relative", "rel", "g91", " G91 "):
        assert normalize_api_coordinate_input_mode(value) == "G91"

    assert normalize_api_coordinate_input_mode("polar") is None


def test_api_move_feedrate_uses_default_minimum_and_rejects_invalid_values() -> None:
    assert api_move_feedrate(None, current_feedrate=77.0, min_feedrate=1.0) == 77.0
    assert api_move_feedrate(None, current_feedrate=0.25, min_feedrate=1.0) == 1.0
    assert api_move_feedrate(0.5, current_feedrate=77.0, min_feedrate=1.0) == 1.0
    assert api_move_feedrate(12.5, current_feedrate=77.0, min_feedrate=1.0) == 12.5

    for value in ("bad", 0, -1, math.inf, math.nan):
        assert api_move_feedrate(
            value,
            current_feedrate=77.0,
            min_feedrate=1.0,
        ) is None


def test_api_coordinate_move_plan_rejects_non_dict_targets() -> None:
    response = api_coordinate_move_plan(
        ["X", 1.0],
        axis_names=AXIS_NAMES,
        mode="G90",
        feedrate=None,
        current_feedrate=77.0,
        min_feedrate=1.0,
        resolve_axis_target=_resolve_axis_target,
        axis_target_limit_error=lambda _axis, _target: None,
    )

    assert response == {
        "accepted": False,
        "status_code": 400,
        "message": "Coordinate targets must be an object.",
    }


def test_api_coordinate_move_plan_preserves_original_labels_for_unsupported_axes() -> None:
    response = api_coordinate_move_plan(
        {" q ": 1.0, 5: 2.0},
        axis_names=AXIS_NAMES,
        mode="G90",
        feedrate=None,
        current_feedrate=77.0,
        min_feedrate=1.0,
        resolve_axis_target=_resolve_axis_target,
        axis_target_limit_error=lambda _axis, _target: None,
    )

    assert response == {
        "accepted": False,
        "status_code": 400,
        "message": "Unsupported axes:  q , 5.",
    }


def test_api_coordinate_move_plan_rejects_invalid_coordinate_values() -> None:
    response = api_coordinate_move_plan(
        {"X": "bad", "Y": math.nan},
        axis_names=AXIS_NAMES,
        mode="G90",
        feedrate=None,
        current_feedrate=77.0,
        min_feedrate=1.0,
        resolve_axis_target=_resolve_axis_target,
        axis_target_limit_error=lambda _axis, _target: None,
    )

    assert response == {
        "accepted": False,
        "status_code": 400,
        "message": "Invalid coordinate values for: X, Y.",
    }


def test_api_coordinate_move_plan_rejects_unavailable_coordinates() -> None:
    response = api_coordinate_move_plan(
        {"A": 4.0},
        axis_names=AXIS_NAMES,
        mode="G90",
        feedrate=None,
        current_feedrate=77.0,
        min_feedrate=1.0,
        resolve_axis_target=_resolve_axis_target,
        axis_target_limit_error=lambda _axis, _target: None,
    )

    assert response == {
        "accepted": False,
        "status_code": 409,
        "message": "Coordinates are unavailable in the GUI for: A.",
    }


def test_api_coordinate_move_plan_joins_limit_error_messages() -> None:
    response = api_coordinate_move_plan(
        {"Y": 2.0, "Z": 3.0},
        axis_names=AXIS_NAMES,
        mode="G90",
        feedrate=None,
        current_feedrate=77.0,
        min_feedrate=1.0,
        resolve_axis_target=_resolve_axis_target,
        axis_target_limit_error=_axis_target_limit_error,
    )

    assert response == {
        "accepted": False,
        "status_code": 409,
        "message": (
            "Y target 2.250 exceeds limits. Z target 3.250 exceeds limits."
        ),
    }


def test_api_coordinate_move_plan_rejects_when_no_targets_remain() -> None:
    response = api_coordinate_move_plan(
        {},
        axis_names=AXIS_NAMES,
        mode="G90",
        feedrate=None,
        current_feedrate=77.0,
        min_feedrate=1.0,
        resolve_axis_target=_resolve_axis_target,
        axis_target_limit_error=lambda _axis, _target: None,
    )

    assert response == {
        "accepted": False,
        "status_code": 400,
        "message": "Provide at least one target coordinate.",
    }


def test_api_coordinate_move_plan_orders_axes_by_stage_axis_names() -> None:
    plan = api_coordinate_move_plan(
        {"Y": 2.0, "X": 1.0},
        axis_names=AXIS_NAMES,
        mode="relative",
        feedrate=55.0,
        current_feedrate=77.0,
        min_feedrate=1.0,
        resolve_axis_target=lambda axis, display_target, input_mode: (
            display_target + (10.0 if input_mode == "G91" else 0.0),
            display_target + 0.5,
        ),
        axis_target_limit_error=lambda _axis, _target: None,
    )

    assert isinstance(plan, ApiCoordinateMovePlan)
    assert plan.mode == "G91"
    assert plan.feedrate_mm_min == 55.0
    assert plan.axes == ["X", "Y"]
    assert plan.ordered_targets == [
        ("X", 11.0, 1.5),
        ("Y", 12.0, 2.5),
    ]
    assert plan.target_map == {
        "X": (11.0, 1.5),
        "Y": (12.0, 2.5),
    }


def test_api_coordinate_move_success_response_matches_main_payload_shape() -> None:
    plan = ApiCoordinateMovePlan(
        ordered_targets=[("X", 5.0, 5.5), ("Y", -2.0, -1.5)],
        target_map={"X": (5.0, 5.5), "Y": (-2.0, -1.5)},
        axes=["X", "Y"],
        feedrate_mm_min=120.0,
        mode="G90",
    )

    assert api_coordinate_move_busy_response() == {
        "accepted": False,
        "status_code": 409,
        "message": "Stage is busy. Ignoring API coordinate target.",
    }
    assert api_coordinate_move_start_failed_response() == {
        "accepted": False,
        "status_code": 409,
        "message": "Unable to start coordinate move.",
    }
    assert api_coordinate_move_success_response(
        plan,
        coordinate_display="Work",
    ) == {
        "accepted": True,
        "message": "API coordinate move accepted: X, Y.",
        "started_axes": ["X", "Y"],
        "queued_axes": [],
        "mode": "G90",
        "current_feedrate_mm_min": 120.0,
        "coordinate_display": "Work",
        "targets": {"X": 5.5, "Y": -1.5},
    }
