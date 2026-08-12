"""State container for optional GDS-backed navigation workflows."""

from __future__ import annotations

import math
from copy import deepcopy
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Optional

from probe_station_gui.coordinates.model import CoordinateFrameRecord
from probe_station_gui.coordinates.source_identity import source_identity
from probe_station_gui.coordinates.transforms import rotate_xy
from probe_station_gui.design.frame_registration import DesignFrameMetadata
from probe_station_gui.design.model import (
    DesignDocument,
    DesignModelError,
    LayerKey,
    MeasurementTarget,
    Point2D,
)
from probe_station_gui.design.rigid_registration import DesignRegistration
from probe_station_gui.design.session_state import (
    DesignSessionState,
    export_persisted_session_state,
    restore_session_state,
    snapshot_session_state,
)
from probe_station_gui.route.model import MeasurementRoute, RoutePoint


DEFAULT_B_AXIS_ROTATION_PIVOT_STAGE: Point2D = (0.0, 0.0)


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


@dataclass
class DesignSession:
    """Track the loaded design, registration, and route navigation state."""

    document: Optional[DesignDocument] = None
    registration: Optional[DesignRegistration] = None
    source_design_marks: tuple[Point2D, ...] = ()
    source_stage_marks: tuple[Point2D, ...] = ()
    check_design_marks: list[Point2D] = field(default_factory=list)
    check_stage_marks: list[Point2D] = field(default_factory=list)
    targets: list[MeasurementTarget] = field(default_factory=list)
    selected_target_index: int = -1
    route: MeasurementRoute | None = None
    selected_route_point_index: int = -1
    registration_status: str = "No design registration."
    active_frame_id: str | None = None
    _runtime_blocked_persisted_state: dict[str, object] | None = field(
        default=None,
        init=False,
        repr=False,
        compare=False,
    )
    _legacy_stage_coordinate_provenance: object = field(
        default=None,
        init=False,
        repr=False,
        compare=False,
    )
    _legacy_stage_coordinate_provenance_present: bool = field(
        default=False,
        init=False,
        repr=False,
        compare=False,
    )

    def snapshot_state(self) -> DesignSessionState:
        """Return a detached snapshot without replacing this session."""

        return snapshot_session_state(self)

    def apply_state(self, state: DesignSessionState) -> None:
        """Replace owned state atomically while retaining this session's identity."""

        restore_session_state(self, state)

    def export_persisted_state(self) -> dict[str, object] | None:
        """Return design state tied to the current controller coordinate session."""

        snapshot = self.snapshot_state()
        if self.document is None or (
            self.active_frame_id is None
            and self._runtime_blocked_persisted_state is not None
        ):
            return export_persisted_session_state(snapshot)
        document_size = None
        document_mtime_ns = None
        try:
            stat = self.document.path.stat()
        except OSError:
            pass
        else:
            document_mtime_ns = int(stat.st_mtime_ns)
            document_size = int(stat.st_size)
        route_state = self._export_persisted_route_state()
        return export_persisted_session_state(
            snapshot,
            document_size=document_size,
            document_mtime_ns=document_mtime_ns,
            route_path=(None if route_state is None else str(route_state["path"])),
        )

    def restore_persisted_state(
        self,
        document: DesignDocument,
        state: dict[str, object],
    ) -> None:
        """Restore persisted design state for a verified controller session."""

        self.document = document
        self.clear_targets()
        self.clear_route()
        try:
            state_version = int(state.get("version", 1))
        except (TypeError, ValueError):
            state_version = 1
        self.active_frame_id = None
        self._legacy_stage_coordinate_provenance_present = (
            state_version < 3 and "stage_coordinate_provenance" in state
        )
        self._legacy_stage_coordinate_provenance = (
            deepcopy(state.get("stage_coordinate_provenance"))
            if self._legacy_stage_coordinate_provenance_present
            else None
        )
        if state_version >= 3:
            frame_id = str(state.get("active_frame_id") or "").strip()
            self.active_frame_id = frame_id or None
            self.source_design_marks = ()
            self.source_stage_marks = ()
            self.check_design_marks = []
            self.check_stage_marks = []
            self.registration = None
            self.registration_status = "Design frame is loading."
            self._restore_persisted_route(document, state.get("route"))
            return
        if state_version <= 1:
            self.source_design_marks = tuple(
                point
                for point in self._coerce_optional_points(
                    state.get("source_design_marks"),
                    expected_count=2,
                )
                if point is not None
            )
            self.source_stage_marks = tuple(
                point
                for point in self._coerce_optional_points(
                    state.get("source_stage_marks"),
                    expected_count=2,
                )
                if point is not None
            )
        else:
            self.source_design_marks = tuple(
                self._coerce_points(state.get("source_design_marks"))
            )
            self.source_stage_marks = tuple(
                self._coerce_points(state.get("source_stage_marks"))
            )
        self.check_design_marks = self._coerce_points(state.get("check_design_marks"))
        self.check_stage_marks = self._coerce_points(state.get("check_stage_marks"))
        self._restore_persisted_route(document, state.get("route"))
        self.registration = None
        self.registration_status = str(
            state.get("registration_status") or "No design registration."
        )
        try:
            self._rebuild_registration()
        except DesignModelError as exc:
            self.registration = None
            self.registration_status = str(exc)
            return
        if self.registration is None:
            return
        if not bool(state.get("registration_valid", True)):
            reason = str(
                state.get("registration_stale_reason")
                or state.get("registration_status")
                or "Design registration is stale."
            )
            self.registration = self.registration.mark_stale(reason)
            self.registration_status = reason

    def _export_persisted_route_state(self) -> dict[str, object] | None:
        route = self.route
        if route is None or route.path is None:
            return None
        route_path = Path(route.path).expanduser().resolve()
        try:
            route_path.stat()
        except OSError:
            return None
        return {
            "path": str(route_path),
            "selected_route_point_index": int(self.selected_route_point_index),
        }

    def _restore_persisted_route(
        self,
        document: DesignDocument,
        value: object,
    ) -> None:
        self.clear_route()
        if not isinstance(value, dict):
            return
        path_text = str(value.get("path") or "").strip()
        if not path_text:
            return
        route_path = Path(path_text).expanduser()
        if not route_path.exists():
            return
        try:
            route = MeasurementRoute.load(route_path)
            route.validate_for_document(document)
        except DesignModelError:
            return
        self.route = route
        try:
            selected_index = int(value.get("selected_route_point_index", 0))
        except (TypeError, ValueError):
            selected_index = 0
        if route.points:
            self.selected_route_point_index = min(
                max(0, selected_index),
                len(route.points) - 1,
            )
        else:
            self.selected_route_point_index = -1

    def load_document(self, document: DesignDocument) -> None:
        """Attach a new design document and clear derived state."""

        self._runtime_blocked_persisted_state = None
        self.document = document
        self.active_frame_id = None
        self.clear_targets()
        self.clear_route()
        self.clear_registration()

    def unload_document(self) -> None:
        """Remove the active design and all related state."""

        self.document = None
        self.active_frame_id = None
        self.clear_targets()
        self.clear_route()
        self.clear_registration()

    def set_top_cell(self, top_cell_name: str) -> None:
        """Switch the active top cell and clear derived state."""

        if self.document is None:
            raise DesignModelError("No design document is loaded.")
        self.load_document(self.document.with_top_cell(top_cell_name))

    def set_visible_layers(self, layers: set[LayerKey]) -> None:
        """Update the active visible layer subset."""

        if self.document is None:
            raise DesignModelError("No design document is loaded.")
        self.document = self.document.with_visible_layers(layers)

    def rotate_document(self, quarter_turn_delta: int) -> DesignDocument:
        """Rotate the active design and all design-space annotations by 90-degree steps."""

        if self.document is None:
            raise DesignModelError("No design document is loaded.")
        delta = int(quarter_turn_delta) % 4
        if delta == 0:
            return self.document

        old_document = self.document
        new_document = old_document.with_rotation_delta(delta)

        def rotate_point(point: Point2D) -> Point2D:
            return old_document.rotate_point(point, delta)

        registration_was_stale = (
            self.registration is not None and not self.registration.valid
        )
        stale_reason = (
            self.registration.stale_reason
            if self.registration is not None and self.registration.stale_reason
            else self.registration_status
        )

        self.document = new_document
        self.source_design_marks = tuple(
            rotate_point(point) for point in self.source_design_marks_compact()
        )
        self.check_design_marks = [
            rotate_point(point) for point in self.check_design_marks
        ]
        self.targets = [
            replace(target, design_center=rotate_point(target.design_center))
            for target in self.targets
        ]
        if self.route is not None:
            self.route.transform_design_coordinates(
                new_document,
                rotate_point,
                lambda vector: old_document.rotate_vector(vector, delta),
            )

        self.registration = None
        self._rebuild_registration()
        if registration_was_stale and self.registration is not None:
            self.registration = self.registration.mark_stale(stale_reason)
            self.registration_status = stale_reason
        return new_document

    def clear_registration(self) -> None:
        """Drop source marks, check marks, and the active registration."""

        self.source_design_marks = ()
        self.source_stage_marks = ()
        self.check_design_marks.clear()
        self.check_stage_marks.clear()
        self.registration = None
        self.registration_status = "No design registration."
        self.active_frame_id = None
        self._runtime_blocked_persisted_state = None
        self._legacy_stage_coordinate_provenance = None
        self._legacy_stage_coordinate_provenance_present = False

    @property
    def legacy_registration_waiting_for_b(self) -> bool:
        return bool(
            self.active_frame_id is None
            and self._runtime_blocked_persisted_state is not None
        )

    def block_legacy_registration_until_b(
        self,
        reason: str,
        *,
        persisted_state: dict[str, object] | None = None,
    ) -> None:
        """Block legacy runtime use without changing its persisted payload."""

        self.block_legacy_registration(reason, persisted_state=persisted_state)

    def block_legacy_registration(
        self,
        reason: str,
        *,
        persisted_state: dict[str, object] | None = None,
    ) -> None:
        """Block legacy runtime use without changing its persisted payload."""

        if self.active_frame_id is not None:
            return
        if self._runtime_blocked_persisted_state is None:
            persisted = (
                deepcopy(persisted_state)
                if persisted_state is not None
                else self.export_persisted_state()
            )
            if persisted is not None:
                self._runtime_blocked_persisted_state = deepcopy(persisted)
        self.invalidate_registration(reason)

    def link_active_frame(
        self,
        frame: CoordinateFrameRecord,
        *,
        machine_point_for_navigation: Callable[[Point2D], Point2D] | None = None,
        machine_b_deg: float | None = None,
        pivot_machine_xy: Point2D = DEFAULT_B_AXIS_ROTATION_PIVOT_STAGE,
    ) -> None:
        """Link one durable frame and project it into legacy navigation state."""

        projection = self.prepare_active_frame_link(
            frame,
            machine_point_for_navigation=machine_point_for_navigation,
            machine_b_deg=machine_b_deg,
            pivot_machine_xy=pivot_machine_xy,
        )
        self.apply_active_frame_link(frame, projection)

    def prepare_active_frame_link(
        self,
        frame: CoordinateFrameRecord,
        *,
        machine_point_for_navigation: Callable[[Point2D], Point2D] | None = None,
        machine_b_deg: float | None = None,
        pivot_machine_xy: Point2D = DEFAULT_B_AXIS_ROTATION_PIVOT_STAGE,
    ) -> DesignFrameLinkProjection:
        """Validate and project a frame without mutating session state."""

        metadata = DesignFrameMetadata.from_mapping(frame.metadata)
        if self.document is None:
            raise DesignModelError(
                "Load a design before selecting its coordinate frame."
            )
        if source_identity(metadata.source_path) != source_identity(self.document.path):
            raise DesignModelError(
                "Coordinate frame belongs to a different design file."
            )
        if metadata.top_cell_name != self.document.top_cell_name:
            raise DesignModelError(
                "Coordinate frame belongs to a different design top cell."
            )
        turns = int(self.document.rotation_quarter_turns) % 4
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
                _rotate_machine_point_about_pivot(point, pivot, delta_b)
                for point in metadata.source_machine_marks
            )
            check_physical_marks = tuple(
                _rotate_machine_point_about_pivot(point, pivot, delta_b)
                for point in metadata.check_machine_marks
            )
        try:
            source_machine_marks = tuple(
                project_machine(point) for point in source_physical_marks
            )
            check_machine_marks = tuple(
                project_machine(point) for point in check_physical_marks
            )
        except Exception as exc:
            raise DesignModelError(
                f"Design frame coordinates are unavailable: {exc}"
            ) from exc
        return DesignFrameLinkProjection(
            frame_id=frame.frame_id,
            source_design_marks=tuple(
                self.document.rotate_point(point, turns)
                for point in metadata.source_design_marks
            ),
            source_stage_marks=source_machine_marks,
            check_design_marks=tuple(
                self.document.rotate_point(point, turns)
                for point in metadata.check_design_marks
            ),
            check_stage_marks=check_machine_marks,
        )

    def apply_active_frame_link(
        self,
        frame: CoordinateFrameRecord,
        projection: DesignFrameLinkProjection,
    ) -> None:
        """Apply a previously validated frame projection to this session."""

        if projection.frame_id != frame.frame_id:
            raise DesignModelError(
                "Design frame projection no longer matches the frame."
            )
        self._runtime_blocked_persisted_state = None
        self._legacy_stage_coordinate_provenance = None
        self._legacy_stage_coordinate_provenance_present = False
        self.active_frame_id = frame.frame_id
        self.source_design_marks = projection.source_design_marks
        self.source_stage_marks = projection.source_stage_marks
        self.check_design_marks = list(projection.check_design_marks)
        self.check_stage_marks = list(projection.check_stage_marks)
        self.registration = None
        if len(self.source_design_marks) >= 2:
            try:
                self._rebuild_registration()
            except DesignModelError as exc:
                self.registration_status = str(exc)
        if self.registration is None:
            self.registration_status = "Design frame registration is incomplete."
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
            self.registration = self.registration.mark_stale(reason)
            self.registration_status = reason

    def clear_source_stage_marks(self) -> None:
        """Drop captured stage-side marks while preserving selected design marks."""

        self.source_stage_marks = ()
        self.check_design_marks.clear()
        self.check_stage_marks.clear()
        self._legacy_stage_coordinate_provenance = None
        self._legacy_stage_coordinate_provenance_present = False
        self.registration = None
        self.registration_status = self.calibration_prompt()

    def record_legacy_stage_coordinate_provenance(
        self,
        provenance: object,
    ) -> None:
        """Retain one verified configured-coordinate context for legacy marks."""

        if self.active_frame_id is not None:
            return
        captured = deepcopy(provenance)
        present = bool(
            getattr(
                self,
                "_legacy_stage_coordinate_provenance_present",
                False,
            )
        )
        previous = getattr(self, "_legacy_stage_coordinate_provenance", None)
        if not present or not (self.source_stage_marks or self.check_stage_marks):
            self._legacy_stage_coordinate_provenance = captured
        elif previous != captured:
            self._legacy_stage_coordinate_provenance = {
                "conflicting_capture_provenance": [deepcopy(previous), captured]
            }
        self._legacy_stage_coordinate_provenance_present = True

    def has_complete_source_design_marks(self) -> bool:
        """Return whether both design-side source marks are selected."""

        return len(self.source_design_marks_compact()) >= 2

    def capture_source_pair(self, design_point: Point2D, stage_point: Point2D) -> int:
        """Append a matched design/stage pair for simplified calibration."""

        design_marks = tuple(self.source_design_marks_compact())
        stage_marks = tuple(self.source_stage_marks_compact())
        if len(design_marks) != len(stage_marks):
            raise DesignModelError("Source mark capture is incomplete.")
        self.source_design_marks = design_marks + (self._point(design_point),)
        self.source_stage_marks = stage_marks + (self._point(stage_point),)
        self.registration = None
        pair_count = self.source_pair_count()
        if pair_count < 2:
            self.registration_status = (
                "Calibration step 2/4: choose the second design mark."
            )
        else:
            self.registration_status = (
                f"{pair_count} mark pairs captured. Preparing chip rotation."
            )
        return pair_count

    def set_source_design_mark(self, slot: int, point: Point2D) -> None:
        """Set or append one design-space calibration mark by index."""

        self.source_design_marks = self._with_slot_point(
            self.source_design_marks,
            slot,
            point,
        )
        self.registration = None
        self.registration_status = self.calibration_prompt()

    def set_source_stage_mark(self, slot: int, point: Point2D) -> None:
        """Set or append one stage-space calibration mark by index."""

        self.source_stage_marks = self._with_slot_point(
            self.source_stage_marks,
            slot,
            point,
        )
        self.registration = None
        self.registration_status = self.calibration_prompt()

    def source_pair_count(self) -> int:
        """Return how many calibration slots contain both design and stage points."""

        return min(
            len(self.source_design_marks_compact()),
            len(self.source_stage_marks_compact()),
        )

    def source_design_marks_compact(self) -> list[Point2D]:
        """Return populated design calibration marks in slot order."""

        return [point for point in self.source_design_marks if point is not None]

    def source_stage_marks_compact(self) -> list[Point2D]:
        """Return populated stage calibration marks in slot order."""

        return [point for point in self.source_stage_marks if point is not None]

    def calibration_prompt(self) -> str:
        """Return a short operator-facing prompt for the next calibration step."""

        design_count = len(self.source_design_marks_compact())
        stage_count = len(self.source_stage_marks_compact())
        if design_count < 2:
            return "Pick mark 1 with left click and mark 2 with right click in the design window."
        if stage_count < design_count:
            return f"Center chip mark {stage_count + 1} and capture it."
        if self.registration is not None and self.registration.valid:
            return "Calibration complete. Use the minimap or click in the design window to navigate."
        return (
            f"{design_count} mark pairs captured. Waiting for chip rotation to finish."
        )

    def prepare_source_alignment(self) -> AlignmentPreparation:
        """Fit all captured pairs and prepare their B-axis correction."""

        return self.prepare_alignment_draft(
            tuple(self.source_design_marks_compact()),
            tuple(self.source_stage_marks_compact()),
        )

    def prepare_alignment_draft(
        self,
        design_marks: tuple[Point2D, ...],
        stage_marks: tuple[Point2D, ...],
    ) -> AlignmentPreparation:
        """Prepare an alignment without changing the active registration."""

        if self.document is None:
            raise DesignModelError("No design document is loaded.")
        design_marks = tuple(self._point(point) for point in design_marks)
        stage_marks = tuple(self._point(point) for point in stage_marks)
        if len(design_marks) < 2 or len(design_marks) != len(stage_marks):
            raise DesignModelError("At least two complete mark pairs are required.")

        design_unit_mm = float(self.document.dbu) * 1e3
        fitted = DesignRegistration.from_marks(
            design_marks,
            stage_marks,
            design_unit_mm=design_unit_mm,
        )
        design_a, design_b = design_marks[:2]
        stage_a, stage_b = stage_marks[:2]
        design_dx = float(design_b[0] - design_a[0])
        design_dy = float(design_b[1] - design_a[1])
        stage_dx = float(stage_b[0] - stage_a[0])
        stage_dy = float(stage_b[1] - stage_a[1])
        design_distance_units = math.hypot(design_dx, design_dy)
        stage_distance_mm = math.hypot(stage_dx, stage_dy)
        design_distance_mm = design_distance_units * float(self.document.dbu) * 1e3
        if design_distance_mm <= 1e-9 or stage_distance_mm <= 1e-9:
            raise DesignModelError("Calibration marks are too close together.")

        rotation_deg = -float(fitted.rotation_deg)
        while rotation_deg <= -180.0:
            rotation_deg += 360.0
        while rotation_deg > 180.0:
            rotation_deg -= 360.0

        pivot_stage = DEFAULT_B_AXIS_ROTATION_PIVOT_STAGE
        adjusted_stage_marks = tuple(
            self._rotate_stage_point(point, pivot_stage, rotation_deg)
            for point in stage_marks
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

    def apply_prepared_alignment(self, preparation: AlignmentPreparation) -> None:
        """Commit a prepared alignment after the B-axis rotation succeeds."""

        self.source_design_marks = tuple(preparation.design_marks)
        self.source_stage_marks = tuple(preparation.stage_marks_after_rotation)
        self._rebuild_registration()
        if self.registration is not None and self.registration.valid:
            self.registration_status = (
                "Calibration complete. "
                f"Rotation {preparation.rotation_deg:+.3f} deg, "
                f"spacing ratio {preparation.distance_ratio:.3f}."
            )

    def invalidate_registration(self, reason: str) -> None:
        """Mark the current registration stale while retaining captured marks."""

        if self.registration is not None:
            self.registration = self.registration.mark_stale(reason)
        self.registration_status = reason

    def add_source_design_mark(self, point: Point2D) -> None:
        """Append a design-space source mark."""

        self.source_design_marks = tuple(self.source_design_marks_compact()) + (
            self._point(point),
        )
        self.registration = None
        self._rebuild_registration()

    def add_source_stage_mark(self, point: Point2D) -> None:
        """Append a stage-space source mark."""

        self.source_stage_marks = tuple(self.source_stage_marks_compact()) + (
            self._point(point),
        )
        self.registration = None
        self._rebuild_registration()

    def add_check_design_mark(self, point: Point2D) -> None:
        """Append a design-space residual check mark."""

        self.check_design_marks.append((float(point[0]), float(point[1])))
        self._rebuild_registration()

    def add_check_stage_mark(self, point: Point2D) -> None:
        """Append a stage-space residual check mark."""

        self.check_stage_marks.append((float(point[0]), float(point[1])))
        self._rebuild_registration()

    def clear_targets(self) -> None:
        """Remove optional navigation targets."""

        self.targets.clear()
        self.selected_target_index = -1

    def set_targets(self, targets: list[MeasurementTarget]) -> None:
        """Replace optional navigation targets with a new ordered list."""

        self.targets = list(targets)
        self.selected_target_index = 0 if self.targets else -1

    def create_route(self, *, name: str | None = None) -> MeasurementRoute:
        """Create an empty route for the active design."""

        if self.document is None:
            raise DesignModelError("Load a design before creating a route.")
        route = MeasurementRoute.default_for_document(self.document, name=name)
        self.set_route(route)
        return route

    def set_route(self, route: MeasurementRoute) -> None:
        """Attach a route after checking that it belongs to the active design."""

        if self.document is None:
            raise DesignModelError("Load a design before loading a route.")
        route.validate_for_document(self.document)
        self.route = route
        self.selected_route_point_index = 0 if route.points else -1

    def clear_route(self) -> None:
        """Remove the current design-bound probe route."""

        self.route = None
        self.selected_route_point_index = -1

    def add_route_point(self, point: Point2D) -> RoutePoint:
        """Append a route point, creating a route for the active design if needed."""

        if self.document is None:
            raise DesignModelError("Load a design before adding route points.")
        if self.route is None:
            self.create_route()
        assert self.route is not None
        route_point = self.route.add_point(point)
        self.selected_route_point_index = len(self.route.points) - 1
        return route_point

    def remove_selected_route_point(self) -> RoutePoint | None:
        """Remove the selected route point and update selection."""

        if self.route is None:
            return None
        removed = self.route.remove_point_at(self.selected_route_point_index)
        if not self.route.points:
            self.selected_route_point_index = -1
        else:
            self.selected_route_point_index = min(
                max(self.selected_route_point_index, 0),
                len(self.route.points) - 1,
            )
        return removed

    def clear_route_points(self) -> None:
        """Remove all points from the current route without dropping its binding."""

        if self.route is None:
            return
        self.route.clear_points()
        self.selected_route_point_index = -1

    def select_route_point(self, index: int) -> RoutePoint | None:
        """Select one route point by row index."""

        if self.route is None or not 0 <= index < len(self.route.points):
            self.selected_route_point_index = -1
            return None
        self.selected_route_point_index = index
        return self.route.points[index]

    def current_route_point(self) -> RoutePoint | None:
        """Return the currently selected route point."""

        if self.route is None:
            return None
        if 0 <= self.selected_route_point_index < len(self.route.points):
            return self.route.points[self.selected_route_point_index]
        return None

    def select_target_by_id(self, target_id: str) -> MeasurementTarget | None:
        """Select a target by identifier and return it."""

        for index, target in enumerate(self.targets):
            if target.id == target_id:
                self.selected_target_index = index
                return target
        return None

    def current_target(self) -> MeasurementTarget | None:
        """Return the currently selected target, if any."""

        if 0 <= self.selected_target_index < len(self.targets):
            return self.targets[self.selected_target_index]
        return None

    def select_next_target(self) -> MeasurementTarget | None:
        """Advance selection to the next target."""

        if not self.targets:
            self.selected_target_index = -1
            return None
        if self.selected_target_index < 0:
            self.selected_target_index = 0
        else:
            self.selected_target_index = min(
                len(self.targets) - 1, self.selected_target_index + 1
            )
        return self.current_target()

    def select_previous_target(self) -> MeasurementTarget | None:
        """Move selection to the previous target."""

        if not self.targets:
            self.selected_target_index = -1
            return None
        if self.selected_target_index < 0:
            self.selected_target_index = 0
        else:
            self.selected_target_index = max(0, self.selected_target_index - 1)
        return self.current_target()

    def design_from_stage(self, stage_xy: Point2D) -> Point2D | None:
        """Project a stage-space point into design-space when registered."""

        if self.registration is None or not self.registration.valid:
            return None
        return self.registration.stage_to_design(stage_xy)

    def stage_from_design(self, design_xy: Point2D) -> Point2D | None:
        """Project a design-space point into stage-space when registered."""

        if self.registration is None or not self.registration.valid:
            return None
        return self.registration.design_to_stage(design_xy)

    def selected_target_stage_xy(self) -> Point2D | None:
        """Resolve the selected target into stage-space coordinates."""

        target = self.current_target()
        if target is None:
            return None
        return self.stage_from_design(target.design_center)

    def _rebuild_registration(self) -> None:
        """Rebuild the active registration when enough data is available."""

        design_marks = self.source_design_marks_compact()
        stage_marks = self.source_stage_marks_compact()
        self.source_design_marks = tuple(design_marks)
        self.source_stage_marks = tuple(stage_marks)
        if len(design_marks) < 2 or len(stage_marks) < 2:
            self.registration = None
            self.registration_status = (
                f"Design marks: {len(design_marks)}/2 | "
                f"Stage marks: {len(stage_marks)}/2"
            )
            return
        if len(design_marks) != len(stage_marks):
            self.registration = None
            self.registration_status = "Source mark capture is incomplete."
            return
        if len(self.check_design_marks) != len(self.check_stage_marks):
            self.registration = None
            self.registration_status = "Check mark capture is incomplete."
            return
        self.registration = DesignRegistration.from_marks(
            design_marks,
            stage_marks,
            check_design_marks=self.check_design_marks,
            check_stage_marks=self.check_stage_marks,
            design_unit_mm=self._design_unit_mm(),
        )
        source_summary = self.registration.source_residual_summary
        check_summary = self.registration.residual_summary
        source_text = (
            f"Fit RMS {source_summary.rms:.4f} mm, "
            f"max {source_summary.max_error:.4f} mm over {source_summary.count} marks."
        )
        if check_summary.count:
            self.registration_status = (
                f"Registered. {source_text} "
                f"Check RMS {check_summary.rms:.4f} mm, "
                f"max {check_summary.max_error:.4f} mm over "
                f"{check_summary.count} marks."
            )
        else:
            self.registration_status = f"Registered. {source_text}"

    def _design_unit_mm(self) -> float:
        """Return the active document's design-unit conversion in millimetres."""

        if self.document is None:
            return 1.0
        return float(self.document.dbu) * 1e3

    @staticmethod
    def _rotate_stage_point(
        point: Point2D, pivot: Point2D, rotation_deg: float
    ) -> Point2D:
        angle = math.radians(rotation_deg)
        dx = float(point[0] - pivot[0])
        dy = float(point[1] - pivot[1])
        cos_a = math.cos(angle)
        sin_a = math.sin(angle)
        return (
            float(pivot[0] + dx * cos_a - dy * sin_a),
            float(pivot[1] + dx * sin_a + dy * cos_a),
        )

    @staticmethod
    def _with_slot_point(
        slots: tuple[Point2D, ...] | list[Optional[Point2D]],
        slot: int,
        point: Point2D,
    ) -> tuple[Point2D, ...]:
        populated = [item for item in slots if item is not None]
        if slot < 0 or slot > len(populated):
            raise DesignModelError("Calibration mark index is not contiguous.")
        normalized = DesignSession._point(point)
        if slot == len(populated):
            populated.append(normalized)
        else:
            populated[slot] = normalized
        return tuple(populated)

    @staticmethod
    def _point(point: Point2D) -> Point2D:
        x_value = float(point[0])
        y_value = float(point[1])
        if not math.isfinite(x_value) or not math.isfinite(y_value):
            raise DesignModelError("Calibration marks must be finite 2D points.")
        return (x_value, y_value)

    @staticmethod
    def _serialize_points(
        points: list[Point2D] | tuple[Point2D, ...],
    ) -> list[list[float]]:
        return [[float(point[0]), float(point[1])] for point in points]

    @staticmethod
    def _serialize_optional_points(
        points: list[Optional[Point2D]],
    ) -> list[list[float] | None]:
        serialized: list[list[float] | None] = []
        for point in points:
            if point is None:
                serialized.append(None)
            else:
                serialized.append([float(point[0]), float(point[1])])
        return serialized

    @classmethod
    def _coerce_optional_points(
        cls,
        value: Any,
        *,
        expected_count: int,
    ) -> list[Optional[Point2D]]:
        points: list[Optional[Point2D]] = []
        if isinstance(value, list):
            for item in value[:expected_count]:
                points.append(cls._coerce_optional_point(item))
        while len(points) < expected_count:
            points.append(None)
        return points

    @classmethod
    def _coerce_points(cls, value: Any) -> list[Point2D]:
        points: list[Point2D] = []
        if not isinstance(value, list):
            return points
        for item in value:
            point = cls._coerce_optional_point(item)
            if point is not None:
                points.append(point)
        return points

    @staticmethod
    def _coerce_optional_point(value: Any) -> Point2D | None:
        if value is None:
            return None
        if not isinstance(value, (list, tuple)) or len(value) != 2:
            return None
        try:
            x_value = float(value[0])
            y_value = float(value[1])
        except (TypeError, ValueError):
            return None
        if not math.isfinite(x_value) or not math.isfinite(y_value):
            return None
        return (x_value, y_value)


__all__ = ["AlignmentPreparation", "DesignSession", "DesignSessionState"]
