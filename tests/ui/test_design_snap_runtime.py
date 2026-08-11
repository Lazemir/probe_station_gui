from __future__ import annotations

import inspect
import os

import pytest


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication

from probe_station_gui.design.klayout_types import KLayoutConfig, SnapFailure
from probe_station_gui.design.snap_coordinator import (
    AttachWorker,
    CancelHover,
    CancelPending,
    DetachWorker,
    ReplaceWorker,
    SubmitClick,
    WorkerFailureEvent,
    WorkerLifecycleFailedEvent,
)
from probe_station_gui.design.plot_interaction import PlotAction, SnapClickIntent
from probe_station_gui.design.snap_coordinator import SnapCoordinator
from probe_station_gui.views import design_snap_runtime as runtime_module
from probe_station_gui.views.design_snap_runtime import DesignSnapRuntime


class _Worker(QObject):
    snap_ready = Signal(object)
    failed = Signal(object)
    lifecycle_failed = Signal(str)
    finished = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.stop_calls: list[float] = []
        self.click_requests: list[object] = []
        self.hover_requests: list[object] = []
        self.cancel_hover_calls = 0
        self.cancel_pending_calls = 0
        self.deleted = 0

    def submit_click(self, request: object) -> None:
        self.click_requests.append(request)

    def submit_hover(self, request: object) -> None:
        self.hover_requests.append(request)

    def cancel_hover(self) -> None:
        self.cancel_hover_calls += 1

    def cancel_pending(self) -> None:
        self.cancel_pending_calls += 1

    def stop(self, timeout_s: float = 1.0) -> None:
        self.stop_calls.append(timeout_s)

    def deleteLater(self) -> None:  # noqa: N802 - Qt interface
        self.deleted += 1


def test_retirement_callback_does_not_retain_runtime_or_pane() -> None:
    QApplication.instance() or QApplication([])
    worker = _Worker()
    runtime = DesignSnapRuntime(worker_factory=lambda: worker)
    runtime.apply((AttachWorker(1, ("layout.gds", "load-1")),))
    runtime.apply((DetachWorker(1, 0.0),))
    callback = runtime_module._RETIREMENT_CALLBACKS[worker]
    captured = inspect.getclosurevars(callback)

    assert runtime not in captured.nonlocals.values()
    assert runtime not in captured.globals.values()
    runtime.close()


def test_runtime_reuses_replaces_and_retires_workers_without_blocking() -> None:
    app = QApplication.instance() or QApplication([])
    workers: list[_Worker] = []

    def factory() -> _Worker:
        worker = _Worker()
        workers.append(worker)
        return worker

    runtime = DesignSnapRuntime(worker_factory=factory)
    runtime.apply((AttachWorker(1, ("a.gds", "load-a")),))
    first = runtime.active_worker
    runtime.apply((ReplaceWorker(2, ("b.gds", "load-b"), 0.0),))

    assert len(workers) == 2
    assert runtime.active_worker is workers[1]
    assert first.stop_calls == [0.0]
    assert runtime.retired_worker_count == 1
    first.finished.emit()
    app.processEvents()
    assert first.deleted == 1
    assert runtime.retired_worker_count == 0
    runtime.close()


def test_runtime_routes_commands_and_surfaces_failure_and_lifecycle_events() -> None:
    worker = _Worker()
    runtime = DesignSnapRuntime(worker_factory=lambda: worker)
    coordinator = SnapCoordinator()
    config = KLayoutConfig(
        path="layout.gds",
        top_cell_name="TOP",
        visible_layers=frozenset({(1, 0)}),
        source_bounds=(0.0, 0.0, 100.0, 50.0),
        display_bounds=(0.0, 0.0, 100.0, 50.0),
        rotation_quarter_turns=0,
        generation=1,
        source_load_id="load-1",
    )
    runtime.apply(coordinator.configure(config).commands)
    submitted = coordinator.submit_click(
        SnapClickIntent(PlotAction.MOVE, (1.0, 2.0), (), False, False, 1),
        radius=2.0,
    )
    runtime.apply(submitted.commands)
    received: list[object] = []
    runtime.event_ready.connect(received.append)
    request = next(c.request for c in submitted.commands if isinstance(c, SubmitClick))

    runtime.apply((CancelHover(1), CancelPending(1)))
    worker.failed.emit(SnapFailure(request.request_id, 1, "click", "failed"))
    worker.lifecycle_failed.emit("backend failed")

    assert worker.click_requests == [request]
    assert worker.cancel_hover_calls == 1
    assert worker.cancel_pending_calls == 1
    assert isinstance(received[0], WorkerFailureEvent)
    assert received[0].worker_token == 1
    assert received[1] == WorkerLifecycleFailedEvent(1, "backend failed")
    runtime.close()


def test_late_retired_worker_terminals_do_not_reach_runtime_sink() -> None:
    app = QApplication.instance() or QApplication([])
    workers = [_Worker(), _Worker()]
    runtime = DesignSnapRuntime(worker_factory=lambda: workers.pop(0))
    received: list[object] = []
    runtime.event_ready.connect(received.append)
    runtime.apply((AttachWorker(1, ("a.gds", "a")),))
    old = runtime.active_worker
    runtime.apply((ReplaceWorker(2, ("b.gds", "b"), 0.0),))

    old.lifecycle_failed.emit("late")
    old.finished.emit()
    app.processEvents()

    assert received == []
    runtime.close()


def test_fatal_active_worker_is_retired_and_next_request_attaches_fresh_worker() -> (
    None
):
    app = QApplication.instance() or QApplication([])
    workers: list[_Worker] = []

    def factory() -> _Worker:
        worker = _Worker()
        workers.append(worker)
        return worker

    runtime = DesignSnapRuntime(worker_factory=factory)
    coordinator = SnapCoordinator()
    config = KLayoutConfig(
        path="layout.gds",
        top_cell_name="TOP",
        visible_layers=frozenset({(1, 0)}),
        source_bounds=(0.0, 0.0, 100.0, 50.0),
        display_bounds=(0.0, 0.0, 100.0, 50.0),
        rotation_quarter_turns=0,
        generation=1,
        source_load_id="load-1",
    )
    runtime.apply(coordinator.configure(config).commands)
    first = runtime.active_worker
    pending = coordinator.submit_click(
        SnapClickIntent(
            PlotAction.ROUTE_POINT,
            (1.0, 2.0),
            (),
            False,
            False,
            1,
        ),
        radius=2.0,
    )
    runtime.apply(pending.commands)

    runtime.event_ready.connect(
        lambda event: runtime.apply(coordinator.worker_event(event).commands)
    )
    first.lifecycle_failed.emit("backend failed")

    assert runtime.active_worker is None
    assert runtime.active_worker_token == 0
    assert first.stop_calls == [0.0]
    assert runtime.retired_worker_count == 1
    first.finished.emit()
    app.processEvents()
    assert first.deleted == 1
    assert runtime.retired_worker_count == 0

    recovered = coordinator.submit_click(
        SnapClickIntent(PlotAction.ROUTE_POINT, (3.0, 4.0), (), False, False, 1),
        radius=2.0,
    )
    runtime.apply(recovered.commands)

    assert len(workers) == 2
    assert runtime.active_worker is workers[1]
    assert workers[1].click_requests == [recovered.commands[-1].request]
    runtime.close()
