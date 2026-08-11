"""Shared Qt fakes and fixtures for Design plot integration tests."""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication

from probe_station_gui.design.klayout_types import KLayoutConfig
from probe_station_gui.design.markup import MarkupDocument
from probe_station_gui.design.model import DesignDocument
from probe_station_gui.design.navigation_bounds import (
    GDS_FOCUS_PADDING_FRACTION,
    pad_bounds,
)
from probe_station_gui.route.model import MeasurementRoute, RoutePoint
from probe_station_gui.views import design_plot_pane as plot_module
from probe_station_gui.views import design_snap_runtime as snap_runtime_module
from probe_station_gui.views.design_layout_window import DesignLayoutWindow


def qt_app() -> QApplication:
    return QApplication.instance() or QApplication([])


class _RasterController(QObject):
    failed = Signal(object)
    succeeded = Signal()

    def __init__(self, *_args, **_kwargs) -> None:
        super().__init__()
        self._view_box = _args[0]
        self.config = None
        self.documents = []
        self.ranges_seen_on_set_document = []
        self.generation = 0
        self.shutdown_calls = 0
        self.closed = False

    def set_document(self, document) -> None:
        x_range, y_range = self._view_box.viewRange()
        self.ranges_seen_on_set_document.append(
            (x_range[0], y_range[0], x_range[1], y_range[1])
        )
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
            source_load_id=document.source_load_id,
        )

    def shutdown(self) -> None:
        self.shutdown_calls += 1
        self.closed = True
        self.config = None


class _SnapWorker(QObject):
    loaded = Signal(object)
    snap_ready = Signal(object)
    failed = Signal(object)
    lifecycle_failed = Signal(str)
    finished = Signal()

    instances = []

    def __init__(self, *_args, **_kwargs) -> None:
        super().__init__()
        self.hover_requests = []
        self.click_requests = []
        self.stop_calls = []
        self.cancel_hover_calls = 0
        self.cancel_pending_calls = 0
        self.instances.append(self)

    def submit_hover(self, request) -> None:
        self.hover_requests.append(request)

    def submit_click(self, request) -> None:
        self.click_requests.append(request)

    def cancel_hover(self) -> None:
        self.cancel_hover_calls += 1

    def cancel_pending(self) -> None:
        self.cancel_pending_calls += 1

    def stop(self, timeout_s: float = 1.0) -> None:
        self.stop_calls.append(timeout_s)


class _BlockingStopSnapWorker(_SnapWorker):
    def stop(self, timeout_s: float = 1.0) -> None:
        super().stop(timeout_s)
        time.sleep(max(0.0, float(timeout_s)))


def pane(monkeypatch, qt_app: QApplication):
    _SnapWorker.instances.clear()
    monkeypatch.setattr(plot_module, "KLayoutRasterController", _RasterController)
    monkeypatch.setattr(snap_runtime_module, "KLayoutSnapWorker", _SnapWorker)
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


def document(tmp_path: Path) -> DesignDocument:
    source = tmp_path / "layout.gds"
    source.write_bytes(b"gds")
    return _document(source)


def window(monkeypatch, qt_app: QApplication):
    monkeypatch.setattr(plot_module, "KLayoutRasterController", _RasterController)
    monkeypatch.setattr(snap_runtime_module, "KLayoutSnapWorker", _SnapWorker)
    widget = DesignLayoutWindow()
    yield widget
    widget._main_view.shutdown()
    widget.deleteLater()


def distant_hidden_markup(document: DesignDocument) -> MarkupDocument:
    return MarkupDocument.empty(document.path, visible=False).append_guide(
        (1_000_000.0, 0.0),
        (1_000_100.0, 100.0),
        guide_id="distant-guide",
    )


def distant_route(document: DesignDocument) -> MeasurementRoute:
    route = MeasurementRoute.default_for_document(document)
    route.points.append(RoutePoint("far", "Far", (1_000_000.0, 0.0), enabled=False))
    return route


def _view_box(pane):
    return pane._plot.getViewBox()


def _box(pane):
    x_range, y_range = _view_box(pane).viewRange()
    return (x_range[0], y_range[0], x_range[1], y_range[1])


def _assert_gds_focus(pane, document: DesignDocument) -> None:
    visible = _box(pane)
    padded = pad_bounds(document.bounds, GDS_FOCUS_PADDING_FRACTION)
    frame = pane._navigation_frame
    assert visible[0] <= padded[0]
    assert visible[1] <= padded[1]
    assert visible[2] >= padded[2]
    assert visible[3] >= padded[3]
    assert (visible[0] + visible[2]) * 0.5 == pytest.approx(
        (padded[0] + padded[2]) * 0.5
    )
    assert (visible[1] + visible[3]) * 0.5 == pytest.approx(
        (padded[1] + padded[3]) * 0.5
    )
    assert visible[0] >= frame[0] - 1e-6
    assert visible[1] >= frame[1] - 1e-6
    assert visible[2] <= frame[2] + 1e-6
    assert visible[3] <= frame[3] + 1e-6
    x_per_pixel, y_per_pixel = _view_box(pane).viewPixelSize()
    assert x_per_pixel == pytest.approx(y_per_pixel, rel=1e-6)


__all__ = [
    "_BlockingStopSnapWorker",
    "_RasterController",
    "_SnapWorker",
    "_assert_gds_focus",
    "_box",
    "_document",
    "_view_box",
    "distant_hidden_markup",
    "distant_route",
    "document",
    "pane",
    "qt_app",
    "window",
]
