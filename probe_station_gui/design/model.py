"""Design document loading and registration helpers for GDS-backed navigation."""

from __future__ import annotations

import importlib
import math
import uuid
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import MappingProxyType
from typing import Any, ClassVar, Iterable, Mapping, Optional


class _LazyModule:
    def __init__(self, module_name: str) -> None:
        self._module_name = module_name
        self._module: Any | None = None

    def __getattr__(self, name: str) -> Any:
        if self._module is None:
            self._module = importlib.import_module(self._module_name)
        return getattr(self._module, name)


np = _LazyModule("numpy")


LayerKey = tuple[int, int]
Point2D = tuple[float, float]


class DesignModelError(RuntimeError):
    """Raised when a design document or transform cannot be prepared."""


@dataclass(frozen=True)
class MeasurementTarget:
    """A design-space target for optional navigation overlays."""

    id: str
    label: str
    design_center: Point2D
    metadata: dict[str, Any] = field(default_factory=dict)
    group: str | None = None

    @classmethod
    def from_object(cls, value: Any) -> "MeasurementTarget":
        """Coerce a mapping-like object into a measurement target."""

        if isinstance(value, cls):
            return value
        if isinstance(value, dict):
            target_id = str(value.get("id", "")).strip()
            label = str(value.get("label", target_id)).strip()
            center = value.get("design_center")
            group = value.get("group")
            metadata = value.get("metadata", {})
        else:
            target_id = str(getattr(value, "id", "")).strip()
            label = str(getattr(value, "label", target_id)).strip()
            center = getattr(value, "design_center", None)
            group = getattr(value, "group", None)
            metadata = getattr(value, "metadata", {})
        if not target_id:
            raise DesignModelError("Measurement target is missing a non-empty 'id'.")
        if center is None or len(center) != 2:
            raise DesignModelError(
                f"Measurement target '{target_id}' is missing a 2D 'design_center'."
            )
        try:
            x = float(center[0])
            y = float(center[1])
        except (TypeError, ValueError, IndexError) as exc:
            raise DesignModelError(
                f"Measurement target '{target_id}' has an invalid 'design_center'."
            ) from exc
        metadata_dict = dict(metadata) if isinstance(metadata, dict) else {}
        return cls(
            id=target_id,
            label=label or target_id,
            design_center=(x, y),
            metadata=metadata_dict,
            group=str(group).strip() or None if group is not None else None,
        )


@dataclass(frozen=True)
class ResidualSummary:
    """Residual error statistics for registration check marks."""

    count: int = 0
    rms: float = 0.0
    max_error: float = 0.0


@dataclass(frozen=True)
class SnapResult:
    """Nearest visible snap target for a design-space point."""

    point: Point2D
    mode: str
    distance: float
    segment_start: Point2D | None = None
    segment_end: Point2D | None = None


@dataclass(frozen=True)
class DesignRegistration:
    """Similarity transform between design coordinates and stage coordinates."""

    source_design_marks: tuple[Point2D, ...]
    source_stage_marks: tuple[Point2D, ...]
    check_design_marks: tuple[Point2D, ...] = ()
    check_stage_marks: tuple[Point2D, ...] = ()
    matrix: np.ndarray = field(default_factory=lambda: np.eye(2, dtype=float))
    offset: np.ndarray = field(default_factory=lambda: np.zeros(2, dtype=float))
    residuals: tuple[float, ...] = ()
    residual_summary: ResidualSummary = field(default_factory=ResidualSummary)
    valid: bool = False
    stale_reason: str = ""

    @classmethod
    def empty(cls) -> "DesignRegistration":
        """Return an invalid placeholder registration."""

        return cls(
            source_design_marks=(),
            source_stage_marks=(),
            valid=False,
            stale_reason="Registration not built.",
        )

    @classmethod
    def from_marks(
        cls,
        source_design_marks: Iterable[Point2D],
        source_stage_marks: Iterable[Point2D],
        *,
        check_design_marks: Iterable[Point2D] = (),
        check_stage_marks: Iterable[Point2D] = (),
    ) -> "DesignRegistration":
        """Build a similarity transform from design and stage mark pairs."""

        design_marks = tuple((float(x), float(y)) for x, y in source_design_marks)
        stage_marks = tuple((float(x), float(y)) for x, y in source_stage_marks)
        check_design = tuple((float(x), float(y)) for x, y in check_design_marks)
        check_stage = tuple((float(x), float(y)) for x, y in check_stage_marks)
        if len(design_marks) < 2 or len(stage_marks) < 2:
            raise DesignModelError("At least two source mark pairs are required.")
        if len(design_marks) != len(stage_marks):
            raise DesignModelError("Design and stage source mark counts must match.")
        if len(check_design) != len(check_stage):
            raise DesignModelError("Design and stage check mark counts must match.")

        d1 = np.asarray(design_marks[0], dtype=float)
        d2 = np.asarray(design_marks[1], dtype=float)
        s1 = np.asarray(stage_marks[0], dtype=float)
        s2 = np.asarray(stage_marks[1], dtype=float)
        design_vector = d2 - d1
        stage_vector = s2 - s1
        design_norm = float(np.linalg.norm(design_vector))
        stage_norm = float(np.linalg.norm(stage_vector))
        if design_norm <= 1e-9 or stage_norm <= 1e-9:
            raise DesignModelError("Registration marks are too close together.")

        scale = stage_norm / design_norm
        design_angle = math.atan2(float(design_vector[1]), float(design_vector[0]))
        stage_angle = math.atan2(float(stage_vector[1]), float(stage_vector[0]))
        theta = stage_angle - design_angle
        rotation = np.array(
            [
                [math.cos(theta), -math.sin(theta)],
                [math.sin(theta), math.cos(theta)],
            ],
            dtype=float,
        )
        matrix = scale * rotation
        offset = s1 - matrix @ d1

        residuals: list[float] = []
        for design_point, stage_point in zip(check_design, check_stage):
            predicted = matrix @ np.asarray(design_point, dtype=float) + offset
            actual = np.asarray(stage_point, dtype=float)
            residuals.append(float(np.linalg.norm(actual - predicted)))
        if residuals:
            residual_array = np.asarray(residuals, dtype=float)
            summary = ResidualSummary(
                count=len(residuals),
                rms=float(np.sqrt(np.mean(np.square(residual_array)))),
                max_error=float(np.max(residual_array)),
            )
        else:
            summary = ResidualSummary()

        return cls(
            source_design_marks=design_marks,
            source_stage_marks=stage_marks,
            check_design_marks=check_design,
            check_stage_marks=check_stage,
            matrix=matrix,
            offset=offset,
            residuals=tuple(residuals),
            residual_summary=summary,
            valid=True,
        )

    def design_to_stage(self, point: Point2D) -> Point2D:
        """Transform a design-space point into stage-space coordinates."""

        if not self.valid:
            raise DesignModelError(self.stale_reason or "Registration is not valid.")
        vec = np.asarray(point, dtype=float)
        result = self.matrix @ vec + self.offset
        return (float(result[0]), float(result[1]))

    def stage_to_design(self, point: Point2D) -> Point2D:
        """Transform a stage-space point back into design coordinates."""

        if not self.valid:
            raise DesignModelError(self.stale_reason or "Registration is not valid.")
        inverse = np.linalg.inv(self.matrix)
        vec = np.asarray(point, dtype=float) - self.offset
        result = inverse @ vec
        return (float(result[0]), float(result[1]))

    def mark_stale(self, reason: str) -> "DesignRegistration":
        """Return a stale copy retaining the previous transform for diagnostics."""

        return replace(self, valid=False, stale_reason=reason)


@dataclass(frozen=True)
class DesignDocument:
    """Loaded GDS design and display-ready polygon geometry."""

    SNAP_VERTEX_PRIORITY_RATIO: ClassVar[float] = 1.8
    SNAP_GRID_DIVISIONS: ClassVar[int] = 256
    SNAP_GRID_MAX_SEGMENT_CELLS: ClassVar[int] = 64

    path: Path
    library: Any
    top_cell: Any
    top_cell_name: str
    cell_names: tuple[str, ...]
    dbu: float
    user_unit: float
    bounds: tuple[float, float, float, float]
    polygons_by_layer: dict[LayerKey, tuple[np.ndarray, ...]]
    visible_layers: frozenset[LayerKey]
    plot_paths_by_layer: dict[LayerKey, tuple[np.ndarray, np.ndarray]] = field(
        default_factory=dict,
        repr=False,
    )
    snap_vertices: np.ndarray = field(
        default_factory=lambda: np.empty((0, 2), dtype=float),
        repr=False,
    )
    snap_segment_starts: np.ndarray = field(
        default_factory=lambda: np.empty((0, 2), dtype=float),
        repr=False,
    )
    snap_segment_ends: np.ndarray = field(
        default_factory=lambda: np.empty((0, 2), dtype=float),
        repr=False,
    )
    snap_grid_cell_size: float = field(default=0.0, repr=False)
    snap_vertex_bins: dict[tuple[int, int], tuple[int, ...]] = field(
        default_factory=dict,
        repr=False,
    )
    snap_segment_bins: dict[tuple[int, int], tuple[int, ...]] = field(
        default_factory=dict,
        repr=False,
    )
    snap_long_segment_indices: tuple[int, ...] = field(default_factory=tuple, repr=False)
    snap_geometry_built: bool = field(default=False, repr=False)
    rotation_quarter_turns: int = 0
    file_backed: bool = False
    available_layers: frozenset[LayerKey] = field(default_factory=frozenset)
    cell_bounds: Mapping[str, tuple[float, float, float, float]] = field(
        default_factory=lambda: MappingProxyType({})
    )
    source_load_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "rotation_quarter_turns",
            int(self.rotation_quarter_turns) % 4,
        )
        object.__setattr__(
            self,
            "available_layers",
            frozenset(self.available_layers or self.polygons_by_layer),
        )
        object.__setattr__(
            self,
            "cell_bounds",
            MappingProxyType(
                {
                    str(name): tuple(float(value) for value in bounds)
                    for name, bounds in self.cell_bounds.items()
                }
            ),
        )
        if not self.plot_paths_by_layer and self.polygons_by_layer:
            object.__setattr__(
                self,
                "plot_paths_by_layer",
                self._build_plot_paths(self.polygons_by_layer, self.visible_layers),
            )
        if (
            not self.snap_geometry_built
            and (len(self.snap_vertices) > 0 or len(self.snap_segment_starts) > 0)
        ):
            object.__setattr__(self, "snap_geometry_built", True)

    @classmethod
    def load(cls, path: str | Path) -> "DesignDocument":
        """Read lightweight metadata from a GDS/OASIS file using KLayout."""

        resolved = Path(path).expanduser().resolve()
        try:
            import klayout.db as db
        except ImportError as exc:
            raise DesignModelError(
                "KLayout is not installed. Install it to enable GDS design navigation."
            ) from exc

        layout = db.Layout()
        try:
            layout.read(str(resolved))
        except Exception as exc:  # pragma: no cover - KLayout specific
            raise DesignModelError(f"Failed to load design '{resolved}': {exc}") from exc

        cells = tuple(layout.each_cell())
        if not cells:
            raise DesignModelError(f"Design '{resolved}' does not contain any cells.")
        top_level = tuple(layout.top_cells())
        top_cell = top_level[0] if top_level else cells[0]
        cell_names = tuple(
            sorted(str(cell.name) for cell in cells if str(cell.name))
        )
        if not cell_names:
            raise DesignModelError(f"Design '{resolved}' does not expose named cells.")
        cell_bounds = {
            str(cell.name): cls._klayout_bounds(cell.bbox(), layout.dbu)
            for cell in cells
            if str(cell.name) and not cell.bbox().empty()
        }
        top_cell_name = str(top_cell.name or cell_names[0])
        if top_cell_name not in cell_bounds:
            geometry_cell_name = next(
                (
                    str(cell.name)
                    for group in (top_level or cells, cells)
                    for cell in group
                    if str(cell.name) in cell_bounds
                ),
                None,
            )
            if geometry_cell_name is not None:
                top_cell_name = geometry_cell_name
        if top_cell_name not in cell_bounds:
            raise DesignModelError(f"Top cell '{top_cell_name}' has no polygon geometry.")

        available_layers = frozenset(
            (int(info.layer), int(info.datatype)) for info in layout.layer_infos()
        )
        bounds = cell_bounds[top_cell_name]
        user_unit = float(layout.dbu) * 1e-6
        del cells, top_level, top_cell, layout
        return cls(
            path=resolved,
            library=None,
            top_cell=None,
            top_cell_name=top_cell_name,
            cell_names=cell_names,
            dbu=1e-6,
            user_unit=user_unit,
            bounds=bounds,
            polygons_by_layer={},
            visible_layers=available_layers,
            file_backed=True,
            available_layers=available_layers,
            cell_bounds=cell_bounds,
            source_load_id=uuid.uuid4().hex,
        )

    @classmethod
    def _from_components(
        cls,
        *,
        path: Path,
        library: Any,
        top_cell_name: str,
        visible_layers: Optional[Iterable[LayerKey]] = None,
        rotation_quarter_turns: int = 0,
    ) -> "DesignDocument":
        rotation_quarter_turns = int(rotation_quarter_turns) % 4
        cells = tuple(getattr(library, "cells", ()))
        cell_by_name = {
            str(getattr(cell, "name", "")): cell for cell in cells if getattr(cell, "name", "")
        }
        if top_cell_name not in cell_by_name:
            raise DesignModelError(f"Cell '{top_cell_name}' was not found in '{path.name}'.")
        top_cell = cell_by_name[top_cell_name]
        polygons_by_layer = cls._extract_fixture_polygons(top_cell)
        if not polygons_by_layer:
            raise DesignModelError(f"Top cell '{top_cell_name}' has no polygon geometry.")
        if rotation_quarter_turns:
            raw_bounds = cls._calculate_bounds(polygons_by_layer)
            polygons_by_layer = cls._rotate_polygons_by_layer(
                polygons_by_layer,
                raw_bounds,
                rotation_quarter_turns,
            )
        bounds = cls._calculate_bounds(polygons_by_layer)
        layers = frozenset(polygons_by_layer.keys())
        if visible_layers is None:
            effective_layers = layers
        else:
            effective_layers = frozenset(layer for layer in visible_layers if layer in layers)
            if not effective_layers:
                effective_layers = layers
        dbu = float(getattr(library, "unit", 1e-6))
        precision = float(getattr(library, "precision", dbu))
        user_unit = precision if precision > 0 else dbu
        return cls(
            path=path,
            library=library,
            top_cell=top_cell,
            top_cell_name=top_cell_name,
            cell_names=tuple(sorted(str(name) for name in cell_by_name.keys())),
            dbu=dbu,
            user_unit=user_unit,
            bounds=bounds,
            polygons_by_layer=polygons_by_layer,
            visible_layers=effective_layers,
            plot_paths_by_layer=cls._build_plot_paths(polygons_by_layer, effective_layers),
            rotation_quarter_turns=rotation_quarter_turns,
        )

    def with_top_cell(self, top_cell_name: str) -> "DesignDocument":
        """Return a copy using a different top cell from the same library."""

        if self.file_backed:
            if top_cell_name not in self.cell_names:
                raise DesignModelError(
                    f"Cell '{top_cell_name}' was not found in '{self.path.name}'."
                )
            if top_cell_name not in self.cell_bounds:
                raise DesignModelError(
                    f"Top cell '{top_cell_name}' has no polygon geometry."
                )
            return replace(
                self,
                top_cell_name=top_cell_name,
                bounds=self._rotate_bounds(
                    self.cell_bounds[top_cell_name],
                    self.rotation_quarter_turns,
                ),
            )
        return self._from_components(
            path=self.path,
            library=self.library,
            top_cell_name=top_cell_name,
            visible_layers=self.visible_layers,
            rotation_quarter_turns=self.rotation_quarter_turns,
        )

    def with_visible_layers(self, layers: Iterable[LayerKey]) -> "DesignDocument":
        """Return a copy with a different visible layer subset."""

        if self.file_backed:
            effective_layers = frozenset(
                layer for layer in layers if layer in self.available_layers
            )
            if not effective_layers:
                effective_layers = self.available_layers
            return replace(self, visible_layers=effective_layers)
        return self._from_components(
            path=self.path,
            library=self.library,
            top_cell_name=self.top_cell_name,
            visible_layers=frozenset(
                layer for layer in layers if layer in self.polygons_by_layer
            ),
            rotation_quarter_turns=self.rotation_quarter_turns,
        )

    def with_rotation_delta(self, quarter_turn_delta: int) -> "DesignDocument":
        """Return a copy rotated by 90-degree steps around the design bounds center."""

        delta = int(quarter_turn_delta) % 4
        if delta == 0:
            return self
        if self.file_backed:
            turns = (self.rotation_quarter_turns + delta) % 4
            return replace(
                self,
                bounds=self._rotate_bounds(
                    self.cell_bounds[self.top_cell_name],
                    turns,
                ),
                rotation_quarter_turns=turns,
            )
        polygons_by_layer = self._rotate_polygons_by_layer(
            self.polygons_by_layer,
            self.bounds,
            delta,
        )
        bounds = self._calculate_bounds(polygons_by_layer)
        empty_points = np.empty((0, 2), dtype=float)
        return replace(
            self,
            bounds=bounds,
            polygons_by_layer=polygons_by_layer,
            plot_paths_by_layer=self._build_plot_paths(
                polygons_by_layer,
                self.visible_layers,
            ),
            snap_vertices=empty_points,
            snap_segment_starts=empty_points,
            snap_segment_ends=empty_points,
            snap_grid_cell_size=0.0,
            snap_vertex_bins={},
            snap_segment_bins={},
            snap_long_segment_indices=(),
            snap_geometry_built=False,
            rotation_quarter_turns=self.rotation_quarter_turns + delta,
        )

    def rotate_point(self, point: Point2D, quarter_turn_delta: int) -> Point2D:
        """Rotate a point from this document orientation by 90-degree steps."""

        left, bottom, right, top = self.bounds
        center = ((left + right) * 0.5, (bottom + top) * 0.5)
        return self._rotate_point_around_center(point, center, quarter_turn_delta)

    @staticmethod
    def rotate_vector(vector: Point2D, quarter_turn_delta: int) -> Point2D:
        """Rotate a vector by 90-degree steps without applying translation."""

        x_value = float(vector[0])
        y_value = float(vector[1])
        turns = int(quarter_turn_delta) % 4
        if turns == 1:
            return (-y_value, x_value)
        if turns == 2:
            return (-x_value, -y_value)
        if turns == 3:
            return (y_value, -x_value)
        return (x_value, y_value)

    def layer_keys(self) -> tuple[LayerKey, ...]:
        """Return layer keys in display order."""

        if self.file_backed:
            return tuple(sorted(self.available_layers))
        return tuple(sorted(self.polygons_by_layer.keys()))

    def visible_polygons(self) -> dict[LayerKey, tuple[np.ndarray, ...]]:
        """Return polygon geometry for currently visible layers."""

        return {
            layer: polygons
            for layer, polygons in self.polygons_by_layer.items()
            if layer in self.visible_layers
        }

    def visible_plot_paths(self) -> dict[LayerKey, tuple[np.ndarray, np.ndarray]]:
        """Return display-ready path arrays for currently visible layers."""

        return {
            layer: paths
            for layer, paths in self.plot_paths_by_layer.items()
            if layer in self.visible_layers
        }

    def snap_point(self, point: Point2D) -> Point2D:
        """Snap a design-space point to the nearest visible vertex or segment."""

        return self.snap_point_info(point).point

    def snap_point_info(
        self,
        point: Point2D,
        *,
        max_distance: float | None = None,
    ) -> SnapResult:
        """Return the nearest visible snap target and its geometry metadata."""

        self._ensure_snap_geometry()
        target = np.asarray(point, dtype=float)
        vertex_point = target
        vertex_distance_sq = float("inf")
        segment_point = target
        segment_distance_sq = float("inf")
        segment_start: Point2D | None = None
        segment_end: Point2D | None = None
        midpoint_point = target
        midpoint_distance_sq = float("inf")
        midpoint_start: Point2D | None = None
        midpoint_end: Point2D | None = None

        vertex_indices, segment_indices = self._snap_candidate_indices(
            target,
            max_distance=max_distance,
        )

        if len(vertex_indices):
            candidate_vertices = self.snap_vertices[vertex_indices]
            vertex_delta = candidate_vertices - target
            vertex_distance_sq_array = np.einsum("ij,ij->i", vertex_delta, vertex_delta)
            vertex_index = int(np.argmin(vertex_distance_sq_array))
            vertex_distance_sq = float(vertex_distance_sq_array[vertex_index])
            vertex_point = np.asarray(candidate_vertices[vertex_index], dtype=float)

        if len(segment_indices):
            candidate_starts = self.snap_segment_starts[segment_indices]
            candidate_ends = self.snap_segment_ends[segment_indices]
            segments = candidate_ends - candidate_starts
            lengths_sq = np.einsum("ij,ij->i", segments, segments)
            valid_lengths = np.maximum(lengths_sq, 1e-18)
            projections = np.einsum(
                "ij,ij->i",
                np.broadcast_to(target, candidate_starts.shape) - candidate_starts,
                segments,
            ) / valid_lengths
            projections = np.clip(projections, 0.0, 1.0)
            snapped_points = candidate_starts + segments * projections[:, np.newaxis]
            segment_delta = snapped_points - target
            segment_distance_sq_array = np.einsum("ij,ij->i", segment_delta, segment_delta)
            segment_index = int(np.argmin(segment_distance_sq_array))
            segment_distance_sq = float(segment_distance_sq_array[segment_index])
            segment_point = np.asarray(snapped_points[segment_index], dtype=float)
            start = candidate_starts[segment_index]
            end = candidate_ends[segment_index]
            segment_start = (float(start[0]), float(start[1]))
            segment_end = (float(end[0]), float(end[1]))

            midpoints = (candidate_starts + candidate_ends) * 0.5
            midpoint_delta = midpoints - target
            midpoint_distance_sq_array = np.einsum(
                "ij,ij->i",
                midpoint_delta,
                midpoint_delta,
            )
            midpoint_index = int(np.argmin(midpoint_distance_sq_array))
            midpoint_distance_sq = float(midpoint_distance_sq_array[midpoint_index])
            midpoint_point = np.asarray(midpoints[midpoint_index], dtype=float)
            midpoint_segment_start = candidate_starts[midpoint_index]
            midpoint_segment_end = candidate_ends[midpoint_index]
            midpoint_start = (
                float(midpoint_segment_start[0]),
                float(midpoint_segment_start[1]),
            )
            midpoint_end = (
                float(midpoint_segment_end[0]),
                float(midpoint_segment_end[1]),
            )

        point_mode = "vertex"
        point = vertex_point
        point_distance_sq = vertex_distance_sq
        point_segment_start: Point2D | None = None
        point_segment_end: Point2D | None = None
        if midpoint_distance_sq < point_distance_sq:
            point_mode = "segment_center"
            point = midpoint_point
            point_distance_sq = midpoint_distance_sq
            point_segment_start = midpoint_start
            point_segment_end = midpoint_end
        midpoint_within_threshold = (
            max_distance is not None
            and midpoint_distance_sq <= float(max_distance) * float(max_distance)
        )
        if point_mode == "segment_center" and midpoint_within_threshold:
            return SnapResult(
                point=(float(point[0]), float(point[1])),
                mode=point_mode,
                distance=math.sqrt(max(0.0, point_distance_sq)),
                segment_start=point_segment_start,
                segment_end=point_segment_end,
            )

        if (
            vertex_distance_sq < float("inf")
            and (
                segment_distance_sq == float("inf")
                or vertex_distance_sq
                <= segment_distance_sq * (self.SNAP_VERTEX_PRIORITY_RATIO ** 2)
            )
        ):
            return SnapResult(
                point=(float(vertex_point[0]), float(vertex_point[1])),
                mode="vertex",
                distance=math.sqrt(max(0.0, vertex_distance_sq)),
            )

        if segment_distance_sq < float("inf"):
            return SnapResult(
                point=(float(segment_point[0]), float(segment_point[1])),
                mode="segment",
                distance=math.sqrt(max(0.0, segment_distance_sq)),
                segment_start=segment_start,
                segment_end=segment_end,
            )

        return SnapResult(
            point=(float(target[0]), float(target[1])),
            mode="free",
            distance=0.0,
        )

    def _ensure_snap_geometry(self) -> None:
        if self.snap_geometry_built:
            return
        (
            snap_vertices,
            snap_segment_starts,
            snap_segment_ends,
            snap_grid_cell_size,
            snap_vertex_bins,
            snap_segment_bins,
            snap_long_segment_indices,
        ) = self._build_snap_geometry(
            self.polygons_by_layer,
            self.visible_layers,
            self.bounds,
        )
        self.set_snap_geometry(
            snap_vertices,
            snap_segment_starts,
            snap_segment_ends,
            snap_grid_cell_size,
            snap_vertex_bins,
            snap_segment_bins,
            snap_long_segment_indices,
        )

    def has_snap_geometry(self) -> bool:
        """Return whether snap geometry is available without doing work."""

        return bool(self.snap_geometry_built)

    def build_snap_geometry(
        self,
    ) -> tuple[
        np.ndarray,
        np.ndarray,
        np.ndarray,
        float,
        dict[tuple[int, int], tuple[int, ...]],
        dict[tuple[int, int], tuple[int, ...]],
        tuple[int, ...],
    ]:
        """Build snap arrays for the visible layers."""

        return self._build_snap_geometry(
            self.polygons_by_layer,
            self.visible_layers,
            self.bounds,
        )

    def set_snap_geometry(
        self,
        snap_vertices: np.ndarray,
        snap_segment_starts: np.ndarray,
        snap_segment_ends: np.ndarray,
        snap_grid_cell_size: float | None = None,
        snap_vertex_bins: dict[tuple[int, int], tuple[int, ...]] | None = None,
        snap_segment_bins: dict[tuple[int, int], tuple[int, ...]] | None = None,
        snap_long_segment_indices: tuple[int, ...] | None = None,
    ) -> None:
        """Install precomputed snap arrays on this immutable document object."""

        object.__setattr__(self, "snap_vertices", snap_vertices)
        object.__setattr__(self, "snap_segment_starts", snap_segment_starts)
        object.__setattr__(self, "snap_segment_ends", snap_segment_ends)
        if snap_grid_cell_size is None or snap_vertex_bins is None or snap_segment_bins is None:
            (
                snap_grid_cell_size,
                snap_vertex_bins,
                snap_segment_bins,
                snap_long_segment_indices,
            ) = self._build_snap_spatial_index(
                snap_vertices,
                snap_segment_starts,
                snap_segment_ends,
                self.bounds,
            )
        object.__setattr__(self, "snap_grid_cell_size", float(snap_grid_cell_size))
        object.__setattr__(self, "snap_vertex_bins", snap_vertex_bins)
        object.__setattr__(self, "snap_segment_bins", snap_segment_bins)
        object.__setattr__(
            self,
            "snap_long_segment_indices",
            tuple(snap_long_segment_indices or ()),
        )
        object.__setattr__(self, "snap_geometry_built", True)

    def _snap_candidate_indices(
        self,
        target: np.ndarray,
        *,
        max_distance: float | None,
    ) -> tuple[np.ndarray, np.ndarray]:
        if (
            max_distance is None
            or not math.isfinite(float(max_distance))
            or max_distance <= 0.0
            or self.snap_grid_cell_size <= 0.0
        ):
            return (
                np.arange(len(self.snap_vertices), dtype=np.intp),
                np.arange(len(self.snap_segment_starts), dtype=np.intp),
            )

        radius = float(max_distance)
        vertex_radius = radius * self.SNAP_VERTEX_PRIORITY_RATIO
        vertex_indices = self._indices_from_bins(
            self.snap_vertex_bins,
            target,
            vertex_radius,
            self.snap_grid_cell_size,
        )
        segment_indices = self._indices_from_bins(
            self.snap_segment_bins,
            target,
            radius,
            self.snap_grid_cell_size,
            extra_indices=self.snap_long_segment_indices,
        )
        return vertex_indices, segment_indices

    def dbu_to_um(self, value: float) -> float:
        """Convert design database units to micrometers."""

        return float(value) * float(self.dbu) * 1e6

    def um_to_dbu(self, value: float) -> float:
        """Convert micrometers to design database units."""

        dbu_um = float(self.dbu) * 1e6
        if dbu_um == 0:
            raise DesignModelError("Document DBU is zero.")
        return float(value) / dbu_um

    def bounds_um(self) -> tuple[float, float, float, float]:
        """Return document bounds in micrometers."""

        left, bottom, right, top = self.bounds
        return (
            self.dbu_to_um(left),
            self.dbu_to_um(bottom),
            self.dbu_to_um(right),
            self.dbu_to_um(top),
        )

    @staticmethod
    def _extract_fixture_polygons(
        cell: Any,
    ) -> dict[LayerKey, tuple[np.ndarray, ...]]:
        """Read polygons from legacy in-memory fixtures without a GDS dependency."""

        polygons_by_layer: dict[LayerKey, list[np.ndarray]] = {}
        polygon_map: Any = None
        if hasattr(cell, "get_polygons"):
            try:
                polygon_map = cell.get_polygons(apply_repetitions=True, by_spec=True)
            except TypeError:
                try:
                    polygon_map = cell.get_polygons(by_spec=True)
                except Exception:
                    polygon_map = None
            except Exception:
                polygon_map = None
        if isinstance(polygon_map, dict):
            for spec, polygons in polygon_map.items():
                if not isinstance(spec, tuple) or len(spec) < 2:
                    continue
                layer = (int(spec[0]), int(spec[1]))
                for polygon in polygons:
                    points = np.asarray(polygon, dtype=float)
                    if points.ndim != 2 or points.shape[1] != 2 or len(points) < 2:
                        continue
                    polygons_by_layer.setdefault(layer, []).append(points)
        if polygons_by_layer:
            return {
                layer: tuple(polygons) for layer, polygons in sorted(polygons_by_layer.items())
            }

        raw_polygons = getattr(cell, "polygons", ())
        for polygon in raw_polygons:
            layer = int(getattr(polygon, "layer", 0))
            datatype = int(getattr(polygon, "datatype", 0))
            points = np.asarray(getattr(polygon, "points", ()), dtype=float)
            if points.ndim != 2 or points.shape[1] != 2 or len(points) < 2:
                continue
            polygons_by_layer.setdefault((layer, datatype), []).append(points)
        return {
            layer: tuple(polygons) for layer, polygons in sorted(polygons_by_layer.items())
        }

    @staticmethod
    def _calculate_bounds(
        polygons_by_layer: dict[LayerKey, tuple[np.ndarray, ...]]
    ) -> tuple[float, float, float, float]:
        mins_x: list[float] = []
        mins_y: list[float] = []
        maxs_x: list[float] = []
        maxs_y: list[float] = []
        for polygons in polygons_by_layer.values():
            for polygon in polygons:
                mins_x.append(float(np.min(polygon[:, 0])))
                mins_y.append(float(np.min(polygon[:, 1])))
                maxs_x.append(float(np.max(polygon[:, 0])))
                maxs_y.append(float(np.max(polygon[:, 1])))
        if not mins_x:
            raise DesignModelError("Unable to determine document bounds.")
        return (min(mins_x), min(mins_y), max(maxs_x), max(maxs_y))

    @staticmethod
    def _klayout_bounds(
        box: Any,
        dbu: float,
    ) -> tuple[float, float, float, float]:
        design_box = box.to_dtype(float(dbu))
        return (
            float(design_box.left),
            float(design_box.bottom),
            float(design_box.right),
            float(design_box.top),
        )

    @staticmethod
    def _rotate_bounds(
        bounds: tuple[float, float, float, float],
        quarter_turns: int,
    ) -> tuple[float, float, float, float]:
        left, bottom, right, top = bounds
        if int(quarter_turns) % 2 == 0:
            return (left, bottom, right, top)
        center_x = (left + right) * 0.5
        center_y = (bottom + top) * 0.5
        half_width = (top - bottom) * 0.5
        half_height = (right - left) * 0.5
        return (
            center_x - half_width,
            center_y - half_height,
            center_x + half_width,
            center_y + half_height,
        )

    @staticmethod
    def _rotate_polygons_by_layer(
        polygons_by_layer: dict[LayerKey, tuple[np.ndarray, ...]],
        bounds: tuple[float, float, float, float],
        quarter_turn_delta: int,
    ) -> dict[LayerKey, tuple[np.ndarray, ...]]:
        left, bottom, right, top = bounds
        center = ((left + right) * 0.5, (bottom + top) * 0.5)
        return {
            layer_key: tuple(
                DesignDocument._rotate_points_around_center(
                    polygon,
                    center,
                    quarter_turn_delta,
                )
                for polygon in polygons
            )
            for layer_key, polygons in polygons_by_layer.items()
        }

    @staticmethod
    def _rotate_points_around_center(
        points: np.ndarray,
        center: Point2D,
        quarter_turn_delta: int,
    ) -> np.ndarray:
        turns = int(quarter_turn_delta) % 4
        if turns == 0:
            return np.asarray(points, dtype=float).copy()
        values = np.asarray(points, dtype=float)
        cx, cy = float(center[0]), float(center[1])
        dx = values[:, 0] - cx
        dy = values[:, 1] - cy
        rotated = np.empty_like(values, dtype=float)
        if turns == 1:
            rotated[:, 0] = cx - dy
            rotated[:, 1] = cy + dx
        elif turns == 2:
            rotated[:, 0] = cx - dx
            rotated[:, 1] = cy - dy
        else:
            rotated[:, 0] = cx + dy
            rotated[:, 1] = cy - dx
        return rotated

    @staticmethod
    def _rotate_point_around_center(
        point: Point2D,
        center: Point2D,
        quarter_turn_delta: int,
    ) -> Point2D:
        x_value = float(point[0])
        y_value = float(point[1])
        cx, cy = float(center[0]), float(center[1])
        dx = x_value - cx
        dy = y_value - cy
        turns = int(quarter_turn_delta) % 4
        if turns == 1:
            return (cx - dy, cy + dx)
        if turns == 2:
            return (cx - dx, cy - dy)
        if turns == 3:
            return (cx + dy, cy - dx)
        return (x_value, y_value)

    @staticmethod
    def _build_plot_paths(
        polygons_by_layer: dict[LayerKey, tuple[np.ndarray, ...]],
        visible_layers: Iterable[LayerKey],
    ) -> dict[LayerKey, tuple[np.ndarray, np.ndarray]]:
        visible_layer_set = frozenset(visible_layers)
        paths_by_layer: dict[LayerKey, tuple[np.ndarray, np.ndarray]] = {}
        for layer_key, polygons in polygons_by_layer.items():
            if layer_key not in visible_layer_set:
                continue
            valid_polygons = [polygon for polygon in polygons if len(polygon) >= 2]
            point_count = sum(len(polygon) + 2 for polygon in valid_polygons)
            if point_count <= 0:
                continue
            x_data = np.empty(point_count, dtype=float)
            y_data = np.empty(point_count, dtype=float)
            offset = 0
            for polygon in valid_polygons:
                count = len(polygon)
                next_offset = offset + count
                x_data[offset:next_offset] = polygon[:, 0]
                y_data[offset:next_offset] = polygon[:, 1]
                x_data[next_offset] = polygon[0, 0]
                y_data[next_offset] = polygon[0, 1]
                x_data[next_offset + 1] = float("nan")
                y_data[next_offset + 1] = float("nan")
                offset = next_offset + 2
            paths_by_layer[layer_key] = (x_data, y_data)
        return paths_by_layer

    @staticmethod
    def _build_snap_geometry(
        polygons_by_layer: dict[LayerKey, tuple[np.ndarray, ...]],
        visible_layers: Iterable[LayerKey],
        bounds: tuple[float, float, float, float],
    ) -> tuple[
        np.ndarray,
        np.ndarray,
        np.ndarray,
        float,
        dict[tuple[int, int], tuple[int, ...]],
        dict[tuple[int, int], tuple[int, ...]],
        tuple[int, ...],
    ]:
        vertices: list[np.ndarray] = []
        segment_starts: list[np.ndarray] = []
        segment_ends: list[np.ndarray] = []
        for layer_key in visible_layers:
            for polygon in polygons_by_layer.get(layer_key, ()):
                if len(polygon) == 0:
                    continue
                closed = polygon
                if len(polygon) > 1 and not np.array_equal(polygon[0], polygon[-1]):
                    closed = np.vstack((polygon, polygon[0]))
                vertices.append(np.asarray(closed, dtype=float))
                if len(closed) > 1:
                    segment_starts.append(np.asarray(closed[:-1], dtype=float))
                    segment_ends.append(np.asarray(closed[1:], dtype=float))
        vertex_array = (
            np.vstack(vertices) if vertices else np.empty((0, 2), dtype=float)
        )
        segment_start_array = (
            np.vstack(segment_starts) if segment_starts else np.empty((0, 2), dtype=float)
        )
        segment_end_array = (
            np.vstack(segment_ends) if segment_ends else np.empty((0, 2), dtype=float)
        )
        (
            cell_size,
            vertex_bins,
            segment_bins,
            long_segment_indices,
        ) = DesignDocument._build_snap_spatial_index(
            vertex_array,
            segment_start_array,
            segment_end_array,
            bounds,
        )
        return (
            vertex_array,
            segment_start_array,
            segment_end_array,
            cell_size,
            vertex_bins,
            segment_bins,
            long_segment_indices,
        )

    @staticmethod
    def _build_snap_spatial_index(
        vertices: np.ndarray,
        segment_starts: np.ndarray,
        segment_ends: np.ndarray,
        bounds: tuple[float, float, float, float],
    ) -> tuple[
        float,
        dict[tuple[int, int], tuple[int, ...]],
        dict[tuple[int, int], tuple[int, ...]],
        tuple[int, ...],
    ]:
        left, bottom, right, top = bounds
        span = max(abs(float(right) - float(left)), abs(float(top) - float(bottom)))
        if not math.isfinite(span) or span <= 0.0:
            span = 1.0
        cell_size = span / float(DesignDocument.SNAP_GRID_DIVISIONS)
        if cell_size <= 0.0 or not math.isfinite(cell_size):
            cell_size = 1.0

        vertex_bins_list: dict[tuple[int, int], list[int]] = {}
        for index, vertex in enumerate(vertices):
            key = DesignDocument._snap_grid_key(vertex, cell_size)
            vertex_bins_list.setdefault(key, []).append(index)

        segment_bins_list: dict[tuple[int, int], list[int]] = {}
        long_segment_indices: list[int] = []
        for index, (start, end) in enumerate(zip(segment_starts, segment_ends)):
            min_x = min(float(start[0]), float(end[0]))
            max_x = max(float(start[0]), float(end[0]))
            min_y = min(float(start[1]), float(end[1]))
            max_y = max(float(start[1]), float(end[1]))
            x0 = math.floor(min_x / cell_size)
            x1 = math.floor(max_x / cell_size)
            y0 = math.floor(min_y / cell_size)
            y1 = math.floor(max_y / cell_size)
            cell_count = (x1 - x0 + 1) * (y1 - y0 + 1)
            if cell_count > DesignDocument.SNAP_GRID_MAX_SEGMENT_CELLS:
                long_segment_indices.append(index)
                continue
            for gx in range(x0, x1 + 1):
                for gy in range(y0, y1 + 1):
                    segment_bins_list.setdefault((gx, gy), []).append(index)

        vertex_bins = {key: tuple(values) for key, values in vertex_bins_list.items()}
        segment_bins = {key: tuple(values) for key, values in segment_bins_list.items()}
        return cell_size, vertex_bins, segment_bins, tuple(long_segment_indices)

    @staticmethod
    def _snap_grid_key(point: np.ndarray, cell_size: float) -> tuple[int, int]:
        return (
            math.floor(float(point[0]) / cell_size),
            math.floor(float(point[1]) / cell_size),
        )

    @staticmethod
    def _indices_from_bins(
        bins: dict[tuple[int, int], tuple[int, ...]],
        target: np.ndarray,
        radius: float,
        cell_size: float,
        *,
        extra_indices: tuple[int, ...] = (),
    ) -> np.ndarray:
        x0 = math.floor((float(target[0]) - radius) / cell_size)
        x1 = math.floor((float(target[0]) + radius) / cell_size)
        y0 = math.floor((float(target[1]) - radius) / cell_size)
        y1 = math.floor((float(target[1]) + radius) / cell_size)
        indices: set[int] = set(extra_indices)
        for gx in range(x0, x1 + 1):
            for gy in range(y0, y1 + 1):
                values = bins.get((gx, gy))
                if values:
                    indices.update(values)
        if not indices:
            return np.empty((0,), dtype=np.intp)
        return np.fromiter(indices, dtype=np.intp, count=len(indices))

    @staticmethod
    def _project_point_to_segment(
        point: np.ndarray, start: np.ndarray, end: np.ndarray
    ) -> np.ndarray:
        segment = np.asarray(end, dtype=float) - np.asarray(start, dtype=float)
        length_sq = float(segment @ segment)
        if length_sq <= 1e-18:
            return np.asarray(start, dtype=float)
        t = float((point - start) @ segment) / length_sq
        t = min(1.0, max(0.0, t))
        return np.asarray(start, dtype=float) + segment * t


__all__ = [
    "DesignDocument",
    "DesignModelError",
    "DesignRegistration",
    "LayerKey",
    "MeasurementTarget",
    "Point2D",
    "ResidualSummary",
    "SnapResult",
]
