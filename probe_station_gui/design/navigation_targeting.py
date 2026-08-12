"""Design targeting, movement planning, and presentation projections."""

from __future__ import annotations

from dataclasses import dataclass

from probe_station_gui.coordinates.coordinator_model import RegistrationWorkflowSnapshot
from probe_station_gui.design.model import DesignDocument, MeasurementTarget, Point2D
from probe_station_gui.design.session import DesignSession
from probe_station_gui.design import session_navigation
from probe_station_gui.route.model import MeasurementRoute


@dataclass(frozen=True)
class DesignMovePlan:
    """Design move outcome for `Main` to execute against the stage."""

    accepted: bool
    stage_xy: Point2D | None = None
    design_xy: Point2D | None = None
    source_label: str = ""
    status_message: str | None = None
    status_timeout_ms: int = 5000
    last_selected_design_point: Point2D | None = None
    pending_planned_move_target_xy: Point2D | None = None


@dataclass(frozen=True)
class DesignTargetSelectionPlan:
    """Design target selection outcome."""

    selected: bool
    target: MeasurementTarget | None = None
    status_message: str | None = None
    status_timeout_ms: int = 3000


@dataclass(frozen=True)
class DesignPanelPresentation:
    """Widget-agnostic payload for design navigator and layout panels."""

    document: DesignDocument | None
    design_snap_enabled: bool
    registration_valid: bool
    targets: list[MeasurementTarget]
    selected_target_id: str | None
    route: MeasurementRoute | None
    selected_route_point_index: int
    route_measurement_running: bool
    calibration_prompt: str
    registration_status: str
    source_design_marks: tuple[Point2D, ...]
    check_design_marks: tuple[Point2D, ...]
    source_stage_marks: tuple[Point2D, ...]


@dataclass(frozen=True)
class DesignPositionPresentation:
    """Widget-agnostic payload for current design position and minimap."""

    document: DesignDocument | None
    targets: list[MeasurementTarget]
    selected_target_id: str | None
    probe_route: MeasurementRoute | None
    selected_route_point_index: int
    selected_design_point: Point2D | None
    stage_xy: Point2D | None
    current_design_position: Point2D | None
    fov_design_size: Point2D | None
    source_design_marks: tuple[Point2D, ...]
    check_design_marks: tuple[Point2D, ...]


def select_design_target(
    session: DesignSession,
    target_id: str,
) -> DesignTargetSelectionPlan:
    target = session_navigation.select_target_by_id(session, target_id)
    return DesignTargetSelectionPlan(target is not None, target)


def select_next_design_target(session: DesignSession) -> DesignTargetSelectionPlan:
    target = session_navigation.select_next_target(session)
    return DesignTargetSelectionPlan(
        target is not None,
        target,
        status_message=(
            f"Selected target '{target.label}'." if target is not None else None
        ),
    )


def select_previous_design_target(session: DesignSession) -> DesignTargetSelectionPlan:
    target = session_navigation.select_previous_target(session)
    return DesignTargetSelectionPlan(
        target is not None,
        target,
        status_message=(
            f"Selected target '{target.label}'." if target is not None else None
        ),
    )


def plan_design_target_move(
    session: DesignSession,
    target_id: str,
    stage_xy: Point2D | None,
) -> DesignMovePlan:
    target = session_navigation.select_target_by_id(session, target_id)
    if target is None:
        return DesignMovePlan(
            False,
            status_message=f"Unknown target '{target_id}'.",
        )
    if stage_xy is None:
        return DesignMovePlan(
            False,
            status_message="Design registration is required before moving to a target.",
            status_timeout_ms=6000,
        )
    return DesignMovePlan(
        True,
        stage_xy=(float(stage_xy[0]), float(stage_xy[1])),
        design_xy=target.design_center,
    )


def plan_design_coordinate_move(
    document_loaded: bool,
    stage_busy: bool,
    design_xy: Point2D,
    stage_xy: Point2D | None,
    source_label: str,
) -> DesignMovePlan:
    if not document_loaded:
        return DesignMovePlan(False)
    if stage_busy:
        return DesignMovePlan(
            False,
            status_message="Stage is busy. Ignoring design move request.",
            status_timeout_ms=3000,
        )
    if stage_xy is None:
        return DesignMovePlan(
            False,
            status_message="Design click-to-move requires completed registration.",
        )
    normalized_design = (float(design_xy[0]), float(design_xy[1]))
    normalized_stage = (float(stage_xy[0]), float(stage_xy[1]))
    return DesignMovePlan(
        True,
        stage_xy=normalized_stage,
        design_xy=normalized_design,
        source_label=source_label,
        last_selected_design_point=normalized_design,
        pending_planned_move_target_xy=normalized_stage,
    )


def design_panel_presentation(
    session: DesignSession,
    registration: RegistrationWorkflowSnapshot,
    route_running: bool,
    pending_alignment_preparation: bool,
    design_snap_enabled: bool,
) -> DesignPanelPresentation:
    current_target = session_navigation.current_target(session)
    design_count = len(registration.source_design_marks)
    stage_count = len(registration.source_stage_marks)
    if design_count < 2:
        calibration_prompt = "Pick mark 1 with left click and mark 2 with right click in the design window."
    elif stage_count < design_count:
        calibration_prompt = f"Center chip mark {stage_count + 1} and capture it."
    elif registration.registration_valid:
        calibration_prompt = "Calibration complete. Use the minimap or click in the design window to navigate."
    else:
        calibration_prompt = (
            f"{design_count} mark pairs captured. Waiting for chip rotation to finish."
        )
    return DesignPanelPresentation(
        document=session.document,
        design_snap_enabled=bool(design_snap_enabled),
        registration_valid=registration.registration_valid,
        targets=list(session.targets),
        selected_target_id=current_target.id if current_target else None,
        route=session.route,
        selected_route_point_index=session.selected_route_point_index,
        route_measurement_running=bool(route_running),
        calibration_prompt=(
            "Calibration step 4/4: chip rotation is in progress."
            if pending_alignment_preparation
            else calibration_prompt
        ),
        registration_status=registration.registration_status,
        source_design_marks=tuple(registration.source_design_marks),
        check_design_marks=tuple(registration.check_design_marks),
        source_stage_marks=tuple(registration.source_stage_marks),
    )


def design_position_presentation(
    session: DesignSession,
    registration: RegistrationWorkflowSnapshot,
    stage_xy: Point2D | None,
    design_xy: Point2D | None,
    fov_design_size: Point2D | None,
    last_selected_design_point: Point2D | None,
) -> DesignPositionPresentation:
    current_target = session_navigation.current_target(session)
    return DesignPositionPresentation(
        document=session.document,
        targets=list(session.targets),
        selected_target_id=current_target.id if current_target else None,
        probe_route=session.route,
        selected_route_point_index=session.selected_route_point_index,
        selected_design_point=last_selected_design_point,
        stage_xy=stage_xy,
        current_design_position=design_xy,
        fov_design_size=fov_design_size,
        source_design_marks=tuple(registration.source_design_marks),
        check_design_marks=tuple(registration.check_design_marks),
    )


__all__ = [
    "DesignMovePlan",
    "DesignPanelPresentation",
    "DesignPositionPresentation",
    "DesignTargetSelectionPlan",
    "design_panel_presentation",
    "design_position_presentation",
    "plan_design_coordinate_move",
    "plan_design_target_move",
    "select_design_target",
    "select_next_design_target",
    "select_previous_design_target",
]
