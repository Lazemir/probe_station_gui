"""Qt-free bounds used to constrain Design Window navigation."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import TypeAlias

from probe_station_gui.design.markup import MarkupDocument
from probe_station_gui.route.model import MeasurementRoute


Point2D: TypeAlias = tuple[float, float]
Box2D: TypeAlias = tuple[float, float, float, float]
CONTENT_PADDING_FRACTION = 0.05
GDS_FOCUS_PADDING_FRACTION = 0.02
VIEW_RANGE_ABS_TOLERANCE = 1e-9


@dataclass(frozen=True)
class NavigationBounds:
    content_bounds: Box2D
    frame: Box2D
    ignored_coordinate_count: int = 0


def content_bounds(
    gds_bounds: Box2D,
    route: MeasurementRoute | None,
    markup: MarkupDocument | None,
) -> tuple[Box2D, int]:
    left, bottom, right, top = _finite_box(gds_bounds)
    ignored = 0

    def include(point: object) -> Point2D | None:
        nonlocal left, bottom, right, top, ignored
        try:
            x_raw, y_raw = point
            x_value, y_value = float(x_raw), float(y_raw)
        except (IndexError, TypeError, ValueError):
            ignored += 1
            return None
        if not math.isfinite(x_value) or not math.isfinite(y_value):
            ignored += 1
            return None
        left = min(left, x_value)
        bottom = min(bottom, y_value)
        right = max(right, x_value)
        top = max(top, y_value)
        return (x_value, y_value)

    if route is not None:
        for route_point in route.points:
            center = include(route_point.camera_center)
            if center is None:
                ignored += len(route.needle_offsets[:2])
                continue
            for offset in route.needle_offsets[:2]:
                try:
                    hit = offset.apply_to(center)
                except (IndexError, TypeError, ValueError):
                    ignored += 1
                    continue
                include(hit)
    if markup is not None:
        for guide in markup.guides:
            include(guide.start)
            include(guide.end)
    return (left, bottom, right, top), ignored


def pad_bounds(bounds: Box2D, fraction: float) -> Box2D:
    left, bottom, right, top = _finite_box(bounds)
    fraction = float(fraction)
    if not math.isfinite(fraction) or fraction < 0.0:
        raise ValueError("Navigation padding must be finite and nonnegative.")
    width = right - left
    height = top - bottom
    x_margin = width * fraction
    y_margin = height * fraction
    return (
        left - x_margin,
        bottom - y_margin,
        right + x_margin,
        top + y_margin,
    )


def fit_bounds_to_aspect(bounds: Box2D, viewport_size: tuple[float, float]) -> Box2D:
    left, bottom, right, top = _finite_box(bounds)
    viewport_width, viewport_height = map(float, viewport_size)
    if viewport_width <= 0.0 or viewport_height <= 0.0:
        return bounds
    width = right - left
    height = top - bottom
    target_aspect = viewport_width / viewport_height
    center_x = (left + right) * 0.5
    center_y = (bottom + top) * 0.5
    if width / height < target_aspect:
        width = height * target_aspect
    else:
        height = width / target_aspect
    return (
        center_x - width * 0.5,
        center_y - height * 0.5,
        center_x + width * 0.5,
        center_y + height * 0.5,
    )


def navigation_frame(
    content: Box2D,
    viewport_size: tuple[float, float],
) -> Box2D:
    return fit_bounds_to_aspect(
        pad_bounds(content, CONTENT_PADDING_FRACTION),
        viewport_size,
    )


def build_navigation_bounds(
    gds_bounds: Box2D,
    route: MeasurementRoute | None,
    markup: MarkupDocument | None,
    *,
    viewport_size: tuple[float, float],
) -> NavigationBounds:
    content, ignored = content_bounds(gds_bounds, route, markup)
    return NavigationBounds(
        content_bounds=content,
        frame=navigation_frame(content, viewport_size),
        ignored_coordinate_count=ignored,
    )


def clamp_view_bounds(view: Box2D, frame: Box2D) -> Box2D:
    view_left, view_bottom, view_right, view_top = _finite_box(view)
    frame_left, frame_bottom, frame_right, frame_top = _finite_box(frame)
    view_width = view_right - view_left
    view_height = view_top - view_bottom
    frame_width = frame_right - frame_left
    frame_height = frame_top - frame_bottom
    scale = min(1.0, frame_width / view_width, frame_height / view_height)
    width = view_width * scale
    height = view_height * scale
    center_x = min(
        frame_right - width * 0.5,
        max(frame_left + width * 0.5, (view_left + view_right) * 0.5),
    )
    center_y = min(
        frame_top - height * 0.5,
        max(frame_bottom + height * 0.5, (view_bottom + view_top) * 0.5),
    )
    return (
        center_x - width * 0.5,
        center_y - height * 0.5,
        center_x + width * 0.5,
        center_y + height * 0.5,
    )


def _finite_box(bounds: Box2D) -> Box2D:
    values = tuple(float(value) for value in bounds)
    if len(values) != 4 or not all(math.isfinite(value) for value in values):
        raise ValueError("Navigation bounds must contain four finite values.")
    left, bottom, right, top = values
    if right <= left or top <= bottom:
        raise ValueError("Navigation bounds must have positive width and height.")
    return left, bottom, right, top


__all__ = [
    "Box2D",
    "CONTENT_PADDING_FRACTION",
    "GDS_FOCUS_PADDING_FRACTION",
    "NavigationBounds",
    "VIEW_RANGE_ABS_TOLERANCE",
    "build_navigation_bounds",
    "clamp_view_bounds",
    "content_bounds",
    "fit_bounds_to_aspect",
    "navigation_frame",
    "pad_bounds",
]
