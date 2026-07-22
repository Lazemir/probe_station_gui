"""Coalesced background persistence for the GUI coordinate-frame selection."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import threading
import time

from PySide6.QtCore import QObject, Qt, Signal, Slot

from probe_station_gui.settings.manager import SoftwareCoordinateSelectionSnapshot


@dataclass(frozen=True)
class SoftwareCoordinateSelectionSaveFailure:
    frame_id: str
    generation: int
    message: str


@dataclass(frozen=True)
class _Publication:
    kind: str
    value: object


PersistSelection = Callable[[SoftwareCoordinateSelectionSnapshot], bool | None]


class SoftwareCoordinateSelectionStoreWorker(QObject):
    """Creator-thread facade around one latest-value persistence thread."""

    saved = Signal(str)
    failed = Signal(object)
    finished = Signal()
    _publication_posted = Signal(object)
    _finished_posted = Signal()

    def __init__(
        self,
        persist_selection: PersistSelection,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        if not callable(persist_selection):
            raise TypeError("persist_selection must be callable.")
        self._creator_thread_id = threading.get_ident()
        self._persist_selection = persist_selection
        self._condition = threading.Condition(threading.Lock())
        self._pending_snapshot: SoftwareCoordinateSelectionSnapshot | None = None
        self._active = False
        self._stopping = False
        self._thread: threading.Thread | None = None
        self._drop_publications = threading.Event()
        self._drain_thread: threading.Thread | None = None
        self._publication_posted.connect(
            self._deliver_publication,
            Qt.ConnectionType.QueuedConnection,
        )
        self._finished_posted.connect(
            self.finished.emit,
            Qt.ConnectionType.QueuedConnection,
        )

    @property
    def is_idle(self) -> bool:
        with self._condition:
            return not self._active and self._pending_snapshot is None

    @property
    def drain_thread(self) -> threading.Thread | None:
        return self._drain_thread

    def publish(self, snapshot: SoftwareCoordinateSelectionSnapshot) -> None:
        self._require_creator_thread()
        if not isinstance(snapshot, SoftwareCoordinateSelectionSnapshot):
            raise TypeError("Selection store requires an immutable snapshot.")
        with self._condition:
            if self._stopping:
                return
            self._pending_snapshot = snapshot
            if self._thread is None:
                self._thread = threading.Thread(
                    target=self._run,
                    name="software-coordinate-selection-store",
                    daemon=True,
                )
                self._thread.start()
            self._condition.notify()

    def stop(self, timeout_s: float = 1.0) -> None:
        self._require_creator_thread()
        timeout = max(0.0, float(timeout_s))
        deadline = time.monotonic() + timeout
        with self._condition:
            self._stopping = True
            if timeout == 0.0:
                self._drop_publications.set()
            thread = self._thread
            self._condition.notify_all()
            post_finished = thread is None
        if post_finished and not self._drop_publications.is_set():
            self._finished_posted.emit()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=max(0.0, deadline - time.monotonic()))
            if thread.is_alive():
                self._drop_publications.set()
                self._start_drain_thread(thread)

    def _start_drain_thread(self, worker_thread: threading.Thread) -> None:
        drain = self._drain_thread
        if drain is not None and drain.is_alive():
            return
        drain = threading.Thread(
            target=worker_thread.join,
            name="software-coordinate-selection-store-drain",
            daemon=False,
        )
        self._drain_thread = drain
        drain.start()

    def _run(self) -> None:
        try:
            while True:
                snapshot = self._take_pending()
                if snapshot is None:
                    return
                try:
                    persisted = self._persist_selection(snapshot)
                    if persisted is not False:
                        self._post_publication("saved", snapshot.frame_id)
                except Exception as exc:
                    self._post_publication(
                        "failed",
                        SoftwareCoordinateSelectionSaveFailure(
                            snapshot.frame_id,
                            snapshot.generation,
                            f"{type(exc).__name__}: {exc}",
                        ),
                    )
                finally:
                    with self._condition:
                        self._active = False
                        self._condition.notify_all()
        finally:
            if not self._drop_publications.is_set():
                self._finished_posted.emit()

    def _take_pending(self) -> SoftwareCoordinateSelectionSnapshot | None:
        with self._condition:
            while self._pending_snapshot is None:
                if self._stopping:
                    return None
                self._condition.wait()
            snapshot = self._pending_snapshot
            self._pending_snapshot = None
            self._active = True
            return snapshot

    def _post_publication(self, kind: str, value: object) -> None:
        if not self._drop_publications.is_set():
            self._publication_posted.emit(_Publication(kind, value))

    @Slot(object)
    def _deliver_publication(self, publication: _Publication) -> None:
        if publication.kind == "saved" and isinstance(publication.value, str):
            self.saved.emit(publication.value)
        elif publication.kind == "failed":
            self.failed.emit(publication.value)

    def _require_creator_thread(self) -> None:
        if threading.get_ident() != self._creator_thread_id:
            raise RuntimeError(
                "Software coordinate selection store methods must be called "
                "from the creator thread."
            )


__all__ = [
    "SoftwareCoordinateSelectionSaveFailure",
    "SoftwareCoordinateSelectionStoreWorker",
]
