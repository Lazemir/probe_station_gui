"""GUI-thread owner for transient Stage motion interpretation."""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, replace

from PySide6.QtCore import QObject, QThread, QTimer, Signal, Slot

from probe_station_gui.coordinates.model import PhysicalMachinePose
from probe_station_gui.stage.controller import StageController
from probe_station_gui.stage.coordinate_targets import (
    CoordinateTargetConfig,
    CoordinateTargetMoveState,
)
from probe_station_gui.stage.exact_step import ExactStepAccumulator
from probe_station_gui.stage.manual_jog_prediction import (
    ManualJogPredictionConfig,
    ManualJogPredictionState,
)
from probe_station_gui.stage.motion_prediction import (
    coerce_finite_xy,
    held_planned_xy,
    matching_planned_xy_start,
    motion_progress,
    planned_xy_matches,
    planned_xy_timing,
)
from probe_station_gui.stage import types as stage_types
from probe_station_gui.stage.machine_coordinates import MachineCoordinateSnapshot


@dataclass(frozen=True)
class StageMotionConfig:
    axis_names: tuple[str, ...]
    prediction_interval_ms: int
    planned_move_duration_padding_s: float
    planned_start_tolerance_mm: float
    min_feedrate_mm_min: float
    b_position_change_tolerance_deg: float
    manual_jog: ManualJogPredictionConfig
    coordinate_target: CoordinateTargetConfig


@dataclass(frozen=True)
class PlannedXYMoveRequest:
    target_stage_xy: tuple[float, float]
    source_label: str
    feedrate_mm_min: float | None = None


@dataclass(frozen=True)
class StageMotionSnapshot:
    presented_position: tuple[float, ...] | None
    presented_stage_xy: tuple[float, float] | None
    physical_machine_pose: PhysicalMachinePose
    active_axes: frozenset[str]
    cancelable: bool
    coordinate_active: bool
    coordinate_display_basis: object | None
    pending_edit_axes: frozenset[str]
    manual_prediction_active: bool
    planned_pending_target_xy: tuple[float, float] | None
    planned_pending_source_label: str | None
    planned_stage_xy: tuple[float, float] | None
    planned_prediction_active: bool
    planned_waiting_for_fresh_status: bool
    last_reported_b_position: float | None


@dataclass(frozen=True)
class StageMotionPresentation:
    reported_position: object | None
    presented_position: tuple[float, ...] | None
    raw_stage_xy: tuple[float, float] | None
    presented_stage_xy: tuple[float, float] | None
    physical_machine_pose: PhysicalMachinePose
    contact_calibration_position: tuple[float, float, float] | None
    b_position: float | None
    active_axes: frozenset[str]
    unhomed_fallback: bool
    clear_motion_axes: bool
    motion_coordinate_snapshot: MachineCoordinateSnapshot | None = None
    stage_state: str | None = None
    homed_axes: frozenset[str] = frozenset()
    status_timestamp: float | None = None
    last_jog_write_timestamp: float | None = None
    material_change: bool = True


@dataclass(frozen=True)
class StageMotionActionState:
    active_axes: frozenset[str]
    cancelable: bool
    coordinate_active: bool
    planned_pending: bool
    planned_active: bool


class _StageMotionSession(QObject):
    presentation_changed = Signal(object)
    action_state_changed = Signal(object)
    click_move_finished = Signal(bool)
    status_requested = Signal(str, int)

    def __init__(
        self,
        controller: StageController,
        config: StageMotionConfig,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._controller = controller
        self._config = config
        self._manual_jog_prediction = ManualJogPredictionState(config.manual_jog)
        self._coordinate_targets = CoordinateTargetMoveState(config.coordinate_target)
        self._exact_step_accumulator = ExactStepAccumulator(config.axis_names)
        self._prediction_timer = QTimer(self)
        self._prediction_timer.setInterval(int(config.prediction_interval_ms))
        self._prediction_timer.timeout.connect(self.tick)

        self._presented_position: tuple[float, ...] | None = None
        self._presented_stage_xy: tuple[float, float] | None = None
        self._physical_machine_pose = PhysicalMachinePose.from_mapping({})
        self._active_axes: frozenset[str] = frozenset()
        self._pending_edit_axes: frozenset[str] = frozenset()
        self._last_reported_b_position: float | None = None
        self._pending_planned_target_xy: tuple[float, float] | None = None
        self._pending_planned_source_label: str | None = None
        self._planned_origin_xy: tuple[float, float] | None = None
        self._planned_stage_xy: tuple[float, float] | None = None
        self._planned_target_xy: tuple[float, float] | None = None
        self._planned_started_at: float | None = None
        self._planned_ends_at: float | None = None
        self._planned_waiting_for_fresh_status = False
        self._planned_stop_status_timestamp: float | None = None
        self._next_planned_motion_token = 1
        self._planned_motion_token: object | None = None
        self._accepted_planned_target_xy: tuple[float, float] | None = None
        self._last_observation = _empty_position_observation()
        self._last_presentation = self._presentation(
            reported_position=None,
            raw_stage_xy=None,
            contact_calibration_position=None,
            unhomed_fallback=False,
            clear_motion_axes=False,
        )

    def snapshot(self) -> StageMotionSnapshot:
        return StageMotionSnapshot(
            presented_position=self._presented_position,
            presented_stage_xy=self._presented_stage_xy,
            physical_machine_pose=self._physical_machine_pose,
            active_axes=self._active_axes,
            cancelable=False,
            coordinate_active=self._coordinate_targets.has_active_move(),
            coordinate_display_basis=self._coordinate_targets.display_basis,
            pending_edit_axes=self._pending_edit_axes,
            manual_prediction_active=self._manual_jog_prediction.prediction_active(),
            planned_pending_target_xy=self._pending_planned_target_xy,
            planned_pending_source_label=self._pending_planned_source_label,
            planned_stage_xy=self._planned_stage_xy,
            planned_prediction_active=self._planned_started_at is not None,
            planned_waiting_for_fresh_status=(self._planned_waiting_for_fresh_status),
            last_reported_b_position=self._last_reported_b_position,
        )

    def request_planned_xy_move(self, request: PlannedXYMoveRequest) -> bool:
        self._require_object_thread()
        if self._planned_request_in_flight():
            return False
        target = coerce_finite_xy(request.target_stage_xy)
        if target is None:
            return False
        source_label = str(request.source_label).strip() or "absolute XY move"
        motion_token = self._next_planned_motion_token
        self._next_planned_motion_token += 1
        self._pending_planned_target_xy = target
        self._pending_planned_source_label = source_label
        accepted = bool(
            self._controller.request_move_to_xy(
                target[0],
                target[1],
                motion_token=motion_token,
            )
        )
        if not accepted:
            self._clear_planned_prediction(clear_wait_state=True)
            return False
        self._accepted_planned_target_xy = target
        self._planned_motion_token = motion_token
        self._emit_action_state()
        return True

    def _planned_request_in_flight(self) -> bool:
        return bool(
            self._pending_planned_target_xy is not None
            or self._planned_started_at is not None
            or self._planned_waiting_for_fresh_status
            or self._planned_motion_token is not None
        )

    def cancel_planned_xy_move(self) -> bool:
        """Clear only planned-XY interpretation; callers retain motion policy."""

        self._require_object_thread()
        if not self._planned_request_in_flight():
            return False
        self._clear_planned_prediction(clear_wait_state=True)
        self._emit_presentation(
            reported_position=self._presented_position,
            raw_stage_xy=self._presented_stage_xy,
            contact_calibration_position=None,
            unhomed_fallback=False,
            clear_motion_axes=False,
        )
        self._emit_action_state()
        return True

    def refresh_physical_machine_pose(self) -> None:
        """Refresh the owned pose from the controller's synchronized cache."""

        self._require_object_thread()
        self._physical_machine_pose = self._controller.latest_physical_machine_pose(
            self._config.axis_names
        )
        self._last_presentation = replace(
            self._last_presentation,
            physical_machine_pose=self._physical_machine_pose,
        )
        self.presentation_changed.emit(self._last_presentation)

    def reset(self, reason: stage_types.StageMotionResetReason) -> None:
        """Reset state owned by the current planned/position slice."""

        self._require_object_thread()
        if not isinstance(reason, stage_types.StageMotionResetReason):
            raise TypeError("reason must be a StageMotionResetReason")
        self._clear_planned_prediction(clear_wait_state=True)
        self._presented_position = None
        self._presented_stage_xy = None
        self._physical_machine_pose = PhysicalMachinePose.from_mapping({})
        self._last_reported_b_position = None
        self._last_observation = _empty_position_observation()
        self._emit_presentation(
            reported_position=None,
            raw_stage_xy=None,
            contact_calibration_position=None,
            unhomed_fallback=False,
            clear_motion_axes=True,
        )
        self._emit_action_state()

    @Slot(object)
    def on_tracked_absolute_xy_move_started(
        self,
        started: stage_types.TrackedAbsoluteXYMoveStarted,
    ) -> None:
        self._require_object_thread()
        if not isinstance(started, stage_types.TrackedAbsoluteXYMoveStarted):
            raise TypeError("started must be a TrackedAbsoluteXYMoveStarted")
        if not self._owns_planned_motion(started.motion_token):
            return
        pending_target = self._pending_planned_target_xy
        if pending_target is None:
            return
        start = matching_planned_xy_start(
            started.target_stage_xy[0],
            started.target_stage_xy[1],
            started.feedrate_mm_min,
            pending_target=pending_target,
            tolerance_mm=self._config.planned_start_tolerance_mm,
        )
        if start is None:
            self._clear_planned_prediction(clear_wait_state=True)
            self._emit_action_state()
            return
        target, feedrate = start
        source_label = self._pending_planned_source_label or "absolute XY move"
        self._pending_planned_target_xy = None
        self._pending_planned_source_label = None
        self._start_planned_prediction(
            target,
            origin_position=started.origin_position,
            source_label=source_label,
            feedrate_mm_min=feedrate,
        )

    @Slot(object)
    def on_stage_position_changed(
        self,
        observation: stage_types.StagePositionObservation,
    ) -> None:
        self._require_object_thread()
        if not isinstance(observation, stage_types.StagePositionObservation):
            raise TypeError("observation must be a StagePositionObservation")
        if observation.reset_reason is not None:
            self.reset(observation.reset_reason)
            return
        previous_observation = self._last_observation
        self._last_observation = observation
        material_change = not previous_observation.has_same_motion_facts_as(observation)
        position = observation.position
        current_position = _coerce_position_tuple(position)
        if current_position is None or len(current_position) < 2:
            presentation_was_available = self._presented_position is not None
            self._physical_machine_pose = PhysicalMachinePose.from_mapping({})
            self._last_observation = replace(
                observation,
                physical_machine_pose=self._physical_machine_pose,
            )
            self._presented_position = None
            self._presented_stage_xy = None
            self._emit_presentation(
                reported_position=position,
                raw_stage_xy=None,
                contact_calibration_position=None,
                unhomed_fallback=False,
                clear_motion_axes=False,
                material_change=material_change or presentation_was_available,
            )
            self._emit_action_state()
            return

        self._physical_machine_pose = observation.physical_machine_pose
        raw_stage_xy = (current_position[0], current_position[1])
        b_position = _axis_value(current_position, self._config.axis_names, "B")
        if b_position is not None:
            self._last_reported_b_position = b_position
        xy_homed = {"X", "Y"}.issubset(observation.homed_axes)
        xyz_homed = {"X", "Y", "Z"}.issubset(observation.homed_axes)
        unhomed_fallback = bool(
            not xy_homed and not self._manual_jog_prediction.prediction_available()
        )
        was_waiting_for_fresh_status = self._planned_waiting_for_fresh_status
        if unhomed_fallback:
            self._planned_stage_xy = None
            self._planned_waiting_for_fresh_status = False
            self._planned_stop_status_timestamp = None
            self._presented_stage_xy = raw_stage_xy
        else:
            self._presented_stage_xy = self._resolved_reported_stage_xy(
                raw_stage_xy,
                status_timestamp=observation.status_timestamp,
            )
        self._presented_position = _position_with_stage_xy(
            current_position,
            self._presented_stage_xy,
        )
        latest_state = str(observation.stage_state or "").lower()
        clear_motion_axes = latest_state == "idle"
        if clear_motion_axes:
            self._active_axes = frozenset()
        contact_position = (
            (current_position[0], current_position[1], current_position[2])
            if xyz_homed and len(current_position) >= 3
            else None
        )
        self._emit_presentation(
            reported_position=current_position,
            raw_stage_xy=raw_stage_xy,
            contact_calibration_position=contact_position,
            unhomed_fallback=unhomed_fallback,
            clear_motion_axes=clear_motion_axes,
            material_change=(
                material_change
                or (
                    was_waiting_for_fresh_status
                    and not self._planned_waiting_for_fresh_status
                )
            ),
        )
        self._emit_action_state()

    @Slot(object)
    def on_tracked_absolute_xy_move_finished(
        self,
        finished: stage_types.TrackedAbsoluteXYMoveFinished,
    ) -> None:
        self._require_object_thread()
        if not isinstance(finished, stage_types.TrackedAbsoluteXYMoveFinished):
            raise TypeError("finished must be a TrackedAbsoluteXYMoveFinished")
        if not self._owns_planned_motion(finished.motion_token):
            return
        accepted_target = self._accepted_planned_target_xy
        if accepted_target is None or not planned_xy_matches(
            finished.target_stage_xy,
            accepted_target,
            tolerance_mm=self._config.planned_start_tolerance_mm,
        ):
            return
        if not finished.success:
            self._clear_planned_prediction(clear_wait_state=True)
            self._emit_action_state()
            return
        completion_target = accepted_target
        if completion_target is not None:
            self._planned_stage_xy = completion_target
            self._presented_stage_xy = completion_target
            self._presented_position = _position_with_stage_xy(
                self._presented_position or self._last_observation.position,
                completion_target,
            )
        self._pending_planned_target_xy = None
        self._pending_planned_source_label = None
        self._planned_origin_xy = None
        self._planned_target_xy = None
        self._planned_started_at = None
        self._planned_ends_at = None
        self._planned_waiting_for_fresh_status = self._planned_stage_xy is not None
        self._planned_stop_status_timestamp = finished.status_timestamp
        self._planned_motion_token = None
        self._accepted_planned_target_xy = None
        self._prediction_timer.stop()
        self._active_axes = frozenset()
        self._emit_action_state()

    @Slot()
    def tick(self) -> None:
        self._require_object_thread()
        if (
            self._planned_origin_xy is None
            or self._planned_target_xy is None
            or self._planned_started_at is None
            or self._planned_ends_at is None
        ):
            self._prediction_timer.stop()
            return
        progress = motion_progress(
            self._planned_started_at,
            self._planned_ends_at,
            time.monotonic(),
        )
        origin_x, origin_y = self._planned_origin_xy
        target_x, target_y = self._planned_target_xy
        self._planned_stage_xy = (
            float(origin_x + (target_x - origin_x) * progress),
            float(origin_y + (target_y - origin_y) * progress),
        )
        self._presented_stage_xy = self._planned_stage_xy
        self._presented_position = _position_with_stage_xy(
            self._presented_position or self._last_observation.position,
            self._planned_stage_xy,
        )
        self._emit_presentation(
            reported_position=self._presented_position,
            raw_stage_xy=self._presented_stage_xy,
            contact_calibration_position=None,
            unhomed_fallback=False,
            clear_motion_axes=False,
        )
        if progress >= 1.0:
            self._planned_waiting_for_fresh_status = True
            self._planned_stop_status_timestamp = None
            self._planned_origin_xy = None
            self._planned_target_xy = None
            self._planned_started_at = None
            self._planned_ends_at = None
            self._prediction_timer.stop()

    def _start_planned_prediction(
        self,
        target: tuple[float, float],
        *,
        origin_position: tuple[float, ...],
        source_label: str,
        feedrate_mm_min: float,
    ) -> None:
        _ = source_label
        origin_position = _coerce_position_tuple(origin_position)
        origin = _stage_xy(origin_position)
        if origin_position is None or origin is None:
            self._clear_planned_prediction(clear_wait_state=True)
            self._emit_action_state()
            return
        timing = planned_xy_timing(
            origin,
            target,
            feedrate_mm_min,
            min_feedrate_mm_min=self._config.min_feedrate_mm_min,
            duration_padding_s=self._config.planned_move_duration_padding_s,
            started_at=time.monotonic(),
        )
        if timing is None:
            self._clear_planned_prediction(clear_wait_state=True)
            self._emit_action_state()
            return
        started_at, ends_at = timing
        self._manual_jog_prediction.clear_waiting_status()
        self._planned_origin_xy = origin
        self._planned_stage_xy = origin
        self._planned_target_xy = target
        self._planned_started_at = started_at
        self._planned_ends_at = ends_at
        self._planned_waiting_for_fresh_status = False
        self._planned_stop_status_timestamp = None
        self._presented_stage_xy = origin
        self._presented_position = _position_with_stage_xy(
            origin_position,
            origin,
        )
        self._active_axes = frozenset({"X", "Y"})
        self._emit_presentation(
            reported_position=self._presented_position,
            raw_stage_xy=origin,
            contact_calibration_position=None,
            unhomed_fallback=False,
            clear_motion_axes=False,
        )
        self._emit_action_state()
        if not self._prediction_timer.isActive():
            self._prediction_timer.start()

    def _resolved_reported_stage_xy(
        self,
        raw_stage_xy: tuple[float, float],
        *,
        status_timestamp: float | None,
    ) -> tuple[float, float]:
        held_stage_xy = held_planned_xy(
            self._planned_stage_xy,
            prediction_active=self._planned_started_at is not None,
            waiting_for_fresh_status=self._planned_waiting_for_fresh_status,
            status_timestamp=status_timestamp,
            stop_status_timestamp=self._planned_stop_status_timestamp,
        )
        if held_stage_xy is not None:
            return held_stage_xy
        self._planned_waiting_for_fresh_status = False
        self._planned_stop_status_timestamp = None
        self._planned_stage_xy = raw_stage_xy
        return raw_stage_xy

    def _clear_planned_prediction(self, *, clear_wait_state: bool) -> None:
        self._pending_planned_target_xy = None
        self._pending_planned_source_label = None
        self._planned_origin_xy = None
        self._planned_target_xy = None
        self._planned_started_at = None
        self._planned_ends_at = None
        self._planned_motion_token = None
        self._accepted_planned_target_xy = None
        self._prediction_timer.stop()
        self._active_axes = frozenset()
        if clear_wait_state:
            self._planned_stage_xy = None
            self._planned_waiting_for_fresh_status = False
            self._planned_stop_status_timestamp = None

    def _owns_planned_motion(self, motion_token: object) -> bool:
        return bool(
            self._planned_motion_token is not None
            and motion_token == self._planned_motion_token
        )

    def _presentation(
        self,
        *,
        reported_position: object | None,
        raw_stage_xy: tuple[float, float] | None,
        contact_calibration_position: tuple[float, float, float] | None,
        unhomed_fallback: bool,
        clear_motion_axes: bool,
        material_change: bool = True,
    ) -> StageMotionPresentation:
        return StageMotionPresentation(
            reported_position=reported_position,
            presented_position=self._presented_position,
            raw_stage_xy=raw_stage_xy,
            presented_stage_xy=(None if unhomed_fallback else self._presented_stage_xy),
            physical_machine_pose=self._physical_machine_pose,
            contact_calibration_position=contact_calibration_position,
            b_position=self._last_reported_b_position,
            active_axes=self._active_axes,
            unhomed_fallback=unhomed_fallback,
            clear_motion_axes=clear_motion_axes,
            motion_coordinate_snapshot=(
                self._last_observation.motion_coordinate_snapshot
            ),
            stage_state=self._last_observation.stage_state,
            homed_axes=self._last_observation.homed_axes,
            status_timestamp=self._last_observation.status_timestamp,
            last_jog_write_timestamp=(self._last_observation.last_jog_write_timestamp),
            material_change=material_change,
        )

    def _emit_presentation(
        self,
        *,
        reported_position: object | None,
        raw_stage_xy: tuple[float, float] | None,
        contact_calibration_position: tuple[float, float, float] | None,
        unhomed_fallback: bool,
        clear_motion_axes: bool,
        material_change: bool = True,
    ) -> None:
        self._last_presentation = self._presentation(
            reported_position=reported_position,
            raw_stage_xy=raw_stage_xy,
            contact_calibration_position=contact_calibration_position,
            unhomed_fallback=unhomed_fallback,
            clear_motion_axes=clear_motion_axes,
            material_change=material_change,
        )
        self.presentation_changed.emit(self._last_presentation)

    def _emit_action_state(self) -> None:
        pending = self._pending_planned_target_xy is not None
        active = self._planned_started_at is not None
        coordinate_active = self._coordinate_targets.has_active_move()
        self.action_state_changed.emit(
            StageMotionActionState(
                active_axes=self._active_axes,
                cancelable=bool(pending or active or coordinate_active),
                coordinate_active=coordinate_active,
                planned_pending=pending,
                planned_active=active,
            )
        )

    def _require_object_thread(self) -> None:
        if QThread.currentThread() != self.thread():
            raise RuntimeError("Stage motion session mutation requires its Qt thread.")


def _coerce_position_tuple(position: object | None) -> tuple[float, ...] | None:
    if not isinstance(position, (tuple, list)):
        return None
    try:
        values = tuple(float(value) for value in position)
    except (TypeError, ValueError, OverflowError):
        return None
    return values if all(math.isfinite(value) for value in values) else None


def _empty_position_observation() -> stage_types.StagePositionObservation:
    return stage_types.StagePositionObservation(
        position=None,
        physical_machine_pose=PhysicalMachinePose.from_mapping({}),
        motion_coordinate_snapshot=None,
        stage_state=None,
        homed_axes=frozenset(),
        status_timestamp=None,
        last_jog_write_timestamp=None,
    )


def _stage_xy(position: object | None) -> tuple[float, float] | None:
    values = _coerce_position_tuple(position)
    if values is None or len(values) < 2:
        return None
    return values[0], values[1]


def _position_with_stage_xy(
    position: object | None,
    stage_xy: tuple[float, float],
) -> tuple[float, ...]:
    values = list(_coerce_position_tuple(position) or ())
    if len(values) < 2:
        values = [stage_xy[0], stage_xy[1]]
    else:
        values[0], values[1] = stage_xy
    return tuple(values)


def _axis_value(
    position: tuple[float, ...],
    axis_names: tuple[str, ...],
    axis: str,
) -> float | None:
    try:
        index = axis_names.index(axis)
    except ValueError:
        return None
    return position[index] if index < len(position) else None
