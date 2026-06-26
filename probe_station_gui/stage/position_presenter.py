from __future__ import annotations

from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class AxisFieldPresentation:
    axis: str
    raw_value: float
    display_value: float
    visible_value: float
    base_background: str
    base_foreground: str
    tooltip: str


@dataclass(frozen=True)
class StagePositionDisplayPlan:
    valid: bool
    homed_axes: frozenset[str]
    axis_updates: tuple[AxisFieldPresentation, ...]
    missing_axes: tuple[str, ...]
    reset_all: bool
    fields_available: bool


@dataclass(frozen=True)
class StagePositionStatusPlan:
    center_xy: tuple[float, float]
    mark_coordinate_move_active: bool
    contact_calibration_position: tuple[float, float, float] | None
    use_unhomed_fallback: bool
    unhomed_design_position: tuple[float, float] | None


@dataclass(frozen=True)
class BAxisRegistrationPlan:
    current_b: float | None
    invalidate_registration: bool
    invalidate_reason: str | None


@dataclass(frozen=True)
class StagePredictionPlan:
    source: str
    predicted_position: tuple[float, ...] | None
    predicted_stage_xy: tuple[float, float] | None
    smooth_predicted_status: bool
    planned_move_active: bool


@dataclass(frozen=True)
class StageReconcilePlan:
    actual_stage_xy: tuple[float, float]
    predicted_stage_xy: tuple[float, float] | None
    log_reconcile: bool
    use_predicted_xy_directly: bool
    smooth_to_actual: bool


@dataclass(frozen=True)
class StagePositionSignalPlan:
    status: StagePositionStatusPlan
    b_axis: BAxisRegistrationPlan
    prediction: StagePredictionPlan
    reconcile: StageReconcilePlan
    defer_manual_jog_stop_sample: bool


def stage_position_display_plan(
    position: object | None,
    *,
    axis_names: tuple[str, ...] | list[str],
    homed_axes: set[str] | frozenset[str],
    limit_axes: set[str] | frozenset[str],
    pending_targets: dict[str, tuple[float, float]],
    display_axis_value: Callable[[str, float], float],
    feedrate_mm_min: float,
) -> StagePositionDisplayPlan:
    if not isinstance(position, tuple) or len(position) < 2:
        return StagePositionDisplayPlan(
            valid=False,
            homed_axes=frozenset(),
            axis_updates=(),
            missing_axes=(),
            reset_all=True,
            fields_available=False,
        )

    normalized_homed_axes = frozenset(str(axis) for axis in homed_axes)
    normalized_limit_axes = frozenset(str(axis) for axis in limit_axes)
    axis_updates: list[AxisFieldPresentation] = []
    missing_axes: list[str] = []

    for axis_name, axis_value in zip(tuple(axis_names), position):
        try:
            raw_value = float(axis_value)
        except (TypeError, ValueError):
            missing_axes.append(axis_name)
            continue
        display_value = float(display_axis_value(axis_name, raw_value))
        if axis_name in normalized_homed_axes:
            background = "#1565c0"
            foreground = "#f5f5f5"
        else:
            background = "#f0b429"
            foreground = "#1f1f1f"
        if axis_name in normalized_limit_axes:
            background = "#c62828"
            foreground = "#ffffff"
        pending_target = pending_targets.get(axis_name)
        visible_value = (
            float(pending_target[1]) if pending_target is not None else display_value
        )
        axis_updates.append(
            AxisFieldPresentation(
                axis=axis_name,
                raw_value=raw_value,
                display_value=display_value,
                visible_value=visible_value,
                base_background=background,
                base_foreground=foreground,
                tooltip=(
                    f"{axis_name} coordinate. Enter targets and press Apply. "
                    f"Move feedrate: {float(feedrate_mm_min):.1f} mm/min."
                ),
            )
        )

    updated_axes = {item.axis for item in axis_updates}
    for axis_name in tuple(axis_names):
        if axis_name not in updated_axes and axis_name not in missing_axes:
            missing_axes.append(axis_name)

    return StagePositionDisplayPlan(
        valid=True,
        homed_axes=normalized_homed_axes,
        axis_updates=tuple(axis_updates),
        missing_axes=tuple(missing_axes),
        reset_all=False,
        fields_available=bool(axis_updates),
    )


def stage_position_status_plan(
    position: tuple[float, ...],
    *,
    latest_state: str,
    coordinate_move_axis_active: bool,
    xy_homed: bool,
    xyz_homed: bool,
    manual_jog_prediction_available: bool,
    can_display_design_position: bool,
) -> StagePositionStatusPlan:
    center_xy = (float(position[0]), float(position[1]))
    contact_position = None
    if len(position) >= 3 and xyz_homed:
        contact_position = (
            float(position[0]),
            float(position[1]),
            float(position[2]),
        )
    use_unhomed_fallback = not xy_homed and not manual_jog_prediction_available
    return StagePositionStatusPlan(
        center_xy=center_xy,
        mark_coordinate_move_active=bool(
            coordinate_move_axis_active and latest_state in {"run", "jog"}
        ),
        contact_calibration_position=contact_position,
        use_unhomed_fallback=use_unhomed_fallback,
        unhomed_design_position=center_xy if can_display_design_position else None,
    )


def b_axis_registration_plan(
    position: object | None,
    *,
    last_reported_b_position: float | None,
    tolerance_deg: float,
    pending_alignment_preparation: object | None,
    registration_valid: bool,
) -> BAxisRegistrationPlan:
    if not isinstance(position, tuple) or len(position) <= 4:
        return BAxisRegistrationPlan(
            current_b=None,
            invalidate_registration=False,
            invalidate_reason=None,
        )
    current_b = float(position[4])
    should_invalidate = bool(
        last_reported_b_position is not None
        and pending_alignment_preparation is None
        and abs(current_b - last_reported_b_position) > float(tolerance_deg)
        and registration_valid
    )
    return BAxisRegistrationPlan(
        current_b=current_b,
        invalidate_registration=should_invalidate,
        invalidate_reason=(
            "Design registration cleared after B-axis motion."
            if should_invalidate
            else None
        ),
    )


def predicted_stage_position_plan(
    *,
    manual_jog_prediction_available: bool,
    manual_jog_stage_position: object | None,
    coordinate_move_stage_position: object | None,
    planned_move_started_at: float | None,
    planned_move_waiting_for_fresh_status: bool,
    planned_move_stage_xy: tuple[float, float] | None,
    position: tuple[float, ...],
    stage_xy_from_position: Callable[[object | None], tuple[float, float] | None],
    position_with_stage_xy: Callable[
        [tuple[float, float]], tuple[float, ...] | None
    ],
) -> StagePredictionPlan:
    source = "raw_status"
    predicted_position = None
    smooth_predicted_status = False

    if manual_jog_prediction_available:
        source = "manual_jog"
        predicted_position = _coerce_position_tuple(manual_jog_stage_position)
        smooth_predicted_status = True
    elif coordinate_move_stage_position is not None:
        source = "coordinate_move"
        predicted_position = _coerce_position_tuple(coordinate_move_stage_position)
    elif (
        planned_move_stage_xy is not None
        and (
            planned_move_started_at is not None
            or bool(planned_move_waiting_for_fresh_status)
        )
    ):
        source = "planned_move"
        predicted_position = position_with_stage_xy(
            planned_move_stage_xy,
            base_position=position,
        )
        smooth_predicted_status = True

    predicted_stage_xy = stage_xy_from_position(predicted_position)
    planned_move_active = (
        planned_move_started_at is not None and predicted_stage_xy is not None
    )
    return StagePredictionPlan(
        source=source,
        predicted_position=predicted_position,
        predicted_stage_xy=predicted_stage_xy,
        smooth_predicted_status=smooth_predicted_status,
        planned_move_active=planned_move_active,
    )


def stage_reconcile_plan(
    *,
    actual_stage_xy: tuple[float, float],
    predicted_stage_xy: tuple[float, float] | None,
    smooth_predicted_status: bool,
    planned_move_active: bool,
) -> StageReconcilePlan:
    return StageReconcilePlan(
        actual_stage_xy=actual_stage_xy,
        predicted_stage_xy=predicted_stage_xy,
        log_reconcile=predicted_stage_xy is not None,
        use_predicted_xy_directly=bool(
            predicted_stage_xy is not None
            and smooth_predicted_status
            and planned_move_active
        ),
        smooth_to_actual=bool(
            predicted_stage_xy is not None
            and smooth_predicted_status
            and not planned_move_active
        ),
    )


def stage_position_signal_plan(
    position: tuple[float, ...],
    *,
    latest_state: str,
    coordinate_move_axis_active: bool,
    xy_homed: bool,
    xyz_homed: bool,
    manual_jog_prediction_available: bool,
    can_display_design_position: bool,
    last_reported_b_position: float | None,
    tolerance_deg: float,
    pending_alignment_preparation: object | None,
    registration_valid: bool,
    manual_jog_stage_position: object | None,
    coordinate_move_stage_position: object | None,
    planned_move_started_at: float | None,
    planned_move_waiting_for_fresh_status: bool,
    planned_move_stage_xy: tuple[float, float] | None,
    manual_jog_waiting_for_fresh_status: bool,
    stage_xy_from_position: Callable[[object | None], tuple[float, float] | None],
    position_with_stage_xy: Callable[
        [tuple[float, float]], tuple[float, ...] | None
    ],
) -> StagePositionSignalPlan:
    status = stage_position_status_plan(
        position,
        latest_state=latest_state,
        coordinate_move_axis_active=coordinate_move_axis_active,
        xy_homed=xy_homed,
        xyz_homed=xyz_homed,
        manual_jog_prediction_available=manual_jog_prediction_available,
        can_display_design_position=can_display_design_position,
    )
    b_axis = b_axis_registration_plan(
        position,
        last_reported_b_position=last_reported_b_position,
        tolerance_deg=tolerance_deg,
        pending_alignment_preparation=pending_alignment_preparation,
        registration_valid=registration_valid,
    )
    prediction = predicted_stage_position_plan(
        manual_jog_prediction_available=manual_jog_prediction_available,
        manual_jog_stage_position=manual_jog_stage_position,
        coordinate_move_stage_position=coordinate_move_stage_position,
        planned_move_started_at=planned_move_started_at,
        planned_move_waiting_for_fresh_status=planned_move_waiting_for_fresh_status,
        planned_move_stage_xy=planned_move_stage_xy,
        position=position,
        stage_xy_from_position=stage_xy_from_position,
        position_with_stage_xy=position_with_stage_xy,
    )
    reconcile = stage_reconcile_plan(
        actual_stage_xy=status.center_xy,
        predicted_stage_xy=prediction.predicted_stage_xy,
        smooth_predicted_status=prediction.smooth_predicted_status,
        planned_move_active=prediction.planned_move_active,
    )
    return StagePositionSignalPlan(
        status=status,
        b_axis=b_axis,
        prediction=prediction,
        reconcile=reconcile,
        defer_manual_jog_stop_sample=bool(
            manual_jog_waiting_for_fresh_status and latest_state != "idle"
        ),
    )


def _coerce_position_tuple(position: object | None) -> tuple[float, ...] | None:
    if not isinstance(position, (tuple, list)):
        return None
    try:
        return tuple(float(value) for value in position)
    except (TypeError, ValueError):
        return None
