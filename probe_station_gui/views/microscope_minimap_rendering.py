"""Overlay drawing and geometry mapping for the microscope minimap."""

from __future__ import annotations

from dataclasses import dataclass
import math

from PySide6.QtCore import QPoint, QPointF, QRect, QRectF, QSize, Qt
from PySide6.QtGui import (
    QColor,
    QFont,
    QPainter,
    QPainterPath,
    QPen,
    QPolygonF,
    QPixmap,
    QImage,
)

from probe_station_gui.design.model import DesignDocument, MeasurementTarget
from probe_station_gui.route.model import MeasurementRoute


@dataclass(frozen=True)
class MinimapScene:
    document: DesignDocument | None
    bounds: tuple[float, float, float, float] = (0.0, 0.0, 1.0, 1.0)
    targets: tuple[MeasurementTarget, ...] = ()
    selected_target_id: str | None = None
    probe_route: MeasurementRoute | None = None
    selected_route_point_index: int = -1
    selected_design_point: tuple[float, float] | None = None
    current_design_position: tuple[float, float] | None = None
    fov_design_size: tuple[float, float] | None = None
    source_design_marks: tuple[tuple[float, float], ...] = ()
    check_design_marks: tuple[tuple[float, float], ...] = ()


class MinimapRendering:
    """Render immutable overlay snapshots and map design coordinates."""

    _MINIMAP_MARGIN = 16
    _MINIMAP_MIN_SIZE = 160
    _MINIMAP_MAX_SIZE = 240
    _PROBE_ROUTE_DETAIL_POINT_LIMIT = 300
    _PROBE_ROUTE_LABEL_POINT_LIMIT = 150

    def __init__(self) -> None:
        self._scene = MinimapScene(None)
        self._state_token: tuple[object, ...] | None = None
        self._design_document: DesignDocument | None = None
        self._bounds = (0.0, 0.0, 1.0, 1.0)
        self._design_targets: tuple[MeasurementTarget, ...] = ()
        self._selected_target_id: str | None = None
        self._probe_route: MeasurementRoute | None = None
        self._probe_route_snapshot: tuple[object, ...] | None = None
        self._probe_route_snapshot_token: tuple[object, ...] | None = None
        self._selected_route_point_index = -1
        self._selected_design_point: tuple[float, float] | None = None
        self._current_design_position: tuple[float, float] | None = None
        self._fov_design_size: tuple[float, float] | None = None
        self._source_design_marks: tuple[tuple[float, float], ...] = ()
        self._check_design_marks: tuple[tuple[float, float], ...] = ()
        self._minimap_static_overlay: QPixmap | None = None
        self._minimap_static_overlay_key: tuple[object, ...] | None = None
        self._last_display_rect: QRect | None = None

    def configure(self, scene: MinimapScene) -> bool:
        route_snapshot = self._route_snapshot(scene.probe_route)
        state = (
            id(scene.document),
            scene.bounds,
            tuple((target.id, target.design_center) for target in scene.targets),
            scene.selected_target_id,
            route_snapshot,
            scene.selected_route_point_index,
            scene.selected_design_point,
            scene.current_design_position,
            scene.fov_design_size,
            scene.source_design_marks,
            scene.check_design_marks,
        )
        previous = self._state_token
        if scene.document is not self._design_document:
            self._minimap_static_overlay = None
            self._minimap_static_overlay_key = None
            self._probe_route_snapshot_token = None
        self._scene = scene
        self._design_document = scene.document
        self._bounds = scene.bounds
        self._design_targets = scene.targets
        self._selected_target_id = scene.selected_target_id
        self._probe_route = scene.probe_route
        self._probe_route_snapshot = route_snapshot
        self._selected_route_point_index = scene.selected_route_point_index
        self._selected_design_point = scene.selected_design_point
        self._current_design_position = scene.current_design_position
        self._fov_design_size = scene.fov_design_size
        self._source_design_marks = scene.source_design_marks
        self._check_design_marks = scene.check_design_marks
        self._state_token = state
        return state != previous

    @property
    def last_display_rect(self) -> QRect | None:
        return (
            QRect(self._last_display_rect)
            if self._last_display_rect is not None
            else None
        )

    def content_size(self, display_rect: QRect) -> QSize:
        return self._content_rect(self._outer_rect(display_rect)).size()

    @classmethod
    def design_rect_for_bounds(
        cls, rect: QRect, bounds: tuple[float, float, float, float]
    ) -> QRectF:
        return cls._minimap_design_rect_for_bounds(rect, bounds)

    def draw(
        self,
        painter: QPainter,
        display_rect: QRect,
        background: QPixmap | None,
    ) -> None:
        if self._design_document is None:
            return
        self._last_display_rect = QRect(display_rect)
        outer_rect = self._outer_rect(display_rect)
        if outer_rect.width() <= 0 or outer_rect.height() <= 0:
            return

        painter.save()
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(8, 12, 18, 210))
        painter.drawRoundedRect(outer_rect, 10, 10)

        title_rect = QRect(
            outer_rect.left() + 10, outer_rect.top() + 6, outer_rect.width() - 20, 18
        )
        painter.setPen(QPen(QColor("#cfd8dc"), 1))
        painter.drawText(title_rect, Qt.AlignLeft | Qt.AlignVCenter, "design")

        content_rect = QRect(
            outer_rect.left() + 8,
            outer_rect.top() + 26,
            outer_rect.width() - 16,
            outer_rect.height() - 34,
        )
        if content_rect.width() <= 0 or content_rect.height() <= 0:
            painter.restore()
            return

        if background is not None:
            if self._design_document.file_backed:
                background_rect = self._minimap_design_rect_for_bounds(
                    content_rect,
                    self._bounds,
                )
                painter.drawPixmap(
                    background_rect,
                    background,
                    QRectF(background.rect()),
                )
            else:
                painter.drawPixmap(content_rect.topLeft(), background)

        static_overlay = self._minimap_static_overlay_for_size(content_rect.size())
        if static_overlay is not None:
            painter.drawPixmap(content_rect.topLeft(), static_overlay)
        self._draw_design_position(painter, content_rect)

        painter.setPen(QPen(QColor("#546e7a"), 1))
        painter.setBrush(Qt.NoBrush)
        painter.drawRoundedRect(outer_rect, 10, 10)
        painter.restore()

    def _minimap_static_overlay_for_size(self, size: QSize) -> QPixmap | None:
        if self._design_document is None:
            self._minimap_static_overlay = None
            self._minimap_static_overlay_key = None
            return None
        cache_key = self._minimap_static_overlay_cache_key(size)
        if (
            self._minimap_static_overlay_key == cache_key
            and self._minimap_static_overlay is not None
        ):
            return self._minimap_static_overlay

        image = QImage(size, QImage.Format_ARGB32_Premultiplied)
        image.fill(Qt.transparent)
        painter = QPainter(image)
        rect = QRect(QPoint(0, 0), size)
        self._draw_design_route(painter, rect)
        self._draw_probe_route(painter, rect)
        self._draw_design_marks(painter, rect)
        painter.end()
        self._minimap_static_overlay = QPixmap.fromImage(image)
        self._minimap_static_overlay_key = cache_key
        return self._minimap_static_overlay

    def _minimap_static_overlay_cache_key(self, size: QSize) -> tuple[object, ...]:
        return (
            id(self._design_document),
            int(size.width()),
            int(size.height()),
            tuple((target.id, target.design_center) for target in self._design_targets),
            self._selected_target_id,
            self._probe_route_snapshot,
            self._selected_route_point_index,
            self._selected_design_point,
            tuple(self._source_design_marks),
            tuple(self._check_design_marks),
        )

    def _draw_design_route(self, painter: QPainter, rect: QRect) -> None:
        if not self._design_targets:
            return
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(QPen(QColor("#4dd0e1"), 1.5))
        path = QPainterPath()
        start = self._map_design_point_to_rect(
            self._design_targets[0].design_center, rect
        )
        path.moveTo(start)
        for target in self._design_targets[1:]:
            path.lineTo(self._map_design_point_to_rect(target.design_center, rect))
        painter.drawPath(path)
        for target in self._design_targets:
            point = self._map_design_point_to_rect(target.design_center, rect)
            painter.setPen(QPen(QColor("#4dd0e1"), 1))
            painter.setBrush(QColor(77, 208, 225, 160))
            radius = 3.5
            if target.id == self._selected_target_id:
                painter.setPen(QPen(QColor("#ff7043"), 2))
                painter.setBrush(QColor(255, 112, 67, 180))
                radius = 5.0
            painter.drawEllipse(point, radius, radius)
        if self._selected_design_point is not None:
            painter.setPen(QPen(QColor("#ffd54f"), 2))
            painter.setBrush(QColor(255, 213, 79, 160))
            painter.drawEllipse(
                self._map_design_point_to_rect(self._selected_design_point, rect),
                4.0,
                4.0,
            )
        painter.restore()

    def _draw_probe_route(self, painter: QPainter, rect: QRect) -> None:
        route = self._probe_route
        if route is None or not route.points:
            return
        points = [point for point in route.points if point.enabled]
        if not points:
            return
        draw_details = len(points) <= self._PROBE_ROUTE_DETAIL_POINT_LIMIT
        draw_labels = len(points) <= self._PROBE_ROUTE_LABEL_POINT_LIMIT

        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)

        center_path = QPainterPath()
        first_point = self._map_design_point_to_rect(points[0].camera_center, rect)
        center_path.moveTo(first_point)
        for route_point in points[1:]:
            center_path.lineTo(
                self._map_design_point_to_rect(route_point.camera_center, rect)
            )
        painter.setPen(QPen(QColor("#29b6f6"), 2.0))
        painter.drawPath(center_path)
        painter.setPen(QPen(QColor("#e1f5fe"), 1.4))
        painter.setBrush(QColor(225, 245, 254, 210))
        if draw_details:
            for start, end in zip(points, points[1:]):
                self._draw_route_arrowhead(
                    painter,
                    self._map_design_point_to_rect(start.camera_center, rect),
                    self._map_design_point_to_rect(end.camera_center, rect),
                )

        connector_pen = QPen(QColor(207, 216, 220, 120), 1.0)
        connector_pen.setStyle(Qt.DotLine)
        needle_colors = (QColor("#ffd54f"), QColor("#ec407a"))
        font = QFont()
        font.setPointSize(7)
        painter.setFont(font)

        for route_index, route_point in enumerate(route.points):
            if not route_point.enabled:
                continue
            center = self._map_design_point_to_rect(route_point.camera_center, rect)
            is_selected = route_index == self._selected_route_point_index
            painter.setPen(QPen(QColor("#ff7043" if is_selected else "#29b6f6"), 2.0))
            painter.setBrush(QColor(41, 182, 246, 170))
            radius = 5.2 if is_selected else 4.0
            painter.drawEllipse(center, radius, radius)

            if draw_labels or is_selected:
                label_rect = QRectF(center.x() + 5.0, center.y() - 15.0, 30.0, 13.0)
                painter.setPen(QPen(QColor(0, 0, 0, 180), 3))
                painter.drawText(
                    label_rect,
                    Qt.AlignLeft | Qt.AlignVCenter,
                    str(route_index + 1),
                )
                painter.setPen(QPen(QColor("#e3f2fd"), 1))
                painter.drawText(
                    label_rect,
                    Qt.AlignLeft | Qt.AlignVCenter,
                    str(route_index + 1),
                )

            if not draw_details and not is_selected:
                continue
            hits = route.needle_hits_for_point(route_point)
            for needle_index, (_offset, hit) in enumerate(hits[:2]):
                needle_point = self._map_design_point_to_rect(hit, rect)
                painter.setPen(connector_pen)
                painter.drawLine(center, needle_point)
                color = needle_colors[min(needle_index, len(needle_colors) - 1)]
                painter.setPen(QPen(color, 1.6))
                painter.setBrush(QColor(color.red(), color.green(), color.blue(), 120))
                if needle_index == 0:
                    painter.drawLine(
                        QPointF(needle_point.x() - 4.0, needle_point.y()),
                        QPointF(needle_point.x() + 4.0, needle_point.y()),
                    )
                    painter.drawLine(
                        QPointF(needle_point.x(), needle_point.y() - 4.0),
                        QPointF(needle_point.x(), needle_point.y() + 4.0),
                    )
                    painter.drawEllipse(needle_point, 3.0, 3.0)
                else:
                    painter.drawLine(
                        QPointF(needle_point.x() - 4.0, needle_point.y() - 4.0),
                        QPointF(needle_point.x() + 4.0, needle_point.y() + 4.0),
                    )
                    painter.drawLine(
                        QPointF(needle_point.x() - 4.0, needle_point.y() + 4.0),
                        QPointF(needle_point.x() + 4.0, needle_point.y() - 4.0),
                    )
                    painter.drawEllipse(needle_point, 3.0, 3.0)
        painter.restore()

    @staticmethod
    def _draw_route_arrowhead(
        painter: QPainter,
        start: QPointF,
        end: QPointF,
    ) -> None:
        dx = float(end.x() - start.x())
        dy = float(end.y() - start.y())
        length = math.hypot(dx, dy)
        if length <= 1e-6:
            return
        ux = dx / length
        uy = dy / length
        px = -uy
        py = ux
        arrow_len = 11.0
        arrow_width = 7.0
        if length < arrow_len * 2.0:
            arrow_len = max(5.0, length * 0.32)
            arrow_width = min(arrow_width, arrow_len * 0.75)
        tip_x = float(start.x() + dx * 0.58)
        tip_y = float(start.y() + dy * 0.58)
        base_x = tip_x - ux * arrow_len
        base_y = tip_y - uy * arrow_len
        notch_x = base_x + ux * arrow_len * 0.22
        notch_y = base_y + uy * arrow_len * 0.22
        polygon = QPolygonF(
            [
                QPointF(tip_x, tip_y),
                QPointF(
                    base_x + px * arrow_width * 0.5, base_y + py * arrow_width * 0.5
                ),
                QPointF(notch_x, notch_y),
                QPointF(
                    base_x - px * arrow_width * 0.5, base_y - py * arrow_width * 0.5
                ),
            ]
        )
        painter.drawPolygon(polygon)

    def _draw_design_marks(self, painter: QPainter, rect: QRect) -> None:
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)
        for point in self._source_design_marks:
            mapped = self._map_design_point_to_rect(point, rect)
            painter.setPen(QPen(QColor("#ffb300"), 2))
            painter.setBrush(QColor(255, 179, 0, 170))
            painter.drawEllipse(mapped, 4.0, 4.0)
        for point in self._check_design_marks:
            mapped = self._map_design_point_to_rect(point, rect)
            painter.setPen(QPen(QColor("#ab47bc"), 2))
            painter.drawLine(
                QPointF(mapped.x() - 4.0, mapped.y()),
                QPointF(mapped.x() + 4.0, mapped.y()),
            )
            painter.drawLine(
                QPointF(mapped.x(), mapped.y() - 4.0),
                QPointF(mapped.x(), mapped.y() + 4.0),
            )
        painter.restore()

    def _draw_design_position(self, painter: QPainter, rect: QRect) -> None:
        if self._current_design_position is None:
            return
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setClipRect(QRectF(rect))
        center, inside = self._map_design_point_to_rect_clamped(
            self._current_design_position, rect
        )
        if self._fov_design_size is not None:
            half_w = abs(float(self._fov_design_size[0])) * 0.5
            half_h = abs(float(self._fov_design_size[1])) * 0.5
            top_left = self._map_design_point_to_rect(
                (
                    self._current_design_position[0] - half_w,
                    self._current_design_position[1] + half_h,
                ),
                rect,
            )
            bottom_right = self._map_design_point_to_rect(
                (
                    self._current_design_position[0] + half_w,
                    self._current_design_position[1] - half_h,
                ),
                rect,
            )
            fov_rect = self._visible_minimap_fov_rect(
                QRectF(top_left, bottom_right),
                rect,
            )
            painter.setPen(QPen(QColor("#81c784"), 1.2))
            painter.setBrush(Qt.NoBrush)
            if (
                fov_rect.isValid()
                and fov_rect.width() > 0.0
                and fov_rect.height() > 0.0
            ):
                painter.drawRect(fov_rect)
        painter.setPen(QPen(QColor(0, 0, 0, 150), 3.6))
        painter.drawLine(
            QPointF(rect.left() + 4.0, center.y()),
            QPointF(rect.right() - 4.0, center.y()),
        )
        painter.drawLine(
            QPointF(center.x(), rect.top() + 4.0),
            QPointF(center.x(), rect.bottom() - 4.0),
        )
        painter.setPen(QPen(QColor("#81c784"), 1.6))
        painter.drawLine(
            QPointF(rect.left() + 4.0, center.y()),
            QPointF(rect.right() - 4.0, center.y()),
        )
        painter.drawLine(
            QPointF(center.x(), rect.top() + 4.0),
            QPointF(center.x(), rect.bottom() - 4.0),
        )
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(129, 199, 132, 90))
        painter.drawEllipse(center, 8.0, 8.0)
        painter.setBrush(QColor(232, 245, 233, 230))
        painter.drawEllipse(center, 2.8, 2.8)
        painter.setPen(QPen(QColor("#e8f5e9"), 1.6))
        painter.drawLine(
            QPointF(center.x() - 7.0, center.y()),
            QPointF(center.x() + 7.0, center.y()),
        )
        painter.drawLine(
            QPointF(center.x(), center.y() - 7.0),
            QPointF(center.x(), center.y() + 7.0),
        )
        if not inside:
            label_rect = QRectF(center.x() - 18.0, center.y() - 22.0, 36.0, 14.0)
            painter.setPen(QPen(QColor("#e8f5e9"), 1))
            painter.drawText(label_rect, Qt.AlignCenter, "OUT")
        painter.restore()

    def _map_design_point_to_rect(
        self, point: tuple[float, float], rect: QRect
    ) -> QPointF:
        assert self._design_document is not None
        return self._map_design_point_to_rect_for_bounds(
            point,
            rect,
            self._bounds,
        )

    @staticmethod
    def _map_design_point_to_rect_for_bounds(
        point: tuple[float, float],
        rect: QRect,
        bounds: tuple[float, float, float, float],
    ) -> QPointF:
        left, bottom, right, top = bounds
        width = max(right - left, 1e-9)
        height = max(top - bottom, 1e-9)
        fitted = MinimapRendering._minimap_design_rect_for_bounds(rect, bounds)
        x_pos = fitted.left() + (point[0] - left) * fitted.width() / width
        y_pos = fitted.top() + (top - point[1]) * fitted.height() / height
        return QPointF(float(x_pos), float(y_pos))

    @staticmethod
    def _minimap_design_rect_for_bounds(
        rect: QRect,
        bounds: tuple[float, float, float, float],
    ) -> QRectF:
        # This six-pixel inset is shared by raster placement and coordinate
        # mapping; changing either side independently would shift click targets.
        left, bottom, right, top = bounds
        width = max(float(right) - float(left), 1e-9)
        height = max(float(top) - float(bottom), 1e-9)
        pad = 6.0
        usable_width = max(float(rect.width()) - 2.0 * pad, 1.0)
        usable_height = max(float(rect.height()) - 2.0 * pad, 1.0)
        scale = min(usable_width / width, usable_height / height)
        return QRectF(
            float(rect.left()) + (float(rect.width()) - width * scale) * 0.5,
            float(rect.top()) + (float(rect.height()) - height * scale) * 0.5,
            width * scale,
            height * scale,
        )

    def _map_rect_point_to_design(
        self, point: QPoint | QPointF, rect: QRect
    ) -> tuple[float, float]:
        assert self._design_document is not None
        left, bottom, right, top = self._bounds
        width = max(right - left, 1e-9)
        height = max(top - bottom, 1e-9)
        fitted = self._minimap_design_rect_for_bounds(
            rect,
            self._bounds,
        )
        x_value = left + (float(point.x()) - fitted.left()) * width / fitted.width()
        y_value = top - (float(point.y()) - fitted.top()) * height / fitted.height()
        x_value = min(max(x_value, left), right)
        y_value = min(max(y_value, bottom), top)
        return (float(x_value), float(y_value))

    def _map_design_point_to_rect_clamped(
        self, point: tuple[float, float], rect: QRect
    ) -> tuple[QPointF, bool]:
        mapped = self._map_design_point_to_rect(point, rect)
        left = float(rect.left() + 6)
        right = float(rect.right() - 6)
        top = float(rect.top() + 6)
        bottom = float(rect.bottom() - 6)
        clamped_x = min(max(mapped.x(), left), right)
        clamped_y = min(max(mapped.y(), top), bottom)
        inside = (
            abs(clamped_x - mapped.x()) < 1e-6 and abs(clamped_y - mapped.y()) < 1e-6
        )
        return QPointF(clamped_x, clamped_y), inside

    def map_click(
        self,
        point: QPoint | QPointF,
        display_rect: QRect,
        *,
        require_inside: bool = True,
    ) -> tuple[float, float] | None:
        if self._design_document is None:
            return None
        mapped = point.toPoint() if isinstance(point, QPointF) else point
        outer_rect = self._outer_rect(display_rect)
        if require_inside and not outer_rect.contains(mapped):
            return None
        content_rect = self._content_rect(outer_rect)
        if content_rect.width() <= 0 or content_rect.height() <= 0:
            return None
        return self._map_rect_point_to_design(point, content_rect)

    def contains(self, point: QPoint | QPointF, display_rect: QRect) -> bool:
        mapped = point.toPoint() if isinstance(point, QPointF) else point
        return self._design_document is not None and self._outer_rect(
            display_rect
        ).contains(mapped)

    @staticmethod
    def _visible_minimap_fov_rect(fov_rect: QRectF, content_rect: QRect) -> QRectF:
        bounds = QRectF(content_rect).adjusted(1.0, 1.0, -1.0, -1.0)
        return fov_rect.normalized().intersected(bounds)

    @staticmethod
    def _layer_color(layer_key: tuple[int, int]) -> QColor:
        hue = int((layer_key[0] * 57 + layer_key[1] * 19) % 360)
        return QColor.fromHsv(hue, 120, 145, 180)

    @classmethod
    def _outer_rect(cls, display_rect: QRect) -> QRect:
        size = int(min(display_rect.width(), display_rect.height()) * 0.24)
        size = max(cls._MINIMAP_MIN_SIZE, min(size, cls._MINIMAP_MAX_SIZE))
        return QRect(
            display_rect.right() - size - cls._MINIMAP_MARGIN,
            display_rect.top() + cls._MINIMAP_MARGIN,
            size,
            size,
        )

    @staticmethod
    def _content_rect(outer_rect: QRect) -> QRect:
        return QRect(
            outer_rect.left() + 8,
            outer_rect.top() + 26,
            outer_rect.width() - 16,
            outer_rect.height() - 34,
        )

    def _route_snapshot(
        self, route: MeasurementRoute | None
    ) -> tuple[object, ...] | None:
        if route is None:
            self._probe_route_snapshot_token = None
            return None
        points = route.points
        token = (
            id(route),
            route.name,
            str(route.path) if route.path is not None else "",
            route.updated_at_utc,
            len(points),
            id(points[0]) if points else 0,
            id(points[-1]) if points else 0,
            tuple(
                (offset.id, offset.dx, offset.dy, offset.source)
                for offset in route.needle_offsets
            ),
        )
        if (
            token == self._probe_route_snapshot_token
            and self._probe_route_snapshot is not None
        ):
            return self._probe_route_snapshot
        self._probe_route_snapshot_token = token
        return (
            route.name,
            str(route.path) if route.path is not None else "",
            tuple(
                (offset.id, offset.dx, offset.dy, offset.source)
                for offset in route.needle_offsets
            ),
            tuple(
                (
                    point.id,
                    point.label,
                    point.camera_center,
                    point.enabled,
                )
                for point in route.points
            ),
        )


__all__ = ["MinimapRendering", "MinimapScene"]
