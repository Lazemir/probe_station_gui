"""Microscope view widget displaying frames and mouse interactions."""

from __future__ import annotations

from PySide6.QtCore import QPoint, QRect, Qt, Signal
from PySide6.QtGui import (
    QColor,
    QImage,
    QMouseEvent,
    QPainter,
    QPen,
    QPixmap,
)
from PySide6.QtWidgets import QWidget


class MicroscopeView(QWidget):
    """Widget that renders camera frames with overlay graphics."""

    clicked: Signal = Signal(float, float, float, float)

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Microscope Qt")
        self.setMinimumSize(960, 720)
        self._pix: QPixmap | None = None
        self._target_rel: tuple[float, float] | None = None
        self._display_rect: QRect | None = None

    def set_frame(self, qimg: QImage) -> None:
        self._pix = QPixmap.fromImage(qimg)
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
        self.clicked.emit(dx, dy, rel_x, rel_y)
        self.update()

    def clear_target_cross(self) -> None:
        """Remove the movable cross overlay."""

        self._target_rel = None
        self.update()


__all__ = ["MicroscopeView"]
