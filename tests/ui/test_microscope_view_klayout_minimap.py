from __future__ import annotations

from dataclasses import replace
import os
from pathlib import Path
import time

import klayout.db as db
import numpy as np
import pytest


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, QRect, QSize, Signal
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtWidgets import QApplication

from probe_station_gui.design.klayout_types import RenderFailure, RenderFrame
from probe_station_gui.design.model import DesignDocument
from probe_station_gui.views.microscope_minimap import MicroscopeMinimap


@pytest.fixture(scope="module")
def qt_app() -> QApplication:
    return QApplication.instance() or QApplication([])


class _RenderWorker(QObject):
    loaded = Signal(object)
    frame_ready = Signal(object)
    failed = Signal(object)
    finished = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.requests = []
        self.stop_calls: list[float] = []

    def submit(self, request) -> None:
        self.requests.append(request)

    def stop(self, timeout_s: float = 1.0) -> None:
        self.stop_calls.append(timeout_s)


class _BlockingStopRenderWorker(_RenderWorker):
    def stop(self, timeout_s: float = 1.0) -> None:
        super().stop(timeout_s)
        time.sleep(max(0.0, float(timeout_s)))


def _document(
    path: Path,
    *,
    visible_layers: frozenset[tuple[int, int]] = frozenset({(1, 0)}),
    turns: int = 0,
) -> DesignDocument:
    source_bounds = (10.0, 20.0, 110.0, 70.0)
    display_bounds = (35.0, -5.0, 85.0, 95.0) if turns % 2 else source_bounds
    return DesignDocument(
        path=path,
        library=None,
        top_cell=None,
        top_cell_name="TOP",
        cell_names=("TOP",),
        dbu=1e-6,
        user_unit=1e-9,
        bounds=display_bounds,
        polygons_by_layer={},
        visible_layers=visible_layers,
        file_backed=True,
        available_layers=frozenset({(1, 0), (2, 0)}),
        cell_bounds={"TOP": source_bounds},
        rotation_quarter_turns=turns,
    )


def _legacy_document(path: Path) -> DesignDocument:
    polygon = np.asarray(
        [(0.0, 0.0), (10.0, 0.0), (10.0, 5.0), (0.0, 5.0)],
        dtype=float,
    )
    return DesignDocument(
        path=path,
        library=None,
        top_cell=None,
        top_cell_name="TOP",
        cell_names=("TOP",),
        dbu=1e-6,
        user_unit=1e-9,
        bounds=(0.0, 0.0, 10.0, 5.0),
        polygons_by_layer={(1, 0): (polygon,)},
        visible_layers=frozenset({(1, 0)}),
    )


def _set_document(minimap: MicroscopeMinimap, document: DesignDocument | None) -> None:
    minimap.configure(
        document,
        document.bounds if document is not None else (0.0, 0.0, 1.0, 1.0),
        targets=[],
        selected_target_id=None,
        probe_route=None,
        selected_route_point_index=-1,
        selected_design_point=None,
        current_design_position=None,
        fov_design_size=None,
        source_design_marks=[],
        check_design_marks=[],
    )


def _view_with_workers(
    worker_type: type[_RenderWorker] = _RenderWorker,
) -> tuple[MicroscopeMinimap, list[_RenderWorker]]:
    workers: list[_RenderWorker] = []

    def factory() -> _RenderWorker:
        worker = worker_type()
        workers.append(worker)
        return worker

    view = MicroscopeMinimap(worker_factory=factory)
    return view, workers


def _drain_events(app: QApplication) -> None:
    for _ in range(4):
        app.processEvents()


def _process_until(
    app: QApplication,
    predicate,
    *,
    timeout_s: float = 5.0,
) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError("timed out waiting for queued Qt delivery")


def _write_tall_design(path: Path) -> None:
    layout = db.Layout()
    layout.dbu = 0.001
    top = layout.create_cell("TOP")
    top.shapes(layout.layer(1, 0)).insert(db.Box(0, 0, 50_000, 115_000))
    layout.write(str(path))


def _write_reload_design(path: Path, *, box_left_um: float) -> None:
    layout = db.Layout()
    layout.dbu = 0.001
    top = layout.create_cell("TOP")
    left = round(box_left_um * 1000.0)
    top.shapes(layout.layer(1, 0)).insert(db.Box(left, 40_000, left + 10_000, 50_000))
    top.shapes(layout.layer(9, 0)).insert(db.Box(0, 0, 1_000, 1_000))
    top.shapes(layout.layer(9, 0)).insert(db.Box(99_000, 99_000, 100_000, 100_000))
    layout.write(str(path))


def _frame(
    request,
    *,
    request_id: int | None = None,
    config_generation: int | None = None,
    top_color: str = "#ef5350",
    bottom_color: str = "#42a5f5",
) -> RenderFrame:
    image = QImage(request.pixel_width, request.pixel_height, QImage.Format_ARGB32)
    image.fill(QColor(bottom_color))
    for x_value in range(image.width()):
        image.setPixelColor(x_value, 0, QColor(top_color))
    return RenderFrame(
        request_id=request.request_id if request_id is None else request_id,
        config_generation=(
            request.config.generation
            if config_generation is None
            else config_generation
        ),
        viewport_generation=request.viewport_generation,
        world_box=request.world_box,
        pixel_width=request.pixel_width,
        pixel_height=request.pixel_height,
        density=request.density,
        image=image,
        elapsed_ms=1.0,
        purpose=request.purpose,
    )


def _draw_canvas(minimap: MicroscopeMinimap) -> QImage:
    canvas = QImage(1000, 1000, QImage.Format_ARGB32)
    canvas.fill(QColor("black"))
    painter = QPainter(canvas)
    minimap.draw(painter, QRect(0, 0, 1000, 1000))
    painter.end()
    return canvas


def test_file_backed_draw_submits_physical_full_bounds_request(
    qt_app: QApplication,
    tmp_path: Path,
) -> None:
    workers: list[_RenderWorker] = []

    def factory() -> _RenderWorker:
        worker = _RenderWorker()
        workers.append(worker)
        return worker

    minimap = MicroscopeMinimap(
        worker_factory=factory,
        device_pixel_ratio=lambda: 2.0,
    )
    document = _document(tmp_path / "layout.gds")
    _set_document(minimap, document)

    _draw_canvas(minimap)
    _drain_events(qt_app)

    request = workers[0].requests[-1]
    assert request.purpose == "minimap"
    assert request.world_box == document.bounds
    assert request.config.path == document.path.resolve()
    assert request.config.visible_layers == document.visible_layers
    assert request.config.source_bounds == document.cell_bounds["TOP"]
    assert request.config.display_bounds == document.bounds
    assert request.pixel_width > 224
    assert request.pixel_height > 100
    minimap.shutdown()


def test_only_latest_klayout_generation_becomes_visible(
    qt_app: QApplication,
    tmp_path: Path,
) -> None:
    minimap, workers = _view_with_workers()
    first = _document(tmp_path / "layout.gds")
    _set_document(minimap, first)
    _draw_canvas(minimap)
    _drain_events(qt_app)
    first_request = workers[0].requests[-1]

    changed = _document(
        first.path,
        visible_layers=frozenset({(2, 0)}),
        turns=1,
    )
    _set_document(minimap, changed)
    _draw_canvas(minimap)
    _drain_events(qt_app)
    second_request = workers[0].requests[-1]

    workers[0].frame_ready.emit(_frame(first_request, bottom_color="#ab47bc"))
    assert _draw_canvas(minimap).pixelColor(862, 145) != QColor("#ab47bc")

    workers[0].frame_ready.emit(_frame(second_request))
    assert _draw_canvas(minimap).pixelColor(862, 145) == QColor("#42a5f5")
    minimap.shutdown()


def test_resize_draws_coalesce_to_latest_request(
    qt_app: QApplication,
    tmp_path: Path,
) -> None:
    minimap, workers = _view_with_workers()
    _set_document(minimap, _document(tmp_path / "layout.gds"))

    for side in (700, 900, 1000):
        canvas = QImage(side, side, QImage.Format_ARGB32)
        painter = QPainter(canvas)
        minimap.draw(painter, QRect(0, 0, side, side))
        painter.end()

    assert workers == []
    _drain_events(qt_app)
    assert len(workers) == 1
    assert len(workers[0].requests) == 1
    assert workers[0].requests[0].pixel_width == 212
    assert workers[0].requests[0].pixel_height == 106
    minimap.shutdown()


def test_document_replacement_and_shutdown_retire_workers_bounded(
    qt_app: QApplication,
    tmp_path: Path,
) -> None:
    minimap, workers = _view_with_workers(_BlockingStopRenderWorker)
    first = replace(_document(tmp_path / "first.gds"), source_load_id="first")
    _set_document(minimap, first)
    _draw_canvas(minimap)
    _drain_events(qt_app)

    started = time.monotonic()
    second = replace(_document(tmp_path / "second.gds"), source_load_id="second")
    _set_document(minimap, second)
    assert time.monotonic() - started < 0.1
    assert workers[0].stop_calls == [0.0]

    _draw_canvas(minimap)
    _drain_events(qt_app)
    started = time.monotonic()
    minimap.shutdown()
    assert time.monotonic() - started < 0.1
    assert workers[1].stop_calls == [0.0]


def test_render_failure_retries_once_via_same_worker(
    qt_app: QApplication,
    tmp_path: Path,
) -> None:
    minimap, workers = _view_with_workers()
    _set_document(minimap, _document(tmp_path / "retry.gds"))
    _draw_canvas(minimap)
    _drain_events(qt_app)
    first = workers[0].requests[0]

    workers[0].failed.emit(
        RenderFailure(
            first.request_id,
            first.config.generation,
            first.viewport_generation,
            "minimap",
            "render failed",
        )
    )
    _drain_events(qt_app)
    retry = workers[0].requests[-1]
    assert retry.request_id > first.request_id

    workers[0].failed.emit(
        RenderFailure(
            retry.request_id,
            retry.config.generation,
            retry.viewport_generation,
            "minimap",
            "still failed",
        )
    )
    _drain_events(qt_app)
    _draw_canvas(minimap)
    _drain_events(qt_app)
    assert len(workers[0].requests) == 2
    minimap.shutdown()


def test_real_legacy_renderer_returns_image_without_qpixmap_worker_use(
    tmp_path: Path,
) -> None:
    from probe_station_gui.views.microscope_minimap_background import (
        ThreadedLegacyRenderer,
    )

    image, point_count = ThreadedLegacyRenderer.render_image(
        _legacy_document(tmp_path / "legacy.gds"),
        QSize(100, 80),
    )

    assert not image.isNull()
    assert point_count == 4
