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

from PySide6.QtCore import QPointF
from PySide6.QtWidgets import QApplication

from probe_station_gui.design.klayout_types import (
    RenderFailure,
    SnapResponse,
)
from probe_station_gui.design.markup import MarkupDocument
from probe_station_gui.design.model import DesignDocument, SnapResult
from probe_station_gui.design.plot_interaction import PlotAction, SnapClickIntent
from probe_station_gui.design.snap_protocol import ClickPublication
from probe_station_gui.views import design_plot_pane as plot_module
from probe_station_gui.views import design_plot_viewport as viewport_module
from probe_station_gui.views import design_snap_runtime as snap_runtime_module
from probe_station_gui.views.design_layout_window import DesignLayoutWindow
from probe_station_gui.views.design_navigator_panel import DesignNavigatorPanel


from tests.ui.design_plot_klayout_support import (
    _BlockingStopSnapWorker,
    _RasterController,
    _SnapWorker,
    _box,
    _document,
    _view_box,
)


pytest_plugins = ("tests.ui.design_plot_klayout_fixtures",)


def _submit_file_click(
    pane,
    action: str,
    point: tuple[float, float],
    payload: tuple[object, ...] = (),
) -> None:
    pane._dispatch_snap_click(
        SnapClickIntent(
            action=PlotAction(action),
            raw_point=point,
            payload=payload,
            shift=False,
            control=False,
            generation=pane._plot_interaction.generation,
        )
    )


def _complete_local_click(
    pane,
    action: PlotAction,
    result: SnapResult,
) -> None:
    intent = SnapClickIntent(
        action=action,
        raw_point=result.point,
        payload=(),
        shift=False,
        control=False,
        generation=pane._plot_interaction.generation,
    )
    pane._apply_click_publication(ClickPublication(intent, result))


def test_click_publication_rechecks_transient_action_after_hover_callback(
    pane,
    tmp_path: Path,
) -> None:
    pane.set_document(_document(tmp_path / "hover-reentry.gds"))
    pane.set_navigation_enabled(True)
    pane.set_active_design_tool("move")
    moves: list[tuple[float, float]] = []
    pane.move_requested.connect(lambda x, y: moves.append((x, y)))

    def unload_after_hover(_result) -> None:
        pane.hover_snap_changed.disconnect(unload_after_hover)
        pane.set_document(None)

    pane.hover_snap_changed.connect(unload_after_hover)
    intent = SnapClickIntent(
        action=PlotAction.MOVE,
        raw_point=(2.0, 3.0),
        payload=(),
        shift=False,
        control=False,
        generation=pane._plot_interaction.generation,
    )

    pane._apply_click_publication(
        ClickPublication(intent, SnapResult((2.0, 3.0), "free", 0.0))
    )

    assert moves == []


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
    assert pane._renderer.state.layer_item_count == 0


def test_pending_document_preview_keeps_tool_but_clears_transient_guide(
    pane,
    tmp_path: Path,
) -> None:
    previous = _document(tmp_path / "previous.gds")
    candidate = _document(tmp_path / "candidate.gds")
    pane.set_document(previous)
    pane.set_route_edit_enabled(True)
    pane.set_active_design_tool("guide")
    _complete_local_click(
        pane,
        PlotAction.GUIDE_POINT,
        SnapResult((2.0, 3.0), "free", 0.0),
    )
    assert pane.guide_anchor == (2.0, 3.0)
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

    assert pane._presentation.document is candidate
    assert pane._presentation.preview_active
    assert not pane._plot.isHidden()
    assert pane.active_design_tool == "guide"
    assert pane.guide_anchor is None
    assert pane._presentation.plan.tool_sketch_points == ()
    assert pane._renderer.state.overlays_visible is False

    pane.finish_document_preview(previous)

    assert pane._presentation.document is previous
    assert not pane._presentation.preview_active
    assert pane.active_design_tool == "guide"
    assert pane.guide_anchor is None
    assert pane._presentation.plan.tool_sketch_points == ()
    assert pane._renderer.state.overlays_visible is True
    restored_range = pane._plot.getViewBox().viewRange()
    assert restored_range[0] == pytest.approx(previous_range[0])
    assert restored_range[1] == pytest.approx(previous_range[1])


def test_file_configuration_changes_reuse_snap_worker_and_generation(
    pane, tmp_path: Path
) -> None:
    document = _document(tmp_path / "layout.gds")
    pane.set_document(document)
    worker = pane._snap_runtime.active_worker
    first_generation = pane._snap_coordinator.config.generation

    pane.set_document(document.with_visible_layers({(2, 0)}))

    assert pane._snap_runtime.active_worker is worker
    assert pane._snap_coordinator.config.generation > first_generation

    pane.set_document(_document(tmp_path / "other.gds"))
    assert worker.stop_calls == [0.0]
    assert pane._snap_runtime.active_worker is not worker


def test_file_backed_layer_toggle_preserves_view_and_cached_navigation(
    pane,
    document: DesignDocument,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_content_bounds = viewport_module.content_bounds
    content_scans = 0

    def counted_content_bounds(*args, **kwargs):
        nonlocal content_scans
        content_scans += 1
        return real_content_bounds(*args, **kwargs)

    monkeypatch.setattr(viewport_module, "content_bounds", counted_content_bounds)
    pane.set_document(document)
    _view_box(pane).setRange(
        xRange=(20.0, 40.0),
        yRange=(10.0, 20.0),
        padding=0.0,
    )
    before_view = _box(pane)
    before_content = pane._viewport.content_bounds
    before_frame = pane._viewport.frame
    scans_before_toggle = content_scans

    pane.set_document(document.with_visible_layers({(2, 0)}))

    assert _box(pane) == pytest.approx(before_view)
    assert pane._viewport.content_bounds is before_content
    assert pane._viewport.frame is before_frame
    assert content_scans == scans_before_toggle
    assert pane._raster_controller.documents[-1].visible_layers == frozenset({(2, 0)})
    assert pane._snap_coordinator.config.visible_layers == frozenset({(2, 0)})


def test_same_path_new_source_retires_snap_worker_while_same_source_reuses(
    pane,
    tmp_path: Path,
) -> None:
    first = replace(_document(tmp_path / "same.gds"), source_load_id="load-a")
    pane.set_document(first)
    worker = pane._snap_runtime.active_worker
    pane.set_document(first.with_visible_layers({(2, 0)}))
    assert pane._snap_runtime.active_worker is worker

    pane.set_document(replace(first, source_load_id="load-b"))

    assert pane._snap_runtime.active_worker is not worker
    assert worker.stop_calls == [0.0]
    assert pane._snap_runtime.retired_worker_count == 1


def test_runtime_worker_response_reaches_pane_once(
    pane,
    tmp_path: Path,
) -> None:
    pane.set_document(_document(tmp_path / "runtime-terminal.gds"))
    pane.set_navigation_enabled(True)
    pane.set_active_design_tool("move")
    worker = pane._snap_runtime.active_worker
    moves: list[tuple[float, float]] = []
    pane.move_requested.connect(lambda x, y: moves.append((x, y)))

    _submit_file_click(pane, "move", (5.0, 6.0))
    request = worker.click_requests[-1]
    response = SnapResponse(
        request_id=request.request_id,
        config_generation=request.config.generation,
        raw_point=request.point,
        result=SnapResult((50.0, 60.0), "vertex", 0.1),
        elapsed_ms=1.0,
        shapes_inspected=1,
        purpose="click",
    )

    worker.snap_ready.emit(response)
    worker.snap_ready.emit(response)

    assert moves == [(50.0, 60.0)]


def test_tool_switch_discards_late_transient_worker_click(
    pane,
    tmp_path: Path,
) -> None:
    pane.set_document(_document(tmp_path / "tool-switch.gds"))
    pane.set_route_edit_enabled(True)
    pane.set_active_design_tool("point")
    worker = pane._snap_runtime.active_worker
    points: list[tuple[float, float]] = []
    pane.point_requested.connect(lambda x, y: points.append((x, y)))

    _submit_file_click(pane, "point", (4.0, 3.0))
    request = worker.click_requests[-1]
    pane.set_active_design_tool("select")
    worker.snap_ready.emit(
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


def test_markup_edits_do_not_reconfigure_file_backed_workers(
    pane,
    tmp_path: Path,
) -> None:
    design_path = tmp_path / "markup-worker-stability.gds"
    design_path.write_bytes(b"gds")
    pane.set_document(_document(design_path))
    snap_worker = pane._snap_runtime.active_worker
    raster_controller = pane._raster_controller
    configured_documents = list(raster_controller.documents)

    pane.set_markup(
        MarkupDocument.empty(design_path).append_guide(
            (0.0, 0.0),
            (2.0, 2.0),
            guide_id="guide",
        )
    )

    assert pane._snap_runtime.active_worker is snap_worker
    assert pane._raster_controller is raster_controller
    assert raster_controller.documents == configured_documents


def test_markup_snap_publication_uses_actual_euclidean_distance(
    pane,
    tmp_path: Path,
) -> None:
    source = tmp_path / "markup-distance.gds"
    source.write_bytes(b"gds")
    pane.set_document(_document(source))
    pane.set_markup(
        MarkupDocument.empty(source).append_guide(
            (0.0, 0.0),
            (100.0, 0.0),
            guide_id="distance",
        )
    )
    pane._viewport.snap_distance = lambda _radius_px: 10.0

    result = pane._resolve_snap_result((3.0, 4.0))

    assert result == SnapResult(
        (0.0, 0.0),
        "guide_end",
        5.0,
        segment_start=(0.0, 0.0),
        segment_end=(100.0, 0.0),
    )


def test_worker_lifecycle_failure_is_visible_to_the_user(
    pane,
    tmp_path: Path,
) -> None:
    pane.set_document(_document(tmp_path / "lifecycle-failure.gds"))
    worker = pane._snap_runtime.active_worker

    worker.lifecycle_failed.emit("backend died")

    assert pane._status_label.text() == "Snap failed. Try again."
    assert pane._snap_failure_visible


def test_render_failure_state_clears_after_accepted_frame_signal(
    pane,
    tmp_path: Path,
) -> None:
    pane.set_document(_document(tmp_path / "render-status.gds"))
    config = pane._snap_coordinator.config
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


def _click_response(
    request,
    point: tuple[float, float],
) -> SnapResponse:
    return SnapResponse(
        request_id=request.request_id,
        config_generation=request.config.generation,
        raw_point=request.point,
        result=SnapResult(point, "vertex", 0.1),
        elapsed_ms=1.0,
        shapes_inspected=1,
        purpose="click",
    )


@pytest.mark.parametrize("invalidation", ["document", "markup", "snap"])
def test_ready_click_tail_is_revalidated_after_synchronous_head_effect(
    pane,
    tmp_path: Path,
    invalidation: str,
) -> None:
    source = tmp_path / "fifo-head.gds"
    source.write_bytes(b"gds")
    pane.set_document(_document(source))
    worker = pane._snap_runtime.active_worker
    head_events: list[tuple[float, float]] = []
    tail_events: list[tuple[int, float, float]] = []

    def invalidate_after_head(x_value: float, y_value: float) -> None:
        head_events.append((x_value, y_value))
        if invalidation == "document":
            pane.set_document(_document(tmp_path / "fifo-replacement.gds"))
        elif invalidation == "markup":
            pane.set_markup(
                MarkupDocument.empty(source).append_guide(
                    (0.0, 0.0),
                    (1.0, 1.0),
                    guide_id="replacement",
                )
            )
        else:
            pane.set_snap_enabled(False)

    pane.route_point_requested.connect(invalidate_after_head)
    pane.calibration_point_selected.connect(
        lambda index, x_value, y_value: tail_events.append(
            (index, x_value, y_value)
        )
    )
    _submit_file_click(pane, "route_point", (1.0, 2.0))
    _submit_file_click(pane, "calibration", (3.0, 4.0), (0,))
    head_request, tail_request = worker.click_requests[-2:]

    worker.snap_ready.emit(_click_response(tail_request, (30.0, 40.0)))
    worker.snap_ready.emit(_click_response(head_request, (10.0, 20.0)))

    assert head_events == [(10.0, 20.0)]
    assert tail_events == []


def test_snap_off_invalidates_inflight_hover_response(pane, tmp_path: Path) -> None:
    pane.set_document(_document(tmp_path / "layout.gds"))
    worker = pane._snap_runtime.active_worker
    changes = []
    pane.hover_snap_changed.connect(changes.append)
    pane._submit_file_backed_hover((1.0, 2.0))
    request = worker.hover_requests[-1]

    pane.set_snap_enabled(False)
    assert worker.cancel_pending_calls == 1
    changes.clear()
    worker.snap_ready.emit(_hover_response(request))

    assert pane._presentation.hover_snap is None
    assert changes == []
    assert pane._renderer.state.hover_segment == ()


def test_extreme_hover_skips_worker_but_keeps_markup_snap(pane, tmp_path) -> None:
    source = tmp_path / "chip.gds"
    source.write_bytes(b"gds")
    pane.set_document(_document(source))
    pane.set_markup(
        MarkupDocument.empty(source).append_guide(
            (0.0, 0.0), (10.0, 0.0), guide_id="guide"
        )
    )
    pane._viewport.snap_distance = lambda _radius_px: 10_000_000.0
    worker = pane._snap_runtime.active_worker

    pane._submit_file_backed_hover((5.0, 0.0))

    assert worker.hover_requests == []
    assert worker.cancel_hover_calls == 1
    assert pane._presentation.hover_snap.mode in {
        "guide_center",
        "guide_intersection",
    }


def test_extreme_move_click_executes_exact_cursor_once_without_worker(
    pane, tmp_path
) -> None:
    source = tmp_path / "move-chip.gds"
    source.write_bytes(b"gds")
    pane.set_document(_document(source))
    emitted: list[tuple[float, float]] = []
    pane.move_requested.connect(lambda x, y: emitted.append((x, y)))
    pane._viewport.snap_distance = lambda _radius_px: 10_000_000.0

    _submit_file_click(pane, "move", (25.0, 30.0))

    assert pane._snap_runtime.active_worker.click_requests == []
    assert emitted == [(25.0, 30.0)]


def test_cursor_leave_invalidates_inflight_hover_response(pane, tmp_path: Path) -> None:
    pane.set_document(_document(tmp_path / "layout.gds"))
    worker = pane._snap_runtime.active_worker
    changes = []
    pane.hover_snap_changed.connect(changes.append)
    pane._submit_file_backed_hover((1.0, 2.0))
    request = worker.hover_requests[-1]

    pane._pending_hover_scene_pos = QPointF(-10_000.0, -10_000.0)
    pane._flush_hover_snap()
    changes.clear()
    worker.snap_ready.emit(_hover_response(request))

    assert pane._presentation.hover_snap is None
    assert changes == []
    assert pane._renderer.state.hover_segment == ()


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

    _submit_file_click(pane, action, (7.0, 8.0), payload)

    assert results == [expected]
    assert pane._snap_runtime.active_worker.click_requests == []


@pytest.mark.parametrize("mode", ["segment", "segment_center"])
def test_hover_highlights_full_segment_for_line_and_center(pane, mode: str) -> None:
    pane._renderer.set_hover(
        SnapResult(
            point=(5.0, 0.0),
            mode=mode,
            distance=0.1,
            segment_start=(0.0, 0.0),
            segment_end=(10.0, 0.0),
        )
    )

    assert pane._renderer.state.hover_segment == (
        (0.0, 0.0),
        (10.0, 0.0),
    )


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
    snap_worker = pane._snap_runtime.active_worker
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
    pane.set_document(replace(_document(tmp_path / "retire-a.gds"), source_load_id="a"))
    worker = pane._snap_runtime.active_worker
    delete_threads: list[int] = []
    worker.deleteLater = lambda: delete_threads.append(threading.get_ident())

    started = time.monotonic()
    pane.set_document(replace(_document(tmp_path / "retire-b.gds"), source_load_id="b"))
    assert time.monotonic() - started < 0.1
    assert pane._snap_runtime.retired_worker_count == 1

    worker.finished.emit()
    qt_app.processEvents()

    assert pane._snap_runtime.retired_worker_count == 0
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
    monkeypatch.setattr(
        snap_runtime_module, "KLayoutSnapWorker", _BlockingStopSnapWorker
    )
    widget = plot_module._DesignPlotPane()
    widget.set_document(_document(tmp_path / f"{action}.gds"))
    worker = widget._snap_runtime.active_worker
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
    assert widget._snap_runtime.retired_worker_count == 1
    worker.finished.emit()
    qt_app.processEvents()
    assert widget._snap_runtime.retired_worker_count == 0
    assert delete_threads == [creator_thread]
    widget.deleteLater()


def test_design_window_close_detaches_workers_and_reopen_restores_document(
    monkeypatch, qt_app: QApplication, tmp_path: Path
) -> None:
    _SnapWorker.instances.clear()
    monkeypatch.setattr(plot_module, "KLayoutRasterController", _RasterController)
    monkeypatch.setattr(snap_runtime_module, "KLayoutSnapWorker", _SnapWorker)
    window = DesignLayoutWindow()
    document = _document(tmp_path / "layout.gds")
    window.set_document(document)
    first_snap_worker = window._main_view._snap_runtime.active_worker
    window.show()
    qt_app.processEvents()

    window.close()
    qt_app.processEvents()

    assert first_snap_worker.stop_calls
    assert window._main_view._snap_coordinator.config is None

    window.show_and_raise()
    qt_app.processEvents()

    assert window._main_view._snap_coordinator.config is not None
    assert window._main_view._snap_runtime.active_worker is not first_snap_worker
    window._main_view.shutdown()
    window.deleteLater()


def test_design_window_closed_refresh_stays_detached_until_reopen(
    monkeypatch,
    qt_app: QApplication,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(plot_module, "KLayoutRasterController", _RasterController)
    monkeypatch.setattr(snap_runtime_module, "KLayoutSnapWorker", _SnapWorker)
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
    assert window._main_view._snap_coordinator.config is None

    window.show_and_raise()
    qt_app.processEvents()

    assert set_document_calls == [refreshed_document]
    assert window._main_view._snap_coordinator.config is not None
    window._main_view.shutdown()
    window.deleteLater()


def test_design_window_closed_preview_finish_clears_preview_before_reopen(
    monkeypatch,
    qt_app: QApplication,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(plot_module, "KLayoutRasterController", _RasterController)
    monkeypatch.setattr(snap_runtime_module, "KLayoutSnapWorker", _SnapWorker)
    window = DesignLayoutWindow()
    document = _document(tmp_path / "current.gds")
    window.set_document(document)
    window.show()
    qt_app.processEvents()
    window.set_document_preview(_document(tmp_path / "preview.gds"))
    assert window._main_view._presentation.preview_active
    window.close()
    qt_app.processEvents()
    set_document_calls: list[object] = []
    real_set_document = window._main_view.set_document

    def set_document(value: object) -> None:
        set_document_calls.append(value)
        real_set_document(value)

    window._main_view.set_document = set_document

    window.finish_document_preview(document)

    assert not window._main_view._presentation.preview_active
    assert set_document_calls == [None]
    assert window._main_view._snap_coordinator.config is None

    window.show_and_raise()
    qt_app.processEvents()

    assert set_document_calls == [None, document]
    assert window._main_view._snap_coordinator.config is not None
    next_preview = _document(tmp_path / "next-preview.gds")
    window.set_document_preview(next_preview)
    assert window._main_view._presentation.preview_active
    assert window._main_view._presentation.preview_previous_document is document
    window.finish_document_preview(document)
    assert not window._main_view._presentation.preview_active
    window._main_view.shutdown()
    window.deleteLater()


def test_design_window_hide_and_show_keeps_the_attached_document(
    monkeypatch,
    qt_app: QApplication,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(plot_module, "KLayoutRasterController", _RasterController)
    monkeypatch.setattr(snap_runtime_module, "KLayoutSnapWorker", _SnapWorker)
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
    monkeypatch.setattr(snap_runtime_module, "KLayoutSnapWorker", _SnapWorker)
    window = DesignLayoutWindow()

    window.set_design_load_pending(True)

    assert not window._escape_shortcut.isEnabled()
    assert not window.navigator_panel.tool_controls._delete_shortcut.isEnabled()

    window.set_design_load_pending(False)

    assert window._escape_shortcut.isEnabled()
    assert window.navigator_panel.tool_controls._delete_shortcut.isEnabled()
    window.deleteLater()
