"""Qt worker adapter for the Design snap coordinator."""

from __future__ import annotations

from collections.abc import Callable, Iterable
import threading
from time import perf_counter
import weakref

from PySide6.QtCore import QObject, Signal

from probe_station_gui.design.klayout_workers import KLayoutSnapWorker
from probe_station_gui.design.snap_coordinator import (
    AttachWorker,
    CancelHover,
    CancelPending,
    DetachWorker,
    ReplaceWorker,
    SnapCommand,
    SubmitClick,
    SubmitHover,
    WorkerFailureEvent,
    WorkerLifecycleFailedEvent,
    WorkerResponseEvent,
)


WorkerFactory = Callable[[], object]
_RETIRED_SNAP_WORKERS: set[object] = set()
_RETIREMENT_CALLBACKS: dict[object, object] = {}


class DesignSnapRuntime(QObject):
    """Execute typed worker commands and publish typed terminal events."""

    event_ready = Signal(object)
    geometry_ready = Signal(int, object, object, object)

    def __init__(
        self,
        parent: QObject | None = None,
        *,
        worker_factory: WorkerFactory | None = None,
    ) -> None:
        super().__init__(parent)
        self._worker_factory = worker_factory or KLayoutSnapWorker
        self._active_worker: object | None = None
        self._active_worker_token = 0
        self._worker_callbacks: dict[str, object] = {}
        self._owned_retired_workers: weakref.WeakSet[object] = weakref.WeakSet()
        self._closed = False

    @property
    def active_worker(self) -> object | None:
        return self._active_worker

    @property
    def active_worker_token(self) -> int:
        return self._active_worker_token

    @property
    def retired_worker_count(self) -> int:
        return sum(
            worker in _RETIRED_SNAP_WORKERS
            for worker in self._owned_retired_workers
        )

    def apply(self, commands: Iterable[SnapCommand]) -> None:
        if self._closed:
            return
        for command in commands:
            if isinstance(command, AttachWorker):
                self._attach(command.worker_token, command.source_key)
            elif isinstance(command, ReplaceWorker):
                self._retire_active(timeout_s=command.timeout_s)
                self._attach(command.worker_token, command.source_key)
            elif isinstance(command, DetachWorker):
                if command.worker_token == self._active_worker_token:
                    self._retire_active(timeout_s=command.timeout_s)
            elif isinstance(command, SubmitHover):
                worker = self._matching_worker(command.worker_token)
                if worker is not None:
                    worker.submit_hover(command.request)
            elif isinstance(command, SubmitClick):
                worker = self._matching_worker(command.worker_token)
                if worker is not None:
                    worker.submit_click(command.request)
            elif isinstance(command, CancelHover):
                worker = self._matching_worker(command.worker_token)
                if worker is not None:
                    worker.cancel_hover()
            elif isinstance(command, CancelPending):
                worker = self._matching_worker(command.worker_token)
                if worker is not None:
                    worker.cancel_pending()

    def build_geometry(self, generation: int, document: object) -> None:
        if self._closed:
            return
        runtime_ref = weakref.ref(self)

        def build() -> None:
            started = perf_counter()
            try:
                geometry = document.build_snap_geometry()
            except Exception as exc:
                runtime = runtime_ref()
                if runtime is not None and not runtime._closed:
                    runtime.geometry_ready.emit(generation, document, None, exc)
                return
            runtime = runtime_ref()
            if runtime is not None and not runtime._closed:
                runtime.geometry_ready.emit(
                    generation,
                    document,
                    geometry,
                    (perf_counter() - started) * 1000.0,
                )

        threading.Thread(
            target=build,
            name="design-snap-geometry",
            daemon=True,
        ).start()

    def close(self) -> None:
        if self._closed:
            return
        self._retire_active(timeout_s=0.0)
        self._closed = True

    def _attach(
        self,
        worker_token: int,
        source_key: tuple[object, str | None],
    ) -> None:
        if self._active_worker is not None:
            self._retire_active(timeout_s=0.0)
        worker = self._worker_factory()
        runtime_ref = weakref.ref(self)

        def response_ready(value: object, *, token: int = worker_token) -> None:
            runtime = runtime_ref()
            if runtime is not None and runtime._active_worker_token == token:
                runtime.event_ready.emit(WorkerResponseEvent(token, value))

        def failed(value: object, *, token: int = worker_token) -> None:
            runtime = runtime_ref()
            if runtime is not None and runtime._active_worker_token == token:
                runtime.event_ready.emit(WorkerFailureEvent(token, value))

        def lifecycle_failed(message: str, *, token: int = worker_token) -> None:
            runtime = runtime_ref()
            if runtime is not None and runtime._active_worker_token == token:
                runtime.event_ready.emit(
                    WorkerLifecycleFailedEvent(token, str(message))
                )

        worker.snap_ready.connect(response_ready)
        worker.failed.connect(failed)
        lifecycle_signal = getattr(worker, "lifecycle_failed", None)
        if lifecycle_signal is not None:
            lifecycle_signal.connect(lifecycle_failed)
        self._active_worker = worker
        self._active_worker_token = int(worker_token)
        self._worker_callbacks = {
            "response": response_ready,
            "failed": failed,
            "lifecycle": lifecycle_failed,
        }

    def _matching_worker(self, worker_token: int) -> object | None:
        if int(worker_token) != self._active_worker_token:
            return None
        return self._active_worker

    def _retire_active(self, *, timeout_s: float) -> None:
        worker = self._active_worker
        callbacks = self._worker_callbacks
        self._active_worker = None
        self._active_worker_token = 0
        self._worker_callbacks = {}
        if worker is None:
            return
        _disconnect(getattr(worker, "snap_ready", None), callbacks.get("response"))
        _disconnect(getattr(worker, "failed", None), callbacks.get("failed"))
        _disconnect(
            getattr(worker, "lifecycle_failed", None),
            callbacks.get("lifecycle"),
        )
        finished = getattr(worker, "finished", None)
        if finished is None:
            worker.stop(timeout_s=timeout_s)
            worker.deleteLater()
            return
        self._owned_retired_workers.add(worker)
        _RETIRED_SNAP_WORKERS.add(worker)

        def finalize(retired: object = worker) -> None:
            callback = _RETIREMENT_CALLBACKS.pop(retired, None)
            _disconnect(getattr(retired, "finished", None), callback)
            _RETIRED_SNAP_WORKERS.discard(retired)
            retired.deleteLater()

        _RETIREMENT_CALLBACKS[worker] = finalize
        finished.connect(finalize)
        worker.stop(timeout_s=timeout_s)
        if bool(getattr(worker, "is_finished", False)):
            finalize()


def _disconnect(signal: object | None, callback: object | None) -> None:
    if signal is None or callback is None:
        return
    try:
        signal.disconnect(callback)
    except (RuntimeError, TypeError):
        pass


__all__ = ["DesignSnapRuntime"]
