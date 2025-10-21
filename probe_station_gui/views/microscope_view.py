"""Microscope view widget displaying frames and mouse interactions."""

from __future__ import annotations

from PySide6.QtCore import QPoint, Qt, Signal
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

    clicked: Signal = Signal(float, float)

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Microscope Qt")
        self.setMinimumSize(960, 720)
        self._pix: QPixmap | None = None
        self._cross: tuple[float, float] | None = None

    def set_frame(self, qimg: QImage) -> None:
        transform = QTransform().rotate(180)
        rotated = qimg.transformed(transform)
        self._pix = QPixmap.fromImage(rotated)
        self.update()

    def paintEvent(self, event) -> None:  # type: ignore[override]
        painter = QPainter(self)
        painter.fillRect(self.rect(), Qt.black)
        if self._pix:
            scaled = self._pix.scaled(self.size(), Qt.KeepAspectRatio)
            pos_x = (self.width() - scaled.width()) // 2
            pos_y = (self.height() - scaled.height()) // 2
            painter.drawPixmap(pos_x, pos_y, scaled)

            painter.setRenderHint(QPainter.Antialiasing)
            painter.setPen(QPen(QColor("lime"), 1))
            center_x = self.width() // 2
            center_y = self.height() // 2
            if self._cross:
                cross_x, cross_y = self._cross
            else:
                cross_x, cross_y = center_x, center_y
            painter.drawLine(0, cross_y, self.width(), cross_y)
            painter.drawLine(cross_x, 0, cross_x, self.height())
            painter.drawEllipse(QPoint(int(cross_x), int(cross_y)), 4, 4)
        painter.end()

    def mousePressEvent(self, event: QMouseEvent) -> None:  # type: ignore[override]
        if event.button() == Qt.LeftButton:
            center_x = self.width() / 2
            center_y = self.height() / 2
            dx = event.position().x() - center_x
            dy = center_y - event.position().y()
            self._cross = (event.position().x(), event.position().y())
            self.clicked.emit(dx, dy)
            self.update()


__all__ = ["MicroscopeView"]
