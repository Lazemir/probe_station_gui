"""Microscope view widget displaying frames and mouse interactions."""

from __future__ import annotations

import math

from PySide6.QtCore import QPoint, QPointF, QRect, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QImage, QMouseEvent, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import QApplication, QWidget

from ..design_model import DesignDocument, MeasurementTarget


class MicroscopeView(QWidget):
    """Widget that renders camera frames with overlay graphics."""

    clicked: Signal = Signal(float, float, float, float)
    hovered: Signal = Signal(float, float, float, float)
    hover_left: Signal = Signal()
    design_minimap_clicked: Signal = Signal(float, float)
    design_minimap_double_clicked: Signal = Signal()
    measure_mode_exited: Signal = Signal()

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
        self._pending_minimap_click_point: QPoint | None = None
        self._minimap_click_timer = QTimer(self)
        self._minimap_click_timer.setSingleShot(True)
        self._minimap_click_timer.timeout.connect(self._emit_pending_minimap_click)
        # Scale bar
        self._scale_mm_per_pixel_x: float | None = None
        self._scale_mm_per_pixel_y: float | None = None
        # Measurement tools: "ruler", "rect", or None
        self._measure_mode: str | None = None
        self._measure_points: list[tuple[float, float]] = []  # in-progress (0 or 1 point)
        self._measure_hover: tuple[float, float] | None = None  # current hover position
        self._ruler_segments: list[tuple[tuple[float, float], tuple[float, float]]] = []
        self._rect_segments: list[tuple[tuple[float, float], tuple[float, float]]] = []

    def set_frame(self, qimg: QImage) -> None:
        self._pix = QPixmap.fromImage(qimg)
        self.update()

    def set_scale(self, mm_per_pixel_x: float, mm_per_pixel_y: float) -> None:
        """Update the physical scale used for the scale bar and ruler."""
        self._scale_mm_per_pixel_x = mm_per_pixel_x if mm_per_pixel_x > 0 else None
        self._scale_mm_per_pixel_y = mm_per_pixel_y if mm_per_pixel_y > 0 else None
        self.update()

    def set_measure_mode(self, mode: str | None) -> None:
        """Set active measurement mode: 'ruler', 'rect', or None to exit."""
        self._measure_mode = mode
        self._measure_points = []
        self._measure_hover = None
        self._ruler_segments = []
        self._rect_segments = []
        self.setCursor(Qt.CrossCursor if mode else Qt.ArrowCursor)
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
            if self._display_rect:
                scale_x = self._display_rect.width() / self._pix.width()
                scale_y = self._display_rect.height() / self._pix.height()
                self._draw_scale_bar(painter, self._display_rect, scale_x)
                self._draw_ruler(painter, self._display_rect, scale_x, scale_y)
                self._draw_rect_measure(painter, self._display_rect, scale_x, scale_y)
        painter.end()

    def mousePressEvent(self, event: QMouseEvent) -> None:  # type: ignore[override]
        if event.button() != Qt.LeftButton or not self._pix or not self._display_rect:
            return
        point = event.position().toPoint()
        if self._minimap_rect is not None and self._minimap_rect.contains(point):
            self._pending_minimap_click_point = QPoint(point)
            self._minimap_click_timer.start(max(1, int(QApplication.doubleClickInterval())))
            event.accept()
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

        if self._measure_mode is not None:
            if len(self._measure_points) == 0:
                self._measure_points = [(dx, dy)]
            else:
                p1 = self._measure_points[0]
                p2 = (dx, dy)
                if self._measure_mode == "ruler":
                    self._ruler_segments.append((p1, p2))
                else:
                    self._rect_segments.append((p1, p2))
                self._measure_points = []
            self._measure_hover = None
            self.update()
            event.accept()
            return

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
            self._minimap_click_timer.stop()
            self._pending_minimap_click_point = None
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
            if self._measure_mode is not None and self._measure_hover is not None:
                self._measure_hover = None
                self.update()
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

        if self._measure_mode is not None:
            # Ctrl + one point placed: lock ruler to nearest axis
            if (
                self._measure_mode == "ruler"
                and (event.modifiers() & Qt.ControlModifier)
                and len(self._measure_points) == 1
            ):
                anchor_dx, anchor_dy = self._measure_points[0]
                if abs(dx - anchor_dx) >= abs(dy - anchor_dy):
                    dy = anchor_dy  # horizontal
                else:
                    dx = anchor_dx  # vertical
            self._measure_hover = (dx, dy)
            self.update()
            return

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

    def _draw_scale_bar(self, painter: QPainter, display_rect: QRect, scale_x: float) -> None:
        """Draw a physical scale bar in the bottom-left corner of the image."""
        if self._scale_mm_per_pixel_x is None or self._scale_mm_per_pixel_x <= 0:
            return
        nice_lengths_mm = [5.0, 2.0, 1.0, 0.5, 0.2, 0.1, 0.05, 0.02, 0.01, 0.005, 0.002, 0.001]
        bar_px: float | None = None
        bar_mm: float | None = None
        for length_mm in nice_lengths_mm:
            px = length_mm * scale_x / self._scale_mm_per_pixel_x
            if 60.0 <= px <= 150.0:
                bar_px = px
                bar_mm = length_mm
                break
        if bar_px is None or bar_mm is None:
            return
        margin = 14
        bar_y = float(display_rect.bottom() - margin)
        bar_x_left = float(display_rect.left() + margin)
        bar_x_right = bar_x_left + bar_px
        tick_h = 5.0
        if bar_mm >= 1.0:
            label = f"{bar_mm:.0f} mm"
        elif bar_mm >= 0.01:
            label = f"{bar_mm * 1000:.0f} \u00b5m"
        else:
            label = f"{bar_mm * 1000:.1f} \u00b5m"
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)
        # Shadow
        painter.setPen(QPen(QColor(0, 0, 0, 160), 3))
        painter.drawLine(QPointF(bar_x_left, bar_y), QPointF(bar_x_right, bar_y))
        painter.drawLine(QPointF(bar_x_left, bar_y - tick_h), QPointF(bar_x_left, bar_y + tick_h))
        painter.drawLine(QPointF(bar_x_right, bar_y - tick_h), QPointF(bar_x_right, bar_y + tick_h))
        # Bar
        painter.setPen(QPen(QColor("white"), 1.5))
        painter.drawLine(QPointF(bar_x_left, bar_y), QPointF(bar_x_right, bar_y))
        painter.drawLine(QPointF(bar_x_left, bar_y - tick_h), QPointF(bar_x_left, bar_y + tick_h))
        painter.drawLine(QPointF(bar_x_right, bar_y - tick_h), QPointF(bar_x_right, bar_y + tick_h))
        # Label
        font = QFont()
        font.setPointSize(8)
        painter.setFont(font)
        label_rect = QRectF(bar_x_left, bar_y - tick_h - 16.0, bar_px, 14.0)
        painter.setPen(QPen(QColor(0, 0, 0, 160), 3))
        painter.drawText(label_rect, Qt.AlignCenter, label)
        painter.setPen(QPen(QColor("white"), 1))
        painter.drawText(label_rect, Qt.AlignCenter, label)
        painter.restore()

    def _draw_ruler(
        self, painter: QPainter, display_rect: QRect, scale_x: float, scale_y: float
    ) -> None:
        """Draw ruler measurement tool overlay (multiple segments)."""
        if self._measure_mode != "ruler":
            return
        cx = display_rect.left() + display_rect.width() / 2.0
        cy = display_rect.top() + display_rect.height() / 2.0

        def to_disp(dx: float, dy: float) -> QPointF:
            return QPointF(cx + dx * scale_x, cy - dy * scale_y)

        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)
        font = QFont()
        font.setPointSize(9)
        painter.setFont(font)

        has_scale = (
            self._scale_mm_per_pixel_x is not None
            and self._scale_mm_per_pixel_y is not None
            and self._scale_mm_per_pixel_x > 0
            and self._scale_mm_per_pixel_y > 0
        )

        def dist_label(ddx: float, ddy: float) -> str:
            if has_scale:
                assert self._scale_mm_per_pixel_x is not None
                assert self._scale_mm_per_pixel_y is not None
                d = math.sqrt(
                    (ddx * self._scale_mm_per_pixel_x) ** 2
                    + (ddy * self._scale_mm_per_pixel_y) ** 2
                )
                return f"{d * 1000:.1f} \u00b5m" if d < 1.0 else f"{d:.4f} mm"
            return f"{math.sqrt(ddx ** 2 + ddy ** 2):.1f} px"

        def draw_segment_label(p1: QPointF, p2: QPointF, label: str, color: QColor) -> None:
            mid = QPointF((p1.x() + p2.x()) / 2.0, (p1.y() + p2.y()) / 2.0)
            w, h = 110.0, 20.0
            bg = QRectF(mid.x() - w / 2.0, mid.y() - h / 2.0, w, h)
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(0, 0, 0, 175))
            painter.drawRoundedRect(bg, 4, 4)
            painter.setPen(QPen(color, 1))
            painter.drawText(bg, Qt.AlignCenter, label)

        def draw_crosshair(p: QPointF, color: QColor, size: float = 6.0) -> None:
            painter.setPen(QPen(color, 1.5))
            painter.drawLine(QPointF(p.x() - size, p.y()), QPointF(p.x() + size, p.y()))
            painter.drawLine(QPointF(p.x(), p.y() - size), QPointF(p.x(), p.y() + size))

        # Completed segments
        for seg_p1, seg_p2 in self._ruler_segments:
            d1 = to_disp(*seg_p1)
            d2 = to_disp(*seg_p2)
            painter.setPen(QPen(QColor("#ff9800"), 2))
            painter.drawLine(d1, d2)
            ddx = seg_p2[0] - seg_p1[0]
            ddy = seg_p2[1] - seg_p1[1]
            draw_segment_label(d1, d2, dist_label(ddx, ddy), QColor("#ff9800"))
            draw_crosshair(d1, QColor("#ff9800"), 6.0)
            draw_crosshair(d2, QColor("#ff9800"), 6.0)

        # In-progress: first point placed, show dashed preview to hover
        if len(self._measure_points) == 1 and self._measure_hover is not None:
            p1 = to_disp(*self._measure_points[0])
            p2 = to_disp(*self._measure_hover)
            pen = QPen(QColor("#ffeb3b"), 1.5, Qt.DashLine)
            pen.setDashPattern([6, 4])
            painter.setPen(pen)
            painter.drawLine(p1, p2)
            ddx = self._measure_hover[0] - self._measure_points[0][0]
            ddy = self._measure_hover[1] - self._measure_points[0][1]
            draw_segment_label(p1, p2, dist_label(ddx, ddy), QColor("#ffeb3b"))

        # First placed point marker
        for pt in self._measure_points:
            draw_crosshair(to_disp(*pt), QColor("#ff9800"), 7.0)

        # Hover crosshair
        if self._measure_hover is not None:
            draw_crosshair(to_disp(*self._measure_hover), QColor("#ffeb3b"), 5.0)

        # Hint bar
        if len(self._measure_points) == 0:
            hint = "Ruler: click to place first point  |  Esc = exit"
        else:
            hint = "Ruler: click second point  |  Ctrl = lock axis  |  Esc = exit"
        hw, hh = 340.0, 22.0
        hint_rect = QRectF(
            display_rect.left() + (display_rect.width() - hw) / 2.0,
            float(display_rect.bottom()) - hh - 36.0,
            hw,
            hh,
        )
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(0, 0, 0, 150))
        painter.drawRoundedRect(hint_rect, 5, 5)
        painter.setPen(QPen(QColor("#ffeb3b"), 1))
        painter.drawText(hint_rect, Qt.AlignCenter, hint)

        painter.restore()

    def _draw_rect_measure(
        self, painter: QPainter, display_rect: QRect, scale_x: float, scale_y: float
    ) -> None:
        """Draw rectangle measurement tool overlay with technical-drawing dimension lines."""
        if self._measure_mode != "rect":
            return
        cx = display_rect.left() + display_rect.width() / 2.0
        cy = display_rect.top() + display_rect.height() / 2.0

        def to_disp(dx: float, dy: float) -> QPointF:
            return QPointF(cx + dx * scale_x, cy - dy * scale_y)

        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)
        font = QFont()
        font.setPointSize(9)
        painter.setFont(font)

        has_scale = (
            self._scale_mm_per_pixel_x is not None
            and self._scale_mm_per_pixel_y is not None
            and self._scale_mm_per_pixel_x > 0
            and self._scale_mm_per_pixel_y > 0
        )

        def dim_label(img_px: float, axis: int) -> str:
            """Format a dimension from image-pixel delta."""
            if has_scale:
                assert self._scale_mm_per_pixel_x is not None
                assert self._scale_mm_per_pixel_y is not None
                scale = self._scale_mm_per_pixel_x if axis == 0 else self._scale_mm_per_pixel_y
                d = abs(img_px) * scale
                return f"{d * 1000:.1f} \u00b5m" if d < 1.0 else f"{d:.4f} mm"
            return f"{abs(img_px):.1f} px"

        def draw_h_arrow(x: float, y: float, direction: float) -> None:
            """Arrow tip at (x, y) pointing in +direction (±1 = right/left)."""
            s = 5.0
            painter.drawLine(QPointF(x, y), QPointF(x - direction * s, y - s * 0.45))
            painter.drawLine(QPointF(x, y), QPointF(x - direction * s, y + s * 0.45))

        def draw_v_arrow(x: float, y: float, direction: float) -> None:
            """Arrow tip at (x, y) pointing in +direction (±1 = down/up)."""
            s = 5.0
            painter.drawLine(QPointF(x, y), QPointF(x - s * 0.45, y - direction * s))
            painter.drawLine(QPointF(x, y), QPointF(x + s * 0.45, y - direction * s))

        def draw_dim_label_h(x_left: float, x_right: float, y: float, label: str, color: QColor) -> None:
            mid_x = (x_left + x_right) / 2.0
            w, h = max(100.0, float(len(label)) * 8.0), 18.0
            bg = QRectF(mid_x - w / 2.0, y - h / 2.0 - 10.0, w, h)
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(0, 0, 0, 175))
            painter.drawRoundedRect(bg, 3, 3)
            painter.setPen(QPen(color, 1))
            painter.drawText(bg, Qt.AlignCenter, label)

        def draw_dim_label_v(x: float, y_top: float, y_bottom: float, label: str, color: QColor) -> None:
            mid_y = (y_top + y_bottom) / 2.0
            w, h = max(90.0, float(len(label)) * 8.0), 18.0
            bg = QRectF(x + 6.0, mid_y - h / 2.0, w, h)
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(0, 0, 0, 175))
            painter.drawRoundedRect(bg, 3, 3)
            painter.setPen(QPen(color, 1))
            painter.drawText(bg, Qt.AlignCenter, label)

        def draw_one_rect(
            corner1: tuple[float, float],
            corner2: tuple[float, float],
            color: QColor,
            dashed: bool,
        ) -> None:
            d1 = to_disp(*corner1)
            d2 = to_disp(*corner2)
            left = min(d1.x(), d2.x())
            right = max(d1.x(), d2.x())
            top = min(d1.y(), d2.y())
            bottom = max(d1.y(), d2.y())
            img_w = abs(corner2[0] - corner1[0])
            img_h = abs(corner2[1] - corner1[1])
            w_label = dim_label(img_w, 0)
            h_label = dim_label(img_h, 1)

            # Rectangle body
            pen = QPen(color, 1.5)
            if dashed:
                pen.setStyle(Qt.DashLine)
                pen.setDashPattern([6, 4])
            painter.setPen(pen)
            painter.setBrush(Qt.NoBrush)
            painter.drawRect(QRectF(left, top, right - left, bottom - top))

            dim_offset = 20.0
            ext_extra = 5.0

            # Width dimension (above rect)
            dim_y = top - dim_offset
            ext_top = top
            # Extension lines
            painter.setPen(QPen(color, 0.8))
            painter.drawLine(QPointF(left, ext_top), QPointF(left, dim_y - ext_extra))
            painter.drawLine(QPointF(right, ext_top), QPointF(right, dim_y - ext_extra))
            # Dimension line + arrows
            painter.setPen(QPen(color, 1.2))
            painter.drawLine(QPointF(left, dim_y), QPointF(right, dim_y))
            draw_h_arrow(left, dim_y, -1.0)   # tip at left, pointing left
            draw_h_arrow(right, dim_y, +1.0)  # tip at right, pointing right
            draw_dim_label_h(left, right, dim_y, w_label, color)

            # Height dimension (right of rect)
            dim_x = right + dim_offset
            ext_left = right
            painter.setPen(QPen(color, 0.8))
            painter.drawLine(QPointF(ext_left, top), QPointF(dim_x + ext_extra, top))
            painter.drawLine(QPointF(ext_left, bottom), QPointF(dim_x + ext_extra, bottom))
            painter.setPen(QPen(color, 1.2))
            painter.drawLine(QPointF(dim_x, top), QPointF(dim_x, bottom))
            draw_v_arrow(dim_x, top, -1.0)    # tip at top, pointing up
            draw_v_arrow(dim_x, bottom, +1.0) # tip at bottom, pointing down
            draw_dim_label_v(dim_x, top, bottom, h_label, color)

        # Completed rectangles
        for seg_c1, seg_c2 in self._rect_segments:
            draw_one_rect(seg_c1, seg_c2, QColor("#4fc3f7"), dashed=False)

        # In-progress preview
        if len(self._measure_points) == 1 and self._measure_hover is not None:
            draw_one_rect(self._measure_points[0], self._measure_hover, QColor("#ffeb3b"), dashed=True)
        elif len(self._measure_points) == 1:
            # First corner placed, no hover yet
            p = to_disp(*self._measure_points[0])
            painter.setPen(QPen(QColor("#ff9800"), 1.5))
            painter.drawLine(QPointF(p.x() - 7, p.y()), QPointF(p.x() + 7, p.y()))
            painter.drawLine(QPointF(p.x(), p.y() - 7), QPointF(p.x(), p.y() + 7))

        if len(self._measure_points) == 0 and self._measure_hover is not None:
            p = to_disp(*self._measure_hover)
            painter.setPen(QPen(QColor("#ffeb3b"), 1.5))
            painter.drawLine(QPointF(p.x() - 5, p.y()), QPointF(p.x() + 5, p.y()))
            painter.drawLine(QPointF(p.x(), p.y() - 5), QPointF(p.x(), p.y() + 5))

        # Hint bar
        if len(self._measure_points) == 0:
            hint = "Rect: click first corner  |  Esc = exit"
        else:
            hint = "Rect: click opposite corner  |  Esc = exit"
        hw, hh = 300.0, 22.0
        hint_rect = QRectF(
            display_rect.left() + (display_rect.width() - hw) / 2.0,
            float(display_rect.bottom()) - hh - 36.0,
            hw,
            hh,
        )
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(0, 0, 0, 150))
        painter.drawRoundedRect(hint_rect, 5, 5)
        painter.setPen(QPen(QColor("#ffeb3b"), 1))
        painter.drawText(hint_rect, Qt.AlignCenter, hint)

        painter.restore()

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

    def _map_rect_point_to_design(self, point: QPoint | QPointF, rect: QRect) -> tuple[float, float]:
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
        x_value = left + (float(point.x()) - offset_x) / scale
        y_value = top - (float(point.y()) - offset_y) / scale
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
        inside = abs(clamped_x - mapped.x()) < 1e-6 and abs(clamped_y - mapped.y()) < 1e-6
        return QPointF(clamped_x, clamped_y), inside

    def _emit_pending_minimap_click(self) -> None:
        if (
            self._design_document is None
            or self._minimap_rect is None
            or self._pending_minimap_click_point is None
        ):
            self._pending_minimap_click_point = None
            return
        content_rect = QRect(
            self._minimap_rect.left() + 8,
            self._minimap_rect.top() + 26,
            self._minimap_rect.width() - 16,
            self._minimap_rect.height() - 34,
        )
        if content_rect.width() <= 0 or content_rect.height() <= 0:
            self._pending_minimap_click_point = None
            return
        design_point = self._map_rect_point_to_design(
            self._pending_minimap_click_point,
            content_rect,
        )
        self._pending_minimap_click_point = None
        self.design_minimap_clicked.emit(design_point[0], design_point[1])

    @staticmethod
    def _layer_color(layer_key: tuple[int, int]) -> QColor:
        hue = int((layer_key[0] * 57 + layer_key[1] * 19) % 360)
        return QColor.fromHsv(hue, 120, 145, 180)


__all__ = ["MicroscopeView"]
