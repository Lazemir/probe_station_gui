"""Pure objective and alignment decision planning."""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from probe_station_gui.design.objective_offsets import (
    ObjectiveOffsetReference,
    base_objective_name,
    calibrated_objective_offset,
    objective_xy_offset,
    objective_xy_offset_is_configured,
)
from probe_station_gui.design.session import AlignmentPreparation
from probe_station_gui.settings.manager import Settings
from probe_station_gui.settings.objective_config import (
    default_objective,
    normalize_objective_name,
    ordered_objective_names,
)


Point2D = tuple[float, float]
StagePosition = Sequence[float] | None

NEGLIGIBLE_ROTATION_DEG = 1e-3
MIN_ALIGNMENT_DISTANCE_MM = 1e-6
ALIGNMENT_TARGET_ANGLES = (0.0, 90.0, 180.0, -90.0)


def active_objective_configuration(objective_settings: object) -> tuple[object, object]:
    objectives = getattr(objective_settings, "objectives", {})
    active_name = normalize_objective_name(getattr(objective_settings, "active_name", ""))
    active_objective = objectives.get(active_name) if hasattr(objectives, "get") else None
    if active_objective is None:
        active_objective = default_objective(active_name)
    return active_objective, objectives


@dataclass(frozen=True)
class ObjectiveComboSyncPlan:
    names: list[str]
    selected_index: int
    rebuild_items: bool
    refresh_calibration_ui: bool


@dataclass(frozen=True)
class ObjectiveSelectionPlan:
    settings: Settings | None = None
    old_name: str = ""
    new_name: str = ""
    restore_combo_name: str | None = None
    apply_settings: bool = False
    apply_offset_motion: bool = False
    refresh_calibration_ui: bool = False
    status: str | None = None
    status_timeout_ms: int = 3000


@dataclass(frozen=True)
class ObjectiveProfilePlan:
    settings: Settings | None = None
    name: str = ""
    select_existing_name: str | None = None
    apply_settings: bool = False
    refresh_calibration_ui: bool = False
    status: str | None = None
    status_timeout_ms: int = 3000


@dataclass(frozen=True)
class ObjectiveChangeOffsetPlan:
    raw_targets: dict[str, float] | None = None
    status: str | None = None
    status_timeout_ms: int = 4000
    accepted_status: str | None = None
    rejected_status: str | None = None


@dataclass(frozen=True)
class ObjectiveOffsetReferencePlan:
    settings: Settings | None = None
    reference: ObjectiveOffsetReference | None = None
    active_name: str = ""
    apply_settings: bool = False
    refresh_calibration_ui: bool = False
    refresh_design_position: bool = False
    status: str | None = None
    status_timeout_ms: int = 8000


@dataclass(frozen=True)
class AlignmentCapturePositionPlan:
    stage_xy: Point2D | None = None
    request_status_refresh: bool = False
    status: str | None = None
    status_timeout_ms: int = 5000


@dataclass(frozen=True)
class AlignmentCapturePlan:
    points: list[Point2D | None] | None = None
    status: str | None = None
    status_timeout_ms: int = 5000
    expand_alignment: bool = False
    refresh_manual_ui: bool = False
    refresh_design_panel: bool = False
    refresh_design_position: bool = False
    collapse_alignment_if_ready: bool = False
    collapse_alignment_if_design_open: bool = False
    invalidate_design_registration: bool = False
    request_b_rotation: bool = False
    pending_quick_alignment_rotation: bool = False
    rotation_deg: float | None = None
    preparation: AlignmentPreparation | None = None
    pending_preparation: AlignmentPreparation | None = None
    apply_prepared_alignment: bool = False
    disable_snap: bool = False


@dataclass(frozen=True)
class AlignmentPresentation:
    captured_points: list[Point2D | None]
    pick_slot: int | None
    instruction: str
    alignment_mode: bool


def objective_combo_sync_plan(
    current_item_data: Sequence[str],
    objective_names: Sequence[str],
    objective_name: str,
) -> ObjectiveComboSyncPlan:
    names = [str(name) for name in objective_names]
    selected = normalize_objective_name(objective_name)
    try:
        selected_index = names.index(selected)
    except ValueError:
        selected_index = -1
    return ObjectiveComboSyncPlan(
        names=names,
        selected_index=selected_index,
        rebuild_items=list(current_item_data) != names,
        refresh_calibration_ui=selected_index >= 0,
    )


def select_active_objective(
    settings: Settings,
    objective_name: str,
    *,
    is_busy: bool,
    apply_motion: bool,
) -> ObjectiveSelectionPlan:
    name = normalize_objective_name(objective_name)
    if not name:
        return ObjectiveSelectionPlan()
    cloned = settings.clone()
    old_name = cloned.objectives.active_name
    if old_name == name:
        return ObjectiveSelectionPlan(refresh_calibration_ui=True)
    if is_busy:
        return ObjectiveSelectionPlan(
            old_name=old_name,
            new_name=name,
            restore_combo_name=old_name,
            status="Stage is busy; objective not changed.",
            status_timeout_ms=4000,
        )
    if name not in cloned.objectives.objectives:
        cloned.objectives.objectives[name] = default_objective(name)
    cloned.objectives.active_name = name
    return ObjectiveSelectionPlan(
        settings=cloned,
        old_name=old_name,
        new_name=name,
        apply_settings=True,
        apply_offset_motion=apply_motion,
        status=f"Objective selected: {name}.",
        status_timeout_ms=3000,
    )


def objective_change_offset_plan(
    objective_settings: Any,
    old_name: str,
    new_name: str,
    latest_position: StagePosition,
    is_busy: bool,
    *,
    display_axis_value_from_raw: Callable[[str, float], float] | None = None,
    raw_axis_value_from_display: Callable[[str, float], float] | None = None,
) -> ObjectiveChangeOffsetPlan:
    if not bool(getattr(objective_settings, "apply_offsets_on_change", False)):
        return ObjectiveChangeOffsetPlan()
    profiles = getattr(objective_settings, "objectives", {})
    old_profile = profiles.get(old_name)
    new_profile = profiles.get(new_name)
    if old_profile is None or new_profile is None:
        return ObjectiveChangeOffsetPlan()
    if not (
        objective_xy_offset_is_configured(profiles, old_name)
        and objective_xy_offset_is_configured(profiles, new_name)
    ):
        return ObjectiveChangeOffsetPlan(
            status="Objective XY offset is not configured for both objectives.",
        )
    if latest_position is None or len(latest_position) < 2:
        return ObjectiveChangeOffsetPlan(
            status="Stage position unavailable; objective offset not applied.",
        )
    old_offset = objective_xy_offset(profiles, old_name)
    new_offset = objective_xy_offset(profiles, new_name)
    raw_targets = {
        "X": float(latest_position[0]) + float(new_offset[0]) - float(old_offset[0]),
        "Y": float(latest_position[1]) + float(new_offset[1]) - float(old_offset[1]),
    }
    if (
        bool(getattr(old_profile, "z_offset_configured", False))
        and bool(getattr(new_profile, "z_offset_configured", False))
        and len(latest_position) >= 3
    ):
        display_from_raw = display_axis_value_from_raw or (lambda _axis, value: value)
        raw_from_display = raw_axis_value_from_display or (lambda _axis, value: value)
        current_z_display = display_from_raw("Z", float(latest_position[2]))
        z_display = (
            current_z_display
            + float(getattr(new_profile, "z_offset_mm", 0.0))
            - float(getattr(old_profile, "z_offset_mm", 0.0))
        )
        raw_targets["Z"] = raw_from_display("Z", z_display)
    if is_busy:
        return ObjectiveChangeOffsetPlan(
            status="Stage is busy; objective offset not applied.",
        )
    axes = ", ".join(sorted(raw_targets))
    return ObjectiveChangeOffsetPlan(
        raw_targets=raw_targets,
        accepted_status=f"Applying {new_name} objective offset on {axes}.",
        rejected_status="Objective offset move was not accepted.",
    )


def profile_add_plan(settings: Settings, raw_name: str) -> ObjectiveProfilePlan:
    name = normalize_objective_name(raw_name)
    if not name:
        return ObjectiveProfilePlan(
            status="Objective name must use letters, digits, dot, dash, or underscore.",
            status_timeout_ms=5000,
        )
    cloned = settings.clone()
    if name in cloned.objectives.objectives:
        return ObjectiveProfilePlan(
            name=name,
            select_existing_name=name,
            status=f"Objective already exists: {name}.",
        )
    cloned.objectives.objectives[name] = default_objective(name)
    cloned.objectives.active_name = name
    return ObjectiveProfilePlan(
        settings=cloned,
        name=name,
        apply_settings=True,
        status=f"Objective added: {name}.",
    )


def profile_delete_plan(
    settings: Settings,
    objective_name: str,
    *,
    confirmed: bool,
) -> ObjectiveProfilePlan:
    name = normalize_objective_name(objective_name)
    if not name:
        return ObjectiveProfilePlan()
    cloned = settings.clone()
    profiles = cloned.objectives.objectives
    if name not in profiles:
        return ObjectiveProfilePlan(
            status=f"Objective does not exist: {name}.",
            status_timeout_ms=4000,
        )
    if len(profiles) <= 1:
        return ObjectiveProfilePlan(
            status="At least one objective profile is required.",
            status_timeout_ms=4000,
        )
    if not confirmed:
        return ObjectiveProfilePlan(name=name)
    del profiles[name]
    if cloned.objectives.active_name == name:
        cloned.objectives.active_name = ordered_objective_names(profiles)[0]
    return ObjectiveProfilePlan(
        settings=cloned,
        name=name,
        apply_settings=True,
        status=f"Objective deleted: {name}.",
    )


def objective_offset_reference_plan(
    settings: Settings,
    raw_stage_xy: Point2D,
) -> ObjectiveOffsetReferencePlan:
    cloned = settings.clone()
    active_name = normalize_objective_name(cloned.objectives.active_name)
    if not active_name:
        return ObjectiveOffsetReferencePlan()
    profiles = cloned.objectives.objectives
    if active_name not in profiles:
        profiles[active_name] = default_objective(active_name)
    base_name = base_objective_name(profiles)
    apply_settings = False
    if active_name == base_name:
        profile = profiles[active_name]
        profile.xy_offset_x_mm = 0.0
        profile.xy_offset_y_mm = 0.0
        profile.xy_offset_configured = True
        profiles[active_name] = profile
        apply_settings = True
    elif not objective_xy_offset_is_configured(profiles, active_name):
        return ObjectiveOffsetReferencePlan(
            active_name=active_name,
            status="Set the objective offset reference with the base objective first.",
            status_timeout_ms=6000,
        )
    reference = ObjectiveOffsetReference(
        objective_name=active_name,
        stage_xy=(float(raw_stage_xy[0]), float(raw_stage_xy[1])),
        offset_xy=objective_xy_offset(profiles, active_name),
    )
    return ObjectiveOffsetReferencePlan(
        settings=cloned if apply_settings else None,
        reference=reference,
        active_name=active_name,
        apply_settings=apply_settings,
        status=(
            f"Objective offset reference set with {active_name}. "
            "Center the same feature under another objective and press Save Offset."
        ),
        status_timeout_ms=8000,
    )


def save_active_objective_offset(
    settings: Settings,
    reference: ObjectiveOffsetReference | None,
    raw_stage_xy: Point2D,
) -> ObjectiveOffsetReferencePlan:
    if reference is None:
        return ObjectiveOffsetReferencePlan(
            status="Set an objective offset reference first.",
            status_timeout_ms=5000,
        )
    cloned = settings.clone()
    active_name = normalize_objective_name(cloned.objectives.active_name)
    if not active_name:
        return ObjectiveOffsetReferencePlan()
    profiles = cloned.objectives.objectives
    if active_name not in profiles:
        profiles[active_name] = default_objective(active_name)
    if active_name == base_objective_name(profiles):
        offset_xy = (0.0, 0.0)
    else:
        offset_xy = calibrated_objective_offset(reference, raw_stage_xy)
    profile = profiles[active_name]
    profile.xy_offset_x_mm = float(offset_xy[0])
    profile.xy_offset_y_mm = float(offset_xy[1])
    profile.xy_offset_configured = True
    profiles[active_name] = profile
    return ObjectiveOffsetReferencePlan(
        settings=cloned,
        active_name=active_name,
        apply_settings=True,
        refresh_design_position=True,
        status=(
            f"Saved {active_name} objective offset: "
            f"X={offset_xy[0]:+.4f}, Y={offset_xy[1]:+.4f} mm."
        ),
        status_timeout_ms=6000,
    )


def reset_active_objective_offset(settings: Settings) -> ObjectiveOffsetReferencePlan:
    cloned = settings.clone()
    active_name = normalize_objective_name(cloned.objectives.active_name)
    if not active_name:
        return ObjectiveOffsetReferencePlan()
    profiles = cloned.objectives.objectives
    if active_name not in profiles:
        profiles[active_name] = default_objective(active_name)
    profile = profiles[active_name]
    profile.xy_offset_x_mm = 0.0
    profile.xy_offset_y_mm = 0.0
    profile.xy_offset_configured = active_name == base_objective_name(profiles)
    profiles[active_name] = profile
    return ObjectiveOffsetReferencePlan(
        settings=cloned,
        active_name=active_name,
        apply_settings=True,
        refresh_calibration_ui=True,
        refresh_design_position=True,
        status=f"Reset {active_name} objective offset.",
        status_timeout_ms=4000,
    )


def update_objective_calibration(
    settings: Settings,
    objective_name: str,
    pixels_to_mm: object,
) -> ObjectiveProfilePlan:
    name = normalize_objective_name(objective_name)
    if not name:
        return ObjectiveProfilePlan()
    cloned = settings.clone()
    profile = cloned.objectives.objectives.get(name)
    if profile is None:
        profile = default_objective(name)
    matrix = _legacy_pixels_to_mm_matrix(pixels_to_mm)
    profile.pixels_to_mm = matrix
    profile.xy_calibration_configured = bool(matrix)
    cloned.objectives.objectives[name] = profile
    return ObjectiveProfilePlan(
        settings=cloned,
        name=name,
        refresh_calibration_ui=True,
    )


def alignment_capture_position_plan(
    *,
    stage_position: StagePosition = None,
    error_message: str | None = None,
    latest_position: StagePosition = None,
) -> AlignmentCapturePositionPlan:
    if error_message:
        if error_message != "Unable to read stage position.":
            return AlignmentCapturePositionPlan(status=error_message)
        if latest_position is None or len(latest_position) < 2:
            return AlignmentCapturePositionPlan(
                request_status_refresh=True,
                status=error_message,
            )
        stage_position = latest_position
    if stage_position is None:
        return AlignmentCapturePositionPlan(
            request_status_refresh=True,
            status="X/Y coordinates are unavailable.",
        )
    if len(stage_position) < 2:
        return AlignmentCapturePositionPlan(
            request_status_refresh=True,
            status="X/Y coordinates are unavailable.",
        )
    return AlignmentCapturePositionPlan(
        stage_xy=(float(stage_position[0]), float(stage_position[1])),
    )


def quick_alignment_rotation(
    first_position: Point2D,
    second_position: Point2D,
    *,
    target_angles: Sequence[float] = ALIGNMENT_TARGET_ANGLES,
) -> float | None:
    dx = float(second_position[0]) - float(first_position[0])
    dy = float(second_position[1]) - float(first_position[1])
    if math.hypot(dx, dy) <= MIN_ALIGNMENT_DISTANCE_MM:
        return None
    angle_deg = math.degrees(math.atan2(dy, dx))
    best_delta = min(
        (_normalise_angle(float(target) - angle_deg) for target in target_angles),
        key=lambda value: abs(value),
    )
    if abs(best_delta) > 45.0:
        return None
    return best_delta


def manual_alignment_capture_plan(
    slot: int,
    captured: Point2D,
    *,
    source: str,
    manual_points: Sequence[Point2D | None],
    target_angles: Sequence[float] = ALIGNMENT_TARGET_ANGLES,
) -> AlignmentCapturePlan:
    if slot not in (0, 1):
        return AlignmentCapturePlan()
    points = list(manual_points[:2])
    while len(points) < 2:
        points.append(None)
    captured_xy = (float(captured[0]), float(captured[1]))
    points[slot] = captured_xy
    label = _alignment_source_label(source)
    other_slot = 1 - slot
    if points[other_slot] is None:
        return AlignmentCapturePlan(
            points=points,
            expand_alignment=True,
            refresh_manual_ui=True,
            status=(
                f"Chip alignment: point {slot + 1} captured from {label} "
                f"at X={captured_xy[0]:.3f}, Y={captured_xy[1]:.3f}. "
                f"Capture point {other_slot + 1} next."
            ),
            status_timeout_ms=6000,
        )
    first_point = points[0]
    second_point = points[1]
    if first_point is None or second_point is None:
        return AlignmentCapturePlan(points=points, refresh_manual_ui=True)
    rotation_deg = quick_alignment_rotation(
        first_point,
        second_point,
        target_angles=target_angles,
    )
    if rotation_deg is None:
        return AlignmentCapturePlan(
            points=points,
            expand_alignment=True,
            refresh_manual_ui=True,
            status="Chip alignment points are too close together. Capture two distinct points.",
            status_timeout_ms=5000,
        )
    if abs(rotation_deg) < NEGLIGIBLE_ROTATION_DEG:
        return AlignmentCapturePlan(
            points=points,
            expand_alignment=True,
            refresh_manual_ui=True,
            collapse_alignment_if_design_open=True,
            rotation_deg=0.0,
            status="Chip alignment points are already aligned.",
            status_timeout_ms=5000,
        )
    return AlignmentCapturePlan(
        points=points,
        expand_alignment=True,
        refresh_manual_ui=True,
        invalidate_design_registration=True,
        request_b_rotation=True,
        pending_quick_alignment_rotation=True,
        rotation_deg=rotation_deg,
        status=f"Chip alignment: rotating B by {rotation_deg:+.3f} deg.",
        status_timeout_ms=5000,
    )


def design_alignment_capture_plan(
    *,
    slot: int,
    stage_xy: Point2D,
    source: str,
    pair_count: int,
    required_pair_count: int = 2,
    preparation: AlignmentPreparation | None,
    preparation_error: str | None,
    spacing_reasonable: bool,
) -> AlignmentCapturePlan:
    label = _alignment_source_label(source)
    required = max(2, int(required_pair_count))
    if pair_count < required:
        remaining = required - pair_count
        point_label = "point" if remaining == 1 else "points"
        return AlignmentCapturePlan(
            expand_alignment=True,
            status=(
                f"Design alignment: point {slot + 1} captured from {label} "
                f"at X={stage_xy[0]:.3f}, Y={stage_xy[1]:.3f}. "
                f"Capture {remaining} remaining {point_label}."
            ),
            status_timeout_ms=6000,
        )
    if preparation_error:
        return AlignmentCapturePlan(status=preparation_error, status_timeout_ms=7000)
    if preparation is None:
        return AlignmentCapturePlan()
    if not spacing_reasonable:
        return AlignmentCapturePlan(
            preparation=preparation,
            status=(
                "Design calibration aborted: mark spacing mismatch. "
                f"Design {preparation.design_distance_mm:.4f} mm vs chip "
                f"{preparation.stage_distance_mm:.4f} mm."
            ),
            status_timeout_ms=8000,
        )
    if abs(preparation.rotation_deg) < NEGLIGIBLE_ROTATION_DEG:
        return AlignmentCapturePlan(
            preparation=preparation,
            apply_prepared_alignment=True,
            disable_snap=True,
            refresh_design_panel=True,
            refresh_design_position=True,
            collapse_alignment_if_ready=True,
            status=(
                "Design calibration complete. "
                f"Spacing ratio {preparation.distance_ratio:.3f}. "
                f"RMS {preparation.rms_residual_mm:.4f} mm, "
                f"max {preparation.max_residual_mm:.4f} mm."
            ),
            status_timeout_ms=7000,
        )
    return AlignmentCapturePlan(
        preparation=preparation,
        pending_preparation=preparation,
        expand_alignment=True,
        request_b_rotation=True,
        rotation_deg=preparation.rotation_deg,
        status=(
            f"{required} mark pairs captured. "
            f"RMS {preparation.rms_residual_mm:.4f} mm, "
            f"max {preparation.max_residual_mm:.4f} mm. "
            f"Rotating chip by {preparation.rotation_deg:+.3f} deg to match the design."
        ),
        status_timeout_ms=7000,
    )


def alignment_presentation(
    *,
    design_backed: bool,
    design_stage_marks: Sequence[Point2D | None],
    manual_points: Sequence[Point2D | None],
    pick_slot: int | None,
    required_design_mark_count: int = 2,
) -> AlignmentPresentation:
    required = max(2, int(required_design_mark_count)) if design_backed else 2
    points = list((design_stage_marks if design_backed else manual_points)[:required])
    while len(points) < required:
        points.append(None)
    if pick_slot is None:
        instruction = ""
    else:
        instruction = (
            f"Chip alignment: click point {pick_slot + 1} "
            "or press Space for the center."
        )
    return AlignmentPresentation(
        captured_points=points,
        pick_slot=pick_slot,
        instruction=instruction,
        alignment_mode=pick_slot is not None,
    )


def _alignment_source_label(source: str) -> str:
    return "image" if source == "image" else "center"


def _legacy_pixels_to_mm_matrix(raw_matrix: object) -> list[list[float]]:
    if not isinstance(raw_matrix, (list, tuple)):
        return []
    try:
        return [
            [float(raw_matrix[0][0]), float(raw_matrix[0][1])],
            [float(raw_matrix[1][0]), float(raw_matrix[1][1])],
        ]
    except (TypeError, ValueError, IndexError):
        return []


def _normalise_angle(angle_deg: float) -> float:
    return ((float(angle_deg) + 180.0) % 360.0) - 180.0


__all__ = [
    "AlignmentCapturePlan",
    "AlignmentCapturePositionPlan",
    "AlignmentPresentation",
    "ObjectiveChangeOffsetPlan",
    "ObjectiveComboSyncPlan",
    "ObjectiveOffsetReferencePlan",
    "ObjectiveProfilePlan",
    "ObjectiveSelectionPlan",
    "active_objective_configuration",
    "alignment_capture_position_plan",
    "alignment_presentation",
    "design_alignment_capture_plan",
    "manual_alignment_capture_plan",
    "objective_change_offset_plan",
    "objective_combo_sync_plan",
    "objective_offset_reference_plan",
    "profile_add_plan",
    "profile_delete_plan",
    "quick_alignment_rotation",
    "reset_active_objective_offset",
    "save_active_objective_offset",
    "select_active_objective",
    "update_objective_calibration",
]
