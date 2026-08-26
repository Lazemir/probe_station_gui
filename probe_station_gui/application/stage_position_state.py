from __future__ import annotations

import time
from dataclasses import dataclass, replace

from probe_station_gui.application import stage_motion_types as motion_types
from probe_station_gui.coordinates.model import PhysicalMachinePose
from probe_station_gui.stage.coordinate_targets import CoordinateTargetMoveState
from probe_station_gui.stage.manual_jog_prediction import ManualJogPredictionState
from probe_station_gui.stage.motion_prediction import (
    axis_value,
    coerce_finite_position,
    coerce_finite_xy,
    position_with_stage_xy,
)
from probe_station_gui.stage.planned_xy import PlannedXYMotionState
from probe_station_gui.stage.types import StagePositionObservation


@dataclass(frozen=True)
class StagePositionObservationDecision:
    presentation: motion_types.StageMotionPresentation
    current_position: tuple[float, ...] | None
    latest_state: str
    coordinate_idle_candidate: bool
    deferred: bool


@dataclass
class StagePositionState:
    axis_names: tuple[str, ...]
    presented_position: tuple[float, ...] | None = None
    presented_stage_xy: tuple[float, float] | None = None
    physical_machine_pose: PhysicalMachinePose = PhysicalMachinePose.from_mapping({})
    active_axes: frozenset[str] = frozenset()
    last_reported_b_position: float | None = None
    last_observation: StagePositionObservation = StagePositionObservation.empty()
    last_presentation: motion_types.StageMotionPresentation | None = None

    def reset(self) -> motion_types.StageMotionPresentation:
        self.presented_position = None
        self.presented_stage_xy = None
        self.physical_machine_pose = PhysicalMachinePose.from_mapping({})
        self.active_axes = frozenset()
        self.last_reported_b_position = None
        self.last_observation = StagePositionObservation.empty()
        return self.record_presentation(
            reported_position=None,
            raw_stage_xy=None,
            contact_calibration_position=None,
            unhomed_fallback=False,
            clear_motion_axes=True,
        )

    def refresh_pose(
        self,
        physical_machine_pose: PhysicalMachinePose,
    ) -> motion_types.StageMotionPresentation:
        self.physical_machine_pose = physical_machine_pose
        if self.last_presentation is None:
            return self.record_presentation(
                reported_position=self.presented_position,
                raw_stage_xy=self.presented_stage_xy,
                contact_calibration_position=None,
                unhomed_fallback=False,
                clear_motion_axes=False,
            )
        self.last_presentation = replace(
            self.last_presentation,
            physical_machine_pose=physical_machine_pose,
        )
        return self.last_presentation

    def observe(
        self,
        observation: StagePositionObservation,
        *,
        coordinate_targets: CoordinateTargetMoveState,
        manual_prediction: ManualJogPredictionState,
        planned_xy: PlannedXYMotionState,
        monotonic_s: float | None = None,
    ) -> StagePositionObservationDecision:
        previous_observation = self.last_observation
        self.last_observation = observation
        material_change = not previous_observation.has_same_motion_facts_as(observation)
        position = observation.position
        current_position = coerce_finite_position(position)
        if current_position is None or len(current_position) < 2:
            presentation_was_available = self.presented_position is not None
            self.physical_machine_pose = PhysicalMachinePose.from_mapping({})
            self.last_observation = replace(
                observation,
                physical_machine_pose=self.physical_machine_pose,
            )
            self.presented_position = None
            self.presented_stage_xy = None
            presentation = self.record_presentation(
                reported_position=position,
                raw_stage_xy=None,
                contact_calibration_position=None,
                unhomed_fallback=False,
                clear_motion_axes=False,
                material_change=material_change or presentation_was_available,
            )
            return StagePositionObservationDecision(
                presentation, None, "", False, False
            )

        self.physical_machine_pose = observation.physical_machine_pose
        raw_stage_xy = (current_position[0], current_position[1])
        latest_state = str(observation.stage_state or "").lower()
        coordinate_active = coordinate_targets.has_active_move()
        if coordinate_active and latest_state in {"run", "jog"}:
            coordinate_targets.seen_active_state = True
        b_position = axis_value(current_position, self.axis_names, "B")
        if b_position is not None:
            self.last_reported_b_position = b_position
        xy_homed = {"X", "Y"}.issubset(observation.homed_axes)
        xyz_homed = {"X", "Y", "Z"}.issubset(observation.homed_axes)
        now = time.monotonic() if monotonic_s is None else float(monotonic_s)
        manual_available = manual_prediction.prediction_available(now)
        unhomed_fallback = not xy_homed and not manual_available
        was_waiting = planned_xy.waiting_for_fresh_status
        if unhomed_fallback:
            planned_xy.stage_xy = None
            planned_xy.waiting_for_fresh_status = False
            planned_xy.stop_status_timestamp = None
            self.presented_stage_xy = raw_stage_xy
        coordinate_position = coordinate_targets.stage_position
        if coordinate_active and coordinate_position is not None:
            self.presented_position = coordinate_position
            coordinate_xy = coerce_finite_xy(coordinate_position)
            if coordinate_xy is not None:
                self.presented_stage_xy = coordinate_xy
        elif manual_available:
            manual_decision = manual_prediction.reconcile_observation(
                current_position,
                raw_stage_xy=raw_stage_xy,
                latest_state=latest_state,
                last_jog_write_timestamp=observation.last_jog_write_timestamp,
                now=now,
                presented_position=self.presented_position,
            )
            if manual_decision.deferred:
                presentation = self.record_presentation(
                    reported_position=self.presented_position,
                    raw_stage_xy=self.presented_stage_xy,
                    contact_calibration_position=self._contact_position(
                        current_position, xyz_homed
                    ),
                    unhomed_fallback=False,
                    clear_motion_axes=False,
                    material_change=material_change,
                )
                return StagePositionObservationDecision(
                    presentation, current_position, latest_state, False, True
                )
            if manual_decision.display_position is not None:
                self.presented_position = manual_decision.display_position
                manual_xy = coerce_finite_xy(manual_decision.display_position)
                if manual_xy is not None:
                    self.presented_stage_xy = manual_xy
        else:
            if not unhomed_fallback:
                self.presented_stage_xy = planned_xy.resolve_reported_stage_xy(
                    raw_stage_xy,
                    status_timestamp=observation.status_timestamp,
                )
            self.presented_position = position_with_stage_xy(
                current_position,
                self.presented_stage_xy,
            )
        clear_motion_axes = latest_state == "idle"
        if clear_motion_axes:
            self.active_axes = frozenset()
        presentation = self.record_presentation(
            reported_position=current_position,
            raw_stage_xy=raw_stage_xy,
            contact_calibration_position=self._contact_position(
                current_position, xyz_homed
            ),
            unhomed_fallback=unhomed_fallback,
            clear_motion_axes=clear_motion_axes,
            material_change=material_change
            or (was_waiting and not planned_xy.waiting_for_fresh_status),
        )
        return StagePositionObservationDecision(
            presentation,
            current_position,
            latest_state,
            latest_state == "idle" and coordinate_targets.seen_active_state,
            False,
        )

    def record_presentation(
        self,
        *,
        reported_position: object | None,
        raw_stage_xy: tuple[float, float] | None,
        contact_calibration_position: tuple[float, float, float] | None,
        unhomed_fallback: bool,
        clear_motion_axes: bool,
        material_change: bool = True,
    ) -> motion_types.StageMotionPresentation:
        self.last_presentation = motion_types.StageMotionPresentation(
            reported_position=reported_position,
            presented_position=self.presented_position,
            raw_stage_xy=raw_stage_xy,
            presented_stage_xy=(None if unhomed_fallback else self.presented_stage_xy),
            physical_machine_pose=self.physical_machine_pose,
            contact_calibration_position=contact_calibration_position,
            b_position=self.last_reported_b_position,
            active_axes=self.active_axes,
            unhomed_fallback=unhomed_fallback,
            clear_motion_axes=clear_motion_axes,
            motion_coordinate_snapshot=self.last_observation.motion_coordinate_snapshot,
            stage_state=self.last_observation.stage_state,
            homed_axes=self.last_observation.homed_axes,
            status_timestamp=self.last_observation.status_timestamp,
            last_jog_write_timestamp=self.last_observation.last_jog_write_timestamp,
            material_change=material_change,
        )
        return self.last_presentation

    @staticmethod
    def _contact_position(
        current_position: tuple[float, ...],
        xyz_homed: bool,
    ) -> tuple[float, float, float] | None:
        if not xyz_homed or len(current_position) < 3:
            return None
        return (current_position[0], current_position[1], current_position[2])
