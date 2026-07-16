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

from PySide6.QtCore import QObject, QPointF, Signal
from PySide6.QtWidgets import QApplication

from probe_station_gui.design.klayout_types import (
    KLayoutConfig,
    RenderFailure,
    SnapFailure,
    SnapResponse,
)
from probe_station_gui.design.markup import MarkupDocument
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
    failed = Signal(object)
    succeeded = Signal()

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
        self.instances.append(self)

    def submit_hover(self, request) -> None:
        self.hover_requests.append(request)

    def submit_click(self, request) -> None:
        self.click_requests.append(request)

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


def test_file_backed_click_uses_nearer_correlated_markup_candidate(
    pane,
    tmp_path: Path,
) -> None:
    design_path = tmp_path / "markup-snap.gds"
    design_path.write_bytes(b"gds")
    pane.set_document(_document(design_path))
    pane._snap_distance_threshold = lambda: 100.0
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


@pytest.mark.parametrize("change", ["hide", "delete", "snap_off"])
def test_pending_click_is_rejected_when_markup_or_snap_state_changes(
    pane,
    tmp_path: Path,
    change: str,
) -> None:
    design_path = tmp_path / f"pending-{change}.gds"
    design_path.write_bytes(b"gds")
    pane.set_document(_document(design_path))
    pane._snap_distance_threshold = lambda: 100.0
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
    pane._snap_distance_threshold = lambda: 100.0
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
    pane._submit_file_backed_click("move", (70.0, 80.0))
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
    changes.clear()
    worker.snap_ready.emit(_hover_response(request))

    assert pane._hover_snap is None
    assert changes == []
    assert len(pane._hover_item.getData()[0]) == 0


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

    assert window._main_view._document is document
    assert window._main_view._klayout_config is not None
    assert window._main_view._snap_worker is not first_snap_worker
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
