"""Pure geometry helpers for the design navigator UI."""

from __future__ import annotations

import math


Point2D = tuple[float, float]


def route_arrow_segments(
    centers: list[Point2D],
    *,
    pixel_size: float | None,
    deferred: bool = False,
) -> tuple[list[float], list[float]]:
    if len(centers) < 2:
        return [], []
    if deferred:
        return [], []
    if pixel_size is None:
        return [], []
    reference_length = first_segment_length(centers)
    if reference_length <= 1e-12:
        return [], []
    common_arrow_len = min(
        max(float(pixel_size) * 5.0, reference_length * 0.18),
        float(pixel_size) * 12.0,
    )
    common_arrow_width = common_arrow_len * 0.58
    x_values: list[float] = []
    y_values: list[float] = []
    for start, end in zip(centers, centers[1:]):
        dx = float(end[0] - start[0])
        dy = float(end[1] - start[1])
        length = math.hypot(dx, dy)
        if length <= 1e-12:
            continue
        ux = dx / length
        uy = dy / length
        px = -uy
        py = ux
        arrow_len = common_arrow_len
        arrow_width = common_arrow_width
        if length < arrow_len * 2.2:
            arrow_len = max(float(pixel_size) * 5.0, length * 0.34)
            arrow_width = min(arrow_width, arrow_len * 0.7)
        for fraction in route_arrow_tip_fractions(
            length,
            reference_length,
            arrow_len,
        ):
            tip_x = float(start[0] + dx * fraction)
            tip_y = float(start[1] + dy * fraction)
            base_x = tip_x - ux * arrow_len
            base_y = tip_y - uy * arrow_len
            left_x = base_x + px * arrow_width * 0.5
            left_y = base_y + py * arrow_width * 0.5
            right_x = base_x - px * arrow_width * 0.5
            right_y = base_y - py * arrow_width * 0.5
            notch_x = base_x + ux * arrow_len * 0.22
            notch_y = base_y + uy * arrow_len * 0.22
            x_values.extend(
                [
                    left_x,
                    tip_x,
                    right_x,
                    float("nan"),
                    left_x,
                    notch_x,
                    right_x,
                    float("nan"),
                ]
            )
            y_values.extend(
                [
                    left_y,
                    tip_y,
                    right_y,
                    float("nan"),
                    left_y,
                    notch_y,
                    right_y,
                    float("nan"),
                ]
            )
    return x_values, y_values


def first_segment_length(centers: list[Point2D]) -> float:
    for start, end in zip(centers, centers[1:]):
        length = math.hypot(float(end[0] - start[0]), float(end[1] - start[1]))
        if length > 1e-12:
            return length
    return 0.0


def route_arrow_tip_fractions(
    segment_length: float,
    reference_length: float,
    arrow_len: float,
) -> list[float]:
    spacing = max(float(reference_length), float(arrow_len) * 4.0)
    if spacing <= 1e-12:
        return []
    arrow_count = max(1, min(80, int(round(float(segment_length) / spacing))))
    if arrow_count == 1:
        return [0.58]
    return [
        float(index + 1) / float(arrow_count + 1)
        for index in range(arrow_count)
    ]


def vector_from_length_angle(length: float, angle_degrees: float) -> Point2D:
    radians = math.radians(float(angle_degrees))
    distance = float(length)
    return (distance * math.cos(radians), distance * math.sin(radians))


def vector_length_angle(vector: Point2D) -> tuple[float, float]:
    dx = float(vector[0])
    dy = float(vector[1])
    length = math.hypot(dx, dy)
    angle = math.degrees(math.atan2(dy, dx)) if length > 0.0 else 0.0
    return length, angle


def count_from_endpoint(
    start: Point2D,
    step: Point2D,
    end: Point2D,
) -> int:
    step_x = float(step[0])
    step_y = float(step[1])
    denominator = step_x * step_x + step_y * step_y
    if denominator <= 1e-18:
        return 1
    delta_x = float(end[0]) - float(start[0])
    delta_y = float(end[1]) - float(start[1])
    projected_steps = (delta_x * step_x + delta_y * step_y) / denominator
    return max(1, int(math.floor(projected_steps + 0.5)) + 1)


def route_pick_label_text(mode: str) -> str:
    labels = {
        "array_origin": "array origin",
        "array_dir1": "direction 1 vector start",
        "array_extent1": "direction 1 approximate end",
        "array_dir2": "direction 2 vector start",
        "array_extent2": "direction 2 approximate end",
    }
    return labels.get(mode, "route point")


def format_bounds(bounds: tuple[float, float, float, float]) -> str:
    left, bottom, right, top = bounds
    width = right - left
    height = top - bottom
    diagonal = math.hypot(width, height)
    return f"{width:.3f} x {height:.3f} (diag {diagonal:.3f})"


def format_mark_label(label: str, point: Point2D | None) -> str:
    if point is None:
        return f"{label}: not set"
    return f"{label}: X={point[0]:.3f}, Y={point[1]:.3f}"


def format_point(point: Point2D) -> str:
    return f"X={point[0]:.3f}, Y={point[1]:.3f}"
