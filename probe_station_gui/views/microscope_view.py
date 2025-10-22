"""Microscope view widget displaying frames and mouse interactions."""

from __future__ import annotations

import math
from typing import Iterable

import cv2
import numpy as np
from PySide6.QtCore import QPoint, QRect, Qt, Signal, QEvent
from PySide6.QtGui import (
    QColor,
    QImage,
    QMouseEvent,
    QPainter,
    QPen,
    QPixmap,
    QTransform,
)
from PySide6.QtWidgets import QWidget


class MicroscopeView(QWidget):
    """Widget that renders camera frames with overlay graphics."""

    clicked: Signal = Signal(float, float, float, float)

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Microscope Qt")
        self.setMouseTracking(True)
        self.setMinimumSize(960, 720)
        self._pix: QPixmap | None = None
        self._target_rel: tuple[float, float] | None = None
        self._target_pixel: tuple[float, float] | None = None
        self._display_rect: QRect | None = None
        self._pixels_to_mm: np.ndarray | None = None
        self._cursor_rel: tuple[float, float] | None = None
        self._cursor_pixel: tuple[float, float] | None = None
        self._tracking_active = False
        self._tracking_reference: np.ndarray | None = None
        self._current_frame_gray: np.ndarray | None = None

    def set_frame(self, qimg: QImage) -> None:
        transform = QTransform().rotate(90)
        rotated = qimg.transformed(transform)
        self._pix = QPixmap.fromImage(rotated)
        gray = self._qimage_to_gray(rotated)
        self._current_frame_gray = gray
        if self._tracking_active:
            self._handle_tracking(gray)
        else:
            self._tracking_reference = None
        if self._target_rel and self._pix:
            width = self._pix.width()
            height = self._pix.height()
            self._target_pixel = (
                self._target_rel[0] * width,
                self._target_rel[1] * height,
            )
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
            self._draw_coordinate_overlay(painter)
            self._draw_scale_bar(painter)
        painter.end()

    def mousePressEvent(self, event: QMouseEvent) -> None:  # type: ignore[override]
        if event.button() != Qt.LeftButton or not self._pix or not self._display_rect:
            return
        point = event.position().toPoint()
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
        self._target_rel = (rel_x, rel_y)
        self._target_pixel = (image_x, image_y)
        self.clicked.emit(dx, dy, rel_x, rel_y)
        self.update()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # type: ignore[override]
        if not self._pix or not self._display_rect:
            self._cursor_rel = None
            self._cursor_pixel = None
            self.update()
            return
        point = event.position().toPoint()
        if not self._display_rect.contains(point):
            self._cursor_rel = None
            self._cursor_pixel = None
            self.update()
            return

        rel_x = (point.x() - self._display_rect.left()) / self._display_rect.width()
        rel_y = (point.y() - self._display_rect.top()) / self._display_rect.height()
        rel_x = max(0.0, min(1.0, rel_x))
        rel_y = max(0.0, min(1.0, rel_y))
        scale_x = self._pix.width() / self._display_rect.width()
        scale_y = self._pix.height() / self._display_rect.height()
        image_x = (point.x() - self._display_rect.left()) * scale_x
        image_y = (point.y() - self._display_rect.top()) * scale_y
        self._cursor_rel = (rel_x, rel_y)
        self._cursor_pixel = (image_x, image_y)
        self.update()

    def leaveEvent(self, event: QEvent) -> None:  # type: ignore[override]
        del event
        if self._cursor_rel is not None or self._cursor_pixel is not None:
            self._cursor_rel = None
            self._cursor_pixel = None
            self.update()

    def clear_target_cross(self) -> None:
        """Remove the movable cross overlay."""

        self._target_rel = None
        self._target_pixel = None
        self._tracking_active = False
        self._tracking_reference = None
        self.update()

    def on_stage_movement_started(self) -> None:
        """Prepare to follow the selected target when the stage begins moving."""

        if self._target_rel is None or self._current_frame_gray is None:
            self.clear_target_cross()
            return
        self._tracking_active = True
        self._tracking_reference = self._current_frame_gray.copy()

    def on_stage_movement_finished(self, _success: bool, _message: str) -> None:
        """Reset tracking state once the stage reports it has stopped."""

        self._tracking_active = False
        self._tracking_reference = None

    def set_calibration_matrix(
        self, m00: float, m01: float, m10: float, m11: float
    ) -> None:
        """Receive the latest pixel-to-millimeter calibration matrix."""

        matrix = np.array([[m00, m01], [m10, m11]], dtype=float)
        if not np.any(matrix):
            self._pixels_to_mm = None
        else:
            self._pixels_to_mm = matrix
        self.update()

    def _handle_tracking(self, current_frame: np.ndarray) -> None:
        if self._target_rel is None or self._pix is None:
            self._tracking_reference = None
            return
        if self._tracking_reference is None:
            self._tracking_reference = current_frame.copy()
            return
        if self._tracking_reference.shape != current_frame.shape:
            self._tracking_reference = current_frame.copy()
            return
        shift_x, shift_y = self._estimate_shift(self._tracking_reference, current_frame)
        if math.hypot(shift_x, shift_y) > 0.05:
            self._apply_shift_to_target(shift_x, shift_y)
        self._tracking_reference = current_frame.copy()

    def _apply_shift_to_target(self, shift_x: float, shift_y: float) -> None:
        if self._pix is None or self._target_rel is None:
            return
        width = self._pix.width()
        height = self._pix.height()
        target_x = self._target_rel[0] * width
        target_y = self._target_rel[1] * height
        new_x = target_x - shift_x
        new_y = target_y - shift_y
        new_x = max(0.0, min(float(width), new_x))
        new_y = max(0.0, min(float(height), new_y))
        rel_x = new_x / width if width else 0.0
        rel_y = new_y / height if height else 0.0
        self._target_rel = (rel_x, rel_y)
        self._target_pixel = (new_x, new_y)

    def _draw_coordinate_overlay(self, painter: QPainter) -> None:
        if self._pix is None:
            return
        lines: list[str] = []
        cursor_offsets = self._compute_offsets(self._cursor_pixel)
        target_offsets = self._compute_offsets(self._target_pixel)
        if cursor_offsets is not None:
            pixel_offset, mm_offset = cursor_offsets
            lines.append(
                self._format_coordinate_line("Cursor", pixel_offset, mm_offset)
            )
        if target_offsets is not None:
            pixel_offset, mm_offset = target_offsets
            lines.append(
                self._format_coordinate_line("Target", pixel_offset, mm_offset)
            )
        if not lines:
            return
        margin = 12
        top = self._display_rect.top() + margin if self._display_rect else margin
        left = self._display_rect.left() + margin if self._display_rect else margin
        self._draw_text_block(painter, lines, left, top)

    def _draw_text_block(
        self, painter: QPainter, lines: Iterable[str], left: int, top: int
    ) -> None:
        lines = list(lines)
        if not lines:
            return
        metrics = painter.fontMetrics()
        line_spacing = metrics.lineSpacing()
        width = max(metrics.horizontalAdvance(line) for line in lines)
        height = line_spacing * len(lines)
        padding = 6
        rect = QRect(
            left - padding,
            top - padding,
            width + padding * 2,
            height + padding * 2,
        )
        painter.fillRect(rect, QColor(0, 0, 0, 160))
        painter.setPen(QPen(Qt.white))
        for index, line in enumerate(lines):
            y = top + index * line_spacing + metrics.ascent()
            painter.drawText(left, y, line)

    def _draw_scale_bar(self, painter: QPainter) -> None:
        if self._pixels_to_mm is None or not self._pix or not self._display_rect:
            return
        column_x = self._pixels_to_mm[:, 0]
        column_y = self._pixels_to_mm[:, 1]
        mm_per_pixel = (np.linalg.norm(column_x) + np.linalg.norm(column_y)) / 2.0
        if mm_per_pixel <= 0:
            return
        pixels_per_mm = 1.0 / mm_per_pixel
        scale_factor = self._display_rect.width() / self._pix.width()
        display_pixels_per_mm = pixels_per_mm * scale_factor
        candidates = [0.01, 0.02, 0.05, 0.1, 0.2, 0.5, 1.0, 2.0, 5.0]
        desired_length = candidates[0]
        for length in candidates:
            bar_pixels = length * display_pixels_per_mm
            if 80 <= bar_pixels <= self._display_rect.width() * 0.4:
                desired_length = length
        bar_pixels = desired_length * display_pixels_per_mm
        if bar_pixels < 10:
            return
        margin = 20
        base_y = self._display_rect.bottom() - margin
        start_x = self._display_rect.left() + margin
        end_x = start_x + int(bar_pixels)
        tick = 6
        painter.setPen(QPen(Qt.white, 2))
        painter.drawLine(start_x, base_y, end_x, base_y)
        painter.drawLine(start_x, base_y, start_x, base_y - tick)
        painter.drawLine(end_x, base_y, end_x, base_y - tick)
        label = self._format_scale_label(desired_length)
        metrics = painter.fontMetrics()
        text_width = metrics.horizontalAdvance(label)
        text_x = start_x + (end_x - start_x - text_width) // 2
        text_y = base_y - tick - 4
        background_rect = QRect(
            text_x - 4,
            text_y - metrics.ascent() - 2,
            text_width + 8,
            metrics.height() + 4,
        )
        painter.fillRect(background_rect, QColor(0, 0, 0, 160))
        painter.drawText(text_x, text_y, label)

    def _compute_offsets(
        self, pixel_coords: tuple[float, float] | None
    ) -> tuple[tuple[float, float], tuple[float, float] | None] | None:
        if pixel_coords is None or self._pix is None:
            return None
        width = self._pix.width()
        height = self._pix.height()
        dx = pixel_coords[0] - width / 2.0
        dy = height / 2.0 - pixel_coords[1]
        mm_offset: tuple[float, float] | None = None
        if self._pixels_to_mm is not None:
            vector = np.array([dx, dy], dtype=float)
            mm_vector = self._pixels_to_mm @ vector
            mm_offset = (float(mm_vector[0]), float(mm_vector[1]))
        return ((dx, dy), mm_offset)

    @staticmethod
    def _format_coordinate_line(
        label: str,
        pixel_offset: tuple[float, float],
        mm_offset: tuple[float, float] | None,
    ) -> str:
        px_x, px_y = pixel_offset
        text = f"{label} Δpx ({px_x:+.1f}, {px_y:+.1f})"
        if mm_offset is not None:
            mm_x, mm_y = mm_offset
            text += f" Δmm ({mm_x:+.4f}, {mm_y:+.4f})"
        return text

    @staticmethod
    def _format_scale_label(length_mm: float) -> str:
        if length_mm < 1.0:
            micrometers = length_mm * 1000.0
            if micrometers >= 100:
                return f"{micrometers:.0f} µm"
            return f"{micrometers:.1f} µm"
        if length_mm < 10.0:
            return f"{length_mm:.1f} mm"
        return f"{length_mm:.0f} mm"

    @staticmethod
    def _estimate_shift(
        reference: np.ndarray, current: np.ndarray
    ) -> tuple[float, float]:
        window = cv2.createHanningWindow((reference.shape[1], reference.shape[0]), cv2.CV_32F)
        (shift_x, shift_y), _ = cv2.phaseCorrelate(
            reference.astype(np.float32), current.astype(np.float32), window
        )
        return float(shift_x), float(-shift_y)

    @staticmethod
    def _qimage_to_gray(image: QImage) -> np.ndarray:
        converted = image.convertToFormat(QImage.Format_RGB888)
        width = converted.width()
        height = converted.height()
        ptr = converted.constBits()
        array = np.frombuffer(ptr, np.uint8, count=converted.sizeInBytes())
        array = array.reshape((height, converted.bytesPerLine()))
        array = array[:, : width * 3].reshape((height, width, 3))
        gray = cv2.cvtColor(array, cv2.COLOR_RGB2GRAY)
        return gray


__all__ = ["MicroscopeView"]
