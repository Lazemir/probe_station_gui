"""Pure geometry selection for local KLayout snap candidates."""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass

from .klayout_types import (
    Box2D,
    Point2D,
    SNAP_UNAVAILABLE_EXCESSIVE_COVERAGE,
    SNAP_UNAVAILABLE_INVALID,
    SNAP_UNAVAILABLE_OUTSIDE_BOUNDS,
)
from .model import SnapResult


Segment2D = tuple[Point2D, Point2D]
VERTEX_PRIORITY_RATIO = 1.8
MAX_FILE_SNAP_GDS_FRACTION = 0.05


@dataclass(frozen=True)
class SnapSearchPlan:
    search_box: Box2D | None
    coverage_fraction: float
    skip_reason: str | None = None

    @property
    def accepted(self) -> bool:
        return self.skip_reason is None and self.search_box is not None


def plan_snap_search(
    point: Point2D,
    radius: float,
    gds_bounds: Box2D,
    *,
    max_coverage_fraction: float = MAX_FILE_SNAP_GDS_FRACTION,
) -> SnapSearchPlan:
    try:
        x_value, y_value = float(point[0]), float(point[1])
        radius_value = float(radius)
        left, bottom, right, top = (float(value) for value in gds_bounds)
    except (IndexError, TypeError, ValueError):
        return SnapSearchPlan(None, 0.0, SNAP_UNAVAILABLE_INVALID)
    values = (x_value, y_value, radius_value, left, bottom, right, top)
    if (
        not all(math.isfinite(value) for value in values)
        or radius_value < 0.0
        or right <= left
        or top <= bottom
    ):
        return SnapSearchPlan(None, 0.0, SNAP_UNAVAILABLE_INVALID)
    max_fraction = float(max_coverage_fraction)
    if not math.isfinite(max_fraction) or not 0.0 < max_fraction <= 1.0:
        raise ValueError("Snap coverage fraction must be in (0, 1].")
    query = (
        x_value - radius_value,
        y_value - radius_value,
        x_value + radius_value,
        y_value + radius_value,
    )
    clipped = (
        max(left, query[0]),
        max(bottom, query[1]),
        min(right, query[2]),
        min(top, query[3]),
    )
    if clipped[2] < clipped[0] or clipped[3] < clipped[1]:
        return SnapSearchPlan(None, 0.0, SNAP_UNAVAILABLE_OUTSIDE_BOUNDS)
    intersection_area = (clipped[2] - clipped[0]) * (clipped[3] - clipped[1])
    coverage = intersection_area / ((right - left) * (top - bottom))
    if coverage > max_fraction:
        return SnapSearchPlan(
            clipped,
            coverage,
            SNAP_UNAVAILABLE_EXCESSIVE_COVERAGE,
        )
    return SnapSearchPlan(clipped, coverage)


def select_snap(
    raw_point: Point2D,
    vertices: Iterable[Point2D],
    segments: Iterable[Segment2D],
    radius: float,
) -> SnapResult:
    """Select a local snap target with the legacy midpoint/vertex/line order."""

    raw = (float(raw_point[0]), float(raw_point[1]))
    snap_radius = float(radius)

    vertex_point: Point2D | None = None
    vertex_distance = math.inf
    for vertex in vertices:
        candidate = (float(vertex[0]), float(vertex[1]))
        distance = math.hypot(candidate[0] - raw[0], candidate[1] - raw[1])
        if distance < vertex_distance:
            vertex_point = candidate
            vertex_distance = distance

    segment_point: Point2D | None = None
    segment_distance = math.inf
    segment_source: Segment2D | None = None
    midpoint_point: Point2D | None = None
    midpoint_distance = math.inf
    midpoint_source: Segment2D | None = None
    for segment in segments:
        start = (float(segment[0][0]), float(segment[0][1]))
        end = (float(segment[1][0]), float(segment[1][1]))
        source = (start, end)

        projected = _project_point_onto_segment(raw, start, end)
        projected_distance = math.hypot(
            projected[0] - raw[0],
            projected[1] - raw[1],
        )
        if projected_distance < segment_distance:
            segment_point = projected
            segment_distance = projected_distance
            segment_source = source

        midpoint = ((start[0] + end[0]) * 0.5, (start[1] + end[1]) * 0.5)
        distance_to_midpoint = math.hypot(
            midpoint[0] - raw[0],
            midpoint[1] - raw[1],
        )
        if distance_to_midpoint < midpoint_distance:
            midpoint_point = midpoint
            midpoint_distance = distance_to_midpoint
            midpoint_source = source

    if (
        midpoint_point is not None
        and midpoint_source is not None
        and midpoint_distance <= snap_radius
        and midpoint_distance < vertex_distance
    ):
        return SnapResult(
            point=midpoint_point,
            mode="segment_center",
            distance=midpoint_distance,
            segment_start=midpoint_source[0],
            segment_end=midpoint_source[1],
        )

    if (
        vertex_point is not None
        and (
            segment_point is None
            or vertex_distance <= segment_distance * VERTEX_PRIORITY_RATIO
        )
    ):
        return SnapResult(
            point=vertex_point,
            mode="vertex",
            distance=vertex_distance,
        )

    if (
        segment_point is not None
        and segment_source is not None
        and segment_distance <= snap_radius
    ):
        return SnapResult(
            point=segment_point,
            mode="segment",
            distance=segment_distance,
            segment_start=segment_source[0],
            segment_end=segment_source[1],
        )

    return SnapResult(point=raw, mode="free", distance=0.0)


def _project_point_onto_segment(
    point: Point2D,
    start: Point2D,
    end: Point2D,
) -> Point2D:
    delta_x = end[0] - start[0]
    delta_y = end[1] - start[1]
    length_squared = delta_x * delta_x + delta_y * delta_y
    if length_squared <= 0.0:
        return start
    fraction = (
        (point[0] - start[0]) * delta_x + (point[1] - start[1]) * delta_y
    ) / length_squared
    fraction = min(1.0, max(0.0, fraction))
    return (start[0] + fraction * delta_x, start[1] + fraction * delta_y)


__all__ = [
    "MAX_FILE_SNAP_GDS_FRACTION",
    "Segment2D",
    "SnapSearchPlan",
    "plan_snap_search",
    "select_snap",
]
