"""Newest-only KLayout structure-bounds worker and query backend."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
import logging
from pathlib import Path
import threading
import time
from typing import Any

from PySide6.QtCore import QObject, Qt, Signal, Slot

from .klayout_types import (
    Box2D,
    KLayoutConfig,
    Point2D,
    StructureBoundsFailure,
    StructureBoundsRequest,
    StructureBoundsResult,
    forward_rotate_point,
)
from .klayout_worker_runtime import (
    CREATOR_THREAD_ERROR,
    STOP_WORKER,
    shape_contours,
)


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _StructurePublication:
    kind: str
    value: object
    request_id: int | None
    generation: int | None


_RETIRED_STRUCTURE_BOUNDS_WORKERS: set[QObject] = set()


class KLayoutStructureBoundsWorker(QObject):
    """Query newest-only visible structure bounds on one daemon thread."""

    ready = Signal(object)
    failed = Signal(object)
    lifecycle_failed = Signal(str)
    finished = Signal()
    _publication_posted = Signal(object)
    _finished_posted = Signal()

    def __init__(self, parent=None, *, backend_factory=None) -> None:
        super().__init__(parent)
        self._creator_thread_id = threading.get_ident()
        self._backend_factory = backend_factory or _KLayoutStructureBoundsBackend
        self._condition = threading.Condition(threading.Lock())
        self._stop_requested = threading.Event()
        self._drop_publications = threading.Event()
        self._pending: StructureBoundsRequest | None = None
        self._latest_identity: tuple[int, int] | None = None
        self._stopping = False
        self._thread: threading.Thread | None = None
        self._finished_publication_posted = False
        self._thread_finished_event = threading.Event()
        self._publication_posted.connect(
            self._deliver_publication,
            Qt.ConnectionType.QueuedConnection,
        )
        self._finished_posted.connect(
            self._deliver_finished,
            Qt.ConnectionType.QueuedConnection,
        )

    def submit(self, request: StructureBoundsRequest) -> None:
        self._require_creator_thread()
        with self._condition:
            if self._stopping or self._stop_requested.is_set():
                return
            self._pending = request
            self._latest_identity = (request.request_id, request.generation)
            if self._thread is None:
                self._thread = threading.Thread(
                    target=self._run,
                    name="klayout-structure-bounds",
                    daemon=True,
                )
                self._thread.start()
            self._condition.notify_all()

    def stop(self, timeout_s: float = 1.0) -> None:
        self._require_creator_thread()
        deadline = time.monotonic() + max(0.0, float(timeout_s))
        with self._condition:
            self._stopping = True
            self._stop_requested.set()
            self._drop_publications.set()
            self._pending = None
            thread = self._thread
            self._condition.notify_all()
            post_finished = thread is None
        if post_finished:
            self._post_finished()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=max(0.0, deadline - time.monotonic()))
            if thread.is_alive():
                self._retire_until_finished()

    @property
    def is_finished(self) -> bool:
        return self._thread_finished_event.is_set()

    def _retire_until_finished(self) -> None:
        """Detach from a closing parent and retain until creator-thread finish."""
        if self.parent() is not None:
            self.setParent(None)
        _RETIRED_STRUCTURE_BOUNDS_WORKERS.add(self)

    def _run(self) -> None:
        backend = None
        try:
            backend = self._backend_factory()
            while True:
                request = self._take_pending()
                if request is STOP_WORKER:
                    return
                try:
                    result = backend.query(request)
                except Exception as exc:
                    self._post_publication(
                        "failed",
                        StructureBoundsFailure(
                            request.request_id,
                            request.generation,
                            f"{type(exc).__name__}: {exc}",
                        ),
                        request=request,
                    )
                    continue
                self._post_publication("ready", result, request=request)
        except Exception as exc:
            self._post_publication(
                "lifecycle_failed",
                f"{type(exc).__name__}: {exc}",
            )
        finally:
            if backend is not None:
                try:
                    backend.close()
                except Exception:
                    logger.exception("Unable to close KLayout structure backend")
            self._post_finished()

    def _take_pending(self):
        with self._condition:
            while self._pending is None and not self._stopping:
                self._condition.wait()
            if self._stopping:
                return STOP_WORKER
            request = self._pending
            self._pending = None
            return request

    def _require_creator_thread(self) -> None:
        if threading.get_ident() != self._creator_thread_id:
            raise RuntimeError(CREATOR_THREAD_ERROR)

    def _post_publication(
        self,
        kind: str,
        value: object,
        *,
        request: StructureBoundsRequest | None = None,
    ) -> None:
        if self._drop_publications.is_set():
            return
        self._publication_posted.emit(
            _StructurePublication(
                kind,
                value,
                None if request is None else request.request_id,
                None if request is None else request.generation,
            )
        )

    def _post_finished(self) -> None:
        with self._condition:
            if self._finished_publication_posted:
                return
            self._finished_publication_posted = True
            self._thread_finished_event.set()
        self._finished_posted.emit()

    @Slot(object)
    def _deliver_publication(self, publication: _StructurePublication) -> None:
        with self._condition:
            if self._stopping or self._drop_publications.is_set():
                return
            if publication.request_id is not None and (
                publication.request_id,
                publication.generation,
            ) != self._latest_identity:
                return
        getattr(self, publication.kind).emit(publication.value)

    @Slot()
    def _deliver_finished(self) -> None:
        self.finished.emit()
        if self in _RETIRED_STRUCTURE_BOUNDS_WORKERS:
            _RETIRED_STRUCTURE_BOUNDS_WORKERS.discard(self)
            self.deleteLater()


class _KLayoutStructureBoundsBackend:
    """Own a KLayout database and flatten visible shapes only on its worker."""

    def __init__(self) -> None:
        self._db: Any = None
        self._layout: Any = None
        self._path: Path | None = None
        self._source_load_id: str | None = None
        self._layer_indexes: dict[tuple[int, int], int] = {}

    def query(self, request: StructureBoundsRequest) -> StructureBoundsResult:
        if request.config is None:
            bounds = tuple(
                bound
                for polygon in request.fixture_polygons
                if (bound := _points_bounds(polygon)) is not None
            )
        else:
            self._ensure_config(request.config)
            bounds = self._visible_shape_bounds(request.config)
        return StructureBoundsResult(
            request_id=request.request_id,
            generation=request.generation,
            structure_bounds=bounds,
        )

    def _ensure_config(self, config: KLayoutConfig) -> None:
        if self._path == config.path and self._source_load_id == config.source_load_id:
            return
        self.close()
        import klayout.db as db

        layout = db.Layout()
        layout.read(str(config.path))
        self._db = db
        self._layout = layout
        self._layer_indexes = {
            (int(info.layer), int(info.datatype)): layout.layer(info)
            for info in layout.layer_infos()
        }
        self._path = config.path
        self._source_load_id = config.source_load_id

    def _visible_shape_bounds(self, config: KLayoutConfig) -> tuple[Box2D, ...]:
        top_cell = self._layout.cell(config.top_cell_name)
        if top_cell is None:
            raise RuntimeError(f"Top cell '{config.top_cell_name}' does not exist")
        result: list[Box2D] = []
        for layer_key in sorted(config.visible_layers):
            layer_index = self._layer_indexes.get(layer_key)
            if layer_index is None:
                continue
            iterator = top_cell.begin_shapes_rec(layer_index)
            try:
                while not iterator.at_end():
                    points = (
                        point
                        for contour, _closed in shape_contours(
                            iterator.shape(), iterator.dtrans(), self._db
                        )
                        for point in contour
                    )
                    source_bounds = _points_bounds(points)
                    if source_bounds is not None:
                        result.append(_forward_rotate_bounds(source_bounds, config))
                    iterator.next()
            finally:
                del iterator
        return tuple(result)

    def close(self) -> None:
        self._layout = None
        self._db = None
        self._path = None
        self._source_load_id = None
        self._layer_indexes = {}


def _points_bounds(points: Iterable[Any]) -> Box2D | None:
    normalized: list[Point2D] = []
    for point in points:
        try:
            normalized.append((float(point[0]), float(point[1])))
        except (TypeError, ValueError, IndexError):
            continue
    if not normalized:
        return None
    return (
        min(point[0] for point in normalized),
        min(point[1] for point in normalized),
        max(point[0] for point in normalized),
        max(point[1] for point in normalized),
    )


def _forward_rotate_bounds(bounds: Box2D, config: KLayoutConfig) -> Box2D:
    left, bottom, right, top = bounds
    corners = tuple(
        forward_rotate_point(point, config)
        for point in (
            (left, bottom),
            (left, top),
            (right, bottom),
            (right, top),
        )
    )
    return (
        min(point[0] for point in corners),
        min(point[1] for point in corners),
        max(point[0] for point in corners),
        max(point[1] for point in corners),
    )


__all__ = ["KLayoutStructureBoundsWorker"]
