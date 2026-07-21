"""Stateless rendering for interactive microscope overlays."""

from __future__ import annotations

from dataclasses import dataclass
import math

from PySide6.QtCore import QPoint, QPointF, QRect, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPen


Point = tuple[float, float]
Segment = tuple[Point, Point]


@dataclass(frozen=True)
class TargetOverlay:
    relative_position: Point | None
    pending: bool
    blink_dimmed: bool


@dataclass(frozen=True)
class AlignmentOverlay:
    enabled: bool
    points: tuple[Point, ...]
    instruction: str


@dataclass(frozen=True)
class MeasurementOverlay:
    mode: str | None
    points: tuple[Point, ...]
    hover: Point | None
    ruler_segments: tuple[Segment, ...]
    rect_segments: tuple[Segment, ...]
    mm_per_pixel_x: float | None
    mm_per_pixel_y: float | None


def draw_target_overlay(
    painter: QPainter,
    display_rect: QRect,
    overlay: TargetOverlay,
) -> None:
    if overlay.relative_position is None:
        return
    rel_x, rel_y = overlay.relative_position
    target_x = display_rect.left() + rel_x * display_rect.width()
    target_y = display_rect.top() + rel_y * display_rect.height()
    color = QColor("red")
    if overlay.pending:
        color = QColor("#ad6b6b" if overlay.blink_dimmed else "#c62828")
    painter.setPen(QPen(color, 1))
    painter.drawLine(
        display_rect.left(), int(target_y), display_rect.right(), int(target_y)
    )
    painter.drawLine(
        int(target_x), display_rect.top(), int(target_x), display_rect.bottom()
    )
    painter.drawEllipse(QPoint(int(target_x), int(target_y)), 6, 6)


def draw_alignment_overlay(
    painter: QPainter,
    display_rect: QRect,
    overlay: AlignmentOverlay,
    *,
    canvas_width: int,
) -> None:
    if not overlay.enabled:
        return
    positions = _draw_alignment_points(painter, display_rect, overlay.points)
    if len(positions) == 2:
        painter.setPen(QPen(QColor("#ffee58"), 2))
        painter.drawLine(positions[0][0], positions[0][1], positions[1][0], positions[1][1])
    if overlay.instruction:
        _draw_alignment_instruction(
            painter,
            overlay.instruction,
            canvas_width=canvas_width,
        )


def _draw_alignment_points(
    painter: QPainter,
    display_rect: QRect,
    points: tuple[Point, ...],
) -> list[tuple[int, int]]:
    painter.setPen(QPen(QColor("#ffd54f"), 2))
    positions: list[tuple[int, int]] = []
    for index, (rel_x, rel_y) in enumerate(points, start=1):
        point_x = int(display_rect.left() + rel_x * display_rect.width())
        point_y = int(display_rect.top() + rel_y * display_rect.height())
        positions.append((point_x, point_y))
        painter.drawEllipse(QPoint(point_x, point_y), 7, 7)
        painter.drawText(point_x + 10, point_y - 10, str(index))
    return positions


def _draw_alignment_instruction(
    painter: QPainter,
    instruction: str,
    *,
    canvas_width: int,
) -> None:
    painter.setPen(Qt.NoPen)
    painter.setBrush(QColor(0, 0, 0, 170))
    painter.drawRoundedRect(12, 12, min(canvas_width - 24, 430), 46, 8, 8)
    painter.setPen(QPen(QColor("#fff3cd"), 1))
    painter.drawText(
        QRect(20, 18, max(0, canvas_width - 40), 34),
        Qt.AlignLeft | Qt.AlignVCenter,
        instruction,
    )


def draw_measurement_overlay(
    painter: QPainter,
    display_rect: QRect,
    scale_x: float,
    scale_y: float,
    overlay: MeasurementOverlay,
) -> None:
    if overlay.mode == "ruler":
        _draw_ruler(painter, display_rect, scale_x, scale_y, overlay)
    elif overlay.mode == "rect":
        _draw_rectangles(painter, display_rect, scale_x, scale_y, overlay)


def draw_scale_bar(
    painter: QPainter,
    display_rect: QRect,
    scale_x: float,
    mm_per_pixel_x: float | None,
) -> None:
    if mm_per_pixel_x is None or mm_per_pixel_x <= 0:
        return
    lengths_mm = (5.0, 2.0, 1.0, 0.5, 0.2, 0.1, 0.05, 0.02, 0.01, 0.005, 0.002, 0.001)
    bar_mm = next(
        (
            length
            for length in lengths_mm
            if length / mm_per_pixel_x * scale_x <= display_rect.width() * 0.3
        ),
        lengths_mm[-1],
    )
    bar_width = bar_mm / mm_per_pixel_x * scale_x
    left = float(display_rect.left() + 20)
    right = left + bar_width
    y = float(display_rect.bottom() - 36)
    painter.save()
    painter.setRenderHint(QPainter.Antialiasing)
    _draw_scale_bar_lines(painter, left, right, y)
    font = QFont()
    font.setPointSize(8)
    painter.setFont(font)
    label_rect = QRectF(left, y - 21.0, bar_width, 14.0)
    _draw_shadowed_text(painter, label_rect, _scale_label(bar_mm), QColor("white"))
    painter.restore()


def _draw_scale_bar_lines(
    painter: QPainter,
    left: float,
    right: float,
    y: float,
) -> None:
    for color, width in ((QColor(0, 0, 0, 160), 3), (QColor("white"), 1.5)):
        painter.setPen(QPen(color, width))
        painter.drawLine(QPointF(left, y), QPointF(right, y))
        painter.drawLine(QPointF(left, y - 5.0), QPointF(left, y + 5.0))
        painter.drawLine(QPointF(right, y - 5.0), QPointF(right, y + 5.0))


def draw_axis_triad(painter: QPainter, display_rect: QRect) -> None:
    painter.save()
    painter.setRenderHint(QPainter.Antialiasing)
    base = QPointF(display_rect.left() + 34.0, display_rect.bottom() - 82.0)
    font = QFont()
    font.setPointSize(8)
    font.setBold(True)
    painter.setFont(font)
    _draw_axis(painter, base, QPointF(base.x() + 34.0, base.y()), QColor("#ef5350"), "X")
    _draw_axis(painter, base, QPointF(base.x(), base.y() - 34.0), QColor("#66bb6a"), "Y")
    _draw_out_of_plane_axis(painter, base, QColor("#42a5f5"))
    painter.restore()


def _draw_axis(
    painter: QPainter,
    base: QPointF,
    end: QPointF,
    color: QColor,
    label: str,
) -> None:
    for pen_color, width in ((QColor(0, 0, 0, 170), 4.0), (color, 2.2)):
        painter.setPen(QPen(pen_color, width))
        painter.drawLine(base, end)
    dx, dy = end.x() - base.x(), end.y() - base.y()
    length = math.hypot(dx, dy)
    if length > 1e-6:
        ux, uy = dx / length, dy / length
        painter.drawLine(end, QPointF(end.x() - ux * 7.0 - uy * 3.85, end.y() - uy * 7.0 + ux * 3.85))
        painter.drawLine(end, QPointF(end.x() - ux * 7.0 + uy * 3.85, end.y() - uy * 7.0 - ux * 3.85))
    x_offset, y_offset = (10.0, 0.0) if label == "X" else (0.0, -10.0)
    rect = QRectF(end.x() + x_offset - 8.0, end.y() + y_offset - 8.0, 16.0, 16.0)
    _draw_shadowed_text(painter, rect, label, color)


def _draw_out_of_plane_axis(
    painter: QPainter,
    center: QPointF,
    color: QColor,
) -> None:
    painter.setBrush(Qt.NoBrush)
    for pen_color, width in ((QColor(0, 0, 0, 180), 4.0), (color, 2.0)):
        painter.setPen(QPen(pen_color, width))
        painter.drawEllipse(center, 7.0, 7.0)
    painter.setPen(Qt.NoPen)
    painter.setBrush(QColor(0, 0, 0, 190))
    painter.drawEllipse(center, 3.2, 3.2)
    painter.setBrush(color)
    painter.drawEllipse(center, 2.2, 2.2)
    _draw_shadowed_text(
        painter,
        QRectF(center.x() - 24.0, center.y() - 26.0, 16.0, 16.0),
        "Z",
        color,
    )


def _draw_shadowed_text(
    painter: QPainter,
    rect: QRectF,
    text: str,
    color: QColor,
) -> None:
    painter.setPen(QPen(QColor(0, 0, 0, 180), 3.0))
    painter.drawText(rect, Qt.AlignCenter, text)
    painter.setPen(QPen(color, 1.0))
    painter.drawText(rect, Qt.AlignCenter, text)


def _scale_label(length_mm: float) -> str:
    if length_mm >= 1.0:
        return f"{length_mm:.0f} mm"
    if length_mm >= 0.01:
        return f"{length_mm * 1000:.0f} µm"
    return f"{length_mm * 1000:.1f} µm"


def _draw_ruler(
    painter: QPainter,
    display_rect: QRect,
    scale_x: float,
    scale_y: float,
    overlay: MeasurementOverlay,
) -> None:
    to_display = _display_mapper(display_rect, scale_x, scale_y)
    painter.save()
    painter.setRenderHint(QPainter.Antialiasing)
    font = QFont()
    font.setPointSize(9)
    painter.setFont(font)
    for first, second in overlay.ruler_segments:
        _draw_ruler_segment(painter, to_display, first, second, overlay)
    if len(overlay.points) == 1 and overlay.hover is not None:
        _draw_ruler_preview(painter, to_display, overlay.points[0], overlay.hover, overlay)
    for point in overlay.points:
        _draw_crosshair(painter, to_display(*point), QColor("#ff9800"), 7.0)
    if overlay.hover is not None:
        _draw_crosshair(painter, to_display(*overlay.hover), QColor("#ffeb3b"), 5.0)
    _draw_hint(
        painter,
        display_rect,
        "Ruler: click to place first point  |  Esc = exit"
        if not overlay.points
        else "Ruler: click second point  |  Ctrl = lock axis  |  Esc = exit",
        width=340.0,
    )
    painter.restore()


def _draw_ruler_segment(
    painter: QPainter,
    to_display,
    first: Point,
    second: Point,
    overlay: MeasurementOverlay,
) -> None:
    p1 = to_display(*first)
    p2 = to_display(*second)
    color = QColor("#ff9800")
    painter.setPen(QPen(color, 2))
    painter.drawLine(p1, p2)
    label = _distance_label(second[0] - first[0], second[1] - first[1], overlay)
    _draw_segment_label(painter, p1, p2, label, color)
    _draw_crosshair(painter, p1, color, 6.0)
    _draw_crosshair(painter, p2, color, 6.0)


def _draw_ruler_preview(
    painter: QPainter,
    to_display,
    first: Point,
    second: Point,
    overlay: MeasurementOverlay,
) -> None:
    p1 = to_display(*first)
    p2 = to_display(*second)
    pen = QPen(QColor("#ffeb3b"), 1.5, Qt.DashLine)
    pen.setDashPattern([6, 4])
    painter.setPen(pen)
    painter.drawLine(p1, p2)
    label = _distance_label(second[0] - first[0], second[1] - first[1], overlay)
    _draw_segment_label(painter, p1, p2, label, QColor("#ffeb3b"))


def _distance_label(dx: float, dy: float, overlay: MeasurementOverlay) -> str:
    scale_x = overlay.mm_per_pixel_x
    scale_y = overlay.mm_per_pixel_y
    if _has_scale(scale_x, scale_y):
        assert scale_x is not None and scale_y is not None
        distance = math.hypot(dx * scale_x, dy * scale_y)
        return _physical_label(distance)
    return f"{math.hypot(dx, dy):.1f} px"


def _draw_segment_label(
    painter: QPainter,
    first: QPointF,
    second: QPointF,
    label: str,
    color: QColor,
) -> None:
    midpoint = QPointF((first.x() + second.x()) / 2.0, (first.y() + second.y()) / 2.0)
    background = QRectF(midpoint.x() - 55.0, midpoint.y() - 10.0, 110.0, 20.0)
    painter.setPen(Qt.NoPen)
    painter.setBrush(QColor(0, 0, 0, 175))
    painter.drawRoundedRect(background, 4, 4)
    painter.setPen(QPen(color, 1))
    painter.drawText(background, Qt.AlignCenter, label)


def _draw_crosshair(
    painter: QPainter,
    point: QPointF,
    color: QColor,
    size: float,
) -> None:
    painter.setPen(QPen(color, 1.5))
    painter.drawLine(QPointF(point.x() - size, point.y()), QPointF(point.x() + size, point.y()))
    painter.drawLine(QPointF(point.x(), point.y() - size), QPointF(point.x(), point.y() + size))


def _draw_rectangles(
    painter: QPainter,
    display_rect: QRect,
    scale_x: float,
    scale_y: float,
    overlay: MeasurementOverlay,
) -> None:
    to_display = _display_mapper(display_rect, scale_x, scale_y)
    painter.save()
    painter.setRenderHint(QPainter.Antialiasing)
    font = QFont()
    font.setPointSize(9)
    painter.setFont(font)
    for first, second in overlay.rect_segments:
        _draw_one_rectangle(painter, to_display, first, second, overlay, QColor("#4fc3f7"), False)
    if len(overlay.points) == 1 and overlay.hover is not None:
        _draw_one_rectangle(
            painter, to_display, overlay.points[0], overlay.hover, overlay, QColor("#ffeb3b"), True
        )
    else:
        _draw_rect_cursor(painter, to_display, overlay)
    _draw_hint(
        painter,
        display_rect,
        "Rect: click first corner  |  Esc = exit"
        if not overlay.points
        else "Rect: click opposite corner  |  Esc = exit",
        width=300.0,
    )
    painter.restore()


def _draw_rect_cursor(painter: QPainter, to_display, overlay: MeasurementOverlay) -> None:
    point = overlay.points[0] if overlay.points else overlay.hover
    if point is None:
        return
    size = 7.0 if overlay.points else 5.0
    color = QColor("#ff9800" if overlay.points else "#ffeb3b")
    _draw_crosshair(painter, to_display(*point), color, size)


def _draw_one_rectangle(
    painter: QPainter,
    to_display,
    first: Point,
    second: Point,
    overlay: MeasurementOverlay,
    color: QColor,
    dashed: bool,
) -> None:
    p1 = to_display(*first)
    p2 = to_display(*second)
    left, right = sorted((p1.x(), p2.x()))
    top, bottom = sorted((p1.y(), p2.y()))
    pen = QPen(color, 1.5)
    if dashed:
        pen.setStyle(Qt.DashLine)
        pen.setDashPattern([6, 4])
    painter.setPen(pen)
    painter.setBrush(Qt.NoBrush)
    painter.drawRect(QRectF(left, top, right - left, bottom - top))
    width_label = _dimension_label(abs(second[0] - first[0]), 0, overlay)
    height_label = _dimension_label(abs(second[1] - first[1]), 1, overlay)
    _draw_width_dimension(painter, left, right, top, width_label, color)
    _draw_height_dimension(painter, right, top, bottom, height_label, color)


def _dimension_label(value_px: float, axis: int, overlay: MeasurementOverlay) -> str:
    scale_x = overlay.mm_per_pixel_x
    scale_y = overlay.mm_per_pixel_y
    if _has_scale(scale_x, scale_y):
        scale = scale_x if axis == 0 else scale_y
        assert scale is not None
        return _physical_label(abs(value_px) * scale)
    return f"{abs(value_px):.1f} px"


def _draw_width_dimension(
    painter: QPainter,
    left: float,
    right: float,
    top: float,
    label: str,
    color: QColor,
) -> None:
    dimension_y = top - 20.0
    painter.setPen(QPen(color, 0.8))
    painter.drawLine(QPointF(left, top), QPointF(left, dimension_y - 5.0))
    painter.drawLine(QPointF(right, top), QPointF(right, dimension_y - 5.0))
    painter.setPen(QPen(color, 1.2))
    painter.drawLine(QPointF(left, dimension_y), QPointF(right, dimension_y))
    _draw_horizontal_arrow(painter, left, dimension_y, -1.0)
    _draw_horizontal_arrow(painter, right, dimension_y, 1.0)
    width = max(100.0, float(len(label)) * 8.0)
    _draw_dimension_label(
        painter,
        QRectF((left + right) / 2.0 - width / 2.0, dimension_y - 19.0, width, 18.0),
        label,
        color,
    )


def _draw_height_dimension(
    painter: QPainter,
    right: float,
    top: float,
    bottom: float,
    label: str,
    color: QColor,
) -> None:
    dimension_x = right + 20.0
    painter.setPen(QPen(color, 0.8))
    painter.drawLine(QPointF(right, top), QPointF(dimension_x + 5.0, top))
    painter.drawLine(QPointF(right, bottom), QPointF(dimension_x + 5.0, bottom))
    painter.setPen(QPen(color, 1.2))
    painter.drawLine(QPointF(dimension_x, top), QPointF(dimension_x, bottom))
    _draw_vertical_arrow(painter, dimension_x, top, -1.0)
    _draw_vertical_arrow(painter, dimension_x, bottom, 1.0)
    width = max(90.0, float(len(label)) * 8.0)
    _draw_dimension_label(
        painter,
        QRectF(dimension_x + 6.0, (top + bottom) / 2.0 - 9.0, width, 18.0),
        label,
        color,
    )


def _draw_horizontal_arrow(painter: QPainter, x: float, y: float, direction: float) -> None:
    painter.drawLine(QPointF(x, y), QPointF(x - direction * 5.0, y - 2.25))
    painter.drawLine(QPointF(x, y), QPointF(x - direction * 5.0, y + 2.25))


def _draw_vertical_arrow(painter: QPainter, x: float, y: float, direction: float) -> None:
    painter.drawLine(QPointF(x, y), QPointF(x - 2.25, y - direction * 5.0))
    painter.drawLine(QPointF(x, y), QPointF(x + 2.25, y - direction * 5.0))


def _draw_dimension_label(
    painter: QPainter,
    rect: QRectF,
    label: str,
    color: QColor,
) -> None:
    painter.setPen(Qt.NoPen)
    painter.setBrush(QColor(0, 0, 0, 175))
    painter.drawRoundedRect(rect, 3, 3)
    painter.setPen(QPen(color, 1))
    painter.drawText(rect, Qt.AlignCenter, label)


def _draw_hint(
    painter: QPainter,
    display_rect: QRect,
    text: str,
    *,
    width: float,
) -> None:
    height = 22.0
    rect = QRectF(
        display_rect.left() + (display_rect.width() - width) / 2.0,
        float(display_rect.bottom()) - height - 36.0,
        width,
        height,
    )
    painter.setPen(Qt.NoPen)
    painter.setBrush(QColor(0, 0, 0, 150))
    painter.drawRoundedRect(rect, 5, 5)
    painter.setPen(QPen(QColor("#ffeb3b"), 1))
    painter.drawText(rect, Qt.AlignCenter, text)


def _display_mapper(display_rect: QRect, scale_x: float, scale_y: float):
    center_x = display_rect.left() + display_rect.width() / 2.0
    center_y = display_rect.top() + display_rect.height() / 2.0

    def to_display(dx: float, dy: float) -> QPointF:
        return QPointF(center_x + dx * scale_x, center_y - dy * scale_y)

    return to_display


def _has_scale(scale_x: float | None, scale_y: float | None) -> bool:
    return scale_x is not None and scale_y is not None and scale_x > 0 and scale_y > 0


def _physical_label(distance_mm: float) -> str:
    return f"{distance_mm * 1000:.1f} µm" if distance_mm < 1.0 else f"{distance_mm:.4f} mm"


__all__ = [
    "AlignmentOverlay",
    "MeasurementOverlay",
    "TargetOverlay",
    "draw_alignment_overlay",
    "draw_measurement_overlay",
    "draw_axis_triad",
    "draw_scale_bar",
    "draw_target_overlay",
]
