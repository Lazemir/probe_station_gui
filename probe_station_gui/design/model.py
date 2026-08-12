"""Design document loading and registration helpers for GDS-backed navigation."""

from __future__ import annotations

import importlib
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import MappingProxyType
from typing import Any, ClassVar, Iterable, Mapping, Optional

from probe_station_gui.design import document_snap, document_source


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
class SnapResult:
    """Nearest visible snap target for a design-space point."""

    point: Point2D
    mode: str
    distance: float
    segment_start: Point2D | None = None
    segment_end: Point2D | None = None


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
    snap_long_segment_indices: tuple[int, ...] = field(
        default_factory=tuple, repr=False
    )
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
        if not self.snap_geometry_built and (
            len(self.snap_vertices) > 0 or len(self.snap_segment_starts) > 0
        ):
            object.__setattr__(self, "snap_geometry_built", True)

    @classmethod
    def load(cls, path: str | Path) -> "DesignDocument":
        """Read lightweight metadata from a GDS/OASIS file using KLayout."""

        try:
            source = document_source.load_file_source(path)
        except document_source.DocumentSourceError as exc:
            raise DesignModelError(str(exc)) from exc
        return cls(
            path=source.path,
            library=source.library,
            top_cell=source.top_cell,
            top_cell_name=source.top_cell_name,
            cell_names=source.cell_names,
            dbu=source.dbu,
            user_unit=source.user_unit,
            bounds=source.bounds,
            polygons_by_layer=source.polygons_by_layer,
            visible_layers=source.available_layers,
            file_backed=source.file_backed,
            available_layers=source.available_layers,
            cell_bounds=source.cell_bounds,
            source_load_id=source.source_load_id,
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
        try:
            source = document_source.load_fixture_source(
                path=path,
                library=library,
                top_cell_name=top_cell_name,
            )
        except document_source.DocumentSourceError as exc:
            raise DesignModelError(str(exc)) from exc
        polygons_by_layer = source.polygons_by_layer
        if rotation_quarter_turns:
            polygons_by_layer = cls._rotate_polygons_by_layer(
                polygons_by_layer,
                source.bounds,
                rotation_quarter_turns,
            )
        try:
            bounds = document_source.polygon_bounds(polygons_by_layer)
        except document_source.DocumentSourceError as exc:
            raise DesignModelError(str(exc)) from exc
        layers = frozenset(polygons_by_layer.keys())
        if visible_layers is None:
            effective_layers = layers
        else:
            effective_layers = frozenset(
                layer for layer in visible_layers if layer in layers
            )
            if not effective_layers:
                effective_layers = layers
        return cls(
            path=source.path,
            library=source.library,
            top_cell=source.top_cell,
            top_cell_name=source.top_cell_name,
            cell_names=source.cell_names,
            dbu=source.dbu,
            user_unit=source.user_unit,
            bounds=bounds,
            polygons_by_layer=polygons_by_layer,
            visible_layers=effective_layers,
            plot_paths_by_layer=cls._build_plot_paths(
                polygons_by_layer, effective_layers
            ),
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
        try:
            bounds = document_source.polygon_bounds(polygons_by_layer)
        except document_source.DocumentSourceError as exc:
            raise DesignModelError(str(exc)) from exc
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
        geometry = document_snap.SnapGeometry.from_arrays(
            self.snap_vertices,
            self.snap_segment_starts,
            self.snap_segment_ends,
            bounds=self.bounds,
            grid_divisions=self.SNAP_GRID_DIVISIONS,
            max_segment_cells=self.SNAP_GRID_MAX_SEGMENT_CELLS,
            cell_size=self.snap_grid_cell_size,
            vertex_bins=self.snap_vertex_bins,
            segment_bins=self.snap_segment_bins,
            long_segment_indices=self.snap_long_segment_indices,
        )
        match = geometry.nearest(
            point,
            max_distance=max_distance,
            vertex_priority_ratio=self.SNAP_VERTEX_PRIORITY_RATIO,
        )
        return SnapResult(
            point=match.point,
            mode=match.mode,
            distance=match.distance,
            segment_start=match.segment_start,
            segment_end=match.segment_end,
        )

    def _ensure_snap_geometry(self) -> None:
        if self.snap_geometry_built:
            return
        self.set_snap_geometry(*self.build_snap_geometry())

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

        return document_snap.SnapGeometry.build(
            polygons_by_layer=self.polygons_by_layer,
            visible_layers=self.visible_layers,
            bounds=self.bounds,
            grid_divisions=self.SNAP_GRID_DIVISIONS,
            max_segment_cells=self.SNAP_GRID_MAX_SEGMENT_CELLS,
        ).as_tuple()

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

        geometry = document_snap.SnapGeometry.from_arrays(
            snap_vertices,
            snap_segment_starts,
            snap_segment_ends,
            bounds=self.bounds,
            grid_divisions=self.SNAP_GRID_DIVISIONS,
            max_segment_cells=self.SNAP_GRID_MAX_SEGMENT_CELLS,
            cell_size=snap_grid_cell_size,
            vertex_bins=snap_vertex_bins,
            segment_bins=snap_segment_bins,
            long_segment_indices=snap_long_segment_indices,
        )
        object.__setattr__(self, "snap_vertices", geometry.vertices)
        object.__setattr__(self, "snap_segment_starts", geometry.segment_starts)
        object.__setattr__(self, "snap_segment_ends", geometry.segment_ends)
        object.__setattr__(self, "snap_grid_cell_size", geometry.cell_size)
        object.__setattr__(self, "snap_vertex_bins", geometry.vertex_bins)
        object.__setattr__(self, "snap_segment_bins", geometry.segment_bins)
        object.__setattr__(
            self,
            "snap_long_segment_indices",
            geometry.long_segment_indices,
        )
        object.__setattr__(self, "snap_geometry_built", True)

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


__all__ = [
    "DesignDocument",
    "DesignModelError",
    "LayerKey",
    "MeasurementTarget",
    "Point2D",
    "SnapResult",
]
