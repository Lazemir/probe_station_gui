"""Immutable application contracts for the Stage motion session."""

from __future__ import annotations

from dataclasses import dataclass

from probe_station_gui.coordinates.model import PhysicalMachinePose
from probe_station_gui.stage.coordinate_targets import (
    CoordinateTargetCommonFeedratePlan,
    CoordinateTargetConfig,
)
from probe_station_gui.stage.machine_coordinates import MachineCoordinateSnapshot
from probe_station_gui.stage.manual_jog_prediction import ManualJogPredictionConfig


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
    settle_status_poll_delays_ms: tuple[int, ...] = ()
    exact_step_accumulation_ms: int = 80
    terminal_resume_after_jog_ms: int = 180


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
    coordinate_display_targets: tuple[tuple[str, float], ...]
    coordinate_stage_position: tuple[float, ...] | None
    coordinate_programmed_feedrate: float | None
    coordinate_effective_feedrate: float | None
    coordinate_common_feedrate: CoordinateTargetCommonFeedratePlan
    pending_edit_axes: frozenset[str]
    manual_prediction_active: bool
    manual_prediction_available: bool
    planned_pending_target_xy: tuple[float, float] | None
    planned_pending_source_label: str | None
    planned_stage_xy: tuple[float, float] | None
    planned_prediction_active: bool
    planned_waiting_for_fresh_status: bool
    last_reported_b_position: float | None
    exact_step_display_targets: tuple[tuple[str, float], ...]
    exact_step_motion_lease: object | None
    exact_step_pose_rebase_allowed: bool


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
