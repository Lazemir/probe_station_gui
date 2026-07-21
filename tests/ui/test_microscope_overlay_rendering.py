from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QRect
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtWidgets import QApplication

from probe_station_gui.views.microscope_overlay_rendering import (
    _scale_bar_layout,
    draw_scale_bar,
)


_QT_APP = QApplication.instance() or QApplication([])


def test_scale_bar_uses_legacy_nice_length_and_exact_geometry() -> None:
    display_rect = QRect(0, 0, 1000, 600)

    layout = _scale_bar_layout(display_rect, scale_x=1.0, mm_per_pixel_x=0.001)

    assert layout is not None
    assert layout.length_mm == 0.1
    assert layout.left == 14.0
    assert layout.right == 114.0
    assert layout.y == 585.0
    assert layout.label == "100 µm"
    assert layout.label_rect.x() == 14.0
    assert layout.label_rect.y() == 564.0
    assert layout.label_rect.width() == 100.0
    assert layout.label_rect.height() == 14.0

    canvas = QImage(1000, 600, QImage.Format_ARGB32)
    canvas.fill(QColor("black"))
    painter = QPainter(canvas)
    draw_scale_bar(painter, display_rect, 1.0, 0.001)
    painter.end()

    assert canvas.pixelColor(64, 585).value() > 150
    assert canvas.pixelColor(64, 563) == QColor("black")


def test_scale_bar_omits_when_no_nice_length_is_between_60_and_150_pixels() -> None:
    display_rect = QRect(0, 0, 1000, 600)

    assert _scale_bar_layout(display_rect, 1.0, 1.0) is None
