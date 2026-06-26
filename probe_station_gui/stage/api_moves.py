"""Pure API stage-move parsing, planning, and response helpers."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Callable, Sequence


ResolveAxisTarget = Callable[[str, float, str], tuple[float | None, float]]
AxisTargetLimitError = Callable[[str, float], str | None]


@dataclass(frozen=True)
class ApiCoordinateMovePlan:
    ordered_targets: list[tuple[str, float, float]]
    target_map: dict[str, tuple[float, float]]
    axes: list[str]
    feedrate_mm_min: float
    mode: str


def api_axis_value_map(
    position: object,
    *,
    axis_names: Sequence[str],
) -> dict[str, float] | None:
    if not isinstance(position, (tuple, list)):
        return None
    values: dict[str, float] = {}
    for axis, value in zip(axis_names, position):
        try:
            values[str(axis)] = float(value)
        except (TypeError, ValueError):
            continue
    return values or None


def normalize_api_coordinate_input_mode(mode: object) -> str | None:
    raw_mode = str(mode or "G90").strip().lower()
    if raw_mode in {"", "absolute", "abs", "g90"}:
        return "G90"
    if raw_mode in {"relative", "rel", "g91"}:
        return "G91"
    return None


def api_move_feedrate(
    feedrate: object,
    *,
    current_feedrate: float,
    min_feedrate: float,
) -> float | None:
    if feedrate is None:
        return max(float(min_feedrate), float(current_feedrate))
    try:
        value = float(feedrate)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value) or value <= 0.0:
        return None
    return max(float(min_feedrate), value)


def api_coordinate_move_plan(
    targets: object,
    *,
    axis_names: Sequence[str],
    mode: object = "G90",
    feedrate: object = None,
    current_feedrate: float,
    min_feedrate: float,
    resolve_axis_target: ResolveAxisTarget,
    axis_target_limit_error: AxisTargetLimitError,
) -> ApiCoordinateMovePlan | dict[str, object]:
    if not isinstance(targets, dict):
        return {
            "accepted": False,
            "status_code": 400,
            "message": "Coordinate targets must be an object.",
        }
    input_mode = normalize_api_coordinate_input_mode(mode)
    if input_mode is None:
        return {
            "accepted": False,
            "status_code": 400,
            "message": f"Unsupported coordinate mode: {mode}.",
        }
    move_feedrate = api_move_feedrate(
        feedrate,
        current_feedrate=current_feedrate,
        min_feedrate=min_feedrate,
    )
    if move_feedrate is None:
        return {
            "accepted": False,
            "status_code": 400,
            "message": f"Invalid feedrate: {feedrate}.",
        }

    normalized_axis_names = tuple(str(axis) for axis in axis_names)
    parsed_targets: dict[str, tuple[float, float]] = {}
    invalid_axes: list[str] = []
    invalid_values: list[str] = []
    unavailable_axes: list[str] = []
    limit_errors: list[str] = []
    for raw_axis, raw_value in targets.items():
        axis = str(raw_axis).strip().upper()
        if axis not in normalized_axis_names:
            invalid_axes.append(str(raw_axis))
            continue
        try:
            display_target = float(raw_value)
        except (TypeError, ValueError):
            invalid_values.append(axis)
            continue
        if not math.isfinite(display_target):
            invalid_values.append(axis)
            continue
        raw_target, resolved_display_target = resolve_axis_target(
            axis,
            display_target,
            input_mode,
        )
        if raw_target is None:
            unavailable_axes.append(axis)
            continue
        limit_error = axis_target_limit_error(axis, resolved_display_target)
        if limit_error is not None:
            limit_errors.append(limit_error)
            continue
        parsed_targets[axis] = (
            float(raw_target),
            float(resolved_display_target),
        )

    if invalid_axes:
        return {
            "accepted": False,
            "status_code": 400,
            "message": f"Unsupported axes: {', '.join(invalid_axes)}.",
        }
    if invalid_values:
        return {
            "accepted": False,
            "status_code": 400,
            "message": f"Invalid coordinate values for: {', '.join(invalid_values)}.",
        }
    if unavailable_axes:
        return {
            "accepted": False,
            "status_code": 409,
            "message": (
                "Coordinates are unavailable in the GUI for: "
                f"{', '.join(unavailable_axes)}."
            ),
        }
    if limit_errors:
        return {
            "accepted": False,
            "status_code": 409,
            "message": " ".join(limit_errors),
        }

    ordered_targets = [
        (axis, *parsed_targets[axis])
        for axis in normalized_axis_names
        if axis in parsed_targets
    ]
    if not ordered_targets:
        return {
            "accepted": False,
            "status_code": 400,
            "message": "Provide at least one target coordinate.",
        }

    axes = [axis for axis, _raw, _display in ordered_targets]
    return ApiCoordinateMovePlan(
        ordered_targets=ordered_targets,
        target_map={
            axis: (raw_target, display_target)
            for axis, raw_target, display_target in ordered_targets
        },
        axes=axes,
        feedrate_mm_min=float(move_feedrate),
        mode=input_mode,
    )


def api_coordinate_move_busy_response() -> dict[str, object]:
    return {
        "accepted": False,
        "status_code": 409,
        "message": "Stage is busy. Ignoring API coordinate target.",
    }


def api_coordinate_move_start_failed_response() -> dict[str, object]:
    return {
        "accepted": False,
        "status_code": 409,
        "message": "Unable to start coordinate move.",
    }


def api_coordinate_move_success_response(
    plan: ApiCoordinateMovePlan,
    *,
    coordinate_display: str,
) -> dict[str, object]:
    return {
        "accepted": True,
        "message": f"API coordinate move accepted: {', '.join(plan.axes)}.",
        "started_axes": list(plan.axes),
        "queued_axes": [],
        "mode": plan.mode,
        "current_feedrate_mm_min": float(plan.feedrate_mm_min),
        "coordinate_display": str(coordinate_display),
        "targets": {
            axis: display_target
            for axis, _raw_target, display_target in plan.ordered_targets
        },
    }


__all__ = [
    "ApiCoordinateMovePlan",
    "api_axis_value_map",
    "api_coordinate_move_busy_response",
    "api_coordinate_move_plan",
    "api_coordinate_move_start_failed_response",
    "api_coordinate_move_success_response",
    "api_move_feedrate",
    "normalize_api_coordinate_input_mode",
]
