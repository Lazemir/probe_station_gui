"""Snap geometry construction, indexing, and nearest-target queries."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Iterable, Mapping


@dataclass(frozen=True)
class SnapMatch:
    """Owner-local nearest-target result converted by the document model."""

    point: tuple[float, float]
    mode: str
    distance: float
    segment_start: tuple[float, float] | None = None
    segment_end: tuple[float, float] | None = None


@dataclass(frozen=True)
class SnapGeometry:
    """Indexed visible polygon geometry with nearest-target policy."""

    vertices: Any
    segment_starts: Any
    segment_ends: Any
    cell_size: float
    vertex_bins: dict[tuple[int, int], tuple[int, ...]]
    segment_bins: dict[tuple[int, int], tuple[int, ...]]
    long_segment_indices: tuple[int, ...]

    @classmethod
    def build(
        cls,
        *,
        polygons_by_layer: Mapping[tuple[int, int], tuple[Any, ...]],
        visible_layers: Iterable[tuple[int, int]],
        bounds: tuple[float, float, float, float],
        grid_divisions: int,
        max_segment_cells: int,
    ) -> SnapGeometry:
        """Build closed contour arrays and their spatial index."""

        import numpy as np

        vertices: list[Any] = []
        segment_starts: list[Any] = []
        segment_ends: list[Any] = []
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
            np.vstack(segment_starts)
            if segment_starts
            else np.empty((0, 2), dtype=float)
        )
        segment_end_array = (
            np.vstack(segment_ends) if segment_ends else np.empty((0, 2), dtype=float)
        )
        cell_size, vertex_bins, segment_bins, long_indices = _build_spatial_index(
            vertex_array,
            segment_start_array,
            segment_end_array,
            bounds,
            grid_divisions=grid_divisions,
            max_segment_cells=max_segment_cells,
        )
        return cls(
            vertices=vertex_array,
            segment_starts=segment_start_array,
            segment_ends=segment_end_array,
            cell_size=cell_size,
            vertex_bins=vertex_bins,
            segment_bins=segment_bins,
            long_segment_indices=long_indices,
        )

    @classmethod
    def from_arrays(
        cls,
        vertices: Any,
        segment_starts: Any,
        segment_ends: Any,
        *,
        bounds: tuple[float, float, float, float],
        grid_divisions: int,
        max_segment_cells: int,
        cell_size: float | None = None,
        vertex_bins: dict[tuple[int, int], tuple[int, ...]] | None = None,
        segment_bins: dict[tuple[int, int], tuple[int, ...]] | None = None,
        long_segment_indices: tuple[int, ...] | None = None,
    ) -> SnapGeometry:
        """Retain caller-owned arrays and build only a missing spatial index."""

        if cell_size is None or vertex_bins is None or segment_bins is None:
            (
                cell_size,
                vertex_bins,
                segment_bins,
                long_segment_indices,
            ) = _build_spatial_index(
                vertices,
                segment_starts,
                segment_ends,
                bounds,
                grid_divisions=grid_divisions,
                max_segment_cells=max_segment_cells,
            )
        return cls(
            vertices=vertices,
            segment_starts=segment_starts,
            segment_ends=segment_ends,
            cell_size=float(cell_size),
            vertex_bins=vertex_bins,
            segment_bins=segment_bins,
            long_segment_indices=tuple(long_segment_indices or ()),
        )

    def as_tuple(
        self,
    ) -> tuple[
        Any,
        Any,
        Any,
        float,
        dict[tuple[int, int], tuple[int, ...]],
        dict[tuple[int, int], tuple[int, ...]],
        tuple[int, ...],
    ]:
        """Return the established DesignDocument installation payload."""

        return (
            self.vertices,
            self.segment_starts,
            self.segment_ends,
            self.cell_size,
            self.vertex_bins,
            self.segment_bins,
            self.long_segment_indices,
        )

    def nearest(
        self,
        point: tuple[float, float],
        *,
        max_distance: float | None,
        vertex_priority_ratio: float,
    ) -> SnapMatch:
        """Return the nearest target using midpoint, vertex, and segment policy."""

        import numpy as np

        target = np.asarray(point, dtype=float)
        vertex_point = target
        vertex_distance_sq = float("inf")
        segment_point = target
        segment_distance_sq = float("inf")
        segment_start: tuple[float, float] | None = None
        segment_end: tuple[float, float] | None = None
        midpoint_point = target
        midpoint_distance_sq = float("inf")
        midpoint_start: tuple[float, float] | None = None
        midpoint_end: tuple[float, float] | None = None

        vertex_indices, segment_indices = self._candidate_indices(
            target,
            max_distance=max_distance,
            vertex_priority_ratio=vertex_priority_ratio,
        )

        if len(vertex_indices):
            candidate_vertices = self.vertices[vertex_indices]
            vertex_delta = candidate_vertices - target
            vertex_distances = np.einsum("ij,ij->i", vertex_delta, vertex_delta)
            vertex_index = int(np.argmin(vertex_distances))
            vertex_distance_sq = float(vertex_distances[vertex_index])
            vertex_point = np.asarray(candidate_vertices[vertex_index], dtype=float)

        if len(segment_indices):
            candidate_starts = self.segment_starts[segment_indices]
            candidate_ends = self.segment_ends[segment_indices]
            segments = candidate_ends - candidate_starts
            lengths_sq = np.einsum("ij,ij->i", segments, segments)
            valid_lengths = np.maximum(lengths_sq, 1e-18)
            projections = (
                np.einsum(
                    "ij,ij->i",
                    np.broadcast_to(target, candidate_starts.shape) - candidate_starts,
                    segments,
                )
                / valid_lengths
            )
            projections = np.clip(projections, 0.0, 1.0)
            snapped_points = candidate_starts + segments * projections[:, np.newaxis]
            segment_delta = snapped_points - target
            segment_distances = np.einsum("ij,ij->i", segment_delta, segment_delta)
            segment_index = int(np.argmin(segment_distances))
            segment_distance_sq = float(segment_distances[segment_index])
            segment_point = np.asarray(snapped_points[segment_index], dtype=float)
            start = candidate_starts[segment_index]
            end = candidate_ends[segment_index]
            segment_start = (float(start[0]), float(start[1]))
            segment_end = (float(end[0]), float(end[1]))

            midpoints = (candidate_starts + candidate_ends) * 0.5
            midpoint_delta = midpoints - target
            midpoint_distances = np.einsum(
                "ij,ij->i",
                midpoint_delta,
                midpoint_delta,
            )
            midpoint_index = int(np.argmin(midpoint_distances))
            midpoint_distance_sq = float(midpoint_distances[midpoint_index])
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
        selected_point = vertex_point
        selected_distance_sq = vertex_distance_sq
        selected_start: tuple[float, float] | None = None
        selected_end: tuple[float, float] | None = None
        if midpoint_distance_sq < selected_distance_sq:
            point_mode = "segment_center"
            selected_point = midpoint_point
            selected_distance_sq = midpoint_distance_sq
            selected_start = midpoint_start
            selected_end = midpoint_end
        midpoint_within_threshold = (
            max_distance is not None
            and midpoint_distance_sq <= float(max_distance) * float(max_distance)
        )
        if point_mode == "segment_center" and midpoint_within_threshold:
            return SnapMatch(
                point=(float(selected_point[0]), float(selected_point[1])),
                mode=point_mode,
                distance=math.sqrt(max(0.0, selected_distance_sq)),
                segment_start=selected_start,
                segment_end=selected_end,
            )

        if vertex_distance_sq < float("inf") and (
            segment_distance_sq == float("inf")
            or vertex_distance_sq
            <= segment_distance_sq * (float(vertex_priority_ratio) ** 2)
        ):
            return SnapMatch(
                point=(float(vertex_point[0]), float(vertex_point[1])),
                mode="vertex",
                distance=math.sqrt(max(0.0, vertex_distance_sq)),
            )

        if segment_distance_sq < float("inf"):
            return SnapMatch(
                point=(float(segment_point[0]), float(segment_point[1])),
                mode="segment",
                distance=math.sqrt(max(0.0, segment_distance_sq)),
                segment_start=segment_start,
                segment_end=segment_end,
            )

        return SnapMatch(
            point=(float(target[0]), float(target[1])),
            mode="free",
            distance=0.0,
        )

    def _candidate_indices(
        self,
        target: Any,
        *,
        max_distance: float | None,
        vertex_priority_ratio: float,
    ) -> tuple[Any, Any]:
        import numpy as np

        if (
            max_distance is None
            or not math.isfinite(float(max_distance))
            or max_distance <= 0.0
            or self.cell_size <= 0.0
        ):
            return (
                np.arange(len(self.vertices), dtype=np.intp),
                np.arange(len(self.segment_starts), dtype=np.intp),
            )

        radius = float(max_distance)
        vertex_indices = _indices_from_bins(
            self.vertex_bins,
            target,
            radius * float(vertex_priority_ratio),
            self.cell_size,
        )
        segment_indices = _indices_from_bins(
            self.segment_bins,
            target,
            radius,
            self.cell_size,
            extra_indices=self.long_segment_indices,
        )
        return vertex_indices, segment_indices


def _build_spatial_index(
    vertices: Any,
    segment_starts: Any,
    segment_ends: Any,
    bounds: tuple[float, float, float, float],
    *,
    grid_divisions: int,
    max_segment_cells: int,
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
    cell_size = span / float(grid_divisions)
    if cell_size <= 0.0 or not math.isfinite(cell_size):
        cell_size = 1.0

    vertex_bins_list: dict[tuple[int, int], list[int]] = {}
    for index, vertex in enumerate(vertices):
        key = _grid_key(vertex, cell_size)
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
        if cell_count > max_segment_cells:
            long_segment_indices.append(index)
            continue
        for gx in range(x0, x1 + 1):
            for gy in range(y0, y1 + 1):
                segment_bins_list.setdefault((gx, gy), []).append(index)

    vertex_bins = {key: tuple(values) for key, values in vertex_bins_list.items()}
    segment_bins = {key: tuple(values) for key, values in segment_bins_list.items()}
    return cell_size, vertex_bins, segment_bins, tuple(long_segment_indices)


def _grid_key(point: Any, cell_size: float) -> tuple[int, int]:
    return (
        math.floor(float(point[0]) / cell_size),
        math.floor(float(point[1]) / cell_size),
    )


def _indices_from_bins(
    bins: Mapping[tuple[int, int], tuple[int, ...]],
    target: Any,
    radius: float,
    cell_size: float,
    *,
    extra_indices: tuple[int, ...] = (),
) -> Any:
    import numpy as np

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


__all__ = ["SnapGeometry"]
