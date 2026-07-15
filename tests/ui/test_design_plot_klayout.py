from __future__ import annotations

import os
from pathlib import Path

import pytest


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")
pytest.importorskip("pyqtgraph")

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication

from probe_station_gui.design.klayout_types import KLayoutConfig, SnapResponse
from probe_station_gui.design.model import DesignDocument, SnapResult
from probe_station_gui.views import design_plot_pane as plot_module
from probe_station_gui.views.design_navigator_panel import (
    DesignLayoutWindow,
    DesignNavigatorPanel,
)


@pytest.fixture(scope="module")
def qt_app() -> QApplication:
    return QApplication.instance() or QApplication([])


class _RasterController(QObject):
    failed = Signal(str)

    def __init__(self, *_args, **_kwargs) -> None:
        super().__init__()
        self.config = None
        self.documents = []
        self.generation = 0
        self.shutdown_calls = 0
        self.closed = False

    def set_document(self, document) -> None:
        self.documents.append(document)
        if self.closed:
            self.config = None
            return
        if document is None:
            self.config = None
            return
        self.generation += 1
        self.config = KLayoutConfig(
            path=document.path,
            top_cell_name=document.top_cell_name,
            visible_layers=document.visible_layers,
            source_bounds=document.cell_bounds[document.top_cell_name],
            display_bounds=document.bounds,
            rotation_quarter_turns=document.rotation_quarter_turns,
            generation=self.generation,
        )

    def shutdown(self) -> None:
        self.shutdown_calls += 1
        self.closed = True
        self.config = None


class _SnapWorker(QObject):
    loaded = Signal(object)
    snap_ready = Signal(object)
    failed = Signal(str)

    instances = []

    def __init__(self, *_args, **_kwargs) -> None:
        super().__init__()
        self.hover_requests = []
        self.click_requests = []
        self.stop_calls = []
        self.instances.append(self)

    def submit_hover(self, request) -> None:
        self.hover_requests.append(request)

    def submit_click(self, request) -> None:
        self.click_requests.append(request)

    def stop(self, timeout_s: float = 1.0) -> None:
        self.stop_calls.append(timeout_s)


@pytest.fixture
def pane(monkeypatch, qt_app: QApplication):
    _SnapWorker.instances.clear()
    monkeypatch.setattr(plot_module, "KLayoutRasterController", _RasterController)
    monkeypatch.setattr(plot_module, "KLayoutSnapWorker", _SnapWorker)
    widget = plot_module._DesignPlotPane()
    yield widget
    widget.shutdown()
    widget.deleteLater()


def _document(path: Path) -> DesignDocument:
    return DesignDocument(
        path=path,
        library=None,
        top_cell=None,
        top_cell_name="TOP",
        cell_names=("TOP",),
        dbu=1e-6,
        user_unit=1e-9,
        bounds=(0.0, 0.0, 100.0, 50.0),
        polygons_by_layer={},
        visible_layers=frozenset({(1, 0)}),
        file_backed=True,
        available_layers=frozenset({(1, 0), (2, 0)}),
        cell_bounds={"TOP": (0.0, 0.0, 100.0, 50.0)},
    )


def test_file_backed_document_never_calls_legacy_plot_or_global_snap(
    pane, monkeypatch, tmp_path: Path
) -> None:
    def fail(*_args, **_kwargs):
        raise AssertionError("legacy geometry path was called")

    monkeypatch.setattr(DesignDocument, "visible_plot_paths", fail)
    monkeypatch.setattr(DesignDocument, "build_snap_geometry", fail)
    monkeypatch.setattr(DesignDocument, "snap_point_info", fail)

    pane.set_document(_document(tmp_path / "layout.gds"))

    assert pane._raster_controller.config is not None
    assert len(_SnapWorker.instances) == 1
    assert pane._layer_items == []


def test_file_configuration_changes_reuse_snap_worker_and_generation(
    pane, tmp_path: Path
) -> None:
    document = _document(tmp_path / "layout.gds")
    pane.set_document(document)
    worker = pane._snap_worker
    first_generation = pane._klayout_config.generation

    pane.set_document(document.with_visible_layers({(2, 0)}))

    assert pane._snap_worker is worker
    assert pane._klayout_config.generation > first_generation

    pane.set_document(_document(tmp_path / "other.gds"))
    assert worker.stop_calls == [0.0]
    assert pane._snap_worker is not worker


def test_hover_is_replaceable_and_click_waits_for_matching_current_response(
    pane, tmp_path: Path
) -> None:
    pane.set_document(_document(tmp_path / "layout.gds"))
    worker = pane._snap_worker
    moves = []
    pane.move_requested.connect(lambda x, y: moves.append((x, y)))

    pane._submit_file_backed_hover((1.0, 2.0))
    pane._submit_file_backed_hover((3.0, 4.0))
    pane._submit_file_backed_click("move", (5.0, 6.0))
    click = worker.click_requests[-1]

    worker.snap_ready.emit(
        SnapResponse(
            request_id=click.request_id + 1,
            config_generation=click.config.generation,
            raw_point=click.point,
            result=SnapResult((50.0, 60.0), "vertex", 0.1),
            elapsed_ms=1.0,
            shapes_inspected=1,
            purpose="click",
        )
    )
    assert moves == []

    worker.snap_ready.emit(
        SnapResponse(
            request_id=click.request_id,
            config_generation=click.config.generation,
            raw_point=click.point,
            result=SnapResult((50.0, 60.0), "vertex", 0.1),
            elapsed_ms=1.0,
            shapes_inspected=1,
            purpose="click",
        )
    )

    assert len(worker.hover_requests) == 2
    assert moves == [(50.0, 60.0)]


@pytest.mark.parametrize(
    ("action", "payload", "signal_name", "expected"),
    [
        ("route_pick", ("array_origin",), "route_pick_requested", ("array_origin", 7.0, 8.0)),
        ("route_point", (), "route_point_requested", (7.0, 8.0)),
        ("move", (), "move_requested", (7.0, 8.0)),
        ("calibration", (0,), "calibration_point_selected", (0, 7.0, 8.0)),
        ("calibration", (1,), "calibration_point_selected", (1, 7.0, 8.0)),
    ],
)
def test_snap_disabled_executes_all_existing_click_actions_immediately(
    pane, tmp_path: Path, action: str, payload: tuple, signal_name: str, expected: tuple
) -> None:
    pane.set_document(_document(tmp_path / "layout.gds"))
    pane.set_snap_enabled(False)
    results = []
    getattr(pane, signal_name).connect(lambda *args: results.append(args))

    pane._submit_file_backed_click(action, (7.0, 8.0), payload)

    assert results == [expected]
    assert pane._snap_worker.click_requests == []


@pytest.mark.parametrize("mode", ["segment", "segment_center"])
def test_hover_highlights_full_segment_for_line_and_center(pane, mode: str) -> None:
    pane._set_hover_snap(
        SnapResult(
            point=(5.0, 0.0),
            mode=mode,
            distance=0.1,
            segment_start=(0.0, 0.0),
            segment_end=(10.0, 0.0),
        )
    )

    x_data, y_data = pane._hover_segment_item.getData()
    assert tuple(x_data) == (0.0, 10.0)
    assert tuple(y_data) == (0.0, 0.0)


@pytest.mark.parametrize(
    ("mode", "label"),
    [("segment_center", "Center"), ("segment", "Line"), ("vertex", "Corner")],
)
def test_snap_hint_names_center_line_and_corner(
    qt_app: QApplication, mode: str, label: str
) -> None:
    panel = DesignNavigatorPanel()
    panel.set_hover_snap(SnapResult((1.0, 2.0), mode, 0.25))

    assert panel._snap_hint_label.text().startswith(f"Hover snap: {label}")
    panel.deleteLater()


def test_unload_and_close_stop_workers(pane, tmp_path: Path) -> None:
    pane.set_document(_document(tmp_path / "layout.gds"))
    snap_worker = pane._snap_worker
    raster_controller = pane._raster_controller

    pane.set_document(None)

    assert snap_worker.stop_calls
    assert raster_controller.config is None

    pane.shutdown()
    assert raster_controller.shutdown_calls == 1


def test_design_window_close_detaches_workers_and_reopen_restores_document(
    monkeypatch, qt_app: QApplication, tmp_path: Path
) -> None:
    _SnapWorker.instances.clear()
    monkeypatch.setattr(plot_module, "KLayoutRasterController", _RasterController)
    monkeypatch.setattr(plot_module, "KLayoutSnapWorker", _SnapWorker)
    window = DesignLayoutWindow()
    document = _document(tmp_path / "layout.gds")
    window.set_document(document)
    first_snap_worker = window._main_view._snap_worker
    window.show()
    qt_app.processEvents()

    window.close()
    qt_app.processEvents()

    assert first_snap_worker.stop_calls
    assert window._main_view._klayout_config is None

    window.show_and_raise()
    qt_app.processEvents()

    assert window._main_view._document is document
    assert window._main_view._klayout_config is not None
    assert window._main_view._snap_worker is not first_snap_worker
    window._main_view.shutdown()
    window.deleteLater()
