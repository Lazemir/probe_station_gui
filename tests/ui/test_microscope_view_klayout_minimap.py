from __future__ import annotations

import os
from pathlib import Path
import time

import numpy as np
import pytest


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, QSize, Signal
from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QApplication

from probe_station_gui.design.klayout_types import RenderFrame
from probe_station_gui.design.model import DesignDocument
from probe_station_gui.views.microscope_view import MicroscopeView


@pytest.fixture(scope="module")
def qt_app() -> QApplication:
    return QApplication.instance() or QApplication([])


class _RenderWorker(QObject):
    loaded = Signal(object)
    frame_ready = Signal(object)
    failed = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self.requests = []
        self.stop_calls: list[float] = []

    def submit(self, request) -> None:
        self.requests.append(request)

    def stop(self, timeout_s: float = 1.0) -> None:
        self.stop_calls.append(timeout_s)


def _document(
    path: Path,
    *,
    visible_layers: frozenset[tuple[int, int]] = frozenset({(1, 0)}),
    turns: int = 0,
) -> DesignDocument:
    source_bounds = (10.0, 20.0, 110.0, 70.0)
    display_bounds = (
        (35.0, -5.0, 85.0, 95.0) if turns % 2 else source_bounds
    )
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


def _set_document(view: MicroscopeView, document: DesignDocument | None) -> None:
    view.set_design_minimap_data(
        document=document,
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


def _view_with_workers() -> tuple[MicroscopeView, list[_RenderWorker]]:
    workers: list[_RenderWorker] = []

    def factory() -> _RenderWorker:
        worker = _RenderWorker()
        workers.append(worker)
        return worker

    view = MicroscopeView(minimap_render_worker_factory=factory)
    return view, workers


def _drain_events(app: QApplication) -> None:
    for _ in range(4):
        app.processEvents()


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
    monkeypatch.setattr(view, "devicePixelRatioF", lambda: 2.0)
    _set_document(view, document)

    assert workers == []
    assert view._design_background_for_size(QSize(120, 80)) is None
    _drain_events(qt_app)
    time.sleep(0.01)

    assert visible_calls == []
    assert len(workers) == 1
    request = workers[0].requests[-1]
    assert request.purpose == "minimap"
    assert request.world_box == document.bounds
    assert (request.pixel_width, request.pixel_height) == (240, 160)
    assert request.config.path == document.path.resolve()
    assert request.config.top_cell_name == "TOP"
    assert request.config.visible_layers == document.visible_layers
    assert request.config.source_bounds == document.cell_bounds["TOP"]
    assert request.config.display_bounds == document.bounds
    assert request.config.rotation_quarter_turns == 0
    view.close()


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
    assert (request.pixel_width, request.pixel_height) == (140, 100)
    assert request.purpose == "minimap"
    view.close()


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
    view.close()


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
    view.close()


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
    assert workers[1].stop_calls == [0.5]

    _set_document(view, _document(tmp_path / "third.gds"))
    view._design_background_for_size(QSize(100, 80))
    _drain_events(qt_app)
    started = time.monotonic()
    view.close()
    assert time.monotonic() - started < 0.2
    assert workers[2].stop_calls == [0.5]


def test_legacy_in_memory_minimap_keeps_polygon_renderer(tmp_path: Path) -> None:
    image, point_count = MicroscopeView._render_minimap_background_image(
        _legacy_document(tmp_path / "legacy.gds"),
        QSize(100, 80),
    )

    assert not image.isNull()
    assert point_count == 4
