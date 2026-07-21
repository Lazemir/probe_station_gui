from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
from PySide6.QtCore import QObject, QPoint, QRect, QSize, Signal
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtTest import QSignalSpy
from PySide6.QtWidgets import QApplication

from probe_station_gui.design.model import DesignDocument
from probe_station_gui.views.microscope_minimap import MicroscopeMinimap


_APP = QApplication.instance() or QApplication([])


@dataclass
class _DeferredRequest:
    size: QSize
    callback: object


class _DeferredRenderer:
    def __init__(self) -> None:
        self.requests: list[_DeferredRequest] = []

    def render(self, document, size: QSize, callback) -> None:
        self.requests.append(_DeferredRequest(QSize(size), callback))

    def complete(self, index: int, color: str) -> None:
        request = self.requests[index]
        image = QImage(request.size, QImage.Format_ARGB32_Premultiplied)
        image.fill(QColor(color))
        request.callback(image)


class _Worker(QObject):
    loaded = Signal(object)
    frame_ready = Signal(object)
    failed = Signal(object)
    finished = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.requests: list[object] = []
        self.stop_calls: list[float] = []

    def submit(self, request: object) -> None:
        self.requests.append(request)

    def stop(self, timeout_s: float = 1.0) -> None:
        self.stop_calls.append(timeout_s)


def _document(document_id: str, *, file_backed: bool = False) -> DesignDocument:
    polygon = np.asarray(
        [(0.0, 0.0), (100.0, 0.0), (100.0, 100.0), (0.0, 100.0)],
        dtype=float,
    )
    return DesignDocument(
        path=Path(f"{document_id}.gds"),
        library=None,
        top_cell=None,
        top_cell_name="TOP",
        cell_names=("TOP",),
        dbu=1e-6,
        user_unit=1e-9,
        bounds=(0.0, 0.0, 100.0, 100.0),
        polygons_by_layer={(1, 0): (polygon,)},
        visible_layers=frozenset({(1, 0)}),
        file_backed=file_backed,
        available_layers=frozenset({(1, 0)}),
        cell_bounds={"TOP": (0.0, 0.0, 100.0, 100.0)},
        source_load_id=document_id,
    )


def _draw(minimap: MicroscopeMinimap, rect: QRect) -> QImage:
    image = QImage(rect.size(), QImage.Format_ARGB32_Premultiplied)
    image.fill(QColor("black"))
    painter = QPainter(image)
    minimap.draw(painter, rect)
    painter.end()
    return image


def test_stale_background_result_cannot_replace_latest_generation() -> None:
    renderer = _DeferredRenderer()
    minimap = MicroscopeMinimap(renderer=renderer)
    display_rect = QRect(0, 0, 1000, 1000)
    sample = QPoint(830, 180)

    minimap.configure(_document("first"), (0.0, 0.0, 100.0, 100.0))
    _draw(minimap, display_rect)
    minimap.configure(_document("second"), (0.0, 0.0, 100.0, 100.0))
    _draw(minimap, display_rect)

    renderer.complete(0, "red")
    renderer.complete(1, "green")

    assert _draw(minimap, display_rect).pixelColor(sample) == QColor("green")


def test_overlay_only_configure_reuses_legacy_background() -> None:
    renderer = _DeferredRenderer()
    minimap = MicroscopeMinimap(renderer=renderer)
    document = _document("same")
    display_rect = QRect(0, 0, 1000, 1000)
    sample = QPoint(830, 180)

    minimap.configure(document, document.bounds)
    _draw(minimap, display_rect)
    renderer.complete(0, "green")
    assert _draw(minimap, display_rect).pixelColor(sample) == QColor("green")

    minimap.configure(
        document,
        document.bounds,
        selected_design_point=(25.0, 75.0),
        current_design_position=(50.0, 50.0),
        fov_design_size=(10.0, 20.0),
        source_design_marks=[(10.0, 10.0)],
        check_design_marks=[(90.0, 90.0)],
    )
    rendered = _draw(minimap, display_rect)

    assert len(renderer.requests) == 1
    assert rendered.pixelColor(sample) == QColor("green")


def test_non_document_configuration_retires_active_worker() -> None:
    workers: list[_Worker] = []

    def worker_factory() -> _Worker:
        worker = _Worker()
        workers.append(worker)
        return worker

    minimap = MicroscopeMinimap(worker_factory=worker_factory)
    file_generation = minimap.configure(
        _document("file", file_backed=True), (0.0, 0.0, 100.0, 100.0)
    )
    _draw(minimap, QRect(0, 0, 1000, 1000))
    QApplication.processEvents()
    invalid_generation = minimap.configure(object(), (0.0, 0.0, 1.0, 1.0))

    assert workers[0].stop_calls == [0.0]
    assert invalid_generation > file_generation
    assert minimap.configure(None, (0.0, 0.0, 1.0, 1.0)) == invalid_generation


def test_delayed_click_uses_latest_painted_minimap_rect() -> None:
    app = QApplication.instance() or QApplication([])
    renderer = _DeferredRenderer()
    minimap = MicroscopeMinimap(renderer=renderer)
    minimap.configure(_document("click"), (0.0, 0.0, 100.0, 100.0))
    first_rect = QRect(0, 0, 1000, 1000)
    latest_rect = QRect(0, 0, 800, 800)
    point = QPoint(760, 100)
    clicked = QSignalSpy(minimap.clicked)
    original_interval = app.doubleClickInterval()
    app.setDoubleClickInterval(1)
    try:
        _draw(minimap, first_rect)
        minimap.queue_click(point, first_rect)
        _draw(minimap, latest_rect)
        expected = minimap.map_click(point, latest_rect)
        assert clicked.wait(1000)
    finally:
        app.setDoubleClickInterval(original_interval)

    assert expected is not None
    assert clicked.count() == 1
    assert tuple(clicked.at(0)) == expected
