"""Design document loading and registration helpers for GDS-backed navigation."""

from __future__ import annotations

import importlib
import math
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, ClassVar, Iterable, Optional


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
    """A design-space target produced by a measurement plan script."""

    id: str
    label: str
    design_center: Point2D
    metadata: dict[str, Any] = field(default_factory=dict)
    group: str | None = None

    @classmethod
    def from_object(cls, value: Any) -> "MeasurementTarget":
        """Coerce a script-produced object into a measurement target."""

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

    @classmethod
    def load(cls, path: str | Path) -> "DesignDocument":
        """Read a GDS/OASIS file using gdstk."""

        gdstk = cls._import_gdstk()
        resolved = Path(path).expanduser().resolve()
        try:
            library = gdstk.read_gds(str(resolved))
        except Exception as exc:  # pragma: no cover - library specific
            raise DesignModelError(f"Failed to load design '{resolved}': {exc}") from exc

        cells = tuple(getattr(library, "cells", ()))
        if not cells:
            raise DesignModelError(f"Design '{resolved}' does not contain any cells.")
        try:
            top_level = tuple(library.top_level())
        except Exception:  # pragma: no cover - library specific
            top_level = ()
        top_cell = top_level[0] if top_level else cells[0]
        cell_names = tuple(str(getattr(cell, "name", "")) for cell in cells if getattr(cell, "name", ""))
        if not cell_names:
            raise DesignModelError(f"Design '{resolved}' does not expose named cells.")
        top_cell_name = str(getattr(top_cell, "name", cell_names[0]))
        if not cls._extract_polygons(top_cell):
            geometry_cell = cls._select_geometry_cell(top_level or cells, cells)
            if geometry_cell is not None:
                top_cell_name = str(getattr(geometry_cell, "name", top_cell_name))
        return cls._from_components(
            path=resolved,
            library=library,
            top_cell_name=top_cell_name,
        )

    @classmethod
    def _from_components(
        cls,
        *,
        path: Path,
        library: Any,
        top_cell_name: str,
        visible_layers: Optional[Iterable[LayerKey]] = None,
    ) -> "DesignDocument":
        cells = tuple(getattr(library, "cells", ()))
        cell_by_name = {
            str(getattr(cell, "name", "")): cell for cell in cells if getattr(cell, "name", "")
        }
        if top_cell_name not in cell_by_name:
            raise DesignModelError(f"Cell '{top_cell_name}' was not found in '{path.name}'.")
        top_cell = cell_by_name[top_cell_name]
        polygons_by_layer = cls._extract_polygons(top_cell)
        if not polygons_by_layer:
            raise DesignModelError(f"Top cell '{top_cell_name}' has no polygon geometry.")
        bounds = cls._calculate_bounds(polygons_by_layer)
        layers = frozenset(polygons_by_layer.keys())
        if visible_layers is None:
            effective_layers = layers
        else:
            effective_layers = frozenset(layer for layer in visible_layers if layer in layers)
            if not effective_layers:
                effective_layers = layers
        snap_vertices, snap_segment_starts, snap_segment_ends = cls._build_snap_geometry(
            polygons_by_layer,
            effective_layers,
        )
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
            snap_vertices=snap_vertices,
            snap_segment_starts=snap_segment_starts,
            snap_segment_ends=snap_segment_ends,
        )

    def with_top_cell(self, top_cell_name: str) -> "DesignDocument":
        """Return a copy using a different top cell from the same library."""

        return self._from_components(
            path=self.path,
            library=self.library,
            top_cell_name=top_cell_name,
            visible_layers=self.visible_layers,
        )

    def with_visible_layers(self, layers: Iterable[LayerKey]) -> "DesignDocument":
        """Return a copy with a different visible layer subset."""

        return self._from_components(
            path=self.path,
            library=self.library,
            top_cell_name=self.top_cell_name,
            visible_layers=frozenset(
                layer for layer in layers if layer in self.polygons_by_layer
            ),
        )

    def layer_keys(self) -> tuple[LayerKey, ...]:
        """Return layer keys in display order."""

        return tuple(sorted(self.polygons_by_layer.keys()))

    def visible_polygons(self) -> dict[LayerKey, tuple[np.ndarray, ...]]:
        """Return polygon geometry for currently visible layers."""

        return {
            layer: polygons
            for layer, polygons in self.polygons_by_layer.items()
            if layer in self.visible_layers
        }

    def snap_point(self, point: Point2D) -> Point2D:
        """Snap a design-space point to the nearest visible vertex or segment."""

        return self.snap_point_info(point).point

    def snap_point_info(self, point: Point2D) -> SnapResult:
        """Return the nearest visible snap target and its geometry metadata."""

        target = np.asarray(point, dtype=float)
        vertex_point = target
        vertex_distance_sq = float("inf")
        segment_point = target
        segment_distance_sq = float("inf")
        segment_start: Point2D | None = None
        segment_end: Point2D | None = None

        if len(self.snap_vertices):
            vertex_delta = self.snap_vertices - target
            vertex_distance_sq_array = np.einsum("ij,ij->i", vertex_delta, vertex_delta)
            vertex_index = int(np.argmin(vertex_distance_sq_array))
            vertex_distance_sq = float(vertex_distance_sq_array[vertex_index])
            vertex_point = np.asarray(self.snap_vertices[vertex_index], dtype=float)

        if len(self.snap_segment_starts):
            segments = self.snap_segment_ends - self.snap_segment_starts
            lengths_sq = np.einsum("ij,ij->i", segments, segments)
            valid_lengths = np.maximum(lengths_sq, 1e-18)
            projections = np.einsum(
                "ij,ij->i",
                np.broadcast_to(target, self.snap_segment_starts.shape)
                - self.snap_segment_starts,
                segments,
            ) / valid_lengths
            projections = np.clip(projections, 0.0, 1.0)
            snapped_points = self.snap_segment_starts + segments * projections[:, np.newaxis]
            segment_delta = snapped_points - target
            segment_distance_sq_array = np.einsum("ij,ij->i", segment_delta, segment_delta)
            segment_index = int(np.argmin(segment_distance_sq_array))
            segment_distance_sq = float(segment_distance_sq_array[segment_index])
            segment_point = np.asarray(snapped_points[segment_index], dtype=float)
            start = self.snap_segment_starts[segment_index]
            end = self.snap_segment_ends[segment_index]
            segment_start = (float(start[0]), float(start[1]))
            segment_end = (float(end[0]), float(end[1]))

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
    def _import_gdstk() -> Any:
        try:
            return importlib.import_module("gdstk")
        except ImportError as exc:
            raise DesignModelError(
                "gdstk is not installed. Install it to enable GDS design navigation."
            ) from exc

    @staticmethod
    def _select_geometry_cell(*cell_groups: Iterable[Any]) -> Any | None:
        for cells in cell_groups:
            for cell in cells:
                if DesignDocument._extract_polygons(cell):
                    return cell
        return None

    @staticmethod
    def _extract_polygons(cell: Any) -> dict[LayerKey, tuple[np.ndarray, ...]]:
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
    def _build_snap_geometry(
        polygons_by_layer: dict[LayerKey, tuple[np.ndarray, ...]],
        visible_layers: Iterable[LayerKey],
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        vertices: list[np.ndarray] = []
        segment_starts: list[np.ndarray] = []
        segment_ends: list[np.ndarray] = []
        for layer_key in visible_layers:
            for polygon in polygons_by_layer.get(layer_key, ()):
                if len(polygon) == 0:
                    continue
                closed = polygon
                if len(polygon) > 1 and not np.allclose(polygon[0], polygon[-1]):
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
        return vertex_array, segment_start_array, segment_end_array

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
