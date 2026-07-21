"""Microscope view widget displaying frames and mouse interactions."""

from __future__ import annotations

import math
import logging
from time import perf_counter
from typing import Callable

from PySide6.QtCore import QPoint, QPointF, QRect, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import (
    QColor,
    QFont,
    QImage,
    QMouseEvent,
    QPainter,
    QPalette,
    QPen,
    QPixmap,
)
from PySide6.QtWidgets import QApplication, QWidget

from probe_station_gui.design.klayout_workers import KLayoutRenderWorker
from probe_station_gui.design.model import DesignDocument, MeasurementTarget
from probe_station_gui.views.microscope_minimap import MicroscopeMinimap
from probe_station_gui.stage.motion_prediction import interpolate_position
from probe_station_gui.route.model import MeasurementRoute


logger = logging.getLogger(__name__)


class MicroscopeView(QWidget):
    """Widget that renders camera frames with overlay graphics."""

    clicked: Signal = Signal(float, float, float, float)
    hovered: Signal = Signal(float, float, float, float)
    hover_left: Signal = Signal()
    design_minimap_clicked: Signal = Signal(float, float)
    design_minimap_double_clicked: Signal = Signal()
    measure_mode_exited: Signal = Signal()
    _TARGET_BLINK_MS = 250
    _TARGET_MOTION_UPDATE_MS = 50
    _TARGET_PENDING_COLOR = QColor("#c62828")
    _TARGET_PENDING_DIMMED_COLOR = QColor("#ad6b6b")

    def __init__(
        self,
        *,
        minimap_render_worker_factory: Callable[
            [], KLayoutRenderWorker
        ] = KLayoutRenderWorker,
    ) -> None:
        super().__init__()
        self.setWindowTitle("Microscope Qt")
        self.setMinimumSize(640, 480)
        self.setAutoFillBackground(True)
        palette = self.palette()
        palette.setColor(QPalette.Window, QColor("black"))
        self.setPalette(palette)
        self.setAttribute(Qt.WA_OpaquePaintEvent, True)
        self.setMouseTracking(True)
        self._pix: QPixmap | None = None
        self._target_rel: tuple[float, float] | None = None
        self._click_to_move_release_target: tuple[float, float, float, float] | None = (
            None
        )
        self._click_to_move_release_cancelled = False
        self._target_pending = False
        self._target_blink_dimmed = False
        self._target_blink_timer = QTimer(self)
        self._target_blink_timer.setInterval(self._TARGET_BLINK_MS)
        self._target_blink_timer.timeout.connect(self._advance_target_blink)
        self._target_motion_origin_rel: tuple[float, float] | None = None
        self._target_motion_started_at: float | None = None
        self._target_motion_ends_at: float | None = None
        self._target_motion_timer = QTimer(self)
        self._target_motion_timer.setInterval(self._TARGET_MOTION_UPDATE_MS)
        self._target_motion_timer.timeout.connect(self._advance_target_motion)
        self._alignment_mode = False
        self._alignment_points: list[tuple[float, float]] = []
        self._alignment_instruction = ""
        self._display_rect: QRect | None = None
        self._minimap = MicroscopeMinimap(
            renderer=minimap_render_worker_factory,
            device_pixel_ratio=self.devicePixelRatioF,
            parent=self,
        )
        self._minimap.changed.connect(self.update)
        self._minimap.clicked.connect(self.design_minimap_clicked.emit)
        app = QApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self.shutdown)
        # Scale bar
        self._scale_mm_per_pixel_x: float | None = None
        self._scale_mm_per_pixel_y: float | None = None
        # Measurement tools: "ruler", "rect", or None
        self._measure_mode: str | None = None
        self._measure_points: list[
            tuple[float, float]
        ] = []  # in-progress (0 or 1 point)
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
        probe_route: MeasurementRoute | None,
        selected_route_point_index: int,
        selected_design_point: tuple[float, float] | None,
        current_design_position: tuple[float, float] | None,
        fov_design_size: tuple[float, float] | None,
        source_design_marks: list[tuple[float, float]],
        check_design_marks: list[tuple[float, float]],
    ) -> None:
        """Pass immutable minimap inputs to the minimap component."""

        bounds = document.bounds if document is not None else (0.0, 0.0, 1.0, 1.0)
        self._minimap.configure(
            document,
            bounds,
            targets=tuple(targets),
            selected_target_id=selected_target_id,
            probe_route=probe_route,
            selected_route_point_index=selected_route_point_index,
            selected_design_point=selected_design_point,
            current_design_position=current_design_position,
            fov_design_size=fov_design_size,
            source_design_marks=tuple(source_design_marks),
            check_design_marks=tuple(check_design_marks),
        )

    def shutdown(self) -> None:
        """Stop the minimap renderer without blocking the GUI thread."""

        self._minimap.shutdown()

    def closeEvent(self, event) -> None:  # type: ignore[override]
        self.shutdown()
        super().closeEvent(event)

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
                target_x = (
                    self._display_rect.left() + rel_x * self._display_rect.width()
                )
                target_y = (
                    self._display_rect.top() + rel_y * self._display_rect.height()
                )
                radius = 6
                pen_width = 1
                color = QColor("red")
                if self._target_pending:
                    color = (
                        self._TARGET_PENDING_DIMMED_COLOR
                        if self._target_blink_dimmed
                        else self._TARGET_PENDING_COLOR
                    )
                painter.setPen(QPen(color, pen_width))
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
                painter.drawEllipse(
                    QPoint(int(target_x), int(target_y)), radius, radius
                )

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
                    painter.drawRoundedRect(
                        12, 12, min(self.width() - 24, 430), 46, 8, 8
                    )
                    painter.setPen(QPen(QColor("#fff3cd"), 1))
                    painter.drawText(
                        QRect(20, 18, max(0, self.width() - 40), 34),
                        Qt.AlignLeft | Qt.AlignVCenter,
                        self._alignment_instruction,
                    )

            if self._display_rect:
                self._minimap.draw(painter, self._display_rect)
            if self._display_rect:
                scale_x = self._display_rect.width() / self._pix.width()
                scale_y = self._display_rect.height() / self._pix.height()
                self._draw_scale_bar(painter, self._display_rect, scale_x)
                self._draw_ruler(painter, self._display_rect, scale_x, scale_y)
                self._draw_rect_measure(painter, self._display_rect, scale_x, scale_y)
                self._draw_axis_triad(painter, self._display_rect)
        painter.end()

    def mousePressEvent(self, event: QMouseEvent) -> None:  # type: ignore[override]
        if event.button() != Qt.LeftButton or not self._pix or not self._display_rect:
            return
        point = event.position().toPoint()
        if self._minimap.contains(point, self._display_rect):
            self._minimap.queue_click(point, self._display_rect)
            event.accept()
            return
        if not self._display_rect.contains(point):
            return

        click = self._image_click_coordinates(event.position())
        if click is None:
            return
        dx, dy, rel_x, rel_y = click

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

        if self._alignment_mode:
            self.clicked.emit(dx, dy, rel_x, rel_y)
            self.update()
            event.accept()
            return

        self._arm_click_to_move_release(click)
        event.accept()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # type: ignore[override]
        if event.button() != Qt.LeftButton:
            super().mouseReleaseEvent(event)
            return
        if self._click_to_move_release_cancelled:
            self._click_to_move_release_cancelled = False
            event.accept()
            return
        if self._click_to_move_release_target is None:
            super().mouseReleaseEvent(event)
            return
        click = self._image_click_coordinates(event.position())
        if click is None:
            self._cancel_click_to_move_release(suppress_until_release=False)
            event.accept()
            return
        self._click_to_move_release_target = None
        dx, dy, rel_x, rel_y = click
        self._clear_target_motion()
        self.set_target_pending(False)
        self._target_rel = (rel_x, rel_y)
        self.clicked.emit(dx, dy, rel_x, rel_y)
        self.update()
        event.accept()

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:  # type: ignore[override]
        point = event.position().toPoint()
        if (
            event.button() == Qt.LeftButton
            and self._display_rect is not None
            and self._minimap.contains(point, self._display_rect)
        ):
            self._minimap.cancel_click()
            self.design_minimap_double_clicked.emit()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # type: ignore[override]
        if not self._pix or not self._display_rect:
            if event.buttons() & Qt.LeftButton:
                self._cancel_click_to_move_release(suppress_until_release=True)
            self.hover_left.emit()
            return
        point = event.position().toPoint()
        if not self._display_rect.contains(point):
            if event.buttons() & Qt.LeftButton:
                self._cancel_click_to_move_release(suppress_until_release=True)
            self.hover_left.emit()
            if self._measure_mode is not None and self._measure_hover is not None:
                self._measure_hover = None
                self.update()
            return

        click = self._image_click_coordinates(event.position())
        if click is None:
            if event.buttons() & Qt.LeftButton:
                self._cancel_click_to_move_release(suppress_until_release=True)
            self.hover_left.emit()
            return
        dx, dy, rel_x, rel_y = click

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

        if (
            event.buttons() & Qt.LeftButton
        ) and self._click_to_move_release_target is not None:
            self._click_to_move_release_target = click
            self._target_rel = (rel_x, rel_y)
            self.update()
        self.hovered.emit(dx, dy, rel_x, rel_y)

    def leaveEvent(self, event) -> None:  # type: ignore[override]
        if self._click_to_move_release_target is not None:
            self._cancel_click_to_move_release(suppress_until_release=True)
        self.hover_left.emit()
        super().leaveEvent(event)

    def clear_target_cross(self) -> None:
        """Remove the movable cross overlay."""

        self._click_to_move_release_target = None
        self._click_to_move_release_cancelled = False
        self._target_rel = None
        self._clear_target_motion()
        self.set_target_pending(False)
        self.update()

    def _image_click_coordinates(
        self, position: QPointF
    ) -> tuple[float, float, float, float] | None:
        if not self._pix or not self._display_rect:
            return None
        if (
            self._display_rect.width() <= 0
            or self._display_rect.height() <= 0
            or self._pix.width() <= 0
            or self._pix.height() <= 0
        ):
            return None
        if not self._display_rect.contains(position.toPoint()):
            return None

        scale_x = self._display_rect.width() / self._pix.width()
        scale_y = self._display_rect.height() / self._pix.height()
        if scale_x <= 0 or scale_y <= 0:
            return None

        image_x = (position.x() - self._display_rect.left()) / scale_x
        image_y = (position.y() - self._display_rect.top()) / scale_y
        center_x = self._pix.width() / 2
        center_y = self._pix.height() / 2
        dx = image_x - center_x
        dy = center_y - image_y
        rel_x = (position.x() - self._display_rect.left()) / self._display_rect.width()
        rel_y = (position.y() - self._display_rect.top()) / self._display_rect.height()
        rel_x = max(0.0, min(1.0, rel_x))
        rel_y = max(0.0, min(1.0, rel_y))
        return dx, dy, rel_x, rel_y

    def _arm_click_to_move_release(
        self, click: tuple[float, float, float, float]
    ) -> None:
        self._click_to_move_release_target = click
        self._click_to_move_release_cancelled = False
        _dx, _dy, rel_x, rel_y = click
        self._clear_target_motion()
        self.set_target_pending(False)
        self._target_rel = (rel_x, rel_y)
        self.update()

    def _cancel_click_to_move_release(self, *, suppress_until_release: bool) -> None:
        if (
            self._click_to_move_release_target is None
            and not self._click_to_move_release_cancelled
        ):
            return
        self._click_to_move_release_target = None
        self._click_to_move_release_cancelled = bool(suppress_until_release)
        self._clear_target_motion()
        self.set_target_pending(False)
        self._target_rel = None
        self.update()

    def set_target_pending(self, pending: bool) -> None:
        """Mark the movable cross as waiting for the stage to accept the click."""

        pending = bool(pending and self._target_rel is not None)
        if self._target_pending == pending:
            return
        self._target_pending = pending
        if pending:
            self._target_blink_dimmed = False
            self._target_blink_timer.start()
        else:
            self._target_blink_timer.stop()
            self._target_blink_dimmed = False
        self.update()

    def set_target_pending_blink_interval(self, interval_ms: int) -> None:
        """Set the movable cross blink interval."""

        self._target_blink_timer.setInterval(max(1, int(interval_ms)))

    def set_target_motion_update_interval(self, interval_ms: int) -> None:
        """Set the movable cross animation update interval."""

        self._target_motion_timer.setInterval(max(1, int(interval_ms)))

    def animate_target_cross_to_center(self, duration_s: float) -> None:
        """Animate the movable target cross to the image center."""

        if self._target_rel is None:
            return
        self.set_target_pending(False)
        origin = (float(self._target_rel[0]), float(self._target_rel[1]))
        target = (0.5, 0.5)
        if duration_s <= 0.0 or (
            abs(origin[0] - target[0]) < 1e-6 and abs(origin[1] - target[1]) < 1e-6
        ):
            self._target_rel = target
            self._clear_target_motion()
            self.update()
            return
        started_at = perf_counter()
        self._target_motion_origin_rel = origin
        self._target_motion_started_at = started_at
        self._target_motion_ends_at = started_at + max(1e-3, float(duration_s))
        self._target_motion_timer.start()
        self._advance_target_motion()

    def finish_target_motion_to_center(self) -> None:
        """Snap the movable target cross to the center and stop target animation."""

        if self._target_rel is not None:
            self._target_rel = (0.5, 0.5)
        self._clear_target_motion()
        self.update()

    def _clear_target_motion(self) -> None:
        if self._target_motion_timer.isActive():
            self._target_motion_timer.stop()
        self._target_motion_origin_rel = None
        self._target_motion_started_at = None
        self._target_motion_ends_at = None

    def _advance_target_motion(self) -> None:
        if (
            self._target_motion_origin_rel is None
            or self._target_motion_started_at is None
            or self._target_motion_ends_at is None
        ):
            self._clear_target_motion()
            return
        now = perf_counter()
        interpolated = interpolate_position(
            self._target_motion_origin_rel,
            (0.5, 0.5),
            self._target_motion_started_at,
            self._target_motion_ends_at,
            now,
        )
        self._target_rel = (float(interpolated[0]), float(interpolated[1]))
        if now >= self._target_motion_ends_at:
            self._target_rel = (0.5, 0.5)
            self._clear_target_motion()
        self.update()

    def _advance_target_blink(self) -> None:
        if not self._target_pending or self._target_rel is None:
            self.set_target_pending(False)
            return
        self._target_blink_dimmed = not self._target_blink_dimmed
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

    def _draw_scale_bar(
        self, painter: QPainter, display_rect: QRect, scale_x: float
    ) -> None:
        """Draw a physical scale bar in the bottom-left corner of the image."""
        if self._scale_mm_per_pixel_x is None or self._scale_mm_per_pixel_x <= 0:
            return
        nice_lengths_mm = [
            5.0,
            2.0,
            1.0,
            0.5,
            0.2,
            0.1,
            0.05,
            0.02,
            0.01,
            0.005,
            0.002,
            0.001,
        ]
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
        painter.drawLine(
            QPointF(bar_x_left, bar_y - tick_h), QPointF(bar_x_left, bar_y + tick_h)
        )
        painter.drawLine(
            QPointF(bar_x_right, bar_y - tick_h), QPointF(bar_x_right, bar_y + tick_h)
        )
        # Bar
        painter.setPen(QPen(QColor("white"), 1.5))
        painter.drawLine(QPointF(bar_x_left, bar_y), QPointF(bar_x_right, bar_y))
        painter.drawLine(
            QPointF(bar_x_left, bar_y - tick_h), QPointF(bar_x_left, bar_y + tick_h)
        )
        painter.drawLine(
            QPointF(bar_x_right, bar_y - tick_h), QPointF(bar_x_right, bar_y + tick_h)
        )
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

    def _draw_axis_triad(self, painter: QPainter, display_rect: QRect) -> None:
        """Draw a small screen-space axis triad in the camera view."""

        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)
        base = QPointF(display_rect.left() + 34.0, display_rect.bottom() - 82.0)
        length = 34.0
        arrow = 7.0
        font = QFont()
        font.setPointSize(8)
        font.setBold(True)
        painter.setFont(font)

        def draw_axis(
            end: QPointF, color: QColor, label: str, label_offset: QPointF
        ) -> None:
            painter.setPen(QPen(QColor(0, 0, 0, 170), 4.0))
            painter.drawLine(base, end)
            painter.setPen(QPen(color, 2.2))
            painter.drawLine(base, end)
            dx = end.x() - base.x()
            dy = end.y() - base.y()
            axis_len = math.hypot(dx, dy)
            if axis_len > 1e-6:
                ux = dx / axis_len
                uy = dy / axis_len
                px = -uy
                py = ux
                p1 = QPointF(
                    end.x() - ux * arrow + px * arrow * 0.55,
                    end.y() - uy * arrow + py * arrow * 0.55,
                )
                p2 = QPointF(
                    end.x() - ux * arrow - px * arrow * 0.55,
                    end.y() - uy * arrow - py * arrow * 0.55,
                )
                painter.drawLine(end, p1)
                painter.drawLine(end, p2)
            label_rect = QRectF(
                end.x() + label_offset.x() - 8.0,
                end.y() + label_offset.y() - 8.0,
                16.0,
                16.0,
            )
            painter.setPen(QPen(QColor(0, 0, 0, 180), 3.0))
            painter.drawText(label_rect, Qt.AlignCenter, label)
            painter.setPen(QPen(color, 1.0))
            painter.drawText(label_rect, Qt.AlignCenter, label)

        def draw_out_of_plane_axis(center: QPointF, color: QColor) -> None:
            radius = 7.0
            painter.setPen(QPen(QColor(0, 0, 0, 180), 4.0))
            painter.setBrush(Qt.NoBrush)
            painter.drawEllipse(center, radius, radius)
            painter.setPen(QPen(color, 2.0))
            painter.drawEllipse(center, radius, radius)
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(0, 0, 0, 190))
            painter.drawEllipse(center, 3.2, 3.2)
            painter.setBrush(color)
            painter.drawEllipse(center, 2.2, 2.2)
            label_rect = QRectF(center.x() - 24.0, center.y() - 26.0, 16.0, 16.0)
            painter.setPen(QPen(QColor(0, 0, 0, 180), 3.0))
            painter.drawText(label_rect, Qt.AlignCenter, "Z")
            painter.setPen(QPen(color, 1.0))
            painter.drawText(label_rect, Qt.AlignCenter, "Z")

        draw_axis(
            QPointF(base.x() + length, base.y()),
            QColor("#ef5350"),
            "X",
            QPointF(10.0, 0.0),
        )
        draw_axis(
            QPointF(base.x(), base.y() - length),
            QColor("#66bb6a"),
            "Y",
            QPointF(0.0, -10.0),
        )
        draw_out_of_plane_axis(base, QColor("#42a5f5"))
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
            return f"{math.sqrt(ddx**2 + ddy**2):.1f} px"

        def draw_segment_label(
            p1: QPointF, p2: QPointF, label: str, color: QColor
        ) -> None:
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
                scale = (
                    self._scale_mm_per_pixel_x
                    if axis == 0
                    else self._scale_mm_per_pixel_y
                )
                d = abs(img_px) * scale
                return f"{d * 1000:.1f} \u00b5m" if d < 1.0 else f"{d:.4f} mm"
            return f"{abs(img_px):.1f} px"

        def draw_h_arrow(x: float, y: float, direction: float) -> None:
            """Arrow tip at (x, y) pointing in +direction (В±1 = right/left)."""
            s = 5.0
            painter.drawLine(QPointF(x, y), QPointF(x - direction * s, y - s * 0.45))
            painter.drawLine(QPointF(x, y), QPointF(x - direction * s, y + s * 0.45))

        def draw_v_arrow(x: float, y: float, direction: float) -> None:
            """Arrow tip at (x, y) pointing in +direction (В±1 = down/up)."""
            s = 5.0
            painter.drawLine(QPointF(x, y), QPointF(x - s * 0.45, y - direction * s))
            painter.drawLine(QPointF(x, y), QPointF(x + s * 0.45, y - direction * s))

        def draw_dim_label_h(
            x_left: float, x_right: float, y: float, label: str, color: QColor
        ) -> None:
            mid_x = (x_left + x_right) / 2.0
            w, h = max(100.0, float(len(label)) * 8.0), 18.0
            bg = QRectF(mid_x - w / 2.0, y - h / 2.0 - 10.0, w, h)
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(0, 0, 0, 175))
            painter.drawRoundedRect(bg, 3, 3)
            painter.setPen(QPen(color, 1))
            painter.drawText(bg, Qt.AlignCenter, label)

        def draw_dim_label_v(
            x: float, y_top: float, y_bottom: float, label: str, color: QColor
        ) -> None:
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
            draw_h_arrow(left, dim_y, -1.0)  # tip at left, pointing left
            draw_h_arrow(right, dim_y, +1.0)  # tip at right, pointing right
            draw_dim_label_h(left, right, dim_y, w_label, color)

            # Height dimension (right of rect)
            dim_x = right + dim_offset
            ext_left = right
            painter.setPen(QPen(color, 0.8))
            painter.drawLine(QPointF(ext_left, top), QPointF(dim_x + ext_extra, top))
            painter.drawLine(
                QPointF(ext_left, bottom), QPointF(dim_x + ext_extra, bottom)
            )
            painter.setPen(QPen(color, 1.2))
            painter.drawLine(QPointF(dim_x, top), QPointF(dim_x, bottom))
            draw_v_arrow(dim_x, top, -1.0)  # tip at top, pointing up
            draw_v_arrow(dim_x, bottom, +1.0)  # tip at bottom, pointing down
            draw_dim_label_v(dim_x, top, bottom, h_label, color)

        # Completed rectangles
        for seg_c1, seg_c2 in self._rect_segments:
            draw_one_rect(seg_c1, seg_c2, QColor("#4fc3f7"), dashed=False)

        # In-progress preview
        if len(self._measure_points) == 1 and self._measure_hover is not None:
            draw_one_rect(
                self._measure_points[0],
                self._measure_hover,
                QColor("#ffeb3b"),
                dashed=True,
            )
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


__all__ = ["MicroscopeView"]
