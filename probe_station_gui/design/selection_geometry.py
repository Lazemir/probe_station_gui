"""Qt-free geometry used by design selection, markup, and transforms."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Iterable, TypeAlias

from probe_station_gui.design.model import Point2D


Geometry: TypeAlias = "PointGeometry | SegmentGeometry"


@dataclass(frozen=True)
class SelectionRect:
    """Normalized inclusive axis-aligned selection rectangle."""

    left: float
    right: float
    bottom: float
    top: float

    @classmethod
    def from_drag(cls, start: Point2D, end: Point2D) -> SelectionRect:
        return cls(
            left=min(float(start[0]), float(end[0])),
            right=max(float(start[0]), float(end[0])),
            bottom=min(float(start[1]), float(end[1])),
            top=max(float(start[1]), float(end[1])),
        )

    def contains(self, point: Point2D) -> bool:
        return (
            self.left <= float(point[0]) <= self.right
            and self.bottom <= float(point[1]) <= self.top
        )


@dataclass(frozen=True)
class PointGeometry:
    """Selectable point in design coordinates."""

    point: Point2D

    def hit_test(self, point: Point2D, *, tolerance: float) -> bool:
        return _within_tolerance(_distance_squared(self.point, point), tolerance)

    def contained_by(self, rect: SelectionRect) -> bool:
        return rect.contains(self.point)

    def crosses(self, rect: SelectionRect) -> bool:
        return rect.contains(self.point)

    def translated(self, dx: float, dy: float) -> PointGeometry:
        return PointGeometry((self.point[0] + float(dx), self.point[1] + float(dy)))


@dataclass(frozen=True)
class SegmentGeometry:
    """Selectable finite straight segment in design coordinates."""

    start: Point2D
    end: Point2D

    @property
    def midpoint(self) -> Point2D:
        return (
            (self.start[0] + self.end[0]) * 0.5,
            (self.start[1] + self.end[1]) * 0.5,
        )

    def hit_test(self, point: Point2D, *, tolerance: float) -> bool:
        return _within_tolerance(
            _point_segment_distance_squared(point, self),
            tolerance,
        )

    def contained_by(self, rect: SelectionRect) -> bool:
        return rect.contains(self.start) and rect.contains(self.end)

    def crosses(self, rect: SelectionRect) -> bool:
        return segment_intersects_rect(self, rect)

    def translated(self, dx: float, dy: float) -> SegmentGeometry:
        offset_x = float(dx)
        offset_y = float(dy)
        return SegmentGeometry(
            (self.start[0] + offset_x, self.start[1] + offset_y),
            (self.end[0] + offset_x, self.end[1] + offset_y),
        )


@dataclass(frozen=True)
class GuideSnapCandidate:
    """One deduplicated snap candidate derived from visible guides."""

    point: Point2D
    mode: str
    segment_start: Point2D | None = None
    segment_end: Point2D | None = None


def segment_intersects_rect(segment: SegmentGeometry, rect: SelectionRect) -> bool:
    """Return whether any point of a finite segment touches the rectangle."""

    if rect.contains(segment.start) or rect.contains(segment.end):
        return True
    corners = (
        (rect.left, rect.bottom),
        (rect.right, rect.bottom),
        (rect.right, rect.top),
        (rect.left, rect.top),
    )
    edges = tuple(
        SegmentGeometry(corners[index], corners[(index + 1) % len(corners)])
        for index in range(len(corners))
    )
    return any(_segments_touch(segment, edge) for edge in edges)


def segment_intersection(
    first: SegmentGeometry,
    second: SegmentGeometry,
) -> Point2D | None:
    """Return the unique finite-segment intersection, excluding collinear overlap."""

    p = first.start
    q = second.start
    r = (first.end[0] - p[0], first.end[1] - p[1])
    s = (second.end[0] - q[0], second.end[1] - q[1])
    denominator = _cross(r, s)
    epsilon = _cross_epsilon(first, second)
    if abs(denominator) <= epsilon:
        return None
    q_minus_p = (q[0] - p[0], q[1] - p[1])
    t = _cross(q_minus_p, s) / denominator
    u = _cross(q_minus_p, r) / denominator
    parameter_epsilon = 1e-12
    if not (
        -parameter_epsilon <= t <= 1.0 + parameter_epsilon
        and -parameter_epsilon <= u <= 1.0 + parameter_epsilon
    ):
        return None
    clamped_t = min(1.0, max(0.0, t))
    return (
        float(p[0] + clamped_t * r[0]),
        float(p[1] + clamped_t * r[1]),
    )


def guide_snap_candidates(
    segments: Iterable[SegmentGeometry],
) -> tuple[GuideSnapCandidate, ...]:
    """Return endpoints, midpoints, and unique finite intersections."""

    materialized = tuple(segments)
    candidates: list[GuideSnapCandidate] = []
    for segment in materialized:
        _merge_candidate(
            candidates,
            GuideSnapCandidate(
                segment.start,
                "guide_end",
                segment.start,
                segment.end,
            ),
        )
        _merge_candidate(
            candidates,
            GuideSnapCandidate(
                segment.end,
                "guide_end",
                segment.start,
                segment.end,
            ),
        )
        _merge_candidate(
            candidates,
            GuideSnapCandidate(
                segment.midpoint,
                "guide_center",
                segment.start,
                segment.end,
            ),
        )
    for first_index, first in enumerate(materialized):
        for second in materialized[first_index + 1 :]:
            intersection = segment_intersection(first, second)
            if intersection is not None:
                _merge_candidate(
                    candidates,
                    GuideSnapCandidate(intersection, "guide_intersection"),
                )
    return tuple(candidates)


def translated_geometry(geometry: Geometry, dx: float, dy: float) -> Geometry:
    """Translate either supported selectable geometry type."""

    return geometry.translated(dx, dy)


def _point_segment_distance_squared(point: Point2D, segment: SegmentGeometry) -> float:
    dx = segment.end[0] - segment.start[0]
    dy = segment.end[1] - segment.start[1]
    length_squared = dx * dx + dy * dy
    if length_squared <= 0.0:
        return _distance_squared(point, segment.start)
    projection = (
        (point[0] - segment.start[0]) * dx
        + (point[1] - segment.start[1]) * dy
    ) / length_squared
    projection = min(1.0, max(0.0, projection))
    closest = (
        segment.start[0] + projection * dx,
        segment.start[1] + projection * dy,
    )
    return _distance_squared(point, closest)


def _segments_touch(first: SegmentGeometry, second: SegmentGeometry) -> bool:
    first_start = _orientation(first.start, first.end, second.start)
    first_end = _orientation(first.start, first.end, second.end)
    second_start = _orientation(second.start, second.end, first.start)
    second_end = _orientation(second.start, second.end, first.end)
    epsilon = _cross_epsilon(first, second)
    if first_start * first_end < -epsilon and second_start * second_end < -epsilon:
        return True
    return any(
        abs(value) <= epsilon and _within_bounds(point, segment)
        for value, point, segment in (
            (first_start, second.start, first),
            (first_end, second.end, first),
            (second_start, first.start, second),
            (second_end, first.end, second),
        )
    )


def _merge_candidate(
    candidates: list[GuideSnapCandidate],
    candidate: GuideSnapCandidate,
) -> None:
    precedence = {
        "guide_end": 0,
        "guide_center": 1,
        "guide_intersection": 2,
    }
    tolerance_squared = _candidate_tolerance(candidate.point) ** 2
    for index, existing in enumerate(candidates):
        if _distance_squared(existing.point, candidate.point) <= tolerance_squared:
            if precedence[candidate.mode] > precedence[existing.mode]:
                candidates[index] = candidate
            return
    candidates.append(candidate)


def _candidate_tolerance(point: Point2D) -> float:
    return 1e-9 * max(1.0, abs(point[0]), abs(point[1]))


def _distance_squared(first: Point2D, second: Point2D) -> float:
    dx = float(first[0]) - float(second[0])
    dy = float(first[1]) - float(second[1])
    return dx * dx + dy * dy


def _within_tolerance(distance_squared: float, tolerance: float) -> bool:
    limit = max(0.0, float(tolerance)) ** 2
    return distance_squared <= limit + 1e-12 * max(1.0, limit)


def _orientation(first: Point2D, second: Point2D, third: Point2D) -> float:
    return _cross(
        (second[0] - first[0], second[1] - first[1]),
        (third[0] - first[0], third[1] - first[1]),
    )


def _cross(first: Point2D, second: Point2D) -> float:
    return first[0] * second[1] - first[1] * second[0]


def _cross_epsilon(first: SegmentGeometry, second: SegmentGeometry) -> float:
    first_length = math.hypot(
        first.end[0] - first.start[0],
        first.end[1] - first.start[1],
    )
    second_length = math.hypot(
        second.end[0] - second.start[0],
        second.end[1] - second.start[1],
    )
    return 1e-12 * max(1.0, first_length * second_length)


def _within_bounds(point: Point2D, segment: SegmentGeometry) -> bool:
    coordinates = (
        float(point[0]),
        float(point[1]),
        float(segment.start[0]),
        float(segment.start[1]),
        float(segment.end[0]),
        float(segment.end[1]),
    )
    span = max(
        1.0,
        abs(segment.end[0] - segment.start[0]),
        abs(segment.end[1] - segment.start[1]),
    )
    epsilon = max(
        1e-12 * span,
        *(4.0 * math.ulp(coordinate) for coordinate in coordinates),
    )
    return (
        min(segment.start[0], segment.end[0]) - epsilon
        <= point[0]
        <= max(segment.start[0], segment.end[0]) + epsilon
        and min(segment.start[1], segment.end[1]) - epsilon
        <= point[1]
        <= max(segment.start[1], segment.end[1]) + epsilon
    )


__all__ = [
    "Geometry",
    "GuideSnapCandidate",
    "PointGeometry",
    "SegmentGeometry",
    "SelectionRect",
    "guide_snap_candidates",
    "segment_intersection",
    "segment_intersects_rect",
    "translated_geometry",
]
