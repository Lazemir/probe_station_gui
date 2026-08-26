"""Stage-position update orchestration for the main window."""

from __future__ import annotations

import logging
import math
import time
from typing import Any, Protocol

from PySide6.QtCore import QTimer

from probe_station_gui.coordinates.model import PhysicalMachinePose
from probe_station_gui.design import navigation_adapter as design_navigation
from probe_station_gui.stage import move_lifecycle as stage_move_lifecycle
from probe_station_gui.stage.position_presenter import stage_position_signal_plan
from probe_station_gui.views import main_window_coordinate_flow as coordinate_flow
from probe_station_gui.views import main_window_design_workspace as design_workspace
from probe_station_gui.views import (
    main_window_stage_position_panel as stage_position_panel,
)


logger = logging.getLogger("main")
_CONTROLLER_CACHE_UNSET = object()


class StagePositionUpdateOwner(Protocol):
    STAGE_AXIS_NAMES: tuple[str, ...]
    B_POSITION_CHANGE_TOLERANCE_DEG: float
    MANUAL_JOG_RECONCILE_SMOOTH_ALPHA: float
    _coordinate_targets: Any
    _manual_jog_prediction: Any
    _stage_motion: Any
    _pending_alignment_preparation: Any
    _coordinate_system_coordinator: Any
    _current_design_stage_xy: tuple[float, float] | None
    contact_calibration_window: Any
    stage_controller: Any

    def _can_display_design_position(self) -> bool: ...
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
    *,
    physical_machine_pose: PhysicalMachinePose | None = None,
    motion_coordinate_snapshot: object = _CONTROLLER_CACHE_UNSET,
    homed_axes: object = _CONTROLLER_CACHE_UNSET,
) -> None:
    stage_xy = stage_xy_from_position(position)
    design_stage_xy = (
        stage_xy
        if stage_xy is not None and owner._can_display_design_position()
        else None
    )
    stage_position_panel.update_stage_position_display(owner, position)
    _observe_coordinate_authority(
        owner,
        physical_machine_pose or owner._stage_motion.snapshot().physical_machine_pose,
        motion_coordinate_snapshot=motion_coordinate_snapshot,
        homed_axes=homed_axes,
    )
    owner._update_coordinate_display(center_xy=design_stage_xy)
    owner._update_design_position(design_stage_xy)


def preferred_design_stage_xy(
    owner: StagePositionUpdateOwner,
) -> tuple[float, float] | None:
    if owner._coordinate_targets.stage_position is not None:
        stage_xy = stage_xy_from_position(owner._coordinate_targets.stage_position)
        if stage_xy is not None:
            return stage_xy
    stage_xy = owner._manual_jog_prediction.predicted_stage_xy(time.monotonic())
    if stage_xy is not None:
        return stage_xy
    stage_xy = owner._manual_jog_prediction.resolve_waiting_stage_xy(
        now=time.monotonic(),
        latest_state=owner.stage_controller.latest_stage_state(),
        last_status_timestamp=owner.stage_controller.last_status_timestamp(),
    )
    if stage_xy is not None:
        return stage_xy
    presented_stage_xy = owner._stage_motion.snapshot().presented_stage_xy
    if presented_stage_xy is not None:
        return presented_stage_xy
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
    if owner._can_display_design_position() and latest is not None and len(latest) >= 2:
        try:
            return (float(latest[0]), float(latest[1]))
        except (TypeError, ValueError):
            return None
    return (
        owner._current_design_stage_xy if owner._can_display_design_position() else None
    )


def on_stage_position_changed(
    owner: StagePositionUpdateOwner,
    position: object,
    *,
    physical_machine_pose: PhysicalMachinePose | None = None,
    motion_coordinate_snapshot: object = _CONTROLLER_CACHE_UNSET,
    stage_state: object = _CONTROLLER_CACHE_UNSET,
    homed_axes: object = _CONTROLLER_CACHE_UNSET,
    last_jog_write_timestamp: object = _CONTROLLER_CACHE_UNSET,
) -> None:
    physical_pose = (
        physical_machine_pose
        if physical_machine_pose is not None
        else owner._stage_motion.snapshot().physical_machine_pose
    )
    if not isinstance(position, tuple) or len(position) < 2:
        stage_position_panel.update_stage_position_display(owner, position)
        _observe_coordinate_authority(
            owner,
            physical_pose,
            motion_coordinate_snapshot=motion_coordinate_snapshot,
            homed_axes=homed_axes,
        )
        return
    logger.debug("TIMING stage_position_changed position=%s", position)
    current_position = design_navigation.coerce_position_tuple(position)
    if current_position is not None:
        design_workspace.maybe_restore_persisted_design(owner, current_position)
    now = time.monotonic()
    latest_state = (
        owner.stage_controller.latest_stage_state()
        if stage_state is _CONTROLLER_CACHE_UNSET
        else stage_state
    )
    latest_state = str(latest_state or "").lower()
    if homed_axes is _CONTROLLER_CACHE_UNSET:
        xy_homed = owner.stage_controller.axes_are_homed({"X", "Y"})
        xyz_homed = owner.stage_controller.axes_are_homed({"X", "Y", "Z"})
    else:
        observed_homed_axes = frozenset(homed_axes or ())
        xy_homed = {"X", "Y"}.issubset(observed_homed_axes)
        xyz_homed = {"X", "Y", "Z"}.issubset(observed_homed_axes)
    manual_prediction_available = owner._manual_jog_prediction.prediction_available(now)
    coordinate_snapshot = owner._coordinate_system_coordinator.snapshot()
    signal_plan = _build_stage_position_signal_plan(
        owner,
        position,
        latest_state=latest_state,
        xy_homed=xy_homed,
        xyz_homed=xyz_homed,
        manual_prediction_available=manual_prediction_available,
        registration_valid=(coordinate_snapshot.registration.registration_valid),
    )
    if signal_plan.status.mark_coordinate_move_active:
        owner._coordinate_targets.seen_active_state = True
    if owner.contact_calibration_window is not None:
        owner.contact_calibration_window.set_current_stage_position(
            signal_plan.status.contact_calibration_position
        )
    center_xy = signal_plan.status.center_xy
    if signal_plan.status.use_unhomed_fallback:
        _apply_unhomed_fallback(
            owner,
            position,
            signal_plan,
            center_xy,
            latest_state,
            physical_machine_pose=physical_pose,
            motion_coordinate_snapshot=motion_coordinate_snapshot,
            homed_axes=homed_axes,
        )
        return
    if signal_plan.defer_manual_jog_stop_sample:
        logger.debug(
            "MOTION PREDICTION deferred_stop_sample stage=%s state=%s",
            owner._format_optional_point(center_xy),
            latest_state,
        )
        _observe_coordinate_authority(
            owner,
            physical_pose,
            motion_coordinate_snapshot=motion_coordinate_snapshot,
            homed_axes=homed_axes,
        )
        return
    if _ignore_manual_idle_sample(
        owner,
        center_xy,
        now,
        latest_state,
        last_jog_write_timestamp=last_jog_write_timestamp,
    ):
        _observe_coordinate_authority(
            owner,
            physical_pose,
            motion_coordinate_snapshot=motion_coordinate_snapshot,
            homed_axes=homed_axes,
        )
        return
    center_xy = _reconciled_stage_xy(owner, signal_plan, center_xy, latest_state)
    display_position = position_with_stage_xy(
        owner,
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
    publish_stage_position_estimate(
        owner,
        display_position,
        physical_machine_pose=physical_pose,
        motion_coordinate_snapshot=motion_coordinate_snapshot,
        homed_axes=homed_axes,
    )
    if latest_state == "idle":
        stage_move_lifecycle.finish_coordinate_move_if_idle(
            owner,
            display_position,
            monotonic_s=time.monotonic(),
            schedule_single_shot=QTimer.singleShot,
            latest_stage_state=latest_state,
        )
        stage_position_panel.clear_stage_motion_axes(owner)


def _build_stage_position_signal_plan(
    owner: StagePositionUpdateOwner,
    position: tuple[float, ...],
    *,
    latest_state: str,
    xy_homed: bool,
    xyz_homed: bool,
    manual_prediction_available: bool,
    registration_valid: bool,
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
        last_reported_b_position=None,
        tolerance_deg=owner.B_POSITION_CHANGE_TOLERANCE_DEG,
        pending_alignment_preparation=owner._pending_alignment_preparation,
        registration_valid=registration_valid,
        manual_jog_stage_position=owner._manual_jog_prediction.stage_position,
        coordinate_move_stage_position=owner._coordinate_targets.stage_position,
        planned_move_started_at=None,
        planned_move_waiting_for_fresh_status=False,
        planned_move_stage_xy=None,
        manual_jog_waiting_for_fresh_status=(
            owner._manual_jog_prediction.waiting_for_fresh_status
        ),
        stage_xy_from_position=stage_xy_from_position,
        position_with_stage_xy=lambda stage_xy, *, base_position=None: (
            position_with_stage_xy(
                owner,
                stage_xy,
                base_position=base_position,
            )
        ),
    )


def _apply_unhomed_fallback(
    owner: StagePositionUpdateOwner,
    position: object,
    signal_plan: Any,
    center_xy: tuple[float, float],
    latest_state: str,
    *,
    physical_machine_pose: PhysicalMachinePose,
    motion_coordinate_snapshot: object,
    homed_axes: object,
) -> None:
    _ = center_xy
    stage_position_panel.update_stage_position_display(owner, position)
    _observe_coordinate_authority(
        owner,
        physical_machine_pose,
        motion_coordinate_snapshot=motion_coordinate_snapshot,
        homed_axes=homed_axes,
    )
    owner._manual_jog_prediction.stage_position = None
    owner._manual_jog_prediction.stage_xy = None
    owner._update_coordinate_display(center_xy=None)
    owner._update_design_position(signal_plan.status.unhomed_design_position)
    if latest_state == "idle":
        stage_move_lifecycle.finish_coordinate_move_if_idle(
            owner,
            position,
            monotonic_s=time.monotonic(),
            schedule_single_shot=QTimer.singleShot,
            latest_stage_state=latest_state,
        )
        stage_position_panel.clear_stage_motion_axes(owner)


def _ignore_manual_idle_sample(
    owner: StagePositionUpdateOwner,
    center_xy: tuple[float, float],
    now: float,
    latest_state: str,
    *,
    last_jog_write_timestamp: object,
) -> bool:
    observed_jog_timestamp = last_jog_write_timestamp
    if observed_jog_timestamp is _CONTROLLER_CACHE_UNSET:
        observed_jog_timestamp = owner.stage_controller.last_jog_write_timestamp()
    ignore_result = owner._manual_jog_prediction.ignore_idle_status_sample(
        actual_stage_xy=center_xy,
        now=now,
        latest_state=latest_state,
        last_jog_write_timestamp=observed_jog_timestamp,
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


def _observe_coordinate_authority(
    owner: StagePositionUpdateOwner,
    physical_machine_pose: PhysicalMachinePose,
    *,
    motion_coordinate_snapshot: object,
    homed_axes: object,
) -> None:
    observation_facts = {}
    if motion_coordinate_snapshot is not _CONTROLLER_CACHE_UNSET:
        observation_facts["machine_snapshot"] = motion_coordinate_snapshot
    if homed_axes is not _CONTROLLER_CACHE_UNSET:
        observation_facts["homed_axes"] = homed_axes
    coordinate_flow.observe_coordinate_authority(
        owner,
        physical_machine_pose,
        **observation_facts,
    )


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
    if owner._manual_jog_prediction.waiting_for_fresh_status and latest_state == "idle":
        _learn_manual_stop_tail(owner, signal_plan, display_position)
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
