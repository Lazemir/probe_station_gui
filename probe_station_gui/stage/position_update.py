"""Stage-position update orchestration for the main window."""

from __future__ import annotations

import logging
import math
import time
from typing import Any, Protocol

from probe_station_gui.stage.position_presenter import stage_position_signal_plan


logger = logging.getLogger("main")


class StagePositionUpdateOwner(Protocol):
    STAGE_AXIS_NAMES: tuple[str, ...]
    B_POSITION_CHANGE_TOLERANCE_DEG: float
    MANUAL_JOG_RECONCILE_SMOOTH_ALPHA: float
    _coordinate_targets: Any
    _manual_jog_prediction: Any
    _planned_move_stage_xy: tuple[float, float] | None
    _planned_move_started_at: float | None
    _planned_move_waiting_for_fresh_status: bool
    _planned_move_stop_status_timestamp: float | None
    _pending_alignment_preparation: Any
    _last_reported_b_position: float | None
    _design_session: Any
    _current_design_stage_xy: tuple[float, float] | None
    contact_calibration_window: Any
    stage_controller: Any

    def _coerce_position_tuple(self, value: object) -> tuple[float, ...] | None: ...
    def _stage_xy_from_position(self, position: object | None) -> tuple[float, float] | None: ...
    def _position_with_stage_xy(
        self,
        stage_xy: tuple[float, float],
        *,
        base_position: object | None = None,
    ) -> tuple[float, ...]: ...
    def _can_display_design_position(self) -> bool: ...
    def _maybe_restore_persisted_design(self, position: tuple[float, ...]) -> None: ...
    def _update_coordinate_display(
        self,
        *,
        center_xy: tuple[float, float] | None = None,
        cursor_xy: tuple[float, float] | None = None,
    ) -> None: ...
    def _update_design_position(self, stage_xy: tuple[float, float] | None) -> None: ...
    def _invalidate_design_registration(self, message: str) -> None: ...
    def _log_design_position_reconcile(
        self,
        predicted_stage_xy: tuple[float, float],
        actual_stage_xy: tuple[float, float],
    ) -> None: ...
    def _format_optional_point(self, point: tuple[float, float] | None) -> str: ...
    def _finish_coordinate_move_if_idle(self, position: object | None = None) -> None: ...
    def _clear_stage_motion_axes(self) -> None: ...
    def _update_stage_position_display(self, position: object | None) -> None: ...
    def _publish_stage_position_estimate(
        self,
        position: tuple[float, ...] | None,
    ) -> None: ...


def stage_xy_from_position(position: object | None) -> tuple[float, float] | None:
    if not isinstance(position, (tuple, list)) or len(position) < 2:
        return None
    try:
        return (float(position[0]), float(position[1]))
    except (TypeError, ValueError):
        return None


def position_with_stage_xy(
    owner: StagePositionUpdateOwner,
    stage_xy: tuple[float, float],
    *,
    base_position: object | None = None,
) -> tuple[float, ...]:
    position = base_position
    if not isinstance(position, (tuple, list)) or len(position) < 2:
        position = owner.stage_controller.latest_stage_position()
    try:
        values = (
            [float(value) for value in position]
            if isinstance(position, (tuple, list)) and len(position) >= 2
            else []
        )
    except (TypeError, ValueError):
        values = []
    if len(values) < 2:
        values = [float(stage_xy[0]), float(stage_xy[1])]
    else:
        values[0] = float(stage_xy[0])
        values[1] = float(stage_xy[1])
    return tuple(values)


def seed_motion_prediction_position(
    owner: StagePositionUpdateOwner,
) -> tuple[float, ...] | None:
    return owner._manual_jog_prediction.seed_position(
        coordinate_move_stage_position=owner._coordinate_targets.stage_position,
        latest_stage_position=owner.stage_controller.latest_stage_position(),
        current_design_stage_xy=owner._current_design_stage_xy,
    )


def publish_stage_position_estimate(
    owner: StagePositionUpdateOwner,
    position: tuple[float, ...] | None,
) -> None:
    stage_xy = owner._stage_xy_from_position(position)
    design_stage_xy = (
        stage_xy
        if stage_xy is not None and owner._can_display_design_position()
        else None
    )
    owner._update_stage_position_display(position)
    owner._update_coordinate_display(center_xy=design_stage_xy)
    owner._update_design_position(design_stage_xy)


def preferred_design_stage_xy(
    owner: StagePositionUpdateOwner,
) -> tuple[float, float] | None:
    if owner._coordinate_targets.stage_position is not None:
        stage_xy = owner._stage_xy_from_position(owner._coordinate_targets.stage_position)
        if stage_xy is not None:
            return stage_xy
    stage_xy = owner._manual_jog_prediction.predicted_stage_xy(time.monotonic())
    if stage_xy is not None:
        return stage_xy
    if (
        owner._planned_move_started_at is not None
        and owner._planned_move_stage_xy is not None
    ):
        return owner._planned_move_stage_xy
    stage_xy = owner._manual_jog_prediction.resolve_waiting_stage_xy(
        now=time.monotonic(),
        latest_state=owner.stage_controller.latest_stage_state(),
        last_status_timestamp=owner.stage_controller.last_status_timestamp(),
    )
    if stage_xy is not None:
        return stage_xy
    if owner._planned_move_waiting_for_fresh_status:
        last_status_timestamp = owner.stage_controller.last_status_timestamp()
        if (
            last_status_timestamp is not None
            and owner._planned_move_stop_status_timestamp is not None
            and last_status_timestamp > owner._planned_move_stop_status_timestamp
        ):
            owner._planned_move_waiting_for_fresh_status = False
            owner._planned_move_stop_status_timestamp = None
        elif owner._planned_move_stage_xy is not None:
            return owner._planned_move_stage_xy
        else:
            owner._planned_move_waiting_for_fresh_status = False
            owner._planned_move_stop_status_timestamp = None
    latest = owner.stage_controller.latest_stage_position()
    if (
        latest is None
        or len(latest) < 2
        or not owner.stage_controller.axes_are_homed({"X", "Y"})
    ):
        return None
    return (float(latest[0]), float(latest[1]))


def preferred_design_display_stage_xy(
    owner: StagePositionUpdateOwner,
) -> tuple[float, float] | None:
    stage_xy = preferred_design_stage_xy(owner)
    if stage_xy is not None:
        return stage_xy
    latest = owner.stage_controller.latest_stage_position()
    if (
        owner._can_display_design_position()
        and latest is not None
        and len(latest) >= 2
    ):
        try:
            return (float(latest[0]), float(latest[1]))
        except (TypeError, ValueError):
            return None
    return (
        owner._current_design_stage_xy
        if owner._can_display_design_position()
        else None
    )


def on_stage_position_changed(
    owner: StagePositionUpdateOwner,
    position: object,
) -> None:
    if not isinstance(position, tuple) or len(position) < 2:
        owner._update_stage_position_display(position)
        return
    logger.debug("TIMING stage_position_changed position=%s", position)
    current_position = owner._coerce_position_tuple(position)
    if current_position is not None:
        owner._maybe_restore_persisted_design(current_position)
    now = time.monotonic()
    latest_state = (owner.stage_controller.latest_stage_state() or "").lower()
    xy_homed = owner.stage_controller.axes_are_homed({"X", "Y"})
    xyz_homed = owner.stage_controller.axes_are_homed({"X", "Y", "Z"})
    manual_prediction_available = owner._manual_jog_prediction.prediction_available(now)
    design_session = getattr(owner, "_design_session", None)
    registration = getattr(design_session, "registration", None)
    signal_plan = _build_stage_position_signal_plan(
        owner,
        position,
        latest_state=latest_state,
        xy_homed=xy_homed,
        xyz_homed=xyz_homed,
        manual_prediction_available=manual_prediction_available,
        registration=registration,
    )
    if signal_plan.status.mark_coordinate_move_active:
        owner._coordinate_targets.seen_active_state = True
    if owner.contact_calibration_window is not None:
        owner.contact_calibration_window.set_current_stage_position(
            signal_plan.status.contact_calibration_position
        )
    center_xy = signal_plan.status.center_xy
    if signal_plan.status.use_unhomed_fallback:
        _apply_unhomed_fallback(owner, position, signal_plan, center_xy, latest_state)
        return
    _apply_b_axis_registration(owner, signal_plan)
    if signal_plan.defer_manual_jog_stop_sample:
        logger.debug(
            "MOTION PREDICTION deferred_stop_sample stage=%s state=%s",
            owner._format_optional_point(center_xy),
            latest_state,
        )
        return
    if _ignore_manual_idle_sample(owner, center_xy, now, latest_state):
        return
    center_xy = _reconciled_stage_xy(owner, signal_plan, center_xy, latest_state)
    display_position = owner._position_with_stage_xy(
        center_xy,
        base_position=position,
    )
    _update_position_prediction_state(
        owner,
        signal_plan,
        center_xy,
        display_position,
        now,
        latest_state,
    )
    owner._publish_stage_position_estimate(display_position)
    if latest_state == "idle":
        owner._finish_coordinate_move_if_idle(display_position)
        owner._clear_stage_motion_axes()


def _build_stage_position_signal_plan(
    owner: StagePositionUpdateOwner,
    position: tuple[float, ...],
    *,
    latest_state: str,
    xy_homed: bool,
    xyz_homed: bool,
    manual_prediction_available: bool,
    registration: object | None,
) -> Any:
    return stage_position_signal_plan(
        position,
        latest_state=latest_state,
        coordinate_move_axis_active=owner._coordinate_targets.has_active_move(),
        xy_homed=xy_homed,
        xyz_homed=xyz_homed,
        manual_jog_prediction_available=manual_prediction_available,
        can_display_design_position=(
            owner._can_display_design_position()
            if not xy_homed and not manual_prediction_available
            else False
        ),
        last_reported_b_position=owner._last_reported_b_position,
        tolerance_deg=owner.B_POSITION_CHANGE_TOLERANCE_DEG,
        pending_alignment_preparation=owner._pending_alignment_preparation,
        registration_valid=bool(
            registration is not None and getattr(registration, "valid", False)
        ),
        manual_jog_stage_position=owner._manual_jog_prediction.stage_position,
        coordinate_move_stage_position=owner._coordinate_targets.stage_position,
        planned_move_started_at=owner._planned_move_started_at,
        planned_move_waiting_for_fresh_status=(
            owner._planned_move_waiting_for_fresh_status
        ),
        planned_move_stage_xy=owner._planned_move_stage_xy,
        manual_jog_waiting_for_fresh_status=(
            owner._manual_jog_prediction.waiting_for_fresh_status
        ),
        stage_xy_from_position=owner._stage_xy_from_position,
        position_with_stage_xy=owner._position_with_stage_xy,
    )


def _apply_b_axis_registration(
    owner: StagePositionUpdateOwner,
    signal_plan: Any,
) -> None:
    if signal_plan.b_axis.invalidate_reason is not None:
        owner._invalidate_design_registration(signal_plan.b_axis.invalidate_reason)
    if signal_plan.b_axis.current_b is not None:
        owner._last_reported_b_position = signal_plan.b_axis.current_b


def _apply_unhomed_fallback(
    owner: StagePositionUpdateOwner,
    position: object,
    signal_plan: Any,
    center_xy: tuple[float, float],
    latest_state: str,
) -> None:
    _ = center_xy
    owner._update_stage_position_display(position)
    owner._manual_jog_prediction.stage_position = None
    owner._manual_jog_prediction.stage_xy = None
    owner._planned_move_stage_xy = None
    owner._update_coordinate_display(center_xy=None)
    owner._update_design_position(signal_plan.status.unhomed_design_position)
    if latest_state == "idle":
        owner._finish_coordinate_move_if_idle(position)
        owner._clear_stage_motion_axes()


def _ignore_manual_idle_sample(
    owner: StagePositionUpdateOwner,
    center_xy: tuple[float, float],
    now: float,
    latest_state: str,
) -> bool:
    ignore_result = owner._manual_jog_prediction.ignore_idle_status_sample(
        actual_stage_xy=center_xy,
        now=now,
        latest_state=latest_state,
        last_jog_write_timestamp=owner.stage_controller.last_jog_write_timestamp(),
    )
    if not ignore_result.ignore:
        return False
    logger.debug(
        "MOTION PREDICTION ignored_idle_sample stage=%s age=%.3f state=%s",
        owner._format_optional_point(center_xy),
        ignore_result.age_s,
        ignore_result.state,
    )
    return True


def _reconciled_stage_xy(
    owner: StagePositionUpdateOwner,
    signal_plan: Any,
    center_xy: tuple[float, float],
    latest_state: str,
) -> tuple[float, float]:
    predicted_stage_xy = signal_plan.reconcile.predicted_stage_xy
    if predicted_stage_xy is None:
        return center_xy
    owner._log_design_position_reconcile(predicted_stage_xy, center_xy)
    if signal_plan.reconcile.use_predicted_xy_directly:
        return predicted_stage_xy
    if not signal_plan.reconcile.smooth_to_actual:
        return center_xy
    smoothed_stage_xy = owner._manual_jog_prediction.smooth_actual_stage_xy(
        predicted_stage_xy,
        center_xy,
        latest_state=latest_state,
    )
    if smoothed_stage_xy != center_xy:
        delta_x = float(center_xy[0] - predicted_stage_xy[0])
        delta_y = float(center_xy[1] - predicted_stage_xy[1])
        delta_norm = math.hypot(delta_x, delta_y)
        logger.debug(
            "MOTION PREDICTION reconcile_smoothed predicted_stage=%s actual_stage=%s smoothed_stage=%s delta_norm=%.4f alpha=%.2f",
            owner._format_optional_point(predicted_stage_xy),
            owner._format_optional_point(center_xy),
            owner._format_optional_point(smoothed_stage_xy),
            delta_norm,
            owner.MANUAL_JOG_RECONCILE_SMOOTH_ALPHA,
        )
    return smoothed_stage_xy


def _update_position_prediction_state(
    owner: StagePositionUpdateOwner,
    signal_plan: Any,
    center_xy: tuple[float, float],
    display_position: tuple[float, ...],
    now: float,
    latest_state: str,
) -> None:
    owner._manual_jog_prediction.stage_position = display_position
    owner._manual_jog_prediction.stage_xy = center_xy
    if not signal_plan.prediction.planned_move_active:
        owner._planned_move_stage_xy = center_xy
    if owner._manual_jog_prediction.waiting_for_fresh_status and latest_state == "idle":
        _learn_manual_stop_tail(owner, signal_plan, display_position)
    if owner._planned_move_waiting_for_fresh_status:
        owner._planned_move_waiting_for_fresh_status = False
        owner._planned_move_stop_status_timestamp = None
    if owner._manual_jog_prediction.prediction_active(now):
        owner._manual_jog_prediction.last_timestamp = now


def _learn_manual_stop_tail(
    owner: StagePositionUpdateOwner,
    signal_plan: Any,
    display_position: tuple[float, ...],
) -> None:
    learn_result = owner._manual_jog_prediction.learn_stop_tail(
        owner._manual_jog_prediction.stop_tail_position
        or signal_plan.prediction.predicted_position,
        display_position,
    )
    if learn_result is not None:
        logger.debug(
            "MOTION PREDICTION stop_tail_learn old=%.4f residual=%.4f learned=%.4f new=%.4f",
            learn_result.old_tail_s,
            learn_result.residual_s,
            learn_result.learned_tail_s,
            learn_result.new_tail_s,
        )
    owner._manual_jog_prediction.clear_waiting_status()
    owner._manual_jog_prediction.clear_stop_prediction()


__all__ = [
    "on_stage_position_changed",
    "position_with_stage_xy",
    "preferred_design_display_stage_xy",
    "preferred_design_stage_xy",
    "publish_stage_position_estimate",
    "seed_motion_prediction_position",
    "stage_xy_from_position",
]
