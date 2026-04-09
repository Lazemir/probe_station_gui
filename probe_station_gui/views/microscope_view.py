"""Microscope view widget displaying frames and mouse interactions."""

from __future__ import annotations

from PySide6.QtCore import QPoint, QPointF, QRect, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QImage, QMouseEvent, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import QWidget

from ..design_model import DesignDocument, MeasurementTarget


class MicroscopeView(QWidget):
    """Widget that renders camera frames with overlay graphics."""

    clicked: Signal = Signal(float, float, float, float)
    hovered: Signal = Signal(float, float, float, float)
    hover_left: Signal = Signal()
    design_minimap_double_clicked: Signal = Signal()

    _MINIMAP_MARGIN = 16
    _MINIMAP_MIN_SIZE = 160
    _MINIMAP_MAX_SIZE = 240

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Microscope Qt")
        self.setMinimumSize(960, 720)
        self.setMouseTracking(True)
        self._pix: QPixmap | None = None
        self._target_rel: tuple[float, float] | None = None
        self._alignment_mode = False
        self._alignment_points: list[tuple[float, float]] = []
        self._alignment_instruction = ""
        self._display_rect: QRect | None = None
        self._design_document: DesignDocument | None = None
        self._design_targets: list[MeasurementTarget] = []
        self._selected_target_id: str | None = None
        self._selected_design_point: tuple[float, float] | None = None
        self._current_design_position: tuple[float, float] | None = None
        self._fov_design_size: tuple[float, float] | None = None
        self._source_design_marks: list[tuple[float, float]] = []
        self._check_design_marks: list[tuple[float, float]] = []
        self._minimap_background: QPixmap | None = None
        self._minimap_cache_key: tuple[object, QSize] | None = None
        self._minimap_rect: QRect | None = None

    def set_frame(self, qimg: QImage) -> None:
        self._pix = QPixmap.fromImage(qimg)
        self.update()

    def set_design_minimap_data(
        self,
        *,
        document: DesignDocument | None,
        targets: list[MeasurementTarget],
        selected_target_id: str | None,
        selected_design_point: tuple[float, float] | None,
        current_design_position: tuple[float, float] | None,
        fov_design_size: tuple[float, float] | None,
        source_design_marks: list[tuple[float, float]],
        check_design_marks: list[tuple[float, float]],
    ) -> None:
        """Update the camera-corner minimap state."""

        if document is not self._design_document:
            self._minimap_cache_key = None
            self._minimap_background = None
        previous_state = (
            self._design_document,
            tuple(target.id for target in self._design_targets),
            self._selected_target_id,
            self._selected_design_point,
            self._current_design_position,
            self._fov_design_size,
            tuple(self._source_design_marks),
            tuple(self._check_design_marks),
        )
        self._design_document = document
        self._design_targets = list(targets)
        self._selected_target_id = selected_target_id
        self._selected_design_point = selected_design_point
        self._current_design_position = current_design_position
        self._fov_design_size = fov_design_size
        self._source_design_marks = list(source_design_marks)
        self._check_design_marks = list(check_design_marks)
        current_state = (
            self._design_document,
            tuple(target.id for target in self._design_targets),
            self._selected_target_id,
            self._selected_design_point,
            self._current_design_position,
            self._fov_design_size,
            tuple(self._source_design_marks),
            tuple(self._check_design_marks),
        )
        if current_state != previous_state:
            self.update()

    def paintEvent(self, event) -> None:  # type: ignore[override]
        painter = QPainter(self)
        painter.fillRect(self.rect(), Qt.black)
        if self._pix:
            scaled = self._pix.scaled(self.size(), Qt.KeepAspectRatio)
            pos_x = (self.width() - scaled.width()) // 2
            pos_y = (self.height() - scaled.height()) // 2
            painter.drawPixmap(pos_x, pos_y, scaled)
            self._display_rect = QRect(pos_x, pos_y, scaled.width(), scaled.height())

            painter.setRenderHint(QPainter.Antialiasing)
            if self._display_rect:
                center_x = self._display_rect.center().x()
                center_y = self._display_rect.center().y()
            else:
                center_x = self.width() // 2
                center_y = self.height() // 2

            painter.setPen(QPen(QColor("cyan"), 1))
            if self._display_rect:
                painter.drawLine(
                    self._display_rect.left(),
                    center_y,
                    self._display_rect.right(),
                    center_y,
                )
                painter.drawLine(
                    center_x,
                    self._display_rect.top(),
                    center_x,
                    self._display_rect.bottom(),
                )
            else:
                painter.drawLine(0, center_y, self.width(), center_y)
                painter.drawLine(center_x, 0, center_x, self.height())
            painter.drawEllipse(QPoint(int(center_x), int(center_y)), 4, 4)

            if self._target_rel and self._display_rect:
                rel_x, rel_y = self._target_rel
                target_x = self._display_rect.left() + rel_x * self._display_rect.width()
                target_y = self._display_rect.top() + rel_y * self._display_rect.height()
                painter.setPen(QPen(QColor("red"), 1))
                painter.drawLine(
                    self._display_rect.left(),
                    int(target_y),
                    self._display_rect.right(),
                    int(target_y),
                )
                painter.drawLine(
                    int(target_x),
                    self._display_rect.top(),
                    int(target_x),
                    self._display_rect.bottom(),
                )
                painter.drawEllipse(QPoint(int(target_x), int(target_y)), 6, 6)

            if self._alignment_mode and self._display_rect:
                painter.setPen(QPen(QColor("#ffd54f"), 2))
                point_positions: list[tuple[int, int]] = []
                for index, (rel_x, rel_y) in enumerate(self._alignment_points, start=1):
                    point_x = int(
                        self._display_rect.left() + rel_x * self._display_rect.width()
                    )
                    point_y = int(
                        self._display_rect.top() + rel_y * self._display_rect.height()
                    )
                    point_positions.append((point_x, point_y))
                    painter.drawEllipse(QPoint(point_x, point_y), 7, 7)
                    painter.drawText(point_x + 10, point_y - 10, str(index))

                if len(point_positions) == 2:
                    painter.setPen(QPen(QColor("#ffee58"), 2))
                    painter.drawLine(
                        point_positions[0][0],
                        point_positions[0][1],
                        point_positions[1][0],
                        point_positions[1][1],
                    )

                if self._alignment_instruction:
                    painter.setPen(Qt.NoPen)
                    painter.setBrush(QColor(0, 0, 0, 170))
                    painter.drawRoundedRect(12, 12, min(self.width() - 24, 430), 46, 8, 8)
                    painter.setPen(QPen(QColor("#fff3cd"), 1))
                    painter.drawText(
                        QRect(20, 18, max(0, self.width() - 40), 34),
                        Qt.AlignLeft | Qt.AlignVCenter,
                        self._alignment_instruction,
                    )

            if self._display_rect:
                self._draw_design_minimap(painter, self._display_rect)
        painter.end()

    def mousePressEvent(self, event: QMouseEvent) -> None:  # type: ignore[override]
        if event.button() != Qt.LeftButton or not self._pix or not self._display_rect:
            return
        point = event.position().toPoint()
        if self._minimap_rect is not None and self._minimap_rect.contains(point):
            return
        if not self._display_rect.contains(point):
            return

        scale_x = self._display_rect.width() / self._pix.width()
        scale_y = self._display_rect.height() / self._pix.height()
        if scale_x <= 0 or scale_y <= 0:
            return

        image_x = (event.position().x() - self._display_rect.left()) / scale_x
        image_y = (event.position().y() - self._display_rect.top()) / scale_y
        center_x = self._pix.width() / 2
        center_y = self._pix.height() / 2
        dx = image_x - center_x
        dy = center_y - image_y

        rel_x = (event.position().x() - self._display_rect.left()) / self._display_rect.width()
        rel_y = (event.position().y() - self._display_rect.top()) / self._display_rect.height()
        rel_x = max(0.0, min(1.0, rel_x))
        rel_y = max(0.0, min(1.0, rel_y))
        if not self._alignment_mode:
            self._target_rel = (rel_x, rel_y)
        self.clicked.emit(dx, dy, rel_x, rel_y)
        self.update()

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:  # type: ignore[override]
        point = event.position().toPoint()
        if (
            event.button() == Qt.LeftButton
            and self._minimap_rect is not None
            and self._minimap_rect.contains(point)
        ):
            self.design_minimap_double_clicked.emit()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # type: ignore[override]
        if not self._pix or not self._display_rect:
            self.hover_left.emit()
            return
        point = event.position().toPoint()
        if not self._display_rect.contains(point):
            self.hover_left.emit()
            return

        scale_x = self._display_rect.width() / self._pix.width()
        scale_y = self._display_rect.height() / self._pix.height()
        if scale_x <= 0 or scale_y <= 0:
            self.hover_left.emit()
            return

        image_x = (event.position().x() - self._display_rect.left()) / scale_x
        image_y = (event.position().y() - self._display_rect.top()) / scale_y
        center_x = self._pix.width() / 2
        center_y = self._pix.height() / 2
        dx = image_x - center_x
        dy = center_y - image_y
        rel_x = (event.position().x() - self._display_rect.left()) / self._display_rect.width()
        rel_y = (event.position().y() - self._display_rect.top()) / self._display_rect.height()
        rel_x = max(0.0, min(1.0, rel_x))
        rel_y = max(0.0, min(1.0, rel_y))
        self.hovered.emit(dx, dy, rel_x, rel_y)

    def leaveEvent(self, event) -> None:  # type: ignore[override]
        self.hover_left.emit()
        super().leaveEvent(event)

    def clear_target_cross(self) -> None:
        """Remove the movable cross overlay."""

        self._target_rel = None
        self.update()

    def set_alignment_mode(self, enabled: bool) -> None:
        """Enable or disable the chip alignment overlay."""

        self._alignment_mode = enabled
        if not enabled:
            self._alignment_points = []
            self._alignment_instruction = ""
        self.update()

    def set_alignment_points(self, points: list[tuple[float, float]]) -> None:
        """Replace the active alignment marker list."""

        self._alignment_points = list(points[:2])
        self.update()

    def set_alignment_instruction(self, instruction: str) -> None:
        """Update the alignment mode helper text."""

        self._alignment_instruction = instruction
        self.update()

    def clear_alignment_points(self) -> None:
        """Remove alignment markers from the overlay."""

        self._alignment_points = []
        self.update()

    def _draw_design_minimap(self, painter: QPainter, display_rect: QRect) -> None:
        if self._design_document is None:
            return
        size = int(min(display_rect.width(), display_rect.height()) * 0.24)
        size = max(self._MINIMAP_MIN_SIZE, min(size, self._MINIMAP_MAX_SIZE))
        outer_rect = QRect(
            display_rect.right() - size - self._MINIMAP_MARGIN,
            display_rect.top() + self._MINIMAP_MARGIN,
            size,
            size,
        )
        self._minimap_rect = QRect(outer_rect)
        if outer_rect.width() <= 0 or outer_rect.height() <= 0:
            return

        painter.save()
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(8, 12, 18, 210))
        painter.drawRoundedRect(outer_rect, 10, 10)

        title_rect = QRect(outer_rect.left() + 10, outer_rect.top() + 6, outer_rect.width() - 20, 18)
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

        background = self._design_background_for_size(content_rect.size())
        if background is not None:
            painter.drawPixmap(content_rect.topLeft(), background)

        self._draw_design_route(painter, content_rect)
        self._draw_design_marks(painter, content_rect)
        self._draw_design_position(painter, content_rect)

        painter.setPen(QPen(QColor("#546e7a"), 1))
        painter.setBrush(Qt.NoBrush)
        painter.drawRoundedRect(outer_rect, 10, 10)
        painter.restore()

    def _design_background_for_size(self, size: QSize) -> QPixmap | None:
        if self._design_document is None:
            return None
        cache_key = (self._design_document, size)
        if self._minimap_cache_key == cache_key and self._minimap_background is not None:
            return self._minimap_background

        pixmap = QPixmap(size)
        pixmap.fill(Qt.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.Antialiasing)
        for layer_key, polygons in self._design_document.visible_polygons().items():
            painter.setPen(QPen(self._layer_color(layer_key), 1))
            painter.setBrush(Qt.NoBrush)
            for polygon in polygons:
                if len(polygon) < 2:
                    continue
                path = QPainterPath()
                start = self._map_design_point_to_rect((float(polygon[0][0]), float(polygon[0][1])), QRect(QPoint(0, 0), size))
                path.moveTo(start)
                for point in polygon[1:]:
                    mapped = self._map_design_point_to_rect((float(point[0]), float(point[1])), QRect(QPoint(0, 0), size))
                    path.lineTo(mapped)
                path.closeSubpath()
                painter.drawPath(path)
        painter.end()
        self._minimap_cache_key = cache_key
        self._minimap_background = pixmap
        return pixmap

    def _draw_design_route(self, painter: QPainter, rect: QRect) -> None:
        if not self._design_targets:
            return
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(QPen(QColor("#4dd0e1"), 1.5))
        path = QPainterPath()
        start = self._map_design_point_to_rect(self._design_targets[0].design_center, rect)
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
            painter.drawEllipse(self._map_design_point_to_rect(self._selected_design_point, rect), 4.0, 4.0)
        painter.restore()

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
            painter.drawLine(QPointF(mapped.x() - 4.0, mapped.y()), QPointF(mapped.x() + 4.0, mapped.y()))
            painter.drawLine(QPointF(mapped.x(), mapped.y() - 4.0), QPointF(mapped.x(), mapped.y() + 4.0))
        painter.restore()

    def _draw_design_position(self, painter: QPainter, rect: QRect) -> None:
        if self._current_design_position is None:
            return
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)
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
            painter.setPen(QPen(QColor("#81c784"), 1.2))
            painter.setBrush(Qt.NoBrush)
            painter.drawRect(QRectF(top_left, bottom_right).normalized())
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

    def _map_design_point_to_rect(self, point: tuple[float, float], rect: QRect) -> QPointF:
        assert self._design_document is not None
        left, bottom, right, top = self._design_document.bounds
        width = max(right - left, 1e-9)
        height = max(top - bottom, 1e-9)
        pad = 6.0
        usable_width = max(rect.width() - 2.0 * pad, 1.0)
        usable_height = max(rect.height() - 2.0 * pad, 1.0)
        scale = min(usable_width / width, usable_height / height)
        offset_x = rect.left() + (rect.width() - width * scale) * 0.5
        offset_y = rect.top() + (rect.height() - height * scale) * 0.5
        x_pos = offset_x + (point[0] - left) * scale
        y_pos = offset_y + (top - point[1]) * scale
        return QPointF(float(x_pos), float(y_pos))

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
        inside = abs(clamped_x - mapped.x()) < 1e-6 and abs(clamped_y - mapped.y()) < 1e-6
        return QPointF(clamped_x, clamped_y), inside

    @staticmethod
    def _layer_color(layer_key: tuple[int, int]) -> QColor:
        hue = int((layer_key[0] * 57 + layer_key[1] * 19) % 360)
        return QColor.fromHsv(hue, 120, 145, 180)


__all__ = ["MicroscopeView"]
