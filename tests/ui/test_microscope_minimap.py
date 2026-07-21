from __future__ import annotations

from dataclasses import dataclass
import os


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QRect, QRectF
from PySide6.QtGui import QColor, QImage, QPixmap
from PySide6.QtWidgets import QApplication

from probe_station_gui.views.microscope_minimap import MicroscopeMinimap


@dataclass(frozen=True)
class _Document:
    document_id: str


class _DeferredRenderer:
    pass


def _document(document_id: str) -> _Document:
    return _Document(document_id=document_id)


def _bounds() -> tuple[float, float, float, float]:
    return (0.0, 0.0, 100.0, 100.0)


def _pixmap(color: str) -> QPixmap:
    QApplication.instance() or QApplication([])
    image = QImage(2, 2, QImage.Format_ARGB32)
    image.fill(QColor({"old": "red", "new": "green"}.get(color, color)))
    return QPixmap.fromImage(image)


def test_stale_background_result_cannot_replace_latest_generation() -> None:
    minimap = MicroscopeMinimap(renderer=_DeferredRenderer())
    first = minimap.configure(_document("first"), _bounds())
    second = minimap.configure(_document("second"), _bounds())

    minimap.accept_background(first, _pixmap("old"))
    minimap.accept_background(second, _pixmap("new"))

    assert minimap.background_cache_key.document_id == "second"
    assert minimap.background_pixel(0, 0) == QColor("green")


def test_click_delay_leaves_room_for_double_click_event() -> None:
    app = QApplication.instance() or QApplication([])
    original_interval = app.doubleClickInterval()
    try:
        app.setDoubleClickInterval(250)
        assert minimap_delay() == 300
    finally:
        app.setDoubleClickInterval(original_interval)


def minimap_delay() -> int:
    return MicroscopeMinimap.click_delay_ms()


def test_visible_fov_rect_stays_inside_content_rect() -> None:
    content = QRect(10, 20, 100, 80)
    raw_fov = QRectF(-25.0, 30.0, 170.0, 60.0)

    visible = MicroscopeMinimap._visible_minimap_fov_rect(raw_fov, content)

    bounds = QRectF(content).adjusted(1.0, 1.0, -1.0, -1.0)
    assert visible.width() > 0.0
    assert visible.height() > 0.0
    assert visible.left() >= bounds.left()
    assert visible.right() <= bounds.right()
    assert visible.top() >= bounds.top()
    assert visible.bottom() <= bounds.bottom()
