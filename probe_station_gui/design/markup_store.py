"""Background newest-only persistence for app-owned design markup."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import threading
import time
from typing import Protocol
import uuid

from PySide6.QtCore import QObject, Qt, Signal, Slot

from probe_station_gui.design.markup import (
    MarkupDocument,
    normalize_source_path,
)


@dataclass(frozen=True)
class StoreLoadResult:
    request_id: int
    source_path: str
    document: MarkupDocument | None


@dataclass(frozen=True)
class StoreSuccess:
    request_id: int
    operation: str
    source_path: str


@dataclass(frozen=True)
class MarkupStoreFailure:
    request_id: int
    operation: str
    source_path: str
    message: str


@dataclass(frozen=True)
class _StoreOperation:
    request_id: int
    operation: str
    source_path: str
    document: MarkupDocument | None = None


@dataclass(frozen=True)
class _Publication:
    kind: str
    value: object


class MarkupStoreBackend(Protocol):
    def load(self, source_path: str) -> MarkupDocument | None: ...

    def save(self, document: MarkupDocument) -> None: ...

    def delete(self, source_path: str) -> None: ...


class FilesystemMarkupStoreBackend:
    """Synchronous filesystem implementation used only by the store thread."""

    def __init__(self, *, root: str | os.PathLike[str] | None = None) -> None:
        self._root = Path(root) if root is not None else default_markup_root()

    def load(self, source_path: str) -> MarkupDocument | None:
        normalized_source = normalize_source_path(source_path)
        path = markup_path_for_source(normalized_source, root=self._root)
        if not path.exists():
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
        document = MarkupDocument.from_dict(payload)
        if document.source_path != normalized_source:
            raise ValueError("Markup source path does not match its sidecar name.")
        return document

    def save(self, document: MarkupDocument) -> None:
        path = markup_path_for_source(document.source_path, root=self._root)
        payload = json.dumps(
            document.to_dict(),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ).encode("utf-8")
        _write_atomic(path, payload)

    def delete(self, source_path: str) -> None:
        markup_path_for_source(source_path, root=self._root).unlink(missing_ok=True)


BackendFactory = Callable[[], MarkupStoreBackend]


class MarkupStoreWorker(QObject):
    """Creator-thread facade around one coalescing filesystem thread."""

    loaded = Signal(object)
    saved = Signal(object)
    deleted = Signal(object)
    failed = Signal(object)
    finished = Signal()
    _publication_posted = Signal(object)
    _finished_posted = Signal()

    def __init__(
        self,
        parent: QObject | None = None,
        *,
        backend_factory: BackendFactory | None = None,
    ) -> None:
        super().__init__(parent)
        self._creator_thread_id = threading.get_ident()
        self._backend_factory = backend_factory or FilesystemMarkupStoreBackend
        self._condition = threading.Condition(threading.Lock())
        self._pending_by_source: dict[str, _StoreOperation] = {}
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
            return not self._active and not self._pending_by_source

    @property
    def drain_thread(self) -> threading.Thread | None:
        """Non-daemon shutdown waiter, present only after a detached stop."""

        return self._drain_thread

    def load(self, request_id: int, source_path: str | os.PathLike[str]) -> None:
        self._submit(
            _StoreOperation(
                request_id=int(request_id),
                operation="load",
                source_path=normalize_source_path(source_path),
            )
        )

    def publish(self, request_id: int, document: MarkupDocument) -> None:
        self._submit(
            _StoreOperation(
                request_id=int(request_id),
                operation="save",
                source_path=document.source_path,
                document=document,
            )
        )

    def delete(self, request_id: int, source_path: str | os.PathLike[str]) -> None:
        self._submit(
            _StoreOperation(
                request_id=int(request_id),
                operation="delete",
                source_path=normalize_source_path(source_path),
            )
        )

    def stop(self, timeout_s: float = 1.0) -> None:
        """Drain the latest queued operation per source, then stop within a bound."""

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
            name="design-markup-store-drain",
            daemon=False,
        )
        self._drain_thread = drain
        drain.start()

    def _submit(self, operation: _StoreOperation) -> None:
        self._require_creator_thread()
        with self._condition:
            if self._stopping:
                return
            self._pending_by_source[operation.source_path] = operation
            if self._thread is None:
                self._thread = threading.Thread(
                    target=self._run,
                    name="design-markup-store",
                    daemon=True,
                )
                self._thread.start()
            self._condition.notify()

    def _run(self) -> None:
        try:
            backend = self._backend_factory()
            while True:
                operation = self._take_pending()
                if operation is None:
                    return
                try:
                    self._execute(backend, operation)
                except Exception as exc:
                    self._post_publication(
                        "failed",
                        MarkupStoreFailure(
                            request_id=operation.request_id,
                            operation=operation.operation,
                            source_path=operation.source_path,
                            message=f"{type(exc).__name__}: {exc}",
                        ),
                    )
                finally:
                    with self._condition:
                        self._active = False
                        self._condition.notify_all()
        finally:
            if not self._drop_publications.is_set():
                self._finished_posted.emit()

    def _take_pending(self) -> _StoreOperation | None:
        with self._condition:
            while not self._pending_by_source:
                if self._stopping:
                    return None
                self._condition.wait()
            source_path = next(iter(self._pending_by_source))
            operation = self._pending_by_source.pop(source_path)
            self._active = True
            return operation

    def _execute(
        self,
        backend: MarkupStoreBackend,
        operation: _StoreOperation,
    ) -> None:
        if operation.operation == "load":
            document = backend.load(operation.source_path)
            self._post_publication(
                "loaded",
                StoreLoadResult(
                    operation.request_id,
                    operation.source_path,
                    document,
                ),
            )
            return
        if operation.operation == "save":
            if operation.document is None:
                raise ValueError("Save operation has no markup document.")
            backend.save(operation.document)
            self._post_publication(
                "saved",
                StoreSuccess(
                    operation.request_id,
                    operation.operation,
                    operation.source_path,
                ),
            )
            return
        if operation.operation == "delete":
            backend.delete(operation.source_path)
            self._post_publication(
                "deleted",
                StoreSuccess(
                    operation.request_id,
                    operation.operation,
                    operation.source_path,
                ),
            )
            return
        raise ValueError(f"Unknown markup store operation {operation.operation!r}.")

    def _post_publication(self, kind: str, value: object) -> None:
        if self._drop_publications.is_set():
            return
        self._publication_posted.emit(_Publication(kind, value))

    @Slot(object)
    def _deliver_publication(self, publication: _Publication) -> None:
        signal = {
            "loaded": self.loaded,
            "saved": self.saved,
            "deleted": self.deleted,
            "failed": self.failed,
        }.get(publication.kind)
        if signal is not None:
            signal.emit(publication.value)

    def _require_creator_thread(self) -> None:
        if threading.get_ident() != self._creator_thread_id:
            raise RuntimeError(
                "Markup store methods must be called from the creator thread."
            )


def default_markup_root() -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / "ProbeStationGUI" / "Markup"
    return Path.home() / "AppData" / "Local" / "ProbeStationGUI" / "Markup"


def markup_path_for_source(
    source_path: str | os.PathLike[str],
    *,
    root: str | os.PathLike[str] | None = None,
) -> Path:
    normalized = normalize_source_path(source_path)
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    directory = Path(root) if root is not None else default_markup_root()
    return directory / f"{digest}.json"


def _write_atomic(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.stem}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_bytes(payload)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


__all__ = [
    "FilesystemMarkupStoreBackend",
    "MarkupStoreFailure",
    "MarkupStoreWorker",
    "StoreLoadResult",
    "StoreSuccess",
    "default_markup_root",
    "markup_path_for_source",
]
