"""Shared immutable messages and shape adaptation for KLayout workers."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, TypeAlias

from .klayout_types import Point2D


STOP_WORKER = object()
CREATOR_THREAD_ERROR = "KLayout worker methods must be called from the creator thread."
ShapeContours: TypeAlias = Iterable[tuple[Iterable[Point2D], bool]]


@dataclass(frozen=True)
class WorkerPublication:
    """One queued render or snap publication tagged by configuration generation."""

    kind: str
    value: object
    config_generation: int | None


def shape_contours(
    shape: Any,
    transform: Any,
    db: Any,
) -> ShapeContours:
    """Yield transformed hull, hole, or edge contours for a KLayout shape."""
    if shape.is_box():
        box = shape.dbox
        points = (
            db.DPoint(box.left, box.bottom),
            db.DPoint(box.right, box.bottom),
            db.DPoint(box.right, box.top),
            db.DPoint(box.left, box.top),
        )
        yield (_point_tuples(transform * point for point in points), True)
        return

    if shape.is_polygon():
        polygon = transform * shape.dpolygon
    elif shape.is_path():
        polygon = transform * shape.dpath.polygon()
    elif hasattr(shape, "is_edge") and shape.is_edge():
        edge = shape.dedge
        yield (
            _point_tuples((transform * edge.p1, transform * edge.p2)),
            False,
        )
        return
    else:
        return

    yield (_point_tuples(polygon.each_point_hull()), True)
    for hole_index in range(polygon.holes()):
        yield (_point_tuples(polygon.each_point_hole(hole_index)), True)


def _point_tuples(points: Iterable[Any]) -> Iterable[Point2D]:
    for point in points:
        yield (float(point.x), float(point.y))


__all__ = [
    "CREATOR_THREAD_ERROR",
    "STOP_WORKER",
    "ShapeContours",
    "WorkerPublication",
    "shape_contours",
]
