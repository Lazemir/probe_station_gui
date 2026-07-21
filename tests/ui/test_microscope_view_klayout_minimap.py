from __future__ import annotations

from dataclasses import replace
import os
from pathlib import Path
import threading
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

    view = MicroscopeMinimap(renderer=factory)
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


def test_file_backed_minimap_uses_full_bounds_physical_klayout_request(
    qt_app: QApplication,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    view, workers = _view_with_workers()
    document = _document(tmp_path / "layout.gds")
    visible_calls: list[None] = []

    def observe_visible_polygons(_document: DesignDocument):
        visible_calls.append(None)
        return {}

    monkeypatch.setattr(DesignDocument, "visible_polygons", observe_visible_polygons)
    monkeypatch.setattr(view, "_device_pixel_ratio", lambda: 2.0)
    _set_document(view, document)

    assert workers == []
    assert view._design_background_for_size(QSize(120, 80)) is None
    _drain_events(qt_app)
    time.sleep(0.01)

    assert visible_calls == []
    assert len(workers) == 1
    request = workers[0].requests[-1]
    fitted = view._minimap_design_rect_for_bounds(
        QRect(0, 0, 120, 80),
        document.bounds,
    )
    assert request.purpose == "minimap"
    assert request.world_box == document.bounds
    assert (request.pixel_width, request.pixel_height) == (
        round(fitted.width() * 2.0),
        round(fitted.height() * 2.0),
    )
    assert request.config.path == document.path.resolve()
    assert request.config.top_cell_name == "TOP"
    assert request.config.visible_layers == document.visible_layers
    assert request.config.source_bounds == document.cell_bounds["TOP"]
    assert request.config.display_bounds == document.bounds
    assert request.config.rotation_quarter_turns == 0
    view.shutdown()


def test_real_tall_klayout_raster_aligns_overlay_and_click_aspect_fit(
    qt_app: QApplication,
    tmp_path: Path,
) -> None:
    design_path = tmp_path / "tall.gds"
    _write_tall_design(design_path)
    document = DesignDocument.load(design_path)
    view = MicroscopeMinimap()
    _set_document(view, document)
    content_size = QSize(224, 206)

    assert view._design_background_for_size(content_size) is None
    _process_until(qt_app, lambda: view._minimap_background is not None)
    background = view._minimap_background
    assert background is not None
    source = background.toImage()
    ink_color = source.pixelColor(source.width() // 2, source.height() // 2)

    canvas = QImage(1000, 1000, QImage.Format_ARGB32)
    canvas.fill(QColor("#000000"))
    painter = QPainter(canvas)
    display_rect = QRect(0, 0, 1000, 1000)
    view.draw(painter, display_rect)
    painter.end()
    assert view._minimap_rect is not None
    content_rect = QRect(
        view._minimap_rect.left() + 8,
        view._minimap_rect.top() + 26,
        view._minimap_rect.width() - 16,
        view._minimap_rect.height() - 34,
    )
    matching = [
        (x_value, y_value)
        for y_value in range(content_rect.top(), content_rect.bottom() + 1)
        for x_value in range(content_rect.left(), content_rect.right() + 1)
        if canvas.pixelColor(x_value, y_value) == ink_color
    ]
    assert matching
    ink_left = min(point[0] for point in matching)
    ink_right = max(point[0] for point in matching)
    ink_top = min(point[1] for point in matching)
    ink_bottom = max(point[1] for point in matching)
    left, bottom, right, top = document.bounds
    mapped_top_left = view._map_design_point_to_rect((left, top), content_rect)
    mapped_bottom_right = view._map_design_point_to_rect(
        (right, bottom),
        content_rect,
    )

    assert ink_left == pytest.approx(mapped_top_left.x(), abs=3.0)
    assert ink_top == pytest.approx(mapped_top_left.y(), abs=3.0)
    assert ink_right == pytest.approx(mapped_bottom_right.x(), abs=3.0)
    assert ink_bottom == pytest.approx(mapped_bottom_right.y(), abs=3.0)
    assert (ink_right - ink_left) / (ink_bottom - ink_top) == pytest.approx(
        (right - left) / (top - bottom),
        rel=0.08,
    )
    assert ink_top - content_rect.top() == pytest.approx(6.0, abs=3.0)
    mapped_center = view._map_design_point_to_rect(
        ((left + right) * 0.5, (bottom + top) * 0.5),
        content_rect,
    )
    assert view.map_click(mapped_center, display_rect) == pytest.approx(
        ((left + right) * 0.5, (bottom + top) * 0.5),
    )
    view.shutdown()


def test_real_minimap_reloads_replaced_file_at_same_path(
    qt_app: QApplication,
    tmp_path: Path,
) -> None:
    design_path = tmp_path / "reload.gds"
    replacement_path = tmp_path / "replacement.gds"
    _write_reload_design(design_path, box_left_um=10.0)
    first = DesignDocument.load(design_path).with_visible_layers({(1, 0)})
    view = MicroscopeMinimap()
    _set_document(view, first)
    view._design_background_for_size(QSize(180, 160))
    _process_until(qt_app, lambda: view._minimap_background is not None)
    first_image = view._minimap_background.toImage().copy()

    _write_reload_design(replacement_path, box_left_um=70.0)
    os.replace(replacement_path, design_path)
    second = DesignDocument.load(design_path).with_visible_layers({(1, 0)})
    _set_document(view, second)
    view._design_background_for_size(QSize(180, 160))
    _process_until(
        qt_app,
        lambda: (
            view._minimap_background is not None
            and view._minimap_background_config_generation
            == view._minimap_klayout_config.generation
        ),
    )
    second_image = view._minimap_background.toImage()

    assert first.source_load_id != second.source_load_id
    assert first_image != second_image
    view.shutdown()


def test_resize_coalesces_to_one_newest_minimap_request(
    qt_app: QApplication,
    tmp_path: Path,
) -> None:
    view, workers = _view_with_workers()
    _set_document(view, _document(tmp_path / "layout.gds"))

    view._design_background_for_size(QSize(80, 60))
    view._design_background_for_size(QSize(90, 70))
    view._design_background_for_size(QSize(140, 100))

    assert workers == []
    _drain_events(qt_app)

    assert len(workers) == 1
    assert len(workers[0].requests) == 1
    request = workers[0].requests[0]
    assert (request.pixel_width, request.pixel_height) == (128, 64)
    assert request.purpose == "minimap"
    view.shutdown()


def test_stale_frame_is_ignored_and_last_matching_image_survives_resize(
    qt_app: QApplication,
    tmp_path: Path,
) -> None:
    view, workers = _view_with_workers()
    _set_document(view, _document(tmp_path / "layout.gds"))
    assert view._design_background_for_size(QSize(100, 70)) is None
    _drain_events(qt_app)
    first_request = workers[0].requests[-1]
    workers[0].frame_ready.emit(_frame(first_request))
    first_background = view._design_background_for_size(QSize(100, 70))
    assert first_background is not None

    pending_background = view._design_background_for_size(QSize(130, 90))
    assert pending_background is first_background
    _drain_events(qt_app)
    second_request = workers[0].requests[-1]

    workers[0].frame_ready.emit(
        _frame(first_request, top_color="#66bb6a", bottom_color="#ab47bc")
    )
    workers[0].frame_ready.emit(
        _frame(
            second_request,
            config_generation=second_request.config.generation - 1,
            top_color="#66bb6a",
            bottom_color="#ab47bc",
        )
    )
    assert view._design_background_for_size(QSize(130, 90)) is first_background

    workers[0].frame_ready.emit(_frame(second_request))
    replacement = view._design_background_for_size(QSize(130, 90))
    assert replacement is not None
    assert replacement is not first_background
    replacement_image = replacement.toImage()
    assert replacement_image.pixelColor(0, 0) == QColor("#ef5350")
    assert replacement_image.pixelColor(0, replacement_image.height() - 1) == QColor(
        "#42a5f5"
    )
    view.shutdown()


def test_layer_and_rotation_changes_reuse_path_but_invalidate_frame(
    qt_app: QApplication,
    tmp_path: Path,
) -> None:
    view, workers = _view_with_workers()
    document = _document(tmp_path / "layout.gds")
    _set_document(view, document)
    view._design_background_for_size(QSize(100, 80))
    _drain_events(qt_app)
    first_request = workers[0].requests[-1]
    workers[0].frame_ready.emit(_frame(first_request))
    assert view._design_background_for_size(QSize(100, 80)) is not None

    changed = _document(
        document.path,
        visible_layers=frozenset({(2, 0)}),
        turns=1,
    )
    _set_document(view, changed)
    assert view._minimap_background is None
    assert len(workers) == 1
    assert workers[0].stop_calls == []
    assert view._design_background_for_size(QSize(100, 80)) is None
    _drain_events(qt_app)
    changed_request = workers[0].requests[-1]

    assert changed_request.config.generation > first_request.config.generation
    assert changed_request.config.visible_layers == frozenset({(2, 0)})
    assert changed_request.config.rotation_quarter_turns == 1
    assert changed_request.config.source_bounds == document.cell_bounds["TOP"]
    assert changed_request.config.display_bounds == changed.bounds
    workers[0].frame_ready.emit(_frame(first_request))
    assert view._minimap_background is None
    view.shutdown()


def test_same_path_new_source_retires_minimap_worker(
    qt_app: QApplication,
    tmp_path: Path,
) -> None:
    view, workers = _view_with_workers()
    first = replace(_document(tmp_path / "same.gds"), source_load_id="load-a")
    _set_document(view, first)
    view._design_background_for_size(QSize(100, 80))
    _drain_events(qt_app)
    assert len(workers) == 1

    _set_document(view, first.with_visible_layers({(2, 0)}))
    view._design_background_for_size(QSize(100, 80))
    _drain_events(qt_app)
    assert len(workers) == 1

    _set_document(view, replace(first, source_load_id="load-b"))
    assert workers[0].stop_calls == [0.0]
    assert workers[0] in view._retired_minimap_workers
    view._design_background_for_size(QSize(100, 80))
    _drain_events(qt_app)
    assert len(workers) == 2


def test_path_switch_detaches_and_removal_or_close_stops_bounded(
    qt_app: QApplication,
    tmp_path: Path,
) -> None:
    view, workers = _view_with_workers()
    first = _document(tmp_path / "first.gds")
    _set_document(view, first)
    view._design_background_for_size(QSize(100, 80))
    _drain_events(qt_app)

    same_path = _document(first.path, visible_layers=frozenset({(2, 0)}))
    _set_document(view, same_path)
    view._design_background_for_size(QSize(100, 80))
    _drain_events(qt_app)
    assert len(workers) == 1
    assert workers[0].stop_calls == []

    _set_document(view, _document(tmp_path / "second.gds"))
    assert workers[0].stop_calls == [0.0]
    view._design_background_for_size(QSize(100, 80))
    _drain_events(qt_app)
    assert len(workers) == 2

    started = time.monotonic()
    _set_document(view, None)
    assert time.monotonic() - started < 0.2
    assert workers[1].stop_calls == [0.0]

    _set_document(view, _document(tmp_path / "third.gds"))
    view._design_background_for_size(QSize(100, 80))
    _drain_events(qt_app)
    started = time.monotonic()
    view.shutdown()
    assert time.monotonic() - started < 0.2
    assert workers[2].stop_calls == [0.0]


@pytest.mark.parametrize("action", ["remove", "close"])
def test_removal_and_close_do_not_wait_for_running_worker(
    qt_app: QApplication,
    tmp_path: Path,
    action: str,
) -> None:
    view, workers = _view_with_workers(_BlockingStopRenderWorker)
    _set_document(view, _document(tmp_path / f"{action}.gds"))
    view._design_background_for_size(QSize(100, 80))
    _drain_events(qt_app)

    started = time.monotonic()
    if action == "remove":
        _set_document(view, None)
    else:
        view.shutdown()
    elapsed = time.monotonic() - started

    assert elapsed < 0.1
    assert workers[0].stop_calls == [0.0]


def test_render_failure_retries_once_and_stale_failure_preserves_newer_state(
    qt_app: QApplication,
    tmp_path: Path,
) -> None:
    view, workers = _view_with_workers()
    _set_document(view, _document(tmp_path / "retry.gds"))
    view._design_background_for_size(QSize(100, 80))
    _drain_events(qt_app)
    assert len(workers[0].requests) == 1

    first = workers[0].requests[0]
    workers[0].failed.emit(
        RenderFailure(
            first.request_id + 100,
            first.config.generation,
            first.viewport_generation,
            "minimap",
            "stale",
        )
    )
    assert len(workers[0].requests) == 1
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

    assert len(workers[0].requests) == 2
    assert workers[0].requests[1].request_id > workers[0].requests[0].request_id
    retry = workers[0].requests[1]
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
    assert len(workers[0].requests) == 2
    assert view._design_background_for_size(QSize(100, 80)) is None
    _drain_events(qt_app)
    assert len(workers[0].requests) == 2
    view.shutdown()


def test_failure_for_size_superseded_before_delivery_does_not_retry(
    qt_app: QApplication,
    tmp_path: Path,
) -> None:
    view, workers = _view_with_workers()
    _set_document(view, _document(tmp_path / "superseded.gds"))
    view._design_background_for_size(QSize(100, 80))
    _drain_events(qt_app)
    first = workers[0].requests[-1]
    view._design_background_for_size(QSize(140, 100))

    workers[0].failed.emit(
        RenderFailure(
            first.request_id,
            first.config.generation,
            first.viewport_generation,
            "minimap",
            "superseded",
        )
    )
    _drain_events(qt_app)

    assert len(workers[0].requests) == 2
    assert workers[0].requests[-1].pixel_width != first.pixel_width
    view.shutdown()


def test_retired_minimap_worker_finalizes_on_creator_thread(
    qt_app: QApplication,
    tmp_path: Path,
) -> None:
    creator_thread = threading.get_ident()
    view, workers = _view_with_workers()
    first = replace(_document(tmp_path / "first.gds"), source_load_id="first")
    _set_document(view, first)
    view._design_background_for_size(QSize(100, 80))
    _drain_events(qt_app)
    worker = workers[0]
    delete_threads: list[int] = []
    worker.deleteLater = lambda: delete_threads.append(threading.get_ident())

    started = time.monotonic()
    _set_document(
        view,
        replace(_document(tmp_path / "second.gds"), source_load_id="second"),
    )
    assert time.monotonic() - started < 0.1
    assert worker in view._retired_minimap_workers

    worker.finished.emit()
    qt_app.processEvents()

    assert worker not in view._retired_minimap_workers
    assert delete_threads == [creator_thread]


def test_legacy_in_memory_minimap_keeps_polygon_renderer(tmp_path: Path) -> None:
    image, point_count = MicroscopeMinimap._render_minimap_background_image(
        _legacy_document(tmp_path / "legacy.gds"),
        QSize(100, 80),
    )

    assert not image.isNull()
    assert point_count == 4
