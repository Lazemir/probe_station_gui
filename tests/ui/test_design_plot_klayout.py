from __future__ import annotations

from dataclasses import replace
import os
from pathlib import Path
import threading
import time

import pytest


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")
pytest.importorskip("pyqtgraph")

from PySide6.QtCore import QObject, QPoint, QPointF, Qt, Signal
from PySide6.QtGui import QAction, QWheelEvent
from PySide6.QtWidgets import QAbstractButton, QApplication

from probe_station_gui.design.klayout_types import (
    KLayoutConfig,
    RenderFailure,
    SnapFailure,
    SnapResponse,
)
from probe_station_gui.design.markup import GuideSegment, MarkupDocument
from probe_station_gui.design.model import DesignDocument, SnapResult
from probe_station_gui.design.navigation_bounds import (
    GDS_FOCUS_PADDING_FRACTION,
    pad_bounds,
)
from probe_station_gui.route.model import MeasurementRoute, NeedleOffset, RoutePoint
from probe_station_gui.views import design_plot_pane as plot_module
from probe_station_gui.views.design_layout_window import DesignLayoutWindow
from probe_station_gui.views.design_navigator_panel import DesignNavigatorPanel


@pytest.fixture(scope="module")
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


@pytest.fixture
def document(tmp_path: Path) -> DesignDocument:
    source = tmp_path / "layout.gds"
    source.write_bytes(b"gds")
    return _document(source)


@pytest.fixture
def window(monkeypatch, qt_app: QApplication):
    monkeypatch.setattr(plot_module, "KLayoutRasterController", _RasterController)
    monkeypatch.setattr(plot_module, "KLayoutSnapWorker", _SnapWorker)
    widget = DesignLayoutWindow()
    yield widget
    widget._main_view.shutdown()
    widget.deleteLater()


@pytest.fixture
def distant_hidden_markup(document: DesignDocument) -> MarkupDocument:
    return MarkupDocument.empty(document.path, visible=False).append_guide(
        (1_000_000.0, 0.0),
        (1_000_100.0, 100.0),
        guide_id="distant-guide",
    )


@pytest.fixture
def distant_route(document: DesignDocument) -> MeasurementRoute:
    route = MeasurementRoute.default_for_document(document)
    route.points.append(
        RoutePoint("far", "Far", (1_000_000.0, 0.0), enabled=False)
    )
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


def test_pending_document_preview_renders_without_destroying_tool_state(
    pane,
    tmp_path: Path,
) -> None:
    previous = _document(tmp_path / "previous.gds")
    candidate = _document(tmp_path / "candidate.gds")
    pane.set_document(previous)
    pane.set_active_design_tool("guide")
    pane._guide_anchor = (2.0, 3.0)
    pane.set_tool_sketch_points([(2.0, 3.0)])
    pane._plot.getViewBox().setRange(
        xRange=(10.0, 20.0),
        yRange=(5.0, 15.0),
        padding=0.0,
    )
    previous_range = pane._plot.getViewBox().viewRange()
    pane.set_status_message("Loading design...")
    assert pane._plot.isHidden()

    pane.set_document_preview(candidate)
    pane._plot.getViewBox().setRange(
        xRange=(30.0, 40.0),
        yRange=(20.0, 30.0),
        padding=0.0,
    )

    assert pane._document is candidate
    assert pane._document_preview_active
    assert not pane._plot.isHidden()
    assert pane.active_design_tool == "guide"
    assert pane._guide_anchor == (2.0, 3.0)
    assert pane._tool_sketch_points == [(2.0, 3.0)]
    assert not pane._tool_sketch_point_item.isVisible()

    pane.finish_document_preview(previous)

    assert pane._document is previous
    assert not pane._document_preview_active
    assert pane.active_design_tool == "guide"
    assert pane._guide_anchor == (2.0, 3.0)
    assert pane._tool_sketch_points == [(2.0, 3.0)]
    assert pane._tool_sketch_point_item.isVisible()
    restored_range = pane._plot.getViewBox().viewRange()
    assert restored_range[0] == pytest.approx(previous_range[0])
    assert restored_range[1] == pytest.approx(previous_range[1])


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


def test_file_backed_layer_toggle_preserves_view_and_cached_navigation(
    pane,
    document: DesignDocument,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_content_bounds = plot_module.content_bounds
    content_scans = 0

    def counted_content_bounds(*args, **kwargs):
        nonlocal content_scans
        content_scans += 1
        return real_content_bounds(*args, **kwargs)

    monkeypatch.setattr(plot_module, "content_bounds", counted_content_bounds)
    pane.set_document(document)
    _view_box(pane).setRange(
        xRange=(20.0, 40.0),
        yRange=(10.0, 20.0),
        padding=0.0,
    )
    before_view = _box(pane)
    before_content = pane._navigation_content_bounds
    before_frame = pane._navigation_frame
    scans_before_toggle = content_scans

    pane.set_document(document.with_visible_layers({(2, 0)}))

    assert _box(pane) == pytest.approx(before_view)
    assert pane._navigation_content_bounds is before_content
    assert pane._navigation_frame is before_frame
    assert content_scans == scans_before_toggle
    assert pane._raster_controller.documents[-1].visible_layers == frozenset(
        {(2, 0)}
    )
    assert pane._klayout_config.visible_layers == frozenset({(2, 0)})


def test_same_path_new_source_retires_snap_worker_while_same_source_reuses(
    pane,
    tmp_path: Path,
) -> None:
    first = replace(_document(tmp_path / "same.gds"), source_load_id="load-a")
    pane.set_document(first)
    worker = pane._snap_worker
    pane.set_document(first.with_visible_layers({(2, 0)}))
    assert pane._snap_worker is worker

    pane.set_document(replace(first, source_load_id="load-b"))

    assert pane._snap_worker is not worker
    assert worker.stop_calls == [0.0]
    assert worker in pane._retired_snap_workers


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


def test_preflight_skipped_click_waits_for_older_accepted_click(
    pane,
    tmp_path: Path,
) -> None:
    pane.set_document(_document(tmp_path / "ordered-clicks.gds"))
    pane._snap_distance_threshold = lambda: 1.0
    worker = pane._snap_worker
    moves: list[tuple[float, float]] = []
    pane.move_requested.connect(lambda x, y: moves.append((x, y)))

    pane._submit_file_backed_click("move", (10.0, 10.0))
    accepted = worker.click_requests[-1]
    pane._submit_file_backed_click("move", (1_000.0, 1_000.0))

    assert len(worker.click_requests) == 1
    assert moves == []

    worker.snap_ready.emit(
        SnapResponse(
            request_id=accepted.request_id,
            config_generation=accepted.config.generation,
            raw_point=accepted.point,
            result=SnapResult((11.0, 12.0), "vertex", 0.1),
            elapsed_ms=1.0,
            shapes_inspected=1,
            purpose="click",
        )
    )

    assert moves == [(11.0, 12.0), (1_000.0, 1_000.0)]


def test_preflight_skipped_click_invalidates_inflight_hover(
    pane,
    tmp_path: Path,
) -> None:
    pane.set_document(_document(tmp_path / "skipped-click-hover.gds"))
    pane._snap_distance_threshold = lambda: 1.0
    worker = pane._snap_worker
    pane._submit_file_backed_hover((10.0, 10.0))
    hover = worker.hover_requests[-1]

    pane._submit_file_backed_click("move", (1_000.0, 1_000.0))

    assert worker.click_requests == []
    assert worker.cancel_hover_calls == 1
    assert pane._pending_hover_markup == {}
    assert pane._hover_snap == SnapResult((1_000.0, 1_000.0), "free", 0.0)

    worker.snap_ready.emit(_hover_response(hover))

    assert pane._hover_snap == SnapResult((1_000.0, 1_000.0), "free", 0.0)


def test_failed_older_click_releases_preflight_skipped_click(
    pane,
    tmp_path: Path,
) -> None:
    pane.set_document(_document(tmp_path / "failed-ordered-click.gds"))
    pane._snap_distance_threshold = lambda: 1.0
    worker = pane._snap_worker
    moves: list[tuple[float, float]] = []
    pane.move_requested.connect(lambda x, y: moves.append((x, y)))
    pane._submit_file_backed_click("move", (10.0, 10.0))
    accepted = worker.click_requests[-1]
    pane._submit_file_backed_click("move", (1_000.0, 1_000.0))

    worker.failed.emit(
        SnapFailure(
            accepted.request_id,
            accepted.config.generation,
            "click",
            "failed",
        )
    )

    assert moves == [(1_000.0, 1_000.0)]
    assert pane._pending_clicks == {}
    assert list(pane._click_order) == []


def test_cancelled_ordered_click_ignores_late_failure(
    pane,
    tmp_path: Path,
) -> None:
    pane.set_document(_document(tmp_path / "cancelled-ordered-click.gds"))
    pane._snap_distance_threshold = lambda: 1.0
    worker = pane._snap_worker
    calibration: list[tuple[int, float, float]] = []
    moves: list[tuple[float, float]] = []
    pane.calibration_point_selected.connect(
        lambda slot, x, y: calibration.append((slot, x, y))
    )
    pane.move_requested.connect(lambda x, y: moves.append((x, y)))
    pane._submit_file_backed_click("calibration", (10.0, 10.0), (0,))
    earlier = worker.click_requests[-1]
    pane._submit_file_backed_click("move", (20.0, 20.0))
    cancelled = worker.click_requests[-1]

    pane.cancel_active_interaction()
    worker.failed.emit(
        SnapFailure(
            cancelled.request_id,
            cancelled.config.generation,
            "click",
            "late failure",
        )
    )

    assert not pane._snap_failure_visible
    worker.snap_ready.emit(
        SnapResponse(
            request_id=earlier.request_id,
            config_generation=earlier.config.generation,
            raw_point=earlier.point,
            result=SnapResult((11.0, 12.0), "vertex", 0.1),
            elapsed_ms=1.0,
            shapes_inspected=1,
            purpose="click",
        )
    )
    assert calibration == [(0, 11.0, 12.0)]
    assert moves == []
    assert pane._pending_clicks == {}


def test_file_backed_click_uses_nearer_correlated_markup_candidate(
    pane,
    tmp_path: Path,
) -> None:
    design_path = tmp_path / "markup-snap.gds"
    design_path.write_bytes(b"gds")
    pane.set_document(_document(design_path))
    pane._snap_distance_threshold = lambda: 1.0
    markup = MarkupDocument.empty(design_path).append_guide(
        (1.0, 1.0),
        (3.0, 1.0),
        guide_id="guide",
    )
    pane.set_markup(markup)
    points = []
    pane.point_requested.connect(lambda x, y: points.append((x, y)))

    pane._submit_file_backed_click("point", (1.1, 1.0))
    request = pane._snap_worker.click_requests[-1]
    pending = pane._pending_clicks[request.request_id]
    assert pending.markup_result is not None
    pane._snap_worker.snap_ready.emit(
        SnapResponse(
            request_id=request.request_id,
            config_generation=request.config.generation,
            raw_point=request.point,
            result=SnapResult((20.0, 20.0), "vertex", 0.1),
            elapsed_ms=1.0,
            shapes_inspected=1,
            purpose="click",
        )
    )

    assert points == [(1.0, 1.0)]


def test_file_backed_guide_click_keeps_originating_modifier_snapshot(
    pane,
    tmp_path: Path,
) -> None:
    pane.set_document(_document(tmp_path / "guide-constraint.gds"))
    pane.set_route_edit_enabled(True)
    pane.set_active_design_tool("guide")
    pane._accept_guide_point((0.0, 0.0))
    guides = []
    pane.guide_requested.connect(lambda start, end: guides.append((start, end)))

    pane._submit_file_backed_click(
        "guide_point",
        (4.0, 3.0),
        modifiers=Qt.ControlModifier,
    )
    request = pane._snap_worker.click_requests[-1]
    pending = pane._pending_clicks[request.request_id]

    assert (pending.shift_constraint, pending.control_constraint) == (False, True)
    pane._snap_worker.snap_ready.emit(
        SnapResponse(
            request_id=request.request_id,
            config_generation=request.config.generation,
            raw_point=request.point,
            result=SnapResult((4.0, 3.0), "vertex", 0.1),
            elapsed_ms=1.0,
            shapes_inspected=1,
            purpose="click",
        )
    )
    assert len(guides) == 1
    assert guides[0][0] == (0.0, 0.0)
    assert guides[0][1] == pytest.approx((3.5, 3.5))


def test_escape_discards_late_file_backed_tool_click(
    pane,
    tmp_path: Path,
) -> None:
    pane.set_document(_document(tmp_path / "cancelled-point.gds"))
    pane.set_route_edit_enabled(True)
    pane.set_active_design_tool("point")
    points = []
    pane.point_requested.connect(lambda x, y: points.append((x, y)))
    pane._submit_file_backed_click("point", (4.0, 3.0))
    request = pane._snap_worker.click_requests[-1]

    pane.cancel_active_interaction()
    pane._snap_worker.snap_ready.emit(
        SnapResponse(
            request_id=request.request_id,
            config_generation=request.config.generation,
            raw_point=request.point,
            result=SnapResult((4.0, 3.0), "vertex", 0.1),
            elapsed_ms=1.0,
            shapes_inspected=1,
            purpose="click",
        )
    )

    assert points == []


def test_file_backed_hover_keeps_modifiers_and_escape_discards_late_response(
    pane,
    tmp_path: Path,
) -> None:
    pane.set_document(_document(tmp_path / "hover-constraint.gds"))
    events = []
    pane.tool_hover_snap_changed.connect(
        lambda result, shift, control: events.append((result, shift, control))
    )
    pane._submit_file_backed_hover(
        (4.0, 3.0),
        modifiers=Qt.ControlModifier,
    )
    request = pane._snap_worker.hover_requests[-1]
    pane._snap_worker.snap_ready.emit(_hover_response(request))

    assert events[-1][1:] == (False, True)

    pane._submit_file_backed_hover(
        (8.0, 7.0),
        modifiers=Qt.ShiftModifier,
    )
    cancelled = pane._snap_worker.hover_requests[-1]
    events.clear()
    pane.cancel_active_interaction()
    pane._snap_worker.snap_ready.emit(_hover_response(cancelled))

    assert events == []


@pytest.mark.parametrize("change", ["hide", "delete", "snap_off"])
def test_pending_click_is_rejected_when_markup_or_snap_state_changes(
    pane,
    tmp_path: Path,
    change: str,
) -> None:
    design_path = tmp_path / f"pending-{change}.gds"
    design_path.write_bytes(b"gds")
    pane.set_document(_document(design_path))
    pane._snap_distance_threshold = lambda: 1.0
    markup = MarkupDocument.empty(design_path).append_guide(
        (1.0, 1.0),
        (3.0, 1.0),
        guide_id="guide",
    )
    pane.set_markup(markup)
    points = []
    pane.point_requested.connect(lambda x, y: points.append((x, y)))
    pane._submit_file_backed_click("point", (1.1, 1.0))
    request = pane._snap_worker.click_requests[-1]

    if change == "hide":
        pane.set_markup(markup.with_visibility(False))
    elif change == "delete":
        pane.set_markup(markup.remove_ids({"guide"}))
    else:
        pane.set_snap_enabled(False)
    pane._snap_worker.snap_ready.emit(
        SnapResponse(
            request_id=request.request_id,
            config_generation=request.config.generation,
            raw_point=request.point,
            result=SnapResult((20.0, 20.0), "vertex", 0.1),
            elapsed_ms=1.0,
            shapes_inspected=1,
            purpose="click",
        )
    )

    assert points == []


def test_failed_file_backed_click_does_not_execute_correlated_markup_candidate(
    pane,
    tmp_path: Path,
) -> None:
    design_path = tmp_path / "markup-failure.gds"
    design_path.write_bytes(b"gds")
    pane.set_document(_document(design_path))
    pane._snap_distance_threshold = lambda: 1.0
    pane.set_markup(
        MarkupDocument.empty(design_path).append_guide(
            (1.0, 1.0),
            (3.0, 1.0),
            guide_id="guide",
        )
    )
    points = []
    pane.point_requested.connect(lambda x, y: points.append((x, y)))
    pane._submit_file_backed_click("point", (1.1, 1.0))
    request = pane._snap_worker.click_requests[-1]

    pane._snap_worker.failed.emit(
        SnapFailure(
            request.request_id,
            request.config.generation,
            "click",
            "failed",
        )
    )

    assert points == []


def test_markup_edits_do_not_reconfigure_file_backed_workers(
    pane,
    tmp_path: Path,
) -> None:
    design_path = tmp_path / "markup-worker-stability.gds"
    design_path.write_bytes(b"gds")
    pane.set_document(_document(design_path))
    snap_worker = pane._snap_worker
    raster_controller = pane._raster_controller
    configured_documents = list(raster_controller.documents)

    pane.set_markup(
        MarkupDocument.empty(design_path).append_guide(
            (0.0, 0.0),
            (2.0, 2.0),
            guide_id="guide",
        )
    )

    assert pane._snap_worker is snap_worker
    assert pane._raster_controller is raster_controller
    assert raster_controller.documents == configured_documents


def test_matching_hover_failure_only_clears_matching_hover(pane, tmp_path: Path) -> None:
    pane.set_document(_document(tmp_path / "hover-failure.gds"))
    pane._submit_file_backed_hover((1.0, 2.0))
    first = pane._snap_worker.hover_requests[-1]
    pane._submit_file_backed_hover((3.0, 4.0))
    second = pane._snap_worker.hover_requests[-1]
    pane._set_hover_snap(SnapResult((9.0, 9.0), "vertex", 0.1))

    pane._snap_worker.failed.emit(
        SnapFailure(first.request_id, first.config.generation, "hover", "stale")
    )
    assert pane._hover_snap is not None
    pane._snap_worker.failed.emit(
        SnapFailure(second.request_id, second.config.generation, "hover", "current")
    )
    assert pane._hover_snap is None


@pytest.mark.parametrize(
    ("action", "payload", "signal_name"),
    [
        ("route_pick", ("array_origin",), "route_pick_requested"),
        ("route_point", (), "route_point_requested"),
        ("move", (), "move_requested"),
        ("calibration", (0,), "calibration_point_selected"),
    ],
)
def test_click_snap_failure_removes_only_matching_action_without_raw_fallback(
    pane,
    tmp_path: Path,
    action: str,
    payload: tuple[object, ...],
    signal_name: str,
) -> None:
    pane.set_document(_document(tmp_path / f"{action}.gds"))
    emitted: list[tuple[object, ...]] = []
    getattr(pane, signal_name).connect(lambda *args: emitted.append(args))
    pane._submit_file_backed_click(action, (7.0, 8.0), payload)
    matching = pane._snap_worker.click_requests[-1]
    pane._submit_file_backed_click("move", (70.0, 40.0))
    other = pane._snap_worker.click_requests[-1]

    pane._snap_worker.failed.emit(
        SnapFailure(
            matching.request_id,
            matching.config.generation,
            "click",
            "query failed",
        )
    )

    assert emitted == []
    assert matching.request_id not in pane._pending_clicks
    assert other.request_id in pane._pending_clicks
    assert pane._status_label.text() == "Snap failed. Try again."


def test_render_failure_state_clears_after_accepted_frame_signal(
    pane,
    tmp_path: Path,
) -> None:
    pane.set_document(_document(tmp_path / "render-status.gds"))
    config = pane._klayout_config
    pane._raster_controller.failed.emit(
        RenderFailure(1, config.generation, 1, "exact", "failed")
    )
    assert pane._status_label.text() == "Design rendering failed."

    pane._raster_controller.succeeded.emit()

    assert pane._plot.isHidden() is False


def _hover_response(request) -> SnapResponse:
    return SnapResponse(
        request_id=request.request_id,
        config_generation=request.config.generation,
        raw_point=request.point,
        result=SnapResult((10.0, 20.0), "vertex", 0.1),
        elapsed_ms=1.0,
        shapes_inspected=1,
        purpose="hover",
    )


def test_snap_off_invalidates_inflight_hover_response(
    pane, tmp_path: Path
) -> None:
    pane.set_document(_document(tmp_path / "layout.gds"))
    worker = pane._snap_worker
    changes = []
    pane.hover_snap_changed.connect(changes.append)
    pane._submit_file_backed_hover((1.0, 2.0))
    request = worker.hover_requests[-1]

    pane.set_snap_enabled(False)
    assert worker.cancel_pending_calls == 1
    changes.clear()
    worker.snap_ready.emit(_hover_response(request))

    assert pane._hover_snap is None
    assert changes == []
    assert len(pane._hover_item.getData()[0]) == 0


def test_extreme_hover_skips_worker_but_keeps_markup_snap(pane, tmp_path) -> None:
    source = tmp_path / "chip.gds"
    source.write_bytes(b"gds")
    pane.set_document(_document(source))
    pane.set_markup(
        MarkupDocument.empty(source).append_guide(
            (0.0, 0.0), (10.0, 0.0), guide_id="guide"
        )
    )
    pane._snap_distance_threshold = lambda: 10_000_000.0
    worker = pane._snap_worker

    pane._submit_file_backed_hover((5.0, 0.0))

    assert worker.hover_requests == []
    assert worker.cancel_hover_calls == 1
    assert pane._hover_snap.mode in {"guide_center", "guide_intersection"}


def test_extreme_move_click_executes_exact_cursor_once_without_worker(
    pane, tmp_path
) -> None:
    source = tmp_path / "move-chip.gds"
    source.write_bytes(b"gds")
    pane.set_document(_document(source))
    emitted: list[tuple[float, float]] = []
    pane.move_requested.connect(lambda x, y: emitted.append((x, y)))
    pane._snap_distance_threshold = lambda: 10_000_000.0

    pane._submit_file_backed_click("move", (25.0, 30.0))

    assert pane._snap_worker.click_requests == []
    assert emitted == [(25.0, 30.0)]


def test_cursor_leave_invalidates_inflight_hover_response(
    pane, tmp_path: Path
) -> None:
    pane.set_document(_document(tmp_path / "layout.gds"))
    worker = pane._snap_worker
    changes = []
    pane.hover_snap_changed.connect(changes.append)
    pane._submit_file_backed_hover((1.0, 2.0))
    request = worker.hover_requests[-1]

    pane._pending_hover_scene_pos = QPointF(-10_000.0, -10_000.0)
    pane._flush_hover_snap()
    changes.clear()
    worker.snap_ready.emit(_hover_response(request))

    assert pane._hover_snap is None
    assert changes == []
    assert len(pane._hover_item.getData()[0]) == 0


@pytest.mark.parametrize(
    ("action", "payload", "signal_name", "expected"),
    [
        (
            "route_pick",
            ("array_origin",),
            "route_pick_requested",
            ("array_origin", 7.0, 8.0, False, False),
        ),
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


def test_retired_snap_worker_finalizes_on_creator_thread(
    pane,
    qt_app: QApplication,
    tmp_path: Path,
) -> None:
    creator_thread = threading.get_ident()
    pane.set_document(
        replace(_document(tmp_path / "retire-a.gds"), source_load_id="a")
    )
    worker = pane._snap_worker
    delete_threads: list[int] = []
    worker.deleteLater = lambda: delete_threads.append(threading.get_ident())

    started = time.monotonic()
    pane.set_document(
        replace(_document(tmp_path / "retire-b.gds"), source_load_id="b")
    )
    assert time.monotonic() - started < 0.1
    assert worker in pane._retired_snap_workers

    worker.finished.emit()
    qt_app.processEvents()

    assert worker not in pane._retired_snap_workers
    assert delete_threads == [creator_thread]


@pytest.mark.parametrize("action", ["remove", "close"])
def test_remove_and_close_never_wait_for_snap_worker_and_retire_on_creator(
    monkeypatch: pytest.MonkeyPatch,
    qt_app: QApplication,
    tmp_path: Path,
    action: str,
) -> None:
    _BlockingStopSnapWorker.instances.clear()
    monkeypatch.setattr(plot_module, "KLayoutRasterController", _RasterController)
    monkeypatch.setattr(plot_module, "KLayoutSnapWorker", _BlockingStopSnapWorker)
    widget = plot_module._DesignPlotPane()
    widget.set_document(_document(tmp_path / f"{action}.gds"))
    worker = widget._snap_worker
    creator_thread = threading.get_ident()
    delete_threads: list[int] = []
    worker.deleteLater = lambda: delete_threads.append(threading.get_ident())

    started = time.monotonic()
    if action == "remove":
        widget.set_document(None)
    else:
        widget.close()
    elapsed = time.monotonic() - started

    assert elapsed < 0.1
    assert worker.stop_calls == [0.0]
    assert worker in widget._retired_snap_workers
    worker.finished.emit()
    qt_app.processEvents()
    assert worker not in widget._retired_snap_workers
    assert delete_threads == [creator_thread]
    widget.deleteLater()


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

    assert window._main_view._klayout_config is not None
    assert window._main_view._snap_worker is not first_snap_worker
    window._main_view.shutdown()
    window.deleteLater()


def test_design_window_closed_refresh_stays_detached_until_reopen(
    monkeypatch,
    qt_app: QApplication,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(plot_module, "KLayoutRasterController", _RasterController)
    monkeypatch.setattr(plot_module, "KLayoutSnapWorker", _SnapWorker)
    window = DesignLayoutWindow()
    window.set_document(_document(tmp_path / "first.gds"))
    window.show()
    qt_app.processEvents()
    window.close()
    qt_app.processEvents()
    set_document_calls: list[object] = []
    real_set_document = window._main_view.set_document

    def set_document(document: object) -> None:
        set_document_calls.append(document)
        real_set_document(document)

    window._main_view.set_document = set_document
    refreshed_document = _document(tmp_path / "refreshed.gds")

    window.set_document(refreshed_document)

    assert set_document_calls == []
    assert window._main_view._klayout_config is None

    window.show_and_raise()
    qt_app.processEvents()

    assert set_document_calls == [refreshed_document]
    assert window._main_view._klayout_config is not None
    window._main_view.shutdown()
    window.deleteLater()


def test_design_window_closed_preview_finish_clears_preview_before_reopen(
    monkeypatch,
    qt_app: QApplication,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(plot_module, "KLayoutRasterController", _RasterController)
    monkeypatch.setattr(plot_module, "KLayoutSnapWorker", _SnapWorker)
    window = DesignLayoutWindow()
    document = _document(tmp_path / "current.gds")
    window.set_document(document)
    window.show()
    qt_app.processEvents()
    window.set_document_preview(_document(tmp_path / "preview.gds"))
    assert window._main_view._document_preview_active
    window.close()
    qt_app.processEvents()
    set_document_calls: list[object] = []
    real_set_document = window._main_view.set_document

    def set_document(value: object) -> None:
        set_document_calls.append(value)
        real_set_document(value)

    window._main_view.set_document = set_document

    window.finish_document_preview(document)

    assert not window._main_view._document_preview_active
    assert set_document_calls == [None]
    assert window._main_view._klayout_config is None

    window.show_and_raise()
    qt_app.processEvents()

    assert set_document_calls == [None, document]
    assert window._main_view._klayout_config is not None
    next_preview = _document(tmp_path / "next-preview.gds")
    window.set_document_preview(next_preview)
    assert window._main_view._document_preview_active
    assert window._main_view._document_preview_previous_document is document
    window.finish_document_preview(document)
    assert not window._main_view._document_preview_active
    window._main_view.shutdown()
    window.deleteLater()


def test_design_window_hide_and_show_keeps_the_attached_document(
    monkeypatch,
    qt_app: QApplication,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(plot_module, "KLayoutRasterController", _RasterController)
    monkeypatch.setattr(plot_module, "KLayoutSnapWorker", _SnapWorker)
    window = DesignLayoutWindow()
    window.set_document(_document(tmp_path / "layout.gds"))
    window.show()
    qt_app.processEvents()
    set_document_calls: list[object] = []
    real_set_document = window._main_view.set_document

    def set_document(document: object) -> None:
        set_document_calls.append(document)
        real_set_document(document)

    window._main_view.set_document = set_document

    window.hide()
    qt_app.processEvents()
    window.show_and_raise()
    qt_app.processEvents()

    assert set_document_calls == []
    window._main_view.shutdown()
    window.deleteLater()


def test_design_window_disables_escape_during_pending_document_preview(
    monkeypatch,
    qt_app: QApplication,
) -> None:
    monkeypatch.setattr(plot_module, "KLayoutRasterController", _RasterController)
    monkeypatch.setattr(plot_module, "KLayoutSnapWorker", _SnapWorker)
    window = DesignLayoutWindow()

    window.set_design_load_pending(True)

    assert not window._escape_shortcut.isEnabled()
    assert not window.navigator_panel._delete_shortcut.isEnabled()

    window.set_design_load_pending(False)

    assert window._escape_shortcut.isEnabled()
    assert window.navigator_panel._delete_shortcut.isEnabled()
    window.deleteLater()


def test_open_frames_gds_but_internal_limits_include_distant_hidden_markup(
    pane, document, distant_hidden_markup
) -> None:
    pane.set_document(document)
    pane.set_markup(distant_hidden_markup)

    _assert_gds_focus(pane, document)
    assert pane._navigation_frame[2] > 1_000_000.0


def test_route_and_markup_updates_expand_limits_without_changing_view(
    pane, document, distant_route, distant_hidden_markup
) -> None:
    pane.set_document(document)
    before = _box(pane)

    pane.set_probe_route(distant_route, selected_route_point_index=-1)
    pane.set_markup(distant_hidden_markup)

    assert _box(pane) == pytest.approx(before)
    assert pane._navigation_frame[2] > 1_000_000.0


def test_deleting_outer_content_shrinks_and_clamps_once(
    pane, document, distant_route
) -> None:
    pane.set_document(document)
    pane.set_probe_route(distant_route, selected_route_point_index=-1)
    _view_box(pane).setRange(
        xRange=(999_900.0, 1_000_100.0),
        yRange=(-100.0, 100.0),
        padding=0.0,
    )

    pane.set_probe_route(None, selected_route_point_index=-1)

    assert pane._navigation_frame[2] < 1_000.0
    assert _box(pane)[2] <= pane._navigation_frame[2]


def test_shrink_preserves_an_already_valid_view(
    pane, document, distant_route
) -> None:
    pane.set_document(document)
    pane.set_probe_route(distant_route, selected_route_point_index=-1)
    _view_box(pane).setRange(
        xRange=(10.0, 40.0),
        yRange=(5.0, 20.0),
        padding=0.0,
    )
    before = _box(pane)

    pane.set_probe_route(None, selected_route_point_index=-1)

    assert _box(pane) == pytest.approx(before)


def test_home_restores_gds_without_changing_content_limits(
    window, document, distant_route
) -> None:
    window.set_document(document)
    window.set_probe_route(distant_route, selected_route_point_index=-1)
    frame = window._main_view._navigation_frame
    window._home_shortcut.activated.emit()

    assert window._main_view._navigation_frame == frame
    _assert_gds_focus(window._main_view, document)


def test_needle_offset_update_expands_then_shrinks_navigation_frame(
    pane, document
) -> None:
    route = MeasurementRoute.default_for_document(document)
    route.points.append(RoutePoint("local", "Local", (20.0, 20.0)))
    pane.set_document(document)
    pane.set_probe_route(route, selected_route_point_index=-1)
    original = pane._navigation_frame

    route.needle_offsets = [
        NeedleOffset("N1", "Needle 1", 2_000_000.0, 0.0)
    ]
    pane.set_probe_route(route, selected_route_point_index=-1)
    assert pane._navigation_frame[2] > 2_000_000.0

    route.needle_offsets = [NeedleOffset("N1", "Needle 1", 0.0, 0.0)]
    pane.set_probe_route(route, selected_route_point_index=-1)
    assert pane._navigation_frame == pytest.approx(original)


def test_rotated_document_ignores_stale_content_until_models_are_refreshed(
    pane, document, distant_route, distant_hidden_markup
) -> None:
    pane.set_document(document)
    pane.set_probe_route(distant_route, selected_route_point_index=-1)
    pane.set_markup(distant_hidden_markup)
    assert pane._navigation_frame[2] > 1_000_000.0

    rotated = replace(
        document,
        bounds=(0.0, 0.0, 50.0, 100.0),
        rotation_quarter_turns=1,
        source_load_id="rotated-load",
    )
    pane.set_document(rotated)
    assert pane._navigation_frame[2] < 1_000.0

    rotated_route = MeasurementRoute.default_for_document(rotated)
    rotated_route.points.append(RoutePoint("far", "Far", (0.0, 1_000_000.0)))
    rotated_markup = replace(
        distant_hidden_markup,
        guides=(
            GuideSegment(
                "rotated-guide",
                (0.0, 1_000_000.0),
                (-100.0, 1_000_100.0),
            ),
        ),
    )
    pane.set_probe_route(rotated_route, selected_route_point_index=-1)
    pane.set_markup(rotated_markup)
    assert pane._navigation_frame[3] > 1_000_000.0


def test_design_window_exposes_no_fit_all_control_or_action(window) -> None:
    button_texts = {
        button.text().replace("&", "")
        for button in window.findChildren(QAbstractButton)
    }
    action_texts = {
        action.text().replace("&", "")
        for action in window.findChildren(QAction)
    }
    assert all("fit all" not in text.casefold() for text in button_texts)
    assert all("fit all" not in text.casefold() for text in action_texts)


def test_file_backed_renderer_receives_gds_focused_range_on_first_request(
    pane, document
) -> None:
    pane.set_document(document)

    first_range = pane._raster_controller.ranges_seen_on_set_document[0]
    assert first_range == pytest.approx(_box(pane))
    assert abs(first_range[2] - first_range[0]) < 2.0 * (
        document.bounds[2] - document.bounds[0]
    )


@pytest.mark.parametrize(
    ("dx", "dy"),
    [
        (-1_000_000.0, 0.0),
        (1_000_000.0, 0.0),
        (0.0, -1_000_000.0),
        (0.0, 1_000_000.0),
    ],
)
def test_viewbox_cannot_pan_past_any_navigation_edge(
    pane, document, qt_app, dx, dy
) -> None:
    pane.set_document(document)
    frame = pane._navigation_frame
    view_box = _view_box(pane)
    view_box.setRange(
        xRange=(20.0, 80.0),
        yRange=(10.0, 40.0),
        padding=0.0,
    )
    view_box.translateBy(x=dx, y=dy)
    qt_app.processEvents()

    visible = _box(pane)
    assert visible[0] >= frame[0] - 1e-6
    assert visible[1] >= frame[1] - 1e-6
    assert visible[2] <= frame[2] + 1e-6
    assert visible[3] <= frame[3] + 1e-6


def test_wheel_zoom_out_stops_at_navigation_frame(pane, document, qt_app) -> None:
    pane.set_document(document)
    frame = pane._navigation_frame
    before = _box(pane)
    viewport = pane._plot.viewport()
    center = viewport.rect().center()
    wheel = QWheelEvent(
        QPointF(center),
        QPointF(viewport.mapToGlobal(center)),
        QPoint(),
        QPoint(0, -12_000),
        Qt.NoButton,
        Qt.NoModifier,
        Qt.ScrollUpdate,
        False,
    )

    QApplication.sendEvent(viewport, wheel)
    qt_app.processEvents()

    visible = _box(pane)
    assert visible[2] - visible[0] > before[2] - before[0]
    assert visible[0] >= frame[0] - 1e-6
    assert visible[1] >= frame[1] - 1e-6
    assert visible[2] <= frame[2] + 1e-6
    assert visible[3] <= frame[3] + 1e-6


def test_resize_recomputes_aspect_frame_without_refocusing(
    pane, document, distant_route, qt_app
) -> None:
    pane.resize(800, 600)
    pane.show()
    qt_app.processEvents()
    pane.set_document(document)
    pane.set_probe_route(distant_route, selected_route_point_index=-1)
    _view_box(pane).setRange(
        xRange=(1_000.0, 1_100.0),
        yRange=(-25.0, 25.0),
        padding=0.0,
    )
    before = _box(pane)
    before_center = ((before[0] + before[2]) * 0.5, (before[1] + before[3]) * 0.5)

    pane.resize(900, 300)
    qt_app.processEvents()

    frame = pane._navigation_frame
    after = _box(pane)
    after_center = ((after[0] + after[2]) * 0.5, (after[1] + after[3]) * 0.5)
    viewport_width, viewport_height = pane._viewport_size()
    frame_aspect = (frame[2] - frame[0]) / (frame[3] - frame[1])
    assert frame_aspect == pytest.approx(
        viewport_width / viewport_height,
        rel=0.05,
    )
    assert after_center == pytest.approx(before_center)


def test_resize_refits_cached_content_without_rescanning_models(
    pane, document, distant_route, qt_app, monkeypatch
) -> None:
    real_content_bounds = plot_module.content_bounds
    calls = 0

    def counted_content_bounds(*args, **kwargs):
        nonlocal calls
        calls += 1
        return real_content_bounds(*args, **kwargs)

    monkeypatch.setattr(plot_module, "content_bounds", counted_content_bounds)
    pane.set_document(document)
    pane.set_probe_route(distant_route, selected_route_point_index=-1)
    scans_before_resize = calls

    pane.resize(900, 300)
    qt_app.processEvents()

    assert calls == scans_before_resize
