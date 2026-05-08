"""State container for optional GDS-backed navigation workflows."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Optional

from .design_model import (
    DesignDocument,
    DesignModelError,
    DesignRegistration,
    LayerKey,
    MeasurementTarget,
    Point2D,
)
from .route_model import MeasurementRoute, RoutePoint


@dataclass(frozen=True)
class AlignmentPreparation:
    """Prepared source-mark alignment ready to be committed after B rotation."""

    design_marks: tuple[Point2D, Point2D]
    stage_marks_before_rotation: tuple[Point2D, Point2D]
    stage_marks_after_rotation: tuple[Point2D, Point2D]
    pivot_stage: Point2D
    rotation_deg: float
    design_distance_mm: float
    stage_distance_mm: float
    distance_ratio: float


@dataclass
class DesignSession:
    """Track the loaded design, registration, and script-generated targets."""

    document: Optional[DesignDocument] = None
    registration: Optional[DesignRegistration] = None
    source_design_marks: list[Optional[Point2D]] = field(
        default_factory=lambda: [None, None]
    )
    source_stage_marks: list[Optional[Point2D]] = field(
        default_factory=lambda: [None, None]
    )
    check_design_marks: list[Point2D] = field(default_factory=list)
    check_stage_marks: list[Point2D] = field(default_factory=list)
    targets: list[MeasurementTarget] = field(default_factory=list)
    selected_target_index: int = -1
    script_path: str | None = None
    script_module_name: str | None = None
    route: MeasurementRoute | None = None
    selected_route_point_index: int = -1
    registration_status: str = "No design registration."

    def export_persisted_state(self) -> dict[str, object] | None:
        """Return design state tied to the current controller coordinate session."""

        if self.document is None:
            return None
        state: dict[str, object] = {
            "version": 1,
            "document_path": str(self.document.path),
            "top_cell_name": self.document.top_cell_name,
            "visible_layers": [
                [int(layer), int(datatype)]
                for layer, datatype in sorted(self.document.visible_layers)
            ],
            "source_design_marks": self._serialize_optional_points(
                self.source_design_marks
            ),
            "source_stage_marks": self._serialize_optional_points(
                self.source_stage_marks
            ),
            "check_design_marks": self._serialize_points(self.check_design_marks),
            "check_stage_marks": self._serialize_points(self.check_stage_marks),
            "registration_valid": bool(
                self.registration is not None and self.registration.valid
            ),
            "registration_status": self.registration_status,
            "registration_stale_reason": (
                self.registration.stale_reason if self.registration is not None else ""
            ),
        }
        try:
            stat = self.document.path.stat()
        except OSError:
            return state
        state["document_mtime_ns"] = int(stat.st_mtime_ns)
        state["document_size"] = int(stat.st_size)
        return state

    def restore_persisted_state(
        self,
        document: DesignDocument,
        state: dict[str, object],
    ) -> None:
        """Restore persisted design state for a verified controller session."""

        self.document = document
        self.clear_targets()
        self.clear_route()
        self.script_path = None
        self.script_module_name = None
        self.source_design_marks = self._coerce_optional_points(
            state.get("source_design_marks"),
            expected_count=2,
        )
        self.source_stage_marks = self._coerce_optional_points(
            state.get("source_stage_marks"),
            expected_count=2,
        )
        self.check_design_marks = self._coerce_points(state.get("check_design_marks"))
        self.check_stage_marks = self._coerce_points(state.get("check_stage_marks"))
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

    def load_document(self, document: DesignDocument) -> None:
        """Attach a new design document and clear derived state."""

        self.document = document
        self.clear_targets()
        self.clear_route()
        self.clear_registration()

    def unload_document(self) -> None:
        """Remove the active design and all related state."""

        self.document = None
        self.clear_targets()
        self.clear_route()
        self.clear_registration()
        self.script_path = None
        self.script_module_name = None

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

    def clear_registration(self) -> None:
        """Drop source marks, check marks, and the active registration."""

        self.source_design_marks = [None, None]
        self.source_stage_marks = [None, None]
        self.check_design_marks.clear()
        self.check_stage_marks.clear()
        self.registration = None
        self.registration_status = "No design registration."

    def clear_source_stage_marks(self) -> None:
        """Drop captured stage-side marks while preserving selected design marks."""

        self.source_stage_marks = [None, None]
        self.check_design_marks.clear()
        self.check_stage_marks.clear()
        self.registration = None
        self.registration_status = self.calibration_prompt()

    def has_complete_source_design_marks(self) -> bool:
        """Return whether both design-side source marks are selected."""

        return all(point is not None for point in self.source_design_marks)

    def capture_source_pair(self, design_point: Point2D, stage_point: Point2D) -> int:
        """Append a matched design/stage pair for simplified calibration."""

        slot = 0
        if self.source_design_marks[0] is not None and self.source_stage_marks[0] is not None:
            slot = 1
        if slot == 1 and self.source_design_marks[1] is not None and self.source_stage_marks[1] is not None:
            self.clear_registration()
            slot = 0
        self.set_source_design_mark(slot, design_point)
        self.set_source_stage_mark(slot, stage_point)
        self.registration = None
        pair_count = self.source_pair_count()
        if pair_count < 2:
            self.registration_status = "Calibration step 2/4: choose the second design mark."
        else:
            self.registration_status = "Two mark pairs captured. Preparing chip rotation."
        return pair_count

    def set_source_design_mark(self, slot: int, point: Point2D) -> None:
        """Set one of the two design-space calibration marks by slot index."""

        self._set_slot_point(self.source_design_marks, slot, point)
        self.registration = None
        self.registration_status = self.calibration_prompt()

    def set_source_stage_mark(self, slot: int, point: Point2D) -> None:
        """Set one of the two stage-space calibration marks by slot index."""

        self._set_slot_point(self.source_stage_marks, slot, point)
        self.registration = None
        self.registration_status = self.calibration_prompt()

    def source_pair_count(self) -> int:
        """Return how many calibration slots contain both design and stage points."""

        return sum(
            1
            for design_point, stage_point in zip(
                self.source_design_marks,
                self.source_stage_marks,
            )
            if design_point is not None and stage_point is not None
        )

    def source_design_marks_compact(self) -> list[Point2D]:
        """Return populated design calibration marks in slot order."""

        return [point for point in self.source_design_marks if point is not None]

    def source_stage_marks_compact(self) -> list[Point2D]:
        """Return populated stage calibration marks in slot order."""

        return [point for point in self.source_stage_marks if point is not None]

    def calibration_prompt(self) -> str:
        """Return a short operator-facing prompt for the next calibration step."""

        if self.source_design_marks[0] is None or self.source_design_marks[1] is None:
            return (
                "Pick mark 1 with left click and mark 2 with right click in the design window."
            )
        if self.source_stage_marks[0] is None:
            return "Center chip mark 1 and capture it."
        if self.source_stage_marks[1] is None:
            return "Center chip mark 2 and capture it."
        if self.registration is not None and self.registration.valid:
            return "Calibration complete. Use the minimap or click in the design window to navigate."
        return "Two mark pairs captured. Waiting for chip rotation to finish."

    def prepare_source_alignment(self) -> AlignmentPreparation:
        """Prepare B-axis rotation and rotated stage marks from two captured pairs."""

        if self.document is None:
            raise DesignModelError("No design document is loaded.")
        if any(point is None for point in self.source_design_marks) or any(
            point is None for point in self.source_stage_marks
        ):
            raise DesignModelError("Exactly two mark pairs are required for calibration.")

        design_a = self.source_design_marks[0]
        design_b = self.source_design_marks[1]
        stage_a = self.source_stage_marks[0]
        stage_b = self.source_stage_marks[1]
        assert design_a is not None and design_b is not None
        assert stage_a is not None and stage_b is not None
        design_dx = float(design_b[0] - design_a[0])
        design_dy = float(design_b[1] - design_a[1])
        stage_dx = float(stage_b[0] - stage_a[0])
        stage_dy = float(stage_b[1] - stage_a[1])
        design_distance_units = math.hypot(design_dx, design_dy)
        stage_distance_mm = math.hypot(stage_dx, stage_dy)
        design_distance_mm = design_distance_units * float(self.document.dbu) * 1e3
        if design_distance_mm <= 1e-9 or stage_distance_mm <= 1e-9:
            raise DesignModelError("Calibration marks are too close together.")

        design_angle = math.atan2(design_dy, design_dx)
        stage_angle = math.atan2(stage_dy, stage_dx)
        rotation_deg = math.degrees(design_angle - stage_angle)
        while rotation_deg <= -180.0:
            rotation_deg += 360.0
        while rotation_deg > 180.0:
            rotation_deg -= 360.0

        pivot_stage = stage_b
        adjusted_stage_a = self._rotate_stage_point(stage_a, pivot_stage, rotation_deg)
        distance_ratio = stage_distance_mm / design_distance_mm
        return AlignmentPreparation(
            design_marks=(design_a, design_b),
            stage_marks_before_rotation=(stage_a, stage_b),
            stage_marks_after_rotation=(adjusted_stage_a, stage_b),
            pivot_stage=pivot_stage,
            rotation_deg=rotation_deg,
            design_distance_mm=design_distance_mm,
            stage_distance_mm=stage_distance_mm,
            distance_ratio=distance_ratio,
        )

    def apply_prepared_alignment(self, preparation: AlignmentPreparation) -> None:
        """Commit a prepared alignment after the B-axis rotation succeeds."""

        self.source_design_marks = list(preparation.design_marks)
        self.source_stage_marks = list(preparation.stage_marks_after_rotation)
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
        """Append a design-space source mark, replacing oldest overflow."""

        slot = 0 if self.source_design_marks[0] is None else 1
        if slot == 1 and self.source_design_marks[1] is not None:
            self.clear_registration()
            slot = 0
        self.set_source_design_mark(slot, point)
        self._rebuild_registration()

    def add_source_stage_mark(self, point: Point2D) -> None:
        """Append a stage-space source mark, replacing oldest overflow."""

        slot = 0 if self.source_stage_marks[0] is None else 1
        if slot == 1 and self.source_stage_marks[1] is not None:
            self.clear_registration()
            slot = 0
        self.set_source_stage_mark(slot, point)
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
        """Remove the current measurement plan."""

        self.targets.clear()
        self.selected_target_index = -1

    def set_targets(self, targets: list[MeasurementTarget]) -> None:
        """Replace the measurement plan with a new ordered target list."""

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
        )
        summary = self.registration.residual_summary
        if summary.count:
            self.registration_status = (
                f"Registered. Check RMS {summary.rms:.4f} mm, "
                f"max {summary.max_error:.4f} mm over {summary.count} marks."
            )
        else:
            self.registration_status = "Registered from 2 source marks."

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
    def _set_slot_point(
        slots: list[Optional[Point2D]], slot: int, point: Point2D
    ) -> None:
        if slot not in (0, 1):
            raise DesignModelError("Calibration slot must be 0 or 1.")
        slots[slot] = (float(point[0]), float(point[1]))

    @staticmethod
    def _serialize_points(points: list[Point2D]) -> list[list[float]]:
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


__all__ = ["AlignmentPreparation", "DesignSession"]
