"""Design registration, frame linking, and calibration state policy."""

from __future__ import annotations

import math
from copy import deepcopy
from dataclasses import dataclass
from typing import Callable, Protocol

from probe_station_gui.coordinates.model import CoordinateFrameRecord
from probe_station_gui.coordinates.source_identity import source_identity
from probe_station_gui.coordinates.transforms import rotate_xy
from probe_station_gui.design.frame_registration import DesignFrameMetadata
from probe_station_gui.design.model import (
    DesignDocument,
    DesignModelError,
    Point2D,
)
from probe_station_gui.design.rigid_registration import DesignRegistration


DEFAULT_B_AXIS_ROTATION_PIVOT_STAGE: Point2D = (0.0, 0.0)


class _RegistrationSession(Protocol):
    document: DesignDocument | None
    registration: DesignRegistration | None
    source_design_marks: tuple[Point2D, ...]
    source_stage_marks: tuple[Point2D, ...]
    check_design_marks: list[Point2D]
    check_stage_marks: list[Point2D]
    registration_status: str
    active_frame_id: str | None
    _runtime_blocked_persisted_state: dict[str, object] | None
    _legacy_stage_coordinate_provenance: object
    _legacy_stage_coordinate_provenance_present: bool


def _rotate_machine_point_about_pivot(
    point: Point2D,
    pivot: Point2D,
    angle_deg: float,
) -> Point2D:
    rotated = rotate_xy(
        (float(point[0]) - float(pivot[0]), float(point[1]) - float(pivot[1])),
        angle_deg,
    )
    return (float(pivot[0]) + rotated[0], float(pivot[1]) + rotated[1])


@dataclass(frozen=True)
class AlignmentPreparation:
    """Prepared source-mark alignment ready to be committed after B rotation."""

    design_marks: tuple[Point2D, ...]
    stage_marks_before_rotation: tuple[Point2D, ...]
    stage_marks_after_rotation: tuple[Point2D, ...]
    pivot_stage: Point2D
    rotation_deg: float
    design_distance_mm: float
    stage_distance_mm: float
    distance_ratio: float
    rms_residual_mm: float = 0.0
    max_residual_mm: float = 0.0


@dataclass(frozen=True)
class DesignFrameLinkProjection:
    """Validated, side-effect-free projection of one durable Design frame."""

    frame_id: str
    source_design_marks: tuple[Point2D, ...]
    source_stage_marks: tuple[Point2D, ...]
    check_design_marks: tuple[Point2D, ...]
    check_stage_marks: tuple[Point2D, ...]


def clear_registration(session: _RegistrationSession) -> None:
    """Drop source marks, check marks, and the active registration."""
    session.source_design_marks = ()
    session.source_stage_marks = ()
    session.check_design_marks.clear()
    session.check_stage_marks.clear()
    session.registration = None
    session.registration_status = "No design registration."
    session.active_frame_id = None
    session._runtime_blocked_persisted_state = None
    session._legacy_stage_coordinate_provenance = None
    session._legacy_stage_coordinate_provenance_present = False


def legacy_registration_waiting_for_b(session: _RegistrationSession) -> bool:
    return bool(
        session.active_frame_id is None
        and session._runtime_blocked_persisted_state is not None
    )


def block_legacy_registration_until_b(
    session: _RegistrationSession, reason: str, *, persisted_state: dict[str, object]
) -> None:
    """Block legacy runtime use without changing its persisted payload."""
    if session.active_frame_id is not None:
        return
    if session._runtime_blocked_persisted_state is None:
        session._runtime_blocked_persisted_state = deepcopy(persisted_state)
    invalidate_registration(session, reason)


def link_active_frame(
    session: _RegistrationSession,
    frame: CoordinateFrameRecord,
    *,
    machine_point_for_navigation: Callable[[Point2D], Point2D] | None = None,
    machine_b_deg: float | None = None,
    pivot_machine_xy: Point2D = DEFAULT_B_AXIS_ROTATION_PIVOT_STAGE,
) -> None:
    """Link one durable frame and project it into legacy navigation state."""
    projection = prepare_active_frame_link(
        session,
        frame,
        machine_point_for_navigation=machine_point_for_navigation,
        machine_b_deg=machine_b_deg,
        pivot_machine_xy=pivot_machine_xy,
    )
    apply_active_frame_link(session, frame, projection)


def prepare_active_frame_link(
    session: _RegistrationSession,
    frame: CoordinateFrameRecord,
    *,
    machine_point_for_navigation: Callable[[Point2D], Point2D] | None = None,
    machine_b_deg: float | None = None,
    pivot_machine_xy: Point2D = DEFAULT_B_AXIS_ROTATION_PIVOT_STAGE,
) -> DesignFrameLinkProjection:
    """Validate and project a frame without mutating session state."""
    metadata = DesignFrameMetadata.from_mapping(frame.metadata)
    if session.document is None:
        raise DesignModelError("Load a design before selecting its coordinate frame.")
    if source_identity(metadata.source_path) != source_identity(session.document.path):
        raise DesignModelError("Coordinate frame belongs to a different design file.")
    if metadata.top_cell_name != session.document.top_cell_name:
        raise DesignModelError(
            "Coordinate frame belongs to a different design top cell."
        )
    turns = int(session.document.rotation_quarter_turns) % 4
    project_machine = machine_point_for_navigation or (
        lambda point: (float(point[0]), float(point[1]))
    )
    source_physical_marks = metadata.source_machine_marks
    check_physical_marks = metadata.check_machine_marks
    if machine_b_deg is not None and frame.transform is not None:
        current_b = float(machine_b_deg)
        pivot = (float(pivot_machine_xy[0]), float(pivot_machine_xy[1]))
        delta_b = current_b - frame.transform.reference_b_deg
        source_physical_marks = tuple(
            (
                _rotate_machine_point_about_pivot(point, pivot, delta_b)
                for point in metadata.source_machine_marks
            )
        )
        check_physical_marks = tuple(
            (
                _rotate_machine_point_about_pivot(point, pivot, delta_b)
                for point in metadata.check_machine_marks
            )
        )
    try:
        source_machine_marks = tuple(
            (project_machine(point) for point in source_physical_marks)
        )
        check_machine_marks = tuple(
            (project_machine(point) for point in check_physical_marks)
        )
    except Exception as exc:
        raise DesignModelError(
            f"Design frame coordinates are unavailable: {exc}"
        ) from exc
    return DesignFrameLinkProjection(
        frame_id=frame.frame_id,
        source_design_marks=tuple(
            (
                session.document.rotate_point(point, turns)
                for point in metadata.source_design_marks
            )
        ),
        source_stage_marks=source_machine_marks,
        check_design_marks=tuple(
            (
                session.document.rotate_point(point, turns)
                for point in metadata.check_design_marks
            )
        ),
        check_stage_marks=check_machine_marks,
    )


def apply_active_frame_link(
    session: _RegistrationSession,
    frame: CoordinateFrameRecord,
    projection: DesignFrameLinkProjection,
) -> None:
    """Apply a previously validated frame projection to this session."""
    if projection.frame_id != frame.frame_id:
        raise DesignModelError("Design frame projection no longer matches the frame.")
    session._runtime_blocked_persisted_state = None
    session._legacy_stage_coordinate_provenance = None
    session._legacy_stage_coordinate_provenance_present = False
    session.active_frame_id = frame.frame_id
    session.source_design_marks = projection.source_design_marks
    session.source_stage_marks = projection.source_stage_marks
    session.check_design_marks = list(projection.check_design_marks)
    session.check_stage_marks = list(projection.check_stage_marks)
    session.registration = None
    if len(session.source_design_marks) >= 2:
        try:
            rebuild_registration(session)
        except DesignModelError as exc:
            session.registration_status = str(exc)
    if session.registration is None:
        session.registration_status = "Design frame registration is incomplete."
        return
    unavailable = [
        frame.readiness[axis]
        for axis in ("X", "Y", "B")
        if not frame.readiness[axis].available
    ]
    if unavailable:
        reason = next(
            (state.reason for state in unavailable if state.reason),
            "Design frame registration is unavailable.",
        )
        session.registration = session.registration.mark_stale(reason)
        session.registration_status = reason


def clear_source_stage_marks(session: _RegistrationSession) -> None:
    """Drop captured stage-side marks while preserving selected design marks."""
    session.source_stage_marks = ()
    session.check_design_marks.clear()
    session.check_stage_marks.clear()
    session._legacy_stage_coordinate_provenance = None
    session._legacy_stage_coordinate_provenance_present = False
    session.registration = None
    session.registration_status = calibration_prompt(session)


def record_legacy_stage_coordinate_provenance(
    session: _RegistrationSession, provenance: object
) -> None:
    """Retain one verified configured-coordinate context for legacy marks."""
    if session.active_frame_id is not None:
        return
    captured = deepcopy(provenance)
    present = bool(
        getattr(session, "_legacy_stage_coordinate_provenance_present", False)
    )
    previous = getattr(session, "_legacy_stage_coordinate_provenance", None)
    if not present or not (session.source_stage_marks or session.check_stage_marks):
        session._legacy_stage_coordinate_provenance = captured
    elif previous != captured:
        session._legacy_stage_coordinate_provenance = {
            "conflicting_capture_provenance": [deepcopy(previous), captured]
        }
    session._legacy_stage_coordinate_provenance_present = True


def has_complete_source_design_marks(session: _RegistrationSession) -> bool:
    """Return whether both design-side source marks are selected."""
    return len(source_design_marks_compact(session)) >= 2


def capture_source_pair(
    session: _RegistrationSession, design_point: Point2D, stage_point: Point2D
) -> int:
    """Append a matched design/stage pair for simplified calibration."""
    design_marks = tuple(source_design_marks_compact(session))
    stage_marks = tuple(source_stage_marks_compact(session))
    if len(design_marks) != len(stage_marks):
        raise DesignModelError("Source mark capture is incomplete.")
    session.source_design_marks = design_marks + (_point(design_point),)
    session.source_stage_marks = stage_marks + (_point(stage_point),)
    session.registration = None
    pair_count = source_pair_count(session)
    if pair_count < 2:
        session.registration_status = (
            "Calibration step 2/4: choose the second design mark."
        )
    else:
        session.registration_status = (
            f"{pair_count} mark pairs captured. Preparing chip rotation."
        )
    return pair_count


def set_source_design_mark(
    session: _RegistrationSession, slot: int, point: Point2D
) -> None:
    """Set or append one design-space calibration mark by index."""
    session.source_design_marks = _with_slot_point(
        session.source_design_marks, slot, point
    )
    session.registration = None
    session.registration_status = calibration_prompt(session)


def set_source_stage_mark(
    session: _RegistrationSession, slot: int, point: Point2D
) -> None:
    """Set or append one stage-space calibration mark by index."""
    session.source_stage_marks = _with_slot_point(
        session.source_stage_marks, slot, point
    )
    session.registration = None
    session.registration_status = calibration_prompt(session)


def source_pair_count(session: _RegistrationSession) -> int:
    """Return how many calibration slots contain both design and stage points."""
    return min(
        len(source_design_marks_compact(session)),
        len(source_stage_marks_compact(session)),
    )


def source_design_marks_compact(session: _RegistrationSession) -> list[Point2D]:
    """Return populated design calibration marks in slot order."""
    return [point for point in session.source_design_marks if point is not None]


def source_stage_marks_compact(session: _RegistrationSession) -> list[Point2D]:
    """Return populated stage calibration marks in slot order."""
    return [point for point in session.source_stage_marks if point is not None]


def calibration_prompt(session: _RegistrationSession) -> str:
    """Return a short operator-facing prompt for the next calibration step."""
    design_count = len(source_design_marks_compact(session))
    stage_count = len(source_stage_marks_compact(session))
    if design_count < 2:
        return "Pick mark 1 with left click and mark 2 with right click in the design window."
    if stage_count < design_count:
        return f"Center chip mark {stage_count + 1} and capture it."
    if session.registration is not None and session.registration.valid:
        return "Calibration complete. Use the minimap or click in the design window to navigate."
    return f"{design_count} mark pairs captured. Waiting for chip rotation to finish."


def prepare_source_alignment(session: _RegistrationSession) -> AlignmentPreparation:
    """Fit all captured pairs and prepare their B-axis correction."""
    return prepare_alignment_draft(
        session,
        tuple(source_design_marks_compact(session)),
        tuple(source_stage_marks_compact(session)),
    )


def prepare_alignment_draft(
    session: _RegistrationSession,
    design_marks: tuple[Point2D, ...],
    stage_marks: tuple[Point2D, ...],
) -> AlignmentPreparation:
    """Prepare an alignment without changing the active registration."""
    if session.document is None:
        raise DesignModelError("No design document is loaded.")
    design_marks = tuple((_point(point) for point in design_marks))
    stage_marks = tuple((_point(point) for point in stage_marks))
    if len(design_marks) < 2 or len(design_marks) != len(stage_marks):
        raise DesignModelError("At least two complete mark pairs are required.")
    design_unit_mm = float(session.document.dbu) * 1000.0
    fitted = DesignRegistration.from_marks(
        design_marks, stage_marks, design_unit_mm=design_unit_mm
    )
    design_a, design_b = design_marks[:2]
    stage_a, stage_b = stage_marks[:2]
    design_dx = float(design_b[0] - design_a[0])
    design_dy = float(design_b[1] - design_a[1])
    stage_dx = float(stage_b[0] - stage_a[0])
    stage_dy = float(stage_b[1] - stage_a[1])
    design_distance_units = math.hypot(design_dx, design_dy)
    stage_distance_mm = math.hypot(stage_dx, stage_dy)
    design_distance_mm = design_distance_units * float(session.document.dbu) * 1000.0
    if design_distance_mm <= 1e-09 or stage_distance_mm <= 1e-09:
        raise DesignModelError("Calibration marks are too close together.")
    rotation_deg = -float(fitted.rotation_deg)
    while rotation_deg <= -180.0:
        rotation_deg += 360.0
    while rotation_deg > 180.0:
        rotation_deg -= 360.0
    pivot_stage = DEFAULT_B_AXIS_ROTATION_PIVOT_STAGE
    adjusted_stage_marks = tuple(
        (_rotate_stage_point(point, pivot_stage, rotation_deg) for point in stage_marks)
    )
    distance_ratio = fitted.distance_scale_ratio
    return AlignmentPreparation(
        design_marks=design_marks,
        stage_marks_before_rotation=stage_marks,
        stage_marks_after_rotation=adjusted_stage_marks,
        pivot_stage=pivot_stage,
        rotation_deg=rotation_deg,
        design_distance_mm=design_distance_mm,
        stage_distance_mm=stage_distance_mm,
        distance_ratio=distance_ratio,
        rms_residual_mm=fitted.source_residual_summary.rms,
        max_residual_mm=fitted.source_residual_summary.max_error,
    )


def apply_prepared_alignment(
    session: _RegistrationSession, preparation: AlignmentPreparation
) -> None:
    """Commit a prepared alignment after the B-axis rotation succeeds."""
    session.source_design_marks = tuple(preparation.design_marks)
    session.source_stage_marks = tuple(preparation.stage_marks_after_rotation)
    rebuild_registration(session)
    if session.registration is not None and session.registration.valid:
        session.registration_status = f"Calibration complete. Rotation {preparation.rotation_deg:+.3f} deg, spacing ratio {preparation.distance_ratio:.3f}."


def invalidate_registration(session: _RegistrationSession, reason: str) -> None:
    """Mark the current registration stale while retaining captured marks."""
    if session.registration is not None:
        session.registration = session.registration.mark_stale(reason)
    session.registration_status = reason


def add_source_design_mark(session: _RegistrationSession, point: Point2D) -> None:
    """Append a design-space source mark."""
    session.source_design_marks = tuple(source_design_marks_compact(session)) + (
        _point(point),
    )
    session.registration = None
    rebuild_registration(session)


def add_source_stage_mark(session: _RegistrationSession, point: Point2D) -> None:
    """Append a stage-space source mark."""
    session.source_stage_marks = tuple(source_stage_marks_compact(session)) + (
        _point(point),
    )
    session.registration = None
    rebuild_registration(session)


def add_check_design_mark(session: _RegistrationSession, point: Point2D) -> None:
    """Append a design-space residual check mark."""
    session.check_design_marks.append((float(point[0]), float(point[1])))
    rebuild_registration(session)


def add_check_stage_mark(session: _RegistrationSession, point: Point2D) -> None:
    """Append a stage-space residual check mark."""
    session.check_stage_marks.append((float(point[0]), float(point[1])))
    rebuild_registration(session)


def build_registration(
    document: DesignDocument | None,
    design_marks: tuple[Point2D, ...],
    stage_marks: tuple[Point2D, ...],
    check_design_marks: tuple[Point2D, ...],
    check_stage_marks: tuple[Point2D, ...],
) -> tuple[DesignRegistration | None, str]:
    """Build one detached registration and its exact operator status."""

    if len(design_marks) < 2 or len(stage_marks) < 2:
        return None, (
            f"Design marks: {len(design_marks)}/2 | Stage marks: {len(stage_marks)}/2"
        )
    if len(design_marks) != len(stage_marks):
        return None, "Source mark capture is incomplete."
    if len(check_design_marks) != len(check_stage_marks):
        return None, "Check mark capture is incomplete."
    design_unit_mm = 1.0 if document is None else float(document.dbu) * 1e3
    registration = DesignRegistration.from_marks(
        design_marks,
        stage_marks,
        check_design_marks=check_design_marks,
        check_stage_marks=check_stage_marks,
        design_unit_mm=design_unit_mm,
    )
    source_summary = registration.source_residual_summary
    check_summary = registration.residual_summary
    source_text = (
        f"Fit RMS {source_summary.rms:.4f} mm, "
        f"max {source_summary.max_error:.4f} mm over {source_summary.count} marks."
    )
    if check_summary.count:
        return registration, (
            f"Registered. {source_text} "
            f"Check RMS {check_summary.rms:.4f} mm, "
            f"max {check_summary.max_error:.4f} mm over {check_summary.count} marks."
        )
    return registration, f"Registered. {source_text}"


def rebuild_registration(session: _RegistrationSession) -> None:
    """Rebuild the active registration when enough data is available."""
    design_marks = source_design_marks_compact(session)
    stage_marks = source_stage_marks_compact(session)
    session.source_design_marks = tuple(design_marks)
    session.source_stage_marks = tuple(stage_marks)
    session.registration, session.registration_status = build_registration(
        session.document,
        tuple(design_marks),
        tuple(stage_marks),
        tuple(session.check_design_marks),
        tuple(session.check_stage_marks),
    )


def _design_unit_mm(session: _RegistrationSession) -> float:
    """Return the active document's design-unit conversion in millimetres."""
    if session.document is None:
        return 1.0
    return float(session.document.dbu) * 1000.0


def _rotate_stage_point(point: Point2D, pivot: Point2D, rotation_deg: float) -> Point2D:
    angle = math.radians(rotation_deg)
    dx = float(point[0] - pivot[0])
    dy = float(point[1] - pivot[1])
    cos_a = math.cos(angle)
    sin_a = math.sin(angle)
    return (
        float(pivot[0] + dx * cos_a - dy * sin_a),
        float(pivot[1] + dx * sin_a + dy * cos_a),
    )


def _with_slot_point(
    slots: tuple[Point2D, ...] | list[Point2D | None], slot: int, point: Point2D
) -> tuple[Point2D, ...]:
    populated = [item for item in slots if item is not None]
    if slot < 0 or slot > len(populated):
        raise DesignModelError("Calibration mark index is not contiguous.")
    normalized = _point(point)
    if slot == len(populated):
        populated.append(normalized)
    else:
        populated[slot] = normalized
    return tuple(populated)


def _point(point: Point2D) -> Point2D:
    x_value = float(point[0])
    y_value = float(point[1])
    if not math.isfinite(x_value) or not math.isfinite(y_value):
        raise DesignModelError("Calibration marks must be finite 2D points.")
    return (x_value, y_value)


__all__ = [
    "AlignmentPreparation",
    "DEFAULT_B_AXIS_ROTATION_PIVOT_STAGE",
    "DesignFrameLinkProjection",
]
